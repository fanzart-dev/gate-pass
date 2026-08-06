#!/usr/bin/env bash
# Back up the gate pass book.
#
#   deploy/backup.sh [destination]     (default: storage/backups)
#
# Uses SQLite's own online backup, NOT `cp`. Copying the file while the app is
# running can capture a half-written page or miss the WAL entirely, producing a
# backup that looks fine and is corrupt. VACUUM INTO takes a consistent snapshot
# of a live database with no downtime, and compacts it on the way out.
#
# Driven through the venv's Python rather than the sqlite3 command line, which
# is not installed on this machine and would be one more thing to keep working.
#
# Run it from cron, nightly:
#   0 21 * * *  /home/fanzart/gate-pass/deploy/backup.sh >> /var/log/gate-pass-backup.log 2>&1

set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEST="${1:-$APP_DIR/storage/backups}"
PYTHON="$APP_DIR/.venv/bin/python3"
[ -x "$PYTHON" ] || PYTHON=python3

KEEP_DAYS=90 DEST="$DEST" APP_DIR="$APP_DIR" "$PYTHON" - <<'PY'
import gzip, os, shutil, sqlite3, sys, time
from datetime import datetime, timedelta
from pathlib import Path

app_dir = Path(os.environ["APP_DIR"])
dest = Path(os.environ["DEST"])
keep_days = int(os.environ["KEEP_DAYS"])
source = app_dir / "storage" / "gate_pass.db"
now = datetime.now()

def log(msg):
    print(f"{now:%F %T}  {msg}", flush=True)

if not source.exists():
    log(f"ERROR: no database at {source}"); sys.exit(1)

dest.mkdir(parents=True, exist_ok=True)
out = dest / f"gate_pass-{now:%Y%m%d-%H%M%S}.db"

conn = sqlite3.connect(source, timeout=30)
try:
    conn.execute("VACUUM INTO ?", (str(out),))
finally:
    conn.close()

# Prove the copy is readable and complete before trusting it. A backup nobody
# has checked is a guess.
check = sqlite3.connect(out)
try:
    if check.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
        log(f"ERROR: {out} failed integrity_check — kept for inspection"); sys.exit(1)
    passes = check.execute("SELECT COUNT(*) FROM gate_passes").fetchone()[0]
    users = check.execute("SELECT COUNT(*) FROM users").fetchone()[0]
finally:
    check.close()

with open(out, "rb") as raw, gzip.open(f"{out}.gz", "wb") as gz:
    shutil.copyfileobj(raw, gz)
out.unlink()
size = Path(f"{out}.gz").stat().st_size / 1024
log(f"ok  {out}.gz  ({passes} gate passes, {users} accounts, {size:.0f} KB)")

# Age out old copies. Nothing is deleted until the new backup above has been
# written AND verified, so a failed run never removes the last good one.
cutoff = time.time() - keep_days * 86400
for old in dest.glob("gate_pass-*.db.gz"):
    if old.stat().st_mtime < cutoff:
        old.unlink()
        log(f"removed expired {old.name}")
PY
