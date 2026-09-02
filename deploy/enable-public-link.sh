#!/usr/bin/env bash
# Put the gate pass on ONE public link that works from anywhere.
#
#   sudo deploy/enable-public-link.sh --replace   # take over the hostname
#   sudo deploy/enable-public-link.sh --off       # take it off the internet
#
# Why
# -------------------------------------------------------------------------
# The logistics team are on Windows machines that are not on the office LAN and
# do not have Tailscale. Nothing can be installed on them — no certificate, no
# VPN client. They need a URL that simply works in a browser, from home, from a
# phone, from anywhere.
#
# Tailscale Funnel publishes this machine on the public internet under its
# tailnet name, with a genuine Let's Encrypt certificate that every browser
# already trusts. So: no certificate to install, no Kaspersky exclusion, no
# warning to click past, no VPN. One link, and it renews itself.
#
# What this means, plainly
# -------------------------------------------------------------------------
# The sign-in page becomes reachable by ANYONE on the internet. Not the data —
# every page behind it still needs a password — but the door is now in public.
#
# That is a deliberate trade, and it is why deploy/enable-public-link.sh exists
# as a separate, explicit step rather than something install.sh does quietly.
# Two things follow from it:
#
#   * The app throttles sign-in attempts (db.LOGIN_MAX_PER_USERNAME). Without
#     that, publishing a login form is publishing a password-guessing target:
#     a Funnel hostname appears in Certificate Transparency logs the moment its
#     certificate is issued, so scanners find it within hours. This script
#     refuses to run if that protection is missing.
#   * Passwords now matter in a way they did not on a LAN. Anything short,
#     shared, or reused is now exposed to the whole internet rather than to the
#     people who can already walk into the building.
#
# Stronger, when there is time: put Cloudflare Access (or similar) in front, so
# only a company email address can reach the sign-in page at all. Then the form
# is not public even though the link is. This script is the version that works
# today with no accounts to create.

set -uo pipefail          # NOT -e: every check assigns first and tests after

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PORT="${GP_PUBLIC_PORT:-443}"     # Funnel accepts only 443, 8443 and 10000
SNIPPET=/etc/nginx/snippets/gate-pass-ts-redirect.conf

