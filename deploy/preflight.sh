#!/usr/bin/env bash
# Is this machine fit to hand to the office?
#
#   sudo deploy/preflight.sh
#
# Read-only: it changes nothing, it only looks. Run it before giving anyone the
# link, and again after any update. Every check prints why it matters, so a
# failure tells you what to do rather than just that something is wrong.
#
# Exits non-zero if anything FAILED, so it can gate a deploy.

set -uo pipefail

APP_DIR="${GP_APP_DIR:-/opt/gate-pass}"
APP_USER="${GP_APP_USER:-gatepass}"
PORT="${GP_PORT:-8090}"
SERVICE="gate-pass"

pass=0; fail=0; warn=0
ok()   { printf "  \033[32mPASS\033[0m  %s\n" "$1"; pass=$((pass+1)); }
bad()  { printf "  \033[31mFAIL\033[0m  %s\n" "$1"; [ -n "${2:-}" ] && printf "        %s\n" "$2"; fail=$((fail+1)); }
soft() { printf "  \033[33mWARN\033[0m  %s\n" "$1"; [ -n "${2:-}" ] && printf "        %s\n" "$2"; warn=$((warn+1)); }
head_() { printf "\n\033[1m%s\033[0m\n" "$1"; }

head_ "The service"
systemctl is-active --quiet "$SERVICE" \
  && ok "$SERVICE is running" \
  || bad "$SERVICE is not running" "journalctl -u $SERVICE -n 40 --no-pager"
# Enabled is the one that matters for a power cut: active only says it is up now.
systemctl is-enabled --quiet "$SERVICE" \
  && ok "and enabled, so it comes back after a reboot" \
  || bad "$SERVICE is NOT enabled — a power cut leaves the office with no book" \
         "sudo systemctl enable $SERVICE"
unit="/etc/systemd/system/$SERVICE.service"
grep -q "^Restart=always" "$unit" 2>/dev/null \
  && ok "restarts automatically if it crashes" \
  || bad "no Restart=always in $unit"
if systemctl show "$SERVICE" -p NRestarts --value | grep -qvx 0; then
  soft "it has restarted $(systemctl show "$SERVICE" -p NRestarts --value) time(s) since boot" \
       "not fatal, but worth reading the log before launch day"
fi

head_ "Serving"
ss -lntH "( sport = :$PORT )" | grep -q 127.0.0.1 \
  && ok "gunicorn is listening on 127.0.0.1:$PORT (loopback only)" \
  || bad "nothing is listening on 127.0.0.1:$PORT"
nginx -t >/dev/null 2>&1 \
  && ok "the nginx configuration parses" \
  || bad "nginx -t fails" "sudo nginx -t"
systemctl is-active --quiet nginx && ok "nginx is running" || bad "nginx is not running"

code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "http://127.0.0.1/login")
case "$code" in
  200) ok "the sign-in page answers through nginx (200)" ;;
  301|302) ok "port 80 redirects to HTTPS ($code) — expected once TLS is on" ;;
  *)   bad "http://127.0.0.1/login returned '${code:-nothing}'" ;;
esac

# The body limit has to be at least what the app itself accepts, or nginx
# rejects a large scanned invoice with its own 413 before Flask can explain.
app_limit=$(grep -oP 'MAX_CONTENT_LENGTH.*?=\s*\K[0-9]+' "$APP_DIR/app.py" | head -1)
ngx_limit=$(grep -ohP 'client_max_body_size\s+\K[0-9]+' /etc/nginx/sites-enabled/gate-pass | head -1)
if [ -n "$app_limit" ] && [ -n "$ngx_limit" ] && [ "$ngx_limit" -ge "$app_limit" ]; then
  ok "nginx accepts ${ngx_limit}M, the app accepts ${app_limit}M — uploads reach the app"
else
  bad "nginx client_max_body_size (${ngx_limit:-?}M) is below the app's ${app_limit:-?}M" \
      "a big scanned invoice would 413 at nginx with no explanation"
fi

