"""Dashboard, jobs list and job detail render after login (plan E4)."""

import re
from datetime import UTC, datetime

import pytest

from app.tasks import daily
from app.upwork.client import UpworkAuthError
from tests.test_auth import make_client as make_web_client
from tests.test_auth import setup_admin
from tests.test_daily import GET_JOB_ID, make_client

ORG = "1000000000000000001"


@pytest.fixture
def web(config):
    app, client = make_web_client(config)
    setup_admin(client)  # setup leaves the client logged in
    app.state.settings.set("upwork.talent_org_uid", ORG)
    return app, client


def run(app, config, balance=50, client=None):
    s = app.state.settings
    return daily.run_daily(app.state.db, s, config, client or make_client(s, balance=balance))


def stat(html, stat_id):
    """The big number of one dashboard stat card."""
    return re.search(rf'id="{stat_id}".*?<strong>([^<]*)</strong>', html, re.S).group(1)


def cards(html):
    return html.count("<tr data-href=")


@pytest.mark.parametrize("path", ["/", "/jobs", "/jobs/123"])
def test_pages_need_login(config, path):
    _, client = make_web_client(config)
    setup_admin(client)
    client.cookies.clear()  # logged out
    r = client.get(path)
    assert r.status_code == 303 and r.headers["location"].startswith("/login")


def test_empty_pages_render(web):
    _, client = web
    r = client.get("/")
    assert r.status_code == 200 and "Home" in r.text
    assert "No Connects balance seen yet" in r.text and "never run" in r.text
    r = client.get("/jobs")
    assert r.status_code == 200 and "No jobs match" in r.text


def test_dashboard_with_low_connects(web, config):
    app, client = web
    run(app, config, balance=5)
    r = client.get("/")
    assert r.status_code == 200
    assert 'class="stat warn" id="connects"' in r.text and "<strong>5</strong>" in r.text
    assert "below the 12 needed" in r.text  # alert row
    assert stat(r.text, "stat-good") == "5" and "2 high, 3 mid" in r.text
    assert 'class="status ok"' in r.text


def test_dashboard_connects_ok_no_warning(web, config):
    app, client = web
    run(app, config, balance=50)
    r = client.get("/")
    assert stat(r.text, "connects") == "50" and "stat warn" not in r.text
    assert "below the" not in r.text


def test_dashboard_auth_expired_alert(web, config):
    app, client = web
    fake = make_client(app.state.settings)
    fake.search["most_recent"] = UpworkAuthError("Upwork login expired or missing")
    run(app, config, client=fake)
    r = client.get("/")
    assert "Upwork login expired" in r.text and 'href="/upwork"' in r.text
    assert "auth_expired" in r.text


def test_dashboard_failed_run_alert(web, config):
    app, client = web
    app.state.settings.set("upwork.talent_org_uid", "")
    empty = {"accounts": []}  # F0a: auto-detect finds nothing
    run(app, config, client=make_client(app.state.settings, accounts=empty))
    r = client.get("/")
    assert "Last daily run failed" in r.text and "No freelancer account found" in r.text


def test_dashboard_kill_switch_status(web, config):
    app, client = web
    app.state.settings.set("general.kill_switch", True)
    run(app, config)
    assert 'class="status skipped"' in client.get("/").text


def test_jobs_list_default_hides_dropped(web, config):
    app, client = web
    run(app, config)
    r = client.get("/jobs")
    assert r.status_code == 200 and cards(r.text) == 5
    assert 'class="badge HIGH"' in r.text and 'class="badge MID"' in r.text
    assert "platform default rate" in r.text  # a flag shown on the card
    assert "$16.97/hr" in r.text or "$16.98/hr" in r.text  # rate_paid on the slack job
    assert "$32,548" in r.text  # client spend


def test_jobs_filters(web, config):
    app, client = web
    run(app, config)
    assert cards(client.get("/jobs?tier=HIGH").text) == 2
    assert cards(client.get("/jobs?tier=mid").text) == 3
    assert cards(client.get("/jobs?status=dropped").text) == 3
    assert cards(client.get("/jobs?status=all").text) == 8
    assert cards(client.get("/jobs?status=candidate&tier=HIGH").text) == 2
    assert cards(client.get("/jobs?status=sent").text) == 0
    assert "tier LOW" in client.get("/jobs?status=dropped").text  # reason shown