say()  { printf "\n\033[1m==> %s\033[0m\n" "$*"; }
note() { printf "    %s\n" "$*"; }
die()  { printf "\n\033[31mERROR: %s\033[0m\n" "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "run with sudo:  sudo deploy/enable-public-link.sh --replace"
command -v tailscale >/dev/null || die "tailscale is not installed on this machine"

TS_NAME="$(tailscale status --json 2>/dev/null \
           | python3 -c 'import json,sys; print((json.load(sys.stdin).get("Self") or {}).get("DNSName","").rstrip("."))')"
[ -n "$TS_NAME" ] || die "could not read this machine's tailnet name — is tailscaled up?"
TS_IP="$(tailscale ip -4 2>/dev/null | head -1)"
[ "$PORT" = "443" ] && URL="https://$TS_NAME" || URL="https://$TS_NAME:$PORT"

# Reads the live serve config. Everything below decides from this rather than
# from the human-readable output: `tailscale funnel status` prints the WHOLE
# config, tailnet-only entries included, so grepping it for a port reports
# "this is on Funnel" about entries that are not. That false positive rolled
# back a perfectly good setup once already.
cfg() { tailscale serve status --json 2>/dev/null | python3 -c "$1" 2>/dev/null; }

current_target() {
    cfg "
import json,sys
d=json.load(sys.stdin)
w=(d.get('Web') or {}).get('$TS_NAME:$PORT') or {}
h=(w.get('Handlers') or {}).get('/') or {}
print(h.get('Proxy') or '')"
}
is_public() {
    cfg "
import json,sys
d=json.load(sys.stdin)
print('yes' if (d.get('AllowFunnel') or {}).get('$TS_NAME:$PORT') else 'no')"
}

# --- taking it back off -----------------------------------------------------
if [ "${1:-}" = "--off" ]; then
    say "Removing the public link"
    tailscale funnel --https="$PORT" off >/dev/null 2>&1
    tailscale serve  --https="$PORT" off >/dev/null 2>&1
    rm -f "$SNIPPET"
    nginx -t >/dev/null 2>&1 && systemctl reload nginx
    note "$URL is no longer served"
    note "the LAN site is untouched: https://$(hostname).local"
    exit 0
fi

# --- the sign-in form must be able to survive being public ------------------
say "Checking the app is fit to be published"
grep -q "LOGIN_MAX_PER_USERNAME" "$APP_DIR/db.py" \
    || die "this copy of the app has no sign-in throttling.
       Publishing a login form without it means publishing an unlimited
       password-guessing target. Update first:
         sudo $APP_DIR/deploy/install.sh"
grep -q "login_lockout" "$APP_DIR/app.py" \
    || die "db.py has the throttle but app.py does not call it — update first"
note "sign-in attempts are throttled per account"

# --- what is already on this hostname ---------------------------------------
say "What $URL currently serves"
EXISTING="$(current_target)"
WAS_PUBLIC="$(is_public)"
if [ -n "$EXISTING" ]; then
    note "$EXISTING   (public: $WAS_PUBLIC)"
    if [ "$EXISTING" != "http://127.0.0.1:8091" ] && [ "${1:-}" != "--replace" ]; then
        die "$URL already serves $EXISTING.
       Taking it over will stop that site being reachable at this address.
       If that is what you want, say so explicitly:
         sudo $0 --replace"
    fi
    [ "$EXISTING" != "http://127.0.0.1:8091" ] && \
        note "REPLACING it — to put it back: sudo tailscale funnel --bg --https=$PORT $EXISTING"
else
    note "nothing yet"
fi

# Every other backend on the tailnet, to confirm afterwards none was collateral.
OTHERS="$(tailscale serve status 2>/dev/null | grep -oE 'http://127\.0\.0\.1:[0-9]+' \
          | grep -v ':8091$' | grep -vF "${EXISTING:-__none__}" | sort -u)"

# --- nginx's loopback front door --------------------------------------------
say "nginx front door on 127.0.0.1:8091"
ss -lntH '( sport = :8091 )' | grep -q 127.0.0.1 \
    || die "nginx is not listening on 127.0.0.1:8091 — run this first:
         sudo $APP_DIR/deploy/enable-https.sh"
note "listening"

# --- publish ----------------------------------------------------------------
say "Publishing $URL"
tailscale funnel --bg --https="$PORT" http://127.0.0.1:8091 >/dev/null 2>&1 \
    || die "tailscale funnel refused. Funnel must be enabled for this tailnet at
         https://login.tailscale.com/admin/settings/keys
       and the node needs the funnel attribute in the tailnet policy file.
       Current state:
$(tailscale serve status 2>&1)"

rollback() {
    printf "\n\033[31mrolling back\033[0m\n" >&2
    if [ -n "$EXISTING" ] && [ "$EXISTING" != "http://127.0.0.1:8091" ]; then
        if [ "$WAS_PUBLIC" = "yes" ]; then
            tailscale funnel --bg --https="$PORT" "$EXISTING" >/dev/null 2>&1
        else
            tailscale serve --bg --https="$PORT" "$EXISTING" >/dev/null 2>&1
        fi
    else
        tailscale funnel --https="$PORT" off >/dev/null 2>&1
        tailscale serve  --https="$PORT" off >/dev/null 2>&1
    fi
    rm -f "$SNIPPET"
    nginx -t >/dev/null 2>&1 && systemctl reload nginx
    die "$1"
}

[ "$(current_target)" = "http://127.0.0.1:8091" ] || rollback "the serve entry did not take"
[ "$(is_public)" = "yes" ] || rollback "$URL was set up but is NOT public, which is the
       whole point. Funnel is probably not permitted for this node in the
       tailnet policy file."
note "served, and public"

say "Checking nothing else was knocked over"
for backend in $OTHERS; do
    tailscale serve status 2>/dev/null | grep -qF "$backend" \
        || rollback "$backend stopped being served — it was fine before this ran"
    note "$backend still served"
done
[ -n "$OTHERS" ] || note "nothing else was being served"

# --- does it work -----------------------------------------------------------
say "Checking the site"
sleep 3
# No -k: the entire point is that the certificate verifies against the public
# trust store with nothing installed anywhere, so skipping verification here
# would prove nothing.
code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 25 "$URL/login")
[ "$code" = "200" ] || rollback "$URL/login returned '${code:-no response}'"
note "$URL/login -> 200, certificate verified with nothing installed"

