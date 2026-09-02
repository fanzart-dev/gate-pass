#!/usr/bin/env bash
# Serve the gate pass over Tailscale with a REAL certificate.
#
#   sudo deploy/enable-tailscale-https.sh          # turn it on
#   sudo deploy/enable-tailscale-https.sh --off    # turn it back off
#
# Why this exists, when deploy/enable-https.sh already does HTTPS
# -------------------------------------------------------------------------
# enable-https.sh serves the LAN over a certificate signed by an authority we
# run ourselves. That works, but every machine has to be told to trust that
# authority before the padlock means anything — and on Windows machines running
# Kaspersky it is not enough even then, because Kaspersky intercepts HTTPS and
# checks against its own list of authorities rather than Windows'.
#
# Tailscale hands out genuine Let's Encrypt certificates for the tailnet name.
# Those are trusted by every browser, phone and antivirus on earth already. So
# over Tailscale there is nothing to install on the client, nothing to exclude
# in Kaspersky, and no warning to click through — the padlock is simply clean.
#
# The bug this actually fixes
# -------------------------------------------------------------------------
# Over Tailscale the site was served as plain HTTP, on the reasoning that
# WireGuard has already encrypted everything. That reasoning is sound for
# confidentiality and wrong in practice: GATE_PASS_HTTPS=1 marks the session
# cookie Secure, and a browser will not store a Secure cookie that arrived on
# an http:// page. So the login form took the password, redirected, found no
# session, and showed the login page again — for ever, with no error. The page
# returned 200 throughout, so every health check reported success.
#
# What it does NOT do
# -------------------------------------------------------------------------
# This is `tailscale serve`, not `tailscale funnel`: reachable from inside the
# tailnet only, never published to the public internet. The gate pass register
# must not be on the open internet, and the script checks afterwards that it
# has not become so.
#
# Anything already being served on the tailnet — FanIQ, the portfolio site — is
# left alone. The script records what was there before, refuses a port that is
# already spoken for, and checks afterwards that every earlier entry survived.

set -uo pipefail          # NOT -e: every check assigns first and tests after

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PORT="${GP_TS_PORT:-9443}"
SNIPPET=/etc/nginx/snippets/gate-pass-ts-redirect.conf

