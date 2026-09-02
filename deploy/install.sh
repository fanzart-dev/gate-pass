#!/usr/bin/env bash
# One-command install of Fanzart Gate Pass on an Ubuntu server.
#
#   sudo ./deploy/install.sh
#
# Safe to run again: re-running pulls the latest code, reinstalls dependencies
# and restarts the service, leaving the database and accounts alone. That is the
# upgrade path as well as the install path.
#
# It sets up: a dedicated service account, a virtualenv, a systemd unit, nginx
# in front of it, a nightly verified backup, and mDNS so the office can reach it
# by name instead of by IP. It does NOT touch the database if one already exists.

set -euo pipefail

REPO="https://github.com/fanzart-dev/gate-pass.git"
APP_DIR="/opt/gate-pass"
CERT_DIR="/opt/gate-pass/deploy/certs"
APP_USER="gatepass"
PORT=8090

say()  { printf "\n\033[1m==> %s\033[0m\n" "$*"; }
note() { printf "    %s\n" "$*"; }
die()  { printf "\n\033[31mERROR: %s\033[0m\n" "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "run with sudo:  sudo ./deploy/install.sh"
command -v apt-get >/dev/null || die "this expects Ubuntu or Debian (no apt-get found)"

say "Installing packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
# avahi-daemon is what makes http://<hostname>.local work on the office network,
# so nobody has to remember an IP address.
apt-get install -y -qq python3-venv python3-pip git nginx avahi-daemon >/dev/null
note "python3-venv, git, nginx, avahi-daemon"

say "Service account"
if id "$APP_USER" >/dev/null 2>&1; then
    note "$APP_USER already exists"
else
    # A system account with no login shell: it exists to own the files and run
    # the service, and is not a way into the machine.
    adduser --system --group --home "$APP_DIR" --shell /usr/sbin/nologin "$APP_USER"
    note "created $APP_USER (no shell, no password)"
fi

say "Application code"
if [ -d "$APP_DIR/.git" ]; then
    # As the service account, not as root. The checkout belongs to $APP_USER,
    # and git refuses to work in a repository owned by somebody else:
    #
    #   fatal: detected dubious ownership in repository at '/opt/gate-pass'
    #
    # The documented escape is `safe.directory`, but that is the wrong fix — it
    # tells git to ignore the mismatch rather than removing it. Running git as
    # the owner has no mismatch to ignore, and keeps root from leaving
    # root-owned objects behind in a tree the service has to write to.
    # This script is IN the tree it is about to update, so the next two lines
    # can rewrite the file bash is currently reading. Bash reads a script by
    # byte offset and re-reads from that offset as it goes, so a file that
    # changes length underneath it carries on executing from the wrong place —
    # in the middle of a line, or in a block it already ran. That is not
    # theoretical: this is what turned one bad edit into
    # "install.sh: line 102: [Unit]: command not found".
    #
    # So: note what the script looked like, update, and if the script itself
    # changed, start the new one over from the top. The environment variable
    # stops that becoming a loop.
    before_update="$(sha256sum "$0" | cut -d' ' -f1)"
    sudo -u "$APP_USER" git -C "$APP_DIR" fetch --quiet origin
    sudo -u "$APP_USER" git -C "$APP_DIR" reset --quiet --hard origin/master
    note "updated to $(sudo -u "$APP_USER" git -C "$APP_DIR" log --oneline -1)"

    if [ "$(sha256sum "$0" | cut -d' ' -f1)" != "$before_update" ]; then
        if [ "${GP_INSTALL_RESTARTED:-}" = "1" ]; then
            die "$0 changed again after restarting — something is rewriting it
       in a loop. Check the branch it is being updated from."
        fi
        note "this installer updated itself — starting the new one"
        GP_INSTALL_RESTARTED=1 exec bash "$0" "$@"
    fi
else
    # First install: the directory is either absent or the empty home adduser
    # made, so this one runs as root and the chown below tidies up after it.
    mkdir -p "$APP_DIR"
    git clone --quiet "$REPO" "$APP_DIR"
    note "cloned into $APP_DIR"
fi
# storage/ holds the book. Created if absent, never touched if present.
mkdir -p "$APP_DIR/storage/invoices" "$APP_DIR/storage/backups"
chown -R "$APP_USER:$APP_USER" "$APP_DIR"

# Two different permissions on purpose.
#
# The app directory must be traversable by nginx's worker (www-data), which
# serves static/ directly. `adduser --system` creates a home directory as 0750,
# so without this every stylesheet and the logo return 403 and the app renders
# as unstyled HTML -- it looks broken rather than unauthorised, which is a
# confusing way to find out. The code here is public on GitHub, so there is
# nothing to protect by keeping it closed.
chmod 755 "$APP_DIR"
# The book is a different matter: the database, the password hashes and the
# session signing key. 0750 means only the service account can read it -- not
# nginx, and not any other user on this machine.
chmod 750 "$APP_DIR/storage"

say "Python environment"
sudo -u "$APP_USER" python3 -m venv "$APP_DIR/.venv" 2>/dev/null || true
sudo -u "$APP_USER" "$APP_DIR/.venv/bin/pip" install --quiet --upgrade pip
sudo -u "$APP_USER" "$APP_DIR/.venv/bin/pip" install --quiet -r "$APP_DIR/requirements.txt"
note "$("$APP_DIR/.venv/bin/python3" --version), dependencies installed"

say "systemd service"
# Rendered from deploy/gate-pass.service rather than written out again here.
# There used to be two copies of this unit — one in the repo and one inline in
# this script — and they had drifted apart on the user and the paths.
APP_DIR="$APP_DIR" APP_USER="$APP_USER" python3 - <<'PY' > /etc/systemd/system/gate-pass.service
import os
template = open(f"{os.environ['APP_DIR']}/deploy/gate-pass.service").read()
print(template.replace("__APP_DIR__", os.environ["APP_DIR"])
              .replace("__APP_USER__", os.environ["APP_USER"]), end="")
PY
systemctl daemon-reload
systemctl enable --quiet gate-pass
systemctl restart gate-pass
sleep 3
systemctl is-active --quiet gate-pass || {
    journalctl -u gate-pass -n 30 --no-pager
    die "the service did not start — the log above says why"
}
note "gate-pass.service running on 127.0.0.1:$PORT"

say "nginx"
# If TLS is already on, leave it on. This file used to write the plain-HTTP
# site unconditionally, so every update silently dropped the server back to
# HTTP -- the padlock would vanish and nobody would connect it to having run an
# update. enable-https.sh is the one place that knows how to build the TLS
# site, so re-run it rather than trying to reproduce it here.
if grep -q '443 ssl' /etc/nginx/sites-available/gate-pass 2>/dev/null; then
    note "HTTPS is on; keeping it"
    HTTPS_WAS_ON=1
else
    HTTPS_WAS_ON=0
fi

# server_name _ is a catch-all: this answers on the IP address, on
# <hostname>.local, and on a real domain later without being edited.
cat > /etc/nginx/sites-available/gate-pass <<NGINX
server {
    listen 80 default_server;
    server_name _;

    # Must be at least MAX_CONTENT_LENGTH in app.py (32MB) or nginx rejects a
    # large scanned invoice before Flask can explain why.
    client_max_body_size 32M;

    # A whole-year Excel export takes ~20s to build; these must outlast the
    # gunicorn timeout or nginx gives up on a download that would have worked.
    proxy_connect_timeout 10s;
    proxy_send_timeout    180s;
    proxy_read_timeout    180s;

    access_log /var/log/nginx/gate-pass.access.log;
    error_log  /var/log/nginx/gate-pass.error.log;

    # The CA certificate, so a machine can fetch the file that makes HTTPS
    # trustworthy. Served on the plain-HTTP site too, because that is the one
    # running when HTTPS is off -- which is exactly when it is needed.
    location = /fanzart-ca.pem {
        alias $CERT_DIR/fanzart-ca.pem;
        default_type application/x-x509-ca-cert;
        add_header Content-Disposition 'attachment; filename="fanzart-ca.pem"';
    }

    location /static/ {
        alias $APP_DIR/static/;
        expires 7d;
        add_header Cache-Control "public";
        access_log off;
    }

    location / {
        proxy_pass http://127.0.0.1:$PORT;
        proxy_http_version 1.1;
        # Host must pass through untouched: every POST is checked for a
        # same-origin Origin header against it. Rewrite it and every form
        # submission in the app starts returning 403.
        proxy_set_header Host              \$host;
        proxy_set_header X-Real-IP         \$remote_addr;
        proxy_set_header X-Forwarded-For   \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_buffering off;
    }
}
NGINX
# Everything enabled before this run. Only `gate-pass` and the stock `default`
# are ours to change; anything else belongs to another site and must be in the
# same state when we finish. A site quietly losing its symlink is the worst
# kind of failure here: nothing errors, the port simply stops answering, and
# the first anyone knows is a page that will not load. It has happened once on
# this server -- `portfolio` on :8080, which is published to the internet
# through Tailscale Funnel -- and the cause was never proven, so this both
# records the state and repairs it.
BEFORE="$(find /etc/nginx/sites-enabled -maxdepth 1 -type l -printf '%f\n' 2>/dev/null | sort || true)"

ln -sf /etc/nginx/sites-available/gate-pass /etc/nginx/sites-enabled/gate-pass

# Only the stock "Welcome to nginx" placeholder is disabled, and only its
# symlink -- /etc/nginx/sites-available/default is left intact, so `ln -s` puts
# it back. Two sites cannot both be `default_server` on port 80, so one has to
# go, and it is the placeholder rather than anything real.
#
# Every other enabled site is left strictly alone. On this server that matters:
# `portfolio` serves on :8080 and is published to the public internet through
# Tailscale Funnel, and must keep working.
if [ -L /etc/nginx/sites-enabled/default ]; then
    rm -f /etc/nginx/sites-enabled/default
    note "disabled the stock nginx placeholder (re-enable: ln -s /etc/nginx/sites-available/default /etc/nginx/sites-enabled/)"
fi
# Put back anything that was enabled when we started and is not now. Also
# catches a site that went missing between runs for any other reason.
for was in $BEFORE; do
    case "$was" in
        gate-pass|default) continue ;;
    esac
    if [ ! -e "/etc/nginx/sites-enabled/$was" ] \
       && [ -e "/etc/nginx/sites-available/$was" ]; then
        ln -s "/etc/nginx/sites-available/$was" "/etc/nginx/sites-enabled/$was"
        note "RESTORED $was — it was enabled before this run and had gone"
    fi
