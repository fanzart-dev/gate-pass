-- Fanzart Gate Pass database schema.
--
-- Drafts are freely mutable/deletable scratch space for an invoice upload
-- that hasn't been issued yet. Nothing here inserts into gate_passes, so an
-- abandoned or unreadable draft never burns a gate pass number.
--
-- gate_passes rows are only ever created by db.create_gate_pass() /
-- create_gate_passes_batch(), which derive the next serial from
-- MAX(serial_seq) and INSERT the row in the same BEGIN IMMEDIATE transaction. The triggers below make serial_no/serial_seq/
-- serial_scope immutable and gate_passes/audit_log append-only, independent
-- of anything the app code does.

-- Timestamps are stored in LOCAL time, not UTC. SQLite's datetime('now')
-- returns UTC, which put every issued_at 5.5 hours behind the office clock in
-- India — a pass issued at 02:37 on the 5th was filed as 21:07 on the 4th, and
-- an audit report for "today" missed everything issued after 18:30. The
-- 'localtime' modifier is what keeps the book agreeing with the wall clock and
-- with the dates the reports page filters on.
PRAGMA foreign_keys = ON;

-- Gate passes are issued by several people at the office. Everyone draws from
-- the same serial run, so the book stays one continuous sequence no matter who
-- is at the keyboard.
-- status is the single source of truth for whether someone may sign in:
--   pending  — signed up, waiting for an admin to approve them (cannot sign in)
--   approved — may sign in and issue passes
--   rejected — an admin turned the request down
--   disabled — was approved, since switched off (e.g. they left)
-- Accounts are never deleted: a gate pass keeps the name of whoever prepared it.
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE COLLATE NOCASE,
    display_name TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'approved', 'rejected', 'disabled')),
    -- Kept in step with permissions.can_manage_people, so one value drives the
    -- navigation, the lockout guards and the older is_admin checks.
    is_admin INTEGER NOT NULL DEFAULT 0,
    -- Per-feature permissions as a JSON object; see db.PERMISSIONS.
    permissions TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    decided_at TEXT,
    decided_by TEXT
);

CREATE TABLE IF NOT EXISTS drafts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    supplier_name TEXT NOT NULL DEFAULT '',
    customer_name TEXT NOT NULL DEFAULT '',
    invoice_no TEXT NOT NULL DEFAULT '',
    invoice_date TEXT NOT NULL DEFAULT '',
    vehicle_no TEXT NOT NULL DEFAULT '',
    -- Typed on the review screen and printed in the Remarks box. Unlike cartons
    -- (rule 5) a remark CAN be known at the keyboard: it is a note about the
    -- consignment, not a count of how it was packed. The printed box is still
    -- empty when nobody typed anything, so it can be written on by hand.
    remarks TEXT NOT NULL DEFAULT '',
    invoice_pdf_path TEXT,
    parse_notes TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

CREATE TABLE IF NOT EXISTS draft_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    draft_id INTEGER NOT NULL REFERENCES drafts(id) ON DELETE CASCADE,
    sl_no INTEGER NOT NULL,
    item_name TEXT NOT NULL DEFAULT '',
    quantity TEXT NOT NULL DEFAULT '',
    cartons TEXT NOT NULL DEFAULT ''
);

-- The old separate counter table. Numbers are now derived from the book itself
-- (MAX(serial_seq) per run, see db.next_seq) so the sequence can never drift out
-- of step with the passes that were actually issued. Nothing reads this table
-- any more; it is dropped on upgrade because a stale second copy of the
-- sequence is a trap for whoever reads this next.
DROP TABLE IF EXISTS serial_counters;

CREATE TABLE IF NOT EXISTS gate_passes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    serial_no TEXT NOT NULL UNIQUE,
    serial_scope TEXT NOT NULL,
    serial_seq INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'issued' CHECK (status IN ('issued', 'cancelled')),
    supplier_name TEXT NOT NULL DEFAULT '',
    customer_name TEXT NOT NULL DEFAULT '',
    invoice_no TEXT NOT NULL DEFAULT '',
    invoice_date TEXT NOT NULL DEFAULT '',
    vehicle_no TEXT NOT NULL DEFAULT '',
    -- Typed on the review screen and printed in the Remarks box. Unlike cartons
    -- (rule 5) a remark CAN be known at the keyboard: it is a note about the
    -- consignment, not a count of how it was packed. The printed box is still
    -- empty when nobody typed anything, so it can be written on by hand.
    remarks TEXT NOT NULL DEFAULT '',
    invoice_pdf_path TEXT,
    -- Who generated it. The display name is snapshotted onto the row so the
    -- printed pass still names them if the account is later renamed or removed.
    prepared_by TEXT NOT NULL DEFAULT '',
    prepared_by_user_id INTEGER REFERENCES users(id),
    issued_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    cancelled_at TEXT,
    cancel_reason TEXT,
    cancelled_by TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_gate_passes_scope_seq
    ON gate_passes(serial_scope, serial_seq);

-- Indexes for the queries the app actually runs. serial_no needs none of its
-- own: its UNIQUE constraint already creates one, which is what makes looking a
-- pass up by its number instant.
--
-- The register lists newest first, optionally filtered by status. Putting id
-- DESC in the index lets SQLite walk it in order instead of sorting the table.
CREATE INDEX IF NOT EXISTS idx_gate_passes_status_id
    ON gate_passes(status, id DESC);
