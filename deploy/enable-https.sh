#!/usr/bin/env bash
# Turn on HTTPS for the gate pass site.
#
#   sudo deploy/enable-https.sh
#
# Issues the certificate if there isn't one, installs the TLS nginx site, tells
# the app it is behind HTTPS so the session cookie becomes Secure, and then
# proves HTTPS actually answers before saying it is done. Safe to run again.
#
# Reversible: deploy/disable-https.sh puts the plain-HTTP site back.

set -uo pipefail          # NOT -e; see the note on checking, below

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
    "$APP_DIR/deploy/make-cert.sh" >/dev/null || die "could not issue a certificate"
    note "issued — install $CERT_DIR/fanzart-ca.pem on the office machines"
fi
# Set every run, not just when the certificate is issued. make-cert.sh only
# runs when there ISN'T one, so a permissions fix made there would never reach
# a machine that already had certificates — which is every machine that matters.
#
# The directory has to be traversable and the CA readable: it is a public
# certificate that every office machine needs a copy of, and nginx (as
# www-data) serves it at /fanzart-ca.pem. The private keys stay 600.
chmod 755 "$CERT_DIR"
chmod 644 "$CERT_DIR/fanzart-ca.pem" "$CERT_DIR/server.pem" 2>/dev/null || true
chmod 600 "$CERT_DIR/server.key" "$CERT_DIR/fanzart-ca.key" 2>/dev/null || true

say "Working out which addresses 443 is ours to use"
# `listen 443 ssl` means 0.0.0.0:443, and that bind FAILS if anything already
# holds 443 on any address — tailscaled does, for Funnel. nginx then rejects
# the whole config and silently keeps running the previous one, so the site
# stays on plain HTTP and nothing looks wrong. Hence: bind named addresses.
# Ignore nginx's OWN listeners. Re-running this after HTTPS is already on
# would otherwise find nginx holding 443 on exactly the addresses it is about
# to reuse, call them taken, and refuse — so the script worked once and then
# never again. A graceful reload also keeps old workers, and their sockets,
# alive for a moment, so even a fresh run can see them.
TAKEN="$(ss -tlnpH '( sport = :443 )' 2>/dev/null \
         | grep -v 'users:(("nginx"' \
         | awk '{print $4}' | sed 's/:443$//' | tr -d '[]')"
if [ -n "$TAKEN" ]; then
    note "held by something other than nginx: $(echo "$TAKEN" | tr '\n' ' ')"
fi

LISTEN_LINES=""
ADDRESSES=""
for addr in 127.0.0.1 $(hostname -I); do
    case "$addr" in
        *:*)      continue ;;                  # IPv6, skip — LAN clients use v4
        100.*)    note "skipping $addr (Tailscale holds 443 there)"; continue ;;
    esac
    if echo "$TAKEN" | grep -qx "$addr"; then
        note "skipping $addr (something else holds 443 there)"
        continue
    fi
    LISTEN_LINES="${LISTEN_LINES}    listen ${addr}:443 ssl;"$'\n'
    ADDRESSES="$ADDRESSES $addr"
done
[ -n "$LISTEN_LINES" ] || die "no address left to serve HTTPS on — everything is taken"
note "HTTPS will listen on:$ADDRESSES"

say "nginx"
cp -f /etc/nginx/sites-available/gate-pass /etc/nginx/sites-available/gate-pass.bak 2>/dev/null || true
LISTEN_LINES="$LISTEN_LINES" python3 - "$APP_DIR" "$CERT_DIR" <<'PY' > /etc/nginx/sites-available/gate-pass
import os, sys
app_dir, cert_dir = sys.argv[1], sys.argv[2]
template = open(f"{app_dir}/deploy/nginx-gate-pass-ssl.conf").read()
print(template.replace("__APP_DIR__", app_dir)
              .replace("__CERT_DIR__", cert_dir)
              .replace("__HTTPS_LISTEN__", os.environ["LISTEN_LINES"].rstrip("\n")))
PY
ln -sf /etc/nginx/sites-available/gate-pass /etc/nginx/sites-enabled/gate-pass

if ! nginx -t 2>/tmp/nginx-test.$$; then
    cat /tmp/nginx-test.$$ >&2; rm -f /tmp/nginx-test.$$
    cp -f /etc/nginx/sites-available/gate-pass.bak /etc/nginx/sites-available/gate-pass 2>/dev/null || true
    die "nginx rejected the config — the previous one has been put back"
fi
rm -f /tmp/nginx-test.$$
systemctl reload nginx || die "nginx would not reload"