done

# Anything in sites-available that is not enabled and is not ours. Worth saying
# out loud: a disabled site is invisible until somebody tries to use it.
for avail in $(find /etc/nginx/sites-available -maxdepth 1 -type f -printf '%f\n' 2>/dev/null | sort); do
    case "$avail" in
        gate-pass|gate-pass.bak|default) continue ;;
    esac
    if [ ! -e "/etc/nginx/sites-enabled/$avail" ]; then
        note "NOTE: $avail exists but is not enabled (ln -s /etc/nginx/sites-available/$avail /etc/nginx/sites-enabled/)"
    fi
done

OTHERS=$(find /etc/nginx/sites-enabled -maxdepth 1 ! -name 'gate-pass' -type l -printf '%f ' 2>/dev/null || true)
if [ -n "$OTHERS" ]; then
    note "left untouched: $OTHERS"
fi

nginx -t >/dev/null 2>&1 || { nginx -t; die "nginx config rejected"; }
systemctl reload nginx
note "nginx proxying port 80 to the app"

if [ "$HTTPS_WAS_ON" = "1" ]; then
    say "Putting HTTPS back"
    "$APP_DIR/deploy/enable-https.sh" || die "could not restore HTTPS — the site
       is up on plain HTTP; run sudo $APP_DIR/deploy/enable-https.sh to retry"
