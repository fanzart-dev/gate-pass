"""Reads a STOCK TRANSFER MEMO / TRANSFER OUT, which is not a sales invoice.

A transfer memo moves stock between Golden Touch branches. It still leaves the
gate, so it still needs a gate pass — but three of its fields do not mean what
the same-looking fields mean on a tax invoice:

  * there is no invoice number, there is a **TO number**;
  * there is no customer, there is a **destination branch** — both ends are
    Golden Touch, so recording the company name alone would make every transfer
    look identical in the register;
  * the parties are the same company at both ends.

Kept in its own module rather than as a branch inside `invoice_parser` so that
tightening one document type cannot quietly shift the other. It shares nothing
with the invoice parser's item extraction, because the two documents are not
laid out alike:

    Tax invoice        drawn vertical rules per column, "Bill To" block
    Transfer memo      NO rules at all, columns held by alignment only,
                       two addresses printed side by side

The memo's `-----` separators are runs of hyphen characters, not drawn lines:
`page.edges` is empty. So column boundaries come from the x positions of the
heading words themselves, and the destination address is separated from the
sender's by x position rather than by reading order — `extract_text()`
interleaves the two columns into single lines:

    FROM ADDRESS :               TO ADDRESS :
    Golden Touch Exports         Golden Touch Exports
    15 Krishnanagar Ind Area,    B1 - Unit A,4Th Floor, Plot No 183 -187,
    Off Hosur Main Road
    Indospace Logistics Complex Bommasandra,

Reading "Indospace" out of that flattened text works by luck; reading it out of
the right-hand column works because that is where it is.

The returned dict is the same shape `invoice_parser.parse_invoice` returns, so
everything downstream — drafts, the review screen, the printed pass — is
unchanged.

Verified against DOC63/DOC66/DOC69_WEBPOS_TO.pdf.
"""

import re

import invoice_parser

# Either marker routes a document here; the real memos carry both, on their
# first two lines.
DOCUMENT_MARKERS = ("stock transfer memo", "transfer out")

# The TO number replaces the invoice number. The label is kept in the value
# ("TO NO: FR 262700512") so a register or export read months later says what
# kind of document the pass came from, without a second column to carry it.
TO_NO_RE = re.compile(r"\bTO\s*NO\s*:?\s*([A-Z]{0,3}\s*\d[\w/-]*)", re.IGNORECASE)
# Spelled exactly as the memo spells it: "TO NO: FR 262700512".
TO_NO_PREFIX = "TO NO: "

# `Date: 01-08-2026`, which shares a line with the TO number.
DATE_RE = re.compile(r"\bDate\s*:?\s*([0-9]{1,2}-[0-9]{1,2}-[0-9]{2,4})", re.IGNORECASE)

# Where the goods are going. Matched by keyword because the three branches write
# their addresses quite differently:
#   Indospace       "Indospace Logistics Complex Bommasandra,"
#   Indiranagar     "Indiranagar."
#   Sadashivanagar  "Bellary Road, Sadashivanagar,"
BRANCH_KEYWORDS = ("Indospace", "Indiranagar", "Sadashivanagar")
BRANCH_OWNER = "Golden Touch Exports"

# Column headings, in the order they appear. Only the name and quantity are
# taken; HSN, Description and Value are read past.
NAME_HEADING = "model name"
QTY_HEADING = "quantity"
SL_HEADING = "sl.no"

# Heading words closer together than this belong to one column: "Model"/"Name"
# sit 5.4pt apart and "Value"/"(In"/"Rupees)" the same, while the narrowest gap
# between two different columns is 14pt (Sl.No to HSN).
HEADING_GAP = 10.0

LINE_TOLERANCE = 3.0

# Rows at or below this text end the item table.
ITEM_END_MARKERS = ("total", "amount in words", "this document is for")


def looks_like_stock_transfer(text):
    """Is this a transfer memo rather than a sales invoice?"""
    low = text.lower()
    return any(marker in low for marker in DOCUMENT_MARKERS)


