"""Flask routes: upload -> review -> issue -> print, register, drafts, settings."""

import os
import secrets
from datetime import date, datetime, timedelta
from functools import wraps
from pathlib import Path
from urllib.parse import urlsplit

from flask import (
    Flask, Response, abort, flash, g, jsonify, redirect, render_template, request,
    session, send_from_directory, url_for,
)
from markupsafe import Markup, escape
from werkzeug.utils import secure_filename

import db
import exports
import invoice_parser

BASE_DIR = Path(__file__).parent

# Pages reachable without signing in.
PUBLIC_ENDPOINTS = {"login", "static"}


def client_ip():
    """The caller's address as best it can be known.

    nginx appends to X-Forwarded-For, and over the public Funnel tailscaled
    has already put the real client at the front, so the first entry is the
    browser and the rest are our own hops.

    Treat it as a hint, never as proof: a caller can send an
    X-Forwarded-For of their own and the proxies in front will append to it
    rather than replace it. That is why the per-address limit is only a
    backstop, and the per-username one — which cannot be forged, because
    the attacker has to name the account — does the real work.
    """
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        return forwarded.split(",")[0].strip()[:64]
    return (request.remote_addr or "?")[:64]


def create_app(db_path=None, storage_dir=None):
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = 32 * 1024 * 1024  # 32MB, plenty for a scanned invoice

    storage_dir = Path(storage_dir) if storage_dir else BASE_DIR / "storage"
    invoices_dir = storage_dir / "invoices"
    invoices_dir.mkdir(parents=True, exist_ok=True)
    app.config["STORAGE_DIR"] = storage_dir
    app.config["INVOICES_DIR"] = invoices_dir
    app.config["DB_PATH"] = str(db_path) if db_path else str(storage_dir / "gate_pass.db")
    app.secret_key = _secret_key(storage_dir)

    # Session cookie hardening.
    #   Lax  — the cookie is not sent on a cross-site POST, which is what stops
    #          another site silently cancelling a pass or adding an account in a
    #          signed-in operator's name.
    #   Secure — only over HTTPS. Off by default because the office runs this on
    #          plain HTTP over the LAN; set GATE_PASS_HTTPS=1 behind TLS.
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=os.environ.get("GATE_PASS_HTTPS") == "1",
        PERMANENT_SESSION_LIFETIME=timedelta(hours=12),
    )

    @app.before_request
    def block_cross_site_writes():
        """Reject any state-changing request that came from another site.

        Belt and braces with SameSite=Lax above: a browser always sends Origin
        on a cross-site POST and cannot be made to omit it, so a mismatched
        Origin is proof of a forged request. A missing Origin is allowed — that
        is a same-origin form post from an old browser, or a command-line client.
        """
        if request.method in ("GET", "HEAD", "OPTIONS"):
            return None
        source = request.headers.get("Origin") or request.headers.get("Referer")
        if not source:
            return None
        if urlsplit(source).netloc != request.host:
            abort(403, "cross-site request blocked")
        return None

    @app.after_request
    def security_headers(response):
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        return response

    @app.before_request
    def open_db():
        g.db = db.connect(app.config["DB_PATH"])
        g.user = None
        user_id = session.get("user_id")
        if user_id is not None:
            user = db.get_user(g.db, user_id)
            # An account disabled mid-session stops working on the next click.
            if user is not None and user["status"] == db.APPROVED:
                g.user = user
            else:
                session.clear()

    @app.teardown_request
    def close_db(exc):
        conn = g.pop("db", None)
        if conn is not None:
            db.close(conn)

    @app.template_global()
    def static_url(filename):
        """`url_for('static', ...)` with the file's modification time on the end.

        Without this the browser keeps serving the stylesheet it already has.
        That produces the most confusing possible failure: template changes
        appear immediately — they are rendered fresh every request — while CSS
        changes do not, so a layout looks half-applied and the code looks wrong
        when it is correct. It cost real time more than once before this
        existed. The stamp changes only when the file does, so caching still
        works normally between edits.
        """
        try:
            stamp = int((Path(app.static_folder) / filename).stat().st_mtime)
        except OSError:
            stamp = 0
        return url_for("static", filename=filename, v=stamp)

    @app.template_filter("line_breaks")
    def line_breaks(text):
        """Render typed newlines as line breaks on the printed pass.

        Escaped first and only then joined with <br>, so a remark containing
        angle brackets prints as text instead of being interpreted as markup.
        Building the string the other way round would be an injection into a
        document the company signs.
        """
        lines = str(text or "").splitlines()
        return Markup("<br>".join(escape(line) for line in lines))

    @app.template_filter("as_day_month_year")
    def as_day_month_year(value):
        """'2026-08-07 12:34:56' -> '07-08-2026'.

        Timestamps are stored as 'YYYY-MM-DD HH:MM:SS' (local time, see _now)
        because that sorts and compares as plain text. The printed pass wants
        the way the office writes a date, which is the way the invoices do.
        """
        text = str(value or "").strip()
        if len(text) < 10:
            return text
        year, month, day = text[:10].split("-")
        return f"{day}-{month}-{year}"

    @app.context_processor
    def inject_user():
        user = g.get("user")
        pending = db.count_pending(g.db) if user and user["is_admin"] else 0
        return {
            "current_user": user,
            "pending_count": pending,
            # `can('...')` in a template asks the same question the route guards
            # ask, so the two can never drift apart.
            "can": lambda permission: db.user_can(user, permission),
        }

    register_routes(app)
    return app


