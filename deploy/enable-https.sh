#!/usr/bin/env bash
# Turn on HTTPS for the gate pass site.
#
#   sudo deploy/enable-https.sh
#
# Issues the certificate if there isn't one, installs the TLS nginx site, and
# tells the app it is behind HTTPS so the session cookie becomes Secure. Safe to
# run again — it reissues nothing that already exists and reloads rather than
# restarts.
#
# Reversible: deploy/disable-https.sh puts the plain-HTTP site back.

set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CERT_DIR="$APP_DIR/deploy/certs"
UNIT=/etc/systemd/system/gate-pass.service

say()  { printf "\n\033[1m==> %s\033[0m\n" "$*"; }
note() { printf "    %s\n" "$*"; }
die()  { printf "\n\033[31mERROR: %s\033[0m\n" "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "run with sudo:  sudo deploy/enable-https.sh"
[ -f "$UNIT" ] || die "the app is not installed — run deploy/install.sh first"

say "Certificate"
if [ -f "$CERT_DIR/server.pem" ] && [ -f "$CERT_DIR/server.key" ]; then
    note "already present; reissue with deploy/make-cert.sh if the address changed"
else
    "$APP_DIR/deploy/make-cert.sh" >/dev/null
    note "issued — install $CERT_DIR/fanzart-ca.pem on the office machines"
fi
# nginx runs its master as root, so root-only keys are correct here.
chmod 600 "$CERT_DIR/server.key"

say "nginx"
sed -e "s|__CERT_DIR__|$CERT_DIR|g" -e "s|__APP_DIR__|$APP_DIR|g" \
    "$APP_DIR/deploy/nginx-gate-pass-ssl.conf" > /etc/nginx/sites-available/gate-pass
ln -sf /etc/nginx/sites-available/gate-pass /etc/nginx/sites-enabled/gate-pass
nginx -t >/dev/null 2>&1 || { nginx -t; die "nginx rejected the config"; }
systemctl reload nginx
note "port 80 now redirects to 443"

say "The app"
# Without this the session cookie stays non-Secure, which works but sends the
# cookie over plain HTTP if anyone ever reaches the site that way. With it set
# before TLS is actually live, nobody can sign in at all — so it goes on only
# now, after nginx is confirmed serving HTTPS.
if grep -q 'GATE_PASS_HTTPS=1' "$UNIT"; then
    note "already told the app it is behind HTTPS"
else
    sed -i 's|^# *Environment="GATE_PASS_HTTPS=1"|Environment="GATE_PASS_HTTPS=1"|' "$UNIT"
    grep -q 'Environment="GATE_PASS_HTTPS=1"' "$UNIT" || \
        sed -i '/^ExecStart=/i Environment="GATE_PASS_HTTPS=1"' "$UNIT"
    systemctl daemon-reload
    systemctl restart gate-pass
    note "session cookie is now HTTPS-only"
fi

say "Checking"
sleep 2
code=$(curl -sk -o /dev/null -w '%{http_code}' --max-time 10 https://127.0.0.1/login)
[ "$code" = "200" ] || die "https://127.0.0.1/login returned $code
       journalctl -u gate-pass -n 30 --no-pager"
note "https://127.0.0.1/login -> 200"

redirect=$(curl -s -o /dev/null -w '%{http_code} %{redirect_url}' --max-time 8 http://127.0.0.1/login)
note "http://127.0.0.1/login -> $redirect"

for asset in /static/css/style.css /static/img/fanzart-logo.png; do
    a=$(curl -sk -o /dev/null -w '%{http_code}' --max-time 8 "https://127.0.0.1$asset")
    [ "$a" = "200" ] || die "$asset returned $a over HTTPS"
done
note "static files serve over HTTPS"

# Anything else nginx was serving must keep serving.
for other in $(find /etc/nginx/sites-enabled -maxdepth 1 ! -name 'gate-pass' -type l -printf '%f ' 2>/dev/null); do
    port=$(grep -oP 'listen\s+\K[0-9]+' "/etc/nginx/sites-enabled/$other" 2>/dev/null | head -1)
    [ -n "$port" ] || continue
    curl -sf --max-time 5 -o /dev/null "http://127.0.0.1:$port/" \
        && note "$other still serving on :$port" \
        || note "WARNING: $other on :$port did not answer"
done

IP=$(hostname -I | awk '{print $1}')
cat <<DONE

    The site is now at:
        https://$(hostname).local
        https://$IP

    Browsers will warn until deploy/certs/fanzart-ca.pem is installed on each
    machine — see the notes from deploy/make-cert.sh. The connection is
    encrypted either way.

    To undo:  sudo deploy/disable-https.sh

DONE
