#!/usr/bin/env bash
# Is the public link actually reachable BY SOMEBODY WITHOUT TAILSCALE, and put
# it back if it is not.
#
#   sudo deploy/watch-public-link.sh           # check once, repair if broken
#   sudo deploy/watch-public-link.sh --check   # check only, change nothing
#   sudo deploy/watch-public-link.sh --install # run it every 5 minutes for ever
#
# WHY THIS EXISTS
# ---------------------------------------------------------------------------
# The link has gone down twice, and both times it looked perfectly healthy from
# the office. That is not bad luck, it is the shape of the problem:
#
#   * `tailscale serve status` prints the entry, correct and marked public,
#     while the ingress is refusing to serve it.
#   * Opening the URL from any machine on the tailnet WORKS, because MagicDNS
#     resolves the name to the tailnet address and never touches the public
#     path at all.
#
# So the only check worth running is the one this does: resolve the name on
# PUBLIC DNS, force curl at that address, and see what a laptop in a warehouse
# with no Tailscale would actually get. Checking it any other way is how a dead
# link stays up on the board for a week.
#
# WHAT IT REPAIRS
# The failure mode seen both times is a serve config carrying entries for
# hostnames this node no longer answers to, which the ingress cannot make sense
# of. clean-tailnet-serve.sh --fix rebuilds it from the current name; this
# calls that, then re-checks from the outside before claiming success.

set -uo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FRONT_DOOR="http://127.0.0.1:8091"
MODE="${1:-}"

log()  { printf '%s  %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"; }
die()  { log "ERROR: $*"; exit 1; }

[ "$(id -u)" -eq 0 ] || die "run with sudo"

# ---------------------------------------------------------------- install ----
if [ "$MODE" = "--install" ]; then
    # Rendered from template files beside this script, not written inline.
    # A systemd unit body sitting in a shell heredoc is what broke install.sh:
    # one careless edit matched the delimiter on the opening line, and thirty
    # lines of unit file became shell commands. Keeping the bodies in their own
    # files means there is nothing here to orphan.
    for unit in gate-pass-linkwatch.service gate-pass-linkwatch.timer; do
        [ -f "$APP_DIR/deploy/$unit" ] || die "missing $APP_DIR/deploy/$unit"
        sed "s|__APP_DIR__|$APP_DIR|g" "$APP_DIR/deploy/$unit" \
            > "/etc/systemd/system/$unit" || die "could not write $unit"
    done

    systemctl daemon-reload
    systemctl enable --now gate-pass-linkwatch.timer
    log "installed — checking every 5 minutes"
    log "  journalctl -u gate-pass-linkwatch -f     to watch it"
    log "  systemctl list-timers gate-pass-linkwatch"
    exit 0
fi

# ------------------------------------------------------------------ check ----
read_name() {
    tailscale status --json 2>/dev/null \
      | python3 -c 'import json,sys; print((json.load(sys.stdin).get("Self") or {}).get("DNSName","").rstrip("."))' 2>/dev/null
}

NAME="$(read_name)"
if [ -z "$NAME" ]; then
    # tailscaled itself is not answering. Worth one restart before giving up:
    # it is a daemon like any other, it can die, and when it does the public
    # link is gone with it. Giving up here would mean the watchdog goes quiet
    # in exactly the situation it exists for.
    log "tailscaled is not answering — restarting it"
    systemctl restart tailscaled >/dev/null 2>&1
    sleep 10
    NAME="$(read_name)"
fi
[ -n "$NAME" ] || die "tailscaled is still not answering after a restart — this needs a person"

# The slow fuse, checked before anything else because it is the one failure
# here that nothing on this machine can repair.
#
# A Tailscale node key expires — 180 days by default. When it does, this
# machine drops off the tailnet, Funnel stops, and the public link dies. There
# is no warning, it happens months after anyone last touched the setup, and the
# repair below cannot fix it: re-authenticating needs a person and the admin
# console. So it gets said loudly, and early, while there is still time to do
# the one-click fix:
#
#   Tailscale admin console -> Machines -> this machine -> ... -> Disable key expiry
#
# That is the correct setting for a server that is meant to stay reachable.
EXPIRY_DAYS="$(tailscale status --json 2>/dev/null | python3 -c '
import json, sys
from datetime import datetime, timezone
self_ = (json.load(sys.stdin).get("Self") or {})
raw = self_.get("KeyExpiry")
if not raw:
    print("never")                      # expiry disabled, which is what we want
else:
    when = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    print((when - datetime.now(timezone.utc)).days)
' 2>/dev/null)"

case "$EXPIRY_DAYS" in
    never|"")   [ "$EXPIRY_DAYS" = "never" ] && log "key expiry is disabled — good, the link cannot die that way" ;;
    *)
        if [ "$EXPIRY_DAYS" -lt 0 ] 2>/dev/null; then
            log "THE NODE KEY HAS EXPIRED. The link is down and no script can fix it."
            log "  Tailscale admin console -> Machines -> $NAME -> Disable key expiry,"
            log "  then on this server:  sudo tailscale up"
        elif [ "$EXPIRY_DAYS" -lt 90 ] 2>/dev/null; then
            log "WARNING: the node key expires in ${EXPIRY_DAYS} days. When it does, the"
            log "  public link stops and this watchdog CANNOT repair it. Fix it now:"
            log "  admin console -> Machines -> $NAME -> ... -> Disable key expiry"
        fi
        ;;
esac

# PUBLIC dns, deliberately. The system resolver here is MagicDNS, which would
# hand back the tailnet address and make a dead public link look alive.
ADDRS="$(dig +short @1.1.1.1 "$NAME" A 2>/dev/null | grep -E '^[0-9.]+$' | head -4)"
[ -n "$ADDRS" ] || die "$NAME does not resolve on public DNS — Funnel is not published at all"

probe() {
    local ip="$1"
    curl -s -o /dev/null -w '%{http_code}' --max-time 20 \
         --resolve "$NAME:443:$ip" "https://$NAME/login" 2>/dev/null
}

reachable=0
for ip in $ADDRS; do
    code="$(probe "$ip")"
    case "$code" in
        200|301|302|401|403) log "OK   via $ip -> $code"; reachable=1 ;;
        *)                   log "DOWN via $ip -> ${code:-no answer}" ;;
    esac
done

if [ "$reachable" = "1" ]; then
    log "the public link is reachable without Tailscale"
    exit 0
fi

log "NOT reachable from outside — every public address failed"
[ "$MODE" = "--check" ] && { log "--check given, nothing repaired"; exit 1; }

# ----------------------------------------------------------------- repair ----
log "repairing: rebuilding the serve config for $NAME"
"$APP_DIR/deploy/clean-tailnet-serve.sh" --fix >/dev/null 2>&1 \
    || log "the rebuild reported a problem, re-checking anyway"

# Re-publish explicitly, in case there was nothing left to rebuild from.
tailscale funnel --bg --https=443 "$FRONT_DOOR" >/dev/null 2>&1 \
    || log "could not re-publish the funnel"

sleep 10
ADDRS="$(dig +short @1.1.1.1 "$NAME" A 2>/dev/null | grep -E '^[0-9.]+$' | head -4)"
for ip in $ADDRS; do
    code="$(probe "$ip")"
    case "$code" in
        200|301|302|401|403) log "REPAIRED — $ip now answers $code"; exit 0 ;;
    esac
done

log "STILL DOWN after repair. This needs a person:"
log "  sudo $APP_DIR/deploy/diagnose-network.sh"
exit 1
