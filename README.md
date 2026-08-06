# Fanzart Gate Pass

Turns a supplier's tax invoice into a printed gate pass with a serial number
that cannot be reused, skipped or edited — replacing the handwritten gate pass
book at the warehouse door.

Upload the invoice PDF, check what was read off it, issue. The pass gets the
next number in the book and prints two-up on A4 for the cut line down the middle.
The number of cartons is left blank on purpose: it is written by hand at the
door, because one fan can be two cartons and the spares can share a third.

---

## What it does

**Reads the invoice.** Invoice number, supplier, customer, date and every line
item are lifted straight out of the PDF — including invoices whose item table
runs across two pages. A scan with no text layer still becomes a draft; it just
has to be typed in before it can be issued.

**Numbers the book.** `FZ-00001` upwards, derived from the passes themselves
rather than a counter, so the sequence can never drift out of step with what was
printed. Four people issuing at the same moment get four consecutive numbers with
no duplicates and no gaps — proven under load, not assumed. Cancelling a pass
keeps its number; the number is never handed out twice.

**Prints properly.** Two A5 passes on A4 landscape (or one per A5 sheet). An
invoice with more items than fit on a page runs onto a second page under *the
same* gate pass number, marked "Page 1 of 2" — never a split serial.

**Keeps the register.** Every pass, searchable and filterable, with a reports
page that exports to CSV or a real `.xlsx` workbook for audit.

**Batching.** Upload 30–50 invoices at once with a progress bar, review them as a
list, issue them as one unbroken block of numbers, and print the lot straight from
the register without downloading anything.

## Rules the database enforces

Not conventions — triggers. They apply to anything touching the file, including
a database browser:

| Rule | What happens if you try |
|---|---|
| A gate pass is never deleted | `gate passes cannot be deleted, cancel instead` |
| A serial number is never edited | `serial number and preparer are immutable` |
| The audit log is append-only | `audit log is append-only` |

Cancelling is a status change that records who did it, when, and why.

## Accounts

There is no self-signup. The first account is created on the server and is
automatically an admin; every account after that is created by an admin on the
**People** page. Accounts are disabled rather than deleted, because a pass keeps
the name of whoever prepared it.

Seven permissions are granted per person:

`can_search_register` · `can_filter_register` · `can_export_reports` ·
`can_access_settings` · `can_manage_people` · `can_cancel_passes` ·
`can_batch_print`

They are enforced on the server, not by hiding buttons — editing the query string
gets you nowhere. Passwords are stored as scrypt hashes and cannot be recovered,
only reset.

---

## Running it

Python 3.12+.

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
```

Create the first account (it becomes the admin):

```bash
.venv/bin/python3 manage_users.py add
```

Development server on <http://127.0.0.1:8090>:

```bash
GATE_PASS_DEBUG=1 .venv/bin/python3 app.py
```

`GATE_PASS_DEBUG` is off by default and should stay off anywhere else — the
Werkzeug debugger is an interactive Python console on the error page.

### Hosting

```bash
.venv/bin/gunicorn -c deploy/gunicorn.conf.py app:app
```

`deploy/` also has a systemd unit, an nginx reverse-proxy config, and
`backup.sh`. Three numbers have to agree between them — see the **Hosting**
section of [CLAUDE.md](CLAUDE.md), which explains what breaks if they don't.

Back up nightly. `storage/` holds the entire book and is not in git:

```
0 21 * * *  /path/to/gate-pass/deploy/backup.sh >> /var/log/gate-pass-backup.log 2>&1
```

`backup.sh` uses SQLite's online backup, verifies the copy before trusting it,
and never deletes an old backup until a new one has passed that check.

### Tests

```bash
.venv/bin/python3 tests/test_flow.py
```

A plain script, no pytest. It covers the parser, serial allocation under
concurrent writers, permissions, the print geometry and the hosting hardening.

The four sample invoices the parser tests read are **not in this repository** —
they are real supplier invoices carrying a GSTIN and named customers. A fresh
clone reports `537/537 checks passed` and lists the 13 tests it skipped; nothing
fails. With the PDFs in `tests/sample_invoices/` it is `698/698`. See
[tests/sample_invoices/README.md](tests/sample_invoices/README.md).

```bash
.venv/bin/python3 tests/benchmark.py 100000
```

Builds a throwaway 100,000-pass database and reports what someone actually waits
for. Last run: register 1.15 ms, print 0.03 ms, and four staff issuing 25 passes
each simultaneously with zero lock errors, zero duplicates and no gaps.

---

## Layout

```
app.py              routes: upload -> review -> issue -> print
db.py               all SQL; the only place the schema is touched
invoice_parser.py   PDF -> fields and items, by word coordinates
exports.py          CSV and xlsx writers
schema.sql          tables, indexes, and the triggers above
templates/          Jinja pages; _pass_card.html is the printed pass
static/css/print.css  print geometry — read the comments before changing it
deploy/             gunicorn, systemd, nginx, backup
tests/              test_flow.py, benchmark.py, preview_server.py
storage/            the book, uploads and the signing key (not in git)
```

[CLAUDE.md](CLAUDE.md) is the design record: why the row height is computed on
the server, why the page margin is zero, which index collation was silently
useless, and what each hard-won number is protecting against. Read it before
changing the print layout or the numbering.
