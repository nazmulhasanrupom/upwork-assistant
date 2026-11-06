"""UNUSED direct path, blocked by Upwork allow list. Kept for reference. The app talks to Upwork
through `app/upwork/bridge.py` (headless Claude Code with its own Upwork MCP login).
Only `ToolResult` and the error classes are still imported from here.

Client for the Upwork MCP server over Streamable HTTP (SPEC 3.4, 5, 6.2).

Flow: `initialize` -> read `Mcp-Session-Id` -> `notifications/initialized` -> `tools/call`.
Replies can be plain JSON or an SSE stream; both are parsed and the message with our id wins.

Rules kept here (all numbers come from settings):
- Bearer token from `UpworkOAuth.access_token()`. A 401 refreshes once and retries; a second
  401 raises `UpworkAuthError` ("login expired").
- 429, 5xx and network errors back off (Retry-After wins, capped) up to `mcp_max_tries` tries.
- At most `mcp_calls_per_second` requests per second (this process).
- At most `max_mcp_calls_per_run` tool calls per run. With a `run_id` the count lives in
  `runs.mcp_calls`, so it survives across client objects. Protocol set-up requests
  (initialize, initialized) and retries are not counted; `call_tool` and `list_tools` are.
- `confirm_preview` is refused while the `general.dry_run` setting is on (safety net).
- Tokens are never logged and never put in error messages.
"""

from __future__ import annotations

import email.utils
import json
import logging
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import httpx

from app.settings import Settings
from app.upwork.oauth import OAuthError, UpworkOAuth

log = logging.getLogger("upwork.mcp")

CLIENT_NAME = "upwork-assistant"
CLIENT_VERSION = "0.1.0"
CONFIRM_TOOL = "confirm_preview"
ERROR_TEXT_MAX = 1000


# ---------- errors ----------


class UpworkMCPError(RuntimeError):
    """Base class. Messages are safe to show to the admin."""


class UpworkAuthError(UpworkMCPError):
    """Upwork rejected our login (even after a refresh). The app shows "login expired"."""


class UpworkHTTPError(UpworkMCPError):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


class UpworkProtocolError(UpworkMCPError):
    """The server answered, but not in the shape MCP promises."""


class UpworkRPCError(UpworkMCPError):
    """A JSON-RPC `error` object came back."""

    def __init__(self, code: int | None, message: str):
        super().__init__(f"Upwork MCP error {code}: {message}")
        self.code = code


class UpworkToolError(UpworkMCPError):
    """The tool ran and said `isError: true`. The message is the tool's own text."""


class CallBudgetExceeded(UpworkMCPError):
    """Too many MCP calls in one run (setting `upwork.max_mcp_calls_per_run`)."""


class DryRunBlocked(UpworkMCPError):
    """A write was refused because the dry_run setting is on."""


class _SessionGone(Exception):
    """HTTP 404 while we held a session id: the server forgot the session."""


# ---------- tool result ----------


@dataclass(frozen=True)
class ToolResult:
    name: str
    content: list[dict[str, Any]] = field(default_factory=list)
    structured: Any = None
    is_error: bool = False
    # Set by the Claude bridge on error results: "missing" (Claude skipped the call) or
    # "budget" (the per-run call limit stopped it). Normal tool errors leave it empty.
    error_kind: str | None = None

    def unwrap(self) -> Any:
        """`data()` for a good result. Raises CallBudgetExceeded / UpworkToolError otherwise."""
        if self.is_error:
            text = self.text.strip() or "The tool reported an error."
            if self.error_kind == "budget":
                raise CallBudgetExceeded(text[:ERROR_TEXT_MAX])
            raise UpworkToolError(text[:ERROR_TEXT_MAX])
        return self.data()

    @property
    def text(self) -> str:
        return "\n".join(str(c.get("text", "")) for c in self.content if c.get("type") == "text")

    def data(self) -> Any:
        """structuredContent if the tool sent it, else the text parsed as JSON, else the text."""
        if self.structured is not None:
            return self.structured
        text = self.text.strip()
        if not text:
            return None
        try:
            return json.loads(text)
        except ValueError:
            return text


# ---------- parsing helpers ----------


def parse_sse(body: str) -> list[Any]:
    """Return the JSON value of every SSE event that has data. Bad events are skipped."""
    messages: list[Any] = []
    data_lines: list[str] = []

    def flush() -> None:
        if data_lines:
            raw = "\n".join(data_lines)
            data_lines.clear()
            try:
                messages.append(json.loads(raw))
            except ValueError:
                log.warning("Skipped an SSE event that is not JSON")

    for line in body.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if line == "":
            flush()
        elif line.startswith(":"):
            continue
        elif line.startswith("data:"):
            value = line[5:]
            data_lines.append(value[1:] if value.startswith(" ") else value)
    flush()
    return messages