def parse_stock_transfer(pages):
    """pages: [{text, words, edges}] as built by invoice_parser.parse_invoice.

    Returns the same dict shape as parse_invoice, so the caller cannot tell the
    two apart and nothing downstream needs to know which parser ran.
    """
    result = {
        "supplier_name": "",
        "customer_name": "",
        "invoice_no": "",
        "invoice_date": "",
        "items": [],
        "notes": [],
    }

    text = pages[0]["text"]
    words = pages[0]["words"]

    result["supplier_name"] = BRANCH_OWNER
    _parse_to_number(text, result)
    _parse_date(text, result)
    _parse_destination_branch(words, result)

    items = []
    for page in pages:
        try:
            items.extend(extract_transfer_items(page["words"]))
        except Exception as exc:  # noqa: BLE001 - surfaced to the operator
            result["notes"].append(f"could not read the item table: {exc}")
            break
    for i, item in enumerate(items, start=1):
        item["sl_no"] = i
    result["items"] = items

    if not items:
        result["notes"].append("could not find an item table, add items manually")

    return result


def _parse_to_number(text, result):
    match = TO_NO_RE.search(text)
    if not match:
        result["notes"].append("could not find the TO number")
        return
    # Collapse any run of spaces inside the number: "FR  262700512" and
    # "FR 262700512" are the same document.
    number = re.sub(r"\s+", " ", match.group(1)).strip()
    result["invoice_no"] = f"{TO_NO_PREFIX}{number}"


def _parse_date(text, result):
    match = DATE_RE.search(text)
    if match:
        result["invoice_date"] = match.group(1).strip()
    else:
        result["notes"].append("could not find the transfer date")


def _parse_destination_branch(words, result):
    """Customer becomes "Golden Touch Exports, <branch>".

    The destination is taken from the words to the RIGHT of the "TO ADDRESS"
    label, not by searching the page. Both addresses are printed side by side,
    and the sender's is Bommasandra — a keyword search over the flattened text
    would be reading the correct answer out of the wrong column, and would stop
    being correct the moment a branch moved.
    """
    label = _to_address_label(words)
    if label is None:
        haystack = " ".join(w["text"] for w in words)
        result["notes"].append("could not find the TO ADDRESS block")
    else:
        # The right-hand column starts at the label. A small margin allows for
        # lines that begin fractionally left of it.
        split_x = label["x0"] - 10
        haystack = " ".join(w["text"] for w in words
                            if w["x0"] >= split_x and w["top"] >= label["top"])

    for keyword in BRANCH_KEYWORDS:
        if re.search(rf"\b{re.escape(keyword)}\b", haystack, re.IGNORECASE):
            result["customer_name"] = f"{BRANCH_OWNER}, {keyword}"
            return

    # An unknown branch is not a parse failure — a new one may have opened. The
    # company name goes in so the pass can still be issued, and the note tells
    # the operator to add the branch by hand.
    result["customer_name"] = BRANCH_OWNER
    result["notes"].append(
        "could not tell which branch this is going to — add it after the "
        "company name"
    )


def _to_address_label(words):
    """The "TO" of "TO ADDRESS :", which marks the left edge of the right-hand
    column. Matched as a pair so the "TO" of "TO NO:" is not taken instead."""
    for i, w in enumerate(words):
        if w["text"].strip().upper() != "TO":
            continue
        for nxt in words[i + 1:i + 3]:
            if abs(nxt["top"] - w["top"]) > LINE_TOLERANCE:
                continue
            if nxt["text"].strip().upper().startswith("ADDRESS"):
                return w
    return None


