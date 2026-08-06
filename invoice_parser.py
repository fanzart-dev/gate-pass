"""Pulls Invoice No / From / Customer / Date / Items out of a Fanzart supplier
tax invoice PDF.

Tuned against the "Golden Touch Exports" format:

    Golden Touch Exports              <- supplier name is the first line of text
    15 Krishnanagar Ind Area,
    ...
    Tax Invoice cum Delivery Note              GSTIN : ...
                                                Invoice No :  FR 262702169
                                                Invoice Date :  03-08-2026
    Bill To :                         Ship To :
    Name :  M/S MANGALDEEP            Name :  M/S MANGALDEEP
    ...
    S.no  Model                          HSN  Description  Qty  Unit Price  Total
    1     VIENNA (52) (DC) (ABS) MATTE   ...  Ceiling Fan   2      9147.46  18294.92
          WHITE
    2     VIENNA (52) (DC) (ABS) BRUSH   ...  Ceiling Fan   1      8470.34   8470.34
          NICKEL

Items are read from word coordinates, not from extract_tables(). The item area
is one open box with column dividers but no rules between rows, so pdfplumber
collapses the whole box into a single table row and the mapping from item name
to quantity is lost. Instead we take the column x-boundaries from the vertical
rules, group words into visual lines by y, and bucket each word into a column.
A line that carries a quantity starts a new item; a line with text only in the
Model column is a wrapped continuation of the name above it.

Any field that can't be found is left blank with a note in parse_notes, so the
review screen degrades to manual entry instead of guessing.
"""

import re

import pdfplumber

INVOICE_NO_RE = re.compile(r"Invoice\s*No\s*:?\s*([^\n]+)", re.IGNORECASE)
INVOICE_DATE_RE = re.compile(r"Invoice\s*Date\s*:?\s*([0-9]{1,2}-[0-9]{1,2}-[0-9]{2,4})", re.IGNORECASE)
BILL_TO_NAME_RE = re.compile(r"Name\s*:?\s*(?:M/S\s*)?(.+?)(?=\s+Name\s*:|\n|$)", re.IGNORECASE)

# Header labels that mark the start of the item table, in column order.
NAME_HEADERS = ("model", "item")
QTY_HEADERS = ("qty", "quantity")
SL_HEADERS = ("s.no", "sl", "sno")
# "Model No" is a code column, never the name -- see _name_column_index.
NUMBER_HEADING_RE = re.compile(r"\bno\.?\b|\bcode\b|\bnumber\b")

# Text that marks the end of the item area, whichever appears first.
ITEM_AREA_END_MARKERS = ("total amount in words", "taxable amount", "bank details")

LINE_TOLERANCE = 3.0  # points; words within this vertical distance are one line
EDGE_TOLERANCE = 2.0  # points; vertical rules closer than this are the same divider


def parse_invoice(pdf_path):
    """Returns a dict: supplier_name, customer_name, invoice_no, invoice_date,
    items (list of {sl_no, item_name, quantity}), notes (list of str)."""
    result = {
        "supplier_name": "",
        "customer_name": "",
        "invoice_no": "",
        "invoice_date": "",
        "items": [],
        "notes": [],
    }

    try:
        with pdfplumber.open(pdf_path) as pdf:
            pages = [
                {"text": p.extract_text() or "", "words": p.extract_words(), "edges": list(p.edges)}
                for p in pdf.pages
            ]
    except Exception as exc:
        result["notes"].append(f"could not read PDF: {exc}")
        return result

    if not pages or not any(p["text"].strip() for p in pages):
        result["notes"].append(
            "this PDF has no text in it — it is a scan or photo, so nothing can be "
            "read automatically. Type the details in below."
        )
        return result

    text = pages[0]["text"]
    _parse_supplier(text, result)
    _parse_field(INVOICE_NO_RE, text, result, "invoice_no", "invoice number")
    _parse_field(INVOICE_DATE_RE, text, result, "invoice_date", "invoice date")
    _parse_customer(text, result)

    # A long invoice continues its item table onto later pages, under a repeated
    # letterhead and a repeated column header. Every page that carries an item
    # header contributes items, in page order.
    items = []
    for page in pages:
        try:
            items.extend(extract_items(page["words"], page["edges"]))
        except Exception as exc:
            result["notes"].append(f"could not read the item table: {exc}")
            break
    for i, item in enumerate(items, start=1):
        item["sl_no"] = i
    result["items"] = items

    if not result["items"]:
        result["notes"].append("could not find an item table, add items manually")

    return result


def _parse_supplier(text, result):
    first_line = next((line.strip() for line in text.splitlines() if line.strip()), "")
    if first_line:
        result["supplier_name"] = first_line
    else:
        result["notes"].append("could not find supplier name")


def _parse_field(pattern, text, result, key, label):
    m = pattern.search(text)
    if m:
        result[key] = m.group(1).strip()
    else:
        result["notes"].append(f"could not find {label}")