# `systemctl reload` returning 0 does NOT mean the new config is live: on a bad
# bind nginx logs the failure and carries on with the old one. Check the socket.
sleep 1
for addr in $ADDRESSES; do
    ss -tlnH "( sport = :443 )" | grep -q "$addr:443" \
        || die "nginx did not bind $addr:443 — the old config is still live.
       tail /var/log/nginx/error.log"
done
note "nginx is listening for TLS on:$ADDRESSES"

say "The app"
# Match an UNCOMMENTED setting. Grepping for the bare string matched the
# explanatory comment in the unit file, so this reported "already done" on a
# machine where it had never been set — and the cookie stayed non-Secure.
if grep -qE '^[[:space:]]*Environment="GATE_PASS_HTTPS=1"' "$UNIT"; then
    note "already set"
else
    sed -i 's|^[[:space:]]*#[[:space:]]*Environment="GATE_PASS_HTTPS=1"|Environment="GATE_PASS_HTTPS=1"|' "$UNIT"
    grep -qE '^[[:space:]]*Environment="GATE_PASS_HTTPS=1"' "$UNIT" || \
        sed -i '/^ExecStart=/i Environment="GATE_PASS_HTTPS=1"' "$UNIT"
    grep -qE '^[[:space:]]*Environment="GATE_PASS_HTTPS=1"' "$UNIT" || \
        die "could not set GATE_PASS_HTTPS in $UNIT"
    systemctl daemon-reload
    systemctl restart gate-pass || die "the app would not restart"
    note "session cookie is now HTTPS-only"
fi

say "Checking"
# Every check assigns first and tests after. Under `set -e` a failing curl
# aborts the script inside the assignment, so `die` never runs and the whole
# thing ends in silence — which is how this failed the first time and looked
# like a success.
sleep 2
# The LAN address rather than 127.0.0.1, so the checks exercise the path the
# office actually uses. ADDRESSES is a space-prefixed list.
FIRST="$(echo $ADDRESSES | awk '{print $NF}')"

code=$(curl -sk -o /dev/null -w '%{http_code}' --max-time 10 "https://$FIRST/login")
[ "$code" = "200" ] || die "https://$FIRST/login returned '${code:-no response}'
       journalctl -u gate-pass -n 30 --no-pager
       tail /var/log/nginx/error.log"
note "https://$FIRST/login -> 200"

for asset in /static/css/style.css /static/img/fanzart-logo.png; do
    a=$(curl -sk -o /dev/null -w '%{http_code}' --max-time 8 "https://$FIRST$asset")
    [ "$a" = "200" ] || die "$asset returned '${a:-no response}' over HTTPS"
done
note "static files serve over HTTPS"

redirect=$(curl -s -o /dev/null -w '%{http_code} %{redirect_url}' --max-time 8 "http://$FIRST/login")
note "http://$FIRST/login -> $redirect"

TS_IP="$(tailscale ip -4 2>/dev/null | head -1)"
if [ -n "$TS_IP" ]; then
    ts=$(curl -s -o /dev/null -w '%{http_code}' --max-time 8 "http://$TS_IP/login")
    [ "$ts" = "200" ] || note "WARNING: http://$TS_IP/login returned '${ts:-no response}'"
    [ "$ts" = "200" ] && note "http://$TS_IP/login -> 200 (Tailscale, still plain HTTP by design)"
fi

# Anything else nginx was serving must keep serving.
for other in $(find /etc/nginx/sites-enabled -maxdepth 1 ! -name 'gate-pass' -type l -printf '%f ' 2>/dev/null); do
    port=$(grep -oP 'listen\s+\K[0-9]+' "/etc/nginx/sites-enabled/$other" 2>/dev/null | head -1)
    [ -n "$port" ] || continue
    if curl -sf --max-time 5 -o /dev/null "http://127.0.0.1:$port/"; then
        note "$other still serving on :$port"
    else
        note "WARNING: $other on :$port did not answer"
    fi
done

cat <<DONE

    On the office LAN, with the padlock:
        https://$(hostname).local
        https://$FIRST

    Over Tailscale, plain HTTP by design — tailscaled owns 443 there for
    Funnel, and WireGuard has already encrypted the connection:
        http://${TS_IP:-<tailscale ip>}

    Browsers warn until deploy/certs/fanzart-ca.pem is installed on each
    machine — see the notes from deploy/make-cert.sh. Traffic is encrypted
    either way.

    To undo:  sudo deploy/disable-https.sh

DONE
