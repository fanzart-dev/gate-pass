"""All SQL for the gate pass app lives here. Routes in app.py never write SQL directly."""

import json
import re
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

from werkzeug.security import check_password_hash, generate_password_hash

import invoice_parser

SCHEMA_PATH = Path(__file__).parent / "schema.sql"

# Columns added after the first release. Existing databases are the real gate
# pass book and are never rebuilt, so missing columns are added in place before
# the schema (and its triggers, which reference them) is applied.
ADDED_COLUMNS = {
    "drafts": [
        ("remarks", "TEXT NOT NULL DEFAULT ''"),
    ],
    "gate_passes": [
        # Typed on the review screen and printed in the Remarks box. Unlike
        # cartons (rule 5) this one CAN be known at the keyboard — it is a note
        # about the consignment, not a count of how it was packed — so it is
        # captured with the pass rather than left blank for the gate. The box
        # still prints empty when nobody typed anything, to be written on.
        ("remarks", "TEXT NOT NULL DEFAULT ''"),
        ("prepared_by", "TEXT NOT NULL DEFAULT ''"),
        ("prepared_by_user_id", "INTEGER REFERENCES users(id)"),
        ("cancelled_by", "TEXT"),
    ],
    "users": [
        ("status", "TEXT NOT NULL DEFAULT 'pending'"),
        ("is_admin", "INTEGER NOT NULL DEFAULT 0"),
        ("decided_at", "TEXT"),
        ("decided_by", "TEXT"),
        ("permissions", "TEXT NOT NULL DEFAULT '{}'"),
    ],
}

# Run once, right after the matching column is added, to carry old data forward.
BACKFILL = {
    # Everyone who could already sign in stays able to. Approval only gates
    # accounts created from here on.
    ("users", "status"):
        "UPDATE users SET status = CASE WHEN is_active = 0 THEN 'disabled' ELSE 'approved' END",
    # An existing single-user install would otherwise have nobody who can
    # approve anyone, so the earliest account becomes the first admin.
    ("users", "is_admin"):
        "UPDATE users SET is_admin = 1 WHERE id = (SELECT MIN(id) FROM users)",
}



# Per-feature permissions, stored as a JSON object on the user row. These are
# the single source of truth for what someone may do; `is_admin` is kept in
# lockstep with can_manage_people (see _sync_admin_flag) purely so the existing
# lockout guards and nav checks keep working off one value.
# Short labels for the tick boxes. Searching and filtering the register used to
# be two separate permissions; nobody ever wanted one without the other, so they
# are one — `can_search_register` covers both and `can_filter_register` is gone.
PERMISSIONS = {
    "can_search_register": "Search Register",
    "can_export_reports": "Reports Access",
    "can_access_settings": "Manage Settings",
    "can_manage_people": "Manage Users",
    "can_cancel_passes": "Cancel Pass",
    "can_batch_print": "Batch Print",
    "can_review_drafts": "Review Drafts",
    "can_edit_parsed_details": "Edit Draft Details",
    "can_edit_issued_pass": "Edit Issued Gate Pass",
}

# The sentence under each tick box. Kept apart from the label so the tick list
# stays scannable while still saying what the setting actually does — several of
# these change how the app behaves, not just what is visible.
PERMISSION_HINTS = {
    "can_search_register": "Search and filter the register by date and status",
    "can_export_reports": "Open Reports and generate CSV or Excel exports",
    "can_access_settings": "View and change Settings, including the numbering",
    "can_manage_people": "Create accounts and set what others may do",
    "can_cancel_passes": "Cancel an issued pass — it keeps its number",
    "can_batch_print": "Select several passes and print them in one go",
    # Without this, an upload is parsed, numbered, issued and sent straight to
    # the print preview. With it, the pass stops at a draft first so it can be
    # checked before a number is spent on it.
    "can_review_drafts": "Check invoices as drafts first, instead of issuing straight away",
    # Whether a person may CHANGE what the parser read, as opposed to filling in
    # what it could not read. Without this, a value the invoice actually yielded
    # is fixed and only the blanks can be typed in — so a scan can still be
    # completed by hand, but nobody can quietly restate what a document said.
    "can_edit_parsed_details": "Change what the invoice was read as saying, not just fill the blanks",
    # The heaviest permission here, and off by default. Everything else changes
    # what happens next; this changes what the register says already happened.
    # Every edit is written to the audit log with the old value beside the new,
    # so the record of the change survives even though the value does not.
    "can_edit_issued_pass": "Correct an issued pass — quantities, cartons, customer or remarks",
}

# The most a batch print can cover. A runaway request would otherwise try to
# render thousands of sheets into one document and hang the browser.
MAX_BATCH_PRINT = 50

# What an admin gets, and what a new account gets if nothing is ticked.
ALL_PERMISSIONS = {key: True for key in PERMISSIONS}
NO_PERMISSIONS = {key: False for key in PERMISSIONS}

# Accounts that pre-date per-feature permissions: an admin keeps every one of
# them, everyone else starts with none and is granted what they need.
BACKFILL[("users", "permissions")] = (
    "UPDATE users SET permissions = CASE WHEN is_admin = 1 THEN '{}' ELSE '{}' END".format(
        json.dumps(ALL_PERMISSIONS), json.dumps(NO_PERMISSIONS))
)

APPROVED = "approved"
PENDING = "pending"
REJECTED = "rejected"
DISABLED = "disabled"

# The printed form holds 26 item rows per page. A longer invoice is NOT refused
# and is NOT split into separate gate passes: it stays one gate pass with one
# number, printed across as many pages as it needs. Splitting the number would
# mean the same shipment leaving under two serials, which is exactly what the
# unbroken run is meant to prevent.
ITEMS_PER_PAGE = 26

# The printed item box is a fixed height and its rows share it, so the number of
# rows drawn is what decides how tall each one is. Blank rows are invisible —
# the box has column dividers but no rules between rows — so drawing fewer of
# them simply spaces the items out. Twenty-two gives a ~6.7mm row (about 25px),
# which reads comfortably without looking loose; a fuller pass draws more rows
# and they tighten further to keep it on one sheet.
#
# This, not the cell padding, is the dial for row spacing. The padding and
# line-height only set a floor (~5mm), and the rows sit well above it because
# the table shares out the fixed box height — changing the padding alone moves
# nothing on a normal pass.
PRINT_MIN_ROWS = 22


# Height of the item box's body on a printed A5 half. Row heights are derived
# from this and written into the page as an explicit value, so a row is the same
# height whatever paper size or print scale the browser is using.
#
# 134, and every millimetre of the reduction from 142.8 was paid for:
#
#   142.8 -> 138  the "Prepared by" line finished 0.15mm inside the pass, which
#                 Chrome just fit and Firefox clipped in half. The pass is
#                 `overflow: hidden`, so a fraction of a millimetre is the
#                 difference between a footer and no footer.
#   138 -> 134    the header's second box gained a line (Document No above Date,
#                 with Remarks matching it on the right), growing from 5.4mm to
#                 11mm and pushing the item table 5.56mm down the page. A full
#                 26-item pass overflowed by 3.17mm.
#
# At 26 rows this gives 5.15mm each, against a hard floor of about 4.96mm
# (2mm vertical padding plus 7pt at line-height 1.2). There is very little left:
# anything else that grows above the table has to come out of the footer's
# signing room instead, or items per page must drop below 26.
ITEM_BODY_MM = 134


# The totals row lives INSIDE the table, as a tfoot, and its height comes out of
# the table's own budget rather than out of anything below it. Taking it from
# the item rows instead of from the page keeps two things true: no item is ever
# pushed off a full 26-line page, and the signature block below keeps its room.
# The rows get fractionally shorter on a page that shows totals, which nobody
# can see; an item vanishing off the bottom, or a signature with nowhere to go,
# both would be.
TOTALS_ROW_MM = 5.5


def print_row_height_mm(rows, with_totals=False):
    """The exact height of one item row, so the box fills without stretching.

    Rounded DOWN to two decimals: a fraction of a millimetre of slack at the
    bottom of the box is invisible, whereas rounding up would push the last row
    past the edge of the sheet.
    """
    rows = max(1, rows)
    budget = ITEM_BODY_MM - (TOTALS_ROW_MM if with_totals else 0)
    return int(budget / rows * 100) / 100


def print_row_count(item_count):
    """How many rows to draw on one printed page holding this many items."""
    return min(max(item_count, PRINT_MIN_ROWS), ITEMS_PER_PAGE)


def page_count(item_count):
    """How many printed pages one gate pass needs. Always at least one."""
    if item_count <= 0:
        return 1
    return (item_count + ITEMS_PER_PAGE - 1) // ITEMS_PER_PAGE