say()  { printf "\n\033[1m==> %s\033[0m\n" "$*"; }
note() { printf "    %s\n" "$*"; }
die()  { printf "\n\033[31mERROR: %s\033[0m\n" "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "run with sudo:  sudo deploy/enable-tailscale-https.sh"
command -v tailscale >/dev/null || die "tailscale is not installed on this machine"

# --- the tailnet name -------------------------------------------------------
# The certificate is issued for this name and no other, so everything below
# hangs off getting it right. The trailing dot in the API response is real.
TS_NAME="$(tailscale status --json 2>/dev/null \
           | python3 -c 'import json,sys; print((json.load(sys.stdin).get("Self") or {}).get("DNSName","").rstrip("."))')"
[ -n "$TS_NAME" ] || die "could not read this machine's tailnet name — is tailscaled up?
       tailscale status"
TS_IP="$(tailscale ip -4 2>/dev/null | head -1)"

# --- turning it off ---------------------------------------------------------
if [ "${1:-}" = "--off" ]; then
    say "Removing the tailnet HTTPS front door"
    tailscale serve --https="$PORT" off 2>/dev/null \
        || tailscale serve --remove "https:$PORT" 2>/dev/null \
        || note "nothing was being served on $PORT"
    rm -f "$SNIPPET"
    nginx -t >/dev/null 2>&1 && systemctl reload nginx
    note "off — Tailscale is back to plain HTTP"
    note "NOTE: with GATE_PASS_HTTPS=1 nobody can sign in over Tailscale now."
    note "      Use the LAN address, or unset it in the unit file."
    exit 0
fi

# --- what is already being served, so it can be put back --------------------
say "What the tailnet is already serving"
BEFORE="$(tailscale serve status 2>/dev/null)"
if [ -n "$BEFORE" ]; then
    printf '%s\n' "$BEFORE" | sed 's/^/    /'
else
    note "nothing yet"
fi

# Refuse a port somebody else is on rather than silently taking it over.
if printf '%s\n' "$BEFORE" | grep -q ":$PORT\b"; then
    die "something is already served on port $PORT of the tailnet.
       Pick another:  sudo GP_TS_PORT=9444 deploy/enable-tailscale-https.sh"
fi

# Every backend that was being proxied to, to confirm later that they survived.
OTHERS="$(printf '%s\n' "$BEFORE" | grep -oE 'http://127\.0\.0\.1:[0-9]+' | sort -u)"

# --- the certificate --------------------------------------------------------
say "Certificate"
# Fetched here rather than waiting for the first browser request, so that a
# tailnet without HTTPS enabled fails now with a clear message instead of
# looking like a broken site later.
if tailscale cert --cert-file /tmp/gp-ts.pem --key-file /tmp/gp-ts.key "$TS_NAME" >/dev/null 2>&1; then
    note "issued for $TS_NAME"
    note "by:      $(openssl x509 -in /tmp/gp-ts.pem -noout -issuer | sed 's/issuer=//')"
    note "expires: $(openssl x509 -in /tmp/gp-ts.pem -noout -enddate | sed 's/notAfter=//')"
    note "renews itself — tailscaled handles it, nothing to diarise"
    rm -f /tmp/gp-ts.pem /tmp/gp-ts.key
else
    die "Tailscale would not issue a certificate for $TS_NAME.
       HTTPS is probably off for this tailnet. Turn it on once, at
         https://login.tailscale.com/admin/dns
       under 'HTTPS Certificates', then run this again."
fi

# --- nginx's loopback front door --------------------------------------------
say "nginx front door on 127.0.0.1:8091"
# tailscaled proxies into nginx, not straight into gunicorn, so the Tailscale
# path gets the same static handling, body limit and export timeouts as the LAN
# path. Without it the two entrances quietly behave differently.
if ! ss -lntH '( sport = :8091 )' | grep -q 127.0.0.1; then
    die "nginx is not listening on 127.0.0.1:8091.
       That block is added by the current deploy/nginx-gate-pass-ssl.conf, so
       run this first:   sudo deploy/enable-https.sh"
fi
note "listening"

# --- the serve entry --------------------------------------------------------
say "Serving https://$TS_NAME:$PORT on the tailnet"
if ! tailscale serve --bg --https="$PORT" http://127.0.0.1:8091 >/dev/null 2>&1; then
    # Older CLIs spell it differently; try the previous form before giving up.
    tailscale serve "https:$PORT" / http://127.0.0.1:8091 >/dev/null 2>&1 \
        || die "tailscale serve refused. Current state:
$(tailscale serve status 2>&1)"
fi
note "set"

rollback() {
    printf "\n\033[31mrolling back\033[0m\n" >&2
    tailscale serve --https="$PORT" off >/dev/null 2>&1
    rm -f "$SNIPPET"
    nginx -t >/dev/null 2>&1 && systemctl reload nginx
    die "$1"
}

# --- did anything else break? -----------------------------------------------
say "Checking the other sites are untouched"
AFTER="$(tailscale serve status 2>/dev/null)"
for backend in $OTHERS; do
    printf '%s\n' "$AFTER" | grep -qF "$backend" \
        || rollback "$backend is no longer being served — it was before this ran"
    note "$backend still served"
done
[ -n "$OTHERS" ] || note "there were none"

# The register must not be reachable from the public internet.
if tailscale funnel status 2>/dev/null | grep -q ":$PORT\b"; then
    rollback "port $PORT ended up on Funnel, which would publish the gate pass
       register to the internet. Removed."
fi
note "tailnet only, not published to the internet"

# --- does it actually work -------------------------------------------------
say "Checking the site over the tailnet"
sleep 2
BASE="https://$TS_NAME:$PORT"

# No -k anywhere below. The entire point is that the certificate verifies
# against the public trust store with nothing installed, so a check that
# skipped verification would prove nothing.
code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 20 "$BASE/login")
[ "$code" = "200" ] || rollback "$BASE/login returned '${code:-no response}'"
note "$BASE/login -> 200, certificate verified with no local trust setup"

