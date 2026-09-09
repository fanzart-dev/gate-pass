"""What a gate pass must contain before a number is spent on it.

One place, because there were three and they disagreed. A pass could be issued
from the review screen, from a batch, or typed by hand on /manual, and each
path checked something different:

    review / batch   draft_problem() — required fields and a non-empty quantity
    manual           the browser's `required` attribute, and nothing else
    corrections      only "at least one item with a name"

The browser's checks are a courtesy to the person typing, never a control: a
form can be posted by anything. So a manual request with a blank supplier
issued a pass, and a quantity of "abc" issued a pass, and both printed a gate
pass with a nonsense field on it — a document signed at the gate.

Everything here is a pure function over plain values. It imports nothing from
db or app on purpose: the checks have to be usable from the route (to answer
the operator before anything happens) AND from the write path (to be the thing
that actually decides), and a module that imported either could not be.

REQUIRED vs OPTIONAL is a judgement about the paper, not about the schema:

    supplier, customer, document no, date   a gate pass without these does not
                                            identify the consignment
    vehicle, remarks                        genuinely optional, often unknown
                                            when the pass is written
    cartons                                 optional AND meaningfully blank —
                                            an empty box is filled in by hand
                                            at the gate, and a 0 would be a
                                            claim nobody made
"""

import re
from datetime import datetime

# What the whole book is written in, and what the printed pass shows.
DATE_FORMAT = "%d-%m-%Y"
DATE_HINT = "DD-MM-YYYY"

REQUIRED_FIELDS = (
    ("supplier_name", "From"),
    ("customer_name", "Customer"),
    ("invoice_no", "Document No"),
    ("invoice_date", "Date"),
)

# Above this a quantity is almost certainly a typo — a pasted document number,
# or a price that landed in the wrong column. Deliberately generous: real
# invoices in this office run to a few hundred of one model.
MAX_QUANTITY = 100000
MAX_CARTONS = 100000


class ValidationError(ValueError):
    """One or more problems, kept together.

    A list rather than the first failure: a form with three blanks should say
    so once, not send the operator round three times.
    """

    def __init__(self, problems):
        self.problems = list(problems)
        super().__init__("; ".join(self.problems))


def normalize_date(value):
    """A date in the book's own format, or None if it is not a date at all.

    Accepts what the HTML date picker posts (YYYY-MM-DD) and what the invoices
    and the parser use (DD-MM-YYYY), and returns the latter. Anything else is
    not silently kept: "not-a-date" used to be stored verbatim and printed onto
    the pass exactly like that.

    Two-digit years are refused rather than guessed. 03-04-26 could be 1926 or
    2026 and the difference matters on a document that is filed.
    """
    text = (value or "").strip()
    if not text:
        return None
    for fmt in ("%d-%m-%Y", "%Y-%m-%d", "%d/%m/%Y", "%d.%m.%Y"):
        try:
            return datetime.strptime(text, fmt).strftime(DATE_FORMAT)
        except ValueError:
            continue
    return None


def clean_quantity(value, field="quantity", maximum=MAX_QUANTITY):
    """A positive whole number, or a reason it is not one.

    Returns (cleaned_string, problem). Quantities are stored as text because
    that is what the parser reads off a PDF and what the printed pass shows,
    but "abc" is not a quantity in any format — it was accepted and printed.
    """
    text = str(value or "").strip()
    if not text:
        return "", f"{field} is required"
    # Tolerate what a person types: "12 nos", "12.0", a stray comma.
    compact = text.replace(",", "").strip()
    compact = re.sub(r"\s*(nos?|pcs?|units?)\.?$", "", compact, flags=re.I).strip()
    if re.fullmatch(r"\d+\.0*", compact):
        compact = compact.split(".")[0]
    if not compact.isdigit():
        return text, f"{field} must be a whole number, not {text!r}"
    number = int(compact)
    if number <= 0:
        return text, f"{field} must be more than zero"
    if number > maximum:
        return text, f"{field} of {number} looks like a mistake"
    return str(number), None


def clean_cartons(value):
    """Cartons: optional, and blank is a real answer.

    Blank means "to be written in by hand at the gate", which is the commonest
    case for anything not on the master list. Only a value that is present has
    to make sense.
    """
    text = str(value or "").strip()
    if not text:
        return "", None
    cleaned, problem = clean_quantity(text, field="cartons", maximum=MAX_CARTONS)
    return cleaned, problem


def clean_items(items):
    """The item lines, checked and tidied. Raises ValidationError.

    A row that is entirely empty is dropped rather than complained about — the
    editor always shows a spare row, and an untouched one is not a mistake. A
    row with SOMETHING in it must be complete, because a half-filled line is
    somebody who was interrupted.
    """
    problems = []
    cleaned = []
    for index, raw in enumerate(items or [], start=1):
        name = str(raw.get("item_name", "") or "").strip()
        quantity = str(raw.get("quantity", "") or "").strip()
        cartons = str(raw.get("cartons", "") or "").strip()

        if not name and not quantity and not cartons:
            continue
        if not name:
            problems.append(f"line {index} has a quantity but no item name")
            continue

        quantity, problem = clean_quantity(quantity, field=f"line {index} quantity")
        if problem:
            problems.append(problem)
        cartons, problem = clean_cartons(cartons)
        if problem:
            problems.append(f"line {index}: {problem}")

        cleaned.append({"item_name": name, "quantity": quantity, "cartons": cartons})

    if not cleaned and not problems:
        problems.append("at least one item is required")
    if problems:
        raise ValidationError(problems)
    return cleaned


def clean_gate_pass(fields, items=None, require_items=True):
    """Everything a pass needs, checked together. Raises ValidationError.

    Returns (fields, items) with values tidied — the date in the book's format,
    quantities as plain numbers, whitespace gone. Call it once and store what
    it hands back; validating and then storing the raw input is how a checked
    value and a stored value drift apart.
    """
    problems = []
    out = {}

    for name, label in REQUIRED_FIELDS:
        value = str(fields.get(name, "") or "").strip()
        if not value:
            problems.append(f"{label} is required")
        out[name] = value

    if out.get("invoice_date"):
        stored = normalize_date(out["invoice_date"])
        if stored is None:
            problems.append(
                f"Date {out['invoice_date']!r} is not a date — use {DATE_HINT}")
        else:
            out["invoice_date"] = stored

    # Never required, always carried through.
    for name in ("vehicle_no", "remarks"):
        out[name] = str(fields.get(name, "") or "").strip()

    cleaned_items = None
    if items is not None or require_items:
        try:
            cleaned_items = clean_items(items or [])
        except ValidationError as exc:
            problems.extend(exc.problems)

    if problems:
        raise ValidationError(problems)
    return out, cleaned_items
