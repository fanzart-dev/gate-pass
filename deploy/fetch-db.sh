#!/usr/bin/env bash
# Bring a copy of the live book down to this machine.
#
#   deploy/fetch-db.sh                 # into ./storage/fetched/
#   deploy/fetch-db.sh ~/Downloads     # or wherever
#
# Run this ON YOUR LAPTOP, not on the server. It will ask for your sudo password
# on the server once, because the book is owned by the service account.
#
# Why not just scp the file
# -------------------------------------------------------------------------
# Two reasons, and both produce a copy that looks fine and is wrong.
#
#   * The database runs in WAL mode. Recent commits live in gate_pass.db-wal
#     until a checkpoint folds them in, so copying gate_pass.db on its own can
#     silently miss the last few gate passes issued. This takes a proper
#     snapshot instead (VACUUM INTO), which is consistent by construction and
#     safe to run while people are using the app.
#   * storage/ is 0750 and owned by `gatepass`, so scp as your own account
#     cannot read it anyway.
#
# What you get is verified before it leaves the server and again after it
# lands, so a copy that arrives is a copy you can trust.

set -uo pipefail

SERVER="${GP_SERVER:-fanzart@100.123.239.33}"
APP_DIR="${GP_APP_DIR:-/opt/gate-pass}"
APP_USER="${GP_APP_USER:-gatepass}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEST="${1:-$HERE/storage/fetched}"

say()  { printf "\n\033[1m==> %s\033[0m\n" "$*"; }
note() { printf "    %s\n" "$*"; }
die()  { printf "\n\033[31mERROR: %s\033[0m\n" "$*" >&2; exit 1; }

mkdir -p "$DEST" || die "cannot write to $DEST"
STAMP="$(date +%Y%m%d-%H%M%S)"
REMOTE_TMP="/tmp/gate-pass-fetch-$STAMP"

say "Taking a consistent snapshot on the server"
note "you will be asked for your password on $SERVER"
# -t so the sudo prompt actually reaches you. backup.sh does the snapshot,
# runs integrity_check on the result, counts the rows and gzips it — the same
# path the nightly backup uses, so this is exercising a tested route rather
# than a second one written for convenience.
ssh -t "$SERVER" "
  set -e
  sudo mkdir -p '$REMOTE_TMP'
  sudo chown '$APP_USER:$APP_USER' '$REMOTE_TMP'
  sudo -u '$APP_USER' '$APP_DIR/deploy/backup.sh' '$REMOTE_TMP'
  sudo chown \$(id -un):\$(id -gn) '$REMOTE_TMP'/*.gz
" || die "the snapshot failed — nothing was copied"

say "Copying it down"
scp -q "$SERVER:$REMOTE_TMP/*.gz" "$DEST/" || die "scp failed; the snapshot is still at $REMOTE_TMP on the server"
ssh "$SERVER" "sudo rm -rf '$REMOTE_TMP'" >/dev/null 2>&1 \
  || note "could not remove $REMOTE_TMP on the server — tidy it up by hand"

FILE="$(ls -t "$DEST"/gate_pass-*.db.gz 2>/dev/null | head -1)"
[ -n "$FILE" ] || die "nothing arrived in $DEST"
gunzip -kf "$FILE" || die "the copy will not decompress"
PLAIN="${FILE%.gz}"

say "Checking what arrived"
PY="$HERE/.venv/bin/python3"; [ -x "$PY" ] || PY=python3
"$PY" - "$PLAIN" <<'CHECK' || die "the copy did not verify — do NOT rely on it"
import sqlite3, sys
from pathlib import Path
path = Path(sys.argv[1])
conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
    print("    integrity_check FAILED"); sys.exit(1)
print("    integrity_check ok")
def n(q):
    try: return conn.execute(q).fetchone()[0]
    except sqlite3.Error: return "-"
print(f"    schema version   {conn.execute('PRAGMA user_version').fetchone()[0]}")
print(f"    gate passes      {n('SELECT COUNT(*) FROM gate_passes')}")
print(f"    accounts         {n('SELECT COUNT(*) FROM users')}")
print(f"    carton mappings  {n('SELECT COUNT(*) FROM item_carton_mappings')}")
last = n("SELECT serial_no FROM gate_passes ORDER BY serial_seq DESC LIMIT 1")
print(f"    last serial      {last}")
print(f"    size             {path.stat().st_size/1024/1024:.1f} MB")
CHECK

cat <<DONE

    $PLAIN
    $FILE   (keep this one; the .db is the working copy)

    Read it without touching the live book:

      sqlite3 "$PLAIN" "SELECT serial_no, customer_name, issued_at
                        FROM gate_passes ORDER BY id DESC LIMIT 10;"

    Or open the whole app against it, on a spare port, touching nothing on
    the server. Put the copy in a directory of its own first — the app keeps
    its uploaded invoices and its signing key beside the database:

      mkdir -p /tmp/gp-review && cp "$PLAIN" /tmp/gp-review/gate_pass.db
      GATE_PASS_STORAGE=/tmp/gp-review GATE_PASS_PORT=8099 \
        .venv/bin/python3 app.py

    then http://127.0.0.1:8099 — the real screens, the real register, and a
    sign-in that wants the same passwords as the server, because this is the
    same book.

    THIS COPY CONTAINS EVERY GATE PASS AND EVERY PASSWORD HASH. It is
    gitignored under storage/, and it belongs on an encrypted disk or nowhere.
    Delete it when you are done with it.

DONE
