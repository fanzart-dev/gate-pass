#!/usr/bin/env bash
# Give this machine a better name on the tailnet, without losing what it serves.
#
#   sudo deploy/rename-tailnet.sh gatepass
#   sudo deploy/rename-tailnet.sh pass
#
# A Tailscale address is <machine name>.<tailnet>.ts.net. There are no
# subdomains to hand out — the only way to reach https://gatepass.<tailnet>
# is for the machine itself to be called `gatepass`.
#
# WHY THIS IS A SCRIPT AND NOT ONE COMMAND
# -------------------------------------------------------------------------
# Every `tailscale serve` entry is keyed by the FULL hostname. Rename the
# machine and every entry is orphaned: it stays in the config, pointing at a
# name that no longer resolves, and the site is simply gone. That is not a
# hypothesis — this tailnet still carries dead entries for
# fanzart-server.tailaf7188.ts.net from the last rename, and both FanIQ and the
# portfolio site disappeared with it until somebody noticed.
#
# So this records what is being served, renames, and puts every entry back
# under the new name with the same ports and the same public/private setting.
# It checks each one answers afterwards, and tells you how to undo it.

set -uo pipefail

NEW_NAME="${1:-}"
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

say()  { printf "\n\033[1m==> %s\033[0m\n" "$*"; }
note() { printf "    %s\n" "$*"; }
die()  { printf "\n\033[31mERROR: %s\033[0m\n" "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "run with sudo:  sudo deploy/rename-tailnet.sh <name>"
[ -n "$NEW_NAME" ] || die "give it a name:  sudo deploy/rename-tailnet.sh gatepass"
# Tailscale lowercases and strips; refuse anything that would not survive that,
# rather than ending up on a name nobody asked for.
printf '%s' "$NEW_NAME" | grep -qE '^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$' \
  || die "a machine name is lower-case letters, digits and hyphens: '$NEW_NAME' is not"
command -v tailscale >/dev/null || die "tailscale is not installed here"

OLD_NAME="$(tailscale status --json | python3 -c \
  'import json,sys; print((json.load(sys.stdin).get("Self") or {}).get("DNSName","").rstrip("."))')"
SUFFIX="$(tailscale status --json | python3 -c \
  'import json,sys; print(json.load(sys.stdin).get("MagicDNSSuffix",""))')"
[ -n "$OLD_NAME" ] && [ -n "$SUFFIX" ] || die "could not read this machine's tailnet name"
TARGET="$NEW_NAME.$SUFFIX"
[ "$OLD_NAME" = "$TARGET" ] && { note "already called $TARGET"; exit 0; }

say "What is served today"
# Only entries under the CURRENT name are ours to move. Anything left over from
# an earlier rename already points at a dead name and is not worth carrying.
PLAN="$(tailscale serve status --json | OLD="$OLD_NAME" python3 -c '
import json, os, sys
cfg = json.load(sys.stdin)
old = os.environ["OLD"]
for host, entry in sorted((cfg.get("Web") or {}).items()):
    name, _, port = host.rpartition(":")
    if name != old:
        continue
    public = "yes" if (cfg.get("AllowFunnel") or {}).get(host) else "no"
    for path, handler in sorted((entry.get("Handlers") or {}).items()):
        target = handler.get("Proxy")
        if target and path == "/":
            print(port, public, target)
')"
[ -n "$PLAN" ] || die "nothing is served under $OLD_NAME — check 'tailscale serve status'"
while read -r port public target; do
    note "port $port -> $target   (public: $public)"
done <<< "$PLAN"

say "Renaming $OLD_NAME -> $TARGET"
tailscale set --hostname="$NEW_NAME" || die "the rename was refused"

# The control plane has to catch up before the new name means anything.
for _ in $(seq 1 30); do
    now="$(tailscale status --json | python3 -c \
      'import json,sys; print((json.load(sys.stdin).get("Self") or {}).get("DNSName","").rstrip("."))')"
    [ "$now" = "$TARGET" ] && break
    sleep 1
done
[ "$now" = "$TARGET" ] || die "the machine is still called '$now'.
       The name may be taken. Put it back with:
         sudo tailscale set --hostname=$(printf '%s' "$OLD_NAME" | cut -d. -f1)"
note "done"

say "Putting the services back under the new name"
# Same ports, same public/private setting. Nothing gains exposure it did not
# already have — a rename must not quietly publish something.
while read -r port public target; do
    if [ "$public" = "yes" ]; then
        tailscale funnel --bg --https="$port" "$target" >/dev/null 2>&1 \
            && note "https://$TARGET:$port -> $target (public)" \
            || note "FAILED to restore port $port -> $target"
    else
        tailscale serve --bg --https="$port" "$target" >/dev/null 2>&1 \
            && note "https://$TARGET:$port -> $target (tailnet only)" \
            || note "FAILED to restore port $port -> $target"
    fi
done <<< "$PLAN"

say "Re-pointing the redirect from the old plain-HTTP address"
# The nginx snippet names the old host, so without this the Tailscale IP would
# redirect to an address that no longer exists.
if [ -x "$APP_DIR/deploy/enable-public-link.sh" ]; then
    # NOT silenced. Hiding this output is how a failure here looked like a
    # success: the script rolled back, took port 443 with it, and the only
    # evidence was a link that no longer answered.
    "$APP_DIR/deploy/enable-public-link.sh" --replace \
        || note "WARNING: that failed — the checks below will say what survived"
fi

# enable-public-link.sh rolls back on failure, and its rollback removes the
# entry on 443. So confirm every port that was serving before the rename is
# still serving after it, and put back anything that went missing.
say "Making sure nothing was lost on the way"
CURRENT="$(tailscale serve status --json 2>/dev/null || echo '{}')"
while read -r port public target; do
    if printf '%s' "$CURRENT" | HOST="$TARGET:$port" python3 -c '
import json, os, sys
cfg = json.load(sys.stdin)
sys.exit(0 if os.environ["HOST"] in (cfg.get("Web") or {}) else 1)' 2>/dev/null; then
        note "port $port is still served"
    else
        note "port $port went missing — putting it back"
        if [ "$public" = "yes" ]; then
            tailscale funnel --bg --https="$port" "$target" >/dev/null 2>&1
        else
            tailscale serve --bg --https="$port" "$target" >/dev/null 2>&1
        fi
    fi
done <<< "$PLAN"

say "Checking"
sleep 3
fail=0
while read -r port public target; do
    url="https://$TARGET"; [ "$port" = "443" ] || url="$url:$port"
    code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 25 "$url/")
    case "$code" in
        200|301|302|401|403) note "$url -> $code" ;;
        *) note "WARNING: $url returned '${code:-nothing}'"; fail=1 ;;
    esac
done <<< "$PLAN"

cat <<DONE

    The machine is now $TARGET

    The gate pass link to hand out:
        https://$TARGET

    A fresh certificate is issued for the new name automatically; the first
    request to it can take a few seconds while that happens.

    ANY BOOKMARK ON $OLD_NAME IS NOW DEAD. That name is gone from DNS, and
    nothing redirects from it — Tailscale does not keep the old one.

    To undo:
        sudo deploy/rename-tailnet.sh $(printf '%s' "$OLD_NAME" | cut -d. -f1)

DONE
exit $fail
