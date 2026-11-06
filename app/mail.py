"""Emails for the events of SPEC 11 (plan M2).

`send_event` is the one door. It checks the on/off switch (`email.events.<event>`), the dedupe
rules (`emails` table), then sends through Gmail when connected, else through SMTP when
`email.smtp_host` is set, else it skips with a log line. It never raises into the caller: a
failure is logged and shown as a dashboard alert.

Emails are not Upwork writes, so they are sent in dry-run too.
"""

from __future__ import annotations

import html as htmllib
import logging
import re
import smtplib
import sqlite3
from datetime import UTC, datetime, timedelta
from typing import Any

from app.config import Config, load_config
from app.gmail import Gmail, GmailError, build_message
from app.settings import Settings

log = logging.getLogger("mail")

EVENTS = (
    "high_job",
    "invite",
    "message",
    "connects_low",
    "auth_expired",
    "run_failed",
    "cannot_do",
    "digest",
    "weekly",
    "monthly",
)
# Events that may be sent at most once per N hours, whatever the ref_id. N is a setting.
MIN_HOURS_SETTING = {
    "connects_low": "alerts.connects_low_min_hours",
    "auth_expired": "alerts.auth_expired_min_hours",
    "run_failed": "alerts.run_failed_min_hours",
}
ALERT_FAILED = "email_failed"

# send_event results
SENT, OFF, DUPLICATE, NO_TRANSPORT, FAILED = "sent", "off", "duplicate", "no_transport", "failed"


# ---------------------------------------------------------------- the email body


def _clean(value: Any) -> str:
    """One line of plain text: control characters and line breaks become spaces."""
    return re.sub(r"[\x00-\x1f\x7f]+", " ", str(value)).strip()


def build_email(
    title: str,
    lines: list[str],
    *,
    facts: list[tuple[str, str]] | None = None,
    link_path: str = "",
    base_url: str = "",
    button_label: str = "Open in panel",
) -> tuple[str, str]:
    """(plain text, HTML). Inline styles only, no images. Every value is escaped.

    `link_path` is a path in the panel (for example /jobs/123). The button appears only when
    `base_url` (PUBLIC_BASE_URL) is set.
    """
    title = _clean(title)
    lines = [_clean(x) for x in lines if _clean(x)]
    facts = [(_clean(k), _clean(v)) for k, v in (facts or [])]
    url = f"{base_url.rstrip('/')}{link_path}" if base_url else ""

    text_parts = [title, "", *lines]
    if facts:
        text_parts += ["", *(f"{k}: {v}" for k, v in facts)]
    if url:
        text_parts += ["", f"{button_label}: {url}"]
    text = "\n".join(text_parts) + "\n"

    e = htmllib.escape
    body = [
        f'<h1 style="margin:0 0 16px;font-size:20px;line-height:1.3;color:#111827">{e(title)}</h1>'
    ]
    body += [
        f'<p style="margin:0 0 12px;font-size:15px;line-height:1.5;color:#374151">{e(x)}</p>'
        for x in lines
    ]
    if facts:
        rows = "".join(
            '<tr><td style="padding:6px 12px 6px 0;color:#6b7280;font-size:14px;'
            f'vertical-align:top">{e(k)}</td>'
            f'<td style="padding:6px 0;color:#111827;font-size:14px">{e(v)}</td></tr>'
            for k, v in facts
        )
        body.append(
            '<table role="presentation" style="border-collapse:collapse;margin:8px 0 16px">'
            f"{rows}</table>"
        )
    if url:
        body.append(
            f'<p style="margin:20px 0 0"><a href="{e(url, quote=True)}" '
            'style="display:inline-block;'
            "padding:10px 18px;background:#2563eb;color:#ffffff;text-decoration:none;"
            f'border-radius:6px;font-size:15px;font-weight:600">{e(button_label)}</a></p>'
        )
    html = (
        '<!doctype html><html><body style="margin:0;padding:24px;background:#f3f4f6;'
        'font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif">'
        '<div style="max-width:560px;margin:0 auto;padding:24px;background:#ffffff;'
        'border-radius:8px">' + "".join(body) + "</div>"
        '<p style="max-width:560px;margin:12px auto 0;font-size:12px;color:#9ca3af">'
        "Upwork Assistant</p></body></html>"
    )
    return text, html