def extract_transfer_items(words):
    """Read the item table, whose columns are held by alignment alone.

    There are no drawn rules on this document, so the boundaries come from the
    heading words: the midpoint between the end of one heading and the start of
    the next. That is also why the heading has to be found before anything else
    can be read.
    """
    heading = _find_heading(words)
    if heading is None:
        return []
    columns, heading_bottom = heading
    bounds = _bounds_from_headings(columns)

    labels = [label for label, _x0, _x1 in columns]
    try:
        name_idx = labels.index(NAME_HEADING)
        qty_idx = labels.index(QTY_HEADING)
    except ValueError:
        return []

    items = []
    for _top, line in _lines_below(words, heading_bottom):
        joined = " ".join(w["text"] for w in line).strip().lower()
        if any(joined.startswith(m) for m in ITEM_END_MARKERS):
            break
        # The `-----` separators are text, not drawn rules, so they arrive here
        # as ordinary words. Skipped explicitly rather than trusting them to
        # land in a harmless column: where the midpoint of that one very wide
        # word falls depends on how far the rule happens to run, and a shorter
        # rule drops it straight into the Model Name column, where it is
        # appended to the item above as part of its name.
        if _is_separator(joined):
            continue

        cells = {}
        for w in line:
            cells.setdefault(_column_of(w, bounds), []).append(w["text"])
        cells = {i: " ".join(parts).strip() for i, parts in cells.items()}

        name = cells.get(name_idx, "").strip()
        qty = cells.get(qty_idx, "").strip()
        sl = cells.get(labels.index(SL_HEADING) if SL_HEADING in labels else 0, "").strip()

        if name and qty and sl.isdigit():
            items.append({"sl_no": len(items) + 1, "item_name": name, "quantity": qty})
        elif name and not qty and items:
            # A model name too long for its column wraps onto the next line,
            # carrying nothing else. It belongs to the item above it.
            items[-1]["item_name"] = f"{items[-1]['item_name']} {name}".strip()

    return items


def _is_separator(joined):
    """A ruled line made of hyphens rather than drawn as a line."""
    stripped = joined.replace(" ", "")
    return len(stripped) >= 8 and set(stripped) <= {"-", "_", "="}


def _find_heading(words):
    """Locate the column heading row. Returns ([(label, x0, x1)], bottom)."""
    for top, line in _group_lines(words):
        joined = " ".join(w["text"] for w in line).lower()
        if SL_HEADING in joined and NAME_HEADING in joined:
            return _group_headings(line), max(w["bottom"] for w in line)
    return None


def _group_headings(line):
    """Heading words into columns. "Model"+"Name" is one heading, and so is
    "Value"+"(In"+"Rupees)"; a gap wider than HEADING_GAP starts a new column."""
    columns = []
    for w in sorted(line, key=lambda w: w["x0"]):
        if columns and w["x0"] - columns[-1][2] <= HEADING_GAP:
            label, x0, _x1 = columns[-1]
            columns[-1] = (f"{label} {w['text'].strip().lower()}", x0, w["x1"])
        else:
            columns.append((w["text"].strip().lower(), w["x0"], w["x1"]))
    return columns


# How far left of the next heading a column boundary sits. The values under a
# heading are wider than the heading itself, so the boundary has to hug the
# NEXT column rather than split the gap.
COLUMN_MARGIN = 10.0


def _bounds_from_headings(columns):
    """Column edges, each just left of the next heading.

    Not the midpoint between headings, which is the obvious choice and is
    wrong: `Model Name` is a 54pt heading over names up to 140pt wide.
    `GRANDMASTER MATTE BLACK 70 INCHES` runs to x=252 while the midpoint
    between `Name` (ends 164.6) and `Description` (starts 276.2) is 220.4 — so
    `INCHES` landed in the Description column and the item came through as
    `GRANDMASTER MATTE BLACK 70`, quietly losing a word off the end of a
    product name.

    Anchoring to the next heading instead gives 266.2 here, which clears the
    longest name and still sits left of the Description values (they start at
    271.6). The guard keeps a boundary from ever falling inside the heading to
    its left, however tightly a future document packs its columns.
    """
    bounds = [columns[0][1] - 20]
    for left, right in zip(columns, columns[1:]):
        bounds.append(max(left[2] + 1, right[1] - COLUMN_MARGIN))
    # The last column is right-open: values run past the right edge of their
    # heading, so there is nothing to hug.
    bounds.append(columns[-1][2] + 60)
    return bounds


def _column_of(word, bounds):
    mid = (word["x0"] + word["x1"]) / 2
    for i in range(len(bounds) - 1):
        if bounds[i] <= mid < bounds[i + 1]:
            return i
    return 0 if mid < bounds[0] else len(bounds) - 2


def _group_lines(words):
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


def _lines_below(words, bottom):
    for top, line in _group_lines(words):
        if top > bottom + 0.5:
            yield top, line
