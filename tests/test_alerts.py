"""Alerts (plan X3): new invites and the reminder, JSS drop + guard, rank drop, charts, the
"Can't afford" pill and filter. A fake bridge answers and the mail transport is a fake (SMTP
send is replaced); no Upwork, no Claude, no real email."""

import re
from datetime import UTC, date, datetime, timedelta

import pytest

from app import alerts, charts, mail, metrics
from app.db import init_db
from app.settings import Settings
from app.tasks import daily
from tests.test_auth import csrf_from, setup_admin
from tests.test_auth import make_client as make_web_client
from tests.test_daily import make_client as make_daily_client
from tests.test_metrics import FakeBridge
from tests.upwork_fakes import load

ORG = "1000000000000000001"
NOW = datetime(2026, 11, 6, 12, 0, tzinfo=UTC)
TODAY = date(2026, 11, 6)
INV1, INV2 = "7000000000000000001", "7000000000000000002"


@pytest.fixture
def conn():
    return init_db(":memory:")


@pytest.fixture
def settings(conn):
    s = Settings(conn)
    s.set("upwork.talent_org_uid", ORG)
    s.set("email.smtp_host", "smtp.example")  # a transport exists; its send is faked below
    s.set("email.to_address", "me@example.test")
    s.set("rank.keywords", ["alpha", "beta"])
    return s


@pytest.fixture
def outbox(monkeypatch):
    sent: list[dict] = []

    def fake_send(settings, subject, text, html):
        sent.append({"subject": subject, "text": text, "html": html})

    monkeypatch.setattr(mail, "_send_smtp", fake_send)
    return sent


def invitations_payload():
    """The two invitations of the fixture, as the dashboard shows them (count + items)."""
    items = load("invitations_two.json")["data"]["invitations"]
    return [
        {
            "id": i["id"],
            "job_id": i["jobPosting"]["id"],
            "title": i["jobPosting"]["title"],
        }
        for i in items
    ]


def dashboard_with(items):
    d = load("freelancer_dashboard.json")
    d["data"]["invitations"] = {
        "count": len(items),
        "items": [
            {"id": i["id"], "jobPosting": {"id": i["job_id"], "title": i["title"]}} for i in items
        ],
    }
    return d


def process(conn, settings, config, invites, now=NOW, complete=True):
    return alerts.process_invites(conn, settings, config, invites, complete, now)


def events(conn, event):
    return [
        r["ref_id"] for r in conn.execute("SELECT ref_id FROM emails WHERE event = ?", (event,))
    ]


# ================================================================ invites


def test_new_invites_are_stored_and_mailed_at_once(conn, settings, config, outbox):
    lines = process(conn, settings, config, invitations_payload())
    rows = conn.execute("SELECT * FROM invites ORDER BY invite_id").fetchall()
    assert [r["invite_id"] for r in rows] == [INV1, INV2]
    assert rows[0]["job_id"] == "4000000000000000101" and rows[0]["first_seen"]
    assert all(r["alerted_at"] and not r["answered_at"] and not r["second_alert_at"] for r in rows)
    assert sorted(events(conn, "invite")) == [INV1, INV2]
    assert len(outbox) == 2 and len(lines) == 2
    first = next(m for m in outbox if "n8n workflow expert" in m["subject"])
    assert "untrusted" not in first["subject"] and "untrusted" not in first["html"]
    assert "https://www.upwork.com/jobs/~024000000000000000101" in first["text"]


def test_a_second_read_sends_nothing_again(conn, settings, config, outbox):
    process(conn, settings, config, invitations_payload())
    process(conn, settings, config, invitations_payload(), now=NOW + timedelta(hours=1))
    assert len(outbox) == 2
    assert conn.execute("SELECT COUNT(*) FROM invites").fetchone()[0] == 2


