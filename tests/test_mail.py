"""app/mail.py (plan M2): events on/off, dedupe, Gmail, SMTP fallback, daily wiring. No network."""

import base64
import email
import json
from datetime import UTC, datetime, timedelta
from email import policy

import httpx
import pytest
import respx

from app import mail
from app.db import init_db
from app.gmail import Gmail, GmailTokens
from app.settings import Settings
from app.tasks import daily
from app.upwork.client import UpworkAuthError
from tests.test_daily import make_client

SEND_URL = "https://gmail.googleapis.com/gmail/v1/users/me/messages/send"
NOW = datetime(2026, 11, 6, 12, 0, tzinfo=UTC)
ORG = "1000000000000000001"


@pytest.fixture
def conn():
    return init_db(":memory:")


@pytest.fixture
def settings(conn):
    s = Settings(conn)
    s.set("upwork.talent_org_uid", ORG)
    return s


def connect_gmail(config, settings, conn, address="me@gmail.example"):
    g = Gmail(config, settings, conn)
    far = (datetime.now(UTC) + timedelta(hours=5)).isoformat(timespec="seconds")
    g.store.save(GmailTokens("RT", "AT", far, address))
    return g


def count(conn, event=None):
    q, a = "SELECT COUNT(*) FROM emails", ()
    if event:
        q, a = q + " WHERE event = ?", (event,)
    return conn.execute(q, a).fetchone()[0]


def send(conn, config, settings, event="high_job", ref="r1", now=NOW):
    return mail.send_event(
        conn,
        event,
        ref,
        "Subject",
        "text",
        "<p>html</p>",
        config=config,
        settings=settings,
        now=now,
    )


class FakeSMTP:
    instances: list = []
    fail = False

    def __init__(self, host, port, timeout=None):
        self.host, self.port, self.timeout = host, port, timeout
        self.log, self.sent = [], []
        FakeSMTP.instances.append(self)
        if FakeSMTP.fail:
            raise ConnectionRefusedError("no")

    def starttls(self):
        self.log.append("starttls")

    def login(self, user, password):
        self.log.append(("login", user, password))

    def send_message(self, msg):
        self.sent.append(msg)

    def quit(self):
        self.log.append("quit")


@pytest.fixture
def smtp(monkeypatch):
    FakeSMTP.instances, FakeSMTP.fail = [], False
    monkeypatch.setattr(mail.smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(mail.smtplib, "SMTP_SSL", FakeSMTP)
    return FakeSMTP


def set_smtp(settings):
    settings.set("email.smtp_host", "smtp.test")
    settings.set("email.smtp_user", "u")
    settings.set("email.smtp_password", "p")
    settings.set("email.to_address", "to@x.example")
    settings.set("email.from_address", "from@x.example")


# ---------- the template ----------


def test_build_email_escapes_and_has_button():
    text, html = mail.build_email(
        "T <b>",
        ["line <script>alert(1)</script>"],
        facts=[("Rate", "$40/hr")],
        link_path="/jobs/1",
        base_url="https://p.example/",
    )
    assert "<script>" not in html and "&lt;script&gt;" in html and "&lt;b&gt;" in html
    assert 'href="https://p.example/jobs/1"' in html and "Open in panel" in html
    assert "<img" not in html and 'style="' in html
    assert "Open in panel: https://p.example/jobs/1" in text and "Rate: $40/hr" in text


def test_build_email_no_button_without_base_url():
    text, html = mail.build_email("T", ["x"], link_path="/jobs/1")
    assert "Open in panel" not in html and "http" not in text


# ---------- send_event ----------


def test_sent_through_gmail_and_recorded(conn, config, settings):
    connect_gmail(config, settings, conn)
    with respx.mock:
        route = respx.post(SEND_URL).mock(return_value=httpx.Response(200, json={}))
        assert send(conn, config, settings) == mail.SENT
    raw = json.loads(route.calls.last.request.content)["raw"]
    msg = email.message_from_bytes(
        base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)), policy=policy.default
    )
    assert msg["Subject"] == "Subject" and msg["To"] == "me@gmail.example"
    assert count(conn, "high_job") == 1


def test_event_off_sends_nothing(conn, config, settings):
    connect_gmail(config, settings, conn)
    settings.set("email.events.high_job", False)
    with respx.mock:
        route = respx.post(SEND_URL).mock(return_value=httpx.Response(200, json={}))
        assert send(conn, config, settings) == mail.OFF
        assert route.call_count == 0
    assert count(conn) == 0
    settings.set("email.events.high_job", True)
    with respx.mock:
        respx.post(SEND_URL).mock(return_value=httpx.Response(200, json={}))
        assert send(conn, config, settings) == mail.SENT


