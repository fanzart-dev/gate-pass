#!/usr/bin/env python3
"""A throwaway instance with demo data, for eyeballing the print layout.

    python3 tests/preview_server.py [port]

Builds its own database in a temp directory and serves on port 8091, so it can
run alongside the real app without touching storage/. The demo pass carries the
longest item names we have ever seen on a real invoice, and a remark that
wraps to a second line, because those are what the print geometry has to
survive.

Sign in as demo / demo-preview-only.
"""

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import db  # noqa: E402
from app import create_app  # noqa: E402

DEMO_ITEMS = [
    ("ERECTION COMMISSIONING AND INSTALLATION SERVICES", "3"),
    ("VIENNA (52) (DC) (ABS) ANTIQUE BRASS", "2"),
    ("FAN ROD19MM_(IN INCHES)", "18"),
    ("REMOTE LCD 2+2.5UF", "2"),
    ("WINDFLOWER - FANDELIER", "1"),
    ("PHOENIX 52 WALNUT", "4"),
]


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8091
    storage = Path(tempfile.mkdtemp(prefix="gatepass-preview-"))
    app = create_app(db_path=storage / "preview.db", storage_dir=storage)

    conn = db.connect(app.config["DB_PATH"])
    # Totals on, so the preview shows the block that sits between the item
    # table and the signatures. Off by default in a real install.
    db.update_settings(conn, show_total_qty="1", show_total_cartons="1")
    db.create_user(conn, "demo", "Dinesh D", "demo-preview-only")          # admin
    db.create_user(conn, "staff", "Asha Nair", "demo-preview-only",         # no extras
                    status=db.APPROVED)
    db.create_user(conn, "partial", "Yuga", "demo-preview-only",             # some extras
                    status=db.APPROVED,
                    permissions={"can_search_register": True, "can_cancel_passes": True,
                                  "can_batch_print": True})
    db.create_gate_pass(
        conn, None, "Golden Touch Exports", "MR Sample Buyer", "FR 262702176",
        "05-08-2026", "KA 01 AB 1234",
        # Cartons filled in on this one, so the preview shows both totals.
        # The others leave them blank, which is the commoner case and the one
        # that must NOT print a bold 0.
        [{"item_name": name, "quantity": qty, "cartons": qty}
         for name, qty in DEMO_ITEMS],
        prepared_by="Dinesh D",
        # Two lines, because a remark that wraps is what the hanging indent in
        # the Remarks box exists for — the second line must sit under the start
        # of the first, not back under the word "Remarks". Two is also all the
        # printed box holds: see .remarks-body in print.css.
        remarks="Delivered to site gate by\nFANZART LLP")
    db.close(conn)

    # A second pass filled to the brim, because the tight case is what the
    # print geometry has to survive — /print/2.
    conn = db.connect(app.config["DB_PATH"])
    db.create_gate_pass(
        conn, None, "Golden Touch Exports", "FANZART LLP", "FR 262702140",
        "05-08-2026", "KA 01 AB 1234",
        [{"item_name": f"{DEMO_ITEMS[i % len(DEMO_ITEMS)][0]} {i + 1}",
          "quantity": str(i + 1), "cartons": ""}
         for i in range(db.ITEMS_PER_PAGE)],
        prepared_by="Dinesh D")
    db.close(conn)

    # A 20-item pass: the top of the range the office actually sees.
    conn = db.connect(app.config["DB_PATH"])
    db.create_gate_pass(
        conn, None, "Golden Touch Exports", "MANGALDEEP", "FR 262702169",
        "05-08-2026", "",
        [{"item_name": f"{DEMO_ITEMS[i % len(DEMO_ITEMS)][0]}", "quantity": str(i + 1),
          "cartons": ""} for i in range(20)],
        prepared_by="Dinesh D")
    db.close(conn)

    # A 32-item invoice: one gate pass, one number, two printed pages.
    conn = db.connect(app.config["DB_PATH"])
    db.create_gate_pass(
        conn, None, "Golden Touch Exports", "FANZART LLP", "FR 262702140",
        "31-07-2026", "",
        [{"item_name": f"{DEMO_ITEMS[i % len(DEMO_ITEMS)][0]}", "quantity": str(i + 1),
          "cartons": ""} for i in range(32)],
        prepared_by="Dinesh D")
    db.close(conn)

    print(f"preview at http://127.0.0.1:{port}/print/1  (demo / demo-preview-only)")
    print(f"  /print/1  a normal pass (6 items), with a two-line remark")
    print(f"  /print/2  a full page ({db.ITEMS_PER_PAGE} items) — the tight case")
    print(f"  /print/3  a 20-item pass — the busy end of a normal day")
    print(f"  /print/4  a 32-item pass — one number, printed over 2 pages")
    print(f"  sign in as demo (full access), partial (search+cancel) or staff (none)")
    print(f"throwaway data in {storage}")
    app.run(host="127.0.0.1", port=port, debug=False)


if __name__ == "__main__":
    main()