def test_reminder_after_12_hours_once(conn, settings, config, outbox):
    process(conn, settings, config, invitations_payload())
    process(
        conn, settings, config, invitations_payload(), now=NOW + timedelta(hours=11, minutes=59)
    )
    assert len(outbox) == 2  # not yet
    process(conn, settings, config, invitations_payload(), now=NOW + timedelta(hours=12))
    assert len(outbox) == 4
    assert sorted(events(conn, "invite_reminder")) == [INV1, INV2]
    assert "still waiting" in outbox[-1]["subject"]
    process(conn, settings, config, invitations_payload(), now=NOW + timedelta(hours=30))
    assert len(outbox) == 4  # one reminder only
    assert all(r["second_alert_at"] for r in conn.execute("SELECT * FROM invites"))


def test_reminder_hours_is_a_setting(conn, settings, config, outbox):
    assert settings.get("alerts.invite_reminder_hours") == 12
    settings.set("alerts.invite_reminder_hours", 3)
    process(conn, settings, config, invitations_payload())
    process(conn, settings, config, invitations_payload(), now=NOW + timedelta(hours=3))
    assert len(events(conn, "invite_reminder")) == 2


def test_answered_invite_gets_no_reminder(conn, settings, config, outbox):
    process(conn, settings, config, invitations_payload())
    later = NOW + timedelta(hours=13)
    process(conn, settings, config, invitations_payload()[:1], now=later)  # INV2 left the list
    row = conn.execute("SELECT * FROM invites WHERE invite_id = ?", (INV2,)).fetchone()
    assert row["answered_at"] and not row["second_alert_at"]
    assert events(conn, "invite_reminder") == [INV1]


def test_incomplete_read_does_not_mark_missing_invites_answered(conn, settings, config, outbox):
    process(conn, settings, config, invitations_payload())
    process(conn, settings, config, invitations_payload()[:1], complete=False)
    assert conn.execute("SELECT COUNT(*) FROM invites WHERE answered_at IS NULL").fetchone()[0] == 2


def test_a_proposal_for_the_job_means_answered(conn, settings, config, outbox):
    process(conn, settings, config, invitations_payload())
    conn.execute(
        "INSERT INTO proposals(proposal_id, job_id, created_at, status) VALUES "
        "('p1', '4000000000000000101', ?, 'Accepted')",
        (NOW.isoformat(),),
    )
    process(conn, settings, config, invitations_payload(), now=NOW + timedelta(hours=13))
    assert events(conn, "invite_reminder") == [INV2]  # still pending in the list, but answered
    row = conn.execute("SELECT answered_at FROM invites WHERE invite_id = ?", (INV1,)).fetchone()
    assert row["answered_at"]


def test_an_invite_that_comes_back_waits_for_an_answer_again(conn, settings, config, outbox):
    process(conn, settings, config, invitations_payload())
    process(conn, settings, config, [], now=NOW + timedelta(hours=1))
    assert (
        conn.execute("SELECT COUNT(*) FROM invites WHERE answered_at IS NOT NULL").fetchone()[0]
        == 2
    )
    process(conn, settings, config, invitations_payload()[:1], now=NOW + timedelta(hours=2))
    row = conn.execute("SELECT answered_at FROM invites WHERE invite_id = ?", (INV1,)).fetchone()
    assert row["answered_at"] is None
    assert len(outbox) == 2  # no new "new invite" email: the first one was sent


def test_switched_off_event_sends_nothing_but_still_stores(conn, settings, config, outbox):
    settings.set("email.events.invite", False)
    process(conn, settings, config, invitations_payload())
    assert outbox == []
    assert conn.execute("SELECT COUNT(*) FROM invites WHERE alerted_at IS NULL").fetchone()[0] == 2


def test_invites_switch_off_stores_nothing(conn, settings, config, outbox):
    settings.set("alerts.invites_enabled", False)
    assert process(conn, settings, config, invitations_payload()) == []
    assert conn.execute("SELECT COUNT(*) FROM invites").fetchone()[0] == 0 and outbox == []


def test_no_transport_is_retried_at_the_next_run(conn, settings, config, outbox):
    settings.set("email.smtp_host", "")
    process(conn, settings, config, invitations_payload())
    assert outbox == []
    assert conn.execute("SELECT COUNT(*) FROM invites WHERE alerted_at IS NULL").fetchone()[0] == 2
    settings.set("email.smtp_host", "smtp.example")
    process(conn, settings, config, invitations_payload(), now=NOW + timedelta(hours=1))
    assert len(outbox) == 2


