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
    sudo -u "$APP_USER" git -C "$APP_DIR" fetch --quiet origin
    sudo -u "$APP_USER" git -C "$APP_DIR" reset --quiet --hard origin/master
    note "updated to $(sudo -u "$APP_USER" git -C "$APP_DIR" log --oneline -1)"
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
cat > /etc/systemd/system/gate-pass.service <<UNIT
[Unit]
Description=Fanzart Gate Pass
After=network.target

[Service]
Type=notify
User=$APP_USER
Group=$APP_USER
WorkingDirectory=$APP_DIR
Environment="PATH=$APP_DIR/.venv/bin"
# Set GATE_PASS_HTTPS=1 only once a certificate is live. On plain HTTP it marks
# the session cookie HTTPS-only, and nobody can sign in.
ExecStart=$APP_DIR/.venv/bin/gunicorn -c deploy/gunicorn.conf.py app:app
ExecReload=/bin/kill -s HUP \$MAINPID
Restart=always
RestartSec=3

# The book is the only thing this service may write to.
ReadWritePaths=$APP_DIR/storage
ProtectSystem=strict
ProtectHome=read-only
PrivateTmp=true
NoNewPrivileges=true
ProtectKernelTunables=true
ProtectControlGroups=true
RestrictSUIDSGID=true

[Install]
WantedBy=multi-user.target
UNIT
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
if curl -sf --max-time 10 -o /dev/null -w '%{http_code}' http://127.0.0.1/login | grep -q 200; then
    note "http://127.0.0.1/login returns 200 through nginx"
else
    die "nginx is up but the app did not answer on port 80 — check: journalctl -u gate-pass -n 30"
fi
# The page returning 200 is not the same as the page being usable. nginx serves
# static/ itself, so a permissions mistake shows up as an unstyled page rather
# than an error, and the install would otherwise report success.
for asset in /static/css/style.css /static/img/fanzart-logo.png; do
    code=$(curl -s --max-time 5 -o /dev/null -w '%{http_code}' "http://127.0.0.1$asset")
    [ "$code" = "200" ] || die "$asset returned $code — nginx cannot read $APP_DIR/static
       (check the directory is traversable: ls -ld $APP_DIR)"
done
note "stylesheets and logo load"

IP=$(hostname -I | awk '{print $1}')
HOST=$(hostname)
TS_IP=$(tailscale ip -4 2>/dev/null | head -1 || true)

say "Done"
printf "\n    The app is at:  \033[1mhttp://%s\033[0m          (office LAN)\n" "$IP"
printf "                    \033[1mhttp://%s.local\033[0m   (by name on the LAN)\n" "$HOST"
[ -n "$TS_IP" ] && \
printf "                    \033[1mhttp://%s\033[0m     (Tailscale, from anywhere)\n" "$TS_IP"
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