def _messages(resp: httpx.Response) -> list[dict[str, Any]]:
    ctype = resp.headers.get("content-type", "").split(";")[0].strip().lower()
    if ctype == "text/event-stream":
        raw: list[Any] = parse_sse(resp.text)
    else:
        try:
            body = resp.json()
        except ValueError:
            raise UpworkProtocolError("Upwork MCP did not answer with JSON.") from None
        raw = body if isinstance(body, list) else [body]
    return [m for m in raw if isinstance(m, dict)]


def _retry_after(value: str | None, cap: float) -> float | None:
    if not value:
        return None
    value = value.strip()
    try:
        seconds = float(value)
    except ValueError:
        try:
            when = email.utils.parsedate_to_datetime(value)
            seconds = when.timestamp() - time.time()
        except (TypeError, ValueError):
            return None
    return max(0.0, min(seconds, cap))


# ---------- the client ----------


class UpworkMCP:
    def __init__(
        self,
        settings: Settings,
        oauth: UpworkOAuth,
        conn: sqlite3.Connection,
        run_id: str | None = None,
        *,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        http: httpx.Client | None = None,
    ):
        self.settings = settings
        self.oauth = oauth
        self.conn = conn
        self.run_id = run_id
        self._sleep = sleep
        self._clock = clock
        self._http = http or httpx.Client(timeout=self._s("mcp_timeout_seconds"))
        self._owns_http = http is None
        self._session_id: str | None = None
        self._protocol_version: str | None = None
        self._initialized = False
        self._next_id = 0
        self._last_request: float | None = None
        self._local_calls = 0  # used when there is no run_id

    # -- plumbing --

    def _s(self, name: str) -> Any:
        return self.settings.get(f"upwork.{name}")

    def close(self) -> None:
        if self._owns_http:
            self._http.close()

    def __enter__(self) -> UpworkMCP:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @property
    def calls_made(self) -> int:
        if self.run_id is None:
            return self._local_calls
        row = self.conn.execute(
            "SELECT mcp_calls FROM runs WHERE run_id = ?", (self.run_id,)
        ).fetchone()
        return int(row["mcp_calls"]) if row else 0

    # -- public API --

    def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> ToolResult:
        """Run one tool. Raises UpworkToolError if the tool reports an error."""
        if name == CONFIRM_TOOL and self.settings.get("general.dry_run"):
            log.warning("Dry-run is on: %s was refused", CONFIRM_TOOL)
            raise DryRunBlocked("Dry-run is on, so nothing was confirmed on Upwork.")
        self._spend()
        result = self._rpc("tools/call", {"name": name, "arguments": arguments or {}})
        content = result.get("content")
        tool = ToolResult(
            name=name,
            content=[c for c in content if isinstance(c, dict)]
            if isinstance(content, list)
            else [],
            structured=result.get("structuredContent"),
            is_error=bool(result.get("isError")),
        )
        if tool.is_error:
            text = tool.text.strip() or "The tool reported an error."
            raise UpworkToolError(text[:ERROR_TEXT_MAX])
        return tool

    def call_json(self, name: str, arguments: dict[str, Any] | None = None) -> Any:
        """Shortcut: `call_tool(...).data()`."""
        return self.call_tool(name, arguments).data()

    def list_tools(self) -> list[dict[str, Any]]:
        """tools/list, following `nextCursor`. Each page counts as one call."""
        tools: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            self._spend()
            result = self._rpc("tools/list", {"cursor": cursor} if cursor else {})
            page = result.get("tools")
            if not isinstance(page, list):
                raise UpworkProtocolError("Upwork MCP tools/list had no tools list.")
            tools.extend(t for t in page if isinstance(t, dict))
            cursor = result.get("nextCursor")
            if not cursor:
                return tools

    # -- budget --

    def _spend(self) -> None:
        limit = int(self._s("max_mcp_calls_per_run"))
        if self.run_id is None:
            if self._local_calls >= limit:
                raise CallBudgetExceeded(f"MCP call limit reached ({limit} per run).")
            self._local_calls += 1
            return
        cur = self.conn.execute(
            "UPDATE runs SET mcp_calls = mcp_calls + 1 WHERE run_id = ? AND mcp_calls < ?",
            (self.run_id, limit),
        )
        if cur.rowcount == 1:
            return
        exists = self.conn.execute("SELECT 1 FROM runs WHERE run_id = ?", (self.run_id,)).fetchone()
        if exists is None:
            raise UpworkMCPError(f"Unknown run id {self.run_id!r}: no row in the runs table.")
        raise CallBudgetExceeded(f"MCP call limit reached ({limit} per run).")

    # -- JSON-RPC --

    def _rpc(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        """Send a request inside a session. A forgotten session is rebuilt once."""
        for attempt in (1, 2):
            self._ensure_session()
            try:
                return self._request(method, params)
            except _SessionGone:
                self._reset_session()
                if attempt == 2:
                    raise UpworkHTTPError(404, "Upwork MCP lost the session twice.") from None
                log.info("MCP session expired, starting a new one")
        raise AssertionError("unreachable")

    def _reset_session(self) -> None:
        self._session_id = None
        self._protocol_version = None
        self._initialized = False

    def _ensure_session(self) -> None:
        if self._initialized:
            return
        resp, result = self._request_raw(
            "initialize",
            {
                "protocolVersion": self._s("mcp_protocol_version"),
                "capabilities": {},
                "clientInfo": {"name": CLIENT_NAME, "version": CLIENT_VERSION},
            },
        )
        self._session_id = resp.headers.get("Mcp-Session-Id") or None
        negotiated = result.get("protocolVersion")
        if not (isinstance(negotiated, str) and negotiated):
            negotiated = self._s("mcp_protocol_version")
        self._protocol_version = negotiated
        try:
            self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})
        except _SessionGone:
            self._reset_session()
            raise UpworkHTTPError(404, "Upwork MCP lost the session during start-up.") from None
        self._initialized = True

    def _request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        return self._request_raw(method, params)[1]

    def _request_raw(self, method: str, params: dict[str, Any]) -> tuple[httpx.Response, dict]:
        self._next_id += 1
        rid = self._next_id
        resp = self._post({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        for msg in _messages(resp):
            if msg.get("id") != rid or ("result" not in msg and "error" not in msg):
                continue
            if "error" in msg:
                err = msg["error"] if isinstance(msg["error"], dict) else {}
                code = err.get("code")
                text = str(err.get("message", "unknown error"))[:ERROR_TEXT_MAX]
                raise UpworkRPCError(code if isinstance(code, int) else None, text)
            result = msg["result"]
            if not isinstance(result, dict):
                raise UpworkProtocolError("Upwork MCP result is not an object.")
            return resp, result
        raise UpworkProtocolError(f"Upwork MCP gave no answer for {method}.")

    # -- HTTP --

    def _headers(self, token: str, body: dict[str, Any]) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
        }
        if body.get("method") != "initialize":
            if self._protocol_version:
                headers["MCP-Protocol-Version"] = self._protocol_version
            if self._session_id:
                headers["Mcp-Session-Id"] = self._session_id
        return headers

    def _throttle(self) -> None:
        per_second = float(self._s("mcp_calls_per_second"))
        if per_second > 0 and self._last_request is not None:
            wait = self._last_request + 1.0 / per_second - self._clock()
            if wait > 0:
                self._sleep(wait)
        self._last_request = self._clock()

    def _token(self, rejected: str | None = None) -> str:
        try:
            return self.oauth.access_token(rejected=rejected)
        except OAuthError as e:
            raise UpworkAuthError(f"Upwork login expired or missing: {e}") from None

    def _post(self, body: dict[str, Any]) -> httpx.Response:
        url = self._s("mcp_url")
        max_tries = max(1, int(self._s("mcp_max_tries")))
        backoff = float(self._s("mcp_backoff_seconds"))
        cap = float(self._s("mcp_retry_after_max_seconds"))
        token = self._token()
        refreshed = False
        tries = 0
        while True:
            self._throttle()
            tries += 1
            try:
                resp = self._http.post(url, json=body, headers=self._headers(token, body))
            except httpx.HTTPError:
                resp = None  # network problem: treated like a 5xx
            status = resp.status_code if resp is not None else 0

            if status == 401:
                if refreshed:
                    raise UpworkAuthError("Upwork rejected the login. Connect Upwork again.")
                refreshed = True
                token = self._token(rejected=token)
                tries -= 1  # a refresh is not a failed try
                continue
            if status == 404 and self._session_id and body.get("method") != "initialize":
                raise _SessionGone
            if resp is not None and 200 <= status < 300:
                return resp
            if resp is None or status == 429 or status >= 500:
                if tries >= max_tries:
                    what = "could not be reached" if resp is None else f"answered HTTP {status}"
                    raise UpworkHTTPError(status, f"Upwork MCP {what} after {tries} tries.")
                wait = _retry_after(resp.headers.get("Retry-After") if resp else None, cap)
                if wait is None:
                    wait = min(backoff * (2 ** (tries - 1)), cap)
                log.warning(
                    "Upwork MCP HTTP %s, waiting %.1fs (try %d/%d)", status, wait, tries, max_tries
                )
                self._sleep(wait)
                continue
            raise UpworkHTTPError(status, f"Upwork MCP refused the request (HTTP {status}).")