def test_snapshot_hands_over_the_pending_invites(conn, settings):
    fake = FakeBridge(dashboard=dashboard_with(invitations_payload()))
    row = metrics.snapshot(fake, conn, settings, NOW, tz="UTC")
    assert row["invites_open"] == 2 and row["invites_complete"] is True
    assert [i["id"] for i in row["invites"]] == [INV1, INV2]
    assert row["invites"][1]["job_id"] == "4000000000000000102"


def test_dashboard_preview_with_fewer_items_than_the_count_is_incomplete():
    d = dashboard_with(invitations_payload())
    d["data"]["invitations"]["count"] = 5
    parsed = metrics.parse_dashboard(d)
    assert parsed["invites_complete"] is False and len(parsed["invites"]) == 2


def test_flat_invite_items_and_items_without_id():
    d = {
        "data": {
            "invitations": {
                "count": 2,
                "items": [{"id": "9", "job_id": 5, "title": "T"}, {"title": "x"}],
            }
        }
    }
    parsed = metrics.parse_dashboard(d)
    assert parsed["invites"] == [{"id": "9", "job_id": "5", "title": "T"}]


def test_empty_sample_has_no_invites():
    parsed = metrics.parse_dashboard(load("freelancer_dashboard.json"))
    assert parsed["invites"] == [] and parsed["invites_complete"] is True


# ================================================================ JSS drop


def add_metrics(conn, day, jss):
    conn.execute(
        "INSERT INTO metrics_daily(date, jss, created_at) VALUES (?,?,?) "
        "ON CONFLICT(date) DO UPDATE SET jss = excluded.jss",
        (day.isoformat(), jss, NOW.isoformat()),
    )


def jss_check(conn, settings, config, now=NOW):
    return alerts.check_jss_drop(conn, settings, config, now.date(), now)


def test_jss_drop_sends_the_email_and_sets_the_guard(conn, settings, config, outbox):
    add_metrics(conn, TODAY - timedelta(days=1), 100)
    add_metrics(conn, TODAY, 98)
    assert settings.get("safety.jss_guard") is False and not alerts.jss_guard_on(settings)
    lines = jss_check(conn, settings, config)
    assert len(lines) == 1 and "guard is on" in lines[0]
    assert alerts.jss_guard_on(settings)
    assert "100 to 98" in settings.get("safety.jss_guard_note")
    assert len(outbox) == 1 and "Job Success Score dropped" in outbox[0]["subject"]
    assert events(conn, "jss_drop") == [TODAY.isoformat()]


def test_jss_drop_compares_with_the_last_known_value(conn, settings, config, outbox):
    add_metrics(conn, TODAY - timedelta(days=5), 100)
    add_metrics(conn, TODAY - timedelta(days=1), None)  # a day without a score
    add_metrics(conn, TODAY, 99)
    jss_check(conn, settings, config)
    assert alerts.jss_guard_on(settings) and len(outbox) == 1


@pytest.mark.parametrize(
    "before, now_, points, expect",
    [
        (100, 99.5, 1, False),
        (100, 99, 1, True),
        (100, 99, 2, False),
        (100, 98, 2, True),
        (90, 95, 1, False),
        (100, 100, 1, False),
    ],
)
def test_jss_drop_threshold_is_a_setting(
    conn, settings, config, outbox, before, now_, points, expect
):
    settings.set("alerts.jss_drop_points", points)
    add_metrics(conn, TODAY - timedelta(days=1), before)
    add_metrics(conn, TODAY, now_)
    jss_check(conn, settings, config)
    assert alerts.jss_guard_on(settings) is expect and len(outbox) == (1 if expect else 0)


def test_jss_no_score_or_no_history_does_nothing(conn, settings, config, outbox):
    add_metrics(conn, TODAY, 90)
    assert jss_check(conn, settings, config) == []  # nothing to compare with
    add_metrics(conn, TODAY - timedelta(days=1), 100)
    add_metrics(conn, TODAY, None)
    assert jss_check(conn, settings, config) == [] and not alerts.jss_guard_on(settings)
    assert outbox == []