def _secret_key(storage_dir):
    """Session-signing key. Kept in storage/ so sessions survive a restart and
    are not a value shipped in the source. Override with GATE_PASS_SECRET_KEY."""
    from_env = os.environ.get("GATE_PASS_SECRET_KEY")
    if from_env:
        return from_env
    key_file = Path(storage_dir) / "secret_key"
    if not key_file.exists():
        key_file.parent.mkdir(parents=True, exist_ok=True)
        key_file.write_text(secrets.token_hex(32))
        key_file.chmod(0o600)
    return key_file.read_text().strip()


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if g.get("user") is None:
            return redirect(url_for("login", next=request.full_path.rstrip("?")))
        return view(*args, **kwargs)
    return wrapped


def requires(permission):
    """Guard a route with one named permission from db.PERMISSIONS.

    Checked on every request rather than at sign-in, so revoking a permission
    takes effect on someone's next click. A signed-in user who lacks it is sent
    back to the register with a message rather than shown a bare 403 — they have
    done nothing wrong, they just followed a link that is not theirs.
    """
    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            user = g.get("user")
            if user is None:
                return redirect(url_for("login", next=request.full_path.rstrip("?")))
            if not db.user_can(user, permission):
                flash("403 Unauthorized — you do not have access to that.", "error")
                return redirect(url_for("register"))
            return view(*args, **kwargs)
        return wrapped
    return decorator


# Kept as a thin alias: "admin" now means holding the permission to manage
# people, which is the closest thing to a super-user this app has.
def admin_required(view):
    return requires("can_manage_people")(view)