def _parse_customer(text, result):
    idx = text.lower().find("bill to")
    search_text = text[idx:] if idx != -1 else text
    m = BILL_TO_NAME_RE.search(search_text)
    if m:
        result["customer_name"] = m.group(1).strip()
    else:
        result["notes"].append("could not find customer name")


# ---------------------------------------------------------------------------
# Item table, read from word coordinates.
# ---------------------------------------------------------------------------

def extract_items(words, edges):
    """words: pdfplumber extract_words() output. edges: page.edges.
    Returns [{sl_no, item_name, quantity}]."""
    header = _find_header(words)
    if header is None:
        return []

    name_col, qty_col, sl_col, header_bottom = header
    body = _item_area_words(words, header_bottom, edges)
    if not body:
        return []

    bounds = _column_bounds(edges, header_bottom, fallback=(name_col, qty_col, sl_col))
    header_words = [w for w in words
                    if abs(w["top"] - name_col["top"]) <= LINE_TOLERANCE]
    name_idx = _name_column_index(header_words, bounds, name_col)
    return _assemble_items(body, bounds, name_idx)


def _name_column_index(header_words, bounds, name_word):
    """Which column holds the item name, read from the column headings.

    Suppliers lay the table out differently. One invoice is headed

        S.no | Model | HSN | Description | Qty | Unit Price | Total

    and another, from the same supplier under a different series, is

        S.no | Model No | Model Name | HSN | Description | Batch_no | Qty | ...

    so the name is not reliably "the column after S.no". Assuming it was is
    what made a BI invoice come through with items called `0003 B` and `0006`
    -- the model numbers -- instead of `HAWK BLACK` and `BUDDY`.

    Reading it from the heading text means a new column appearing to the left
    of the name shifts nothing, and a column headed with a number is never
    mistaken for the name.
    """
    headings = {}
    for w in header_words:
        headings.setdefault(_column_index(w, bounds), []).append(w)
    texts = {
        idx: " ".join(w["text"] for w in sorted(ws, key=lambda w: w["x0"])).strip().lower()
        for idx, ws in headings.items()
    }

    # "Model Name" wins outright where both it and "Model No" are present.
    for idx, text in sorted(texts.items()):
        if "name" in text and any(h in text for h in NAME_HEADERS):
            return idx
    # Otherwise a column headed exactly "Model" or "Item".
    for idx, text in sorted(texts.items()):
        if text.rstrip(".") in NAME_HEADERS:
            return idx
    # Last resort: something name-ish that is explicitly not a number column.
    for idx, text in sorted(texts.items()):
        if any(h in text for h in NAME_HEADERS) and not NUMBER_HEADING_RE.search(text):
            return idx
    return _column_index(name_word, bounds)


def _find_header(words):
    """Locate the item-table header row. Returns (name_word, qty_word, sl_word, bottom)."""
    name_word = qty_word = None
    for w in words:
        low = w["text"].strip().lower().rstrip(".")
        if name_word is None and low in NAME_HEADERS:
            name_word = w
        elif qty_word is None and low in QTY_HEADERS:
            qty_word = w
    if name_word is None or qty_word is None:
        return None
    # The two labels must sit on the same visual line to be one header row.
    if abs(name_word["top"] - qty_word["top"]) > LINE_TOLERANCE:
        return None

    sl_word = None
    for w in words:
        if abs(w["top"] - name_word["top"]) > LINE_TOLERANCE:
            continue
        if w["text"].strip().lower().rstrip(".") in SL_HEADERS and w["x1"] <= name_word["x0"]:
            sl_word = w
    bottom = max(name_word["bottom"], qty_word["bottom"])
    return name_word, qty_word, sl_word, bottom


def _item_area_words(words, header_bottom, edges):
    """Words below the header and above the end of the item box.

    Two independent signals, because neither is reliable alone: the rule drawn
    across the bottom of the box, and the text that follows it. Some invoices
    print no "Total Amount in words" line at all, and a supplier could draw
    rules between rows. Taking whichever ends the box higher keeps the terms
    and conditions from being read as another item.
    """
    below = [w for w in words if w["top"] > header_bottom + 0.5]

    candidates = []
    box_bottom = _item_box_bottom(edges, header_bottom)
    if box_bottom is not None:
        candidates.append(box_bottom)
    for line_top, line_words in _group_lines(below):
        joined = " ".join(w["text"] for w in line_words).strip().lower()
        if any(marker in joined for marker in ITEM_AREA_END_MARKERS):
            candidates.append(line_top)
            break

    if candidates:
        end_top = min(candidates)
        below = [w for w in below if w["top"] < end_top - 0.5]
    return below


