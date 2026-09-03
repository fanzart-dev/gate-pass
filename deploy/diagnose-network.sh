#!/usr/bin/env bash
# Why can this machine reach the gate pass when other machines cannot?
#
#   sudo deploy/diagnose-network.sh          # look, change nothing
#   sudo deploy/diagnose-network.sh --fix    # repair what is safe to repair
#
# Works through the three separate ways in, because they fail for completely
# different reasons and the fix for one does nothing for the others:
#
#   the public link   gatepass.<tailnet>.ts.net — Tailscale Funnel. Any device
#                     with internet, no VPN, no certificate. If this is broken
#                     nothing else matters, because it is the link people have.
#   the LAN name      <hostname>.local — mDNS, via avahi. Same subnet only, and
#                     plenty of routers and guest networks drop multicast.
#   the LAN address   https://<ip> — no name resolution at all. This is the
#                     fallback that works when the other two do not.

set -uo pipefail

FIX=0; [ "${1:-}" = "--fix" ] && FIX=1
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FRONT_DOOR="http://127.0.0.1:8091"

pass=0; fail=0; warn=0
ok()   { printf "  \033[32mOK  \033[0m  %s\n" "$1"; pass=$((pass+1)); }
bad()  { printf "  \033[31mBAD \033[0m  %s\n" "$1"; [ -n "${2:-}" ] && printf "        %s\n" "$2"; fail=$((fail+1)); }
soft() { printf "  \033[33mHMM \033[0m  %s\n" "$1"; [ -n "${2:-}" ] && printf "        %s\n" "$2"; warn=$((warn+1)); }
head_(){ printf "\n\033[1m%s\033[0m\n" "$1"; }

[ "$(id -u)" -eq 0 ] || { echo "run with sudo"; exit 1; }

LAN_IP="$(hostname -I | awk '{print $1}')"
HOST="$(hostname)"
TS_NAME="$(tailscale status --json 2>/dev/null \
  | python3 -c 'import json,sys; print((json.load(sys.stdin).get("Self") or {}).get("DNSName","").rstrip("."))' 2>/dev/null)"

head_ "The public link — the one people actually have"
if [ -z "$TS_NAME" ]; then
    soft "tailscaled is not up, so there is no public link"
else
    served="$(tailscale serve status --json 2>/dev/null \
      | HOST="$TS_NAME:443" python3 -c '
import json, os, sys
cfg = json.load(sys.stdin)
w = (cfg.get("Web") or {}).get(os.environ["HOST"]) or {}
h = (w.get("Handlers") or {}).get("/") or {}
pub = (cfg.get("AllowFunnel") or {}).get(os.environ["HOST"])
print(f"{h.get('Proxy') or ''}|{'yes' if pub else 'no'}")' 2>/dev/null)"
    target="${served%%|*}"; public="${served##*|}"

    if [ -z "$target" ]; then
        bad "nothing is served on https://$TS_NAME" \
            "This is the whole outage: the name resolves on public DNS, so a
        browser finds an address and then waits for a machine that never
        answers. Everything else below can be perfect and the link still dies."
        if [ "$FIX" = "1" ]; then
            tailscale funnel --bg --https=443 "$FRONT_DOOR" >/dev/null 2>&1 \
                && ok "restored: https://$TS_NAME -> $FRONT_DOOR" \
                || bad "could not restore it — is Funnel allowed for this node?"
        else
            printf "        FIX: sudo tailscale funnel --bg --https=443 %s\n" "$FRONT_DOOR"
        fi
    else
        ok "https://$TS_NAME -> $target"
        [ "$public" = "yes" ] \
          && ok "and it is public, so a machine without Tailscale can reach it" \
          || bad "it is tailnet-only — a laptop without Tailscale cannot open it" \
                 "sudo $APP_DIR/deploy/enable-public-link.sh --replace"
    fi
    code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 20 "https://$TS_NAME/login")
    [ "$code" = "200" ] \
      && ok "https://$TS_NAME/login answers 200" \
      || bad "https://$TS_NAME/login returned '${code:-nothing}'"
fi

