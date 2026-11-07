"""UNUSED direct path, blocked by Upwork allow list. Kept for reference (plan P2).
The app talks to Upwork through `app/upwork/bridge.py` instead.

Upwork OAuth 2 + PKCE for a public client (SPEC 3.6, 13.1). No client secret.

- `client_id` is the URL of our own metadata document: ${panel address}/oauth/client-metadata.json
- `state` and the PKCE verifier live in the DB table `oauth_state`, not in the session cookie.
- Tokens live in `${DATA_DIR}/tokens.enc`: one Fernet-encrypted JSON blob, written atomically
  (temp file + rename) with mode 0600. A lock file keeps the panel and the scheduler from
  refreshing at the same moment (refresh tokens may rotate).
- Tokens and codes are never logged and never put in error messages.
"""

from __future__ import annotations

import base64
import fcntl
import hashlib
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
from pathlib import Path
from urllib.parse import urlencode

import httpx

from app import crypto
from app.base_url import get_base_url
from app.config import Config
from app.settings import Settings

log = logging.getLogger("upwork.oauth")


class OAuthError(RuntimeError):
    """A login problem that is safe to show to the admin (never contains a token or code)."""


class TokenFileError(OAuthError):
    """tokens.enc exists but cannot be read (SECRET_KEY changed, or the file is damaged)."""


def utcnow() -> datetime:
    """Wrapped so tests can move the clock."""
    return datetime.now(UTC)


# ---------- PKCE ----------


def new_verifier() -> str:
    return secrets.token_urlsafe(64)  # 86 characters, RFC 7636 allows 43 to 128


