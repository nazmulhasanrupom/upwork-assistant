"""Panel login (SPEC 10.4): one admin, argon2 password, TOTP, signed session, CSRF, lockout.

Secrets never go to logs. The TOTP secret is stored encrypted (see app/crypto.py).
Lockout state is in the `auth_state` table, so it survives a restart.
"""

from __future__ import annotations

import hmac
import logging
import math
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import cache
from typing import Any

import pyotp
import segno
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from fastapi import HTTPException, Request
from itsdangerous import BadSignature, URLSafeTimedSerializer

from app import crypto
from app.settings import Settings

log = logging.getLogger("auth")

COOKIE_NAME = "ua_session"
CSRF_HEADER = "x-csrf-token"
CSRF_FIELD = "csrf_token"
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
# /oauth/start is NOT public: it needs a login. The callback must be public, because the
# SameSite=Strict session cookie is not sent on the redirect back from Upwork.
PUBLIC_PATHS = {
    "/healthz",
    "/login",
    "/setup/admin",
    "/setup/admin/totp",
    "/setup/admin/confirm",
    "/oauth/client-metadata.json",
    "/oauth/callback",
}
ISSUER = "Upwork Assistant"

_hasher = PasswordHasher()


def utcnow() -> datetime:
    """Wrapped so tests can move the clock."""
    return datetime.now(UTC)


# ---------- password and TOTP ----------


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(hashed: str, password: str) -> bool:
    try:
        return _hasher.verify(hashed, password)
    except (VerificationError, InvalidHashError):
        return False


@cache
def _dummy_hash() -> str:
    # Checked when the username is wrong, so the timing does not tell which part failed.
    return _hasher.hash(secrets.token_hex(16))


def new_totp_secret() -> str:
    return pyotp.random_base32()


def provisioning_uri(secret: str, username: str) -> str:
    return pyotp.TOTP(secret).provisioning_uri(name=username, issuer_name=ISSUER)


def qr_svg(data: str) -> str:
    """Inline SVG QR code, made on the server. Black on white with a 4-module quiet zone,
    so it scans in dark mode too. No JS, no external service."""
    qr = segno.make(data, error="m")
    return qr.svg_inline(
        scale=1,
        border=4,
        dark="#000",
        light="#fff",
        omitsize=True,
        title="QR code for your authenticator app",
        desc="Scan this code with an authenticator app to add the account.",
    )


def verify_totp(secret: str, code: str) -> bool:
    code = "".join(code.split())
    if len(code) != 6 or not code.isdigit():
        return False
    return pyotp.TOTP(secret).verify(code, valid_window=1)


# ---------- admin row ----------


def admin_exists(conn: sqlite3.Connection) -> bool:
    return conn.execute("SELECT 1 FROM admin WHERE id = 1").fetchone() is not None