CREATE INDEX IF NOT EXISTS idx_gate_passes_issued_at
    ON gate_passes(issued_at DESC);
CREATE INDEX IF NOT EXISTS idx_gate_passes_invoice_no
    ON gate_passes(invoice_no);
CREATE INDEX IF NOT EXISTS idx_gate_passes_invoice_date
    ON gate_passes(invoice_date);
-- Plain collation on purpose. An index declared COLLATE NOCASE cannot serve a
-- plain `WHERE customer_name = ?`, because the column's own collation is BINARY
-- and the two must agree — an earlier NOCASE version of these two indexes was
-- silently never used. Confirmed with EXPLAIN QUERY PLAN in tests/benchmark.py.
DROP INDEX IF EXISTS idx_gate_passes_customer;
DROP INDEX IF EXISTS idx_gate_passes_supplier;
CREATE INDEX IF NOT EXISTS idx_gate_passes_customer_name
    ON gate_passes(customer_name);
CREATE INDEX IF NOT EXISTS idx_gate_passes_supplier_name
    ON gate_passes(supplier_name);
CREATE INDEX IF NOT EXISTS idx_gate_passes_prepared_by
    ON gate_passes(prepared_by_user_id);

CREATE TABLE IF NOT EXISTS gate_pass_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    gate_pass_id INTEGER NOT NULL REFERENCES gate_passes(id),
    sl_no INTEGER NOT NULL,
    item_name TEXT NOT NULL DEFAULT '',
    quantity TEXT NOT NULL DEFAULT '',
    cartons TEXT NOT NULL DEFAULT ''
);

-- Printing a pass reads its items. Without this index that is a full scan of
-- every item row ever written, which is what makes prints crawl as the book
-- grows. Same story for a draft's items and a pass's audit trail.
CREATE INDEX IF NOT EXISTS idx_gate_pass_items_pass
    ON gate_pass_items(gate_pass_id, sl_no);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    gate_pass_id INTEGER REFERENCES gate_passes(id),
    action TEXT NOT NULL,
    details TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

CREATE INDEX IF NOT EXISTS idx_audit_log_pass ON audit_log(gate_pass_id);
CREATE INDEX IF NOT EXISTS idx_draft_items_draft ON draft_items(draft_id, sl_no);
CREATE INDEX IF NOT EXISTS idx_drafts_created ON drafts(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_users_status ON users(status);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Rule 1: the serial is immutable once allocated. Nothing may UPDATE these
-- columns; create_gate_pass() only ever sets them via INSERT. prepared_by is
-- covered too — who issued a pass is a fact about the record, not a setting.
CREATE TRIGGER IF NOT EXISTS trg_gate_passes_no_serial_edit
BEFORE UPDATE OF serial_no, serial_seq, serial_scope, prepared_by, prepared_by_user_id
ON gate_passes
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'serial number and preparer are immutable');
END;

-- Rule 2: gate passes are never deleted. Cancelling is an UPDATE of status.
CREATE TRIGGER IF NOT EXISTS trg_gate_passes_no_delete
BEFORE DELETE ON gate_passes
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'gate passes cannot be deleted, cancel instead');
END;

-- Rule 3: the audit log is append-only.
CREATE TRIGGER IF NOT EXISTS trg_audit_log_no_update
BEFORE UPDATE ON audit_log
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'audit log is append-only');
END;

CREATE TRIGGER IF NOT EXISTS trg_audit_log_no_delete
BEFORE DELETE ON audit_log
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'audit log is append-only');
END;

-- Failed sign-in attempts, so a password cannot be guessed indefinitely.
--
-- Only needed once the site is reachable from the public internet: a Funnel
-- hostname appears in Certificate Transparency logs the moment its certificate
-- is issued, so scanners find it within hours and start trying passwords. On a
-- LAN this table stays empty.
--
-- Deliberately NOT the audit log. The audit log is append-only and permanent
-- because it is the register's evidence; this is throwaway operational noise
-- that gets pruned, and mixing the two would either bloat the evidence or put
-- a delete trigger where one must never be.
CREATE TABLE IF NOT EXISTS login_attempts (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL,
    ip       TEXT NOT NULL,
    at       TEXT NOT NULL
);

-- Both lookups are "how many in the last N minutes", so time leads each index.
CREATE INDEX IF NOT EXISTS idx_login_attempts_user ON login_attempts(username, at);
CREATE INDEX IF NOT EXISTS idx_login_attempts_ip   ON login_attempts(ip, at);

-- The master carton list: how many boxes one unit of each item ships in.
--
-- Seeded from the company's "Box Qty" sheet via manage_cartons.py. Kept in the
-- database rather than read from the spreadsheet at request time so that the
-- office can correct a wrong count without editing a file on the server, and so
-- the nightly backup carries it.
--
-- `normalized` is the match key, not `item_name`: invoices spell the same fan
-- with different spacing and different kinds of dash, and the raw name is kept
-- only so a human can see what the master sheet actually said.
CREATE TABLE IF NOT EXISTS item_carton_mappings (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    item_name  TEXT NOT NULL,
    normalized TEXT NOT NULL UNIQUE,
    cartons    INTEGER NOT NULL,
    updated_at TEXT NOT NULL
);
