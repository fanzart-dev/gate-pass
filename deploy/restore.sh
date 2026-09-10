#!/usr/bin/env bash
# Put the gate pass book back, and prove beforehand that it can be put back.
#
#   deploy/restore.sh --drill              # prove the newest backup is usable
#   deploy/restore.sh --drill <file.gz>    # prove a particular one is
#   deploy/restore.sh --list               # what backups exist, and how old
#   sudo deploy/restore.sh --restore <file.gz>   # actually put it back
#
# WHY THE DRILL IS THE IMPORTANT HALF
# ---------------------------------------------------------------------------
# A backup nobody has restored is a guess. It can be the right size, pass an
# integrity check, and still be useless — the wrong database, an empty one, a
# schema the current code cannot open, a file that gunzip cannot finish. None
# of that is visible until the morning the server is gone, which is the worst
# possible moment to find out.
#
# --drill answers the only question that matters: if the office server died
# right now, would this file give us the register back? It unpacks the backup
# to a temporary copy, opens it with the app's OWN database module — so the
# schema is checked by the code that will have to read it — counts what is
# inside, and names the newest gate pass in it. It touches nothing live and
# needs no privileges. Run it monthly; it takes seconds.
#
# --restore is the other half, and it is deliberately harder to invoke: it
# stops the service, keeps the current database beside the restored one rather
# than deleting it, and starts back up.

set -uo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKUP_DIR="${GATE_PASS_BACKUP_DIR:-$APP_DIR/storage/backups}"
LIVE_DB="$APP_DIR/storage/gate_pass.db"
PYTHON="$APP_DIR/.venv/bin/python3"
[ -x "$PYTHON" ] || PYTHON=python3

MODE="${1:-}"
FILE="${2:-}"

say()  { printf "\n\033[1m==> %s\033[0m\n" "$*"; }
note() { printf "    %s\n" "$*"; }
die()  { printf "\n\033[31mERROR: %s\033[0m\n" "$*" >&2; exit 1; }

newest_backup() {
    ls -1t "$BACKUP_DIR"/gate_pass-*.db.gz 2>/dev/null | head -1
}

# ------------------------------------------------------------------- list ----
if [ "$MODE" = "--list" ]; then
    say "Backups in $BACKUP_DIR"
    found=0
    for f in $(ls -1t "$BACKUP_DIR"/gate_pass-*.db.gz 2>/dev/null); do
        age=$(( ( $(date +%s) - $(stat -c %Y "$f") ) / 86400 ))
        note "$(basename "$f")  $(du -h "$f" | cut -f1)  ${age}d old"
        found=$((found + 1))
    done
    [ "$found" -gt 0 ] || note "none — nothing has ever been backed up here"
    exit 0
fi

[ "$MODE" = "--drill" ] || [ "$MODE" = "--restore" ] \
    || die "usage: $0 --drill | --list | --restore <file.gz>"

[ -n "$FILE" ] || FILE="$(newest_backup)"
[ -n "$FILE" ] || die "no backups found in $BACKUP_DIR"
[ -f "$FILE" ] || die "no such file: $FILE"

# ------------------------------------------------------------------ drill ----
say "Checking $(basename "$FILE")"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

gunzip -c "$FILE" > "$WORK/restored.db" 2>/dev/null \
    || die "gunzip could not unpack it — this backup is not usable"
note "unpacked: $(du -h "$WORK/restored.db" | cut -f1)"

# Opened with the app's own module, not with a bare sqlite3 connection. That is
# the difference between "the file parses" and "the code that has to read this
# can read it" — including the migrations, which is what would actually run.
APP_DIR="$APP_DIR" WORK="$WORK" "$PYTHON" - <<'PY' || die "the backup could not be opened by the application"
import os, sys
sys.path.insert(0, os.environ["APP_DIR"])
from pathlib import Path
import db

path = Path(os.environ["WORK"]) / "restored.db"
conn = db.connect(path)

passes = db.count_gate_passes(conn)
users = db.count_users(conn)
cartons = db.carton_mapping_count(conn)
version = conn.execute("PRAGMA user_version").fetchone()[0]

rows = sorted(db.list_gate_passes(conn), key=lambda p: p["serial_seq"])
newest = rows[-1] if rows else None

print(f"    schema version   {version}")
print(f"    gate passes      {passes}")
print(f"    accounts         {users}")
print(f"    carton mappings  {cartons}")
if newest:
    print(f"    newest pass      {newest['serial_no']}  {newest['invoice_no']}"
          f"  issued {newest['issued_at']}")
    print(f"    next would be    {db.next_serial_preview(conn)}")

# An empty book restores perfectly and tells you nothing. Say so loudly rather
# than reporting success for a file that would lose everything.
if passes == 0:
    print("    WARNING: this backup contains NO gate passes")
if users == 0:
    print("    WARNING: this backup contains NO accounts — nobody could sign in")
conn.close()
PY

if [ "$MODE" = "--drill" ]; then
    cat <<DONE

    The backup is readable by this version of the app and holds a real book.
    Nothing live was touched.

      sudo $0 --restore $FILE     would put this back

DONE
    exit 0
fi

# ---------------------------------------------------------------- restore ----
[ "$(id -u)" -eq 0 ] || die "restoring needs sudo"

say "Restoring — this replaces the live register"
note "current database: $LIVE_DB"
note "restoring from:   $FILE"
printf "\n    Type 'restore' to continue: "
read -r answer
[ "$answer" = "restore" ] || die "not confirmed, nothing changed"

systemctl stop gate-pass 2>/dev/null && note "service stopped"

# The current database is MOVED aside, never deleted. If the backup turns out
# to be the wrong one, or older than somebody thought, the thing it replaced is
# still on disk — restoring the wrong day and destroying the right one in the
# same command would be a second disaster on top of the first.
if [ -f "$LIVE_DB" ]; then
    aside="$LIVE_DB.replaced-$(date +%Y%m%d-%H%M%S)"
    mv "$LIVE_DB" "$aside"
    # The WAL and shared-memory files belong to the database that just moved.
    # Leaving them beside a DIFFERENT database is how a restore comes up with
    # rows from both.
    rm -f "$LIVE_DB-wal" "$LIVE_DB-shm"
    note "previous database kept at $(basename "$aside")"
fi

cp "$WORK/restored.db" "$LIVE_DB"
chown "$(stat -c '%U:%G' "$APP_DIR")" "$LIVE_DB" 2>/dev/null || true
note "restored"

systemctl start gate-pass 2>/dev/null && sleep 3
if systemctl is-active --quiet gate-pass; then
    note "service is back up"
else
    die "the service did not start — check: journalctl -u gate-pass -n 40"
fi

cat <<DONE

    Restored. Check the register in a browser before telling anyone it is done.

    The database this replaced is still on disk. Delete it only once you are
    certain the restore is the one you wanted.

DONE
