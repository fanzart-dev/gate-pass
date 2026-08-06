# Fanzart Gate Pass — project notes for Claude Code

Web app that turns a supplier tax invoice PDF into a printed gate pass with an
unbreakable serial number. Replaces the handwritten gate pass book at Fanzart.

## Run and test

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python3 app.py                      # http://127.0.0.1:8090
                                    # first person to sign up becomes the admin
python3 tests/test_flow.py          # 678 checks, must all pass before shipping
python3 tests/benchmark.py          # timings + concurrency proof at 100k passes
python3 tests/verify_real_invoice.py   # end-to-end on a real supplier invoice
```

`tests/test_flow.py` is a plain script, not pytest — run it directly.
It builds its own temp database, so it never touches `storage/`.

`tests/verify_real_invoice.py` defaults to the bundled sample invoice; pass a
PDF path to try a new supplier's format.

`tests/make_invoice_pdf.py` builds a Golden-Touch-shaped invoice PDF from
scratch (no reportlab) so parser tests can cover layouts we have no real file
for — multi-item invoices, wrapped item names. Prefer adding a case there over
hand-made word dictionaries: it exercises pdfplumber's own extraction.

## Layout

| File | What it holds |
|---|---|
| `app.py` | Flask routes: upload → review → issue → print, register, settings |
| `db.py` | All SQL. Serial allocation lives in `create_gate_pass()` |
| `invoice_parser.py` | Pulls Invoice No / From / Customer / Date / Items out of a PDF |
| `tests/make_invoice_pdf.py` | Generates invoice PDFs for parser tests |
| `tests/benchmark.py` | Builds a 100k-pass database and measures reads + concurrent writes |
| `tests/preview_server.py` | Throwaway instance with demo data on :8091, for eyeballing the print layout |
| `schema.sql` | Tables, indexes, and the immutability triggers |
| `templates/_pass_card.html` | One printed page of a pass — shared by both print pages |
| `templates/print.html` | One gate pass |
| `templates/print_batch.html` | Several passes as one printable document |
| `static/css/print.css` | Print geometry — A4 landscape, two A5 passes side by side |
| `static/img/fanzart-logo.png` | The Fanzart lockup, top-left of every pass and in the app header |
| `exports.py` | CSV and .xlsx writers for the reports page |
| `manage_users.py` | CLI fallback for accounts — the way back in if admins are locked out |
| `templates/login.html` | Sign-in page |
| `templates/people.html` | Admin: create accounts, set roles and passwords |
| `storage/` | Database, session key, and uploads still awaiting review (gitignored — this is the real book) |

## Rules that must not be broken

These are the reason the app exists. Do not "simplify" them away.

1. **The serial number is immutable.** Allocated by the database inside the same
   transaction that inserts the gate pass. Never add a route, form field, or
   admin screen that writes `serial_no`, `serial_seq` or `serial_scope`.
   `schema.sql` has triggers that ABORT such an UPDATE — keep them.
2. **Gate passes are never deleted.** Cancelling sets status and keeps the
   number, so the run stays continuous. The delete trigger enforces this.
3. **The audit log is append-only.** Same reason, same enforcement.
4. **The serial is allocated at issue time, not at upload.** Abandoning a draft
   must not burn a number.
5. **Cartons are never filled in by the app, and never required.** A single fan
   can ship in two cartons and several spares can share one box — the count
   depends on how the goods were actually packed, is known at the gate rather
   than at the keyboard, and appears nowhere on the invoice. So: the parser must
   not return a `cartons` key, no button may derive cartons from quantity (the
   *Cartons = quantity* shortcut was removed for this reason), the box renders
   empty, **and a pass issues fine with cartons blank** — the column prints
   empty to be written on by hand. The field is still accepted if someone does
   type a count.
6. **Who prepared a pass is part of the record.** `prepared_by` is snapshotted
   onto the row at issue time and covered by the immutability trigger alongside
   the serial. Renaming or disabling an account never rewrites an old pass.
7. **One serial run for everyone.** `serial_counters` is global, never per-user.
   Whoever is signed in, the next number is the next number — allocated inside
   the same `BEGIN IMMEDIATE` transaction as the insert, so simultaneous issues
   from different people cannot collide.
8. **A number is never issued twice.** This is what makes restarting the count
   delicate — see below.

## Serial numbers

Serials are `<prefix>-<5-digit seq>`, e.g. **`FZ-00001`**. The prefix is the
`serial_prefix` setting (default `FZ`) and is stored per row in `serial_scope`.

**The next number is derived from the book itself** — `MAX(serial_seq) + 1` for
the current prefix (`db.next_seq`), read inside the same `BEGIN IMMEDIATE`
transaction that inserts the pass. There is no separate counter table any more
(`serial_counters` is dropped on upgrade): a second copy of the sequence can
drift out of step with the passes actually issued, and the last row IS the last
number. The `(serial_scope, serial_seq)` index makes this an index lookup, not a
scan — 0.1 ms per pass against a 100k-row book.

A duplicate serial is impossible by construction rather than by constraint: the
number is always one past the highest that exists, and the write lock is held
across the read and the insert. Cancelled passes keep their number and still
count, so cancelling never causes the next pass to reuse one.

**Numbering does not reset by itself.** There is no fiscal-year rollover — the
count runs on until an admin deliberately restarts it under Settings →
Numbering (`db.start_new_run`).

**Restarting requires a new prefix.** Going back to `00001` under the same
prefix would produce a second gate pass carrying a number already printed on a
first one, breaking rule 8. So `start_new_run()` refuses a prefix that any pass
has ever been issued under, and refuses the one currently in use. `FZ` → `FZ27`
→ `FZ28` keeps every number unique for good. Restarting deletes nothing: passes
from earlier runs keep their numbers and stay in the register, and the restart
itself is written to the audit log.

Older passes issued before this change keep their original
`GP/<fiscal-year>/<seq>` numbers — serials are immutable, so a register may show
both shapes. That is correct, not a bug to "fix" by rewriting old rows.

## Reading invoices

The gate pass takes Invoice No, From (the supplier), Customer (the *Bill To*
party), Date, and the Items with their Quantity. Cartons are never read from
the invoice — see rule 5.

Items are read from **word coordinates, not `extract_tables()`**. The invoice's
item area is one open box with column dividers but no rules between rows, so
pdfplumber collapses every item into a single table row and the mapping from
name to quantity is lost. Instead `extract_items()`:

1. takes column x-boundaries from the vertical rules below the header,
2. groups words into visual lines by their `top` coordinate,
3. buckets each word into a column by its horizontal midpoint,
4. treats a line carrying a quantity as a new item, and a line with text only
   in the Model column as the wrapped rest of the name above it.

That last step is what makes `VIENNA (52) (DC) (ABS) MATTE` + `WHITE` come back
as one item. Do not "simplify" this back to `extract_tables()`.

**The item table can run onto later pages.** A long invoice repeats the
letterhead and the column header on page two and carries on. Every page is
scanned for an item header and its items appended, then the whole list is
renumbered. Header fields (invoice number, parties, date) come from page one
only, since later pages repeat them.

**Where the item box ends is read from the rule drawn across it**, not from the
text below it. Some invoices print no "Total Amount in words" line, and without
a bottom bound the terms and conditions get read as one enormous extra item.
`_item_box_bottom()` takes the *second* full-width horizontal rule below the
header — the first is the one under the column headings. The text markers in
`ITEM_AREA_END_MARKERS` remain as a second opinion; whichever ends the box
higher wins.

**Which column holds the item name is read from the heading text**, never from
its position. The supplier issues at least two layouts:

```
FR series:  S.no | Model            | HSN | Description | Qty | Unit Price | Total
BI series:  S.no | Model No | Model Name | HSN | Description | Batch_no | Qty | ...
```

`_name_column_index()` buckets the heading words into the same columns as the
data, then prefers a heading containing "name", falls back to one that is
exactly `Model`/`Item`, and never accepts one matching `NUMBER_HEADING_RE`
(`No`, `Code`, `Number`). Taking "the column after S.no" is what put model
*numbers* on a BI gate pass — items came through as `0003 B` and `0006` instead
of `HAWK BLACK` and `BUDDY`. The quantity column was already found from the
data this way; the name column now matches.

`_is_quantity()` accepts bare integers of at most four digits only. That is
deliberate: it rejects the 8-digit HSN code and the decimal prices that sit in
the same row. A supplier using fractional quantities falls through to manual
entry, which is the right way to fail.

**Scanned invoices cannot be read at all.** A scan has no text layer, so the
parser reports one plain note saying so and the operator types the pass in.
There is no OCR — invoices normally arrive as the supplier's original PDF.

Sample invoices under `tests/sample_invoices/`, each covering a different trap.
**They are not in git** — they are real supplier invoices carrying a GSTIN, IRN
hashes and named customers, and the repository is public. The 13 tests that read
them skip with a message when they are absent (`@needs_fixtures`), so a fresh
clone reports 537/537 and says what it skipped rather than failing. On a machine
that has the PDFs it is 698/698. See `tests/sample_invoices/README.md`.

| Fixture | What it exercises |
|---|---|
| `golden_touch_sample.pdf` | The plain case: one item, one page |
| `golden_touch_two_page.pdf` | 13 items over two pages, no "Total Amount in words" line |
| `golden_touch_service_line.pdf` | Non-Fanzart customer, a service line, a two-digit quantity |
| `mangaldeep_scan.pdf` | A scan with no text layer at all |

Add the supplier's invoice here whenever the parser changes.

## Print format

Must match the Fanzart gate pass form exactly:

- A4 landscape, two A5 passes side by side, dashed cut line down the middle.
  (A5 stock, one pass per sheet, is a Settings option.)
- **`@page` has no margin, and must not be given one.** The browser draws its own
  header and footer — document title, URL, "1 of 1", timestamp — *into the page
  margin band*, which is where the four stray lines in the corners of a printed
  pass came from. They are not in the template. With `margin: 0` there is nowhere
  for them to go. The 7mm is padding on `.sheet` instead, and `.sheet` is now the
  full sheet: **297×210mm** (A4 landscape) or **148×210mm** (`.single`, A5).
  `box-sizing: border-box` is global, so the content box stays exactly
  **283×196mm** — every other measurement in the print CSS is unchanged.
  Measured after the change: content 283×196 on both, `.pass` 141.37×196 (2-up)
  and 134×196 (A5), rows still 6.27mm at 22 and 5.30mm at 26, zero overflow.
  CSS cannot suppress browser headers in every browser, so the definitive control
  is still the **"Headers and footers" checkbox** in the print dialog (Chrome: More
  settings; Firefox: Margins and Header/Footer). Tell staff to untick it once —
  the browser remembers it.
- The Fanzart logo sits top-left of each pass at 30 mm wide (~10 mm tall),
  transparent PNG so it prints clean on any stock. It is the full lockup
  including "LUXURY DESIGNER FANS" and "SINCE 2012", matching the paper form.
  Sized by width, never by height, so the strapline stays legible.
- Item area is **one open box** — outer border and column dividers only, no
  rules between rows.
- **Only rows carrying an item are drawn.** The box closes straight after the
  last item rather than running blank column dividers to the foot of the page.
  The template iterates `page_items`; it used to iterate `range(1, rows + 1)`
  and emit empty cells, which is what drew the filler rows.

  **`rows` is still the height budget, and that is a different thing from the
  number of rows drawn.** `print_row_count()` (min 22, capped at 26) is what
  divides `ITEM_BODY_MM` into `--row-h`, so a 3-item pass gets the same roomy
  6.27mm rows as a 20-item one, and a full 26-item pass tightens to 5.30mm and
  still fits. Do not collapse the two — using the item count as the budget
  would make a 2-item pass draw two 69mm-tall rows.

  Because the table no longer fills the page, `.pass-foot` carries
  `margin-top: auto`. Without it the signature block rides up under the table
  and signs halfway down an empty pass. Measured after the change: 6-item pass
  draws 6 rows at 6.27mm with 106.75mm of clear space above a footer still
  pinned 6.88mm from the bottom; a 26-item pass draws 26 at 5.30mm with zero
  overflow; a 32-item pass splits 26 + 6 with Sl No. running 1–26 then 27–32.
- The item column is headed **`Item Name`** and is **centred**, heading and
  cells alike (`.col-item { text-align: center }`).
- Columns are **locked at `Sl No.` 8% / `Item Name` 68% / `Quantity` 12% /
  `No.of Cartons` 12%**, and there is a test asserting those exact numbers.
  Item at 68% keeps names like
  `ERECTION COMMISSIONING AND INSTALLATION SERVICES` (48 characters) on one line.

  **What actually constrains the numeric columns is the heading text, not the
  digits.** `No.of Cartons` is the longest heading; at 12% its column is 15.7mm
  and the heading needs 15.4mm at **6.2pt**. It needed 16.1mm at 6.5pt and spilled
  past its border — headings are `white-space: nowrap`, so they *overflow*
  rather than wrap. If you raise the heading size, widen the column or lengthen
  the wording, re-render and check `scrollWidth` against `clientWidth` on each
  `th`; the overflow is easy to miss by eye. Header padding is 0.2mm 0.8mm.
- **The row height is explicit, never elastic.** `db.print_row_height_mm()`
  divides a fixed body height (`ITEM_BODY_MM`, 138mm — 142.8mm left "Prepared by"
  0.15mm inside a pass that is `overflow: hidden`, so Firefox clipped it; the
  lower figure buys ~5mm of slack, measured at 8.97mm on a 22-row pass) by the
  number of rows, and the template writes the result into the page as `--row-h`.
  The table is `flex: none`.

  It used to be `flex: 1`, which let the browser share leftover page height out
  across the rows — so the *same* gate pass printed with different row heights on
  different machines. Measured on identical HTML: **5.60mm rows at a 170mm
  printable page, 8.47mm at 230mm** — a 51% swing, and the rows were not even
  uniform within one page (four different heights on a 22-row pass). That is what
  "warped and distorted on Staff's machine" was; it has nothing to do with roles.
  **Do not put `flex` back on the items table.**

  The trade is deliberate: if the printable area is genuinely shorter than the
  196mm sheet, content now overflows rather than silently compressing. A visible
  overflow is better than geometry that is quietly wrong.
- `max-height` plus `overflow: hidden` on the `td` means one unusually long value
  can never push its row taller than its neighbours.
- **How many rows are drawn is what sets the row height.** The item box is a
  fixed height and the table is `flex: 1` inside it, so the spare space is shared
  across however many rows are drawn. `db.print_row_count()` draws
  `PRINT_MIN_ROWS` (22) rows for a short pass and one row per item beyond that,
  capped at `MAX_ITEMS_PER_PASS`. A typical 6-item pass therefore gets ~6.8mm
  (26px) rows; a full 26-item pass settles to ~5.6mm and still fits one sheet.
  Blank rows are invisible — the box has column dividers but no rules between
  rows — so drawing fewer of them simply spaces the items out.
- **`PRINT_MIN_ROWS` is the dial for row spacing, not the cell padding.** The
  padding and line-height only set a floor (~5mm) and the rows sit well above it,
  so changing the padding alone moves nothing on a normal pass — to tighten or
  loosen the rows, change how many are drawn.
- `td` carries `padding: 1mm 2mm` (~4px 8px), `line-height: 1.3` and
  `vertical-align: middle`. The 2mm side padding keeps text off the column
  borders; it was checked against the longest real item name
  (`ERECTION COMMISSIONING AND INSTALLATION SERVICES`) to be sure it does not
  push it onto a second line. **`height: 5mm` on the `td` is a floor, not the row
  height, and must stay low.** Raising it to the comfortable 7mm made a 26-item
  pass 233mm tall inside a 196mm sheet — a table row cannot shrink below its
  stated height, so the last items fell off the page. Raising the vertical
  padding has the same effect for the same reason: at 1.2mm the full pass still
  overflowed by 7mm.
- The `.col-sl/.col-qty/.col-ctn` rule sets `padding-left`/`padding-right` only.
  Using the `padding` shorthand there silently resets the vertical padding those
  cells inherit, and that selector is more specific — the rows lose their
  breathing room with nothing obviously wrong in the CSS.
- After any change here, render both a short pass and a full one and check
  `pass.scrollHeight` against `pass.clientHeight`. `tests/preview_server.py`
  serves exactly those two at `/print/1` and `/print/2`.
- Below the table: **Authorised by** bottom-right with a blank rule to sign —
  the only signature block on the pass. Optional totals and vehicle number sit
  to its left and are off by default; the Boxes total is dropped when no cartons
  were typed, rather than printing a label with nothing after it.
- **Prepared by** is a record of provenance, not a signature, so it is a quiet
  watermark line in the bottom corner: 5.5pt, `#888888`, uppercase with letter
  spacing, reading `PREPARED BY: <name>`. It deliberately has no rule and no
  box — it must not compete with the authoriser's signature above it.