def test_jobs_date_filter(web, config):
    app, client = web
    run(app, config)
    today = datetime.now(UTC).date().isoformat()
    assert cards(client.get(f"/jobs?date_from={today}&date_to={today}").text) == 5
    assert cards(client.get("/jobs?date_from=2999-01-01").text) == 0
    assert cards(client.get("/jobs?date_to=2000-01-01").text) == 0
    assert cards(client.get("/jobs?date_from=garbage&tier=nope").text) == 5  # bad input ignored


def test_jobs_title_search(web, config):
    app, client = web
    run(app, config)
    assert cards(client.get("/jobs?q=zzzz-no-such-title").text) == 0
    assert cards(client.get("/jobs?q=%25").text) == 0  # % is a literal, not a wildcard
    first = app.state.db.execute(
        "SELECT title FROM jobs WHERE status != 'dropped' LIMIT 1"
    ).fetchone()[0]
    assert cards(client.get("/jobs", params={"q": first[:12].lower()}).text) >= 1


def test_jobs_pagination(web, config):
    app, client = web
    run(app, config)
    app.state.settings.set("ui.jobs_page_size", 2)
    r = client.get("/jobs")
    assert cards(r.text) == 2 and "Page 1 of 3" in r.text and "page=2" in r.text
    r = client.get("/jobs?page=3")
    assert cards(r.text) == 1 and "Page 3 of 3" in r.text
    assert "Page 3 of 3" in client.get("/jobs?page=99").text


def test_job_detail(web, config):
    app, client = web
    run(app, config)
    r = client.get("/jobs/2118413555956864697")
    assert r.status_code == 200
    assert 'href="https://www.upwork.com/jobs/~022118413555956864697"' in r.text
    assert "Open on Upwork" in r.text
    assert "GoHighLevel Systems Specialist" in r.text  # description text, shown escaped
    assert "&lt;untrusted_participant_content&gt;" not in r.text  # wrapper removed
    assert "Screening questions" in r.text and "Draft" in r.text and "MID" in r.text
    assert "16.97" in r.text


def test_dropped_job_detail_shows_reason(web, config):
    app, client = web
    run(app, config)
    r = client.get(f"/jobs/{GET_JOB_ID}")
    assert r.status_code == 200 and "tier LOW" in r.text and "1-star review" in r.text


def test_platform_default_shown_on_detail(web, config):
    app, client = web
    run(app, config)
    r = client.get("/jobs/2118442174097105410")
    assert "platform default range" in r.text and "platform default rate" in r.text


def test_job_detail_unknown_is_404(web):
    _, client = web
    assert client.get("/jobs/999").status_code == 404


def test_job_text_is_escaped_and_bad_links_ignored(web):
    app, client = web
    app.state.db.execute(
        "INSERT INTO jobs(job_id, first_seen, title, url, status, tier, details) "
        "VALUES ('555', '2026-11-06T00:00:00+00:00', '<script>alert(1)</script>', "
        "'javascript:alert(1)', 'candidate', 'MID', '{\"description\": \"<b>x</b>\"}')"
    )
    for path in ("/jobs", "/jobs/555"):
        r = client.get(path)
        assert "<script>alert(1)</script>" not in r.text
        assert "&lt;script&gt;" in r.text
        assert "javascript:" not in r.text
    assert "&lt;b&gt;x&lt;/b&gt;" in client.get("/jobs/555").text
    assert 'href="https://www.upwork.com/jobs/~02555"' in client.get("/jobs/555").text


# ---------------------------------------------------------------- plan G0: Budget row


