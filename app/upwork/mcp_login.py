"""One-click Upwork login from the panel (plan P5).

`claude mcp login upwork --no-browser` needs a terminal. We start it in a pty, read the Upwork
authorize link from its output, and show the link to the admin. Upwork sends the browser to
`http://localhost:<port>/callback?code=...&state=...`; that page does not load on the user's
machine (the port is inside the container), so the admin copies the address and pastes it back.
We check it, type it into the pty, and wait for the process to exit (exit code 0 = logged in).

Rules kept here:
- One session at a time. Starting a new one kills the old one. A session dies after 10 minutes.
- The pasted address is checked before it is typed: scheme http, host localhost or 127.0.0.1,
  the port and path /callback of this session, a `code`, and the same `state` as the link.
- Nothing from the process output is shown without cleaning: ANSI/OSC codes removed, links and
  long token-like strings hidden. Pasted values and tokens are never logged.
"""

from __future__ import annotations

import fcntl
import logging
import os
import re
import signal
import struct
import subprocess
import termios
import threading
import time
from dataclasses import dataclass
from urllib.parse import parse_qs, urlparse

log = logging.getLogger("upwork.mcp_login")

COMMAND = ["claude", "mcp", "login", "upwork", "--no-browser"]
URL_WAIT_SECONDS = 30
FINISH_WAIT_SECONDS = 60
SESSION_SECONDS = 600
MAX_BUFFER = 64 * 1024
MAX_PASTE = 4000
MAX_SHOWN_LINES = 4
MAX_SHOWN_CHARS = 300

AUTH_HOST = "www.upwork.com"
AUTH_PATH_PART = "/oauth2/authorize"

# OSC: ESC ] ... (BEL | ESC \). CSI: ESC [ ... final byte. Other two-byte escapes.
_OSC = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?")
_CSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_ESC2 = re.compile(r"\x1b[@-Z\\-_=>78]")
_OSC8_URL = re.compile(r"\x1b\]8;[^;\x07\x1b]*;(https://[^\x07\x1b]+)(?:\x07|\x1b\\)")
_TEXT_URL = re.compile(r"https://www\.upwork\.com/\S+(?=\s)")
_LINK = re.compile(r"https?://\S+")
_TOKENISH = re.compile(r"[A-Za-z0-9_\-+/=.]{32,}")


class LoginError(RuntimeError):
    """The message is safe to show to the admin."""