fi

# Prove we did not break a neighbour. Anything already being served keeps
# serving, or this install is a regression however well the app itself works.
for other in $OTHERS; do
    port=$(grep -oP 'listen\s+\K[0-9]+' "/etc/nginx/sites-enabled/$other" 2>/dev/null | head -1)
    [ -n "$port" ] || continue
    if curl -sf --max-time 5 -o /dev/null "http://127.0.0.1:$port/"; then
        note "verified $other still serving on :$port"
    else
        note "WARNING: $other on :$port did not answer — check it"
    fi
done

say "Nightly backup"
cat > /etc/cron.d/gate-pass-backup <<CRON
# Verified online backup of the gate pass book, 21:00 daily.
SHELL=/bin/bash
0 21 * * * $APP_USER $APP_DIR/deploy/backup.sh >> /var/log/gate-pass-backup.log 2>&1
CRON
chmod 644 /etc/cron.d/gate-pass-backup
touch /var/log/gate-pass-backup.log
chown "$APP_USER:$APP_USER" /var/log/gate-pass-backup.log

# Rotate it. A nightly append with nothing trimming it is a file that grows
# quietly for years and is then discovered by a full disk — on the machine that
# holds the book, which is the worst place to run out of room.
cat > /etc/logrotate.d/gate-pass <<'ROTATE'
/var/log/gate-pass-backup.log {
    weekly
    rotate 12
    compress
    delaycompress
    missingok
    notifempty
    create 0640 __APP_USER__ __APP_USER__
}
ROTATE
sed -i "s/__APP_USER__/$APP_USER/g" /etc/logrotate.d/gate-pass
note "backup log rotates weekly, 12 kept"
note "21:00 daily into $APP_DIR/storage/backups (90 days kept)"

