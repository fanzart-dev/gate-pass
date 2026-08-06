#!/usr/bin/env python3
"""Measures the database at size, and proves concurrent writers never collide.

    python3 tests/benchmark.py            # 100,000 gate passes
    python3 tests/benchmark.py 250000     # or however many

Builds a throwaway database in a temp directory — it never touches storage/.
Reports the timings that matter to someone using the app: opening the register,
searching it, and printing a pass.
"""

import shutil
import sqlite3
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import db  # noqa: E402

CUSTOMERS = ["FANZART LLP", "MR Sample Buyer", "MANGALDEEP", "Golden Interiors",
              "Lakeview Homes", "Prestige Estates"]
ITEMS = ["VIENNA (52) (DC) (ABS) ANTIQUE BRASS", "MICRON MODREN OAK",
          "PHOENIX 52 WALNUT", "REMOTE LCD 2+2.5UF", "RACE (AC) MATTE WHITE"]


def build(conn, count):
    """Insert directly, in batches — create_gate_pass() one row at a time would
    measure Python overhead rather than the database."""
    print(f"Building {count:,} gate passes ...", flush=True)
    started = time.perf_counter()
    batch = 5000
    made = 0
    while made < count:
        n = min(batch, count - made)
        with db.writing(conn):
            for i in range(made + 1, made + n + 1):
                conn.execute(
                    """INSERT INTO gate_passes
                       (serial_no, serial_scope, serial_seq, status, supplier_name,
                        customer_name, invoice_no, invoice_date, prepared_by, issued_at)
                       VALUES (?, 'FZ', ?, ?, 'Golden Touch Exports', ?, ?, ?, ?, ?)""",
                    (db.serial_for("FZ", i), i,
                     "cancelled" if i % 50 == 0 else "issued",
                     CUSTOMERS[i % len(CUSTOMERS)],
                     f"FR 2627{i:06d}",
                     f"{(i % 28) + 1:02d}-08-2026",
                     "Dinesh D",
                     f"2026-08-04 {(i % 24):02d}:{(i % 60):02d}:00"),
                )
                pass_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
                for sl in range(1, (i % 5) + 2):
                    conn.execute(
                        """INSERT INTO gate_pass_items
                           (gate_pass_id, sl_no, item_name, quantity, cartons)
                           VALUES (?, ?, ?, ?, '')""",
                        (pass_id, sl, ITEMS[(i + sl) % len(ITEMS)], str(sl)),
                    )
        made += n
        print(f"  {made:,} ...", end="\r", flush=True)
    # No counter to seed: the next number is derived from MAX(serial_seq), so
    # the concurrent phase carries on from the rows we just wrote.
    conn.execute("ANALYZE")
    print(f"  built in {time.perf_counter() - started:.1f}s" + " " * 20)


def time_it(label, fn, rounds=5, budget_ms=None):
    fn()  # warm the cache so we measure the query, not the first page fault
    best = min(_ms(fn) for _ in range(rounds))
    verdict = ""
    if budget_ms is not None:
        verdict = "  OK" if best <= budget_ms else f"  SLOW (budget {budget_ms}ms)"
    print(f"  {label:<46} {best:8.2f} ms{verdict}")
    return best


def _ms(fn):
    start = time.perf_counter()
    fn()
    return (time.perf_counter() - start) * 1000


def uses_index(conn, sql, params=()):
    plan = " ".join(str(r[3]) for r in conn.execute("EXPLAIN QUERY PLAN " + sql, params))
    return "USING INDEX" in plan or "USING COVERING INDEX" in plan, plan