def paginate_items(items, per_page=None):
    """Split a pass's items into printed pages, keeping the running Sl No.

    Returns a list of pages, each a list of (sl_no, item). The numbering runs
    straight through — page 2 of a 32-item pass starts at 27, not at 1 —
    because it is one gate pass, not two.
    """
    per_page = per_page or ITEMS_PER_PAGE
    numbered = list(enumerate(items, start=1))
    return [numbered[i:i + per_page] for i in range(0, len(numbered), per_page)] or [[]]

DEFAULT_SETTINGS = {
    # Serials are <prefix><separator><5 digits>, e.g. FZ-00001. The prefix names
    # the run and may carry its own separator (FZ, FZ-, FZ27-, FZ/).
    "serial_prefix": "FZ",
    # The lowest number the next pass may take. Normally 0, meaning "carry on
    # from the last one issued". Set when somebody deliberately jumps the
    # numbering forward — after a box of pre-printed stationery is spoiled, say.
    # A stored floor rather than a placeholder row, because a fake gate pass
    # inserted to hold a gap would show up in the register and in every report
    # as a real one.
    "serial_min_seq": "0",
    "paper_mode": "a4x2",  # "a4x2" (two A5 passes on A4 landscape) or "a5" (one per sheet)
    # Two settings, not one. An office that counts cartons by hand at the gate
    # wants the quantity totalled and the carton column left alone; the single
    # "show totals" tick could not express that.
    "show_total_qty": "0",
    "show_total_cartons": "0",
    # There is no "show_vehicle": the vehicle prints whenever one was recorded
    # and is absent when one was not, which is what the setting was really for.
    # A tick box that only ever HIDES something already written on the pass is
    # a way to print a document missing information somebody entered.
    "company_name": "fanzart",
}


# Bump when schema.sql or ADDED_COLUMNS changes. Stored in the file as
# PRAGMA user_version, so a connection can tell in one cheap read whether the
# schema script needs running at all.
SCHEMA_VERSION = 14

# How long a writer waits for another writer before giving up. Four people
# clicking Issue at the same moment are serialised in milliseconds, so this is
# really a guard against a stuck process, not normal contention.
BUSY_TIMEOUT_MS = 10_000

# Retries for the rare BEGIN IMMEDIATE that fails despite busy_timeout (SQLite
# does not always apply the timeout to a snapshot conflict).
WRITE_RETRIES = 4
WRITE_RETRY_BASE_DELAY = 0.05


def connect(db_path, timeout=BUSY_TIMEOUT_MS / 1000):
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(
        db_path,
        timeout=timeout,
        detect_types=sqlite3.PARSE_DECLTYPES,
        # Autocommit. Transactions are opened explicitly with BEGIN IMMEDIATE
        # by writing() below, instead of the driver starting a DEFERRED one
        # behind our back — a deferred transaction that later upgrades to a
        # write is exactly what produces "database is locked" under load.
        isolation_level=None,
    )
    conn.row_factory = sqlite3.Row
    _apply_pragmas(conn)
    if conn.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
        init_db(conn)
    return conn


def _apply_pragmas(conn):
    # WAL is stored in the file, but re-asserting it costs nothing and means a
    # brand new database is never left in rollback-journal mode.
    conn.execute("PRAGMA journal_mode = WAL")
    # Readers never block the writer and the writer never blocks readers, so a
    # long register query cannot stall someone issuing a pass.
    conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    # NORMAL is the documented companion to WAL: a commit no longer waits for a
    # disk flush, and the worst case on power loss is losing the last few
    # transactions, never a corrupt database.
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA cache_size = -16000")   # 16 MB per connection
    conn.execute("PRAGMA temp_store = MEMORY")
    conn.execute("PRAGMA mmap_size = 268435456")  # 256 MB
    return conn


def init_db(conn):
    # executescript() issues its own COMMIT, so it cannot run inside writing().
    # Everything in it is CREATE ... IF NOT EXISTS, so two processes racing here
    # is harmless; busy_timeout makes them queue rather than fail.
    previous = conn.execute("PRAGMA user_version").fetchone()[0]
    with writing(conn):
        _migrate(conn)
        _migrate_data(conn, previous)
    conn.executescript(SCHEMA_PATH.read_text())
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")


# Columns whose value came from SQLite's datetime('now'), i.e. UTC, before
# SCHEMA_VERSION 5. Columns written by Python's _now() were already local and
# must not be shifted again.
# audit_log is deliberately absent: it is append-only (rule 3) and its trigger
# refuses an UPDATE. Correcting it would mean disabling the very guarantee the
# log exists for, so its historical rows keep the UTC values they were written
# with and only new entries are local.
UTC_TIMESTAMP_COLUMNS = [
    ("gate_passes", "issued_at"),
    ("drafts", "created_at"),
    ("users", "created_at"),
]


def _table_exists(conn, name):
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
    ).fetchone() is not None


def _migrate_data(conn, previous_version):
    """One-off data corrections, keyed to the version the file was last at.

    `previous_version` is 0 for a database that did not exist yet, which is why
    these are skipped on a fresh install — there is nothing to correct.
    """
    if previous_version and previous_version < 14 and _table_exists(conn, "settings"):
        # show_totals split into show_total_qty and show_total_cartons. Anyone
        # who had it on keeps both, so nothing disappears off a printed pass
        # without somebody choosing to turn it off.
        #
        # The table check is not paranoia: this runs BEFORE schema.sql creates
        # anything, so a database old enough to predate the settings table
        # reaches here with no table to read.
        row = conn.execute(
            "SELECT value FROM settings WHERE key = 'show_totals'").fetchone()
        if row is not None:
            for key in ("show_total_qty", "show_total_cartons"):
                conn.execute(
                    "INSERT INTO settings (key, value) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (key, row["value"]))
            conn.execute("DELETE FROM settings WHERE key = 'show_totals'")

    if previous_version and previous_version < 10:
        # can_search_register and can_filter_register merged into the first.
        # Anyone who held either keeps the combined one; nobody gains anything
        # they did not already have half of.
        try:
            conn.execute(
                "UPDATE users SET permissions = json_set(permissions, "
                "'$.can_search_register', json('true')) "
                "WHERE json_extract(permissions, '$.can_filter_register') = 1")
            conn.execute(
                "UPDATE users SET permissions = json_remove(permissions, "
                "'$.can_filter_register')")
        except sqlite3.OperationalError:
            pass

    if previous_version and previous_version < 9:
        # can_edit_parsed_details, same reasoning as below: an admin who could
        # already correct a draft keeps that, and nobody else silently gains it.
        try:
            conn.execute(
                "UPDATE users SET permissions = json_set(permissions, "
                "'$.can_edit_parsed_details', json('true')) WHERE is_admin = 1")
        except sqlite3.OperationalError:
            pass

    if previous_version and previous_version < 8:
        # can_review_drafts was added after permissions existed, and it is the
        # switch between "issue straight away" and "check it as a draft first".
        # Admins keep the reviewing step they already had; without this they
        # would silently lose the Drafts page on upgrade.
        try:
            conn.execute(
                "UPDATE users SET permissions = json_set(permissions, "
                "'$.can_review_drafts', json('true')) WHERE is_admin = 1")
        except sqlite3.OperationalError:
            pass

    if previous_version and previous_version < 7:
        # can_batch_print was added after permissions existed. Anyone who held
        # every permission at the time keeps parity — without this they would
        # silently drop from "full access" to "6 of 7" on upgrade.
        try:
            conn.execute(
                "UPDATE users SET permissions = json_set(permissions, "
                "'$.can_batch_print', json('true')) WHERE is_admin = 1")
        except sqlite3.OperationalError:
            pass

    if previous_version and previous_version < 5:
        # These were recorded in UTC by datetime('now'). Re-express them in local
        # time, which is what the app has always displayed and filtered on.
        # SQLite's 'localtime' modifier handles the offset per timestamp.
        for table, column in UTC_TIMESTAMP_COLUMNS:
            if not conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                (table,),
            ).fetchone():
                continue
            conn.execute(
                f"UPDATE {table} SET {column} = datetime({column}, 'localtime') "
                f"WHERE {column} IS NOT NULL AND {column} != ''"
            )


@contextmanager
def writing(conn):
    """One atomic write, taking the write lock up front.

    BEGIN IMMEDIATE claims the lock at the start rather than partway through,
    which is what makes concurrent writers queue politely instead of colliding
    once they have already done half their work.
    """
    if conn.in_transaction:
        # Already inside an outer writing() block — let that one commit.
        yield conn
        return

    for attempt in range(WRITE_RETRIES):
        try:
            conn.execute("BEGIN IMMEDIATE")
            break
        except sqlite3.OperationalError as exc:
            if attempt == WRITE_RETRIES - 1 or not _is_busy(exc):
                raise
            time.sleep(WRITE_RETRY_BASE_DELAY * (2 ** attempt))

    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def _is_busy(exc):
    message = str(exc).lower()
    return "locked" in message or "busy" in message