def register_routes(app):

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if g.get("user") is not None:
            return redirect(url_for("upload"))

        if request.method == "POST":
            username = request.form.get("username", "")
            password = request.form.get("password", "")

            # Before the password is even looked at, so a locked-out caller
            # cannot learn from the reply whether their guess was right.
            waiting_for = db.login_lockout(g.db, username, client_ip())
            if waiting_for:
                minutes = max(1, round(waiting_for / 60))
                flash(f"Too many failed sign-ins. Try again in about "
                      f"{minutes} minute{'s' if minutes != 1 else ''}.", "error")
                return render_template("login.html", username=username,
                                       next_url=request.args.get("next", "")), 429

            user = db.authenticate(g.db, username, password)
            if user is None:
                db.record_failed_login(g.db, username, client_ip())
                # Someone whose own password is right is told why they can't get
                # in; everyone else gets a vague message that reveals nothing
                # about whether the username exists.
                if db.password_is_correct(g.db, username, password):
                    waiting = db.get_user_by_username(g.db, username)
                    if waiting["status"] == db.PENDING:
                        flash("Your account is waiting for an admin to approve it.", "warn")
                    elif waiting["status"] == db.REJECTED:
                        flash("Your account request was turned down. Speak to an admin.", "error")
                    else:
                        flash("Your account has been switched off. Speak to an admin.", "error")
                else:
                    flash("Wrong username or password.", "error")
                return render_template("login.html", username=username,
                                        no_users=db.count_users(g.db) == 0), 401
            session.clear()
            db.clear_login_attempts(g.db, username)
            session["user_id"] = user["id"]
            session.permanent = False
            target = request.args.get("next") or url_for("upload")
            # Only ever bounce back to a path on this site.
            if not target.startswith("/") or target.startswith("//"):
                target = url_for("upload")
            return redirect(target)

        return render_template("login.html", username="",
                                no_users=db.count_users(g.db) == 0)

    @app.route("/people")
    @admin_required
    def people():
        users = db.list_users(g.db)
        return render_template(
            "people.html", active="people",
            pending=[u for u in users if u["status"] == db.PENDING],
            others=[u for u in users if u["status"] != db.PENDING],
            permission_labels=db.PERMISSIONS,
            permission_hints=db.PERMISSION_HINTS,
        )

    @app.route("/people/add", methods=["POST"])
    @admin_required
    def add_person():
        """Accounts are created here and nowhere else — there is no self-signup.

        The admin sets an initial password and hands it over; it is stored only
        as a salted hash, and the person can be given a new one at any time from
        this same screen.
        """
        username = request.form.get("username", "")
        display_name = request.form.get("display_name", "")
        password = request.form.get("password", "")
        confirm = request.form.get("confirm_password", "")
        permissions = _permissions_from_form()

        if password != confirm:
            flash("The two passwords do not match.", "error")
            return redirect(url_for("people"))
        try:
            db.create_user(g.db, username, display_name, password,
                            status=db.APPROVED, permissions=permissions)
        except ValueError as exc:
            flash(str(exc)[0].upper() + str(exc)[1:] + ".", "error")
            return redirect(url_for("people"))

        granted = sum(1 for allowed in permissions.values() if allowed)
        flash(f"{display_name.strip()} can now sign in with {granted} "
              f"permission{'' if granted == 1 else 's'}. Give them their password.", "ok")
        return redirect(url_for("people"))

    @app.route("/people/<int:user_id>/permissions", methods=["POST"])
    @requires("can_manage_people")
    def set_permissions(user_id):
        target = db.get_user(g.db, user_id)
        if target is None:
            abort(404)
        wanted = _permissions_from_form()
        # Removing your own ability to manage people would lock you out of this
        # very screen; the database also refuses to strip the last one.
        if target["id"] == g.user["id"] and not wanted["can_manage_people"]:
            flash("You cannot remove your own permission to manage people.", "error")
            return redirect(url_for("people"))
        try:
            db.set_user_permissions(g.db, user_id, wanted,
                                     decided_by=g.user["display_name"])
        except ValueError as exc:
            flash(str(exc)[0].upper() + str(exc)[1:] + ".", "error")
            return redirect(url_for("people"))
        granted = sum(1 for allowed in wanted.values() if allowed)
        flash(f"{target['display_name']} now has {granted} "
              f"permission{'' if granted == 1 else 's'}.", "ok")
        return redirect(url_for("people"))

    @app.route("/people/<int:user_id>/password", methods=["POST"])
    @admin_required
    def reset_password(user_id):
        target = db.get_user(g.db, user_id)
        if target is None:
            abort(404)
        password = request.form.get("password", "")
        if password != request.form.get("confirm_password", ""):
            flash("The two passwords do not match.", "error")
            return redirect(url_for("people"))
        try:
            db.set_password(g.db, user_id, password)
        except ValueError as exc:
            flash(str(exc)[0].upper() + str(exc)[1:] + ".", "error")
            return redirect(url_for("people"))
        flash(f"New password set for {target['display_name']}.", "ok")
        return redirect(url_for("people"))

    @app.route("/people/<int:user_id>/status", methods=["POST"])
    @admin_required
    def admin_set_status(user_id):
        status = request.form.get("status", "")
        target = db.get_user(g.db, user_id)
        if target is None:
            abort(404)
        if target["id"] == g.user["id"] and status != db.APPROVED:
            flash("You cannot switch off your own account.", "error")
            return redirect(url_for("people"))
        try:
            db.set_user_status(g.db, user_id, status, decided_by=g.user["display_name"])
        except ValueError as exc:
            flash(str(exc)[0].upper() + str(exc)[1:] + ".", "error")
            return redirect(url_for("people"))

        wording = {db.APPROVED: "approved", db.REJECTED: "turned down",
                    db.DISABLED: "switched off", db.PENDING: "put back to pending"}
        flash(f"{target['display_name']} {wording.get(status, 'updated')}.", "ok")
        return redirect(url_for("people"))

    @app.route("/people/<int:user_id>/role", methods=["POST"])
    @admin_required
    def admin_set_admin(user_id):
        make_admin = request.form.get("is_admin") == "1"
        target = db.get_user(g.db, user_id)
        if target is None:
            abort(404)
        if target["id"] == g.user["id"] and not make_admin:
            flash("You cannot remove your own admin rights — ask another admin.", "error")
            return redirect(url_for("people"))
        try:
            db.set_user_admin(g.db, user_id, make_admin, decided_by=g.user["display_name"])
        except ValueError as exc:
            flash(str(exc)[0].upper() + str(exc)[1:] + ".", "error")
            return redirect(url_for("people"))
        flash(f"{target['display_name']} is {'now an admin' if make_admin else 'no longer an admin'}.",
              "ok")
        return redirect(url_for("people"))

    @app.route("/logout", methods=["POST"])
    def logout():
        session.clear()
        flash("Signed out.", "ok")
        return redirect(url_for("login"))

    @app.route("/")
    @login_required
    def index():
        return redirect(url_for("upload"))

    @app.route("/upload", methods=["GET", "POST"])
    @login_required
    def upload():
        if request.method == "GET":
            return render_template("upload.html", active="upload")

        files = [f for f in request.files.getlist("invoice") if f and f.filename]
        if not files:
            flash("Choose one or more invoice PDFs to upload.", "error")
            return redirect(url_for("upload"))

        draft_ids, unreadable, rejected = [], [], []
        accepted = []
        for file in files:
            if not file.filename.lower().endswith(".pdf"):
                rejected.append(file.filename)
            else:
                accepted.append(file)

        # The whole batch is parsed together so the files can be read several
        # at a time — fifty invoices takes about 3.5 seconds this way against
        # 13 one after another. A PDF that cannot be read still becomes a
        # draft: it just cannot be issued until someone fills it in, and a
        # draft never holds a serial number.
        for file, (draft_id, notes) in zip(accepted, _drafts_from_uploads(app, accepted)):
            draft_ids.append(draft_id)
            if notes:
                unreadable.append(f"{file.filename}: {notes}")

        if rejected:
            flash(f"Skipped {len(rejected)} file(s) that were not PDFs: "
                  + ", ".join(rejected[:5]) + ("…" if len(rejected) > 5 else ""), "error")
        if not draft_ids:
            return redirect(url_for("upload"))

        # Without the reviewing permission there is no draft step: the pass is
        # numbered, issued and put on screen ready to print in one go.
        if not db.user_can(g.user, "can_review_drafts"):
            return _issue_immediately(app, draft_ids)

        # One file behaves exactly as before: straight to its review screen.
        if len(draft_ids) == 1 and not rejected:
            if unreadable:
                flash("Some details could not be read — please check them: "
                      + unreadable[0].split(": ", 1)[1], "warn")
            return redirect(url_for("review", draft_id=draft_ids[0]))

        flash(f"Read {len(draft_ids)} invoice(s). Check them below, then issue "
              "the ones that are ready.", "ok")
        if unreadable:
            flash(f"{len(unreadable)} could not be read in full and need attention "
                  "before they can be issued.", "warn")
        return redirect(url_for("drafts"))

    @app.route("/review/<int:draft_id>", methods=["GET", "POST"])
    @login_required
    def review(draft_id):
        draft = db.get_draft(g.db, draft_id)
        if draft is None:
            # Issuing DELETES the draft, so this link dies the moment the pass
            # exists — and that is precisely where the browser's Back button
            # lands after issuing one. A bare 404 tells the operator nothing
            # and loses the pass they just made.
            issued = db.gate_pass_from_draft(g.db, draft_id)
            if issued is not None:
                gate_pass = db.get_gate_pass(g.db, issued)
                if gate_pass is not None:
                    # No flash: print.html is a standalone page and renders
                    # none, so the message would sit in the session and
                    # surface later on an unrelated screen. The print page
                    # names the serial in its toolbar anyway, which is the
                    # thing the operator wanted to know.
                    return redirect(url_for("print_gate_pass", gate_pass_id=issued))
            flash("That draft is gone — it was either issued or discarded. "
                  "Nothing was lost from the register.", "warn")
            return redirect(url_for("upload"))

        may_edit = db.user_can(g.user, "can_edit_parsed_details")

        if request.method == "GET":
            return render_template("review.html", draft=draft,
                                    items_per_page=db.ITEMS_PER_PAGE,
                                    may_edit=may_edit,
                                    active="drafts")

        action = request.form.get("action", "save")
        # Never parsed from the invoice, so always the operator's to type —
        # same as the vehicle number. _fill_blanks_only leaves it alone.
        remarks = request.form.get("remarks", "").strip()
        if may_edit:
            supplier_name = request.form.get("supplier_name", "").strip()
            customer_name = request.form.get("customer_name", "").strip()
            invoice_no = request.form.get("invoice_no", "").strip()
            invoice_date = request.form.get("invoice_date", "").strip()
            vehicle_no = request.form.get("vehicle_no", "").strip()
            items = _items_from_form(request.form)
        else:
            # Enforced here, not merely in the template: a readonly attribute is
            # a courtesy to the person typing, not a control. Anyone can post
            # whatever they like to this route.
            fields, items = _fill_blanks_only(draft, request.form)
            supplier_name = fields["supplier_name"]
            customer_name = fields["customer_name"]
            invoice_no = fields["invoice_no"]
            invoice_date = fields["invoice_date"]
            vehicle_no = fields["vehicle_no"]

        db.update_draft(g.db, draft_id, supplier_name, customer_name, invoice_no,
                         invoice_date, vehicle_no, items, remarks=remarks)

        if action == "issue":
            try:
                gate_pass = db.create_gate_pass(
                    g.db, draft_id, supplier_name, customer_name, invoice_no,
                    invoice_date, vehicle_no, items,
                    prepared_by=g.user["display_name"],
                    prepared_by_user_id=g.user["id"],
                    remarks=remarks,
                )
            except ValueError as exc:
                flash(str(exc), "error")
                return redirect(url_for("review", draft_id=draft_id))
            # Everything the invoice had to say is now on the gate pass, so the
            # upload is deleted rather than left to fill the disk. Only after
            # the pass is safely committed.
            _discard_invoice_file(app, draft["invoice_pdf_path"])
            return redirect(url_for("print_gate_pass", gate_pass_id=gate_pass["id"]))

        flash("Draft saved. No gate pass number has been used.", "ok")
        return redirect(url_for("review", draft_id=draft_id))

    @app.route("/drafts")
    @requires("can_review_drafts")
    def drafts():
        rows = db.list_drafts(g.db)
        for draft in rows:
            full = db.get_draft(g.db, draft["id"])
            draft["problem"] = db.draft_problem(full)
            draft["item_count"] = len(full["items"])
        return render_template("drafts.html", drafts=rows, active="drafts",
                                ready_count=sum(1 for d in rows if not d["problem"]),
                                next_serial=db.next_serial_preview(g.db))

    def _back_to_drafts(draft_ids):
        """Where to land after a drafts action.

        The Drafts list is behind `can_review_drafts`, so someone who reached a
        draft by being redirected to its review screen must not be bounced to a
        page they cannot open — that would show them a 403 for finishing the
        job they were sent to do. They go back to Upload instead.
        """
        if db.user_can(g.user, "can_review_drafts"):
            return url_for("drafts")
        remaining = [d for d in (draft_ids or []) if db.get_draft(g.db, d) is not None]
        if remaining:
            return url_for("review", draft_id=remaining[0])
        return url_for("upload")

    def _issue_immediately(app, draft_ids):
        """Parse straight through to a printed pass, with no draft step.

        For staff without `can_review_drafts`. The draft row still exists for a
        moment — it is what `create_gate_passes_batch` reads and deletes inside
        the transaction — but it is never a screen anyone sees, and it never
        holds a serial number.

        A pass cannot be issued without a supplier, customer, document number,
        date and at least one item with a quantity, so a PDF that could not be
        read has nowhere to go but the review screen. That fallback is not
        optional: the alternative is issuing a numbered pass with no items on
        it. The register would then carry a permanent gap in meaning that
        cancelling cannot undo, because a cancelled pass keeps its number.
        """
        uploads = {}
        for draft_id in draft_ids:
            draft = db.get_draft(g.db, draft_id)
            if draft is not None:
                uploads[draft_id] = draft["invoice_pdf_path"]

        try:
            issued, skipped = db.create_gate_passes_batch(
                g.db, draft_ids,
                prepared_by=g.user["display_name"], prepared_by_user_id=g.user["id"])
        except Exception as exc:  # noqa: BLE001 - surfaced to the operator
            flash(f"Nothing was issued — the batch was rolled back: {exc}", "error")
            return redirect(url_for("upload"))

        held_back = {draft_id for draft_id, _reason in skipped}
        for draft_id, relpath in uploads.items():
            if draft_id not in held_back:
                _discard_invoice_file(app, relpath)

        if issued:
            first, last = issued[0]["serial_no"], issued[-1]["serial_no"]
            span = first if len(issued) == 1 else f"{first} to {last}"
            flash(f"Issued {len(issued)} gate pass(es): {span}.", "ok")

        # Anything that could not be read goes to its review screen so the
        # operator can finish it by hand. That screen is per-draft and needs no
        # permission, so it is reachable even without the Drafts list.
        if skipped:
            for _draft_id, reason in skipped:
                flash(f"Not issued — {reason}. No number was used.", "warn")
            return redirect(url_for("review", draft_id=skipped[0][0]))

        if len(issued) == 1:
            return redirect(url_for("print_gate_pass", gate_pass_id=issued[0]["id"]))
        if issued and db.user_can(g.user, "can_batch_print"):
            return redirect(url_for("print_batch",
                                     ids=",".join(str(p["id"]) for p in issued)))
        return redirect(url_for("register"))

    @app.route("/drafts/issue", methods=["POST"])
    @login_required
    def issue_drafts():
        """Issue many drafts in one go. The whole batch is one transaction, so
        the numbers come out as an unbroken block or not at all."""
        draft_ids = [int(i) for i in request.form.getlist("draft_id") if i.isdigit()]
        if not draft_ids:
            flash("Select at least one invoice to issue.", "error")
            return redirect(_back_to_drafts(draft_ids))

        # Noted before the transaction, because the drafts are deleted by it.
        uploads = {}
        for draft_id in draft_ids:
            draft = db.get_draft(g.db, draft_id)
            if draft is not None:
                uploads[draft_id] = draft["invoice_pdf_path"]

        try:
            issued, skipped = db.create_gate_passes_batch(
                g.db, draft_ids,
                prepared_by=g.user["display_name"], prepared_by_user_id=g.user["id"])
        except Exception as exc:  # noqa: BLE001 - surfaced to the operator
            flash(f"Nothing was issued — the batch was rolled back: {exc}", "error")
            return redirect(_back_to_drafts(draft_ids))

        # Only the uploads whose draft actually became a gate pass. A skipped
        # draft keeps its PDF, because it is still waiting to be fixed and
        # issued.
        held_back = {draft_id for draft_id, _reason in skipped}
        for draft_id, relpath in uploads.items():
            if draft_id not in held_back:
                _discard_invoice_file(app, relpath)

        if issued:
            first, last = issued[0]["serial_no"], issued[-1]["serial_no"]
            span = first if len(issued) == 1 else f"{first} to {last}"
            flash(f"Issued {len(issued)} gate pass(es): {span}.", "ok")
        for _draft_id, reason in skipped:
            flash(f"Not issued — {reason}. No number was used.", "warn")

        if len(issued) == 1:
            return redirect(url_for("print_gate_pass", gate_pass_id=issued[0]["id"]))
        if issued:
            return redirect(url_for("register"))
        return redirect(_back_to_drafts(draft_ids))

    @app.route("/drafts/<int:draft_id>/delete", methods=["POST"])
    @login_required
    def delete_draft(draft_id):
        draft = db.get_draft(g.db, draft_id)
        if draft is None:
            abort(404)
        db.delete_draft(g.db, draft_id)
        _discard_invoice_file(app, draft["invoice_pdf_path"])
        flash("Draft discarded. No gate pass number was used.", "ok")
        return redirect(_back_to_drafts([]))

    @app.route("/print/<int:gate_pass_id>")
    @login_required
    def print_gate_pass(gate_pass_id):
        gate_pass = db.get_gate_pass(g.db, gate_pass_id)
        if gate_pass is None:
            abort(404)
        settings = db.get_settings(g.db)
        return render_template("print.html", gate_pass=gate_pass, settings=settings,
                                pages=db.paginate_items(gate_pass["items"]),
                                page_rows=db.print_row_count,
                                row_height_mm=db.print_row_height_mm)

    @app.route("/print/batch")
    @requires("can_batch_print")
    def print_batch():
        """Several passes as one printable document.

        Nothing is written to disk: no PDF, no ZIP. The browser renders the
        sheets and sends them straight to the printer, which is also why the
        page calls window.print() itself on load.
        """
        raw = request.args.get("ids", "")
        wanted, seen = [], set()
        for part in raw.split(","):
            part = part.strip()
            if part.isdigit() and int(part) not in seen:
                seen.add(int(part))
                wanted.append(int(part))

        if not wanted:
            flash("Select at least one gate pass to print.", "error")
            return redirect(url_for("register"))
        if len(wanted) > db.MAX_BATCH_PRINT:
            flash(f"{len(wanted)} passes selected — {db.MAX_BATCH_PRINT} is the most "
                  "that can be printed in one go. Print them in smaller batches.", "error")
            return redirect(url_for("register"))

        passes, missing = [], []
        for gate_pass_id in wanted:
            gate_pass = db.get_gate_pass(g.db, gate_pass_id)
            if gate_pass is None:
                missing.append(gate_pass_id)
                continue
            gate_pass["pages"] = db.paginate_items(gate_pass["items"])
            passes.append(gate_pass)

        if not passes:
            flash("None of those gate passes could be found.", "error")
            return redirect(url_for("register"))
        if missing:
            flash(f"{len(missing)} of the selected passes no longer exist and were "
                  "left out.", "warn")

        # Oldest first, so a batch prints in the order the book runs.
        passes.sort(key=lambda p: p["id"])
        return render_template(
            "print_batch.html", passes=passes, settings=db.get_settings(g.db),
            page_rows=db.print_row_count, row_height_mm=db.print_row_height_mm,
            sheet_count=sum(len(p["pages"]) for p in passes))

    @app.route("/register")
    @login_required
    def register():
        # Search and filter are separate permissions, enforced here rather than
        # merely hidden in the template — otherwise anyone could filter by
        # editing the query string.
        # One permission covers both: nobody ever wanted to search a register
        # they could not narrow, or narrow one they could not search.
        may_search = may_filter = db.user_can(g.user, "can_search_register")
        requested = _register_args()
        filters = {
            "search": requested["search"] if may_search else None,
            "status": requested["status"] if may_filter else None,
            "date_from": requested["date_from"] if may_filter else None,
            "date_to": requested["date_to"] if may_filter else None,
        }
        gate_passes = db.list_gate_passes(g.db, **filters)
        total = db.count_gate_passes(g.db, **filters)
        # Anything other than the search box counts as a filter, so the button
        # can show that something is narrowing the list.
        active_filters = sum(1 for key in ("status", "date_from", "date_to")
                             if filters.get(key))
        return render_template("register.html", gate_passes=gate_passes,
                                status=filters["status"] or "",
                                search=filters["search"] or "",
                                date_from=filters["date_from"] or "",
                                date_to=filters["date_to"] or "",
                                may_search=may_search, may_filter=may_filter,
                                may_cancel=db.user_can(g.user, "can_cancel_passes"),
                                may_batch_print=db.user_can(g.user, "can_batch_print"),
                                max_batch=db.MAX_BATCH_PRINT,
                                active_filters=active_filters,
                                total=total, page_size=db.REGISTER_PAGE_SIZE,
                                today=date.today().isoformat(),
                                active="register")

    @app.route("/reports")
    @requires("can_export_reports")
    def reports():
        """Everything to do with exporting lives here, off the register."""
        filters = _report_args()
        # The preview answers "did my filters catch the right passes?" before
        # anything is downloaded — so it only appears once Apply Filters has
        # been pressed. A bare /reports is the empty form, and does not run the
        # two queries at all.
        applied = bool(request.args)
        preview = (db.list_gate_passes(g.db, limit=db.REPORT_PREVIEW_LIMIT, **filters)
                   if applied else [])
        matching = db.count_gate_passes(g.db, **filters) if applied else 0
        return render_template(
            "reports.html", active="reports",
            applied=applied, preview=preview, matching=matching,
            preview_limit=db.REPORT_PREVIEW_LIMIT,
            today=date.today().isoformat(),
            quick=request.args.get("range", ""),
            detail=request.args.get("detail", ""),
            fmt=request.args.get("format", "csv"),
            suppliers=db.distinct_values(g.db, "supplier_name"),
            customers=db.distinct_values(g.db, "customer_name"),
            **{k: v or "" for k, v in filters.items()})

    @app.route("/reports/export")
    @requires("can_export_reports")
    def export_report():
        """Generate the report as CSV or a real .xlsx workbook."""
        filters = _report_args()
        detailed = request.args.get("detail") == "1"
        fmt = "xlsx" if request.args.get("format") == "xlsx" else "csv"
        name = exports.filename("gate-passes", detailed, fmt,
                                 filters["date_from"], filters["date_to"])
        headers = {"Content-Disposition": f'attachment; filename="{name}"'}
        db_path = app.config["DB_PATH"]

        if fmt == "xlsx":
            # A workbook's zip index is written last, so it cannot be streamed
            # the way CSV can; build it, then send it.
            conn = db.connect(db_path)
            try:
                payload = exports.xlsx_bytes(
                    db.export_rows(conn, detailed=detailed, **filters),
                    title="Gate Pass Items" if detailed else "Gate Passes")
            finally:
                db.close(conn)
            return Response(payload, mimetype=exports.CONTENT_TYPES["xlsx"],
                             headers=headers)

        def stream():
            # A streamed response runs *after* the request context has ended, so
            # g.db is already closed — the export owns its own connection.
            conn = db.connect(db_path)
            try:
                yield from exports.csv_bytes(
                    db.export_rows(conn, detailed=detailed, **filters))
            finally:
                db.close(conn)

        return Response(stream(), mimetype=exports.CONTENT_TYPES["csv"], headers=headers)

    @app.route("/upload/one", methods=["POST"])
    @login_required
    def upload_one():
        """One PDF at a time, for the batch uploader.

        Uploading 50 files as a single request gives no progress and can exceed
        the request size limit; sending them one by one lets the page report
        real progress — each response means that file is parsed and stored.
        """
        file = request.files.get("invoice")
        if not file or not file.filename:
            return jsonify(ok=False, error="no file"), 400
        if not file.filename.lower().endswith(".pdf"):
            return jsonify(ok=False, filename=file.filename,
                            error="not a PDF"), 400

        draft_id, notes = _draft_from_upload(app, file)
        draft = db.get_draft(g.db, draft_id)
        problem = db.draft_problem(draft)
        return jsonify(ok=True, filename=file.filename, draft_id=draft_id,
                        notes=notes, problem=problem)

    @app.route("/gate-passes/<int:gate_pass_id>/cancel", methods=["POST"])
    @requires("can_cancel_passes")
    def cancel_gate_pass(gate_pass_id):
        reason = request.form.get("reason", "").strip()
        gate_pass = db.get_gate_pass(g.db, gate_pass_id)
        if gate_pass is None:
            abort(404)
        if not reason:
            flash("A reason is required to cancel a gate pass.", "error")
        elif gate_pass["status"] != "issued":
            flash("Only an issued gate pass can be cancelled.", "error")
        else:
            db.cancel_gate_pass(g.db, gate_pass_id, reason,
                                 cancelled_by=g.user["display_name"])
            flash(f"{gate_pass['serial_no']} cancelled. The number stays reserved.", "ok")
        return redirect(url_for("register"))

    @app.route("/settings", methods=["GET", "POST"])
    @requires("can_access_settings")
    def settings():
        if request.method == "POST":
            db.update_settings(
                g.db,
                paper_mode="a5" if request.form.get("paper_mode") == "a5" else "a4x2",
                show_total_qty="1" if request.form.get("show_total_qty") else "0",
                show_total_cartons="1" if request.form.get("show_total_cartons") else "0",
                company_name=request.form.get("company_name", "fanzart").strip() or "fanzart",
            )
            flash("Settings saved.", "ok")
            return redirect(url_for("settings"))
        return render_template("settings.html", settings=db.get_settings(g.db),
                                next_serial=db.next_serial_preview(g.db),
                                active="settings")

    @app.route("/settings/new-run", methods=["POST"])
    @requires("can_access_settings")
    def new_serial_run():
        """Set the numbering to a prefix and a starting number.

        Nothing is deleted — every pass already issued keeps its number and
        stays in the register. A prefix that has been used before is allowed:
        it carries on from the last number issued under it rather than starting
        again, which is what makes reuse safe.
        """
        if request.form.get("confirm") != "yes":
            flash("Tick the confirmation box to change the numbering.", "error")
            return redirect(url_for("settings"))
        try:
            # "FZ-", "FZ27-" or a whole example such as "FZ-00001".
            prefix, start_at = db.parse_run_spec(request.form.get("prefix", ""))
            typed = request.form.get("start_at", "").strip()
            if typed:
                if not typed.isdigit():
                    raise ValueError("the starting number must be digits only")
                start_at = int(typed)
            prefix, start_at = db.start_new_run(
                g.db, prefix, start_at, started_by=g.user["display_name"])
        except ValueError as exc:
            flash(str(exc)[0].upper() + str(exc)[1:] + ".", "error")
            return redirect(url_for("settings"))
        flash(f"Numbering set. The next gate pass will be "
              f"{db.serial_for(prefix, start_at)}. Everything already issued is "
              f"untouched.", "ok")
        return redirect(url_for("settings"))

    @app.route("/invoices/<path:relpath>")
    @login_required
    def uploaded_invoice(relpath):
        """Serve an uploaded invoice.

        Rooted at the invoices folder, NOT at storage/. Serving from storage/
        made `/invoices/gate_pass.db` and `/invoices/secret_key` downloadable by
        any signed-in account — the whole book with every password hash, and the
        key used to sign session cookies, which is enough to forge an admin
        session. send_from_directory blocks `..`, but it cannot know that the
        root it was given was too wide.

        Paths are stored relative to storage/ ("invoices/xxx.pdf"), so the
        leading folder is stripped before joining.
        """
        relpath = relpath.removeprefix("invoices/")
        return send_from_directory(app.config["INVOICES_DIR"], relpath)


