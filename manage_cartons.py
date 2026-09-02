#!/usr/bin/env python3
"""Load and inspect the master carton list.

    python3 manage_cartons.py import "Box Qty.xlsx"   # load or refresh
    python3 manage_cartons.py count                   # how many rows are loaded
    python3 manage_cartons.py look-up "AARI - APRICOT GOLD"

The list lives in the database, not in the repository, for two reasons. It is
the company's own product data and this repository is public; and the office
needs to be able to correct a wrong count without anybody editing a file on a
server. Re-running `import` against a corrected spreadsheet is the normal way
to update it — existing rows are updated in place, nothing is deleted.

The spreadsheet needs two columns, `ITEM NAME` and `no of cartons`. Column
order does not matter; the header row is found by name.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import db  # noqa: E402

DEFAULT_DB = Path(__file__).parent / "storage" / "gate_pass.db"
NAME_HEADINGS = ("ITEM NAME", "ITEM", "NAME", "MODEL")
COUNT_HEADINGS = ("NO OF CARTONS", "NO. OF CARTONS", "CARTONS", "BOX QTY")


def _heading_columns(row):
    """Which column holds the name and which the count.

    Read from the header text rather than assumed to be columns A and B: the
    sheet is maintained by hand and a column inserted in front of it would
    otherwise load 313 rows of the wrong data without complaining.
    """
    name_at = count_at = None
    for index, cell in enumerate(row):
        heading = str(cell or "").strip().upper().replace(".", "")
        if name_at is None and heading in [h.replace(".", "") for h in NAME_HEADINGS]:
            name_at = index
        if count_at is None and heading in [h.replace(".", "") for h in COUNT_HEADINGS]:
            count_at = index
    return name_at, count_at


def read_spreadsheet(path):
    """Returns [(item name, cartons)] and a list of complaints about skipped rows."""
    import openpyxl

    sheet = openpyxl.load_workbook(path, data_only=True).worksheets[0]
    rows = list(sheet.iter_rows(values_only=True))
    if not rows:
        raise SystemExit(f"{path} is empty")

    name_at, count_at = _heading_columns(rows[0])
    if name_at is None or count_at is None:
        raise SystemExit(
            f"could not find the columns in {path}.\n"
            f"  first row: {rows[0]}\n"
            f"  expected a heading like 'ITEM NAME' and one like 'no of cartons'")

    pairs, skipped = [], []
    for number, row in enumerate(rows[1:], start=2):
        name = str(row[name_at] or "").strip() if name_at < len(row) else ""
        raw = row[count_at] if count_at < len(row) else None
        if not name:
            continue                      # trailing blank rows are normal
        try:
            count = int(str(raw).strip())
        except (TypeError, ValueError):
            skipped.append(f"row {number}: {name!r} has carton count {raw!r}")
            continue
        if count < 0:
            skipped.append(f"row {number}: {name!r} has a negative count")
            continue
        pairs.append((name, count))
    return pairs, skipped


def main(argv):
    if len(argv) < 2:
        print(__doc__)
        return 1
    command = argv[1]
    conn = db.connect(DEFAULT_DB)

    if command == "import":
        if len(argv) < 3:
            print("usage: manage_cartons.py import <spreadsheet.xlsx>")
            return 1
        source = Path(argv[2]).expanduser()
        if not source.exists():
            print(f"no such file: {source}")
            return 1
        pairs, skipped = read_spreadsheet(source)

        # Two rows that differ only in spacing or dash would collide on the
        # match key, and the second would silently overwrite the first. Say so
        # rather than picking one.
        seen = {}
        for name, count in pairs:
            seen.setdefault(db.normalize_item_name(name), []).append((name, count))
        clashes = {k: v for k, v in seen.items() if len({c for _, c in v}) > 1}
        for key, entries in clashes.items():
            print(f"  CONFLICT: these are the same item but disagree -> {entries}")
        if clashes:
            print("\nFix the spreadsheet and run this again; nothing was loaded.")
            return 1

        added, updated, unchanged = db.upsert_carton_mappings(conn, pairs)
        print(f"read {len(pairs)} rows from {source.name}")
        print(f"  {added} added, {updated} updated, {unchanged} already correct")
        print(f"  {db.carton_mapping_count(conn)} items in the master list now")
        for complaint in skipped:
            print(f"  skipped {complaint}")
        return 0

    if command == "count":
        print(f"{db.carton_mapping_count(conn)} items in the master list")
        return 0

    if command == "look-up":
        if len(argv) < 3:
            print('usage: manage_cartons.py look-up "ITEM NAME"')
            return 1
        name = argv[2]
        print(f"  as written : {name}")
        print(f"  match key  : {db.normalize_item_name(name)}")
        if db.is_non_stock_item(name):
            print("  result     : left blank (spare, freight or service line)")
            return 0
        found = db.carton_count_for(conn, name)
        print(f"  result     : {found if found is not None else 'left blank (not in the master list)'}")
        return 0

    print(__doc__)
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