def test_jss_same_day_rerun_sends_one_email(conn, settings, config, outbox):
    add_metrics(conn, TODAY - timedelta(days=1), 100)
    add_metrics(conn, TODAY, 97)
    jss_check(conn, settings, config)
    jss_check(conn, settings, config, now=NOW + timedelta(hours=2))
    assert len(outbox) == 1


def test_guard_is_never_cleared_by_the_code(conn, settings, config, outbox):
    add_metrics(conn, TODAY - timedelta(days=1), 100)
    add_metrics(conn, TODAY, 97)
    jss_check(conn, settings, config)
    # the score recovers, a week goes by, more checks run: the guard stays
    add_metrics(conn, TODAY + timedelta(days=1), 100)
    jss_check(conn, settings, config, now=NOW + timedelta(days=1))
    alerts.run_alerts(conn, settings, config, NOW + timedelta(days=1), TODAY + timedelta(days=1))
    assert alerts.jss_guard_on(settings)


def test_owner_clears_the_guard_and_the_same_day_rerun_does_not_set_it_again(
    conn, settings, config, outbox
):
    add_metrics(conn, TODAY - timedelta(days=1), 100)
    add_metrics(conn, TODAY, 97)
    jss_check(conn, settings, config)
    settings.set("safety.jss_guard", False)  # the owner's click
    jss_check(conn, settings, config, now=NOW + timedelta(hours=3))
    assert not alerts.jss_guard_on(settings)


def test_guard_is_set_even_when_the_email_is_off(conn, settings, config, outbox):
    settings.set("email.events.jss_drop", False)
    add_metrics(conn, TODAY - timedelta(days=1), 100)
    add_metrics(conn, TODAY, 90)
    jss_check(conn, settings, config)
    assert alerts.jss_guard_on(settings) and outbox == []


def test_jss_alert_switch_off(conn, settings, config, outbox):
    settings.set("alerts.jss_drop_enabled", False)
    add_metrics(conn, TODAY - timedelta(days=1), 100)
    add_metrics(conn, TODAY, 80)
    assert jss_check(conn, settings, config) == [] and not alerts.jss_guard_on(settings)


# ================================================================ rank drop


def add_rank(conn, day, keyword, position, seen=100):
    conn.execute(
        "INSERT OR REPLACE INTO rank_daily(date, keyword, position, organic_position, "
        "pages_checked, total_seen, created_at) VALUES (?,?,?,?,?,?,?)",
        (day.isoformat(), keyword, position, position, 1, seen, NOW.isoformat()),
    )


def fill(conn, keyword, by_offset):
    """by_offset: {days_back: position}"""
    for back, pos in by_offset.items():
        add_rank(conn, TODAY - timedelta(days=back), keyword, pos)


def worse_for_a_week(conn, keyword):
    # 14 days at place 10, then 7 days at place 20: every one of the last 3 days is 3+ worse
    fill(conn, keyword, {**{b: 10 for b in range(7, 21)}, **{b: 20 for b in range(0, 7)}})


def rank_check(conn, settings, config, now=NOW):
    return alerts.check_rank_drops(conn, settings, config, now.date(), now)


def test_rank_drop_three_days_in_a_row_sends_one_email(conn, settings, config, outbox):
    worse_for_a_week(conn, "alpha")
    lines = rank_check(conn, settings, config)
    assert len(lines) == 1 and lines[0].startswith("Rank drop: alpha")
    assert len(outbox) == 1 and "alpha" in outbox[0]["subject"]
    assert events(conn, "rank_drop") == ["alpha|2026-W45"]


