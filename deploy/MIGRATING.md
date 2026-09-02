# Taking the book somewhere else

Exports come from `deploy/export-db.sh`. It writes a timestamped folder with a
binary snapshot, a `.sql` dump, one CSV per table, and a `MANIFEST.txt` giving
row counts, a checksum for every file, and two integrity answers an auditor
will ask for: whether any item row has lost its gate pass, and whether the
serial run has gaps.

```bash
sudo -u gatepass /opt/gate-pass/deploy/export-db.sh
```

## Read this before planning a migration

**SQLite is not the weak link here, and moving off it is not an upgrade.**
Measured on this app at 100,000 gate passes: the register opens in 1.15 ms, a
pass prints in 0.03 ms, and four staff issuing 25 passes each simultaneously
produced zero lock errors, zero duplicate numbers and no gaps in the run. The
office issues a few dozen passes a day. At that size the database is not what
will ever be slow.

What you would actually gain by moving to PostgreSQL is **concurrent writers
across more than one machine** — which matters only if you run the app on
several servers at once. What you would lose is real:

- **The immutability triggers move with the data but not automatically.** A
  gate pass cannot be deleted and a serial cannot be edited because
  `schema.sql` says so in SQLite's dialect. Ported carelessly, the CSVs arrive
  in a database where nothing stops an `UPDATE` on a serial number, and the
  guarantee the whole app exists to provide is quietly gone.
- **`BEGIN IMMEDIATE` is how a number is never issued twice.** The allocation
  reads `MAX(serial_seq)` and inserts inside one transaction holding the write
  lock. PostgreSQL needs `SELECT ... FOR UPDATE` or a sequence to get the same
  guarantee; the code as written would allow two people to take the same
  number.
- **One file is easy to back up, copy and prove.** `deploy/backup.sh` takes a
  verified snapshot in under a second with no downtime.

So: migrate when you have a reason — several servers, or an IT policy that
requires it — not because SQLite sounds small.

## Moving to another machine, still on SQLite

The easy case, and usually the right one.

1. Stop the service on the old machine: `sudo systemctl stop gate-pass`.
   Passes issued after the export do not come with you.
2. `sudo -u gatepass /opt/gate-pass/deploy/export-db.sh`
3. Copy the folder to the new machine.
4. Install the app there: `sudo /opt/gate-pass/deploy/install.sh`
5. Put `gate_pass.db` from the export at `/opt/gate-pass/storage/gate_pass.db`,
   `chown gatepass:gatepass` it, and delete any `-wal`/`-shm` beside it.
6. Bring `storage/secret_key` too if you want to avoid signing everyone out.
   Passwords work either way; sessions do not survive a new key.
7. Start it, then **check before letting anyone in**: the pass count and the
   next serial must match `MANIFEST.txt` exactly.

## Moving to AWS

### The shape of it

| | |
|---|---|
| **EC2, still SQLite** | The app as it is, on a cloud machine. Nothing changes but the address. |
| **EC2 + RDS PostgreSQL** | Needs the code changes below. Only worth it for several app servers. |

An EC2 instance running the existing install is a lift-and-shift: follow
"Moving to another machine" above, then put the app behind a real domain and
`certbot` for genuine TLS instead of the local CA.

Two things change once it is on the internet rather than the office LAN:

- **There is still no rate limit on `/login`.** Acceptable on a closed network,
  not acceptable facing the internet. Fix that first.
- **Set `GATE_PASS_HTTPS=1`** once the certificate is live, so the session
  cookie is HTTPS-only.

### If you do move to RDS PostgreSQL

The CSVs are what you load — **not** `gate_pass.sql`, which is SQLite's
dialect and will not run.

```bash
# On the RDS instance, create the schema BY HAND first, translated from
# schema.sql: AUTOINCREMENT -> BIGSERIAL, TEXT stays TEXT, and the CHECK
# constraints carry over unchanged.
psql -h your-db.rds.amazonaws.com -U gatepass -d gatepass -f schema-postgres.sql

# Then load each table, parents before children (gate_pass_items references
# gate_passes, audit_log references gate_passes).
for t in users settings gate_passes gate_pass_items audit_log drafts draft_items; do
  psql -h your-db.rds.amazonaws.com -U gatepass -d gatepass \
       -c "\copy $t FROM 'csv/$t.csv' WITH CSV HEADER"
done

# Sequences do not know about the rows you just inserted.
psql ... -c "SELECT setval('gate_passes_id_seq', (SELECT MAX(id) FROM gate_passes));"
```

**Then do these four things, or the app is no longer the app:**

1. **Recreate the triggers.** The four in `schema.sql` — no delete, no serial
   edit, no audit update, no audit delete — become PostgreSQL trigger functions
   that `RAISE EXCEPTION`. Without them the register is editable and every
   guarantee in "Rules that must not be broken" is gone.
2. **Rewrite serial allocation.** `db.next_seq()` plus `BEGIN IMMEDIATE` must
   become `SELECT ... FOR UPDATE` or a database sequence. Two people issuing at
   the same moment is the case that matters, and it is not hypothetical — the
   benchmark does it deliberately.
3. **Replace the connection layer.** `db.connect()` and its pragmas are
   SQLite-specific; every query in `db.py` is plain SQL and mostly portable,
   but `datetime('now','localtime')` and `json_set`/`json_extract` are not.
4. **Run the suite against it.** `python3 tests/test_flow.py` is 871 checks and
   is the definition of "still working". Do not migrate anything you cannot
   then run it against.

### Verifying a migration

Whatever the target, the arithmetic must come out identical:

```bash
# Row counts, from MANIFEST.txt
gate_passes  53      gate_pass_items  364      users  2      audit_log  60

# And on the new database:
SELECT COUNT(*) FROM gate_passes;                    -- must match exactly
SELECT MIN(serial_no), MAX(serial_no) FROM gate_passes;
SELECT COUNT(*) FROM gate_pass_items i
  LEFT JOIN gate_passes p ON p.id = i.gate_pass_id WHERE p.id IS NULL;   -- 0
```

A missing pass is a document that left the building with no record. Check the
counts before anyone issues anything new, because once the new book starts
taking numbers, reconciling it against the old one gets much harder.