def test_dedupe_same_ref_only(conn, config, settings):
    connect_gmail(config, settings, conn)
    with respx.mock:
        route = respx.post(SEND_URL).mock(return_value=httpx.Response(200, json={}))
        assert send(conn, config, settings, ref="a") == mail.SENT
        assert send(conn, config, settings, ref="a") == mail.DUPLICATE
        assert send(conn, config, settings, ref="b") == mail.SENT
        assert route.call_count == 2


def test_connects_low_max_once_a_day(conn, config, settings):
    connect_gmail(config, settings, conn)
    with respx.mock:
        respx.post(SEND_URL).mock(return_value=httpx.Response(200, json={}))
        e = "connects_low"
        assert send(conn, config, settings, e, "d1", NOW) == mail.SENT
        assert send(conn, config, settings, e, "d2", NOW + timedelta(hours=3)) == mail.DUPLICATE
        assert send(conn, config, settings, e, "d3", NOW + timedelta(hours=25)) == mail.SENT


def test_no_transport_skips_silently(conn, config, settings, caplog):
    caplog.set_level("INFO", logger="mail")
    assert send(conn, config, settings) == mail.NO_TRANSPORT
    assert count(conn) == 0 and "skipped" in caplog.text
    assert conn.execute("SELECT COUNT(*) FROM alerts").fetchone()[0] == 0


def test_unknown_event_is_not_sent(conn, config, settings):
    assert send(conn, config, settings, event="nope") == mail.OFF


def test_smtp_used_when_gmail_not_connected(conn, config, settings, smtp):
    set_smtp(settings)
    assert send(conn, config, settings) == mail.SENT
    s = smtp.instances[0]
    assert (s.host, s.port) == ("smtp.test", 587)
    assert s.log == ["starttls", ("login", "u", "p"), "quit"]
    msg = s.sent[0]
    assert msg["To"] == "to@x.example" and msg["From"] == "from@x.example"
    assert msg.get_content_type() == "multipart/alternative"
    assert count(conn) == 1


def test_smtp_port_465_uses_ssl_no_starttls(conn, config, settings, smtp):
    set_smtp(settings)
    settings.set("email.smtp_port", 465)
    assert send(conn, config, settings) == mail.SENT
    assert "starttls" not in smtp.instances[0].log


def test_gmail_failure_falls_back_to_smtp(conn, config, settings, smtp):
    connect_gmail(config, settings, conn)
    set_smtp(settings)
    with respx.mock:
        respx.post(SEND_URL).mock(return_value=httpx.Response(500, json={}))
        assert send(conn, config, settings) == mail.SENT
    assert len(smtp.instances[0].sent) == 1


def test_failure_does_not_raise_and_sets_alert(conn, config, settings, smtp):
    connect_gmail(config, settings, conn)
    set_smtp(settings)
    smtp.fail = True
    with respx.mock:
        respx.post(SEND_URL).mock(return_value=httpx.Response(500, json={}))
        assert send(conn, config, settings) == mail.FAILED
    alert = conn.execute("SELECT message FROM alerts WHERE key = 'email_failed'").fetchone()
    assert alert and "high_job" in alert["message"]
    assert count(conn) == 0  # not recorded, so a later try can send it
    smtp.fail = False
    assert send(conn, config, settings) == mail.SENT
    assert not conn.execute("SELECT 1 FROM alerts WHERE key = 'email_failed'").fetchone()


def test_send_event_never_raises(conn, config, settings, monkeypatch):
    monkeypatch.setattr(mail, "_send_event", lambda *a: 1 / 0)
    assert send(conn, config, settings) == mail.FAILED


def test_header_injection_in_subject_is_flattened(conn, config, settings, smtp):
    set_smtp(settings)
    mail.send_event(
        conn,
        "high_job",
        "x",
        "Hi\r\nBcc: evil@x.example",
        "t",
        "<p>h</p>",
        config=config,
        settings=settings,
        now=NOW,
    )
    msg = smtp.instances[0].sent[0]
    assert msg["Bcc"] is None and "\n" not in msg["Subject"]


# ---------- wired into daily ----------


@pytest.fixture
def gmail_route(conn, config, settings):
    connect_gmail(config, settings, conn)
    with respx.mock:
        yield respx.post(SEND_URL).mock(return_value=httpx.Response(200, json={}))


def subjects(route):
    out = []
    for call in route.calls:
        raw = json.loads(call.request.content)["raw"]
        out.append(
            email.message_from_bytes(
                base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)), policy=policy.default
            )
        )
    return out


@pytest.fixture
def user_md(config):
    (config.data_dir / "memory").mkdir(exist_ok=True)
    (config.data_dir / "memory" / "USER.md").write_text("Built 12 n8n flows for a CRM client.")