def _item_box_bottom(edges, header_bottom):
    """Where the item box closes, from the full-width rules below the header.

    The first such rule is the one drawn under the column headings; the item
    box closes at the next one. With only one rule present, that rule is the
    close.
    """
    horizontals = [e for e in edges
                   if e.get("orientation") == "h" and e["top"] > header_bottom + 2]
    if not horizontals:
        return None
    widest = max(e["x1"] - e["x0"] for e in horizontals)
    tops = sorted(e["top"] for e in horizontals if (e["x1"] - e["x0"]) >= widest * 0.8)

    merged = []
    for top in tops:
        if not merged or top - merged[-1] > LINE_TOLERANCE:
            merged.append(top)
    if not merged:
        return None
    return merged[1] if len(merged) > 1 else merged[0]


def _column_bounds(edges, header_bottom, fallback):
    """x boundaries of the item table's columns, from the vertical rules that
    run below the header. Falls back to splitting midway between header labels."""
    xs = sorted(
        {round(e["x0"], 1) for e in edges
         if e.get("orientation") == "v" and e.get("bottom", 0) > header_bottom + 2}
    )
    merged = []
    for x in xs:
        if not merged or x - merged[-1] > EDGE_TOLERANCE:
            merged.append(x)
    if len(merged) >= 3:
        return merged

    name_word, qty_word, sl_word = fallback
    left = sl_word["x0"] - 2 if sl_word else name_word["x0"] - 2
    guessed = [left]
    if sl_word:
        guessed.append((sl_word["x1"] + name_word["x0"]) / 2)
    guessed.extend([
        (name_word["x1"] + qty_word["x0"]) / 2,
        qty_word["x0"] - 2,
        qty_word["x1"] + 2,
    ])
    return sorted(set(round(x, 1) for x in guessed))


def _group_lines(words):
    """Group words into visual lines by their top coordinate. Yields (top, words)."""
    lines = []
    for w in sorted(words, key=lambda w: (w["top"], w["x0"])):
        for line in lines:
            if abs(line["top"] - w["top"]) <= LINE_TOLERANCE:
                line["words"].append(w)
                break
        else:
            lines.append({"top": w["top"], "words": [w]})
    for line in lines:
        line["words"].sort(key=lambda w: w["x0"])
        yield line["top"], line["words"]


def _column_index(word, bounds):
    """Which column a word falls in, by its horizontal midpoint."""
    mid = (word["x0"] + word["x1"]) / 2
    for i in range(len(bounds) - 1):
        if bounds[i] <= mid < bounds[i + 1]:
            return i
    if mid < bounds[0]:
        return 0
    return len(bounds) - 2


def _assemble_items(body_words, bounds, name_idx=None):
    """Turn positioned words into items, joining wrapped name lines.

    Neither column position is hard-coded: `name_idx` comes from the heading
    text (see _name_column_index) and the quantity column is found by looking
    at which column the bare numbers actually land in. Both used to be fixed
    positions, which broke the moment a supplier added a column.
    """
    rows = []
    for _top, line_words in _group_lines(body_words):
        cells = {}
        for w in line_words:
            idx = _column_index(w, bounds)
            cells.setdefault(idx, []).append(w["text"])
        rows.append({i: " ".join(parts).strip() for i, parts in cells.items()})

    if not rows:
        return []

    if name_idx is None:
        name_idx = 1 if len(bounds) > 2 else 0
    qty_idx = _detect_qty_column(rows, name_idx)

    items = []
    for cells in rows:
        name = cells.get(name_idx, "").strip()
        qty = cells.get(qty_idx, "").strip() if qty_idx is not None else ""
        if not name and not qty:
            continue
        if qty and _is_quantity(qty):
            items.append({"sl_no": len(items) + 1, "item_name": name, "quantity": qty})
        elif items and name:
            # No quantity on this line: it is the rest of the name above.
            items[-1]["item_name"] = f"{items[-1]['item_name']} {name}".strip()
        elif name:
            items.append({"sl_no": len(items) + 1, "item_name": name, "quantity": ""})
    return items


def _detect_qty_column(rows, name_idx):
    """The quantity column is the one right of the name that most often holds a
    small bare number. Picking it from the data avoids hard-coding the position,
    which shifts between suppliers."""
    counts = {}
    for cells in rows:
        for idx, value in cells.items():
            if idx <= name_idx:
                continue
            if _is_quantity(value):
                counts[idx] = counts.get(idx, 0) + 1
    if not counts:
        return None
    best = max(counts.values())
    return min(idx for idx, n in counts.items() if n == best)


QUANTITY_RE = re.compile(r"\d{1,4}")


def _is_quantity(value):
    """A bare count like 1 or 12.

    Deliberately integers only, at most four digits. That rejects everything
    else that shares the row: prices carry decimals (9147.46), and the HSN code
    is eight digits (84145120). A supplier writing a fractional quantity falls
    through to manual entry, which is the right way to fail here.
    """
    text = value.strip()
    return bool(QUANTITY_RE.fullmatch(text)) and int(text) > 0
