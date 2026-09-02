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


def print_row_height_mm(rows):
    """The exact height of one item row, so the box fills without stretching.

    Rounded DOWN to two decimals: a fraction of a millimetre of slack at the
    bottom of the box is invisible, whereas rounding up would push the last row
    past the edge of the sheet.
    """
    rows = max(1, rows)
    return int(ITEM_BODY_MM / rows * 100) / 100


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
    # Serials are <prefix>-<5 digits>, e.g. FZ-00001. The prefix identifies the
    # run; starting a new run takes a new prefix so a number is never repeated.
    "serial_prefix": "FZ",
    "paper_mode": "a4x2",  # "a4x2" (two A5 passes on A4 landscape) or "a5" (one per sheet)
    "show_totals": "0",
    "show_vehicle": "0",
    "company_name": "fanzart",
}


# Bump when schema.sql or ADDED_COLUMNS changes. Stored in the file as
# PRAGMA user_version, so a connection can tell in one cheap read whether the
# schema script needs running at all.
SCHEMA_VERSION = 12

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


def _migrate_data(conn, previous_version):
    """One-off data corrections, keyed to the version the file was last at.

    `previous_version` is 0 for a database that did not exist yet, which is why
    these are skipped on a fresh install — there is nothing to correct.
    """
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


PREFIX_RE = re.compile(r"[A-Za-z0-9]{1,10}")


def serial_for(prefix, seq):
    return f"{prefix}-{seq:05d}"


def validate_prefix(prefix):
    """A prefix is the part before the dash, e.g. FZ in FZ-00001."""
    prefix = (prefix or "").strip().upper()
    if not PREFIX_RE.fullmatch(prefix):
        raise ValueError(
            "the prefix must be 1 to 10 letters or digits, with no spaces or symbols")
    return prefix


def current_prefix(conn):
    return get_settings(conn)["serial_prefix"]


def prefix_in_use(conn, prefix):
    """Whether any gate pass was ever issued under this prefix.

    Reusing one would restart the count at 00001 and hand out a number that
    already exists on a printed pass, so a new run must take a fresh prefix.
    """
    row = conn.execute(
        "SELECT 1 FROM gate_passes WHERE serial_scope = ? LIMIT 1", (prefix,)
    ).fetchone()
    return row is not None


def next_seq(conn, scope):
    """The next number in a run, read from the gate passes themselves.

    Deriving it from MAX(serial_seq) rather than a separate counter means the
    sequence can never drift out of step with the book: the last number issued
    IS the last row. Cancelled passes keep their number and still count, so a
    cancellation never causes the next pass to reuse it.

    Only correct when called inside writing() — BEGIN IMMEDIATE holds the write
    lock, so no other connection can insert between this read and our insert.
    The (serial_scope, serial_seq) index makes it a single index lookup, not a
    scan, however large the book gets.
    """
    row = conn.execute(
        "SELECT MAX(serial_seq) AS highest FROM gate_passes WHERE serial_scope = ?",
        (scope,),
    ).fetchone()
    return (row["highest"] or 0) + 1


def next_serial_preview(conn):
    """What the next gate pass will be numbered, without allocating anything."""
    prefix = current_prefix(conn)
    return serial_for(prefix, next_seq(conn, prefix))


def start_new_run(conn, prefix, started_by=""):
    """Begin numbering again from 00001 under a new prefix.

    Nothing is deleted: every pass issued under the previous prefix keeps its
    number and stays in the register. Only the counter for the new prefix starts
    at one.
    """
    prefix = validate_prefix(prefix)
    if prefix_in_use(conn, prefix):
        raise ValueError(
            f"gate passes have already been issued as {prefix}-00001 and up — "
            "pick a prefix that has not been used, so no number is ever repeated")
    if prefix == current_prefix(conn):
        raise ValueError(f"the current run already uses {prefix}")

    previous = current_prefix(conn)
    with writing(conn):
        update_settings(conn, serial_prefix=prefix)
        conn.execute(
            "INSERT INTO audit_log (action, details) VALUES ('new_serial_run', ?)",
            (f"numbering restarted at {serial_for(prefix, 1)} (was {previous}) "
             f"by {started_by or 'unknown'}",),
        )
    return prefix


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


def _insert_gate_pass(conn, scope, seq, supplier_name, customer_name, invoice_no,
                       invoice_date, vehicle_no, items, invoice_pdf_path,
                       prepared_by, prepared_by_user_id, source, remarks=""):
    """One pass at an already-allocated number. Caller holds the write lock."""
    serial_no = serial_for(scope, seq)
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