say "Firewall"
if ufw status 2>/dev/null | grep -q "Status: active"; then
    ufw allow 80/tcp >/dev/null
    note "opened port 80"
else
    # Deliberately not enabling ufw here: switching it on without an SSH rule
    # locks you out of a machine you are administering remotely.
    note "ufw is not active — nothing to change"
fi

say "Checking the app actually answers"
# Ask over whatever the site is actually serving. With HTTPS on, port 80 is a
# 301 by design -- demanding 200 there declared a perfectly healthy server
# broken, right after enable-https.sh had finished setting it up.
if [ "$HTTPS_WAS_ON" = "1" ] || grep -q '443 ssl' /etc/nginx/sites-available/gate-pass 2>/dev/null; then
    BASE="https://127.0.0.1"
    CURL="curl -sk"      # -k: the local CA is not in root's trust store
else
    BASE="http://127.0.0.1"
    CURL="curl -s"
fi

code=$($CURL --max-time 10 -o /dev/null -w '%{http_code}' "$BASE/login")
[ "$code" = "200" ] || die "nginx is up but the app did not answer: $BASE/login returned '${code:-nothing}'
       journalctl -u gate-pass -n 30 --no-pager"
note "$BASE/login returns 200 through nginx"

# 200 is not the same as usable. nginx serves static/ itself, so a permissions
# mistake shows up as an unstyled page rather than an error, and the install
# would otherwise report success on something visibly broken.
for asset in /static/css/style.css /static/img/fanzart-logo.png; do
    code=$($CURL --max-time 5 -o /dev/null -w '%{http_code}' "$BASE$asset")
    [ "$code" = "200" ] || die "$asset returned '${code:-nothing}' — nginx cannot read $APP_DIR/static
       (check the directory is traversable: ls -ld $APP_DIR)"
done
note "stylesheets and logo load"

