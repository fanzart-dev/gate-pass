#!/usr/bin/env bash
# A second Gate Pass anyone can be handed, use properly, and break.
#
#   sudo deploy/demo-instance.sh --install    # stand it up and publish it
#   sudo deploy/demo-instance.sh --reset      # wipe it back to sample data
#   sudo deploy/demo-instance.sh --status     # where it is and whether it answers
#   sudo deploy/demo-instance.sh --remove     # take it down and unpublish
#
# WHY A SECOND INSTANCE AND NOT A SECOND ACCOUNT
# ---------------------------------------------------------------------------
# Somebody reviewing this app needs to USE it — upload an invoice, issue a
# pass, cancel one, print it — because that is the only way to have an opinion
# worth hearing. Every one of those actions is permanent in the live book: a
# serial cannot be deleted, cancelling keeps the number, and the register is
# the record the company is audited on. A read-only login avoids the damage by
# preventing the review.
#
# So this runs the SAME CODE against a DIFFERENT DATABASE. Different storage
# directory, different port, different systemd unit. Nothing that happens here
# can reach the real register, so the reviewer can do anything at all, and the
# whole thing goes back to a known state with --reset.
#
# It is published on the tailnet's Funnel at port 8443, which needs no domain,
# no DNS change, no port forwarding and no Tailscale on the reviewer's machine.

set -uo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_USER="${APP_USER:-gatepass}"
DEMO_DIR="${DEMO_DIR:-/opt/gate-pass-demo}"
DEMO_PORT=8092
FUNNEL_PORT=8443
MODE="${1:-}"

say()  { printf "\n\033[1m==> %s\033[0m\n" "$*"; }
note() { printf "    %s\n" "$*"; }
die()  { printf "\n\033[31mERROR: %s\033[0m\n" "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "run with sudo"
[ -d "$APP_DIR/.venv" ] || die "no virtualenv at $APP_DIR/.venv — run deploy/install.sh first"

tailnet_name() {
    tailscale status --json 2>/dev/null \
      | python3 -c 'import json,sys; print((json.load(sys.stdin).get("Self") or {}).get("DNSName","").rstrip("."))' 2>/dev/null
}

seed() {
    # Sample data so the register, the reports and the print view all have
    # something in them on the first click. Written through the app's own db
    # module, so it can never drift from the real schema.
    APP_DIR="$APP_DIR" DEMO_DIR="$DEMO_DIR" DEMO_PASSWORD="$DEMO_PASSWORD" \
    "$APP_DIR/.venv/bin/python" - <<'PY'
import os, sys
sys.path.insert(0, os.environ["APP_DIR"])
from pathlib import Path
import db

storage = Path(os.environ["DEMO_DIR"])
(storage / "invoices").mkdir(parents=True, exist_ok=True)
conn = db.connect(storage / "gate_pass.db")

if db.count_users(conn) == 0:
    # Full permissions: the reviewer has to be able to reach everything,
    # including Settings and the numbering, or the feedback is about a cage
    # rather than about the app.
    db.create_user(conn, "reviewer", "Reviewer", os.environ["DEMO_PASSWORD"],
                   status=db.APPROVED, is_admin=True,
                   permissions=dict(db.ALL_PERMISSIONS))
    print("  created the 'reviewer' account with every permission")

if db.count_gate_passes(conn) == 0:
    db.update_settings(conn, show_total_qty="1", show_total_cartons="1")
    samples = [
        ("Golden Touch Exports", "MBS DECOR LLP", "LP 262700338", "02-09-2026",
         [("WINDFLOWER - FANDELIER", 2), ("PHOENIX 52 WALNUT", 4)]),
        ("Golden Touch Exports", "NIKSHAN ELECTRONICS", "FR 262702181", "03-09-2026",
         [("AEROSLIM 1200MM WHITE", 6)]),
        ("FANZART LLP", "BCP ASSOCIATES LLP", "RT 262700289", "04-09-2026",
         [("BRISA 1400MM", 3), ("TIVOLI 48 BRASS", 1), ("STELLA LED", 8)]),
    ]
    for supplier, customer, number, when, items in samples:
        db.create_gate_pass(
            conn, None, supplier, customer, number, when, "KA 01 AB 1234",
            [{"item_name": n, "quantity": q, "cartons": q} for n, q in items],
            prepared_by="Reviewer", remarks="Sample data — safe to change or delete.")
    print(f"  seeded {len(samples)} sample gate passes")

print(f"  next gate pass here would be {db.next_serial_preview(conn)}")
conn.close()
PY
}

# ------------------------------------------------------------------ status ---
if [ "$MODE" = "--status" ]; then
    NAME="$(tailnet_name)"
    say "Demo instance"
    note "code      $APP_DIR   (shared with live — same app, same version)"
    note "data      $DEMO_DIR  (its own database, shared with nothing)"
    systemctl is-active --quiet gate-pass-demo \
      && note "service   running on 127.0.0.1:$DEMO_PORT" \
      || note "service   NOT running"
    if [ -n "$NAME" ]; then
        note "link      https://$NAME:$FUNNEL_PORT"
        code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 20 "https://$NAME:$FUNNEL_PORT/login")
        note "answers   ${code:-no answer}"
    fi
    exit 0
fi

