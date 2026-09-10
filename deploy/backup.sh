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

# Where else a copy goes. A backup on the same disk as the database protects
# against a mistake — a bad import, a wrong delete — and against nothing else.
# It does not survive the disk failing, the machine being stolen, or the office
# flooding, and this app deletes the source PDFs once a pass is issued, so the
# register is the ONLY copy of the book.
#
# Set in /opt/gate-pass/.env, space-separated, anything scp understands:
#
#   GATE_PASS_BACKUP_MIRRORS="fanzart@100.123.71.31:/home/fanzart/gate-pass-backups /mnt/usb/gate-pass"
#
# A tailnet address works from anywhere the other machine happens to be, which
# is the point: the second copy should not be in the same building.
[ -f "$APP_DIR/.env" ] && . "$APP_DIR/.env"
MIRRORS="${GATE_PASS_BACKUP_MIRRORS:-}"

OUTPUT="$(KEEP_DAYS=90 DEST="$DEST" APP_DIR="$APP_DIR" "$PYTHON" - <<'PY'
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

# Named on stdout so the shell below knows what to copy, without guessing at
# the newest file and racing a concurrent run.
print(f"BACKUP_FILE={out}.gz")
PY
)"

# The log gets everything the snapshot said; BACKUP_FILE is for this script.
printf '%s\n' "$OUTPUT" | grep -v '^BACKUP_FILE=' || true
BACKUP_FILE="$(printf '%s\n' "$OUTPUT" | sed -n 's/^BACKUP_FILE=//p')"

stamp() { printf '%s  %s\n' "$(date '+%F %T')" "$*"; }

if [ -z "$BACKUP_FILE" ] || [ ! -f "$BACKUP_FILE" ]; then
    stamp "ERROR: the snapshot did not report a file — nothing to mirror"
    exit 1
fi

# ------------------------------------------------------------------ mirrors --
if [ -z "$MIRRORS" ]; then
    stamp "WARNING: no off-machine copy. The only backup of the register is on"
    stamp "  the same disk as the register. Set GATE_PASS_BACKUP_MIRRORS in"
    stamp "  $APP_DIR/.env — see deploy/env.example."
    exit 0
fi

LOCAL_SUM="$(sha256sum "$BACKUP_FILE" | awk '{print $1}')"
failed=0
for target in $MIRRORS; do
    name="$(basename "$BACKUP_FILE")"
    if [ -d "$target" ]; then
        # A mounted disk: a USB stick, a NAS share.
        if cp "$BACKUP_FILE" "$target/$name" 2>/dev/null; then
            remote_sum="$(sha256sum "$target/$name" | awk '{print $1}')"
        else
            remote_sum=""
        fi
    else
        # Anything scp understands, including a tailnet address.
        if scp -q -o BatchMode=yes -o ConnectTimeout=30 "$BACKUP_FILE" "$target/" 2>/dev/null; then
            host="${target%%:*}"; path="${target#*:}"
            remote_sum="$(ssh -o BatchMode=yes -o ConnectTimeout=30 "$host" \
                          "sha256sum '$path/$name' 2>/dev/null | awk '{print \$1}'" 2>/dev/null)"
        else
            remote_sum=""
        fi
    fi

    # Compared, not assumed. A copy that silently truncated is the backup you
    # discover is useless on the day you need it.
    if [ "$remote_sum" = "$LOCAL_SUM" ]; then
        stamp "mirrored to $target (sha256 matches)"
    else
        stamp "ERROR: mirror to $target FAILED or does not match"
        failed=1
    fi
done

[ "$failed" -eq 0 ] || stamp "at least one off-machine copy did not land — the register is not safe"
exit $failed