asset=$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "$BASE/static/css/style.css")
[ "$asset" = "200" ] || rollback "static files return '$asset' over the tailnet"
note "static files serve"

# The important one. A POST is rejected with 403 if the Host that reached the
# app does not match the Origin the browser sent — which is exactly what
# happens if a proxy in front rewrites Host. A wrong answer here means every
# form in the app fails while the pages themselves look perfectly fine.
post=$(curl -s -o /dev/null -w '%{http_code}' --max-time 15 \
       -H "Origin: $BASE" -X POST "$BASE/login" \
       -d 'username=__probe__&password=__not-a-real-password__')
case "$post" in
    401) note "form posts reach the app with the right Host (bad password -> 401)" ;;
    403) rollback "a form post came back 403: the Host is being rewritten in
       front of the app, so every form would fail. Nothing was left enabled." ;;
    *)   rollback "a form post returned '$post', expected 401" ;;
esac

# And the cookie must now be storable, which was the whole problem.
flags=$(curl -s -i --max-time 15 -H "Origin: $BASE" -X POST "$BASE/login" \
        -d 'username=__probe__&password=__not-a-real-password__' \
        | grep -i '^set-cookie' | head -1)
case "$flags" in
    *Secure*) note "the Secure session cookie is served over HTTPS — sign-in works here" ;;
    *)        note "WARNING: session cookie is not marked Secure; is GATE_PASS_HTTPS set?" ;;
esac

# --- send the old plain-HTTP address to the new one -------------------------
say "Redirecting the old Tailscale address"
mkdir -p /etc/nginx/snippets
cat > "$SNIPPET" <<EOF
# Written by deploy/enable-tailscale-https.sh — do not edit; it is overwritten.
#
# Included inside the port 80 "location /". \$needs_https is 0 exactly for
# requests that arrived on a Tailscale address, where tailscaled owns 443 and
# nginx has no TLS listener of its own. Those now go to the tailnet HTTPS URL,
# which has a real certificate.
#
# Without this, anyone with the old http://$TS_IP bookmark lands on a page that
# renders perfectly and cannot be signed in to, because the session cookie is
# Secure and the page is not.
if (\$needs_https = 0) {
    return 301 https://$TS_NAME:$PORT\$request_uri;
}
EOF

if ! nginx -t 2>/tmp/nginx-ts.$$; then
    cat /tmp/nginx-ts.$$ >&2; rm -f /tmp/nginx-ts.$$
    rollback "nginx rejected the redirect snippet"
fi
rm -f /tmp/nginx-ts.$$
systemctl reload nginx || rollback "nginx would not reload"

sleep 1
if [ -n "$TS_IP" ]; then
    got=$(curl -s -o /dev/null -w '%{http_code} %{redirect_url}' --max-time 10 "http://$TS_IP/login")
    case "$got" in
        301*"$TS_NAME:$PORT"*) note "http://$TS_IP/login -> $got" ;;
        *) rollback "http://$TS_IP/login gave '$got', expected a 301 to the tailnet URL" ;;
    esac
fi

# The LAN must be exactly as it was.
LAN_IP="$(hostname -I | awk '{print $1}')"
lan=$(curl -sk -o /dev/null -w '%{http_code}' --max-time 10 "https://$LAN_IP/login")
[ "$lan" = "200" ] || rollback "the LAN site broke: https://$LAN_IP/login -> '$lan'"
note "the LAN site is unchanged (https://$LAN_IP/login -> 200)"

cat <<DONE

    Over Tailscale, with a real certificate and NOTHING to install:

        $BASE

    Anyone signed in to the tailnet gets a clean padlock on any device — no
    fanzart-ca.pem, no Kaspersky exclusion, no warning to click past. The old
    http://${TS_IP:-<tailscale ip>} address redirects here, so existing
    bookmarks keep working and start working properly.

    On the office LAN, unchanged:
        https://$(hostname).local

    LAN machines still need deploy/certs/fanzart-ca.pem installed, because
    .local is not a name any public authority will vouch for. The way to avoid
    that entirely is to put those machines on Tailscale and use the address
    above — see deploy/OFFICE-MACHINES.md.

    To undo:  sudo deploy/enable-tailscale-https.sh --off

DONE
