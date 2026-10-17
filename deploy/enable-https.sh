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
# The public link's name, answered on the LAN as well, once
# enable-office-link.sh has fetched its certificate. Rendered here rather than
# as a site of its own so that every re-run of this script — which install.sh
# does on every update — keeps it, and so it always listens on exactly the
# addresses the main TLS site does (two sites disagreeing over a socket's
# options is a config nginx refuses outright).
TAILNET_TLS_DIR=/etc/gate-pass/tailnet-tls
TAILNET_NAME=""
if [ -f "$TAILNET_TLS_DIR/name" ]; then
    TAILNET_NAME="$(tr -d '[:space:]' < "$TAILNET_TLS_DIR/name")"
    # It is pasted into the nginx config, so it must be a hostname and nothing else.
    if ! [[ "$TAILNET_NAME" =~ ^[a-z0-9]([a-z0-9.-]*[a-z0-9])?$ ]]; then
        note "WARNING: $TAILNET_TLS_DIR/name does not hold a hostname — ignoring it"
        TAILNET_NAME=""
    elif [ -s "$TAILNET_TLS_DIR/$TAILNET_NAME.crt" ] && [ -s "$TAILNET_TLS_DIR/$TAILNET_NAME.key" ]; then
        note "also answering as $TAILNET_NAME on the LAN"
    else
        note "WARNING: no certificate for $TAILNET_NAME in $TAILNET_TLS_DIR —"
        note "         the office path is off until: sudo deploy/enable-office-link.sh"
        TAILNET_NAME=""
    fi
fi
cp -f /etc/nginx/sites-available/gate-pass /etc/nginx/sites-available/gate-pass.bak 2>/dev/null || true
LISTEN_LINES="$LISTEN_LINES" TAILNET_NAME="$TAILNET_NAME" TAILNET_TLS_DIR="$TAILNET_TLS_DIR" \
python3 - "$APP_DIR" "$CERT_DIR" <<'PY' > /etc/nginx/sites-available/gate-pass
import os, sys
app_dir, cert_dir = sys.argv[1], sys.argv[2]
listen = os.environ["LISTEN_LINES"].rstrip("\n")
template = open(f"{app_dir}/deploy/nginx-gate-pass-ssl.conf").read()
print(template.replace("__APP_DIR__", app_dir)
              .replace("__CERT_DIR__", cert_dir)
              .replace("__HTTPS_LISTEN__", listen))
name = os.environ.get("TAILNET_NAME", "")
if name:
    extra = open(f"{app_dir}/deploy/nginx-gate-pass-tailnet-name.conf").read()
    print(extra.replace("__HTTPS_LISTEN__", listen)
               .replace("__TAILNET_NAME__", name)
               .replace("__TAILNET_TLS_DIR__", os.environ["TAILNET_TLS_DIR"]))
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

# Verified, not -k: the whole point of the office path is a padlock nobody had
# to install anything for, so a certificate the system store rejects is a fail.
if [ -n "$TAILNET_NAME" ]; then
    tn=$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 \
              --resolve "$TAILNET_NAME:443:$FIRST" "https://$TAILNET_NAME/login")
    # A warning, not a stop: the main site is up and fine at this point, and
    # failing here would make install.sh report HTTPS as broken when it is not.
    if [ "$tn" = "200" ]; then
        note "https://$TAILNET_NAME/login on $FIRST -> 200, genuine certificate"
    else
        note "WARNING: https://$TAILNET_NAME/login on $FIRST returned '${tn:-no response}'"
        note "         office machines using that name will fail. If it is a"
        note "         certificate error: sudo deploy/enable-office-link.sh --renew"
    fi
fi

redirect=$(curl -s -o /dev/null -w '%{http_code} %{redirect_url}' --max-time 8 "http://$FIRST/login")
note "http://$FIRST/login -> $redirect"

TS_IP="$(tailscale ip -4 2>/dev/null | head -1)"
TS_NAME="$(tailscale status --json 2>/dev/null \
           | python3 -c 'import json,sys; print((json.load(sys.stdin).get("Self") or {}).get("DNSName","").rstrip("."))' 2>/dev/null)"
if [ -n "$TS_IP" ]; then
    ts=$(curl -s -o /dev/null -w '%{http_code}' --max-time 8 "http://$TS_IP/login")
    # A 301 is the RIGHT answer once enable-public-link.sh is set up: the plain
    # HTTP address redirects to the public HTTPS one. This check used to demand
    # a 200 and warned about the correct behaviour, which on a launch day reads
    # as though something is broken.
    #
    # A 200 is the one worth warning about: it means the redirect is missing
    # while GATE_PASS_HTTPS is set, so the session cookie is Secure, the browser
    # will not keep it, and sign-in fails on that address with no error at all.
    case "$ts" in
        301|302) note "http://$TS_IP/login -> $ts to the tailnet HTTPS address" ;;
        200)     note "WARNING: http://$TS_IP/login serves plain HTTP (200)."
                 note "         With GATE_PASS_HTTPS set nobody can sign in there —"
                 note "         the cookie is Secure and the page is not. Run:"
                 note "           sudo deploy/enable-public-link.sh --replace" ;;
        *)       note "WARNING: http://$TS_IP/login returned '${ts:-no response}'" ;;
    esac
fi

# Anything else nginx was serving must keep serving.
for other in $(find /etc/nginx/sites-enabled -maxdepth 1 ! -name 'gate-pass' ! -name 'zz-gate-pass-office-link' -type l -printf '%f ' 2>/dev/null); do
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

    Away from the office, if enable-public-link.sh has been run — a real
    certificate, nothing to install on the machine using it:
        https://${TS_NAME:-<tailnet name>}

    http://${TS_IP:-<tailscale ip>} redirects there, so old bookmarks keep
    working. Do not hand that IP out: it only resolves inside the tailnet.

    Browsers warn until deploy/certs/fanzart-ca.pem is installed on each
    machine — see the notes from deploy/make-cert.sh. Traffic is encrypted
    either way.

    To undo:  sudo deploy/disable-https.sh

DONE
