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


def build_stock_transfer(path, to_no="FR 262700512", date="01-08-2026",
                          to_address=("B1 - Unit A,4Th Floor, Plot No 183 -187,",
                                      "Indospace Logistics Complex Bommasandra,",
                                      "BENGALURU 560105"),
                          from_address=("15 Krishnanagar Ind Area,",
                                        "Off Hosur Main Road",
                                        "Bangalore 560029"),
                          items=()):
    """A STOCK TRANSFER MEMO, laid out like the real WEBPOS ones.

    Traced from DOC63/DOC66/DOC69_WEBPOS_TO.pdf, so the tests exercise the
    layout that actually exists rather than an assumed one. What matters:

      * NO drawn rules anywhere. The separators are runs of hyphen characters,
        so page.edges is empty and columns are held by alignment alone.
      * Columns are Sl.No | HSN | Model Name | Description | Quantity | Value,
        with HSN BEFORE the name.
      * The two addresses are printed side by side, so extract_text()
        interleaves them into single lines.
      * The heading "Model Name" is narrow over names up to three times its
        width, which is what makes column boundaries delicate.

    items: sequence of (model_name, description, qty).
    """
    # x positions measured off the real documents.
    X_SL, X_HSN, X_NAME, X_DESC, X_QTY, X_VALUE = 21.3, 58.5, 113.8, 271.6, 440.9, 519.5
    X_FROM, X_TO = 19.0, 296.8
    # Long enough to span the page, as the real separators do. A short rule is
    # one very wide word whose midpoint lands inside a data column.
    RULE = "-" * 280

    c = ""
    c += _text(X_FROM, 30.0, "STOCK TRANSFER MEMO", size=9)
    c += _text(X_FROM, 45.0, "TRANSFER OUT", size=9)
    c += _text(31.0, 74.8, f"TO NO: {to_no}")
    c += _text(447.8, 74.8, f"Date: {date}")

    c += _text(X_FROM, 95.9, "FROM ADDRESS :")
    c += _text(X_TO, 95.9, "TO ADDRESS :")
    c += _text(X_FROM, 108.9, "Golden Touch Exports")
    c += _text(300.0, 108.9, "Golden Touch Exports")
    for i, line in enumerate(from_address):
        c += _text(X_FROM, 121.9 + i * 12.5, line)
    for i, line in enumerate(to_address):
        c += _text(300.0, 121.9 + i * 12.5, line)
    # A dummy GSTIN in the documented format, not the supplier's real one.
    c += _text(X_FROM, 200.0, "GST No: 29AAAAA0000A1Z5")
    c += _text(300.0, 200.0, "GST No: 29AAAAA0000A1Z5")

    c += _text(X_SL, 212.0, RULE, size=6)
    for x, label in ((X_SL, "Sl.No"), (62.3, "HSN"), (110.6, "Model Name"),
                     (276.2, "Description"), (415.0, "Quantity"),
                     (479.5, "Value (In Rupees)")):
        c += _text(x, 223.3, label)
    c += _text(X_SL, 236.0, RULE, size=6)

    top = 252.9
    total_qty = 0
    for i, (name, description, qty) in enumerate(items, start=1):
        c += _text(X_SL, top, str(i))
        c += _text(X_HSN, top, "84145120")
        # size 7: this generator's Helvetica runs wider than the real
        # document's font, and at size 8 "GRANDMASTER MATTE BLACK 70 INCHES"
        # spills past the Description boundary. 7 reproduces the real width
        # (the name ends around x=252, clear of the 266.2 column edge).
        c += _text(X_NAME, top, name, size=7)
        c += _text(X_DESC, top, description)
        c += _text(X_QTY, top, qty)
        c += _text(X_VALUE, top, "130449.13")
        total_qty += int(qty)
        top += 12.0

    c += _text(X_SL, top + 6.0, RULE, size=6)
    c += _text(215.5, top + 20.0, "TOTAL")
    c += _text(X_QTY, top + 20.0, str(total_qty))
    c += _text(X_VALUE, top + 20.0, "186355.90")
    c += _text(X_SL, top + 34.0, "Amount in words: Rupees One Lakh Only")
    c += _text(X_SL, top + 48.0, "THIS DOCUMENT IS FOR STOCK TRANSFER FROM ONE PREMISES TO ANOTHER")
    c += _text(X_SL, top + 62.0, "Authorised Signatory:")

    _write_pdf(path, c)


# A long name that overruns its narrow heading, which is what broke the first
# attempt at column boundaries.
TRANSFER_ITEMS = [
    ("GRANDMASTER MATTE BLACK 70 INCHES", "Industrial Fan", "1"),
    ("HUGGER 52 - MUD BROWN", "Ceiling Fan", "2"),
    ("DRIFT DARK COFFEE", "Ceiling Fan", "5"),
]
