"""Sending a signed-in person on to MailStream Logistics.

MailStream is a separate app, in its own repository, with its own database and
no accounts of its own. Gate Pass is where accounts live, so when somebody picks
MailStream on the app chooser, Gate Pass signs a short-lived token saying who
they are and that they may use it, and sends the browser there with it.

The token is a JWT signed with HMAC-SHA256 under a secret the two apps share
through their configuration — GATE_PASS_MAILSTREAM_SECRET here, the same value
as MailStream's own. Standard library only, per the conventions in CLAUDE.md.

It travels in a URL, so it lives for a minute and carries a one-off id that
MailStream refuses to accept twice.
"""

import base64
import hashlib
import hmac
import json
import secrets
import time

LIFETIME = 60               # seconds: long enough for a slow redirect, no longer


def _b64(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def sign(claims, secret):
    head = _b64(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode())
    body = _b64(json.dumps(claims, separators=(",", ":")).encode())
    mac = hmac.new(secret.encode(), f"{head}.{body}".encode(), hashlib.sha256).digest()
    return f"{head}.{body}.{_b64(mac)}"


def mailstream_token(user, secret, may_use_mailstream, now=None):
    """The token for this signed-in user. `user` is a db user dict."""
    now = int(time.time() if now is None else now)
    return sign({
        "iss": "gatepass",
        "aud": "mailstream",
        "iat": now,
        "exp": now + LIFETIME,
        "jti": secrets.token_urlsafe(16),
        "user_id": f"usr_{user['id']}",
        "name": user.get("display_name") or user.get("username") or "",
        # Gate Pass keeps no email addresses (its database was not changed
        # for this), so MailStream shows the name alone.
        "email": "",
        "permissions": {"gatepass": True, "mailstream": bool(may_use_mailstream)},
    }, secret)
