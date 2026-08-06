#!/usr/bin/env python3
"""Add and manage the people who can issue gate passes.

Run on the office server, alongside app.py:

    python3 manage_users.py add                # prompts for everything
    python3 manage_users.py list
    python3 manage_users.py approve <username>
    python3 manage_users.py passwd <username>
    python3 manage_users.py admin <username>   # make an admin
    python3 manage_users.py unadmin <username>
    python3 manage_users.py disable <username>
    python3 manage_users.py enable <username>

Normally people request an account on the sign-in page and an admin approves
them in the app. These commands are the way back in if every admin is locked
out, and the way to create the very first account without touching a browser.

Passwords are typed at the prompt and never appear in the command line or the
shell history. They are stored only as a salted hash.

Accounts are never deleted — a gate pass keeps the name of whoever prepared it,
so someone who has left is disabled rather than removed.
"""

import getpass
import sys
from pathlib import Path

import db

DB_PATH = Path(__file__).parent / "storage" / "gate_pass.db"


def _conn():
    return db.connect(DB_PATH)


def cmd_add(args):
    conn = _conn()
    username = (args[0] if args else input("Username (for signing in): ")).strip()
    if not username:
        return fail("username is required")
    if db.get_user_by_username(conn, username):
        return fail(f"user {username!r} already exists")

    display_name = input("Full name (printed as 'Prepared by'): ").strip()
    if not display_name:
        return fail("full name is required — it is printed on every pass")

    password = getpass.getpass("Password: ")
    if len(password) < 8:
        return fail("password must be at least 8 characters")
    if password != getpass.getpass("Repeat password: "):
        return fail("passwords do not match")

    first = db.count_users(conn) == 0
    make_admin = first or input("Make this person an admin? [y/N]: ").strip().lower() == "y"
    db.create_user(conn, username, display_name, password,
                    status=db.APPROVED, is_admin=make_admin)
    role = "admin" if make_admin else "staff"
    if first:
        print(f"Added {username} ({display_name}) as the first admin.")
    else:
        print(f"Added {username} ({display_name}) as an approved {role}.")


def cmd_list(args):
    conn = _conn()
    users = db.list_users(conn)
    if not users:
        print("No users yet. Add one with: python3 manage_users.py add")
        return
    width = max(len(u["username"]) for u in users)
    for u in users:
        role = "admin" if u["is_admin"] else "staff"
        print(f"{u['username']:<{width}}  {u['display_name']:<24}  {u['status']:<9}  {role}")
    pending = db.count_pending(conn)
    if pending:
        print(f"\n{pending} waiting for approval — approve in the app, or: "
              "manage_users.py approve <username>")


def cmd_passwd(args):
    if not args:
        return fail("usage: manage_users.py passwd <username>")
    conn = _conn()
    user = db.get_user_by_username(conn, args[0])
    if user is None:
        return fail(f"no user named {args[0]!r}")
    password = getpass.getpass(f"New password for {user['username']}: ")
    if len(password) < 8:
        return fail("password must be at least 8 characters")
    if password != getpass.getpass("Repeat password: "):
        return fail("passwords do not match")
    db.set_password(conn, user["id"], password)
    print(f"Password changed for {user['username']}.")


def _set_status(args, status, verb):
    if not args:
        return fail(f"usage: manage_users.py {verb} <username>")
    conn = _conn()
    user = db.get_user_by_username(conn, args[0])
    if user is None:
        return fail(f"no user named {args[0]!r}")
    try:
        db.set_user_status(conn, user["id"], status, decided_by="command line")
    except ValueError as exc:
        return fail(str(exc))
    print(f"{user['username']} is now {status}. "
          "Passes they already prepared keep their name.")


def _set_admin(args, is_admin, verb):
    if not args:
        return fail(f"usage: manage_users.py {verb} <username>")
    conn = _conn()
    user = db.get_user_by_username(conn, args[0])
    if user is None:
        return fail(f"no user named {args[0]!r}")
    try:
        db.set_user_admin(conn, user["id"], is_admin, decided_by="command line")
    except ValueError as exc:
        return fail(str(exc))
    print(f"{user['username']} is {'now an admin' if is_admin else 'no longer an admin'}.")


def fail(message):
    print(f"Error: {message}", file=sys.stderr)
    sys.exit(1)


COMMANDS = {
    "add": cmd_add,
    "list": cmd_list,
    "passwd": cmd_passwd,
    "approve": lambda a: _set_status(a, db.APPROVED, "approve"),
    "disable": lambda a: _set_status(a, db.DISABLED, "disable"),
    "enable": lambda a: _set_status(a, db.APPROVED, "enable"),
    "admin": lambda a: _set_admin(a, True, "admin"),
    "unadmin": lambda a: _set_admin(a, False, "unadmin"),
}


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        print(__doc__.strip())
        sys.exit(0 if len(sys.argv) < 2 else 1)
    COMMANDS[sys.argv[1]](sys.argv[2:])


if __name__ == "__main__":
    main()