- Row height in `print.css` is tuned so 26 rows fill the sheet. If you change it,
  re-render and confirm a copy still fits on one page — an overflow splits a
  gate pass across two sheets, which is the worst possible bug here.

## Long invoices print over several pages

`db.ITEMS_PER_PAGE` (26) is the number of item rows on one printed **page**, not
a limit on a gate pass. An invoice with more items is never refused and never
given a second number:

* it stays **one gate pass record with one serial** — `FZ-00039`, never
  `FZ-00039-1`. The same shipment leaving under two serials is exactly what the
  unbroken run exists to prevent;
* `db.paginate_items()` chunks the items into pages of 26, keeping the running
  Sl No. — page 2 of a 32-item pass starts at **27**, not back at 1;
* every page repeats the same header (From, Customer, Invoice No, Sl. No.) and
  carries a **"Page N of M"** indicator, which is hidden entirely on a
  single-page pass;
* on A4 each page still prints as its own sheet with two copies side by side to
  cut apart, so a 2-page pass is 2 sheets. `@media print` sets
  `page-break-after` on `.sheet`; without it the pages run together.

Totals, when switched on, print on the **last page only** — repeated on every
page they would read as if each page were the whole consignment.

The review screen states this rather than blocking: *"This invoice contains 32
items. It will automatically generate as a 2-page Gate Pass under one number."*
There is no longer any item count that stops a pass being issued.