head_ "The book"
DB="$APP_DIR/storage/gate_pass.db"
if [ -f "$DB" ]; then
  ok "the database is at $DB (persistent, not /tmp)"
  integrity=$(sudo -u "$APP_USER" "$APP_DIR/.venv/bin/python3" - <<PY 2>/dev/null
import sqlite3
c = sqlite3.connect("file:$DB?mode=ro", uri=True)
print(c.execute("PRAGMA integrity_check").fetchone()[0])
PY
)
  [ "$integrity" = "ok" ] && ok "integrity_check is ok" || bad "integrity_check said: ${integrity:-unreadable}"

  read -r passes cartons <<<"$(sudo -u "$APP_USER" "$APP_DIR/.venv/bin/python3" - <<PY 2>/dev/null
import sqlite3
c = sqlite3.connect("file:$DB?mode=ro", uri=True)
def n(q):
    try: return c.execute(q).fetchone()[0]
    except Exception: return -1
print(n("SELECT COUNT(*) FROM gate_passes"), n("SELECT COUNT(*) FROM item_carton_mappings"))
PY
)"
  ok "$passes gate passes in the register"
  if [ "${cartons:-0}" -gt 0 ]; then
    ok "$cartons items in the carton master list"
  else
    bad "the carton master list is EMPTY — every carton box will print blank" \
        "sudo -u $APP_USER $APP_DIR/.venv/bin/python $APP_DIR/manage_cartons.py import '/tmp/Box Qty.xlsx'"
  fi
else
  bad "no database at $DB"
fi

head_ "Backups"
[ -f /etc/cron.d/gate-pass-backup ] \
  && ok "the nightly backup is scheduled ($(grep -oE '^[0-9]+ [0-9]+' /etc/cron.d/gate-pass-backup | head -1) daily)" \
  || bad "no /etc/cron.d/gate-pass-backup"
latest=$(ls -t "$APP_DIR/storage/backups/"*.db.gz 2>/dev/null | head -1)
if [ -n "$latest" ]; then
  age=$(( ( $(date +%s) - $(stat -c %Y "$latest") ) / 3600 ))
  [ "$age" -lt 48 ] \
    && ok "most recent backup is ${age}h old ($(basename "$latest"))" \
    || soft "most recent backup is ${age}h old" "the schedule may not be firing"
else
  soft "no backup has run yet" "sudo -u $APP_USER $APP_DIR/deploy/backup.sh"
fi
[ -f /etc/logrotate.d/gate-pass ] \
  && ok "the backup log rotates" \
  || soft "no logrotate for /var/log/gate-pass-backup.log" "it will grow for ever"

head_ "Room and logs"
avail=$(df -Pk "$APP_DIR" | awk 'NR==2{print int($4/1024)}')
pct=$(df -Ph "$APP_DIR" | awk 'NR==2{print $5}')
if [ "$avail" -gt 1024 ]; then ok "${avail}MB free on the volume holding the book (${pct} used)"
else bad "only ${avail}MB free (${pct} used) — a full disk stops the register"; fi
errs=$(journalctl -u "$SERVICE" --since "24 hours ago" -p err --no-pager 2>/dev/null | grep -c . || true)
[ "${errs:-0}" -le 1 ] \
  && ok "no errors in the service log in the last 24h" \
  || soft "$errs error lines in the last 24h" "journalctl -u $SERVICE -p err --since '24 hours ago'"
grep -q "debug=True" "$APP_DIR/app.py" \
  && bad "app.py has debug=True — never on a live book" \
  || ok "debug mode is off"

head_ "Exposure"
if command -v tailscale >/dev/null && tailscale serve status --json 2>/dev/null | grep -q '"AllowFunnel"'; then
  if tailscale serve status --json 2>/dev/null | python3 -c 'import json,sys; d=json.load(sys.stdin); sys.exit(0 if any((d.get("AllowFunnel") or {}).values()) else 1)'; then
    soft "this machine is published on the PUBLIC internet via Tailscale Funnel" \
         "intended for the logistics team; the sign-in page is reachable by anyone"
    grep -q "LOGIN_MAX_PER_USERNAME" "$APP_DIR/db.py" \
      && ok "sign-in attempts are throttled, which a public login form must be" \
      || bad "public, and the sign-in form has no throttle"
  fi
fi

printf "\n\033[1m%d passed, %d warnings, %d failed\033[0m\n" "$pass" "$warn" "$fail"
[ "$fail" -eq 0 ] && printf "Fit to hand over.\n" || printf "Fix the failures above before sharing the link.\n"
exit $(( fail > 0 ? 1 : 0 ))
