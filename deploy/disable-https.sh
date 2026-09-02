#!/usr/bin/env bash
# Put the plain-HTTP site back.
#
#   sudo deploy/disable-https.sh
#
# The way out if a certificate expires, the server's name changes, or HTTPS is
# keeping the office out of the app on a morning when passes need issuing. It
# leaves the certificates alone so re-enabling is one command.

set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UNIT=/etc/systemd/system/gate-pass.service

say()  { printf "\n\033[1m==> %s\033[0m\n" "$*"; }
note() { printf "    %s\n" "$*"; }
die()  { printf "\n\033[31mERROR: %s\033[0m\n" "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "run with sudo:  sudo deploy/disable-https.sh"

say "nginx"
sed -e "s|__APP_DIR__|$APP_DIR|g" \
    "$APP_DIR/deploy/nginx-gate-pass.conf" > /etc/nginx/sites-available/gate-pass
nginx -t >/dev/null 2>&1 || { nginx -t; die "nginx rejected the config"; }
systemctl reload nginx
note "serving plain HTTP on port 80 again"

say "The app"
# This MUST come off with the TLS. A Secure cookie is never sent over plain
# HTTP, so leaving it set means the sign-in form works and then silently fails
# to keep anyone signed in.
if grep -q '^Environment="GATE_PASS_HTTPS=1"' "$UNIT"; then
    sed -i 's|^Environment="GATE_PASS_HTTPS=1"|# Environment="GATE_PASS_HTTPS=1"|' "$UNIT"
    systemctl daemon-reload
    systemctl restart gate-pass
    note "session cookie no longer requires HTTPS"
else
    note "already off"
fi

say "Checking"
sleep 2
code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 http://127.0.0.1/login)
[ "$code" = "200" ] || die "http://127.0.0.1/login returned $code"
note "http://127.0.0.1/login -> 200"
echo
note "Certificates are untouched; sudo deploy/enable-https.sh turns TLS back on."
echo
