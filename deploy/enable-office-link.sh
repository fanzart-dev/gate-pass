#!/usr/bin/env bash
# Make the public link work in the office even when the office internet line
# cannot reach Tailscale's relay.
#
#   sudo deploy/enable-office-link.sh           # set it up (safe to re-run)
#   sudo deploy/enable-office-link.sh --renew   # refresh the certificate (the timer does this daily)
#   sudo deploy/enable-office-link.sh --off     # take the office path away again
#
# WHAT WENT WRONG
# ---------------------------------------------------------------------------
# https://<name>.ts.net is published by Tailscale Funnel. A browser that is not
# on Tailscale reaches it through Tailscale's relay on the public internet, so
# a PC in the office goes office -> ISP -> relay -> ISP -> this server. On
# 17-10-2026 the office ISP stopped connecting to that relay at all (it had
# been dropping it for a few minutes a day all week), and every office PC
# without Tailscale got ERR_TIMED_OUT — while the same link worked from the
# rest of the world, and nothing on this server was wrong.
#
# WHAT THIS DOES
# Gives the name a second way in that never leaves the building:
#
#   1. Fetches the name's genuine Let's Encrypt certificate from tailscaled
#      (`tailscale cert`) — the same one the relay shows.
#   2. Has nginx answer the name on the LAN address with it (enable-https.sh
#      renders nginx-gate-pass-tailnet-name.conf once the certificate exists).
#   3. Renews it daily with a systemd timer.
#
#   4. Does the same for every other port the name carries through Funnel
#      (FanIQ on 8443, MailStream on 10000), so nothing under the name
#      breaks for an office PC that is sent here.
#   5. Runs a small DNS server on the LAN address (dnsmasq, already on the
#      machine) that answers the name with the LAN address and passes every
#      other lookup to the router, exactly as before.
#
# Then ONE change on the router, and no PC is touched: DHCP -> Primary DNS =
# this server, Secondary DNS = the router. Each PC picks it up when its lease
# renews (or at once on reconnect / restart), and gets the clean padlock, a
# page ten times faster, and no dependence on the ISP's route to the relay.
# If this server is ever off, PCs fall back to the secondary and everything
# but the link keeps working. Everyone outside the office is unaffected.

set -uo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TLS_DIR=/etc/gate-pass/tailnet-tls
MODE="${1:-}"

say()  { printf "\n\033[1m==> %s\033[0m\n" "$*"; }
note() { printf "    %s\n" "$*"; }
die()  { printf "\n\033[31mERROR: %s\033[0m\n" "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "run with sudo"

case "$MODE" in ""|--renew|--off) ;; *) die "unknown option: $MODE" ;; esac

if [ "$MODE" = "--off" ]; then
    say "Removing the office path"
    systemctl disable --now gate-pass-tailnet-cert.timer >/dev/null 2>&1
    rm -f /etc/systemd/system/gate-pass-tailnet-cert.service \
          /etc/systemd/system/gate-pass-tailnet-cert.timer
    systemctl daemon-reload
    # The name file is what enable-https.sh keys on; the certificate is left
    # so turning it back on is instant.
    rm -f "$TLS_DIR/name"
    # DNS first: put the router's DNS back BEFORE running --off, or PCs are
    # sent to a server that no longer answers the name.
    systemctl disable --now gate-pass-office-dns.service >/dev/null 2>&1
    rm -f /etc/systemd/system/gate-pass-office-dns.service
    systemctl daemon-reload
    rm -f /etc/nginx/sites-enabled/zz-gate-pass-office-link \
          /etc/nginx/sites-available/zz-gate-pass-office-link
    "$APP_DIR/deploy/enable-https.sh" || die "enable-https.sh failed"
    note "off. Set the router's DHCP DNS back to automatic, and undo any"
    note "fix-name-windows.ps1 -Name <name> on PCs (-Undo -Name <name>)."
    exit 0
fi

NAME="$(tailscale status --json 2>/dev/null \
        | python3 -c 'import json,sys; print((json.load(sys.stdin).get("Self") or {}).get("DNSName","").rstrip("."))' 2>/dev/null)"
[ -n "$NAME" ] || die "tailscaled is not answering — sudo systemctl status tailscaled"
[[ "$NAME" =~ ^[a-z0-9]([a-z0-9.-]*[a-z0-9])?$ ]] || die "tailscale reported an odd name: '$NAME'"