# ---------------------------------------------------------------- transports


def _smtp_ready(settings: Settings) -> bool:
    return bool(
        str(settings.get("email.smtp_host")).strip()
        and str(settings.get("email.to_address")).strip()
    )


def _send_smtp(settings: Settings, subject: str, text: str, html: str) -> None:
    host = str(settings.get("email.smtp_host")).strip()
    port = int(settings.get("email.smtp_port"))
    user = str(settings.get("email.smtp_user"))
    sender = str(settings.get("email.from_address")).strip() or user
    to = str(settings.get("email.to_address")).strip()
    timeout = settings.get("email.smtp_timeout_seconds")
    msg = build_message(sender, to, subject, text, html)
    if port == 465:
        server = smtplib.SMTP_SSL(host, port, timeout=timeout)
    else:
        server = smtplib.SMTP(host, port, timeout=timeout)
    try:
        if port != 465 and settings.get("email.smtp_starttls"):
            server.starttls()
        if user:
            server.login(user, str(settings.get("email.smtp_password")))
        server.send_message(msg)
    finally:
        try:
            server.quit()
        except Exception:
            pass


# ---------------------------------------------------------------- send_event


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat(timespec="seconds")


def _already_sent(
    conn: sqlite3.Connection, settings: Settings, event: str, ref_id: str, now: datetime
) -> bool:
    if conn.execute(
        "SELECT 1 FROM emails WHERE event = ? AND ref_id IS ? LIMIT 1", (event, ref_id)
    ).fetchone():
        return True
    key = MIN_HOURS_SETTING.get(event)
    if key:
        since = _iso(now - timedelta(hours=float(settings.get(key))))
        if conn.execute(
            "SELECT 1 FROM emails WHERE event = ? AND sent_at > ? LIMIT 1", (event, since)
        ).fetchone():
            return True
    return False


def _set_failed_alert(conn: sqlite3.Connection, event: str, reason: str, now: datetime) -> None:
    conn.execute(
        "INSERT INTO alerts(key, message, created_at) VALUES (?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET message = excluded.message",
        (ALERT_FAILED, f"Could not send the '{event}' email: {reason}"[:300], _iso(now)),
    )


def send_event(
    conn: sqlite3.Connection,
    event: str,
    ref_id: str | None,
    subject: str,
    text: str,
    html: str,
    *,
    config: Config | None = None,
    settings: Settings | None = None,
    now: datetime | None = None,
) -> str:
    """Send one event email if it is on, new, and a transport exists. Never raises.

    Returns one of SENT, OFF, DUPLICATE, NO_TRANSPORT, FAILED.
    `config` defaults to the environment (load_config) and `settings` to the DB settings.
    """
    try:
        return _send_event(conn, event, ref_id, subject, text, html, config, settings, now)
    except Exception:
        log.exception("Email '%s' failed unexpectedly", event)
        return FAILED


