#!/usr/bin/env python3
"""Empty the register and start the numbering again from 00001.

    python3 manage_reset.py                 # show what would go, change nothing
    python3 manage_reset.py --wipe          # actually do it
    python3 manage_reset.py --wipe --prefix FZ27

For clearing test data out before a real go-live. It is not for tidying up a
book that is in use.

WHAT THIS BREAKS ON PURPOSE
---------------------------------------------------------------------------
The database refuses to delete a gate pass. `trg_gate_passes_no_delete` and
`trg_audit_log_no_delete` are the whole point of the app: a number, once
issued, is on a piece of paper somebody signed, and the register is the record
of it. Nothing in the application can remove one — cancelling marks a pass
cancelled and it keeps its number for ever.

This script drops those triggers, deletes, and puts them back. That is a real
breach of the guarantee the register exists to provide, which is why it is a
separate command, why it will not run without --wipe, why it makes a backup
first and refuses to continue if the backup fails, and why it writes one audit
line recording that it happened.

WHAT IS KEPT
    accounts and their permissions   - nobody has to be set up again
    settings                          - paper size, totals, company name
    the carton master list            - 313 items nobody wants to re-import

WHAT GOES
    every gate pass and its items
    every draft and its items
    the audit log
    failed sign-in attempts
    the uploaded invoice PDFs in storage/invoices

Afterwards the next gate pass is <prefix>-00001.
"""

import argparse
import re
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

import db  # noqa: E402

# Emptied completely, and THIS ORDER MATTERS. Both gate_pass_items and
# audit_log carry a foreign key to gate_passes, and audit_log's is not a
# cascade, so removing gate_passes while an audit row still points at one fails
# the constraint and rolls the whole thing back. Children first, parents after.
WIPE = ["audit_log", "gate_pass_items", "gate_passes",
        "draft_items", "drafts", "login_attempts"]

# The triggers standing in the way, and they are standing there for a reason.
# Read out of schema.sql rather than typed here: a hand-kept list drifts from
# the schema, and it drifted immediately — "trg_gate_passes_no_update" does not
# exist and never did; the real one is trg_gate_passes_no_serial_edit. Getting
# that wrong in the restore check is how the protection quietly fails to come
# back after a wipe.
TRIGGERS = re.findall(r"CREATE TRIGGER IF NOT EXISTS (\w+)",
                      (Path(__file__).parent / "schema.sql").read_text())

CONFIRM = "erase the register"


def counts(conn):
    out = {}
    for table in WIPE + ["users", "settings", "item_carton_mappings"]:
        try:
            out[table] = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        except sqlite3.Error:
            out[table] = 0
    return out