# With TLS on, port 80 must redirect rather than serve.
if [ "$BASE" = "https://127.0.0.1" ]; then
    redirect=$(curl -s --max-time 8 -o /dev/null -w '%{http_code}' http://127.0.0.1/login)
    case "$redirect" in
        30*) note "http://127.0.0.1/login -> $redirect to HTTPS" ;;
        *)   note "WARNING: port 80 returned $redirect, expected a redirect" ;;
    esac
    # And the CA must NOT be redirected, or a machine cannot fetch the file
    # that would let it trust this server in the first place.
    ca=$(curl -s --max-time 8 -o /dev/null -w '%{http_code}' http://127.0.0.1/fanzart-ca.pem)
    [ "$ca" = "200" ] || die "http://127.0.0.1/fanzart-ca.pem returned '${ca:-nothing}'
       Office machines fetch the certificate from there; it must not redirect."
    note "the CA is fetchable over plain HTTP for new machines"
fi

IP=$(hostname -I | awk '{print $1}')
HOST=$(hostname)
TS_IP=$(tailscale ip -4 2>/dev/null | head -1 || true)
TS_NAME=$(tailscale status --json 2>/dev/null \
          | python3 -c 'import json,sys; print((json.load(sys.stdin).get("Self") or {}).get("DNSName","").rstrip("."))' 2>/dev/null || true)
# Is the public link actually serving? Only then is it the address to hand out.
PUBLIC=""
if [ -n "$TS_NAME" ] && tailscale serve status --json 2>/dev/null \
     | python3 -c 'import json,sys; d=json.load(sys.stdin); sys.exit(0 if any((d.get("AllowFunnel") or {}).values()) else 1)' 2>/dev/null; then
    PUBLIC="https://$TS_NAME"
fi

say "Done"
printf "\n"
# The public link first when there is one: it is the address the logistics team
# uses, and the only one that works from a machine with no Tailscale and no
# certificate installed. The LAN addresses still want fanzart-ca.pem.
[ -n "$PUBLIC" ] && \
printf "    The app is at:  \033[1m%s\033[0m   (anywhere, no VPN, real certificate)\n" "$PUBLIC"
printf "%s\033[1mhttps://%s\033[0m        (office LAN)\n" \
       "$([ -n "$PUBLIC" ] && printf '                    ' || printf '    The app is at:  ')" "$IP"
printf "                    \033[1mhttps://%s.local\033[0m  (by name on the LAN)\n" "$HOST"
# Deliberately NOT the Tailscale IP: it resolves only inside the tailnet, and
# handing it out is how somebody ends up on an address that cannot reach the
# app at all. It redirects to the public link anyway.
printf "\n"

if sudo -u "$APP_USER" "$APP_DIR/.venv/bin/python3" -c "
import sys; sys.path.insert(0, '$APP_DIR')
import db; c = db.connect('$APP_DIR/storage/gate_pass.db')
sys.exit(0 if db.count_users(c) == 0 else 1)" 2>/dev/null; then
    note "No accounts yet. Create the first one (it becomes the admin):"
    printf "\n      sudo -u %s %s/.venv/bin/python3 %s/manage_users.py add\n\n" \
        "$APP_USER" "$APP_DIR" "$APP_DIR"
else
    note "Existing accounts kept; the database was not touched."
fi

cat <<TIPS
    Useful:
      sudo systemctl status gate-pass         is it running
      sudo journalctl -u gate-pass -f         watch its log
      sudo $APP_DIR/deploy/install.sh         update to the latest code
      sudo -u $APP_USER $APP_DIR/deploy/backup.sh   back up right now

    Two things worth doing on the router:
      * give this server a fixed IP (a DHCP reservation for $IP), or the
        address changes on reboot and everyone's bookmark breaks
      * when the domain arrives: put it in server_name, run certbot, then add
        Environment="GATE_PASS_HTTPS=1" to the systemd unit and restart

TIPS