def test_rank_drop_is_deduped_per_keyword_per_week(conn, settings, config, outbox):
    worse_for_a_week(conn, "alpha")
    worse_for_a_week(conn, "beta")
    rank_check(conn, settings, config)
    rank_check(conn, settings, config, now=NOW + timedelta(hours=3))
    assert sorted(events(conn, "rank_drop")) == ["alpha|2026-W45", "beta|2026-W45"]
    assert len(outbox) == 2
    # a new week (the drop goes on): the keyword may be mailed again
    nxt = NOW + timedelta(days=7)
    for back in range(0, 7):
        add_rank(conn, nxt.date() - timedelta(days=back), "alpha", 40)
    for back in range(7, 14):
        add_rank(conn, nxt.date() - timedelta(days=back), "alpha", 20)
    rank_check(conn, settings, config, now=nxt)
    assert "alpha|2026-W46" in events(conn, "rank_drop")


def test_two_bad_days_are_not_enough(conn, settings, config, outbox):
    fill(conn, "alpha", {**{b: 10 for b in range(2, 21)}, 1: 30, 0: 30})
    assert rank_check(conn, settings, config) == [] and outbox == []


def test_days_in_a_row_is_a_setting(conn, settings, config, outbox):
    fill(conn, "alpha", {**{b: 10 for b in range(2, 21)}, 1: 30, 0: 30})
    settings.set("alerts.rank_drop_days", 1)
    assert len(rank_check(conn, settings, config)) == 1


def test_a_small_change_does_not_alert(conn, settings, config, outbox):
    fill(conn, "alpha", {**{b: 10 for b in range(7, 21)}, **{b: 12 for b in range(0, 7)}})
    assert rank_check(conn, settings, config) == []
    settings.set("alerts.rank_drop_positions", 1)
    assert len(rank_check(conn, settings, config)) == 1  # the threshold is a setting


def test_a_better_rank_does_not_alert(conn, settings, config, outbox):
    fill(conn, "alpha", {**{b: 30 for b in range(7, 21)}, **{b: 10 for b in range(0, 7)}})
    assert rank_check(conn, settings, config) == []


def test_not_found_counts_as_a_drop(conn, settings, config, outbox):
    fill(conn, "alpha", {b: 10 for b in range(7, 21)})
    for b in range(0, 7):
        add_rank(conn, TODAY - timedelta(days=b), "alpha", None, seen=100)
    assert len(rank_check(conn, settings, config)) == 1


def test_no_row_today_means_no_alert(conn, settings, config, outbox):
    worse_for_a_week(conn, "alpha")
    conn.execute("DELETE FROM rank_daily WHERE date = ?", (TODAY.isoformat(),))
    assert rank_check(conn, settings, config) == []


def test_not_enough_history_does_not_alert(conn, settings, config, outbox):
    fill(conn, "alpha", {0: 50, 1: 50, 2: 50})
    assert rank_check(conn, settings, config) == []


def test_keyword_not_in_the_list_is_ignored_and_switch_off(conn, settings, config, outbox):
    worse_for_a_week(conn, "gamma")
    assert rank_check(conn, settings, config) == []
    worse_for_a_week(conn, "alpha")
    settings.set("alerts.rank_drop_enabled", False)
    assert rank_check(conn, settings, config) == [] and outbox == []


# ================================================================ run_alerts and the daily wiring