def _migrate(conn):
    """Add columns missing from an older database, in place.

    Runs before the schema script because the immutability triggers name these
    columns and would fail to compile against a table that lacks them. A no-op
    on a fresh database, where the tables do not exist yet.
    """
    for table, columns in ADDED_COLUMNS.items():
        existing = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
        ).fetchone()
        if existing is None:
            continue
        have = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        for name, definition in columns:
            if name in have:
                continue
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
            backfill = BACKFILL.get((table, name))
            if backfill:
                # Guard the backfill against columns the old schema may not have.
                try:
                    conn.execute(backfill)
                except sqlite3.OperationalError:
                    pass


# A run is named by a prefix, optionally ending in the separator that will be
# printed before the digits: FZ, FZ-, FZ27-, FZ/. Without one, a dash is used,
# so FZ and FZ- both print FZ-00001 and neither produces FZ--00001.
SERIAL_SEPARATORS = "-/_."
PREFIX_RE = re.compile(r"[A-Za-z0-9]{1,10}[-/_.]?")

# "FZ-00001" typed in full: the leading part is the prefix and the digits are
# where to start. Only split on an explicit separator, or FZ27 would be read as
# prefix FZ starting at 27 rather than as a prefix in its own right.
RUN_SPEC_RE = re.compile(r"^\s*([A-Za-z0-9]{1,10}[-/_.])(\d{1,6})\s*$")


def serial_for(prefix, seq):
    """FZ + 1 -> FZ-00001. FZ- + 1 -> FZ-00001. FZ/ + 1 -> FZ/00001."""
    prefix = (prefix or "").strip().upper()
    if prefix and prefix[-1] in SERIAL_SEPARATORS:
        return f"{prefix}{seq:05d}"
    return f"{prefix}-{seq:05d}"


def run_scope(prefix):
    """Which RUN a prefix belongs to — the counter is shared across spellings.

    FZ, FZ- and FZ/ are one run. If they were separate, FZ and FZ- would each
    count from one and both produce FZ-00001, and the second would be refused
    by the database at the moment somebody pressed Issue.

    Stripping the separator also means this matches the scopes already in the
    book, which were stored before prefixes could carry one.
    """
    return (prefix or "").strip().upper().rstrip(SERIAL_SEPARATORS)


def parse_run_spec(text):
    """Read what the user typed into (prefix, starting number or None).

    Accepts a bare prefix ("FZ", "FZ-", "FZ27-") or a whole example serial
    ("FZ-00001"), because the second is what people actually have in mind and
    typing it should not be an error.
    """
    text = (text or "").strip()
    whole = RUN_SPEC_RE.match(text)
    if whole:
        return validate_prefix(whole.group(1)), int(whole.group(2))
    return validate_prefix(text), None


def validate_prefix(prefix):
    """A prefix is the part before the digits, e.g. FZ or FZ- in FZ-00001."""
    prefix = (prefix or "").strip().upper()
    if not PREFIX_RE.fullmatch(prefix):
        raise ValueError(
            "the prefix must be 1 to 10 letters or digits, optionally ending in "
            "- / _ or . — for example FZ, FZ- or FZ27-")
    return prefix


def current_prefix(conn):
    return get_settings(conn)["serial_prefix"]


def prefix_in_use(conn, prefix):
    """Whether any gate pass was ever issued under this prefix.

    No longer a reason to refuse a prefix — reusing one RESUMES its count
    rather than restarting it, because next_seq reads MAX(serial_seq) for the
    run. Kept because the settings screen says what will happen, and because
    start_new_run needs to know whether "restarting" is the honest word.
    """
    row = conn.execute(
        "SELECT 1 FROM gate_passes WHERE serial_scope = ? LIMIT 1", (run_scope(prefix),)
    ).fetchone()
    return row is not None


def seq_after_last_issued(conn, scope):
    """The number after the highest ever issued in this run, ignoring the floor.

    Separate from next_seq because the floor is a single setting shared by
    whichever run is current. Switching from a run that was jumped forward to
    200 must not carry that 200 onto a different prefix, so start_new_run asks
    what the ROWS say and sets the floor itself.
    """
    row = conn.execute(
        "SELECT MAX(serial_seq) AS highest FROM gate_passes WHERE serial_scope = ?",
        (run_scope(scope),),
    ).fetchone()
    return (row["highest"] or 0) + 1


def next_seq(conn, scope):
    """The next number in a run, read from the gate passes themselves.

    Deriving it from MAX(serial_seq) rather than a separate counter means the
    sequence can never drift out of step with the book: the last number issued
    IS the last row. Cancelled passes keep their number and still count, so a
    cancellation never causes the next pass to reuse it.

    The one thing not in the rows is a deliberate jump forward, so the stored
    floor is applied on top. It only ever raises the answer, never lowers it —
    a floor below what has already been issued is ignored rather than handing
    out a number twice.

    Only correct when called inside writing() — BEGIN IMMEDIATE holds the write
    lock, so no other connection can insert between this read and our insert.
    The (serial_scope, serial_seq) index makes it a single index lookup, not a
    scan, however large the book gets.
    """
    after_last = seq_after_last_issued(conn, scope)
    try:
        floor = int(get_settings(conn).get("serial_min_seq") or 0)
    except (TypeError, ValueError):
        floor = 0
    return max(after_last, floor)


def next_serial_preview(conn):
    """What the next gate pass will be numbered, without allocating anything."""
    prefix = current_prefix(conn)
    return serial_for(prefix, next_seq(conn, prefix))


def start_new_run(conn, prefix, start_at=None, started_by=""):
    """Begin numbering under `prefix`, from `start_at` or the next free number.

    Nothing is ever deleted: every pass already issued keeps its number and
    stays in the register.

    A prefix that has been used before is allowed. It used to be refused, on
    the reasoning that a new run starts at 00001 and would hand out a number
    that is already on a printed pass. That is only true of a FRESH prefix:
    next_seq reads MAX(serial_seq) for the run, so reusing FZ after FZ-00115
    continues at FZ-00116. The rule was protecting against something that
    could not happen, while blocking the thing people actually want — going
    back to a clean FZ- after a run under an awkward prefix.

    What genuinely cannot happen is issuing a number twice. The database
    enforces that itself, with a UNIQUE constraint on (serial_scope,
    serial_seq); removing the check here would not have allowed a duplicate,
    only moved the failure from a sentence on this screen to an IntegrityError
    at the moment somebody pressed Issue. So the check is not removed, it is
    narrowed: it now refuses only a starting number that is actually taken, and
    says which number is free.
    """
    prefix = validate_prefix(prefix)
    scope = run_scope(prefix)
    first_free = seq_after_last_issued(conn, scope)

    if start_at is None:
        start_at = first_free
    start_at = int(start_at)
    if start_at < 1:
        raise ValueError("the starting number must be 1 or more")
    if start_at < first_free:
        raise ValueError(
            f"{serial_for(prefix, start_at)} has already been issued — "
            f"the first free number under {prefix} is "
            f"{serial_for(prefix, first_free)}. To begin again at "
            f"{serial_for(prefix, 1)}, use a prefix that has not been used, or "
            f"clear the passes already issued under this one")

    previous = current_prefix(conn)
    with writing(conn):
        update_settings(conn, serial_prefix=prefix)
        update_settings(conn, serial_min_seq=str(start_at))
        conn.execute(
            "INSERT INTO audit_log (action, details) VALUES ('new_serial_run', ?)",
            (f"numbering set to {serial_for(prefix, start_at)} (was {previous}) "
             f"by {started_by or 'unknown'}",),
        )
    return prefix, start_at


def _now():
    """Local wall-clock time, the single source of every timestamp in the book.

    Not UTC: the register, the date filters and the audit reports are all read
    against the office clock, and SQLite's own datetime('now') is UTC, which is
    what put issued_at 5.5 hours behind in India.
    """
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------------------
# Users. Several people at the office issue passes; all share one serial run.
# ---------------------------------------------------------------------------

MIN_PASSWORD_LENGTH = 8


def read_permissions(row):
    """The permission dict for a user row, with every key present.

    Missing or unreadable JSON reads as "no permissions" rather than raising —
    a corrupt value must never accidentally grant access.
    """
    raw = row["permissions"] if "permissions" in row.keys() else None
    granted = {}
    if raw:
        try:
            loaded = json.loads(raw)
            if isinstance(loaded, dict):
                granted = loaded
        except (ValueError, TypeError):
            granted = {}
    return {key: bool(granted.get(key, False)) for key in PERMISSIONS}


