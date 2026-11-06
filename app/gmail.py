"""Gmail through "Sign in with Google" (plan M1). Web-app OAuth with PKCE, send only.

- The Google OAuth client id is a setting. The client secret is a setting too, but encrypted with
  app/crypto.py, and it is never shown again.
- `state` and the PKCE verifier live in the DB table `oauth_state` (like the Upwork flow in
  app/upwork/oauth.py): the redirect back from Google drops the SameSite=Strict cookie.
  Gmail states start with "gm-" so they cannot mix with other flows. One use, 10 minutes.
- Tokens live in `${DATA_DIR}/gmail.enc`: one Fernet-encrypted JSON blob, written atomically
  (temp file + rename), mode 0600. A lock file keeps the panel and the tasks (separate processes)
  from refreshing at the same moment.
- Tokens, codes and the client secret are never logged and never put in error messages.
- Scopes: gmail.send plus `openid email`, only to read the address from the id_token claims.
  The id_token came straight from Google's token endpoint over TLS, so it is not verified again.
"""

from __future__ import annotations

import base64
import fcntl
import json
import logging
import os
import secrets
import sqlite3
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from pathlib import Path
from urllib.parse import urlencode

import httpx

from app import crypto
from app.config import Config
from app.settings import Settings
from app.upwork.oauth import challenge_for, new_verifier

log = logging.getLogger("gmail")

STATE_PREFIX = "gm-"
SEND_SCOPE = "https://www.googleapis.com/auth/gmail.send"
ALERT_EXPIRED = "gmail_expired"
EXPIRED_MESSAGE = "Gmail login expired. Connect Gmail again on the Email page."


class GmailError(RuntimeError):
    """A problem that is safe to show to the admin (never contains a token, code or secret)."""


class GmailAuthExpired(GmailError):
    """Google refused the refresh token (invalid_grant): the admin must connect again."""


def utcnow() -> datetime:
    """Wrapped so tests can move the clock."""
    return datetime.now(UTC)


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat(timespec="seconds")


# ---------- the message ----------


def build_message(
    sender: str, to: str, subject: str, text: str, html: str | None = None
) -> EmailMessage:
    """multipart/alternative: plain text first, HTML second. Headers with a newline are refused."""
    msg = EmailMessage()
    msg["To"] = to
    if sender:
        msg["From"] = sender
    msg["Subject"] = subject
    msg.set_content(text)
    if html:
        msg.add_alternative(html, subtype="html")
    return msg


def raw_message(msg: EmailMessage) -> str:
    """base64url of the RFC 2822 bytes, the form `users.messages.send` wants."""
    return base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")


def email_from_id_token(id_token: str) -> str:
    """The `email` claim. No signature check: the token came from Google's token endpoint."""
    try:
        payload = id_token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        email = claims.get("email", "")
    except Exception:
        return ""
    return email if isinstance(email, str) and "@" in email else ""


# ---------- token file ----------


@dataclass(frozen=True)
class GmailTokens:
    refresh_token: str
    access_token: str = ""
    expires_at: str = ""  # ISO-8601 UTC, of the access token
    address: str = ""

    def access_valid(self, skew: int) -> bool:
        if not self.access_token or not self.expires_at:
            return False
        return datetime.fromisoformat(self.expires_at) - utcnow() > timedelta(seconds=skew)