def test_run_alerts_never_raises(conn, settings, config, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("x")

    monkeypatch.setattr(alerts, "check_jss_drop", boom)
    lines = alerts.run_alerts(conn, settings, config, NOW, TODAY, None)
    assert any("Alert step jss failed" in line for line in lines)


def test_alerts_part_never_raises(conn, settings, config, monkeypatch):
    monkeypatch.setattr(alerts, "run_alerts", lambda *a, **k: 1 / 0)
    assert "Alerts failed" in daily.alerts_part(conn, settings, config, NOW, None)[0]


def test_new_event_switches_exist_and_are_on_by_default(conn):
    s = Settings(conn)
    for event in ("invite", "invite_reminder", "jss_drop", "rank_drop"):
        assert s.get(f"email.events.{event}") is True
    for event in ("invite", "invite_reminder", "jss_drop", "rank_drop"):
        assert event in mail.EVENTS


class Combo:
    """Daily bridge: dashboard / profile / JSS from FakeBridge, jobs from the daily fake."""

    def __init__(self, settings, dashboard):
        self.inner = make_daily_client(settings)
        self.metrics = FakeBridge(dashboard=dashboard)

    def close(self):
        pass

    def call_many(self, calls):
        if calls[0][0] in ("get_freelancer_dashboard", "get_profile", "find_freelancers"):
            return self.metrics.call_many(calls)
        return self.inner.call_many(calls)


@pytest.mark.metrics
def test_daily_run_mails_new_invites_and_notes_them(conn, settings, config, outbox):
    settings.set("rank.enabled", False)
    client = Combo(settings, dashboard_with(invitations_payload()))
    res = daily.run_daily(conn, settings, config, client, now=NOW)
    assert res.status == "ok"
    assert len(events(conn, "invite")) == 2 and len(outbox) >= 2
    note = (config.memory_dir / "daily" / "2026-11-06.md").read_text()
    assert "New invite" in note
    # the next run 13 hours later: the reminder, once
    later = NOW + timedelta(hours=13)
    daily.run_daily(
        conn, settings, config, Combo(settings, dashboard_with(invitations_payload())), now=later
    )
    assert len(events(conn, "invite_reminder")) == 2


@pytest.mark.metrics
def test_daily_run_jss_drop_sets_the_guard(conn, settings, config, outbox):
    settings.set("rank.enabled", False)
    settings.set("metrics.profile_key", "~01sample")
    add_metrics(conn, TODAY - timedelta(days=1), 100)
    client = Combo(settings, None)
    client.metrics.jss = {"job_success_score": 97}
    daily.run_daily(conn, settings, config, client, now=NOW)
    assert alerts.jss_guard_on(settings) and events(conn, "jss_drop")


# ================================================================ charts


def add_proposal(conn, pid, created, status="Accepted", viewed=0):
    conn.execute(
        "INSERT INTO proposals(proposal_id, created_at, status, viewed_by_client) VALUES (?,?,?,?)",
        (pid, created.isoformat(), status, viewed),
    )


def test_nice_ceiling():
    assert [charts.nice_ceiling(v) for v in (0, 1, 2, 3, 6, 11, 23, 101)] == [
        1,
        1,
        2,
        5,
        10,
        20,
        50,
        200,
    ]


def test_funnel_counts_by_the_week_sent(conn, settings):
    monday = datetime(2026, 11, 2, 9, 0, tzinfo=UTC)
    add_proposal(conn, "a", monday, viewed=1)
    add_proposal(conn, "b", monday + timedelta(days=2), status="Offered", viewed=1)
    add_proposal(conn, "c", monday - timedelta(days=7), status="Hired", viewed=1)
    add_proposal(conn, "old", monday - timedelta(days=400))
    data = charts.funnel_data(conn, settings, UTC, NOW)
    assert len(data["weeks"]) == 8 and data["total"] == 3
    this, last = data["weeks"][-1], data["weeks"][-2]
    assert (this["sent"], this["viewed"], this["interviews"], this["hires"]) == (2, 2, 1, 0)
    assert (last["sent"], last["viewed"], last["interviews"], last["hires"]) == (1, 1, 0, 1)
    assert data["weeks"][0]["sent"] == 0


def test_funnel_weeks_is_a_setting(conn, settings):
    settings.set("ui.funnel_weeks", 3)
    assert len(charts.funnel_data(conn, settings, UTC, NOW)["weeks"]) == 3


def test_funnel_svg_has_bars_and_no_script(conn, settings):
    add_proposal(conn, "a", NOW, viewed=1)
    svg = str(charts.funnel_svg(charts.funnel_data(conn, settings, UTC, NOW)))
    assert svg.startswith("<svg") and svg.count('class="bar c1"') == 8
    assert (
        'class="bar c4"' in svg
        and "<script" not in svg
        and "http://" not in svg.replace("http://www.w3.org/2000/svg", "")
    )
    assert "Proposal funnel by week" in svg


def test_funnel_without_proposals_is_none(conn, settings):
    assert charts.funnel_svg(charts.funnel_data(conn, settings, UTC, NOW)) is None


def rank_entry(conn, settings, keyword="alpha"):
    return charts.rank_data(conn, settings, UTC, NOW, [keyword])[0]


def test_rank_svg_y_axis_is_reversed(conn, settings):
    add_rank(conn, TODAY - timedelta(days=2), "alpha", 30)
    add_rank(conn, TODAY - timedelta(days=1), "alpha", 10)
    add_rank(conn, TODAY, "alpha", 20)
    svg = str(charts.rank_svg(rank_entry(conn, settings)))
    ys = {m[0]: float(m[1]) for m in re.findall(r"<title>(\w+ \d+): place (\d+)</title>", svg)}
    cy = [float(v) for v in re.findall(r'<circle class="dot" cx="[\d.]+" cy="([\d.]+)"', svg)]
    assert len(cy) == 3
    assert cy[1] < cy[2] < cy[0]  # place 10 is drawn above place 20, above place 30
    assert 'class="line"' in svg and "<script" not in svg
    assert "lower is better" in svg
    assert ys


def test_rank_svg_breaks_the_line_on_a_missing_day_and_marks_it(conn, settings):
    for b, pos in {4: 12, 3: 11, 2: None, 1: 10, 0: 9}.items():
        add_rank(conn, TODAY - timedelta(days=b), "alpha", pos)
    svg = str(charts.rank_svg(rank_entry(conn, settings)))
    assert svg.count('class="line"') == 2 and svg.count('class="miss"') == 1
    assert svg.count('class="dot"') == 4 and "not found" in svg


def test_rank_svg_single_point_and_empty(conn, settings):
    assert charts.rank_svg(rank_entry(conn, settings)) is None
    add_rank(conn, TODAY, "alpha", 5)
    svg = str(charts.rank_svg(rank_entry(conn, settings)))
    assert svg.count('class="dot"') == 1 and 'class="line"' not in svg


def test_rank_chart_escapes_the_keyword(conn, settings):
    add_rank(conn, TODAY, '<b>"x"&', 5)
    svg = str(charts.rank_svg(rank_entry(conn, settings, '<b>"x"&')))
    assert "<b>" not in svg and "&lt;b&gt;" in svg


def test_rank_chart_days_is_a_setting(conn, settings):
    settings.set("ui.rank_chart_days", 10)
    add_rank(conn, TODAY - timedelta(days=15), "alpha", 5)
    add_rank(conn, TODAY - timedelta(days=3), "alpha", 6)
    assert len(rank_entry(conn, settings)["points"]) == 1


def test_css_has_light_and_dark_chart_colors():
    css = open("app/static/app.css").read()
    dark_at = css.rindex("@media (prefers-color-scheme: dark)")
    assert "--chart-1" in css[:dark_at]  # the light set
    dark = css[dark_at:]
    assert "--chart-1" in dark and "--chart-line" in dark and "--chart-miss" in dark


# ================================================================ dashboard and pages


@pytest.fixture
def web(config):
    app, client = make_web_client(config)
    setup_admin(client)
    app.state.settings.set("upwork.talent_org_uid", ORG)
    app.state.settings.set("rank.keywords", ["alpha", "beta"])
    return app, client


def test_dashboard_empty_states(web):
    _, client = web
    html = client.get("/").text
    assert 'id="funnel-chart"' in html and "No proposals stored" in html
    main = re.sub(r'<svg class="icon".*?</svg>', "", html.split("<main", 1)[1], flags=re.S)
    assert "<svg" not in main  # no chart drawn (icons are not charts)
    assert 'id="rank-charts"' in html and html.count("No checks in this period yet") == 2


def test_dashboard_shows_both_charts_with_data(web):
    app, client = web
    conn = app.state.db
    add_proposal(conn, "a", datetime.now(UTC), viewed=1)
    today = datetime.now(UTC).date()
    for b, pos in {3: 14, 2: 12, 1: 13, 0: 9}.items():
        add_rank(conn, today - timedelta(days=b), "alpha", pos)
    html = client.get("/").text
    assert 'aria-labelledby="funnel-title funnel-desc"' in html and "swatch c1" in html
    assert html.count('class="chart"') == 2  # the funnel and the one keyword with data
    assert "Rank, last 30 days" in html and "No checks in this period yet" in html  # beta


def test_dashboard_jss_guard_banner_and_clear_button(web):
    app, client = web
    s = app.state.settings
    assert "JSS guard is ON" not in client.get("/").text
    s.set("safety.jss_guard", True)
    s.set("safety.jss_guard_note", "2026-11-06: JSS fell from 100 to 97")
    html = client.get("/").text
    assert "JSS guard is ON" in html and "fell from 100 to 97" in html
    token = csrf_from(html)
    r = client.post("/safety/jss-guard/clear", data={"csrf_token": token}, follow_redirects=False)
    assert r.status_code == 303 and s.get("safety.jss_guard") is False
    assert "JSS guard is ON" not in client.get("/").text


def test_clear_guard_needs_csrf(web):
    app, client = web
    app.state.settings.set("safety.jss_guard", True)
    r = client.post("/safety/jss-guard/clear", data={}, follow_redirects=False)
    assert r.status_code in (400, 403) and app.state.settings.get("safety.jss_guard") is True


# ---- "Can't afford" on the job list


def run_jobs(app, config, balance):
    s = app.state.settings
    return daily.run_daily(app.state.db, s, config, make_daily_client(s, balance=balance))


def test_jobs_that_cost_more_than_the_balance_get_a_pill(web, config):
    app, client = web
    run_jobs(app, config, balance=5)
    conn = app.state.db
    n_costly = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE status != 'dropped' AND connects_cost > 5"
    ).fetchone()[0]
    assert n_costly > 0
    html = client.get("/jobs").text
    assert pills(html) == n_costly
    assert 'title="Costs ' in html