def challenge_for(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


# ---------- token file ----------


@dataclass(frozen=True)
class Tokens:
    access_token: str
    refresh_token: str
    expires_at: str  # ISO-8601 UTC
    scope: str

    @property
    def expires(self) -> datetime:
        return datetime.fromisoformat(self.expires_at)

    def expires_within(self, seconds: int) -> bool:
        return self.expires - utcnow() <= timedelta(seconds=seconds)


class TokenStore:
    def __init__(self, path: Path, secret_key: str):
        self.path = Path(path)
        self._key = secret_key

    def exists(self) -> bool:
        return self.path.exists()

    def load(self) -> Tokens | None:
        """None when not connected. Raises TokenFileError when the file cannot be read."""
        try:
            raw = self.path.read_text(encoding="ascii")
        except FileNotFoundError:
            return None
        try:
            data = json.loads(crypto.decrypt(self._key, raw.strip()))
            return Tokens(
                access_token=str(data["access_token"]),
                refresh_token=str(data.get("refresh_token", "")),
                expires_at=str(data["expires_at"]),
                scope=str(data.get("scope", "")),
            )
        except Exception:  # never include the content in the message
            raise TokenFileError(
                "The saved Upwork login cannot be read. SECRET_KEY may have changed. "
                "Disconnect and connect again."
            ) from None

    def save(self, tokens: Tokens) -> None:
        """Atomic: write a temp file in the same folder (mode 0600), flush, then rename."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        blob = crypto.encrypt(self._key, json.dumps(asdict(tokens)))
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".tokens-", suffix=".tmp")
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
        """Cross-process lock (panel and scheduler are two containers sharing /data)."""
        lock_path = self.path.with_name(self.path.name + ".lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)  # closing releases the lock


# ---------- the OAuth client ----------


@dataclass(frozen=True)
class Status:
    state: str  # "not_connected" | "connected" | "unreadable"
    expires_at: str = ""
    expired: bool = False
    has_refresh_token: bool = False
    scope: str = ""
    message: str = ""


class UpworkOAuth:
    def __init__(self, config: Config, settings: Settings, conn: sqlite3.Connection):
        self.config = config
        self.settings = settings
        self.conn = conn
        self.store = TokenStore(config.tokens_path, config.secret_key)

    # -- identity --

    @property
    def client_id(self) -> str:
        return f"{self.require_base_url()}/oauth/client-metadata.json"

    @property
    def redirect_uri(self) -> str:
        return f"{self.require_base_url()}/oauth/callback"

    def require_base_url(self) -> str:
        base = get_base_url(self.settings, self.config)
        if not base:
            raise OAuthError(
                "The panel address is not known yet. Open the panel on its real address "
                "or set it in Settings."
            )
        return base

    def _s(self, name: str):
        return self.settings.get(f"upwork.{name}")

    def _resource_params(self) -> dict[str, str]:
        return {"resource": self._s("mcp_url")} if self._s("oauth_send_resource") else {}

    # -- step 1: start --

    def start(self) -> str:
        """Save state + verifier, return the Upwork authorize URL to send the browser to."""
        client_id, redirect_uri = self.client_id, self.redirect_uri  # fail early if no base URL
        self._purge_state()
        state = secrets.token_urlsafe(32)
        verifier = new_verifier()
        self.conn.execute(
            "INSERT INTO oauth_state(state, verifier, created_at) VALUES (?, ?, ?)",
            (state, verifier, utcnow().isoformat(timespec="seconds")),
        )
        params = {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "state": state,
            "code_challenge": challenge_for(verifier),
            "code_challenge_method": "S256",
        }
        scope = self._s("oauth_scope").strip()
        if scope:
            params["scope"] = scope
        params.update(self._resource_params())
        return f"{self._s('oauth_authorize_url')}?{urlencode(params)}"

    def _purge_state(self) -> None:
        cutoff = utcnow() - timedelta(minutes=self._s("oauth_state_minutes"))
        self.conn.execute(
            "DELETE FROM oauth_state WHERE created_at < ?", (cutoff.isoformat(timespec="seconds"),)
        )

    def consume_state(self, state: str) -> str:
        """Check `state` against the DB. One use: the row is deleted. Returns the verifier."""
        row = self.conn.execute(
            "SELECT verifier, created_at FROM oauth_state WHERE state = ?", (state,)
        ).fetchone()
        if row is None:
            raise OAuthError("This login link is not known or was already used. Start again.")
        deleted = self.conn.execute("DELETE FROM oauth_state WHERE state = ?", (state,)).rowcount
        if deleted != 1:  # another request used it first
            raise OAuthError("This login link is not known or was already used. Start again.")
        age = utcnow() - datetime.fromisoformat(row["created_at"])
        if age > timedelta(minutes=self._s("oauth_state_minutes")):
            raise OAuthError("This login link has expired. Start again.")
        return row["verifier"]

    # -- step 2: callback --

    def finish(self, state: str, code: str) -> Tokens:
        verifier = self.consume_state(state)
        if not code:
            raise OAuthError("Upwork sent no authorization code.")
        data = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": self.redirect_uri,
            "client_id": self.client_id,
            "code_verifier": verifier,
            **self._resource_params(),
        }
        body = self._post(self._s("oauth_token_url"), data)
        tokens = self._tokens_from(body, old=None)
        self.store.save(tokens)
        log.info("Upwork connected (token saved)")
        return tokens

    # -- refresh --

    def refresh(self) -> Tokens:
        """Always refresh, under the file lock. Raises OAuthError if Upwork refuses."""
        with self.store.lock():
            return self._refresh_locked(self.store.load())

    def _refresh_locked(self, old: Tokens | None) -> Tokens:
        if old is None:
            raise OAuthError("Upwork is not connected.")
        if not old.refresh_token:
            raise OAuthError("There is no refresh token. Connect Upwork again.")
        data = {
            "grant_type": "refresh_token",
            "refresh_token": old.refresh_token,
            "client_id": self.client_id,
            **self._resource_params(),
        }
        body = self._post(self._s("oauth_token_url"), data)
        tokens = self._tokens_from(body, old=old)
        self.store.save(tokens)
        log.info("Upwork token refreshed")
        return tokens

    def access_token(self, rejected: str | None = None) -> str:
        """A valid access token. Refreshes first when it ends soon.

        After a 401, pass the token Upwork rejected as `rejected`: if the file already holds a
        different token (another process refreshed), that one is returned; if it is the same
        token, it is refreshed even when its expiry looks fine. The file is read again under the
        lock, so two processes never refresh at once.
        """
        with self.store.lock():
            tokens = self.store.load()
            if tokens is None:
                raise OAuthError("Upwork is not connected.")
            stale = tokens.expires_within(self._s("oauth_refresh_skew_seconds"))
            if stale or (rejected is not None and tokens.access_token == rejected):
                tokens = self._refresh_locked(tokens)
            return tokens.access_token

    # -- revoke / disconnect --

    def revoke(self) -> bool:
        """Ask Upwork to revoke the login. True if Upwork accepted. Never raises."""
        try:
            tokens = self.store.load()
        except TokenFileError:
            return False
        if tokens is None:
            return True
        token, hint = (
            (tokens.refresh_token, "refresh_token")
            if tokens.refresh_token
            else (tokens.access_token, "access_token")
        )
        try:
            r = httpx.post(
                self._s("oauth_revoke_url"),
                data={"token": token, "token_type_hint": hint, "client_id": self.client_id},
                timeout=self._s("oauth_timeout_seconds"),
            )
        except (httpx.HTTPError, OAuthError):
            log.warning("Upwork revoke call failed (network)")
            return False
        if r.status_code >= 400:
            log.warning("Upwork revoke call refused (HTTP %s)", r.status_code)
            return False
        return True

    def disconnect(self) -> bool:
        """Revoke at Upwork, then delete the token file even if revoke failed."""
        with self.store.lock():
            ok = self.revoke()
            self.store.delete()
        return ok

    # -- status --

    def status(self) -> Status:
        try:
            tokens = self.store.load()
        except TokenFileError as e:
            return Status("unreadable", message=str(e))
        if tokens is None:
            return Status("not_connected")
        return Status(
            "connected",
            expires_at=tokens.expires_at,
            expired=tokens.expires <= utcnow(),
            has_refresh_token=bool(tokens.refresh_token),
            scope=tokens.scope,
        )

    # -- helpers --

    def _post(self, url: str, data: dict[str, str]) -> dict:
        try:
            r = httpx.post(
                url,
                data=data,
                headers={"Accept": "application/json"},
                timeout=self._s("oauth_timeout_seconds"),
            )
        except httpx.HTTPError:
            raise OAuthError("Could not reach Upwork. Try again in a moment.") from None
        try:
            body = r.json()
        except ValueError:
            body = None
        if r.status_code >= 400 or not isinstance(body, dict):
            raise OAuthError(_describe_error(r.status_code, body))
        if not body.get("access_token"):
            raise OAuthError("Upwork answered without an access token.")
        return body

    def _tokens_from(self, body: dict, old: Tokens | None) -> Tokens:
        try:
            seconds = int(body.get("expires_in") or self._s("oauth_default_expires_in"))
        except (TypeError, ValueError):
            seconds = self._s("oauth_default_expires_in")
        expires = utcnow() + timedelta(seconds=seconds)
        refresh = body.get("refresh_token") or (old.refresh_token if old else "")
        scope = body.get("scope") or (old.scope if old else "") or self._s("oauth_scope")
        return Tokens(
            access_token=str(body["access_token"]),
            refresh_token=str(refresh),
            expires_at=expires.isoformat(timespec="seconds"),
            scope=str(scope),
        )


def _describe_error(status: int, body: object) -> str:
    """Only the standard OAuth error fields are shown, cut short. Never the whole body."""
    err = desc = ""
    if isinstance(body, dict):
        e, d = body.get("error"), body.get("error_description")
        err = e[:60] if isinstance(e, str) else ""
        desc = d[:200] if isinstance(d, str) else ""
    detail = ": ".join(x for x in (err, desc) if x)
    if err == "invalid_grant":
        detail += " (the login ended or was revoked: connect Upwork again)"
    return f"Upwork refused the request (HTTP {status})" + (f": {detail}" if detail else ".")