To check print output without a browser:

```bash
wkhtmltopdf --enable-local-file-access --print-media-type \
  -O Landscape -s A4 -T 7mm -B 7mm -L 7mm -R 7mm page.html out.pdf
```

## Conventions

- Standard library plus Flask and pdfplumber (password hashing uses
  `werkzeug.security`, which ships with Flask). Do not add a framework, an ORM, or
  a build step — this runs on one office server and must stay easy to fix.
- SQLite with WAL. Writes go through `db.py`, never inline SQL in routes.
- Parser changes need a new sample invoice added to the tests. When a supplier's
  format cannot be read, the review screen must degrade to manual entry rather
  than guess — including one blank item row to type into.
- In Jinja, gate pass and draft dicts carry an `items` list — write
  `gate_pass['items']`, not `gate_pass.items`, which resolves to `dict.items`.

## Signing in, and who may do what

Several people at the office issue passes, so the app requires a login and
prints the signed-in person's name as *Prepared by*.

**Two roles.** Staff issue gate passes. Admins additionally approve accounts and
set roles, on the *People* page (`/admin/users`).

**Joining.** Someone requests an account at `/signup`; it is created `pending`
and cannot sign in until an admin approves it. The person is told plainly that
they are waiting rather than being shown a wrong-password error — but only once
they have proved they know their own password, so a stranger guessing usernames
still learns nothing.

