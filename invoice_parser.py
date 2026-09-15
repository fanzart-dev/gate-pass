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

# Imported at the bottom of the module's own imports on purpose: the transfer
# parser imports this one back, for the shared column logic and letterhead
# reading. Python resolves the cycle because neither uses the other at import
# time, only inside functions.
import stock_transfer_parser

INVOICE_NO_RE = re.compile(r"Invoice\s*No\s*:?\s*([^\n]+)", re.IGNORECASE)
INVOICE_DATE_RE = re.compile(r"Invoice\s*Date\s*:?\s*([0-9]{1,2}-[0-9]{1,2}-[0-9]{2,4})", re.IGNORECASE)
BILL_TO_NAME_RE = re.compile(r"Name\s*:?\s*(?:M/S\s*)?(.+?)(?=\s+Name\s*:|\n|$)", re.IGNORECASE)

# Header labels that mark the start of the item table, in column order.
NAME_HEADERS = ("model", "item")
QTY_HEADERS = ("qty", "quantity")
SL_HEADERS = ("s.no", "sl", "sno")
# "Model No" is a code column, never the name — see _name_column_index.
NUMBER_HEADING_RE = re.compile(r"\bno\.?\b|\bcode\b|\bnumber\b")

# Set as the only note when one of our own printed passes is uploaded. db.py
# matches on it so the operator is told what is actually wrong, rather than
# being sent to fix a missing customer name that was never going to be there.
GATE_PASS_UPLOADED_NOTE = (
    "this is a printed gate pass, not a supplier invoice — upload the "
    "supplier's tax invoice instead"
)

# Set when the exclusion below empties the item list — every line was a charge.
# Also matched by db.py, and for the same reason as the note above: the default
# "no items" message tells the operator the PDF could not be read and to type
# the items in by hand, which here would mean typing the charge lines back onto
# the gate pass. Nothing was missed; there is simply nothing to hand over.
ONLY_CHARGES_NOTE = (
    "every line on this invoice is a charge or service — there are no goods "
    "to put on a gate pass"
)

# Text that marks the end of the item area, whichever appears first.
ITEM_AREA_END_MARKERS = ("total amount in words", "taxable amount", "bank details")

LINE_TOLERANCE = 3.0  # points; words within this vertical distance are one line
EDGE_TOLERANCE = 2.0  # points; vertical rules closer than this are the same divider


def page_count(path):
    """How many pages the document has, or 0 if it cannot be opened."""
    try:
        import pypdfium2
        with pypdfium2.PdfDocument(str(path)) as doc:
            return len(doc)
    except Exception:
        return 0


def render_page_png(path, number, width=1100):
    """One page as PNG bytes, or None if that page does not exist.

    Used by the review screen, which shows the original beside the extracted
    fields so a misread can be seen against the document it came from.

    pypdfium2 rather than a rendering library in the browser: it is already
    installed (pdfplumber renders through it), it takes about 80ms, and it
    produces roughly 35 KB against the 3.3 MB of shipping PDF.js to the
    browser. The reason for rendering at all is that an embedded PDF is not
    one thing — every browser supplies its own viewer, and they disagree about
    theme, toolbar and sidebar. A PNG cannot disagree with itself.

    `number` is 1-based, because it is a page number as a person would say it.

    The width is fixed rather than following the panel: the result is cached,
    so it has to be one size, and 1100px is enough to read an invoice line at
    the size the panel displays while staying small enough to be cheap.
    """
    try:
        import pypdfium2
    except ImportError:
        return None

    try:
        with pypdfium2.PdfDocument(str(path)) as doc:
            if number < 1 or number > len(doc):
                return None
            page = doc[number - 1]
            # Scale from the page's own width so every document arrives at the
            # same pixel width whatever paper size it was made on.
            scale = width / page.get_width() if page.get_width() else 1.5
            # Clamped: a tiny page would otherwise be blown up into an enormous
            # bitmap, and a huge one rendered uselessly small.
            scale = max(0.5, min(scale, 4.0))
            image = page.render(scale=scale).to_pil()
    except Exception:
        return None

    import io
    buffer = io.BytesIO()
    # PNG, not JPEG: an invoice is text and thin rules on white, which JPEG
    # blurs into grey fringes at exactly the sizes that matter — and it is
    # SMALLER here anyway (35 KB against 149 KB) because the page is flat
    # colour rather than a photograph.
    image.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