asset=$(curl -s -o /dev/null -w '%{http_code}' --max-time 15 "$URL/static/css/style.css")
[ "$asset" = "200" ] || rollback "static files return '$asset'"
note "static files serve"

# A POST is rejected with 403 if the Host reaching the app does not match the
# Origin the browser sent, which is what a Host-rewriting proxy causes. Every
# form in the app would fail while the pages still looked perfect.
post=$(curl -s -o /dev/null -w '%{http_code}' --max-time 20 -H "Origin: $URL" \
       -X POST "$URL/login" -d 'username=__probe__&password=__not-a-real-password__')
case "$post" in
    401) note "forms reach the app with the right Host (bad password -> 401)" ;;
    429) note "forms reach the app (throttled from an earlier check -> 429)" ;;
    403) rollback "a form post came back 403: something in front is rewriting the
       Host, so every form would fail. Nothing was left published." ;;
    *)   rollback "a form post returned '$post', expected 401" ;;
esac

# The throttle, proven on the live site rather than assumed. Enough wrong
# passwords for one made-up account to trip it, which must not touch anyone real.
say "Proving the sign-in throttle works on the live site"
tripped=no
for i in $(seq 1 12); do
    r=$(curl -s -o /dev/null -w '%{http_code}' --max-time 15 -H "Origin: $URL" \
        -X POST "$URL/login" -d "username=__probe__&password=guess-$i")
    [ "$r" = "429" ] && { tripped=yes; note "guessing was cut off after $i attempts"; break; }
done
[ "$tripped" = "yes" ] || rollback "twelve wrong passwords in a row were all accepted for
       another try. The throttle is not working, and this link must not be
       public without it."

# --- point the old addresses at it ------------------------------------------
say "Redirecting the old addresses"
mkdir -p /etc/nginx/snippets
cat > "$SNIPPET" <<EOF
# Written by deploy/enable-public-link.sh — do not edit; it is overwritten.
#
# Included inside the port 80 "location /". \$needs_https is 0 exactly for
# requests that arrived on a Tailscale address, where tailscaled owns 443 and
# nginx has no TLS listener of its own.
#
# Those used to be served as plain HTTP, which looked fine and could not be
# signed in to: the session cookie is Secure and a browser will not keep a
# Secure cookie that arrived over http://. They now go to the public link.
if (\$needs_https = 0) {
    return 301 $URL\$request_uri;
}
EOF
nginx -t >/dev/null 2>&1 || { rm -f "$SNIPPET"; rollback "nginx rejected the redirect"; }
systemctl reload nginx || rollback "nginx would not reload"

sleep 1
if [ -n "$TS_IP" ]; then
    got=$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "http://$TS_IP/login")
    note "http://$TS_IP/login -> $got (redirects to the public link)"
fi

LAN_IP="$(hostname -I | awk '{print $1}')"
lan=$(curl -sk -o /dev/null -w '%{http_code}' --max-time 10 "https://$LAN_IP/login")
[ "$lan" = "200" ] || rollback "the LAN site broke: https://$LAN_IP/login -> '$lan'"
note "the office LAN site is unchanged"

cat <<DONE

    The link to give the logistics team — works on any machine, anywhere,
    with nothing to install:

        $URL

    Windows, phones, home broadband, mobile data. Real certificate, clean
    padlock, no Kaspersky exclusion, no VPN.

    On the office LAN, unchanged and slightly faster:
        https://$(hostname).local

    THIS IS NOW ON THE PUBLIC INTERNET. The data is still behind a password,
    but the sign-in page is not. Worth doing this week:

      * Make everyone change to a long, unique password. Anything shared or
        reused is now exposed to the internet, not just to the office.
      * Remove accounts for people who have left — check the Users page.
      * Watch for guessing:  sudo journalctl -u gate-pass | grep -c 429

    To take it back off the internet:
        sudo deploy/enable-public-link.sh --off

DONE