fetch() {
    # Written beside the live files and moved into place, so nginx never reads
    # a half-written key, and a failed fetch leaves the working pair alone.
    mkdir -p "$TLS_DIR" && chmod 755 "$TLS_DIR"
    local crt="$TLS_DIR/.new.crt" key="$TLS_DIR/.new.key" err
    if ! err="$(tailscale cert --cert-file "$crt" --key-file "$key" "$NAME" 2>&1)"; then
        rm -f "$crt" "$key"
        printf '%s\n' "$err" >&2
        return 1
    fi
    chmod 644 "$crt"; chmod 600 "$key"
    if cmp -s "$crt" "$TLS_DIR/$NAME.crt" && cmp -s "$key" "$TLS_DIR/$NAME.key"; then
        rm -f "$crt" "$key"
        echo same
    else
        mv -f "$key" "$TLS_DIR/$NAME.key"
        mv -f "$crt" "$TLS_DIR/$NAME.crt"
        echo new
    fi
}

if [ "$MODE" = "--renew" ]; then
    result="$(fetch)" || die "tailscale cert failed — the current certificate stays in use"
    expiry="$(openssl x509 -noout -enddate -in "$TLS_DIR/$NAME.crt" 2>/dev/null | cut -d= -f2)"
    if [ "$result" = "new" ]; then
        nginx -t >/dev/null 2>&1 || die "nginx rejected the config after renewal"
        systemctl reload nginx || die "nginx would not reload"
        note "renewed; valid until $expiry"
    else
        note "unchanged; valid until $expiry"
    fi
    exit 0
fi

grep -q '443 ssl' /etc/nginx/sites-available/gate-pass 2>/dev/null \
    || die "HTTPS is not on for the LAN yet — run sudo deploy/enable-https.sh first"

say "Certificate for $NAME"
fetch >/dev/null || die "tailscale cert failed. HTTPS certificates must be enabled for the
       tailnet (admin console -> DNS -> HTTPS Certificates); Funnel needs them, so
       if the public link has ever worked they already are."
printf '%s\n' "$NAME" > "$TLS_DIR/name"
note "valid until $(openssl x509 -noout -enddate -in "$TLS_DIR/$NAME.crt" | cut -d= -f2)"

say "Daily renewal"
for unit in gate-pass-tailnet-cert.service gate-pass-tailnet-cert.timer; do
    [ -f "$APP_DIR/deploy/$unit" ] || die "missing $APP_DIR/deploy/$unit"
    sed "s|__APP_DIR__|$APP_DIR|g" "$APP_DIR/deploy/$unit" \
        > "/etc/systemd/system/$unit" || die "could not write $unit"
done
systemctl daemon-reload
systemctl enable --now gate-pass-tailnet-cert.timer >/dev/null 2>&1 \
    || die "could not start the renewal timer"
note "systemctl list-timers gate-pass-tailnet-cert"

say "nginx"
"$APP_DIR/deploy/enable-https.sh" || die "enable-https.sh failed — see above"

# The office address: the one the default route leaves by, which is the
# address the router knows this server as.
GATEWAY="$(ip -4 route show default 2>/dev/null | awk '{print $3; exit}')"
LAN_IP="$(ip -4 route get "${GATEWAY:-192.168.1.1}" 2>/dev/null | grep -oP 'src \K[0-9.]+' | head -1)"
[ -n "$GATEWAY" ] && [ -n "$LAN_IP" ] || die "could not work out the LAN address and router"
grep -q "listen $LAN_IP:443 ssl" /etc/nginx/sites-available/gate-pass \
    || die "the gate pass site does not listen on $LAN_IP:443 — run sudo deploy/enable-https.sh"
note "LAN address $LAN_IP, router $GATEWAY"

say "The name's other ports"
# Read from tailscaled, Funnel ports only: a port that is tailnet-only stays
# tailnet-only, rather than being opened to the LAN by this script.
PORTS="$(tailscale serve status --json 2>/dev/null | NAME="$NAME" python3 -c '
import json, os, sys, re
name = os.environ["NAME"]
cfg = json.load(sys.stdin) or {}
funnel = cfg.get("AllowFunnel") or {}
for hostport, web in sorted((cfg.get("Web") or {}).items()):
    host, _, port = hostport.rpartition(":")
    if host != name or port == "443" or not funnel.get(hostport):
        continue
    target = ((web.get("Handlers") or {}).get("/") or {}).get("Proxy", "")
    if re.fullmatch(r"http://127\.0\.0\.1:\d+/?", target) and port.isdigit():
        print(port, target.rstrip("/"))