**The first account is approved and made admin automatically**, because there
would otherwise be nobody able to approve it. This is in `db.create_user()`, not
in the route, so the CLI gets the same behaviour. It only ever applies while the
users table is empty.

**Guardrails against locking the office out** — the app must never reach a state
with no admin who can sign in:

- an admin cannot remove their own admin rights or switch off their own account
  (checked in the route, so the message can name the situation);
- the last approved admin cannot be demoted or disabled by anyone
  (checked in `db.set_user_admin` / `db.set_user_status`, so the CLI is bound by
  it too);
- an account must be approved before it can be made an admin.

`manage_users.py` is the way back in if every admin is somehow locked out, and
the way to create the first account without a browser:
`add | list | approve | passwd | admin | unadmin | disable | enable`. Passwords
are prompted for, never passed as arguments, and stored as a salted hash via
`werkzeug.security`.

Accounts are **disabled, never deleted**, for the same reason passes are never
deleted: old passes keep the name of whoever prepared them. `status` is the one
source of truth (`pending` / `approved` / `rejected` / `disabled`); the old
`is_active` column survives only on upgraded databases and is not read.

Admin rights are re-checked on every request, not cached in the session, so
revoking them takes effect on the person's next click. Same for disabling.

The session key lives at `storage/secret_key` (generated on first run, mode
0600) so sessions survive a restart. `GATE_PASS_SECRET_KEY` overrides it.

