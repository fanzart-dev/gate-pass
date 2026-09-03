#!/usr/bin/env bash
# Throw away serve entries for hostnames this machine no longer answers to.
#
#   sudo deploy/clean-tailnet-serve.sh          # show them, change nothing
#   sudo deploy/clean-tailnet-serve.sh --fix    # rebuild the config cleanly
#
# Every `tailscale serve` entry is keyed by a full hostname, and a rename does
# not remove the old ones — it strands them. This machine has been renamed
# twice, so the config accumulated six entries across three names, of which
# only two were ever going to work.
#
# That is not just untidy. Funnel registers ingress for the node, and a config
# naming hostnames the node does not own is a config the ingress cannot make
# sense of: the live entry was present, correct, marked public — and the URL
# timed out from the internet.
#
# So this rebuilds the config from scratch with only the entries belonging to
# the CURRENT name, preserving each one's port, target and public/private
# setting. Anything stranded under an old name is listed before it goes, so
# nothing disappears without being named.

set -uo pipefail

FIX=0; [ "${1:-}" = "--fix" ] && FIX=1

say()  { printf "\n\033[1m==> %s\033[0m\n" "$*"; }
note() { printf "    %s\n" "$*"; }
die()  { printf "\n\033[31mERROR: %s\033[0m\n" "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "run with sudo:  sudo deploy/clean-tailnet-serve.sh --fix"
command -v tailscale >/dev/null || die "tailscale is not installed here"

NAME="$(tailscale status --json 2>/dev/null \
  | python3 -c 'import json,sys; print((json.load(sys.stdin).get("Self") or {}).get("DNSName","").rstrip("."))')"
[ -n "$NAME" ] || die "could not read this machine's tailnet name — is tailscaled up?"
note "this machine answers to: $NAME"

RAW="$(tailscale serve status --json 2>/dev/null)" || die "could not read the serve config"

say "Entries that belong here"
KEEP="$(printf '%s' "$RAW" | NAME="$NAME" python3 -c '
import json, os, sys
cfg = json.load(sys.stdin); name = os.environ["NAME"]
for host, entry in sorted((cfg.get("Web") or {}).items()):
    hostname, _, port = host.rpartition(":")
    if hostname != name:
        continue
    public = "yes" if (cfg.get("AllowFunnel") or {}).get(host) else "no"
    for path, handler in sorted((entry.get("Handlers") or {}).items()):
        if path == "/" and handler.get("Proxy"):
            print(port, public, handler["Proxy"])
')"
if [ -z "$KEEP" ]; then
    note "none — there is nothing to rebuild from"
else
    while read -r port public target; do
        note "port $port -> $target   (public: $public)"
    done <<< "$KEEP"
fi

say "Entries stranded under an old name"
STRANDED="$(printf '%s' "$RAW" | NAME="$NAME" python3 -c '
import json, os, sys
cfg = json.load(sys.stdin); name = os.environ["NAME"]
for host, entry in sorted((cfg.get("Web") or {}).items()):
    hostname, _, port = host.rpartition(":")
    if hostname == name:
        continue
    for path, handler in sorted((entry.get("Handlers") or {}).items()):
        if path == "/" and handler.get("Proxy"):
            print(f"{host}{path} -> {handler[\"Proxy\"]}")
')"
if [ -z "$STRANDED" ]; then
    note "none — the config is already clean"
    [ "$FIX" = "0" ] && exit 0
else
    while read -r line; do note "$line"; done <<< "$STRANDED"
    note ""
    note "These name machines that do not exist. Nothing can reach them, and"
    note "they are why the live entry can be correct and still not serve."
fi

if [ "$FIX" = "0" ]; then
    printf "\n    Nothing was changed. Add --fix to rebuild the config.\n\n"
    exit 0
fi

[ -n "$KEEP" ] || die "refusing to reset: there is nothing under $NAME to put back.
       Set the service up first, e.g.
         sudo deploy/enable-public-link.sh --replace"

say "Rebuilding"
# reset is the only way to drop an entry whose hostname the CLI can no longer
# address — `serve --https=443 off` acts on the current name, which is exactly
# the one being kept.
tailscale serve reset || die "reset failed"
note "cleared"

while read -r port public target; do
    if [ "$public" = "yes" ]; then
        tailscale funnel --bg --https="$port" "$target" >/dev/null 2>&1 \
            && note "https://$NAME:$port -> $target (public)" \
            || note "FAILED to restore port $port -> $target"
    else
        tailscale serve --bg --https="$port" "$target" >/dev/null 2>&1 \
            && note "https://$NAME:$port -> $target (tailnet only)" \
            || note "FAILED to restore port $port -> $target"
    fi
done <<< "$KEEP"

say "Checking"
sleep 5
fail=0
while read -r port public target; do
    url="https://$NAME"; [ "$port" = "443" ] || url="$url:$port"
    code=""
    for attempt in 1 2 3 4 5 6; do
        code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 20 "$url/")
        case "$code" in 200|301|302|401|403) break ;; esac
        note "waiting for $url (attempt $attempt, got '${code:-nothing}')"
        sleep 5
    done
    case "$code" in
        200|301|302|401|403) note "$url -> $code" ;;
        *) note "STILL FAILING: $url returned '${code:-nothing}'"; fail=1 ;;
    esac
done <<< "$KEEP"

printf "\n"
[ "$fail" -eq 0 ] && note "The config is clean and everything answers." \
                  || note "Something is still not answering — see above."
exit $fail