head_ "The LAN name — $HOST.local"
# tailscale set --hostname changes the TAILNET name only. The OS hostname is
# what avahi advertises, so after a rename the two disagree and the .local
# address people were told to use is still the old one.
if [ -n "$TS_NAME" ] && [ "${TS_NAME%%.*}" != "$HOST" ]; then
    soft "the tailnet calls this machine '${TS_NAME%%.*}' but the OS calls it '$HOST'" \
         "So the LAN name is $HOST.local, NOT ${TS_NAME%%.*}.local. Renaming on
        the tailnet does not rename the machine. To make them match:
          sudo hostnamectl set-hostname ${TS_NAME%%.*}"
fi
systemctl is-active --quiet avahi-daemon \
  && ok "avahi-daemon is running, so $HOST.local is advertised" \
  || { bad "avahi-daemon is not running — nothing answers $HOST.local"
       [ "$FIX" = "1" ] && systemctl enable --now avahi-daemon && ok "started it"; }
if command -v avahi-resolve >/dev/null; then
    got="$(avahi-resolve -n "$HOST.local" 2>/dev/null | awk '{print $2}')"
    [ -n "$got" ] && ok "$HOST.local resolves here to $got" \
                  || soft "$HOST.local does not resolve even on this machine"
fi
soft "mDNS is same-subnet only, always" \
     "Wi-Fi guest networks, band steering and any router that drops multicast
        will break .local for some machines and not others. It is a convenience,
        never the address to standardise on."

head_ "The LAN address — the fallback that always works"
ok "https://$LAN_IP"
for port in 80 443; do
    if ss -lntH "( sport = :$port )" | grep -qE "0\.0\.0\.0:$port|\[::\]:$port|$LAN_IP:$port"; then
        ok "nginx is listening on $port for the LAN"
    else
        bad "nothing is listening on $port for the LAN" \
            "sudo $APP_DIR/deploy/enable-https.sh"
    fi
done
# Gunicorn stays on loopback on purpose.
ss -lntH '( sport = :8090 )' | grep -q '127.0.0.1' \
  && ok "gunicorn is on loopback only, which is correct — nginx is the way in" \
  || soft "gunicorn is not on 127.0.0.1:8090"

head_ "Firewall"
if ufw status 2>/dev/null | grep -q "Status: active"; then
    for port in 80 443; do
        ufw status | grep -q "$port/tcp" \
          && ok "ufw allows $port/tcp" \
          || { bad "ufw is active and $port/tcp is NOT allowed — this alone blocks every other machine"
               [ "$FIX" = "1" ] && ufw allow "$port/tcp" >/dev/null && ok "opened $port/tcp"; }
    done
else
    ok "ufw is not active, so it is not blocking anything"
fi

head_ "Speed"
grep -rq "gzip on" /etc/nginx/sites-enabled/gate-pass 2>/dev/null \
  && ok "compression is on" \
  || soft "no gzip — run sudo $APP_DIR/deploy/install.sh to pick it up"
grep -rq "keepalive_timeout" /etc/nginx/sites-enabled/gate-pass 2>/dev/null \
  && ok "keep-alive is set" \
  || soft "no keepalive_timeout — run sudo $APP_DIR/deploy/install.sh"
workers=$(grep -oP 'worker_connections\s+\K[0-9]+' /etc/nginx/nginx.conf 2>/dev/null | head -1)
[ "${workers:-0}" -ge 512 ] 2>/dev/null \
  && ok "worker_connections is $workers — ample for an office this size" \
  || soft "worker_connections is ${workers:-unset}"

cat <<DONE

  What to tell people
  ------------------------------------------------------------------
    Anywhere, any machine, no VPN:   https://${TS_NAME:-<not set up>}
    In the office, if that is slow:  https://$LAN_IP
    By name on the LAN:              https://$HOST.local   (same subnet only)

  Standardise on the first one. It does not care which subnet, which Wi-Fi
  band, or whether multicast is allowed — the three things that make .local
  work for one desk and not the next.

DONE
printf "\033[1m%d ok, %d to look at, %d broken\033[0m\n" "$pass" "$warn" "$fail"
exit $(( fail > 0 ? 1 : 0 ))