Every route except login and signup is behind `@login_required`, and the admin
pages behind `@admin_required`. When adding a route, add the decorator — there
are tests that walk the main paths signed out (expecting a redirect) and as a
non-admin (expecting 403).

The app has no HTTPS of its own. On the office LAN that is the existing
arrangement, but do not expose it to the internet without putting TLS in front —
session cookies and passwords would otherwise cross the network in the clear.

## Uploaded invoices are working material, not records

An uploaded PDF lives in `storage/invoices/` only while it is still a **draft** —
it is what the operator checks the parsed fields against. The moment the draft
becomes a gate pass, everything the invoice had to say is on the pass, and
`_discard_invoice_file()` deletes the upload. Left in place they accumulated at
roughly 300 KB an invoice.

Deleted in three places, all in `app.py` because `db.py` does no filesystem work:

* issuing one draft, **after** the pass is committed;
* issuing a batch — only the uploads whose draft actually became a pass. A
  skipped draft keeps its PDF, because it is still waiting to be fixed;
* discarding a draft.

`gate_passes.invoice_pdf_path` is left **NULL** for new passes: a stored path to
a file that has been deleted is worse than no path. Rows issued before this
change keep their old paths, and those files may or may not still exist.

`_discard_invoice_file()` resolves the path and checks it is inside the invoices
directory before unlinking. The path comes from the database rather than a
request, but a delete driven by a stored string should not be able to wander out
of its folder if that string is ever wrong.

## Batch uploads

Upload accepts many PDFs at once (`request.files.getlist`). Each becomes its own
draft, so **one unreadable PDF never stops the other 49**. Drafts hold no serial
number, which is what keeps the run continuous when some files fail.

The drafts screen marks each one **Ready** or **Needs attention**
(`db.draft_problem` — missing supplier/customer/invoice no/date, no items, an
item with no quantity, or more than 26 items). Only ready ones are tickable.

`db.create_gate_passes_batch()` issues the selected drafts in **one
transaction**:

* numbers come out as an unbroken block, allocated once at the start;
* anything not ready is filtered out *before* the transaction opens, so it is
  skipped rather than failing the batch, and takes no number;
* if any pass inside the transaction fails, **the whole batch rolls back** and no
  numbers are consumed — the run is never left with a hole in the middle;
* another person's batch running at the same moment waits for the lock and then
  continues from the end of ours. Two batches never interleave.

Measured: 50 PDFs uploaded and parsed in 4.0 s (79 ms per file, nearly all of it
pdfplumber), then issued in **5 ms** as `FZ-100001..FZ-100050` against a book
already holding 100,000 passes. Four simultaneous batches of 15 produced 60
contiguous numbers, no duplicates, no gaps.

Note that "no gaps" refers to *allocation*. A cancelled pass keeps its number and
stays in the register (rule 2) — that is a retained number, not a gap.

### The printed pass is role-independent

`print.html` is a standalone document — it does not extend the app chrome and
contains no `current_user`, `is_admin` or session reference, and `print.css` has
none either. Both roles get a **byte-identical** response from `/print/<id>`
(verified by hashing both). The screen toolbar is the only non-pass element and
carries `.no-print`, which `@media print` hides.

If a pass ever prints differently for two people, it is not the template: look at
paper size, margins and print scale in the browser dialog.

### Batch printing

`/print/batch?ids=71,72,73` renders the selected passes as **one continuous
HTML document** and calls `window.print()` on `load`. Nothing is written to
disk — no PDF, no ZIP; the sheets go from the browser to the printer. The
register shows tick boxes and a sticky action bar only for
`can_batch_print`, and the route enforces the same.

* Both print pages render `_pass_card.html`. **Keep it that way** — the single
  and batch pages must not each own a copy of the card, or they will drift.
* A pass that runs to several pages contributes several sheets, so a batch of 3
  where one has 32 items prints 4 sheets. Cancelled passes cannot be ticked.
* Ids are de-duplicated, non-numeric ones are dropped, ids that no longer exist
  are left out with a message rather than failing the whole batch, and more than
  `db.MAX_BATCH_PRINT` (50) is refused — a runaway request would otherwise try to
  render thousands of sheets into one document.
* The batch page shows flash messages in its **`.no-print` toolbar**. Anything
  added to that page must carry `.no-print` or it will appear on the sheets.

### Wide pages