def clean_text(raw: str) -> str:
    """Drop OSC/ANSI escapes and carriage returns; keep readable text."""
    text = _OSC.sub("", raw)
    text = _CSI.sub("", text)
    text = _ESC2.sub("", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return "".join(ch for ch in text if ch == "\n" or ch == "\t" or ch >= " ")


def safe_lines(raw: str, limit: int = MAX_SHOWN_LINES) -> str:
    """The last few cleaned lines, with links and token-like strings hidden."""
    lines = []
    for line in clean_text(raw).split("\n"):
        line = _LINK.sub("[link]", line)
        line = _TOKENISH.sub("[hidden]", line).strip()
        if line:
            lines.append(line)
    return " | ".join(lines[-limit:])[:MAX_SHOWN_CHARS]


def extract_authorize_url(raw: str) -> str | None:
    """The Upwork authorize link from raw pty output, or None if it is not complete yet."""
    candidates = [m.group(1) for m in _OSC8_URL.finditer(raw)]
    candidates += [m.group(0) for m in _TEXT_URL.finditer(clean_text(raw))]
    for url in candidates:
        parts = urlparse(url)
        if parts.scheme == "https" and parts.hostname == AUTH_HOST and "authorize" in parts.path:
            return url
    return None


@dataclass(frozen=True)
class AuthLink:
    url: str
    port: int
    state: str


def parse_authorize_url(url: str) -> AuthLink:
    """Check the link and read the callback port and `state` from it."""
    parts = urlparse(url)
    if parts.scheme != "https" or parts.hostname != AUTH_HOST or AUTH_PATH_PART not in parts.path:
        raise LoginError("Claude printed a link that is not an Upwork login link.")
    query = parse_qs(parts.query)
    redirect = urlparse((query.get("redirect_uri") or [""])[0])
    try:
        port = redirect.port
    except ValueError:
        port = None
    state = (query.get("state") or [""])[0]
    if redirect.hostname not in ("localhost", "127.0.0.1") or not port or not state:
        raise LoginError("The Upwork login link has no usable localhost callback.")
    return AuthLink(url=url, port=port, state=state)


def validate_pasted(pasted: str, link: AuthLink) -> str:
    """Return the cleaned pasted address, or raise LoginError (message is safe to show)."""
    text = (pasted or "").strip()
    if not text or len(text) > MAX_PASTE or any(ch.isspace() or ord(ch) < 32 for ch in text):
        raise LoginError("Paste the full address from the browser, as one line.")
    try:
        parts = urlparse(text)
        port = parts.port
    except ValueError:
        raise LoginError("That does not look like a web address.") from None
    if parts.scheme != "http":
        raise LoginError("The address must start with http:// (the page that did not load).")
    if parts.hostname not in ("localhost", "127.0.0.1"):
        raise LoginError("The address must be the localhost page, not an Upwork page.")
    if port != link.port:
        raise LoginError("The port is different from the one in this login. Start again.")
    if parts.path != "/callback":
        raise LoginError("The address must end with /callback?code=...&state=...")
    query = parse_qs(parts.query)
    if not (query.get("code") or [""])[0]:
        raise LoginError("The address has no code. Copy the whole address, after you approve.")
    sent_state = (query.get("state") or [""])[0]
    if not sent_state:
        raise LoginError("The address has no state. Copy the whole address.")
    if sent_state != link.state:
        raise LoginError("This address belongs to a different login. Start again.")
    return text


class _Session:
    def __init__(self, env: dict[str, str], cwd: str, session_seconds: float):
        self.lock = threading.Lock()
        self.buffer = ""
        self.done = threading.Event()
        self.returncode: int | None = None
        self.started = time.monotonic()
        self.expires_at = time.time() + session_seconds
        self.link: AuthLink | None = None
        self.finishing = False
        self.master, slave = os.openpty()
        try:
            # Wide terminal: the long link must not be wrapped by the child.
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 50, 500, 0, 0))
            self.proc = subprocess.Popen(
                COMMAND,
                stdin=slave,
                stdout=slave,
                stderr=slave,
                env=env,
                cwd=cwd,
                start_new_session=True,
                close_fds=True,
            )
        except BaseException:
            os.close(self.master)
            raise
        finally:
            os.close(slave)
        self.timer = threading.Timer(session_seconds, self.kill)
        self.timer.daemon = True
        self.timer.start()
        threading.Thread(target=self._read, daemon=True, name="mcp-login-read").start()

    def _read(self) -> None:
        while True:
            try:
                chunk = os.read(self.master, 4096)
            except OSError:
                break
            if not chunk:
                break
            with self.lock:
                self.buffer = (self.buffer + chunk.decode("utf-8", "replace"))[-MAX_BUFFER:]
        try:
            self.returncode = self.proc.wait()
        finally:
            self.timer.cancel()
            try:
                os.close(self.master)
            except OSError:
                pass
            self.done.set()

    def output(self) -> str:
        with self.lock:
            return self.buffer

    def alive(self) -> bool:
        return not self.done.is_set()

    def write_line(self, text: str) -> None:
        os.write(self.master, (text + "\r").encode("utf-8"))

    def kill(self) -> None:
        """Kill the whole process group (the child started its own session)."""
        if self.proc.poll() is None:
            try:
                os.killpg(self.proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                try:
                    self.proc.kill()
                except OSError:
                    pass
        self.done.wait(5)


class LoginManager:
    """Holds at most one login session. Safe to call from several threads."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._session: _Session | None = None
        self.url_wait = URL_WAIT_SECONDS
        self.finish_wait = FINISH_WAIT_SECONDS
        self.session_seconds = SESSION_SECONDS

    # ----- state -----

    def active(self) -> AuthLink | None:
        """The link of the running session, or None."""
        with self._lock:
            s = self._session
            if s is not None and s.alive() and s.link is not None:
                return s.link
            return None

    def remaining_seconds(self) -> int:
        with self._lock:
            s = self._session
            if s is None or not s.alive():
                return 0
            return max(0, int(s.expires_at - time.time()))

    # ----- actions -----

    def start(self, env: dict[str, str], cwd: str) -> AuthLink:
        """Kill any old session, start a new one, wait for the Upwork link."""
        with self._lock:
            old, self._session = self._session, None
        if old is not None:
            old.kill()
        try:
            session = _Session(env, cwd, self.session_seconds)
        except FileNotFoundError:
            raise LoginError("The claude program was not found in this container.") from None
        except OSError as e:
            log.error("Could not start claude login: %s", e.strerror or type(e).__name__)
            raise LoginError("Could not start the Claude login.") from None
        deadline = time.monotonic() + self.url_wait
        url = None
        while time.monotonic() < deadline:
            url = extract_authorize_url(session.output())
            if url or session.done.is_set():
                break
            time.sleep(0.05)
        if url is None and session.done.is_set():  # last chance: output arrived with the exit
            url = extract_authorize_url(session.output())
        if url is None:
            timed_out = not session.done.is_set()
            detail = safe_lines(session.output())
            session.kill()
            if timed_out:
                raise LoginError("Claude did not give an Upwork login link in time. Try again.")
            if not detail:
                detail = f"claude stopped with exit code {session.returncode}"
            raise LoginError(f"Claude did not give an Upwork login link: {detail}")
        try:
            link = parse_authorize_url(url)
        except LoginError:
            session.kill()
            raise
        session.link = link
        with self._lock:
            self._session = session
        log.info("Upwork login started (callback port %s)", link.port)
        return link

    def finish(self, pasted: str) -> tuple[bool, str]:
        """Check the pasted address, hand it to claude, wait for the result.

        A bad address returns (False, message) and the session stays open for another try.
        Otherwise the session is over when this returns."""
        with self._lock:
            s = self._session
            if s is None or s.link is None:
                return False, "There is no login in progress. Press Connect Upwork to start."
            if s.finishing:
                return False, "The login is already being finished. Wait a moment."
            link = s.link
        if s.alive():
            try:
                text = validate_pasted(pasted, link)
            except LoginError as e:
                return False, str(e)
        else:
            text = ""
        with self._lock:
            if self._session is not s:
                return False, "This login was replaced by a newer one. Start again."
            if s.finishing:
                return False, "The login is already being finished. Wait a moment."
            s.finishing = True
        try:
            if s.alive():
                try:
                    s.write_line(text)
                except OSError:
                    pass  # the process just ended; the exit code decides
            if not s.done.wait(self.finish_wait):
                s.kill()
                return False, "Claude did not finish the login in time. Try again."
            ok = s.returncode == 0
            if ok:
                log.info("Upwork login finished")
                return True, "Upwork login saved."
            detail = safe_lines(self._without_echo(s.output(), text))
            log.info("Upwork login failed (exit code %s)", s.returncode)
            return False, "The login did not work" + (f": {detail}" if detail else ".")
        finally:
            with self._lock:
                if self._session is s:
                    self._session = None
            s.kill()

    @staticmethod
    def _without_echo(output: str, pasted: str) -> str:
        return output.replace(pasted, "") if pasted else output

    def cancel(self) -> None:
        with self._lock:
            s, self._session = self._session, None
        if s is not None:
            s.kill()
            log.info("Upwork login cancelled")


manager = LoginManager()