def user_can(user, permission):
    """Whether this user holds a permission. Unknown names are always False."""
    if not user or permission not in PERMISSIONS:
        return False
    return bool(user.get("permissions", {}).get(permission, False))


def _clean_permissions(permissions):
    """Only known keys, only booleans."""
    permissions = permissions or {}
    return {key: bool(permissions.get(key, False)) for key in PERMISSIONS}


def set_user_permissions(conn, user_id, permissions, decided_by=""):
    """Replace someone's permissions wholesale.

    Refuses to remove the last account that can manage people — otherwise the
    office locks itself out of its own user management with no way back except
    the command line.
    """
    user = get_user(conn, user_id)
    if user is None:
        raise ValueError("no such user")
    wanted = _clean_permissions(permissions)

    losing_people_admin = (user["permissions"]["can_manage_people"]
                            and not wanted["can_manage_people"])
    if losing_people_admin and count_admins(conn) <= 1:
        raise ValueError(
            "this is the last account that can manage people — give someone else "
            "that permission first")

    with writing(conn):
        conn.execute(
            "UPDATE users SET permissions = ?, is_admin = ? WHERE id = ?",
            (json.dumps(wanted), 1 if wanted["can_manage_people"] else 0, user_id),
        )
        conn.execute(
            "INSERT INTO audit_log (action, details) VALUES ('permissions', ?)",
            (f"{user['username']}: "
             + ", ".join(k for k, v in wanted.items() if v) or f"{user['username']}: none"
             + f" (set by {decided_by or 'unknown'})",),
        )
    return wanted