# Lines that are not goods. A gate pass is a list of things physically leaving
# the building and being signed for at the gate, so a delivery charge or an
# installation service has no business on it — it is money, not a carton.
#
# This lives HERE, in the parser, rather than in db, for two reasons. It is
# needed at parse time, before a draft exists; and db already imports this
# module, so putting it the other way round would rebuild the import cycle that
# was just taken out.
#
# NOT the same question as db.is_non_stock_item, and the difference is the
# whole point of this block. That one asks "does this line have a carton of its
# own?", and answers no for a spare — a spare fan rod ships inside somebody
# else's box, so its carton count is left blank rather than written as 0 or 1.
# It is still a rod. It still leaves the building and is still signed for.
#
# This asks the narrower question "is this line a thing at all?", and only
# money and labour answer no. Reusing the carton predicate here would have
# dropped four kinds of goods that real gate passes have already carried:
# FAN ROD FALCON 26MM_IN Spares, FAN ROD19MM_(IN INCHES) Spares, and two
# CANOPY BIG ... Spares — physical parts with "Spares" as a supplier suffix.
#
# So: no SPARE, no HARDWARE, no ACCESSORY. Those are goods.
#
# The regex below is the only list. A parallel tuple of the same words reads
# nicely and then quietly stops matching it.
#
# Whole words, plural or singular: real invoices carry "DELIVERY CHARGE" and
# "DELIVERY CHARGES", "MAINTENANCE OR REPAIR SERVICES", "MAINTENANCE & REPAIR
# SERVICES" and "ERECTION COMMISSIONING AND INSTALLATION SERVICES (CRYSTAL
# FANS)". Whole words matter in both directions: \b stops "DISCHARGE 1200"
# being read as a charge, and a fan genuinely named "SERVICE STATION" would
# still be dropped — none exists in the 313-row master list, which was
# checked, but that is the trade being made against matching exact phrases.
_CHARGE_OR_SERVICE_RE = re.compile(
    r"\b(?:CHARGES?|SERVICES?|FREIGHTS?|INSTALLATION|COMMISSIONING"
    r"|CUSTOMIS?ATION|CUSTOMIZATION)\b")


# Matched in the same shape db.normalize_item_name uses, and defined here
# because db imports this module rather than the other way round. db re-exports
# it, so there is still only one definition of what "the same name" means.
_DASH_CHARACTERS = "\u2010\u2011\u2012\u2013\u2014\u2015\u2212"


# Kept deliberately conservative: case, dashes and spacing only. Stripping
# punctuation more aggressively risks collapsing two genuinely different models
# into one key and silently putting the wrong carton count on a gate pass.
def normalize_item_name(name):
    """The key an item name is matched on. Same item, same key."""
    text = (name or "").upper().replace("\u00a0", " ")
    for dash in _DASH_CHARACTERS:
        text = text.replace(dash, "-")
    text = re.sub(r"\s*-\s*", " - ", text)
    return re.sub(r"\s+", " ", text).strip()


def is_charge_or_service_item(name):
    """True for money and labour — never for goods, however small."""
    return bool(_CHARGE_OR_SERVICE_RE.search(normalize_item_name(name)))


