"""Builds a Golden-Touch-shaped tax invoice PDF from scratch, for tests.

Hand-rolled so the tests stay dependency-free (no reportlab). It only needs to
reproduce the parts of the real invoice the parser looks at: the header fields,
the item table's column rules, and item rows whose names wrap onto a second
line. Geometry matches the real Golden Touch invoice closely enough that
pdfplumber sees the same structure.
"""

# Column x-boundaries, taken from the real invoice's vertical rules.
BOUNDS = [25.9, 59.6, 205.5, 262.1, 408.2, 450.7, 508.8, 572.9]
COL_X = {"sl": 28.0, "name": 62.0, "hsn": 208.0, "desc": 265.0,
         "qty": 412.0, "price": 454.0, "total": 512.0}

PAGE_W, PAGE_H = 595.276, 842.0
HEADER_TOP, HEADER_BOTTOM = 264.0, 286.0   # in pdfplumber "top" coordinates
ITEM_BOTTOM = 492.0
LINE_HEIGHT = 12.0


def _esc(text):
    return text.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")


def _text(x, top, value, size=8):
    """Place text at a pdfplumber-style 'top' coordinate."""
    y = PAGE_H - top - size
    return f"BT /F1 {size} Tf 1 0 0 1 {x:.2f} {y:.2f} Tm ({_esc(value)}) Tj ET\n"


def _line(x0, top0, x1, top1):
    return f"{x0:.2f} {PAGE_H - top0:.2f} m {x1:.2f} {PAGE_H - top1:.2f} l S\n"


def build_invoice(path, supplier="Golden Touch Exports", customer="MANGALDEEP",
                  invoice_no="FR 262702169", invoice_date="03-08-2026", items=()):
    """items: sequence of (name_lines, hsn, description, qty, price, total)."""
    c = "0.5 w\n"

    # Letterhead and header fields, laid out like the real invoice.
    c += _text(60.0, 40.0, supplier, size=10)
    c += _text(60.0, 55.0, "15 Krishnanagar Ind Area,")
    c += _text(200.0, 150.0, "Tax Invoice cum Delivery Note", size=9)
    c += _text(430.0, 175.0, f"Invoice No : {invoice_no}")
    c += _text(430.0, 190.0, f"Invoice Date : {invoice_date}")
    c += _text(60.0, 215.0, "Bill To :")
    c += _text(380.0, 215.0, "Ship To :")
    c += _text(60.0, 230.0, f"Name : M/S {customer}")
    c += _text(380.0, 230.0, f"Name : M/S {customer}")

    # Item table: column rules top to bottom, plus the header's own rules.
    for x in BOUNDS:
        c += _line(x, HEADER_TOP, x, ITEM_BOTTOM)
    c += _line(BOUNDS[0], HEADER_TOP, BOUNDS[-1], HEADER_TOP)
    c += _line(BOUNDS[0], HEADER_BOTTOM, BOUNDS[-1], HEADER_BOTTOM)
    c += _line(BOUNDS[0], ITEM_BOTTOM, BOUNDS[-1], ITEM_BOTTOM)

    header_text_top = HEADER_TOP + 7.0
    for key, label in (("sl", "S.no"), ("name", "Model"), ("hsn", "HSN"),
                       ("desc", "Description"), ("qty", "Qty"),
                       ("price", "Unit Price"), ("total", "Total")):
        c += _text(COL_X[key], header_text_top, label)

    top = HEADER_BOTTOM + 6.0
    for i, (name_lines, hsn, description, qty, price, total) in enumerate(items, start=1):
        c += _text(COL_X["sl"], top, str(i))
        c += _text(COL_X["name"], top, name_lines[0])
        c += _text(COL_X["hsn"], top, hsn)
        c += _text(COL_X["desc"], top, description)
        c += _text(COL_X["qty"], top, qty)
        c += _text(COL_X["price"], top, price)
        c += _text(COL_X["total"], top, total)
        # Continuation lines carry only the rest of the name.
        for extra in name_lines[1:]:
            top += LINE_HEIGHT
            c += _text(COL_X["name"], top, extra)
        top += LINE_HEIGHT

    c += _text(COL_X["sl"], ITEM_BOTTOM + 10.0, "Total Amount in words: Forty One Thousand Only")
    c += _text(420.0, ITEM_BOTTOM + 10.0, "Taxable Amount Rs. 35235.61")

    _write_pdf(path, c)


def _write_pdf(path, content):
    stream = content.encode("latin-1")
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {PAGE_W} {PAGE_H}] "
         f"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>").encode("latin-1"),
        b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, obj in enumerate(objs, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + obj + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    for off in offsets:
        out += b"%010d 00000 n \n" % off
    out += (b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n"
            % (len(objs) + 1, xref))
    path.write_bytes(bytes(out))


MANGALDEEP_ITEMS = [
    (["VIENNA (52) (DC) (ABS) MATTE", "WHITE"], "84145120", "Ceiling Fan", "2", "9147.46", "18294.92"),
    (["VIENNA (52) (DC) (ABS) BRUSH", "NICKEL"], "84145120", "Ceiling Fan", "1", "8470.34", "8470.34"),
    (["VIENNA (52) (DC) (ABS) ANTIQUE", "BRASS"], "84145120", "Ceiling Fan", "1", "8470.34", "8470.34"),
]
