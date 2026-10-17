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
# Then each office PC is told the name lives at the LAN address: once on the
# router (a "local DNS" / "static DNS" entry), or per Windows PC with
#   .\fix-name-windows.ps1 -Name <name>.ts.net
# Those PCs get the clean padlock, a page ten times faster, and no dependence
# on the ISP's route to the relay. Everyone outside the office is unaffected.

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
    "$APP_DIR/deploy/enable-https.sh" || die "enable-https.sh failed"
    note "off. Remove the router entry / run fix-name-windows.ps1 -Undo -Name <name> on the PCs."
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

LAN_IP="$(grep -oP 'listen\s+\K(?!127\.)[0-9.]+(?=:443 ssl)' /etc/nginx/sites-available/gate-pass | head -1)"
cat <<DONE

    ------------------------------------------------------------------
    The server now answers  https://$NAME  on ${LAN_IP:-the LAN address}.

    Last step: tell the office PCs where the name lives. EITHER

      * on the router, once, for every machine: add a local / static DNS
        entry   $NAME  ->  ${LAN_IP:-the LAN address}

      * or on each Windows PC, in an Administrator PowerShell:
            .\\fix-name-windows.ps1 -Name $NAME
        (download it from  http://${LAN_IP:-the LAN address}/fix-name-windows.ps1)

    Do NOT do the per-PC step on a laptop that leaves the office: away
    from the building that address is not the server, and the link would
    stop working for it there. Laptops should use Tailscale instead.
    ------------------------------------------------------------------

DONE