def _permissions_from_form():
    """Tick boxes to a permission dict. An unticked box is simply absent from the
    form, so anything not listed is denied rather than left as it was."""
    return {key: request.form.get(key) == "1" for key in db.PERMISSIONS}


def _register_args():
    """Filters from the query string, including the quick date ranges."""
    quick = request.args.get("range", "")
    date_from = request.args.get("from") or None
    date_to = request.args.get("to") or None

    today = date.today()
    if quick == "today":
        date_from = date_to = today.isoformat()
    elif quick == "week":
        # Monday of the current week through today.
        date_from = (today - timedelta(days=today.weekday())).isoformat()
        date_to = today.isoformat()
    elif quick == "month":
        date_from = today.replace(day=1).isoformat()
        date_to = today.isoformat()
    elif quick == "year":
        date_from = today.replace(month=1, day=1).isoformat()
        date_to = today.isoformat()

    return {
        "status": request.args.get("status") or None,
        "search": request.args.get("q") or None,
        "date_from": _valid_date(date_from),
        "date_to": _valid_date(date_to),
    }


def _report_args():
    """Register filters plus the two the reports page adds."""
    args = _register_args()
    args["supplier"] = request.args.get("supplier") or None
    args["customer"] = request.args.get("customer") or None
    return args


def _valid_date(value):
    """Accept YYYY-MM-DD only; anything else is ignored rather than trusted."""
    if not value:
        return None
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        return None