class TokenFile:
    def __init__(self, path: Path, secret_key: str):
        self.path = Path(path)
        self._key = secret_key

    def exists(self) -> bool:
        return self.path.exists()

    def load(self) -> GmailTokens | None:
        """None when not connected or the file cannot be read (wrong SECRET_KEY, damaged)."""
        try:
            raw = self.path.read_text(encoding="ascii")
        except FileNotFoundError:
            return None
        try:
            data = json.loads(crypto.decrypt(self._key, raw.strip()))
            return GmailTokens(
                refresh_token=str(data["refresh_token"]),
                access_token=str(data.get("access_token", "")),
                expires_at=str(data.get("expires_at", "")),
                address=str(data.get("address", "")),
            )
        except Exception:  # never include the content in the message
            log.warning("gmail.enc cannot be read (SECRET_KEY changed or file damaged)")
            return None

    def save(self, tokens: GmailTokens) -> None:
        """Atomic: temp file in the same folder (mode 0600), flush, fsync, rename."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        blob = crypto.encrypt(self._key, json.dumps(asdict(tokens)))
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".gmail-", suffix=".tmp")
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="ascii") as f:
                f.write(blob)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    def delete(self) -> None:
        self.path.unlink(missing_ok=True)

    @contextmanager
    def lock(self) -> Iterator[None]:
        lock_path = self.path.with_name(self.path.name + ".lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)


# ---------- the client ----------


class Gmail:
    def __init__(self, config: Config, settings: Settings, conn: sqlite3.Connection):
        self.config = config
        self.settings = settings
        self.conn = conn
        self.store = TokenFile(config.gmail_path, config.secret_key)

    def _s(self, name: str):
        return self.settings.get(f"email.{name}")

    # -- client credentials --

    @property
    def client_id(self) -> str:
        return str(self._s("gmail_client_id")).strip()

    def client_secret(self) -> str:
        blob = self._s("gmail_client_secret_enc")
        if not blob:
            return ""
        try:
            return crypto.decrypt(self.config.secret_key, blob)
        except Exception:
            raise GmailError(
                "The saved Google client secret cannot be read. SECRET_KEY may have changed. "
                "Enter the secret again."
            ) from None

    def has_client(self) -> bool:
        return bool(self.client_id and self._s("gmail_client_secret_enc"))

    def save_client(self, client_id: str, secret: str) -> None:
        """An empty `secret` keeps the saved one."""
        self.settings.set("email.gmail_client_id", client_id.strip())
        if secret:
            self.settings.set(
                "email.gmail_client_secret_enc", crypto.encrypt(self.config.secret_key, secret)
            )

    @property
    def redirect_uri(self) -> str:
        return f"{self.require_base_url()}/gmail/callback"

    def require_base_url(self) -> str:
        if not self.config.public_base_url:
            raise GmailError("PUBLIC_BASE_URL is not set. Google needs your public HTTPS address.")
        return self.config.public_base_url

    # -- status --

    def connected(self) -> bool:
        return self.store.load() is not None

    def address(self) -> str:
        tokens = self.store.load()
        return (tokens.address if tokens else "") or str(self._s("gmail_address"))

    def recipient(self) -> str:
        return str(self._s("gmail_recipient")).strip() or self.address()

    # -- step 1: start --

    def start(self) -> str:
        """Save state + verifier, return the Google consent URL."""
        if not self.has_client():
            raise GmailError("Save the Google client ID and secret first.")
        redirect_uri = self.redirect_uri
        self._purge_state()
        state = STATE_PREFIX + secrets.token_urlsafe(32)
        verifier = new_verifier()
        self.conn.execute(
            "INSERT INTO oauth_state(state, verifier, created_at) VALUES (?, ?, ?)",
            (state, verifier, _iso(utcnow())),
        )
        params = {
            "response_type": "code",
            "client_id": self.client_id,
            "redirect_uri": redirect_uri,
            "scope": self._s("gmail_scope"),
            "access_type": "offline",
            "prompt": "consent",
            "include_granted_scopes": "false",
            "state": state,
            "code_challenge": challenge_for(verifier),
            "code_challenge_method": "S256",
        }
        return f"{self._s('gmail_auth_url')}?{urlencode(params)}"

    def _purge_state(self) -> None:
        cutoff = utcnow() - timedelta(minutes=self._s("gmail_state_minutes"))
        self.conn.execute("DELETE FROM oauth_state WHERE created_at < ?", (_iso(cutoff),))

    def consume_state(self, state: str) -> str:
        """Check `state` against the DB. One use: the row is deleted. Returns the verifier."""
        bad = GmailError("This connect link is not known or was already used. Start again.")
        if not state.startswith(STATE_PREFIX):
            raise bad
        row = self.conn.execute(
            "SELECT verifier, created_at FROM oauth_state WHERE state = ?", (state,)
        ).fetchone()
        if row is None:
            raise bad
        if self.conn.execute("DELETE FROM oauth_state WHERE state = ?", (state,)).rowcount != 1:
            raise bad  # another request used it first
        age = utcnow() - datetime.fromisoformat(row["created_at"])
        if age > timedelta(minutes=self._s("gmail_state_minutes")):
            raise GmailError("This connect link has expired. Start again.")
        return row["verifier"]

    # -- step 2: callback --

    def finish(self, state: str, code: str) -> str:
        """Exchange the code, save the tokens. Returns the connected address."""
        verifier = self.consume_state(state)
        if not code:
            raise GmailError("Google sent no authorization code.")
        body = self._post_token(
            {
                "grant_type": "authorization_code",
                "code": code,
                "code_verifier": verifier,
                "redirect_uri": self.redirect_uri,
                "client_id": self.client_id,
                "client_secret": self.client_secret(),
            }
        )
        granted = str(body.get("scope", "")).split()
        if SEND_SCOPE not in granted:
            raise GmailError(
                "Google did not grant the permission to send email. "
                "Connect again and keep every box ticked."
            )
        refresh = body.get("refresh_token")
        if not refresh:
            raise GmailError(
                "Google sent no refresh token. Remove this app at "
                "myaccount.google.com/permissions and connect again."
            )
        address = email_from_id_token(str(body.get("id_token", "")))
        tokens = GmailTokens(
            refresh_token=str(refresh),
            access_token=str(body["access_token"]),
            expires_at=self._expiry(body),
            address=address,
        )
        with self.store.lock():
            self.store.save(tokens)
        if address:
            self.settings.set("email.gmail_address", address)
        self.conn.execute("DELETE FROM alerts WHERE key = ?", (ALERT_EXPIRED,))
        log.info("Gmail connected (token saved)")
        return address

    # -- access token --

    def access_token(self, rejected: str | None = None) -> str:
        """A valid access token, refreshed under the file lock when it ends soon (or when Google
        just rejected `rejected`). Raises GmailAuthExpired on invalid_grant."""
        skew = self._s("gmail_refresh_skew_seconds")
        with self.store.lock():
            tokens = self.store.load()
            if tokens is None:
                raise GmailError("Gmail is not connected.")
            if tokens.access_valid(skew) and tokens.access_token != rejected:
                return tokens.access_token
            return self._refresh_locked(tokens).access_token

    def _refresh_locked(self, tokens: GmailTokens) -> GmailTokens:
        try:
            body = self._post_token(
                {
                    "grant_type": "refresh_token",
                    "refresh_token": tokens.refresh_token,
                    "client_id": self.client_id,
                    "client_secret": self.client_secret(),
                }
            )
        except GmailAuthExpired:
            self.mark_disconnected()
            raise
        fresh = GmailTokens(
            refresh_token=str(body.get("refresh_token") or tokens.refresh_token),
            access_token=str(body["access_token"]),
            expires_at=self._expiry(body),
            address=tokens.address,
        )
        self.store.save(fresh)
        return fresh

    def mark_disconnected(self) -> None:
        """Google ended the login: drop the tokens and put an alert on the dashboard."""
        self.store.delete()
        self.conn.execute(
            "INSERT INTO alerts(key, message, created_at) VALUES (?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET message = excluded.message",
            (ALERT_EXPIRED, EXPIRED_MESSAGE, _iso(utcnow())),
        )
        log.warning("Gmail login expired (invalid_grant): disconnected")

    # -- send --

    def send(self, to: str, subject: str, text: str, html: str | None = None) -> None:
        """Send one email as the connected account. Raises GmailError (safe message)."""
        if not to:
            raise GmailError("There is no recipient address.")
        try:
            msg = build_message(self.address(), to, subject, text, html)
            raw = raw_message(msg)
        except (ValueError, TypeError):
            raise GmailError("The email could not be built (bad address or subject).") from None
        token = self.access_token()
        for attempt in (1, 2):
            r = self._post_send(raw, token)
            if r.status_code == 401 and attempt == 1:  # token ended early: refresh once
                token = self.access_token(rejected=token)
                continue
            break
        if r.status_code >= 400:
            raise GmailError(_describe_error("Gmail refused the email", r))
        log.info("Gmail email sent")

    def _post_send(self, raw: str, token: str) -> httpx.Response:
        try:
            return httpx.post(
                self._s("gmail_send_url"),
                json={"raw": raw},
                headers={"Authorization": f"Bearer {token}"},
                timeout=self._s("gmail_timeout_seconds"),
            )
        except httpx.HTTPError:
            raise GmailError("Could not reach Gmail. Try again in a moment.") from None

    # -- disconnect --

    def disconnect(self) -> bool:
        """Revoke at Google, then delete the token file even if revoke failed. True if revoked."""
        ok = True
        with self.store.lock():
            tokens = self.store.load()
            if tokens is not None:
                ok = self._revoke(tokens.refresh_token)
            self.store.delete()
        self.settings.set("email.gmail_address", "")
        self.conn.execute("DELETE FROM alerts WHERE key = ?", (ALERT_EXPIRED,))
        return ok

    def _revoke(self, token: str) -> bool:
        try:
            r = httpx.post(
                self._s("gmail_revoke_url"),
                data={"token": token},
                timeout=self._s("gmail_timeout_seconds"),
            )
        except httpx.HTTPError:
            log.warning("Google revoke call failed (network)")
            return False
        if r.status_code >= 400:
            log.warning("Google revoke call refused (HTTP %s)", r.status_code)
            return False
        return True

    # -- helpers --

    def _expiry(self, body: dict) -> str:
        try:
            seconds = int(body.get("expires_in") or 3600)
        except (TypeError, ValueError):
            seconds = 3600
        return _iso(utcnow() + timedelta(seconds=seconds))

    def _post_token(self, data: dict[str, str]) -> dict:
        try:
            r = httpx.post(
                self._s("gmail_token_url"),
                data=data,
                headers={"Accept": "application/json"},
                timeout=self._s("gmail_timeout_seconds"),
            )
        except httpx.HTTPError:
            raise GmailError("Could not reach Google. Try again in a moment.") from None
        try:
            body = r.json()
        except ValueError:
            body = None
        if r.status_code >= 400 or not isinstance(body, dict):
            if isinstance(body, dict) and body.get("error") == "invalid_grant":
                raise GmailAuthExpired(EXPIRED_MESSAGE)
            raise GmailError(_describe_error("Google refused the request", r, body))
        if not body.get("access_token"):
            raise GmailError("Google answered without an access token.")
        return body


def _describe_error(prefix: str, r: httpx.Response, body: object = None) -> str:
    """Only the standard error fields, cut short. Never the whole body."""
    if body is None:
        try:
            body = r.json()
        except ValueError:
            body = None
    err = desc = ""
    if isinstance(body, dict):
        e = body.get("error")
        if isinstance(e, dict):  # Google API style: {"error": {"message": ...}}
            desc = str(e.get("message", ""))[:200]
        elif isinstance(e, str):
            err = e[:60]
            d = body.get("error_description")
            desc = d[:200] if isinstance(d, str) else ""
    detail = ": ".join(x for x in (err, desc) if x)
    return f"{prefix} (HTTP {r.status_code})" + (f": {detail}" if detail else ".")