def get_admin(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM admin WHERE id = 1").fetchone()


def create_admin(
    conn: sqlite3.Connection, username: str, password_hash: str, totp_secret_enc: str
) -> None:
    """Raises sqlite3.IntegrityError if an admin already exists. The secret is pre-encrypted."""
    conn.execute(
        "INSERT INTO admin(id, username, password_hash, totp_secret_enc, created_at) "
        "VALUES (1, ?, ?, ?, ?)",
        (username, password_hash, totp_secret_enc, utcnow().isoformat(timespec="seconds")),
    )


# ---------- one-time setup code (plan D0) ----------


def _setup_code_hash(secret_key: str, code: str) -> str:
    return hmac.new(
        crypto.derive_key(secret_key, "setup-code"), code.strip().encode(), "sha256"
    ).hexdigest()


def issue_setup_code(conn: sqlite3.Connection, secret_key: str) -> str:
    """Make a new random code, store only its hash, return the plain code (to print once)."""
    code = secrets.token_urlsafe(12)
    conn.execute(
        "INSERT INTO setup_code(id, code_hash, created_at) VALUES (1, ?, ?) "
        "ON CONFLICT(id) DO UPDATE SET code_hash = excluded.code_hash, "
        "created_at = excluded.created_at",
        (_setup_code_hash(secret_key, code), utcnow().isoformat(timespec="seconds")),
    )
    return code


def check_setup_code(conn: sqlite3.Connection, secret_key: str, code: str) -> bool:
    row = conn.execute("SELECT code_hash FROM setup_code WHERE id = 1").fetchone()
    if row is None or not code.strip():
        return False
    return hmac.compare_digest(row["code_hash"], _setup_code_hash(secret_key, code))


def finish_setup(conn: sqlite3.Connection) -> None:
    """The admin exists: drop the code and clear any lock caused by wrong setup codes."""
    conn.execute("DELETE FROM setup_code WHERE id = 1")
    record_success(conn)


# ---------- lockout (stored in the DB) ----------


def lock_seconds_remaining(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT locked_until FROM auth_state WHERE id = 1").fetchone()
    if row is None or not row["locked_until"]:
        return 0
    left = (datetime.fromisoformat(row["locked_until"]) - utcnow()).total_seconds()
    return max(0, math.ceil(left))


def record_failure(conn: sqlite3.Connection, settings: Settings) -> int:
    """Count one wrong try. Returns the lock time left in seconds (0 if not locked)."""
    left = lock_seconds_remaining(conn)
    if left:
        return left
    max_tries = settings.get("security.max_login_tries")
    minutes = settings.get("security.lockout_minutes")
    conn.execute("UPDATE auth_state SET locked_until = NULL WHERE id = 1")  # clear an expired lock
    conn.execute("UPDATE auth_state SET failed_count = failed_count + 1 WHERE id = 1")
    count = conn.execute("SELECT failed_count FROM auth_state WHERE id = 1").fetchone()[0]
    if count >= max_tries:
        until = utcnow() + timedelta(minutes=minutes)
        conn.execute(
            "UPDATE auth_state SET failed_count = 0, locked_until = ? WHERE id = 1",
            (until.isoformat(),),
        )
        log.warning("login locked for %s minutes after %s wrong tries", minutes, count)
        return lock_seconds_remaining(conn)
    return 0


def record_success(conn: sqlite3.Connection) -> None:
    conn.execute("UPDATE auth_state SET failed_count = 0, locked_until = NULL WHERE id = 1")


@dataclass(frozen=True)
class LoginResult:
    status: str  # "ok" | "bad" | "locked"
    locked_seconds: int = 0


def attempt_login(
    conn: sqlite3.Connection,
    settings: Settings,
    secret_key: str,
    username: str,
    password: str,
    code: str,
) -> LoginResult:
    left = lock_seconds_remaining(conn)
    if left:
        return LoginResult("locked", left)
    admin = get_admin(conn)
    stored_hash = admin["password_hash"] if admin else _dummy_hash()
    pw_ok = verify_password(stored_hash, password)
    user_ok = admin is not None and hmac.compare_digest(
        username.strip().encode(), admin["username"].encode()
    )
    totp_ok = False
    if admin is not None:
        try:
            totp_ok = verify_totp(crypto.decrypt(secret_key, admin["totp_secret_enc"]), code)
        except Exception:  # bad key or damaged row; never log the value
            log.error("could not read the stored TOTP secret (SECRET_KEY changed?)")
    if pw_ok and user_ok and totp_ok:
        record_success(conn)
        return LoginResult("ok")
    left = record_failure(conn, settings)
    return LoginResult("locked" if left else "bad", left)


# ---------- signed session cookie + CSRF ----------


class Session:
    """Small dict in a signed cookie: user name, CSRF token, pending setup data."""

    def __init__(self, data: dict[str, Any] | None = None):
        self.data: dict[str, Any] = data or {}
        self.dirty = False

    @property
    def user(self) -> str | None:
        return self.data.get("u")

    @property
    def csrf(self) -> str:
        if "csrf" not in self.data:
            self.data["csrf"] = secrets.token_urlsafe(32)
            self.dirty = True
        return self.data["csrf"]

    def set(self, key: str, value: Any) -> None:
        self.data[key] = value
        self.dirty = True

    def pop(self, key: str) -> Any:
        if key in self.data:
            self.dirty = True
        return self.data.pop(key, None)

    def login(self, username: str) -> None:
        """New session on login: a fresh CSRF token, nothing else carried over."""
        self.data = {"u": username, "csrf": secrets.token_urlsafe(32)}
        self.dirty = True

    def logout(self) -> None:
        self.data = {}
        self.dirty = True


def serializer(secret_key: str) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(
        crypto.derive_key(secret_key, "session").hex(), salt="ua-session"
    )


def load_session(request: Request) -> Session:
    existing = getattr(request.state, "session", None)
    if existing is not None:
        return existing
    session = Session()
    raw = request.cookies.get(COOKIE_NAME)
    if raw:
        hours = request.app.state.settings.get("security.session_hours")
        try:
            data = serializer(request.app.state.config.secret_key).loads(
                raw, max_age=hours * 3600
            )
            if isinstance(data, dict):
                session = Session(data)
        except BadSignature:
            pass
    request.state.session = session
    return session


def cookie_value(request: Request, session: Session) -> str:
    return serializer(request.app.state.config.secret_key).dumps(session.data)


def current_user(request: Request) -> str | None:
    """The logged-in admin name, or None. The name must still match the admin row."""
    session = load_session(request)
    if not session.user:
        return None
    admin = get_admin(request.app.state.db)
    if admin is None or admin["username"] != session.user:
        return None
    return session.user


def is_public(path: str) -> bool:
    return path in PUBLIC_PATHS


async def _submitted_csrf(request: Request) -> str:
    header = request.headers.get(CSRF_HEADER)
    if header:
        return header
    ctype = request.headers.get("content-type", "")
    if ctype.startswith(("application/x-www-form-urlencoded", "multipart/form-data")):
        form = await request.form()
        value = form.get(CSRF_FIELD)
        if isinstance(value, str):
            return value
    return ""


async def guard(request: Request) -> None:
    """App-wide dependency. CSRF check on every write, login check on every non-public page."""
    session = load_session(request)
    if request.method not in SAFE_METHODS:
        expected = session.data.get("csrf", "")
        sent = await _submitted_csrf(request)
        if not expected or not sent or not hmac.compare_digest(expected, sent):
            raise HTTPException(status_code=403, detail="CSRF token missing or wrong")
    if is_public(request.url.path):
        return
    if current_user(request) is None:
        if request.headers.get("hx-request"):
            raise HTTPException(status_code=401, headers={"HX-Redirect": "/login"})
        target = "/login" if admin_exists(request.app.state.db) else "/setup/admin"
        raise HTTPException(status_code=303, headers={"Location": target})