def _discard_invoice_file(app, relpath):
    """Delete an uploaded invoice once nothing refers to it any more.

    The path comes from the database rather than a request, but it is still
    resolved and checked against the invoices directory before anything is
    unlinked — a delete driven by a stored string should not be able to wander
    outside its folder if that string is ever wrong.
    """
    if not relpath:
        return False
    root = Path(app.config["STORAGE_DIR"]).resolve()
    invoices = Path(app.config["INVOICES_DIR"]).resolve()
    try:
        target = (root / relpath).resolve()
        target.relative_to(invoices)
    except (ValueError, OSError):
        return False
    try:
        target.unlink()
        return True
    except FileNotFoundError:
        return False
    except OSError:
        return False


def _draft_from_upload(app, file):
    """Save one uploaded PDF and turn it into a draft. Returns (draft_id, notes)."""
    dest = app.config["INVOICES_DIR"] / _timestamped_filename(file.filename)
    file.save(dest)
    try:
        parsed = invoice_parser.parse_invoice(dest)
    except Exception as exc:  # noqa: BLE001 - one bad file must not stop the batch
        parsed = {"supplier_name": "", "customer_name": "", "invoice_no": "",
                  "invoice_date": "", "items": [], "notes": [f"could not be read: {exc}"]}
    return _draft_from_parsed(app, dest, parsed)