The register and the People page both carry more columns than the reading width
suits, so they opt into `container-wide` (1300px) via the
`{% block container_class %}` in `base.html`. Everything else keeps the narrower
width, which reads better for forms.

The register is deliberately dense: the search box and filter sit directly under
the heading, with no summary cards above them.

Reports is a **left-aligned single panel** reading top to bottom — dates, then
narrowing, then what to export — followed by a preview table. Every block is
flush to the same left edge and inputs are sized to their content rather than
stretched, so nothing is centred.

The workflow is **Apply Filters → view → Export**. One form with two
destinations: `Apply Filters` submits to `/reports` and reloads the preview,
`Export Report` carries the same fields to `/reports/export` via `formaction`.

**Nothing is listed until Apply Filters has been pressed.** The page opens as a
form to fill in, not as a view of the whole book. `applied` is simply
`bool(request.args)`, and when it is false the route skips both queries rather
than running them and throwing the answer away.

The preview is capped at `db.REPORT_PREVIEW_LIMIT` (50) and says so when it is
truncated — **the export itself is never capped**.

`table.list` is `table-layout: fixed` with explicit percentages on `.reg-*`
classes (4 / 10 / 14 / 11 / 14 / 18 / 11 / 7 / 11). With `auto` layout, one long
customer name takes the space the actions column needs and "Cancel" gets pushed
off the edge. Text cells wrap (`overflow-wrap: anywhere`) rather than being
truncated — a register is for reading, so a long name should take a second line.

Two things that look like details and are not:

* **The flex row for Print/Cancel is on an inner `div`, never on the `<td>`.**
  A `td` with `display: flex` stops being a table-cell, and `table-layout: fixed`
  can then no longer size that column — measured, it collapsed from 11% to 1.9%
  and clipped exactly as before.
* **`.table-wrap` scrolls, it does not hide.** On a narrow screen the last
  column stays reachable instead of being cut off.

The People accounts table works the same way: `table-layout: fixed` with
`.ppl-*` percentages (22 / 16 / 26 / 12 / 24) and 10px 12px cell padding.

Its three row actions are `.btn-action-sm` buttons in a single right-aligned
flex row. **The labels are short on purpose** — "Edit permissions / New password
/ Switch off" measures 312px against the 300px the column gives, so it wrapped
onto a second line and looked cluttered. "Permissions / Password / Disable"
measures 276px and fits with room to spare, which was cheaper than widening the
column or hiding the actions behind a menu. Lengthen a label and it wraps
again.

## Register vs Reports

The two screens are deliberately separate: **/register is for reading the book,
/reports is for getting data out of it.** Do not put export controls back on the
register — that is what made it unusable.

The register has one search box and one **Filter** button that reveals a panel
with From / To / Status. The panel opens by itself when a filter is already
applied, and the button carries a badge with how many are on, so a filtered
register never looks like the whole book. `?range=today` still works there for
old bookmarks even though the pills have moved.

The reports page carries the quick presets (Today / This week / This month /
This year / Custom), custom dates, status, supplier and customer, the choice of
summary vs detailed and CSV vs Excel, and a live count of what matches before
anything is generated. Choosing a preset clears the custom dates and typing a
custom date reverts to Custom, so the two cannot silently disagree.

## Register filters and the audit export

`db._register_filters()` builds the WHERE clause shared by the register list,
its count and both exports, so the three can never disagree about what a filter
means. Add a filter there, not in three places.

Dates filter on **`issued_at`** — when the pass was generated, which is what an
audit asks about — not `invoice_date`, which is supplier-supplied free text in
DD-MM-YYYY and does not sort. `issued_at` is `YYYY-MM-DD HH:MM:SS`, so a plain
string comparison is both correct and able to use `idx_gate_passes_issued_at`.
`date_to` covers the whole of its end day.

Quick ranges (Today / This week / This month / This year) are resolved server
side in `_register_args()`; `_valid_date()` drops anything that is not
`YYYY-MM-DD` rather than trusting it.

Two exports, both CSV with a UTF-8 BOM so Excel opens them correctly:

* **Summary** — one row per pass, with item count and totalled quantity/cartons.
* **Detailed** — one row per item, for checking what actually left.

Both are written by `exports.py` from the same `db.export_rows()` generator, so a
CSV and an Excel export of the same report always contain the same data.

**Excel is written with openpyxl, not pandas.** pandas would add tens of
megabytes and hands the actual .xlsx writing to openpyxl anyway. `xlsx_bytes()`
uses openpyxl's write-only mode so a large export streams rows into the file
instead of holding a cell object for each. A workbook cannot be streamed to the
browser the way CSV can — the zip index is written last — so it is built and
then sent; CSV is still streamed.

Only the columns in `exports.NUMERIC_COLUMNS` go into Excel as numbers, so an
auditor can total a quantity column. Everything else stays text on purpose: an
invoice number like `0012` must not arrive as `12`.

`db.export_rows()`
is a **generator** and the route streams it, so exporting a whole year never
builds the year in memory. The generator opens its **own connection**: a streamed
response is produced after the request context has ended, so `g.db` is already
closed by then — that is a real bug the tests caught, not a hypothetical.

## Uploading a batch