@pytest.mark.parametrize(
    ("job", "text", "note"),
    [
        (
            {
                "job_type": "fixed",
                "budget_min": 300.0,
                "budget_max": 300.0,
                "hourly_budget_type": "NOT_PROVIDED",
            },
            "$300 fixed",
            "",
        ),
        ({"job_type": "fixed", "budget_min": 1250.5, "budget_max": 1250.5}, "$1,250.50 fixed", ""),
        (
            {
                "job_type": "hourly",
                "budget_min": 15.0,
                "budget_max": 35.0,
                "hourly_budget_type": "client-specified range",
            },
            "$15-$35/hr",
            "client range",
        ),
        (
            {
                "job_type": "hourly",
                "budget_min": 20.0,
                "budget_max": 20.0,
                "hourly_budget_type": "client-specified range",
            },
            "$20/hr",
            "client range",
        ),
        (
            {
                "job_type": "hourly",
                "budget_min": 8.0,
                "budget_max": 20.0,
                "hourly_budget_type": "platform default range",
            },
            "$8-$20/hr",
            "platform default range, not the client's rate",
        ),
        ({"job_type": "hourly", "budget_min": None, "budget_max": None}, "n/a", ""),
    ],
)
def test_budget_text(job, text, note):
    from app.pages import budget_text

    assert budget_text(job) == (text, note)


def test_fixed_job_detail_has_no_hourly_type(web):
    app, client = web
    app.state.db.execute(
        "INSERT INTO jobs(job_id, first_seen, title, status, tier, job_type, budget_min, "
        "budget_max, hourly_budget_type, details) VALUES ('777', '2026-11-06T00:00:00+00:00', "
        "'Fixed one', 'candidate', 'MID', 'fixed', 300, 300, 'NOT_PROVIDED', '{}')"
    )
    html = client.get("/jobs/777").text
    assert "$300 fixed" in html
    assert "NOT_PROVIDED" not in html and "300.0" not in html


# ---------------------------------------------------------------- plan G2: fit and draft


@pytest.mark.judging
def test_job_pages_show_fit_and_draft(web, config, fake_claude_gen):
    app, client = web
    (config.data_dir / "memory").mkdir(parents=True, exist_ok=True)
    (config.data_dir / "memory" / "USER.md").write_text("Built 12 n8n flows.")
    fake_claude_gen.env("FAKE_GEN_SKIP", 1)
    run(app, config, balance=10)

    jobs = client.get("/jobs").text
    assert "<strong>80</strong>" in jobs and "Draft ready" in jobs  # Score column + pill
    assert "Can&#39;t afford" in jobs or "Can't afford" in jobs

    jid = "2118527230669974661"  # HIGH, costs 12 Connects, balance 10
    r = client.get(f"/jobs/{jid}")
    assert r.status_code == 200
    assert "Score 80 / 100" in r.text and "Why apply" in r.text and "reason two" in r.text
    assert "vague scope" in r.text and "Instructions in the post" in r.text
    assert "afford this one yet" in r.text and "costs 12 Connects and you have 10" in r.text
    assert "Bots. Your CRM leaks leads" in r.text  # the cover letter
    assert 'data-copy="#draft-cover"' in r.text and "/static/copy.js" in r.text
    assert 'id="draft-answer-1"' in r.text  # the post has screening questions
    assert "Skipped instruction" in r.text  # the flag is shown
    assert "Portfolio item A" in r.text and "Approve is the only step that can send" in r.text
    assert client.get("/static/copy.js").status_code == 200


def test_job_without_draft_says_why(web, config):
    app, client = web
    run(app, config)  # judging is off in this test
    r = client.get("/jobs/2118527230669974661")
    assert "Not judged yet" in r.text
    conn = app.state.db
    conn.execute(
        "INSERT INTO judgements(job_id, fit_score, why_apply, risks, plan) "
        "VALUES ('2118527230669974661', 40, 'x', '', '{}')"
    )
    assert "fit 40 is below the minimum of 65" in client.get("/jobs/2118527230669974661").text
    conn.execute("UPDATE judgements SET fit_score = 90")
    assert "add your profile facts (USER.md)" in client.get("/jobs/2118527230669974661").text


def test_dashboard_shows_user_md_alert(web):
    app, client = web
    app.state.db.execute(
        "INSERT INTO alerts(key, message, created_at) VALUES ('user_md_missing', "
        "'Add your profile facts (USER.md) to get drafts', '2026-11-06T00:00:00+00:00')"
    )
    assert "Add your profile facts (USER.md) to get drafts" in client.get("/").text


def test_home_shows_job_alert_status(web):
    app, client = web
    assert "Job alerts: off" in re.sub(r"\s+", " ", client.get("/").text)
    app.state.settings.set("job_alerts.enabled", True)
    html = re.sub(r"\s+", " ", client.get("/").text)
    assert "last alert check never, 0 Upwork call(s) today" in html