@pytest.mark.judging
def test_daily_sends_one_email_per_new_high_job_even_in_dry_run(
    conn, config, settings, gmail_route, fake_claude_gen, user_md
):
    assert settings.get("general.dry_run") is True
    res = daily.run_daily(conn, settings, config, make_client(settings, balance=50))
    assert res.tiers["HIGH"] == 2
    msgs = subjects(gmail_route)
    assert len(msgs) == 2 and count(conn, "high_job") == 2
    html = msgs[0].get_body(("html",)).get_content()
    assert "/jobs/" in html and "Rate paid" in html and "Client spend" in html and "Flags" in html
    assert "Fit score" in html and "80 / 100" in html and "Why apply" in html
    assert "reason two" in html and "can&#x27;t afford" not in html
    # second run: nothing new, nothing sent
    daily.run_daily(conn, settings, config, make_client(settings, balance=50))
    assert len(subjects(gmail_route)) == 2


@pytest.mark.judging
def test_high_job_email_only_when_fit_is_high_enough(
    conn, config, settings, gmail_route, fake_claude_gen, user_md
):
    fake_claude_gen.env(
        "FAKE_GEN_FIT", json.dumps({"2118527230669974661": 64, "2118387252193629626": 65})
    )
    daily.run_daily(conn, settings, config, make_client(settings, balance=50))
    msgs = subjects(gmail_route)
    assert len(msgs) == 1 and count(conn, "high_job") == 1
    assert "65 / 100" in msgs[0].get_body(("plain",)).get_content()
    settings.set("pay.fit_min", 66)  # a later threshold change does not email old judgements
    daily.run_daily(conn, settings, config, make_client(settings, balance=50))
    assert len(subjects(gmail_route)) == 1


@pytest.mark.judging
def test_high_job_email_says_when_you_cannot_afford_it(
    conn, config, settings, gmail_route, fake_claude_gen, user_md
):
    daily.run_daily(conn, settings, config, make_client(settings, balance=10))
    msgs = [m for m in subjects(gmail_route) if "HIGH job" in m["Subject"]]
    assert len(msgs) == 2
    text = msgs[0].get_body(("plain",)).get_content()
    assert "can't afford this one yet" in text and "Connects" in text and "you have 10" in text


def test_daily_connects_low_email_once(conn, config, settings, gmail_route):
    settings.set("email.events.high_job", False)
    daily.run_daily(conn, settings, config, make_client(settings, balance=5))
    assert count(conn, "connects_low") == 1
    daily.run_daily(conn, settings, config, make_client(settings, balance=5))
    assert count(conn, "connects_low") == 1


def test_daily_auth_expired_email(conn, config, settings, gmail_route):
    client = make_client(settings)
    client.search["most_recent"] = UpworkAuthError("Upwork login expired or missing")
    res = daily.run_daily(conn, settings, config, client)
    assert res.status == "auth_expired"
    assert count(conn, "auth_expired") == 1
    assert "login expired" in subjects(gmail_route)[0]["Subject"]


def test_daily_run_failed_after_three_in_a_row(conn, config, settings, gmail_route, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("kaput")

    monkeypatch.setattr(daily, "ensure_talent_account", boom)
    for _ in range(2):
        assert daily.run_daily(conn, settings, config, make_client(settings)).status == "error"
        assert count(conn, "run_failed") == 0
    daily.run_daily(conn, settings, config, make_client(settings))
    assert count(conn, "run_failed") == 1
    assert "failed 3 times" in subjects(gmail_route)[0]["Subject"]
    daily.run_daily(conn, settings, config, make_client(settings))  # 4th: within the daily gap
    assert count(conn, "run_failed") == 1


def test_daily_failed_runs_reset_by_success(conn, config, settings, gmail_route, monkeypatch):
    real = daily.ensure_talent_account
    monkeypatch.setattr(daily, "ensure_talent_account", lambda *a: 1 / 0)
    daily.run_daily(conn, settings, config, make_client(settings))
    daily.run_daily(conn, settings, config, make_client(settings))
    monkeypatch.setattr(daily, "ensure_talent_account", real)
    daily.run_daily(conn, settings, config, make_client(settings))
    monkeypatch.setattr(daily, "ensure_talent_account", lambda *a: 1 / 0)
    daily.run_daily(conn, settings, config, make_client(settings))
    assert count(conn, "run_failed") == 0


@pytest.mark.judging
def test_daily_mail_failure_does_not_break_the_run(
    conn, config, settings, fake_claude_gen, user_md
):
    connect_gmail(config, settings, conn)
    with respx.mock:
        respx.post(SEND_URL).mock(return_value=httpx.Response(500, json={}))
        res = daily.run_daily(conn, settings, config, make_client(settings, balance=50))
    assert res.status == "ok" and res.kept == 5
    assert conn.execute("SELECT 1 FROM alerts WHERE key = 'email_failed'").fetchone()
