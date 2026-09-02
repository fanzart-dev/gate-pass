#!/usr/bin/env bash
# Export the whole book, for archiving or for moving it somewhere else.
#
#   sudo -u gatepass deploy/export-db.sh [destination]
#
# Produces four things in one timestamped folder:
#
#   gate_pass.db        a consistent binary snapshot (VACUUM INTO, safe on a
#                       live database) — the fastest way back if you are
#                       restoring onto SQLite again
#   gate_pass.sql       a text dump, for reading, diffing, or loading into
#                       another SQLite
#   csv/*.csv           one file per table, for loading into PostgreSQL, MySQL,
#                       a spreadsheet, or an auditor's tool
#   MANIFEST.txt        row counts and a checksum of every file
#
# The .sql dump is SQLite-flavoured and will NOT load into PostgreSQL unedited;
# the CSVs are what you migrate with. See deploy/MIGRATING.md.
#
# Driven through the venv's Python rather than the sqlite3 command line, which
# is not installed on this machine.

set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEST="${1:-$APP_DIR/storage/exports}"
PYTHON="$APP_DIR/.venv/bin/python3"
[ -x "$PYTHON" ] || PYTHON=python3

APP_DIR="$APP_DIR" DEST="$DEST" "$PYTHON" - <<'PY'
import csv, gzip, hashlib, os, shutil, sqlite3, sys
from datetime import datetime
from pathlib import Path

app_dir = Path(os.environ["APP_DIR"])
source = app_dir / "storage" / "gate_pass.db"
if not source.exists():
    print(f"ERROR: no database at {source}", file=sys.stderr); sys.exit(1)

stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
out = Path(os.environ["DEST"]) / f"gate_pass-{stamp}"
(out / "csv").mkdir(parents=True, exist_ok=True)

# A consistent snapshot of a LIVE database. `cp` can catch a half-written page
# or miss the write-ahead log entirely, and the result looks like a valid file.
snapshot = out / "gate_pass.db"
src = sqlite3.connect(source, timeout=30)
try:
    src.execute("VACUUM INTO ?", (str(snapshot),))
finally:
    src.close()

# Everything below reads the SNAPSHOT, so the export is internally consistent
# even if somebody issues a pass while it runs.
db = sqlite3.connect(snapshot)
db.row_factory = sqlite3.Row

if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
    print("ERROR: the snapshot failed integrity_check", file=sys.stderr); sys.exit(1)

tables = [r[0] for r in db.execute(
    "SELECT name FROM sqlite_master WHERE type='table' "
    "AND name NOT LIKE 'sqlite_%' ORDER BY name")]

# Text dump, for reading and for restoring onto SQLite.
with open(out / "gate_pass.sql", "w", encoding="utf-8") as fh:
    fh.write(f"-- Fanzart Gate Pass, exported {datetime.now():%Y-%m-%d %H:%M:%S}\n")
    fh.write("-- SQLite dialect. For PostgreSQL or MySQL use the CSVs.\n")
    for line in db.iterdump():
        fh.write(line + "\n")

# One CSV per table. This is what another database actually ingests.
counts = {}
for table in tables:
    rows = db.execute(f"SELECT * FROM {table}").fetchall()
    counts[table] = len(rows)
    columns = [c[1] for c in db.execute(f"PRAGMA table_info({table})")]
    with open(out / "csv" / f"{table}.csv", "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(columns)
        for row in rows:
            writer.writerow([row[c] for c in columns])

# The checks an auditor would ask for, answered in the export itself.
passes = counts.get("gate_passes", 0)
items = counts.get("gate_pass_items", 0)
orphans = db.execute(
    "SELECT COUNT(*) FROM gate_pass_items i "
    "LEFT JOIN gate_passes p ON p.id = i.gate_pass_id WHERE p.id IS NULL").fetchone()[0]
gaps = []
if passes:
    for scope, in db.execute("SELECT DISTINCT serial_scope FROM gate_passes"):
        seqs = [r[0] for r in db.execute(
            "SELECT serial_seq FROM gate_passes WHERE serial_scope=? ORDER BY serial_seq",
            (scope,))]
        expected = list(range(min(seqs), max(seqs) + 1))
        if seqs != expected:
            gaps.append(f"{scope}: {sorted(set(expected) - set(seqs))[:10]}")
db.close()

lines = [
    f"Fanzart Gate Pass export",
    f"taken          {datetime.now():%Y-%m-%d %H:%M:%S}",
    f"source         {source}",
    "",
    "ROWS",
]
lines += [f"  {t:<20} {counts[t]:>8}" for t in tables]
lines += [
    "",
    "INTEGRITY",
    f"  integrity_check      ok",
    f"  items with no pass   {orphans}" + ("  <-- PROBLEM" if orphans else ""),
    f"  gaps in the run      {'; '.join(gaps) if gaps else 'none'}"
    + ("  <-- check these were cancelled, not lost" if gaps else ""),
    "",
    "FILES (sha256)",
]
for path in sorted(out.rglob("*")):
    if path.is_file() and path.name != "MANIFEST.txt":
        digest = hashlib.sha256(path.read_bytes()).hexdigest()[:16]
        size = path.stat().st_size
        lines.append(f"  {digest}  {size:>10}  {path.relative_to(out)}")
(out / "MANIFEST.txt").write_text("\n".join(lines) + "\n")

print("\n".join(lines))
print(f"\nWritten to {out}")
PY