def _send_event(conn, event, ref_id, subject, text, html, config, settings, now) -> str:
    now = now or datetime.now(UTC)
    settings = settings or Settings(conn)
    key = f"email.events.{event}"
    if key not in settings.defaults:
        log.warning("Email event '%s' is not known: not sent", event)
        return OFF
    if not settings.get(key):
        log.info("Email '%s' is switched off", event)
        return OFF
    ref = None if ref_id is None else str(ref_id)
    if _already_sent(conn, settings, event, ref, now):
        log.info("Email '%s' (%s) was already sent", event, ref)
        return DUPLICATE
    config = config or load_config()
    subject = _clean(subject)[:200]

    gmail = Gmail(config, settings, conn)
    tried: list[str] = []
    errors: list[str] = []
    if gmail.connected():
        tried.append("gmail")
        try:
            gmail.send(gmail.recipient(), subject, text, html)
        except GmailError as e:
            errors.append(f"Gmail: {e}")
            log.warning("Gmail send failed for '%s': %s", event, e)
        else:
            return _record(conn, event, ref, now)
    if _smtp_ready(settings):
        tried.append("smtp")
        try:
            _send_smtp(settings, subject, text, html)
        except Exception as e:  # smtplib raises many kinds; show only the class name
            errors.append(f"SMTP: {type(e).__name__}")
            log.warning("SMTP send failed for '%s': %s", event, type(e).__name__)
        else:
            return _record(conn, event, ref, now)
    if not tried:
        log.info("Email '%s' skipped: Gmail is not connected and SMTP is not set", event)
        return NO_TRANSPORT
    _set_failed_alert(conn, event, "; ".join(errors), now)
    return FAILED


def _record(conn: sqlite3.Connection, event: str, ref: str | None, now: datetime) -> str:
    conn.execute(
        "INSERT INTO emails(event, sent_at, ref_id) VALUES (?,?,?)", (event, _iso(now), ref)
    )
    conn.execute("DELETE FROM alerts WHERE key = ?", (ALERT_FAILED,))
    log.info("Email '%s' sent", event)
    return SENT


# ---------------------------------------------------------------- the emails daily.py sends


def _money(v: Any) -> str:
    return f"${v:,.0f}" if isinstance(v, (int, float)) else "n/a"


def _rate(v: Any) -> str:
    return f"${v:,.2f}/hr" if isinstance(v, (int, float)) else "n/a"


def notify_high_job(conn, config: Config, job: dict[str, Any], **kw) -> str:
    title = _clean(job.get("title") or job.get("id"))
    flags = [_clean(f) for f in (job.get("flags") or [])]
    facts = [
        ("Tier", f"{job.get('tier')}" + (" (estimated)" if job.get("tier_estimated") else "")),
        ("Rate paid", _rate(job.get("rate_paid"))),
        ("Client spend", _money(job.get("client_spend"))),
        ("Flags", ", ".join(flags) if flags else "none"),
    ]
    text, html = build_email(
        "New HIGH job",
        [title],
        facts=facts,
        link_path=f"/jobs/{job['id']}",
        base_url=config.public_base_url,
    )
    return send_event(
        conn, "high_job", str(job["id"]), f"New HIGH job: {title}", text, html, config=config, **kw
    )


def notify_connects_low(conn, config: Config, message: str, day: str, **kw) -> str:
    text, html = build_email(
        "Connects are low", [message], link_path="/", base_url=config.public_base_url
    )
    return send_event(
        conn,
        "connects_low",
        day,
        "Connects are too low for the top job",
        text,
        html,
        config=config,
        **kw,
    )


def notify_auth_expired(conn, config: Config, run_id: str, **kw) -> str:
    text, html = build_email(
        "Upwork login expired",
        [
            "The daily run could not read Upwork because the login ended.",
            "Log in to Upwork again on the Upwork page.",
        ],
        link_path="/upwork",
        base_url=config.public_base_url,
        button_label="Open the Upwork page",
    )
    return send_event(
        conn, "auth_expired", run_id, "Upwork login expired", text, html, config=config, **kw
    )


def notify_run_failed(
    conn, config: Config, task: str, run_id: str, failures: int, error: str, **kw
) -> str:
    text, html = build_email(
        f"The {task} run failed {failures} times in a row",
        ["The last error was:", error[:300]],
        link_path="/",
        base_url=config.public_base_url,
    )
    return send_event(
        conn,
        "run_failed",
        run_id,
        f"The {task} run failed {failures} times in a row",
        text,
        html,
        config=config,
        **kw,
    )