def drop_charge_and_service_items(items):
    """Remove charge and service lines, renumbering what is left.

    Returns (kept, dropped_names).

    Applied AFTER the item list is complete, never while it is being built.
    Both parsers attach a wrapped model name to items[-1] — a name too long
    for its column continues on the line below — so skipping a row mid-loop
    would graft the dropped line's continuation onto the item above it and
    silently corrupt a real one. Building the list first cannot do that.
    """
    kept, dropped = [], []
    for item in items or []:
        if is_charge_or_service_item(item.get("item_name", "")):
            dropped.append(str(item.get("item_name", "")).strip())
        else:
            kept.append(item)
    for position, item in enumerate(kept, start=1):
        item["sl_no"] = position
    return kept, dropped


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

    # A gate pass this app printed earlier, fed back into the uploader. It
    # happens: the printed pass and the supplier invoice sit in the same folder
    # and look alike at a glance.
    #
    # Left to run, the parser produces confident nonsense rather than failing:
    # the two A5 halves of an A4 sheet are read as one line of text, so every
    # field comes out doubled ("FR 262702140 Date : 31-07-2026 Invoice No : FR
    # 262702140 ..."), the supplier reads as "Sl. No. : FZ-00057 Sl. No. :
    # FZ-00057", and the item count doubles. The operator was then told only
    # "no customer — could not be read from the PDF", which points at the wrong
    # problem entirely.
    if _looks_like_a_gate_pass(text):
        result["notes"].append(GATE_PASS_UPLOADED_NOTE)
        return result

    # A stock transfer memo moves goods between branches rather than selling
    # them. It still leaves the gate, so it still needs a pass, but its TO
    # number is not an invoice number and its destination is a branch rather
    # than a customer. Routed to its own module so that tightening one document
    # type cannot quietly shift the other.
    if stock_transfer_parser.looks_like_stock_transfer(text):
        # Filtered here too. This path RETURNS, so it never reaches the
        # exclusion at the end of parse_invoice — a transfer memo would have
        # kept its charge lines while an invoice lost them, which is the kind
        # of difference nobody notices until the two are compared side by side.
        transfer = stock_transfer_parser.parse_stock_transfer(pages)
        _drop_charges(transfer)
        return transfer

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
    _drop_charges(result)

    # Empty because every line was excluded is not the same as empty because
    # the table could not be read, and _drop_charges has already said which.
    if not result["items"] and ONLY_CHARGES_NOTE not in result["notes"]:
        result["notes"].append("could not find an item table, add items manually")

    return result


def _drop_charges(result):
    """Take the charge and service lines out, and SAY so.

    Saying so is the point. Silently shortening the list would leave the
    operator unable to tell an excluded line from one the parser failed to
    read — and those need opposite responses: ignore the first, type the second
    back in by hand. The note names what went.
    """
    kept, dropped = drop_charge_and_service_items(result.get("items"))
    if not dropped:
        return
    result["items"] = kept
    named = ", ".join(dropped[:3]) + ("..." if len(dropped) > 3 else "")
    result["notes"].append(
        f"{len(dropped)} charge/service line(s) left off the gate pass: {named}")
    if not kept:
        result["notes"].append(ONLY_CHARGES_NOTE)


def _looks_like_a_gate_pass(text):
    """Is this one of our own printed passes rather than a supplier invoice?

    Both markers are required, and a supplier invoice carries neither: a tax
    invoice has no signature block and never calls itself a gate pass. Asking
    for both keeps an invoice that merely mentions a gate pass in its terms
    from being turned away.
    """
    low = text.lower()
    return "gate pass" in low and "authorised by" in low


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

    # The code column, used only when a row has no name at all. It is whatever
    # sits between the Sl No. column and the name — on a BI invoice that is
    # "Model No", and on an FR invoice there is nothing there, so no fallback.
    sl_idx = _column_index(sl_col, bounds) if sl_col else 0
    code_idx = name_idx - 1 if name_idx - 1 > sl_idx else None

    return _assemble_items(body, bounds, name_idx, code_idx)