def read_benchmarks(conn, count):
    print("\nReads (what someone waits for):")
    time_it("Register, first page", lambda: db.list_gate_passes(conn), budget_ms=50)
    time_it("Register, filtered to issued", lambda: db.list_gate_passes(conn, status="issued"),
            budget_ms=50)
    time_it("Look up one pass by its number",
            lambda: db.get_gate_pass_by_serial(conn, db.serial_for("FZ", count // 2)),
            budget_ms=10)
    time_it("Print a pass (row + its items)",
            lambda: db.get_gate_pass(conn, count // 2), budget_ms=10)
    time_it("Search by invoice number",
            lambda: db.list_gate_passes(conn, search=f"FR 2627{count // 2:06d}"), budget_ms=400)
    time_it("Search by customer name",
            lambda: db.list_gate_passes(conn, search="MANGALDEEP"), budget_ms=400)
    time_it("Count the whole book", lambda: db.count_gate_passes(conn), budget_ms=200)
    time_it("Next serial number", lambda: db.next_serial_preview(conn), budget_ms=10)

    print("\nIndex use (EXPLAIN QUERY PLAN):")
    checks = [
        ("pass by serial_no", "SELECT * FROM gate_passes WHERE serial_no = ?", ("FZ-00001",)),
        ("items of one pass",
         "SELECT * FROM gate_pass_items WHERE gate_pass_id = ? ORDER BY sl_no", (1,)),
        ("register filtered by status",
         "SELECT * FROM gate_passes WHERE status = ? ORDER BY id DESC LIMIT 200", ("issued",)),
        ("passes for an invoice", "SELECT * FROM gate_passes WHERE invoice_no = ?", ("FR 2627000001",)),
        ("passes for a customer", "SELECT * FROM gate_passes WHERE customer_name = ?", ("MANGALDEEP",)),
        ("passes on a date", "SELECT * FROM gate_passes WHERE invoice_date = ?", ("04-08-2026",)),
    ]
    for label, sql, params in checks:
        ok, plan = uses_index(conn, sql, params)
        print(f"  {label:<30} {'index' if ok else 'SCAN':>6}   {plan[:70]}")


def concurrency_benchmark(db_path, writers=4, each=25):
    """Four staff issuing at once, the way the office actually uses it."""
    print(f"\nConcurrent writes ({writers} staff x {each} passes, all at once):")
    results, errors = [], []
    barrier = threading.Barrier(writers)

    def worker(n):
        conn = db.connect(db_path)
        try:
            barrier.wait(timeout=30)
            for i in range(each):
                gp = db.create_gate_pass(
                    conn, None, "Golden Touch Exports", CUSTOMERS[i % len(CUSTOMERS)],
                    f"CONC-{n}-{i}", "04-08-2026", "",
                    [{"item_name": "MICRON MODREN OAK", "quantity": "1", "cartons": ""}],
                    prepared_by=f"Staff {n}")
                results.append(gp["serial_seq"])
        except Exception as exc:  # noqa: BLE001 - reported below
            errors.append(f"{type(exc).__name__}: {exc}")
        finally:
            db.close(conn)

    started = time.perf_counter()
    threads = [threading.Thread(target=worker, args=(n,)) for n in range(writers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=120)
    elapsed = time.perf_counter() - started

    expected = writers * each
    locked = [e for e in errors if "locked" in e.lower() or "busy" in e.lower()]
    print(f"  passes issued            {len(results)} of {expected}")
    print(f"  'database is locked'     {len(locked)}")
    print(f"  other errors             {len(errors) - len(locked)}")
    print(f"  duplicate numbers        {len(results) - len(set(results))}")
    contiguous = (bool(results)
                  and sorted(results) == list(range(min(results), max(results) + 1)))
    print(f"  gaps in the run          {'no' if contiguous else 'YES'}")
    print(f"  total time               {elapsed * 1000:.0f} ms "
          f"({elapsed / max(len(results), 1) * 1000:.1f} ms per pass)")
    for e in errors[:5]:
        print(f"    ! {e}")
    return not errors and len(results) == expected and len(set(results)) == len(results)


def main():
    count = int(sys.argv[1]) if len(sys.argv) > 1 else 100_000
    tmp = Path(tempfile.mkdtemp(prefix="gatepass-bench-"))
    db_path = tmp / "bench.db"
    try:
        conn = db.connect(db_path)
        print(f"SQLite {sqlite3.sqlite_version}")
        print("PRAGMAs:", ", ".join(
            f"{p}={conn.execute(f'PRAGMA {p}').fetchone()[0]}"
            for p in ("journal_mode", "synchronous", "busy_timeout", "cache_size", "temp_store")))

        build(conn, count)
        size_mb = db_path.stat().st_size / 1024 / 1024
        items = conn.execute("SELECT COUNT(*) AS n FROM gate_pass_items").fetchone()["n"]
        print(f"  database {size_mb:.1f} MB, {count:,} passes, {items:,} item rows")

        read_benchmarks(conn, count)
        db.close(conn)

        ok = concurrency_benchmark(db_path)
        print("\n" + ("PASS — no locking errors, no duplicate numbers, no gaps."
                      if ok else "FAIL — see errors above."))
        return 0 if ok else 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