The picker keeps its own list of chosen files (so a second drop adds to the
first rather than replacing it), shows each filename with its size and a running
"N files selected", and lets the operator clear it.

On submit, JavaScript posts the files **one at a time** to `/upload/one` and
advances the progress bar as each response arrives. That means the bar tracks
work actually finished rather than bytes in flight, and it sidesteps
`MAX_CONTENT_LENGTH` — 50 scanned PDFs in a single request would exceed the
32 MB cap. Each row is then marked ✓, ! (readable but needs attention) or ×.
With JavaScript off the plain form still posts every file in one request and the
server handles it the same way.

**The form sets `noValidate` and validates in JavaScript.** The chosen files live
in a JS array, not in the `<input>` — the input is cleared after every pick so
re-selecting the same file still fires a `change`. That leaves the input empty,
so the browser's own `required` check refused to submit ("Please select one or
more files") even with files clearly listed, and the submit handler never ran.
`required` stays in the markup for the no-JS path, where the input really does
carry the files. Do not remove either half of that pairing.

## Timestamps

**Everything is stored in local time, never UTC**, because the register, the
date filters and the audit reports are all read against the office clock.

`db._now()` is the single source: `datetime.now()`, formatted
`YYYY-MM-DD HH:MM:SS`. Timestamps are passed **explicitly** on every INSERT
rather than left to the column default, because SQLite's `datetime('now')`
returns **UTC** — in India that filed a pass created at 02:37 on the 5th as
21:07 on the 4th, and a report for "today" silently missed everything issued
after 18:30. The schema defaults now say `datetime('now', 'localtime')` as a
belt-and-braces fallback for fresh databases.

Two traps that made this bug survive its first fix, both now closed:

* **Changing a column DEFAULT does nothing to an existing database.**
  `CREATE TABLE IF NOT EXISTS` leaves an existing table exactly as it was, so
  older installs kept recording UTC. Set the value in Python; do not trust the
  default.
* **There were two copies of the gate pass INSERT** (single and batch), so
  fixing one left the other wrong. `create_gate_pass()` and
  `create_gate_passes_batch()` both go through `_insert_gate_pass()` now — keep
  it that way.

`_migrate_data()` corrects an older book in place, keyed on the `user_version`
the file was last at, and runs exactly once. `audit_log.created_at` is
deliberately **not** corrected: the log is append-only (rule 3) and its trigger
refuses the UPDATE. Disabling that trigger to tidy timestamps would defeat the
guarantee the log exists for, so its historical rows keep their UTC values.

## Database and concurrency

Four people share one SQLite file on the office server. The settings that make
that safe live in `db.connect()` / `_apply_pragmas()`:

| PRAGMA | Value | Why |
|---|---|---|
| `journal_mode` | `WAL` | Readers never block the writer and vice versa |
| `busy_timeout` | 10000 ms | A writer waits its turn instead of failing instantly — this is the setting that removes "database is locked" |
| `synchronous` | `NORMAL` | The documented companion to WAL: fast commits, still no corruption on power loss |
| `foreign_keys` | `ON` | Per-connection, off by default in SQLite |
| `cache_size` | `-16000` | 16 MB per connection |
| `temp_store` | `MEMORY` | Sorts and temp tables never touch disk |
| `mmap_size` | 256 MB | Reads come from the page cache |

**Writes go through `db.writing(conn)`**, a context manager that issues
`BEGIN IMMEDIATE`, commits on success and rolls back on any exception. Two rules:

1. The connection is opened with `isolation_level=None` (autocommit). The driver
   must not open a DEFERRED transaction of its own — a deferred transaction that
   later upgrades to a write is exactly what produces `SQLITE_BUSY` under load.
2. `BEGIN IMMEDIATE` takes the write lock **before** reading anything. That is
   what lets `create_gate_pass()` read the serial counter and insert the pass
   without another writer slipping in between. Never use a plain `BEGIN` there.

Any new multi-statement write must be wrapped in `writing()`. Single statements
autocommit correctly on their own. `writing()` nests safely.

**The schema is only applied when it changes.** `PRAGMA user_version` is compared
against `db.SCHEMA_VERSION` on connect; the schema script used to run on *every*
request, taking a write lock each time. **Bump `SCHEMA_VERSION` whenever you edit
`schema.sql` or `ADDED_COLUMNS`**, or existing databases will never pick the
change up.

### Indexes

Declared in `schema.sql`. `serial_no` needs none of its own — its `UNIQUE`
constraint already provides one.

A trap worth remembering: an index declared `COLLATE NOCASE` **cannot** serve a
plain `WHERE col = ?`, because the column's own collation is BINARY and the two
must match. The customer and supplier indexes were originally written that way
and were silently never used. `tests/test_flow.py` now asserts the query plan for
every hot lookup, and `tests/benchmark.py` prints `EXPLAIN QUERY PLAN` for each.

### Reads

`list_gate_passes()` is capped at `REGISTER_PAGE_SIZE` (200). Without a LIMIT the
register built every row in the book into memory on each page view — the one
query that genuinely falls over as the book grows. Pass `limit=None` for exports.