def pills(html):
    return html.count('class="pill warn cant-afford"')


def test_no_pill_when_the_balance_is_enough(web, config):
    app, client = web
    run_jobs(app, config, balance=500)
    assert pills(client.get("/jobs").text) == 0


def test_filter_hides_unaffordable_jobs(web, config):
    app, client = web
    run_jobs(app, config, balance=5)
    all_cards = client.get("/jobs?afford=show").text.count("<tr data-href=")
    shown = pills(client.get("/jobs?afford=show").text)
    assert shown > 0
    hidden = client.get("/jobs?afford=hide").text
    assert pills(hidden) == 0
    assert hidden.count("<tr data-href=") == all_cards - shown


def test_setting_hides_by_default_and_the_url_overrides_it(web, config):
    app, client = web
    run_jobs(app, config, balance=5)
    default_pills = pills(client.get("/jobs").text)
    assert default_pills > 0  # the setting is off: shown with the pill
    app.state.settings.set("ui.hide_unaffordable", True)
    assert pills(client.get("/jobs").text) == 0
    assert pills(client.get("/jobs?afford=show").text) == default_pills
    assert "default (hide)" in client.get("/jobs").text


def test_unknown_afford_value_falls_back_to_the_setting(web, config):
    app, client = web
    run_jobs(app, config, balance=5)
    assert pills(client.get("/jobs?afford=zzz").text) == pills(client.get("/jobs").text)


def test_pagination_keeps_the_afford_choice(web, config):
    app, client = web
    run_jobs(app, config, balance=500)
    app.state.settings.set("ui.jobs_page_size", 2)
    html = client.get("/jobs?afford=show").text
    assert "afford=show" in html and "page=2" in html


def test_pill_shows_for_unjudged_jobs_too(web, config):
    app, client = web
    run_jobs(app, config, balance=5)
    assert app.state.db.execute("SELECT COUNT(*) FROM judgements").fetchone()[0] == 0
    assert pills(client.get("/jobs").text) > 0


def test_unknown_balance_never_hides_and_has_no_pill(web, config):
    app, client = web
    run_jobs(app, config, balance=500)
    app.state.db.execute("DELETE FROM connects")
    app.state.settings.set("ui.hide_unaffordable", True)
    html = client.get("/jobs").text
    assert pills(html) == 0 and html.count("<tr data-href=") > 0