def _drafts_from_uploads(app, files):
    """Save and parse a batch, then create the drafts in the order given.

    Only the PARSING is done in parallel, and only in worker processes. The
    database work stays here, one draft at a time, on this thread: the sqlite
    connection on `g` belongs to this request and this thread, and drafts must
    appear in the order the files were chosen so that issuing them allocates
    serial numbers in that order too.
    """
    saved = []
    for file in files:
        dest = app.config["INVOICES_DIR"] / _timestamped_filename(file.filename)
        file.save(dest)
        saved.append(dest)

    results = []
    for dest, parsed in zip(saved, invoice_parser.parse_many(saved)):
        results.append(_draft_from_parsed(app, dest, parsed))
    return results


def _draft_from_parsed(app, dest, parsed):
    """Store one already-parsed invoice as a draft. Returns (draft_id, notes)."""
    notes = "; ".join(parsed["notes"])

    # Carton counts come from the master list rather than being counted by
    # hand, which was the slowest part of writing a gate pass. Done here, once,
    # as the draft is created: the answer is then saved, survives a reload, and
    # is never re-applied over a correction somebody made on the review page.
    # Items with no match — and every spare, freight or service line — are left
    # blank on purpose. See db.fill_cartons.
    parsed["items"] = db.fill_cartons(g.db, parsed["items"])
    # Every carton box blank looks the same whether the item is unlisted or the
    # master list was never imported. Say which, where it will be noticed.
    if parsed["items"] and db.carton_list_is_empty(g.db):
        notes = "; ".join(filter(None, [
            notes, "carton master list is not loaded — run manage_cartons.py import"]))

    relpath = str(dest.relative_to(app.config["STORAGE_DIR"]))
    try:
        draft_id = db.create_draft(
            g.db,
            supplier_name=parsed["supplier_name"],
            customer_name=parsed["customer_name"],
            invoice_no=parsed["invoice_no"],
            invoice_date=parsed["invoice_date"],
            invoice_pdf_path=relpath,
            parse_notes=notes,
            items=parsed["items"],
        )
    except Exception:
        # The file is on disk but no draft will ever point at it, so nothing
        # would delete it later. Clean up before the error propagates, or the
        # invoices folder grows a file every time a write fails.
        _discard_invoice_file(app, relpath)
        raise
    return draft_id, notes


