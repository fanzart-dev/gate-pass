#!/usr/bin/env python3
"""End-to-end check on a real supplier invoice: upload -> parse -> issue -> print.

Runs against a throwaway temp database/storage dir, so it never touches
storage/ (the real book). Defaults to the bundled sample invoice; pass a
different PDF path to try a new supplier's format.

Usage:
    python3 tests/verify_real_invoice.py
    python3 tests/verify_real_invoice.py path/to/other_invoice.pdf
"""

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import db  # noqa: E402
from app import create_app  # noqa: E402

DEFAULT_INVOICE = Path(__file__).parent / "sample_invoices" / "golden_touch_sample.pdf"


def fail(msg):
    print(f"FAIL: {msg}")
    sys.exit(1)


def main():
    invoice_path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_INVOICE
    if not invoice_path.exists():
        fail(f"invoice not found: {invoice_path}")

    tmpdir = Path(tempfile.mkdtemp(prefix="gate-pass-verify-"))
    storage = tmpdir / "storage"
    flask_app = create_app(db_path=storage / "gate_pass.db", storage_dir=storage)
    client = flask_app.test_client()

    print(f"Uploading {invoice_path.name} ...")
    with open(invoice_path, "rb") as f:
        resp = client.post(
            "/upload",
            data={"invoice": (f, invoice_path.name)},
            content_type="multipart/form-data",
        )
    location = resp.headers.get("Location", "")
    if resp.status_code != 302 or "/review/" not in location:
        fail("upload did not reach the review screen")
    draft_id = int(location.rstrip("/").rsplit("/", 1)[-1])

    conn = db.connect(flask_app.config["DB_PATH"])
    draft = db.get_draft(conn, draft_id)

    print(f"  supplier : {draft['supplier_name']!r}")
    print(f"  customer : {draft['customer_name']!r}")
    print(f"  invoice  : {draft['invoice_no']!r} dated {draft['invoice_date']!r}")
    print(f"  items    : {len(draft['items'])}")
    for item in draft["items"]:
        print(f"    - {item['item_name']!r} x {item['quantity']!r}")

    # When the PDF has no text at all the note already explains everything, so
    # don't repeat it once per field.
    unreadable = "no text in it" in draft["parse_notes"]
    gaps = []
    if not unreadable:
        if not draft["supplier_name"]:
            gaps.append("no supplier name parsed")
        if not draft["customer_name"]:
            gaps.append("no customer name parsed")
        if not draft["invoice_no"]:
            gaps.append("no invoice number parsed")
        if not draft["invoice_date"]:
            gaps.append("no invoice date parsed")
        if not draft["items"]:
            gaps.append("no items parsed")

    if draft["parse_notes"] or gaps:
        print("\nDidn't read automatically (review screen would ask for manual entry):")
        for note in filter(None, [draft["parse_notes"]] + gaps):
            print(f"  - {note}")

    print("\nStanding in for the operator typing cartons, then issuing ...")
    items = draft["items"] or [{"item_name": "UNKNOWN ITEM", "quantity": "1"}]
    for item in items:
        # A placeholder so the script can reach the print step — NOT a rule.
        # Cartons depend on how the goods are packed and are always typed by
        # hand; nothing in the app may derive them from quantity.
        item["cartons"] = "1"

    gate_pass = db.create_gate_pass(
        conn, draft_id,
        draft["supplier_name"] or "UNKNOWN SUPPLIER",
        draft["customer_name"] or "UNKNOWN CUSTOMER",
        draft["invoice_no"] or "UNKNOWN",
        draft["invoice_date"] or "",
        "KA 01 AB 1234",
        items,
        invoice_pdf_path=draft["invoice_pdf_path"],
    )
    print(f"  issued {gate_pass['serial_no']}")

    print_resp = client.get(f"/print/{gate_pass['id']}")
    if print_resp.status_code != 200:
        fail(f"print page returned {print_resp.status_code}")
    if gate_pass["serial_no"].encode() not in print_resp.data:
        fail("print page does not show the serial number")

    out_html = tmpdir / "print_preview.html"
    out_html.write_text(print_resp.data.decode())
    print(f"\nPrint preview saved to {out_html}")
    print("(open it in a browser, or run the wkhtmltopdf command from CLAUDE.md, to eyeball layout)")
    print("\nPASS: end-to-end flow completed for this invoice")


if __name__ == "__main__":
    main()