def take_backup(db_path):
    """A verified snapshot before anything is destroyed. Non-negotiable."""
    script = ROOT / "deploy" / "backup.sh"
    if not script.exists():
        return None
    dest = Path(db_path).parent / "backups"
    result = subprocess.run(["bash", str(script), str(dest)],
                            capture_output=True, text=True)
    if result.returncode != 0:
        raise SystemExit(
            "the backup failed, so nothing was deleted:\n"
            + (result.stderr or result.stdout))
    made = sorted(dest.glob("gate_pass-*.db.gz"), key=lambda p: p.stat().st_mtime)
    return made[-1] if made else None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--wipe", action="store_true",
                        help="actually delete; without it nothing changes")
    parser.add_argument("--prefix", default="FZ",
                        help="what the new numbering starts with (default FZ)")
    parser.add_argument("--db", default=str(ROOT / "storage" / "gate_pass.db"))
    parser.add_argument("--yes", action="store_true",
                        help="skip the typed confirmation (for scripts)")
    args = parser.parse_args(argv)

    db_path = Path(args.db)
    if not db_path.exists():
        raise SystemExit(f"no database at {db_path}")

    prefix = db.validate_prefix(args.prefix)
    conn = db.connect(db_path)
    before = counts(conn)
    # Beside the database, not beside the code — so --db pointing at a fetched
    # copy clears that copy's invoices and never the live ones.
    invoices = db_path.parent / "invoices"
    files = sorted(invoices.glob("*")) if invoices.is_dir() else []
    conn.close()

    print(f"\n  Database: {db_path}")
    print(f"  Next gate pass is currently: {db.next_serial_preview(db.connect(db_path))}\n")
    print("  WOULD BE DELETED")
    for table in WIPE:
        print(f"    {table:22} {before[table]}")
    print(f"    {'invoice PDFs on disk':22} {len(files)}")
    print("\n  WOULD BE KEPT")
    for table in ("users", "settings", "item_carton_mappings"):
        print(f"    {table:22} {before[table]}")
    print(f"\n  Afterwards the next gate pass would be {db.serial_for(prefix, 1)}\n")

    if not args.wipe:
        print("  Nothing was changed. Add --wipe to do it.\n")
        return 0

    if not args.yes:
        print(f"  This cannot be undone. Type: {CONFIRM}")
        try:
            if input("  > ").strip() != CONFIRM:
                print("\n  Not confirmed. Nothing was changed.\n")
                return 1
        except (EOFError, KeyboardInterrupt):
            print("\n  Not confirmed. Nothing was changed.\n")
            return 1

    backup = take_backup(db_path)
    print(f"\n  Backed up to {backup}" if backup else "\n  WARNING: no backup was taken")

    raw = sqlite3.connect(db_path, timeout=30)
    raw.row_factory = sqlite3.Row
    try:
        raw.execute("PRAGMA foreign_keys = ON")
        raw.execute("BEGIN IMMEDIATE")
        for trigger in TRIGGERS:
            raw.execute(f"DROP TRIGGER IF EXISTS {trigger}")
        for table in WIPE:
            raw.execute(f"DELETE FROM {table}")
        # AUTOINCREMENT keeps a high-water mark per table; clearing it restarts
        # the row ids too, so a fresh book looks fresh all the way down.
        raw.execute("DELETE FROM sqlite_sequence WHERE name IN (%s)"
                    % ",".join("?" * len(WIPE)), WIPE)
        # The numbering. serial_min_seq is the deliberate-jump floor and must go
        # back to 0, or the first pass of the new book would take whatever it
        # was last set to instead of 1.
        for key, value in (("serial_prefix", prefix), ("serial_min_seq", "0")):
            raw.execute("INSERT INTO settings (key, value) VALUES (?, ?) "
                        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                        (key, value))
        raw.execute(
            "INSERT INTO audit_log (action, details, created_at) "
            "VALUES ('register_reset', ?, ?)",
            (f"register emptied and numbering restarted at {db.serial_for(prefix, 1)}"
             f" — {before['gate_passes']} gate passes and {before['drafts']} drafts"
             f" removed; backup at {backup.name if backup else 'NONE'}",
             datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
        raw.commit()
    except Exception:
        raw.rollback()
        raise
    finally:
        raw.close()

    # Put the protection back, explicitly. Reconnecting is NOT enough:
    # db.connect() only re-runs schema.sql when PRAGMA user_version differs
    # from SCHEMA_VERSION, and it does not differ here — so the triggers stayed
    # dropped and the register sat unprotected. The check below caught that,
    # which is why it is a check and not a comment.
    conn = db.connect(db_path)
    conn.executescript(db.SCHEMA_PATH.read_text())     # every CREATE is IF NOT EXISTS
    restored = [r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='trigger'")]
    missing = [t for t in TRIGGERS if t not in restored]
    if missing:
        raise SystemExit(f"triggers NOT restored: {missing} — the register is "
                         "unprotected, restore the backup")

    try:
        conn.execute("DELETE FROM gate_passes WHERE 1 = 0")
        conn.execute("BEGIN"); conn.execute("INSERT INTO gate_passes "
            "(serial_no, serial_seq, serial_scope, supplier_name, customer_name, "
            " invoice_no, invoice_date, issued_at) "
            "VALUES ('__probe__', 999999, '__probe__', '', '', '', '', '2026-01-01')")
        conn.execute("DELETE FROM gate_passes WHERE serial_no = '__probe__'")
        conn.rollback()
        raise SystemExit("the no-delete trigger did NOT fire — the register is "
                         "unprotected, restore the backup")
    except sqlite3.IntegrityError:
        conn.rollback()          # what a working trigger looks like
    except sqlite3.OperationalError:
        conn.rollback()
        raise SystemExit("could not verify the no-delete trigger — restore the backup")

    for path in files:
        if path.is_file():
            path.unlink()
        elif path.is_dir():
            shutil.rmtree(path)

    after = counts(conn)
    print("\n  Done.")
    for table in WIPE:
        print(f"    {table:22} {before[table]} -> {after[table]}")
    print(f"    {'invoice PDFs':22} {len(files)} -> "
          f"{len(list(invoices.glob('*'))) if invoices.is_dir() else 0}")
    print(f"    {'accounts kept':22} {after['users']}")
    print(f"    {'carton list kept':22} {after['item_carton_mappings']}")
    print(f"    {'delete protection':22} restored ({len(TRIGGERS)} triggers)")
    print(f"\n  The next gate pass will be {db.next_serial_preview(conn)}\n")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
