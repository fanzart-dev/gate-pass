# Sample invoices

The four PDFs the parser tests read are **not in this repository**. They are real
supplier invoices and carry a supplier's GSTIN and address, government IRN
hashes, and the names of individual customers — none of which belongs in a
public repo.

Without them, `tests/test_flow.py` runs everything else and reports which tests
it skipped. Nothing fails.

To run the parser tests in full, put these files here:

| File | What it exercises |
|---|---|
| `golden_touch_sample.pdf` | The plain case: one item, one page |
| `golden_touch_two_page.pdf` | 13 items over two pages, no "Total Amount in words" line |
| `golden_touch_service_line.pdf` | A customer who is a person, a service line, a two-digit quantity |
| `golden_touch_bi_series.pdf` | The BI layout: two extra columns before the name |
| `mangaldeep_scan.pdf` | A scan with no text layer at all |

Each one is here because it broke the parser once. The two-page invoice is the
important one: its item box has to be bounded by the *second* full-width rule on
the page, because the first is the underline beneath the column headings — using
the first finds zero items on every invoice.

Add the supplier's invoice here whenever the parser changes, and keep it out of
git.
