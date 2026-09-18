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

# Varied names, so a register page does not read as one repeated row — column
# widths and text wrapping only misbehave once the content differs.
DEMO_CUSTOMERS = [
    "LA ESPADA", "Material Studio", "AIRAVATA", "HOME SQUARE",
    "SATYAM AUTO COMPONENTS PRIVATE LIMITED", "NEW INDIA ELECTRIC-CITY",
    "MACJ SURAT PVT LTD", "FANZART LLP",
]


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8091
    storage = Path(tempfile.mkdtemp(prefix="gatepass-preview-"))
    app = create_app(db_path=storage / "preview.db", storage_dir=storage)

    conn = db.connect(app.config["DB_PATH"])
    # Totals on, so the preview shows the block that sits between the item
    # table and the signatures. Off by default in a real install.
    db.update_settings(conn, show_total_qty="1", show_total_cartons="1")
    # A few master rows, so the manual screen's carton lookup has something to
    # find. The two-carton entries are the ones that matter: they are what
    # proves the number comes from the master list times the quantity, rather
    # than from the quantity alone.
    db.upsert_carton_mappings(conn, [
        ("VENETIAN BLACK - FANDELIER", 2),
        ("CRYSTAL - FANDELIER", 2),
        ("AEROSLIM 1200MM WHITE", 1),
        ("MICRON MODREN OAK", 1),
    ])
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

    # Enough passes to fill several register pages. Four would fit on one, and
    # the pager would never appear — the thing most likely to be wrong is how
    # it behaves on the FIRST page, the LAST page and the short page at the
    # end, and none of those exist until the book is bigger than a page.
    conn = db.connect(app.config["DB_PATH"])
    for i in range(db.REGISTER_PAGE_SIZE * 2 + 13):
        db.create_gate_pass(
            conn, None, "Golden Touch Exports", DEMO_CUSTOMERS[i % len(DEMO_CUSTOMERS)],
            f"FR 2627{i:05d}", "31-07-2026", "",
            [{"item_name": DEMO_ITEMS[i % len(DEMO_ITEMS)][0],
              "quantity": str(i % 7 + 1), "cartons": ""}],
            prepared_by="Vinay")
    db.close(conn)

    # Passes marked printed, so the register shows BOTH print states and the
    # header checkbox's "select what still needs printing" stage has something
    # to distinguish. With every pass in one state that stage is dropped, which
    # is correct behaviour but leaves it untested.
    #
    # Spread through the book rather than one pass at a known position. The
    # register now shows a page at a time, and the browser tests issue passes
    # of their own as they run — which pushes the top of the book along and
    # slid the single printed pass off page 1. The checkbox tests then skipped
    # themselves, quietly, and the suite still said everything passed. Every
    # seventh pass means page 1 has a mix however far the window has moved.
    conn = db.connect(app.config["DB_PATH"])
    for pass_ in sorted(db.list_gate_passes(conn, limit=None),
                        key=lambda p: p["serial_seq"])[::7]:
        db.mark_printed(conn, pass_["id"])
    db.close(conn)

    # Two drafts sharing a document number, and one matching an issued pass, so
    # the duplicate warnings can be looked at. The prefixes differ deliberately:
    # "TO NO: FR ..." and a bare "FR ..." are the same number, and catching that
    # is the point.
    conn = db.connect(app.config["DB_PATH"])
    for number, source in (("TO NO: FR 262702176", "TO 1.pdf"),
                            ("FR-262702176", "TO 2.pdf")):
        db.create_draft(conn, supplier_name="Golden Touch Exports",
                        customer_name="Golden Touch Exports, Indiranagar",
                        invoice_no=number,
                        invoice_date="03-09-2026",
                        invoice_pdf_path=f"invoices/20260903120000_{source}",
                        items=[{"sl_no": 1, "item_name": name, "quantity": qty,
                                "cartons": qty} for name, qty in DEMO_ITEMS[:2]])
    db.close(conn)

    # A draft WITH its PDF still on disk, so the review screen's document panel
    # has something to show. That panel is the point of the screen now: an
    # extraction error is obvious against the original and invisible without
    # it, so a preview lacking one hides the feature being previewed.
    import shutil as _shutil
    sample = Path(__file__).parent / "sample_invoices" / "golden_touch_sample.pdf"
    if sample.exists():
        (storage / "invoices").mkdir(parents=True, exist_ok=True)
        _shutil.copy(sample, storage / "invoices" / "20260903120000_sample.pdf")
        conn = db.connect(app.config["DB_PATH"])
        db.create_draft(
            conn, supplier_name="Golden Touch Exports",
            customer_name="NIKSHAN ELECTRONICS",
            invoice_no="FR 262702181", invoice_date="03-09-2026",
            invoice_pdf_path="invoices/20260903120000_sample.pdf",
            items=[{"sl_no": i, "item_name": name, "quantity": qty, "cartons": ""}
                   for i, (name, qty) in enumerate(DEMO_ITEMS[:3], 1)])
        db.close(conn)

    # A draft, so /review can be looked at too. The print pages are not the
    # only screen with a layout worth eyeballing.
    conn = db.connect(app.config["DB_PATH"])
    db.create_draft(
        conn, supplier_name="Golden Touch Exports", customer_name="FANZART LLP",
        invoice_no="FR 262702188", invoice_date="03-09-2026",
        remarks="Delivered to site gate by\nFANZART LLP",
        items=[{"sl_no": i, "item_name": name, "quantity": qty, "cartons": qty}
               for i, (name, qty) in enumerate(DEMO_ITEMS, 1)])
    db.close(conn)

    # A cancelled pass, so the watermark can be looked at. Cancelling keeps the
    # number and the row — nothing is deleted — which is exactly why the paper
    # has to say so.
    conn = db.connect(app.config["DB_PATH"])
    db.cancel_gate_pass(conn, 3, "printed in error", cancelled_by="Dinesh D")
    db.close(conn)

    print(f"preview at http://127.0.0.1:{port}/print/1  (demo / demo-preview-only)")
    print(f"  /print/1  a normal pass (6 items), with a two-line remark")
    print(f"  /print/2  a full page ({db.ITEMS_PER_PAGE} items) — the tight case")
    print(f"  /print/3  a 20-item pass — the busy end of a normal day")
    print(f"  /print/4  a 32-item pass — one number, printed over 2 pages")
    print(f"  /drafts    one draft waiting, for the review screen")
    print(f"  sign in as demo (full access), partial (search+cancel) or staff (none)")
    print(f"throwaway data in {storage}")
    app.run(host="127.0.0.1", port=port, debug=False)


if __name__ == "__main__":
    main()