')"
SITE=/etc/nginx/sites-available/zz-gate-pass-office-link
cp -f "$SITE" "$SITE.bak" 2>/dev/null || rm -f "$SITE.bak"
{
    echo "# Written by $APP_DIR/deploy/enable-office-link.sh — re-run it, do not edit."
    echo "map \$http_upgrade \$gp_office_upgrade { default upgrade; \"\" \"\"; }"
} > "$SITE"
OFFICE_PORTS=""
while read -r port target; do
    [ -n "$port" ] || continue
    holder="$(ss -tlnpH "( sport = :$port )" 2>/dev/null | grep -E "($LAN_IP|0\.0\.0\.0|\*):$port " | grep -v 'users:(("nginx"')"
    if [ -n "$holder" ]; then
        note "skipping :$port — something other than nginx holds $LAN_IP:$port"
        continue
    fi
    sed -e "s|__LAN_IP__|$LAN_IP|g" -e "s|__PORT__|$port|g" -e "s|__TARGET__|$target|g" \
        -e "s|__TAILNET_NAME__|$NAME|g" -e "s|__TAILNET_TLS_DIR__|$TLS_DIR|g" \
        "$APP_DIR/deploy/nginx-gate-pass-office-ports.conf" >> "$SITE"
    OFFICE_PORTS="$OFFICE_PORTS $port"
    note ":$port -> $target"
done <<< "$PORTS"
ln -sf "$SITE" /etc/nginx/sites-enabled/zz-gate-pass-office-link
if ! nginx -t 2>/tmp/office-link-nginx.$$; then
    cat /tmp/office-link-nginx.$$ >&2; rm -f /tmp/office-link-nginx.$$
    if [ -f "$SITE.bak" ]; then cp -f "$SITE.bak" "$SITE"
    else rm -f "$SITE" /etc/nginx/sites-enabled/zz-gate-pass-office-link; fi
    die "nginx rejected the extra ports — put back as it was"
fi
rm -f /tmp/office-link-nginx.$$
systemctl reload nginx || die "nginx would not reload"
sleep 1
for port in 443 $OFFICE_PORTS; do
    code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 \
                --resolve "$NAME:$port:$LAN_IP" "https://$NAME:$port/")
    case "$code" in
        2*|3*|401|403|404) note "https://$NAME:$port on $LAN_IP -> $code, genuine certificate" ;;
        *) note "WARNING: https://$NAME:$port on $LAN_IP -> '${code:-no answer}'" ;;
    esac
done

say "Office DNS"
# Port 53 on the LAN address must be free, or ours. systemd-resolved holds it
# on 127.0.0.53 only, which is no conflict.
holder="$(ss -ulnpH "( sport = :53 )" 2>/dev/null | grep -E "($LAN_IP|0\.0\.0\.0|\*):53 " | grep -v 'dnsmasq')"
[ -z "$holder" ] || die "something else already answers DNS on $LAN_IP:53:
       $holder"
cat > /etc/gate-pass/office-dns.conf <<CONF
# Written by $APP_DIR/deploy/enable-office-link.sh — re-run it, do not edit.
# The office PCs' DNS once the router hands this server out as DNS.
listen-address=$LAN_IP
bind-dynamic
port=53
# Everything else goes to the router, which is what the PCs asked before.
no-resolv
no-hosts
server=$GATEWAY
# The public name, and anything under it, is this server. local= makes this
# server the authority for it, so the relay's IPv6 addresses (and HTTPS
# records) are not passed through as a way back to the relay.
local=/$NAME/
address=/$NAME/$LAN_IP
cache-size=2000
user=nobody
group=nogroup
pid-file=
CONF
sed "s|__APP_DIR__|$APP_DIR|g" "$APP_DIR/deploy/gate-pass-office-dns.service" \
    > /etc/systemd/system/gate-pass-office-dns.service || die "could not write the DNS unit"
systemctl daemon-reload
systemctl enable gate-pass-office-dns.service >/dev/null 2>&1
systemctl restart gate-pass-office-dns.service || die "the office DNS would not start:
       journalctl -u gate-pass-office-dns -n 20 --no-pager"
sleep 1
got="$(dig +short +time=3 +tries=1 @"$LAN_IP" "$NAME" A 2>/dev/null | head -1)"
other="$(dig +short +time=3 +tries=1 @"$LAN_IP" github.com A 2>/dev/null | head -1)"
[ "$got" = "$LAN_IP" ] || die "the office DNS answered '$got' for $NAME, not $LAN_IP"
[ -n "$other" ] || die "the office DNS does not pass other names on to the router"
note "$NAME -> $got, and other names still resolve (github.com -> $other)"

cat <<DONE

    ------------------------------------------------------------------
    The server now answers  https://$NAME  in the office, on
    $LAN_IP, ports 443$OFFICE_PORTS — and answers office DNS on $LAN_IP.

    LAST STEP — once, on the router ($GATEWAY), nothing on the PCs:

      DHCP Server settings:
          Primary DNS    $LAN_IP
          Secondary DNS  $GATEWAY
      Save.

    PCs pick it up as their address lease renews; a restart, or
    unplugging and reconnecting the network, does it at once.
    ------------------------------------------------------------------

DONE