# ------------------------------------------------------------------ remove ---
if [ "$MODE" = "--remove" ]; then
    say "Taking the demo down"
    systemctl disable --now gate-pass-demo >/dev/null 2>&1 && note "service stopped"
    rm -f /etc/systemd/system/gate-pass-demo.service
    systemctl daemon-reload
    tailscale funnel --https="$FUNNEL_PORT" off >/dev/null 2>&1 && note "unpublished"
    note "the data is still at $DEMO_DIR — delete it by hand if you want it gone"
    exit 0
fi

# ------------------------------------------------------------------- reset ---
if [ "$MODE" = "--reset" ]; then
    [ -d "$DEMO_DIR" ] || die "no demo instance at $DEMO_DIR"
    say "Resetting the demo"
    systemctl stop gate-pass-demo >/dev/null 2>&1
    DEMO_PASSWORD="${DEMO_PASSWORD:-}"
    [ -n "$DEMO_PASSWORD" ] || die "set DEMO_PASSWORD to the password the reviewer should use, e.g.
       sudo DEMO_PASSWORD='...' $0 --reset"
    rm -f "$DEMO_DIR/gate_pass.db" "$DEMO_DIR/gate_pass.db-wal" "$DEMO_DIR/gate_pass.db-shm"
    rm -rf "${DEMO_DIR:?}/invoices"
    seed || die "seeding failed"
    chown -R "$APP_USER:$APP_USER" "$DEMO_DIR"
    systemctl start gate-pass-demo
    note "reset — the demo is back to sample data"
    exit 0
fi

# ----------------------------------------------------------------- install ---
[ "$MODE" = "--install" ] || die "usage: $0 --install | --reset | --status | --remove"

DEMO_PASSWORD="${DEMO_PASSWORD:-}"
[ -n "$DEMO_PASSWORD" ] || die "choose the password the reviewer will sign in with:
       sudo DEMO_PASSWORD='something-long' $0 --install
     Pick it yourself and send it to them separately from the link — it is not
     generated here, and it is never written to a file or into git."

id -u "$APP_USER" >/dev/null 2>&1 || die "no such user: $APP_USER"

say "Creating $DEMO_DIR"
mkdir -p "$DEMO_DIR/invoices"
note "made"

say "Seeding a database of its own"
seed || die "seeding failed"
chown -R "$APP_USER:$APP_USER" "$DEMO_DIR"
chmod 750 "$DEMO_DIR"

say "Installing the service"
sed -e "s|__APP_DIR__|$APP_DIR|g" \
    -e "s|__APP_USER__|$APP_USER|g" \
    -e "s|__DEMO_DIR__|$DEMO_DIR|g" \
    "$APP_DIR/deploy/gate-pass-demo.service" > /etc/systemd/system/gate-pass-demo.service \
    || die "could not write the unit"
systemctl daemon-reload
systemctl enable --quiet gate-pass-demo
systemctl restart gate-pass-demo
sleep 3
systemctl is-active --quiet gate-pass-demo || {
    journalctl -u gate-pass-demo -n 30 --no-pager
    die "the demo service did not start — the log above says why"
}
note "running on 127.0.0.1:$DEMO_PORT"

say "Publishing it"
NAME="$(tailnet_name)"
[ -n "$NAME" ] || die "tailscaled is not up, so there is nothing to publish through"
# Funnel accepts 443, 8443 and 10000 only. The live site holds 443, so the demo
# takes 8443 — which needs no domain, no DNS change and no port forwarding, and
# works from anywhere without Tailscale on the far end.
tailscale funnel --bg --https="$FUNNEL_PORT" "http://127.0.0.1:$DEMO_PORT" >/dev/null 2>&1 \
    || die "could not publish on $FUNNEL_PORT — is Funnel enabled for this node?"

say "Checking it from the outside"
# Public DNS and a forced address, for the same reason watch-public-link.sh
# does it: resolving normally here would go over the tailnet and prove nothing
# about whether a laptop in San Francisco can open it.
ok=0
for attempt in 1 2 3 4 5 6; do
    for ip in $(dig +short @1.1.1.1 "$NAME" A 2>/dev/null | grep -E '^[0-9.]+$'); do
        code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 20 \
               --resolve "$NAME:$FUNNEL_PORT:$ip" "https://$NAME:$FUNNEL_PORT/login")
        case "$code" in
            200|301|302) note "reachable via $ip -> $code"; ok=1 ;;
        esac
    done
    [ "$ok" = "1" ] && break
    note "waiting for the certificate and ingress (attempt $attempt)"
    sleep 10
done
[ "$ok" = "1" ] || die "it is not answering from the public internet yet.
       Give it a minute and run: sudo $0 --status"

cat <<DONE

    ------------------------------------------------------------------
    Send these two things separately.

      Link      https://$NAME:$FUNNEL_PORT
      Username  reviewer
      Password  the one you passed in as DEMO_PASSWORD

    It is a sandbox. Its own database, its own numbering, seeded with
    three sample passes. Every page carries a banner saying so. Nothing
    done there can reach the live register.

      sudo $0 --reset    put it back to sample data
      sudo $0 --status   check it is still answering
      sudo $0 --remove   take it down when the review is done

    The live site is untouched and still at https://$NAME
    ------------------------------------------------------------------

DONE