Search is a "contains" match (`%term%`), which **no B-tree index can serve**;
SQLite reads the table. That is deliberate — it is ~17 ms over 100k passes and
keeps the box behaving the way people expect. If it ever stops feeling instant,
the answer is an FTS5 table, not more indexes.

### Measured at 100,000 passes / 300,000 item rows (48 MB)

    Register, first page              0.6 ms
    Look up a pass by its number     0.02 ms
    Print a pass (row + items)       0.01 ms
    Search by invoice number         16.5 ms
    4 staff x 25 passes at once      0 locked errors, 0 duplicates, no gaps

### Running it for the office

`python3 app.py` is Flask's **development** server — fine for trying things out,
not for four people all day. For the real deployment put a production WSGI server
in front (waitress is the least intrusive: `pip install waitress`, then
`waitress-serve --port=8090 app:app`) and keep it to a **single process**. Threads
are fine — each request opens its own connection. Multiple *processes* also work
thanks to WAL, but there is no reason to add them at this size.

There is still no HTTPS; see the note in "Signing in".

### Backups

`storage/` is the book and is gitignored, so committing does **not** back it up.
With WAL there are `-wal` and `-shm` files alongside the database — copying only
`gate_pass.db` while the app is running can miss recent transactions, or catch a
half-written page, and the result looks like a valid file. Use `deploy/backup.sh`,
which runs `VACUUM INTO` (SQLite's own online backup, safe on a live database),
runs `integrity_check` on the copy before trusting it, gzips it, and only then
ages out anything older than 90 days — a failed run never deletes the last good
backup. Nightly cron:

```
0 21 * * *  /home/fanzart/gate-pass/deploy/backup.sh >> /var/log/gate-pass-backup.log 2>&1
```

To restore: stop the service, `gunzip` the chosen copy over
`storage/gate_pass.db`, delete any `-wal`/`-shm` beside it, start the service.
Verified end to end — a restored copy comes back with its passes, accounts and
all four triggers intact.

It drives Python from `.venv` rather than the `sqlite3` command line, which is
not installed on this machine.

## Hosting

Three files in `deploy/`, all verified to boot:

| File | What it is |
|---|---|
| `gunicorn.conf.py` | `gunicorn -c deploy/gunicorn.conf.py app:app` — 3 workers x 2 threads on 127.0.0.1:8090 |
| `gate-pass.service` | systemd unit, `ProtectSystem=strict` with `storage/` the only writable path |
| `nginx-gate-pass.conf` | reverse proxy, `client_max_body_size 32M`, static files served directly |

Numbers that have to agree, or something breaks in a way that looks unrelated:

- **`client_max_body_size` (32M) ≥ `MAX_CONTENT_LENGTH` in app.py (32MB).** Lower
  and nginx rejects a big scanned invoice with its own 413 before Flask can say
  anything useful. The batch uploader posts one file per request, so this is a
  per-invoice limit, not per batch.
- **nginx `proxy_read_timeout` (180s) > gunicorn `timeout` (120s) > the slowest
  request.** A whole-year detailed .xlsx takes ~20s to build at 120,000 item
  rows; gunicorn's default 30s would kill it partway and hand over a broken file.
- **`proxy_set_header Host $host` is required, not cosmetic.** Every POST is
  checked for a same-origin `Origin` header against `request.host`; rewrite the
  Host and every form submission in the app starts returning 403.

`app.py`'s `__main__` block is for development only and gunicorn never uses it.
The debugger is off unless `GATE_PASS_DEBUG=1` — the Werkzeug debugger is an
interactive Python console rendered on the error page, so leaving it on where
anyone can reach it is a way in, not a convenience.

Set `GATE_PASS_HTTPS=1` **only once a certificate is live**: it marks the session
cookie HTTPS-only, and setting it on a plain-HTTP site means nobody can sign in.

### What a hosted instance is hardened against

- `/invoices/<path>` is rooted at `storage/invoices/`, **not** `storage/`. Rooted
  at `storage/` it served `gate_pass.db` (every password hash) and `secret_key`
  (enough to forge an admin session cookie) to any signed-in account, including
  one with every permission switched off. `send_from_directory` blocks `..`, but
  it cannot know the root it was handed was too wide. Tested in
  `test_hosting_hardening`.
- Cross-site writes are refused. `SESSION_COOKIE_SAMESITE="Lax"` stops the cookie
  being sent on a cross-site POST at all, and `block_cross_site_writes` rejects
  any unsafe method whose `Origin`/`Referer` host is not `request.host`. A
  browser always sends `Origin` on a cross-site POST and cannot be made to omit
  it, so a mismatch is proof of forgery; a *missing* Origin is allowed, which is
  what keeps command-line clients and the test suite working. Before this, any
  web page could cancel a gate pass in a signed-in operator's name.
- Passwords are scrypt (`scrypt:32768:8:1`, Werkzeug's default) — no change
  needed, but do not "upgrade" this to a bare hash.
- `X-Content-Type-Options`, `X-Frame-Options: DENY`, `Referrer-Policy` on every
  response.

**Still open, accepted for a LAN deployment:** there is no rate limit on
`/login`, so a machine on the office network can try passwords as fast as scrypt
allows (deliberately slow, but not zero). Add one before this is ever reachable
from the internet.