def create_user(conn, username, display_name, password, status=PENDING, is_admin=False,
                 permissions=None):
    """Creates an account. Defaults to pending — an admin approves it before the
    person can sign in. The very first account is forced to an approved admin,
    since otherwise there would be nobody able to approve anyone."""
    username = username.strip()
    display_name = display_name.strip()
    if not username:
        raise ValueError("username is required")
    if not username.replace("_", "").replace(".", "").replace("-", "").isalnum():
        raise ValueError("username may only contain letters, numbers, dot, dash and underscore")
    if not display_name:
        raise ValueError("full name is required — it is printed as 'Prepared by'")
    if not password or len(password) < MIN_PASSWORD_LENGTH:
        raise ValueError(f"password must be at least {MIN_PASSWORD_LENGTH} characters")

    if count_users(conn) == 0:
        # Nobody exists to grant anything, so the first account gets everything.
        status, is_admin, permissions = APPROVED, True, ALL_PERMISSIONS

    if permissions is None:
        permissions = ALL_PERMISSIONS if is_admin else NO_PERMISSIONS
    permissions = _clean_permissions(permissions)
    is_admin = permissions["can_manage_people"]

    try:
        cur = conn.execute(
            """INSERT INTO users (username, display_name, password_hash, status, is_admin,
                                   permissions, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (username, display_name, generate_password_hash(password), status,
             1 if is_admin else 0, json.dumps(permissions), _now()),
        )
    except sqlite3.IntegrityError as exc:
        raise ValueError(f"user {username!r} already exists") from exc
    conn.commit()
    return cur.lastrowid


def _user_dict(row):
    user = dict(row)
    user["permissions"] = read_permissions(row)
    # Kept in step with can_manage_people so one value drives the nav, the
    # lockout guards and the old is_admin checks.
    user["is_admin"] = 1 if user["permissions"]["can_manage_people"] else 0
    return user


def get_user_by_username(conn, username):
    row = conn.execute(
        "SELECT * FROM users WHERE username = ?", ((username or "").strip(),)
    ).fetchone()
    return _user_dict(row) if row else None


def get_user(conn, user_id):
    row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    return _user_dict(row) if row else None


def list_users(conn):
    rows = conn.execute("SELECT * FROM users ORDER BY display_name COLLATE NOCASE").fetchall()
    return [_user_dict(r) for r in rows]


def authenticate(conn, username, password):
    """Returns the user on a correct password for an approved account.

    Returns None if the password is wrong OR the account is not approved — the
    caller distinguishes the two by looking the user up separately, so a failed
    sign-in never reveals whether the username exists.
    """
    user = get_user_by_username(conn, username)
    if user is None or user["status"] != APPROVED:
        return None
    if not check_password_hash(user["password_hash"], password or ""):
        return None
    return user


# --- the master carton list ----------------------------------------------
#
# Counting cartons by hand was the slowest part of writing a gate pass, and the
# count is a property of the item, not of the invoice: one AARI ships in one
# box whoever is buying it. So it is looked up rather than typed.
#
# The lookup fills the field in and then gets out of the way — every carton box
# stays editable, because the master list will be wrong or missing sometimes and
# the person at the gate can see the actual pallet.

# Invoices spell the same fan several ways: "AARI-APRICOT GOLD",
# "AARI - APRICOT GOLD", "AARI  –  APRICOT  GOLD". All three are one item, so
# the match key folds the differences away rather than the master list needing a
# row per spelling.
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


# Lines that are not physical goods, or are goods that do not ship in a carton
# of their own. A spare part travels inside somebody else's box, and freight is
# not a thing at all — neither has a carton count, and writing 0 or 1 would be
# stating something untrue on a document that is signed at the gate.
NON_STOCK_KEYWORDS = ("SPARE", "SPARES", "SERVICE", "FREIGHT",
                      "CHARGES", "HARDWARE", "ACCESSORY")
# Plurals matter: a real invoice line reads "ERECTION COMMISSIONING AND
# INSTALLATION SERVICES", which \bSERVICE\b does not match. That one came out
# blank anyway because it is not in the master list, so the gate pass was right
# by luck — and luck stops working the day somebody adds a row for it.
_NON_STOCK_RE = re.compile(
    r"\b(?:SPARES?|SERVICES?|FREIGHTS?|CHARGES?|HARDWARES?|ACCESSORY|ACCESSORIES)\b")


def is_non_stock_item(name):
    """True for spares, freight and service lines.

    Whole words only: a model legitimately called something containing these
    letters inside a longer word must not be caught. Checked BEFORE the master
    list, so a spare stays blank even if some future row happens to match it.
    """
    return bool(_NON_STOCK_RE.search(normalize_item_name(name)))


def carton_count_for(conn, name):
    """Cartons for one item name, or None if it should be left for a human.

    None covers both "not in the master list" and "not the kind of line that
    has a carton count". Both mean the same thing to the caller: leave the box
    empty. An empty box is a question; a 0 or a 1 is an assertion, and a wrong
    assertion on a gate pass is worse than a blank one.
    """
    key = normalize_item_name(name)
    if not key or is_non_stock_item(key):
        return None
    row = conn.execute(
        "SELECT cartons FROM item_carton_mappings WHERE normalized = ?", (key,)
    ).fetchone()
    return row["cartons"] if row else None


# The master sheet gives cartons per UNIT, not per invoice line. The 32 rows
# worth 2 are the FANDELIERs, the 100-inch GRANDMASTERs and the two-part
# wall-mounts — physically large things that ship in two boxes each. Everything
# else is one box per fan.
#
# So a line reading "MICRON MODREN OAK, quantity 8" is 8 cartons, not 1. Real
# invoices in this office regularly run to 4, 5 and 8 of one model, so taking
# the master value literally would understate the count on a document that is
# signed at the gate — and understating boxes is how a box goes missing without
# anybody noticing.
#
# Set this to False for the literal reading: the master value regardless of how
# many were bought.
CARTONS_SCALE_WITH_QUANTITY = True


def cartons_for_line(conn, name, quantity):
    """Cartons for one invoice line, or None to leave the box empty."""
    per_unit = carton_count_for(conn, name)
    if per_unit is None:
        return None
    if not CARTONS_SCALE_WITH_QUANTITY:
        return per_unit
    try:
        units = int(str(quantity or "").strip())
    except ValueError:
        # The quantity could not be read, so the arithmetic cannot be done. An
        # empty box asks the operator a question; a number would assert
        # something nobody has actually worked out.
        return None
    return per_unit * units if units > 0 else None


def fill_cartons(conn, items):
    """Fill in carton counts on freshly parsed lines, leaving the rest blank.

    Never overwrites a value that is already there: if a parser managed to read
    a carton count off the document, the document beats the master list.
    """
    items = list(items or [])
    if not items:
        return []

    # One query for the whole invoice rather than one per line. The saving is
    # small in absolute terms — the lookup was already well under a millisecond
    # against ~285 ms of PDF parsing — but a query per line grows with the
    # document, and a 60-line invoice has no business issuing 60 statements.
    #
    # Deliberately NOT a cache loaded at startup: that would save the same
    # fraction of a millisecond and buy a real bug, where re-importing a
    # corrected sheet appears to do nothing until somebody restarts the app.
    wanted = {normalize_item_name(i.get("item_name")) for i in items
              if not str(i.get("cartons") or "").strip()}
    wanted = {key for key in wanted if key and not is_non_stock_item(key)}

    known = {}
    if wanted:
        keys = list(wanted)
        # Chunked: SQLite refuses a statement with more than 999 parameters by
        # default, and a big transfer memo could otherwise exceed it.
        for start in range(0, len(keys), 500):
            chunk = keys[start:start + 500]
            placeholders = ",".join("?" * len(chunk))
            for row in conn.execute(
                    f"SELECT normalized, cartons FROM item_carton_mappings "
                    f"WHERE normalized IN ({placeholders})", chunk):
                known[row["normalized"]] = row["cartons"]

    filled = []
    for item in items:
        item = dict(item)
        if not str(item.get("cartons") or "").strip():
            per_unit = known.get(normalize_item_name(item.get("item_name")))
            item["cartons"] = "" if per_unit is None else _line_cartons(
                per_unit, item.get("quantity"))
        filled.append(item)
    return filled


def _line_cartons(per_unit, quantity):
    """Per-unit count times how many were bought, as a string. "" if unknowable."""
    if not CARTONS_SCALE_WITH_QUANTITY:
        return str(per_unit)
    try:
        units = int(str(quantity or "").strip())
    except ValueError:
        return ""
    return str(per_unit * units) if units > 0 else ""


def upsert_carton_mappings(conn, pairs):
    """Load master rows. Returns (added, updated, unchanged).

    An upsert rather than a wipe-and-reload: reloading the sheet must not
    briefly empty the table while somebody is uploading an invoice, and a row
    the office corrected by hand should be updated in place, not resurrected
    with an old value under a new id.
    """
    added = updated = unchanged = 0
    with writing(conn):
        for name, cartons in pairs:
            key = normalize_item_name(name)
            if not key:
                continue
            row = conn.execute(
                "SELECT id, cartons FROM item_carton_mappings WHERE normalized = ?",
                (key,)).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO item_carton_mappings "
                    "(item_name, normalized, cartons, updated_at) VALUES (?, ?, ?, ?)",
                    (str(name).strip(), key, int(cartons), _now()))
                added += 1
            elif row["cartons"] != int(cartons):
                conn.execute(
                    "UPDATE item_carton_mappings SET item_name = ?, cartons = ?, "
                    "updated_at = ? WHERE id = ?",
                    (str(name).strip(), int(cartons), _now(), row["id"]))
                updated += 1
            else:
                unchanged += 1
    return added, updated, unchanged


def carton_list_is_empty(conn):
    """True when no master list has been imported at all.

    Worth distinguishing from "this item is not on the list": every carton box
    coming out blank looks identical either way, and the first time this
    happened the cause was simply that manage_cartons.py had never been run on
    the server. Silence made that look like a bug in the matching.
    """
    return carton_mapping_count(conn) == 0


def carton_mapping_count(conn):
    return conn.execute("SELECT COUNT(*) AS n FROM item_carton_mappings").fetchone()["n"]


# --- guarding the sign-in form against guessing --------------------------
#
# Needed the moment the site is reachable from the public internet. A Tailscale
# Funnel hostname is published in Certificate Transparency logs as soon as its
# certificate is issued, so it is found by scanners within hours and tried.
# Without this, /login accepts unlimited guesses at whatever rate the network
# allows.
#
# Two counters, deliberately very different sizes:
#
#   Per USERNAME is the real defence. Nobody legitimately gets a password wrong
#   eight times in a quarter of an hour, and an attacker cannot dodge it: they
#   must name the account they are attacking.
#
#   Per ADDRESS is a backstop against spraying one password across many
#   usernames. Its limit is deliberately loose because the whole office can sit
#   behind a single NAT address, and locking one address could lock out
#   everybody at once. It is also the weaker of the two: the address is read
#   from a forwarded header, which a determined caller can lie about. The
#   username counter cannot be lied about, which is why it is the tight one.
#
# Nothing here locks an account permanently — the window simply passes. An
# admin cannot be locked out of their own system by somebody else's attempt for
# longer than that.
LOGIN_WINDOW_MINUTES = 15
LOGIN_MAX_PER_USERNAME = 8
LOGIN_MAX_PER_IP = 40


def _lockout_seconds(conn, column, value, limit):
    row = conn.execute(
        f"SELECT COUNT(*) AS n, MIN(at) AS first FROM login_attempts "
        f"WHERE {column} = ? AND at > datetime(?, ?)",
        (value, _now(), f"-{LOGIN_WINDOW_MINUTES} minutes"),
    ).fetchone()
    if row["n"] < limit:
        return 0
    # Unblocks once the OLDEST attempt in the window ages out, so it is a
    # rolling window rather than a fixed one — each further guess pushes the
    # release later, and a patient user is let back in as soon as it is fair.
    started = datetime.strptime(row["first"], "%Y-%m-%d %H:%M:%S")
    free_at = started + timedelta(minutes=LOGIN_WINDOW_MINUTES)
    return max(1, int((free_at - datetime.now()).total_seconds()))


def login_lockout(conn, username, ip):
    """Seconds before this username or address may try a password again.

    0 means go ahead. Checked BEFORE the password is verified, so a locked-out
    caller cannot even learn whether their guess was right.
    """
    return max(
        _lockout_seconds(conn, "username", (username or "").strip().lower(),
                         LOGIN_MAX_PER_USERNAME),
        _lockout_seconds(conn, "ip", ip or "?", LOGIN_MAX_PER_IP),
    )


def record_failed_login(conn, username, ip):
    with writing(conn):
        conn.execute(
            "INSERT INTO login_attempts (username, ip, at) VALUES (?, ?, ?)",
            ((username or "").strip().lower(), ip or "?", _now()))
        # Pruned here rather than on a timer: this is the only place rows are
        # created, so the table cannot grow while nobody is failing to sign in.
        conn.execute(
            "DELETE FROM login_attempts WHERE at <= datetime(?, ?)",
            (_now(), f"-{LOGIN_WINDOW_MINUTES * 4} minutes"))


def clear_login_attempts(conn, username):
    """Wipe a username's failures after a correct password.

    Someone who mistypes a few times and then gets it right must not carry
    those failures towards a lockout for the next quarter of an hour.
    """
    with writing(conn):
        conn.execute("DELETE FROM login_attempts WHERE username = ?",
                     ((username or "").strip().lower(),))


def password_is_correct(conn, username, password):
    """Whether the password matches, ignoring account status. Used only to tell
    someone their own account is still awaiting approval."""
    user = get_user_by_username(conn, username)
    if user is None:
        return False
    return check_password_hash(user["password_hash"], password or "")


def list_users_by_status(conn, status):
    rows = conn.execute(
        "SELECT * FROM users WHERE status = ? ORDER BY created_at", (status,)
    ).fetchall()
    return [dict(r) for r in rows]


def count_pending(conn):
    return conn.execute(
        "SELECT COUNT(*) AS n FROM users WHERE status = ?", (PENDING,)
    ).fetchone()["n"]


def count_admins(conn, approved_only=True):
    query = "SELECT COUNT(*) AS n FROM users WHERE is_admin = 1"
    params = ()
    if approved_only:
        query += " AND status = ?"
        params = (APPROVED,)
    return conn.execute(query, params).fetchone()["n"]


def set_user_status(conn, user_id, status, decided_by=""):
    """Approve, reject, disable or re-enable an account.

    Refuses to leave the office with no admin who can sign in — that would lock
    everyone out of approvals with no way back except the command line.
    """
    if status not in (PENDING, APPROVED, REJECTED, DISABLED):
        raise ValueError(f"unknown status {status!r}")
    user = get_user(conn, user_id)
    if user is None:
        raise ValueError("no such user")
    if (user["is_admin"] and user["status"] == APPROVED and status != APPROVED
            and count_admins(conn) <= 1):
        raise ValueError("this is the last admin who can sign in — promote someone else first")

    conn.execute(
        "UPDATE users SET status = ?, decided_at = ?, decided_by = ? WHERE id = ?",
        (status, _now(), decided_by, user_id),
    )
    conn.commit()


def set_user_admin(conn, user_id, is_admin, decided_by=""):
    """Grant or revoke every permission at once — "admin" is now shorthand for
    holding all of them."""
    user = get_user(conn, user_id)
    if user is None:
        raise ValueError("no such user")
    if is_admin and user["status"] != APPROVED:
        raise ValueError("approve the account before making it an admin")
    set_user_permissions(conn, user_id,
                          ALL_PERMISSIONS if is_admin else NO_PERMISSIONS,
                          decided_by=decided_by)


def set_password(conn, user_id, password):
    if not password or len(password) < MIN_PASSWORD_LENGTH:
        raise ValueError(f"password must be at least {MIN_PASSWORD_LENGTH} characters")
    conn.execute("UPDATE users SET password_hash = ? WHERE id = ?",
                 (generate_password_hash(password), user_id))
    conn.commit()


def count_users(conn):
    return conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]


# ---------------------------------------------------------------------------
# Drafts — mutable scratch space, never touch gate_passes, so nothing here
# can consume a serial number.
# ---------------------------------------------------------------------------

def create_draft(conn, supplier_name="", customer_name="", invoice_no="",
                  invoice_date="", invoice_pdf_path=None, parse_notes="", items=None,
                  remarks=""):
    with writing(conn):
        cur = conn.execute(
            """INSERT INTO drafts (supplier_name, customer_name, invoice_no, invoice_date,
                                    invoice_pdf_path, parse_notes, remarks, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (supplier_name, customer_name, invoice_no, invoice_date, invoice_pdf_path,
             parse_notes, remarks, _now()),
        )
        draft_id = cur.lastrowid
        _replace_draft_items(conn, draft_id, items or [])
    return draft_id


def _replace_draft_items(conn, draft_id, items):
    conn.execute("DELETE FROM draft_items WHERE draft_id = ?", (draft_id,))
    for i, item in enumerate(items, start=1):
        conn.execute(
            """INSERT INTO draft_items (draft_id, sl_no, item_name, quantity, cartons)
               VALUES (?, ?, ?, ?, ?)""",
            (draft_id, item.get("sl_no", i), item.get("item_name", ""),
             item.get("quantity", ""), item.get("cartons", "")),
        )


def get_draft(conn, draft_id):
    row = conn.execute("SELECT * FROM drafts WHERE id = ?", (draft_id,)).fetchone()
    if row is None:
        return None
    items = conn.execute(
        "SELECT * FROM draft_items WHERE draft_id = ? ORDER BY sl_no", (draft_id,)
    ).fetchall()
    d = dict(row)
    d["items"] = [dict(item) for item in items]
    return d


def list_drafts(conn):
    rows = conn.execute("SELECT * FROM drafts ORDER BY created_at DESC").fetchall()
    return [dict(r) for r in rows]


def update_draft(conn, draft_id, supplier_name, customer_name, invoice_no,
                  invoice_date, vehicle_no, items, remarks=""):
    with writing(conn):
        conn.execute(
            """UPDATE drafts SET supplier_name = ?, customer_name = ?, invoice_no = ?,
                                  invoice_date = ?, vehicle_no = ?, remarks = ?
               WHERE id = ?""",
            (supplier_name, customer_name, invoice_no, invoice_date, vehicle_no,
             remarks, draft_id),
        )
        _replace_draft_items(conn, draft_id, items)


def delete_draft(conn, draft_id):
    with writing(conn):
        conn.execute("DELETE FROM draft_items WHERE draft_id = ?", (draft_id,))
        conn.execute("DELETE FROM drafts WHERE id = ?", (draft_id,))


# ---------------------------------------------------------------------------
# Gate passes — serial allocated here, immutable/append-only after this.
# ---------------------------------------------------------------------------

def create_gate_pass(conn, draft_id, supplier_name, customer_name, invoice_no,
                      invoice_date, vehicle_no, items, invoice_pdf_path=None,
                      prepared_by="", prepared_by_user_id=None, remarks=""):
    items = [i for i in items if str(i.get("item_name", "")).strip()]
    if not items:
        raise ValueError("at least one item with a name is required to issue a gate pass")
    for item in items:
        if not str(item.get("quantity", "")).strip():
            raise ValueError("every item needs a quantity")
        # Cartons are NOT required. They depend on how the goods are actually
        # packed, which is known at the gate rather than at the keyboard, so the
        # column prints blank and is filled in by hand on the paper copy.

    # The prefix names the current run. It only changes when an admin starts a
    # new one, so the count carries on across years by default.
    scope = current_prefix(conn)
    # The whole allocation — bump the counter, insert the pass, its items and the
    # audit line, and clear the draft — is one transaction that takes the write
    # lock before it reads the counter. Two people issuing at the same instant
    # are serialised by SQLite, so a number can never be handed out twice.
    with writing(conn):
        # Both the single and the batch path go through _insert_gate_pass, so
        # there is exactly one place that decides what a gate pass row looks
        # like — the duplicate INSERT that used to live here is how the UTC
        # timestamp survived being "fixed" once.
        gate_pass_id = _insert_gate_pass(
            conn, scope, next_seq(conn, scope), supplier_name, customer_name,
            invoice_no, invoice_date, vehicle_no, items, invoice_pdf_path,
            prepared_by, prepared_by_user_id, source=f"draft {draft_id}",
            remarks=remarks,
        )

        if draft_id is not None:
            conn.execute("DELETE FROM draft_items WHERE draft_id = ?", (draft_id,))
            conn.execute("DELETE FROM drafts WHERE id = ?", (draft_id,))

    return get_gate_pass(conn, gate_pass_id)


# --- duplicate document numbers -------------------------------------------
#
# The same invoice arriving twice is an ordinary mistake: a file uploaded twice
# in one batch, or a document already turned into a gate pass last week. Issuing
# both spends two numbers on one consignment, and the register then shows goods
# leaving twice that only left once.
#
# It is a WARNING, never a block. The same document number legitimately recurs —
# a split delivery, a corrected reissue — and the person at the desk knows which
# it is. What they cannot do is notice it unaided across fifty uploaded files.

def normalize_document_no(number):
    """The key two document numbers are compared on.

    Transfer memos carry their number as "TO NO: FR 262700644" while the same
    number on an invoice is "FR 262700644", and spacing varies. Comparing the
    raw strings would miss exactly the duplicate worth catching.
    """
    text = (number or "").upper()
    text = re.sub(r"^\s*(TO\s*NO|DOC(UMENT)?\s*NO|INVOICE\s*NO)\s*[:.\-]?\s*", "", text)
    return re.sub(r"[^A-Z0-9]", "", text)


def duplicate_document_report(conn, drafts):
    """Which of these drafts share a document number, and with what.

    Returns {draft_id: message}. Three places a number can already exist, and
    the message says which, because the answer to each is different:
    another file in the same batch, a draft already waiting, or a gate pass
    that has already been issued.
    """
    report = {}
    seen = {}

    # Normalised in Python on BOTH sides, using the one function, rather than
    # trying to reproduce it in SQL with nested REPLACEs. The SQL version
    # stripped spaces and "TO NO:" and nothing else, so FR-262700644 and
    # FR 262700644 would not have matched — which is precisely the pair worth
    # catching.
    already_issued = {}
    for row in conn.execute(
            "SELECT serial_no, invoice_no FROM gate_passes WHERE status = 'issued'"):
        key = normalize_document_no(row["invoice_no"])
        if key:
            already_issued.setdefault(key, row["serial_no"])

    for draft in drafts:
        key = normalize_document_no(draft["invoice_no"])
        if not key:
            continue

        issued = already_issued.get(key)
        if issued:
            report[draft["id"]] = (
                f"{draft['invoice_no']} has already been issued as {issued}.")
            continue

        if key in seen:
            first = seen[key]
            report[draft["id"]] = (
                f"{draft['invoice_no']} is also on draft #{first['id']}"
                + (f" ({first['source']})" if first["source"] else "") + ".")
            # The first one is a duplicate too — it just did not know yet.
            report.setdefault(first["id"], (
                f"{first['invoice_no']} is also on draft #{draft['id']}"
                + (f" ({_source_name(draft)})" if _source_name(draft) else "") + "."))
            continue

        seen[key] = {"id": draft["id"], "invoice_no": draft["invoice_no"],
                     "source": _source_name(draft)}
    return report


def _source_name(draft):
    """The uploaded file a draft came from, for naming it in a warning."""
    path = draft.get("invoice_pdf_path") or ""
    name = path.rsplit("/", 1)[-1]
    # Stored as <timestamp>_<original name>; the timestamp is noise to a reader.
    return name.split("_", 1)[-1] if "_" in name else name


def draft_problem(draft):
    """Why this draft cannot be issued yet, or None if it is ready.

    Used to hold back the failures in a batch. A draft that never becomes a
    gate pass never takes a number, which is what keeps the run continuous when
    some of the PDFs in a batch could not be read.
    """
    # A gate pass uploaded in place of an invoice fails every field check, and
    # reporting the first of them sends the operator off to type in a customer
    # name that was never going to be on the page. Say what actually happened.
    if invoice_parser.GATE_PASS_UPLOADED_NOTE in (draft.get("parse_notes") or ""):
        return invoice_parser.GATE_PASS_UPLOADED_NOTE

    if not draft["supplier_name"].strip():
        return "no supplier — could not be read from the PDF"
    if not draft["customer_name"].strip():
        return "no customer — could not be read from the PDF"
    if not draft["invoice_no"].strip():
        return "no invoice number — could not be read from the PDF"
    if not draft["invoice_date"].strip():
        return "no invoice date — could not be read from the PDF"

    items = [i for i in draft["items"] if str(i["item_name"]).strip()]
    if not items:
        return "no items — the PDF could not be read, type them in"
    if any(not str(i["quantity"]).strip() for i in items):
        return "an item has no quantity"
    return None


def create_gate_passes_batch(conn, draft_ids, prepared_by="", prepared_by_user_id=None):
    """Issue many drafts at once, as one all-or-nothing transaction.

    Every pass in the batch gets the next number in an unbroken block. Because
    the whole batch is a single BEGIN IMMEDIATE transaction:

      * another person issuing at the same moment waits, then continues from the
        end of our block — the two batches never interleave or collide;
      * if any one draft fails, the entire batch rolls back and NO numbers are
        consumed, so the run is never left with a hole in the middle.

    Drafts that are not ready are rejected up front, before the transaction
    opens, so one unreadable PDF does not stop the other 49 from being issued.
    Returns (issued, skipped) where skipped is [(draft_id, reason)].
    """
    ready, skipped = [], []
    for draft_id in draft_ids:
        draft = get_draft(conn, draft_id)
        if draft is None:
            skipped.append((draft_id, "draft no longer exists"))
            continue
        problem = draft_problem(draft)
        if problem:
            skipped.append((draft_id, problem))
        else:
            ready.append(draft)

    if not ready:
        return [], skipped

    scope = current_prefix(conn)
    issued_ids = []
    with writing(conn):
        seq = next_seq(conn, scope)
        for draft in ready:
            items = [i for i in draft["items"] if str(i["item_name"]).strip()]
            gate_pass_id = _insert_gate_pass(
                conn, scope, seq, draft["supplier_name"], draft["customer_name"],
                draft["invoice_no"], draft["invoice_date"], draft["vehicle_no"],
                # No PDF path: the upload is deleted once the pass exists, and a
                # path to a file that is gone is worse than no path at all.
                items, None, prepared_by, prepared_by_user_id,
                source=f"batch, draft {draft['id']}",
                # Whatever was typed on the review screen travels with the draft.
                remarks=draft.get("remarks", "") or "",
            )
            issued_ids.append(gate_pass_id)
            conn.execute("DELETE FROM draft_items WHERE draft_id = ?", (draft["id"],))
            conn.execute("DELETE FROM drafts WHERE id = ?", (draft["id"],))
            seq += 1

    return [get_gate_pass(conn, i) for i in issued_ids], skipped


def _insert_gate_pass(conn, prefix, seq, supplier_name, customer_name, invoice_no,
                       invoice_date, vehicle_no, items, invoice_pdf_path,
                       prepared_by, prepared_by_user_id, source, remarks=""):
    """One pass at an already-allocated number. Caller holds the write lock."""
    serial_no = serial_for(prefix, seq)
    # The COLUMN gets the canonical run, not the prefix as spelled. Storing
    # "FZ27-" here while next_seq looked the run up as "FZ27" made MAX() find
    # nothing, so every pass was allocated number 1 and the second one issued
    # died on the UNIQUE constraint. The serial STRING keeps the separator; the
    # scope is what identifies the run.
    scope = run_scope(prefix)
    # issued_at is set here rather than left to the column default. Changing a
    # DEFAULT in schema.sql only affects databases created afterwards — an
    # existing table keeps the default it was made with — so relying on it left
    # older installs still recording UTC. _now() is local time, always.
    cur = conn.execute(
        """INSERT INTO gate_passes (serial_no, serial_scope, serial_seq, supplier_name,
                                     customer_name, invoice_no, invoice_date, vehicle_no,
                                     invoice_pdf_path, prepared_by, prepared_by_user_id,
                                     remarks, issued_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (serial_no, scope, seq, supplier_name, customer_name, invoice_no,
         invoice_date, vehicle_no, invoice_pdf_path, prepared_by, prepared_by_user_id,
         remarks, _now()),
    )
    gate_pass_id = cur.lastrowid
    for i, item in enumerate(items, start=1):
        conn.execute(
            """INSERT INTO gate_pass_items (gate_pass_id, sl_no, item_name, quantity, cartons)
               VALUES (?, ?, ?, ?, ?)""",
            (gate_pass_id, i, item["item_name"], item["quantity"],
             str(item.get("cartons", "") or "")),
        )
    conn.execute(
        "INSERT INTO audit_log (gate_pass_id, action, details) VALUES (?, ?, ?)",
        (gate_pass_id, "issue",
         f"issued as {serial_no} by {prepared_by or 'unknown'} from {source}"),
    )
    return gate_pass_id


def gate_pass_from_draft(conn, draft_id):
    """The pass a draft became, or None if it was discarded instead.

    A draft is deleted by the transaction that issues it, so /review/<id> is a
    dead link the moment the pass exists — which is exactly where the browser's
    Back button lands after issuing. The audit row written at issue ends with
    "from draft 51" (or "from batch, draft 51"), so the link survives even
    though the draft row does not.

    The LIKE has no trailing wildcard on purpose: the id is the last thing in
    the string, so 'draft 5' cannot match a row written for draft 51.
    """
    row = conn.execute(
        "SELECT gate_pass_id FROM audit_log "
        "WHERE action = 'issue' AND details LIKE '%draft ' || ? "
        "ORDER BY id DESC LIMIT 1",
        (str(draft_id),),
    ).fetchone()
    return row["gate_pass_id"] if row else None


def get_gate_pass(conn, gate_pass_id):
    row = conn.execute("SELECT * FROM gate_passes WHERE id = ?", (gate_pass_id,)).fetchone()
    if row is None:
        return None
    return _gate_pass_with_items(conn, row)


def get_gate_pass_by_serial(conn, serial_no):
    row = conn.execute("SELECT * FROM gate_passes WHERE serial_no = ?", (serial_no,)).fetchone()
    if row is None:
        return None
    return _gate_pass_with_items(conn, row)


def _gate_pass_with_items(conn, row):
    items = conn.execute(
        "SELECT * FROM gate_pass_items WHERE gate_pass_id = ? ORDER BY sl_no", (row["id"],)
    ).fetchall()
    d = dict(row)
    d["items"] = [dict(item) for item in items]
    d["total_qty"] = _sum_numeric(item["quantity"] for item in items)
    d["total_cartons"] = _sum_numeric(item["cartons"] for item in items)
    return d


def _sum_numeric(values):
    total = 0
    saw_number = False
    for v in values:
        try:
            total += float(v)
            saw_number = True
        except (TypeError, ValueError):
            continue
    if not saw_number:
        return ""
    return int(total) if total == int(total) else total


REGISTER_PAGE_SIZE = 200

# How many rows the reports page shows as a preview. It is there to confirm the
# filters caught the right passes before exporting, not to be read end to end —
# the export itself is never capped.
REPORT_PREVIEW_LIMIT = 50


def _register_filters(status=None, search=None, date_from=None, date_to=None,
                       supplier=None, customer=None):
    """The WHERE clause shared by the register, its count and the exports, so
    they can never disagree about what a filter means.

    Dates filter on issued_at — when the pass was actually generated, which is
    what an audit asks about. issued_at is stored as 'YYYY-MM-DD HH:MM:SS', so a
    plain string comparison is both correct and able to use the index; date_to
    is inclusive of the whole day.
    """
    clauses, params = [], []
    if status:
        clauses.append("status = ?")
        params.append(status)
    if search:
        clauses.append(
            "(serial_no LIKE ? OR invoice_no LIKE ? OR customer_name LIKE ? OR supplier_name LIKE ?)"
        )
        like = f"%{search}%"
        params.extend([like, like, like, like])
    if supplier:
        clauses.append("supplier_name LIKE ?")
        params.append(f"%{supplier}%")
    if customer:
        clauses.append("customer_name LIKE ?")
        params.append(f"%{customer}%")
    if date_from:
        clauses.append("issued_at >= ?")
        params.append(f"{date_from} 00:00:00")
    if date_to:
        clauses.append("issued_at <= ?")
        params.append(f"{date_to} 23:59:59")
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    return where, params


def distinct_values(conn, column, limit=200):
    """The suppliers/customers actually in the book, for the report filters."""
    if column not in ("supplier_name", "customer_name"):
        raise ValueError("not a filterable column")
    rows = conn.execute(
        f"SELECT DISTINCT {column} AS v FROM gate_passes "
        f"WHERE {column} != '' ORDER BY {column} COLLATE NOCASE LIMIT ?", (limit,))
    return [r["v"] for r in rows]


def list_gate_passes(conn, status=None, search=None, limit=REGISTER_PAGE_SIZE,
                      date_from=None, date_to=None, supplier=None, customer=None):
    """The register, newest first.

    Capped by default. The register is a browsing screen, and nobody reads past
    the first screenful — without a limit this builds every row in the book into
    memory on every page view, which is the one query that genuinely falls over
    as the book grows. Pass limit=None to walk the whole book (exports, totals).

    A search is a "contains" match, which no B-tree index can serve — SQLite has
    to read the table. That is fine at this size (tens of milliseconds over
    100k rows) and keeps the box behaving the way people expect. If it ever
    stops feeling instant, the answer is an FTS5 table, not more indexes.
    """
    where, params = _register_filters(status, search, date_from, date_to,
                                       supplier, customer)
    query = f"SELECT * FROM gate_passes{where} ORDER BY id DESC"
    if limit is not None:
        query += " LIMIT ?"
        params = params + [limit]
    return [dict(r) for r in conn.execute(query, params)]


def count_gate_passes(conn, status=None, search=None, date_from=None, date_to=None,
                       supplier=None, customer=None):
    """How many match, so the register can say when it is showing only the top."""
    where, params = _register_filters(status, search, date_from, date_to,
                                       supplier, customer)
    return conn.execute(f"SELECT COUNT(*) AS n FROM gate_passes{where}", params).fetchone()["n"]


EXPORT_SUMMARY_COLUMNS = [
    "Gate Pass No", "Issued On", "Status", "Prepared By", "From", "Customer",
    "Invoice No", "Invoice Date", "Vehicle No", "Items", "Total Quantity",
    "Total Cartons", "Cancelled On", "Cancelled By", "Cancel Reason",
]

EXPORT_DETAIL_COLUMNS = [
    "Gate Pass No", "Issued On", "Status", "Prepared By", "From", "Customer",
    "Invoice No", "Invoice Date", "Vehicle No", "Sl No", "Item", "Quantity",
    "No. of Cartons",
]


def export_rows(conn, detailed=False, **filters):
    """Yields a header row then one row per pass (or per item, if detailed).

    A generator that streams in id order rather than loading everything — an
    export covering a whole year must not build the year in memory. Items come
    through the (gate_pass_id, sl_no) index.
    """
    yield EXPORT_DETAIL_COLUMNS if detailed else EXPORT_SUMMARY_COLUMNS

    where, params = _register_filters(**filters)
    cursor = conn.execute(f"SELECT * FROM gate_passes{where} ORDER BY id", params)
    for row in cursor:
        gp = dict(row)
        items = [dict(i) for i in conn.execute(
            "SELECT * FROM gate_pass_items WHERE gate_pass_id = ? ORDER BY sl_no",
            (gp["id"],))]
        common = [gp["serial_no"], gp["issued_at"], gp["status"], gp["prepared_by"],
                   gp["supplier_name"], gp["customer_name"], gp["invoice_no"],
                   gp["invoice_date"], gp["vehicle_no"]]
        if detailed:
            if not items:
                yield common + ["", "", "", ""]
            for item in items:
                yield common + [item["sl_no"], item["item_name"],
                                 item["quantity"], item["cartons"]]
        else:
            yield common + [
                len(items),
                _sum_numeric(i["quantity"] for i in items),
                _sum_numeric(i["cartons"] for i in items),
                gp["cancelled_at"] or "", gp["cancelled_by"] or "",
                gp["cancel_reason"] or "",
            ]


def close(conn):
    """Let SQLite refresh its query-planner statistics as it goes.

    PRAGMA optimize is a no-op almost every time; occasionally it runs a quick
    ANALYZE so the planner keeps choosing the right index as the table grows.
    """
    try:
        conn.execute("PRAGMA optimize")
    except sqlite3.Error:
        pass
    conn.close()


# What may be corrected after issue. Deliberately not the serial, the sequence,
# the preparer or the issue date: the first three the database refuses outright
# (trg_gate_passes_no_serial_edit), and the fourth is when the goods left, which
# is a fact about the world rather than a field.
EDITABLE_FIELDS = ("supplier_name", "customer_name", "invoice_no",
                   "invoice_date", "vehicle_no", "remarks")


def update_gate_pass(conn, gate_pass_id, items=None, edited_by="", **fields):
    """Correct an already-issued pass, and record that it was corrected.

    A gate pass is the record of goods that have already left the building, so
    changing one is not an ordinary edit. Two things make it defensible rather
    than reckless:

      * the serial, the sequence, the preparer and the issue date do not move,
        so the pass is still the same pass and still says who issued it and
        when;
      * every field that changes is written to the audit log with its old value
        beside the new one. The value is gone from the row; what it used to be
        is not gone from the book.

    Returns the list of human-readable changes, empty if nothing differed.
    """
    existing = get_gate_pass(conn, gate_pass_id)
    if existing is None:
        raise ValueError("that gate pass does not exist")
    if existing["status"] != "issued":
        # A cancelled pass is a closed record. Correcting one would mean
        # rewriting something that has already been struck out, and the paper
        # in the drawer would no longer match the book.
        raise ValueError("a cancelled gate pass cannot be edited")

    changes = []
    updates = {}
    for name in EDITABLE_FIELDS:
        if name not in fields:
            continue
        new = (fields[name] or "").strip()
        old = (existing[name] or "").strip()
        if new != old:
            updates[name] = new
            changes.append(f"{name}: {old or '(blank)'} -> {new or '(blank)'}")

    if items is not None:
        cleaned = [i for i in items if str(i.get("item_name", "")).strip()]
        if not cleaned:
            raise ValueError("a gate pass needs at least one item")
        before = [(i["item_name"], i["quantity"], i["cartons"]) for i in existing["items"]]
        after = [(str(i.get("item_name", "")).strip(), str(i.get("quantity", "")).strip(),
                  str(i.get("cartons", "")).strip()) for i in cleaned]
        if before != after:
            if len(before) != len(after):
                changes.append(f"items: {len(before)} line(s) -> {len(after)} line(s)")
            for old_row, new_row in zip(before, after):
                if old_row != new_row:
                    changes.append(
                        f"{old_row[0]}: qty {old_row[1]}->{new_row[1]}, "
                        f"cartons {old_row[2] or '(blank)'}->{new_row[2] or '(blank)'}")
            for extra in after[len(before):]:
                changes.append(f"added {extra[0]} qty {extra[1]}")
            for gone in before[len(after):]:
                changes.append(f"removed {gone[0]} qty {gone[1]}")
    else:
        cleaned = None

    if not changes:
        return []

    with writing(conn):
        if updates:
            columns = ", ".join(f"{name} = ?" for name in updates)
            conn.execute(f"UPDATE gate_passes SET {columns} WHERE id = ?",
                         (*updates.values(), gate_pass_id))
        if cleaned is not None:
            conn.execute("DELETE FROM gate_pass_items WHERE gate_pass_id = ?", (gate_pass_id,))
            for position, item in enumerate(cleaned, start=1):
                conn.execute(
                    "INSERT INTO gate_pass_items (gate_pass_id, sl_no, item_name, quantity, cartons) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (gate_pass_id, position, str(item.get("item_name", "")).strip(),
                     str(item.get("quantity", "")).strip(), str(item.get("cartons", "")).strip()))
        conn.execute(
            "INSERT INTO audit_log (gate_pass_id, action, details, created_at) "
            "VALUES (?, 'edit', ?, ?)",
            (gate_pass_id,
             f"edited by {edited_by or 'unknown'}: " + "; ".join(changes),
             _now()))
    return changes


def cancel_gate_pass(conn, gate_pass_id, reason, cancelled_by=""):
    with writing(conn):
        conn.execute(
            """UPDATE gate_passes SET status = 'cancelled', cancelled_at = ?, cancel_reason = ?,
                                       cancelled_by = ?
               WHERE id = ?""",
            (_now(), reason, cancelled_by, gate_pass_id),
        )
        conn.execute(
            "INSERT INTO audit_log (gate_pass_id, action, details) VALUES (?, 'cancel', ?)",
            (gate_pass_id, f"{reason or ''} (by {cancelled_by or 'unknown'})"),
        )


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

def get_settings(conn):
    rows = conn.execute("SELECT key, value FROM settings").fetchall()
    settings = dict(DEFAULT_SETTINGS)
    settings.update({r["key"]: r["value"] for r in rows})
    return settings


def update_settings(conn, **kv):
    with writing(conn):
        for key, value in kv.items():
            conn.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, str(value)),
            )