def _name_column_index(header_words, bounds, name_word):
    """Which column holds the item name, read from the column headings.

    Suppliers lay the table out differently. One invoice is headed

        S.no | Model | HSN | Description | Qty | Unit Price | Total

    and another, from the same supplier under a different series, is

        S.no | Model No | Model Name | HSN | Description | Batch_no | Qty | ...

    so the name is not reliably "the column after S.no". Assuming it was is
    what made a BI invoice come through with items called `0003 B` and `0006`
    — the model numbers — instead of `HAWK BLACK` and `BUDDY`.

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


def _assemble_items(body_words, bounds, name_idx=None, code_idx=None):
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
        # A row with a quantity but no name would otherwise reach the operator
        # blank, and a pass cannot be issued without an item name. The code is
        # a poor label but it is one the warehouse can match against the
        # invoice, which beats an empty line.
        if not name and code_idx is not None:
            name = cells.get(code_idx, "").strip()
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


# --- parsing a batch ------------------------------------------------------
#
# Parsing is where essentially all the time goes: about 285 ms for a two-page
# invoice, against 0.7 ms for everything else the upload does. Fifty invoices
# is therefore fifty times 285 ms, and the only way to make that shorter is to
# do several at once.
#
# PROCESSES, not threads. pdfplumber is Python almost all the way down, so the
# GIL serialises it and a thread pool only adds contention — measured on this
# machine with eight copies of a two-page invoice:
#
#     sequential                2.21 s
#     ThreadPoolExecutor(4)     3.16 s     <- slower than doing nothing
#     ThreadPoolExecutor(8)     3.53 s     <- slower still
#     ProcessPoolExecutor(8)    0.64 s     <- 3.4x faster
#
# SPAWN, not fork. gunicorn runs gthread workers, so this process has threads,
# and forking a threaded process can inherit a lock held by a thread that does
# not exist in the child — a deadlock that shows up rarely and under load,
# which is the worst kind. Spawn costs about 100 ms of start-up per worker and
# cannot do that.
#
# The pool is skipped for small batches, where that start-up is most of the
# work, and any failure to build one falls back to parsing in order. A slow
# upload is a nuisance; a failed upload is a gate pass nobody can issue.
PARALLEL_THRESHOLD = 4
MAX_PARSE_WORKERS = 8


def parse_many(paths, workers=None):
    """Parse several invoices, in parallel when it is worth it.

    Returns results in the same order as `paths`. A file that cannot be read
    yields the same error-shaped dict `parse_invoice` failures produce, so one
    unreadable PDF never costs the rest of the batch.
    """
    paths = list(paths)
    if not paths:
        return []

    def failed(exc):
        return {"supplier_name": "", "customer_name": "", "invoice_no": "",
                "invoice_date": "", "items": [], "notes": [f"could not be read: {exc}"]}

    def sequentially():
        out = []
        for path in paths:
            try:
                out.append(parse_invoice(path))
            except Exception as exc:  # noqa: BLE001 - one bad file, not a bad batch
                out.append(failed(exc))
        return out

    if len(paths) < PARALLEL_THRESHOLD:
        return sequentially()

    import concurrent.futures as futures
    import multiprocessing
    import os

    count = min(len(paths), MAX_PARSE_WORKERS, (os.cpu_count() or 2))
    try:
        context = multiprocessing.get_context("spawn")
        with futures.ProcessPoolExecutor(count, mp_context=context) as pool:
            submitted = [pool.submit(parse_invoice, str(p)) for p in paths]
            results, broken = [], 0
            for future in submitted:
                try:
                    results.append(future.result())
                except Exception as exc:  # noqa: BLE001
                    broken += 1
                    results.append(failed(exc))
    except Exception:  # noqa: BLE001 - no pool available at all
        return sequentially()

    # A pool that cannot start its workers fails EVERY file, and the failures
    # come back looking exactly like unreadable PDFs. Left alone, one broken
    # pool turns a batch of fifty good invoices into fifty drafts marked
    # "could not be read" — the parallelism silently destroying the work it was
    # meant to speed up. If nothing at all parsed, distrust the pool rather
    # than fifty documents that were fine yesterday, and redo it in order.
    if broken == len(paths):
        return sequentially()
    return results