def _timestamped_filename(original):
    stamp = datetime.now().strftime("%Y%m%d%H%M%S%f")
    safe = secure_filename(original) or "invoice.pdf"
    return f"{stamp}_{safe}"


PARSED_FIELDS = ("supplier_name", "customer_name", "invoice_no", "invoice_date",
                  "vehicle_no")


def _fill_blanks_only(draft, form):
    """What someone without `can_edit_parsed_details` is allowed to submit.

    The rule: **the parser's output is the record.** A value the invoice
    actually yielded is kept exactly as read; only what came back blank is
    taken from the form. So a scan with no text layer can still be typed in and
    issued, and an invoice that parsed cleanly cannot be quietly restated as
    saying something else.

    Applied per field and per item cell rather than all-or-nothing, because a
    half-read invoice is the common case — the supplier and date come through
    and one quantity does not, and that one gap is what needs filling.

    `vehicle_no` never comes from the parser, so it is always blank here and
    always editable. Cartons likewise: rule 5 means the parser never returns
    them, so they can always be typed.
    """
    fields = {}
    for key in PARSED_FIELDS:
        stored = str(draft.get(key) or "").strip()
        fields[key] = stored or form.get(key, "").strip()

    submitted = _items_from_form(form)
    stored_items = draft.get("items") or []
    if not stored_items:
        # The parser found no item table at all, so every row is theirs to type.
        return fields, submitted

    # It found items: the list is fixed. Rows cannot be added or removed, and
    # each cell keeps what was read, taking the form's value only where blank.
    items = []
    for index, stored_item in enumerate(stored_items):
        offered = submitted[index] if index < len(submitted) else {}
        merged = {"sl_no": index + 1}
        for key in ("item_name", "quantity", "cartons"):
            was_read = str(stored_item.get(key) or "").strip()
            merged[key] = was_read or str(offered.get(key) or "").strip()
        items.append(merged)
    return fields, items


def _items_from_form(form):
    names = form.getlist("item_name")
    quantities = form.getlist("quantity")
    cartons = form.getlist("cartons")
    items = []
    for i, raw_name in enumerate(names):
        name = raw_name.strip()
        qty = quantities[i].strip() if i < len(quantities) else ""
        ctn = cartons[i].strip() if i < len(cartons) else ""
        if not name and not qty and not ctn:
            continue
        items.append({"sl_no": len(items) + 1, "item_name": name, "quantity": qty, "cartons": ctn})
    return items


app = create_app()

if __name__ == "__main__":
    # debug is OFF unless asked for. The Werkzeug debugger is an interactive
    # Python console on an error page — on a hosted machine that is a way in,
    # not a convenience. For development: GATE_PASS_DEBUG=1 python3 app.py
    # In production this file is not the entry point at all; gunicorn imports
    # `app` directly (see the deployment notes in CLAUDE.md).
    debug = os.environ.get("GATE_PASS_DEBUG") == "1"
    app.run(host=os.environ.get("GATE_PASS_HOST", "127.0.0.1"),
            port=int(os.environ.get("GATE_PASS_PORT", "8090")),
            debug=debug)
