"""Daily metrics snapshot (plan X1): parsing the real dashboard sample, JSS read, funnel from the
proposals table, one row per day, daily wiring, dashboard card and the profile views box.
A fake bridge answers; no Upwork and no Claude."""

import copy
import json
from datetime import UTC, date, datetime

import pytest

from app import metrics
from app.db import init_db
from app.llm.runner import BudgetCapReached
from app.settings import Settings
from app.tasks import daily
from app.upwork import bridge
from app.upwork.client import UpworkAuthError, UpworkToolError
from tests.test_auth import csrf_from, setup_admin
from tests.test_auth import make_client as make_web_client
from tests.test_daily import make_client as make_daily_client
from tests.upwork_fakes import load, to_result

ORG = "1000000000000000001"
KEY = "~01sample"
NOW = datetime(2026, 11, 6, 12, 0, tzinfo=UTC)


class FakeBridge:
    """Answers the three read tools X1 uses. `batches` keeps every call_many request."""

    def __init__(self, dashboard=None, profile=None, jss=None):
        self.dashboard = dashboard if dashboard is not None else load("freelancer_dashboard.json")
        self.profile = profile if profile is not None else load("get_profile.json")
        self.jss = jss if jss is not None else load("find_freelancers_get_profile.json")
        self.batches: list[list[tuple[str, dict]]] = []

    @property
    def calls(self):
        return [c for batch in self.batches for c in batch]

    def close(self):
        pass

    def call_many(self, calls):
        self.batches.append(copy.deepcopy(calls))
        out = []
        for name, _args in calls:
            if name == "get_freelancer_dashboard":
                payload = self.dashboard
            elif name == "get_profile":
                payload = self.profile
            elif name == "find_freelancers":
                payload = self.jss
            else:
                raise AssertionError(f"unexpected tool {name}")
            if isinstance(payload, Exception) and not isinstance(payload, UpworkToolError):
                raise payload
            out.append(to_result(name, copy.deepcopy(payload)))
        return out


@pytest.fixture
def conn():
    return init_db(":memory:")


@pytest.fixture
def settings(conn):
    s = Settings(conn)
    s.set("upwork.talent_org_uid", ORG)
    return s


def add_proposal(conn, pid, created, status="Accepted", viewed=0):
    conn.execute(
        "INSERT INTO proposals(proposal_id, created_at, status, viewed_by_client) VALUES (?,?,?,?)",
        (pid, created, status, viewed),
    )


def dashboard_with_invites(*ids):
    d = load("freelancer_dashboard.json")
    d["data"]["invitations"] = {
        "count": len(ids),
        "items": [{"id": i, "jobPosting": {"id": "1", "title": "t"}} for i in ids],
    }
    return d


# ---------------------------------------------------------------- bridge safety


def test_new_read_tools_are_allowed_and_writes_still_blocked():
    assert "get_freelancer_dashboard" in bridge.READ_TOOLS
    assert "find_freelancers" in bridge.READ_TOOLS
    bridge.check_tools(["get_freelancer_dashboard", "find_freelancers"])
    for name in bridge.READ_TOOLS:
        assert not bridge._FORBIDDEN_RE.match(name), name
    for tool in ("confirm_preview", "manage_proposals", "update_profile", "send_message"):
        with pytest.raises(bridge.ToolNotAllowed):
            bridge.check_tools([tool])


def test_snapshot_only_makes_read_calls(conn, settings):
    fake = FakeBridge()
    metrics.snapshot(fake, conn, settings, NOW)
    names = [n for n, _ in fake.calls]
    assert names
    bridge.check_tools(names)


# ---------------------------------------------------------------- parsing


def test_parse_dashboard_sample():
    d = metrics.parse_dashboard(load("freelancer_dashboard.json"))
    assert d["connects"] == 3 and d["connects_paid"] == 0
    assert d["invites_open"] == 0 and d["offers_open"] == 0
    assert d["messages_awaiting_me"] == 1  # one room waits for "you", one for "them"
    assert d["active_contracts"] == 1
    assert len(d["interview_room_dates"]) == 2


def test_parse_dashboard_missing_parts_are_none():
    d = metrics.parse_dashboard({"data": {}})
    assert d["connects"] is None and d["invites_open"] is None
    assert d["messages_awaiting_me"] is None and d["active_contracts"] is None


@pytest.mark.parametrize(
    "payload, expected",
    [
        ({"job_success_score": 96}, 96.0),
        ({"freelancer": {"jobSuccessScore": "98%"}}, 98.0),
        ({"data": [{"x": 1}, {"job_success_score": 87.5}]}, 87.5),
        ({"job_success_score": None}, None),
        ({"job_success_score": "n/a"}, None),
        ({"job_success_score": 250}, None),
        ({"job_success_score": True}, None),
        ({"nothing": 1}, None),
        ("text", None),
        (None, None),
    ],
)
def test_parse_jss(payload, expected):
    assert metrics.parse_jss(payload) == expected


def test_own_profile_key_from_real_sample():
    assert metrics.own_profile_key(load("get_profile.json")) == KEY
    assert metrics.own_profile_key({"data": {"identity": {"ciphertext": "abc"}}}) is None


def test_guessed_jss_fixture_is_marked_as_guessed():
    assert "GUESSED" in load("find_freelancers_get_profile.json")["_note"]
    readme = (load.__globals__["FIX"] / "README.md").read_text()
    assert "SHAPE GUESSED, NOT CAPTURED" in readme


# ---------------------------------------------------------------- snapshot


def test_first_snapshot_learns_the_key_and_reads_jss(conn, settings):
    fake = FakeBridge()
    row = metrics.snapshot(fake, conn, settings, NOW, tz="UTC")
    assert row["date"] == "2026-11-06"
    assert row["connects"] == 3 and row["jss"] == 96.0
    assert row["invites_open"] == 0 and row["offers_open"] == 0
    assert row["messages_awaiting_me"] == 1 and row["active_contracts"] == 1
    assert settings.get("metrics.profile_key") == KEY
    # batch 1: dashboard + own profile; batch 2: the optional JSS read
    assert [n for n, _ in fake.batches[0]] == ["get_freelancer_dashboard", "get_profile"]
    assert fake.batches[1] == [
        (
            "find_freelancers",
            {"action": "get_profile", "org_uid": ORG, "params": {"profile_key": KEY}},
        )
    ]
    assert fake.batches[0][0][1] == {"action": "check", "org_uid": ORG, "params": {}}


def test_known_key_means_one_batch(conn, settings):
    settings.set("metrics.profile_key", KEY)
    fake = FakeBridge()
    row = metrics.snapshot(fake, conn, settings, NOW, tz="UTC")
    assert len(fake.batches) == 1
    assert [n for n, _ in fake.batches[0]] == ["get_freelancer_dashboard", "find_freelancers"]
    assert row["jss"] == 96.0


def test_no_score_in_the_answer_stores_null_and_does_not_fail(conn, settings):
    settings.set("metrics.profile_key", KEY)
    fake = FakeBridge(jss={"status": "ok", "freelancer": {"name": "x"}})
    row = metrics.snapshot(fake, conn, settings, NOW, tz="UTC")
    assert row["jss"] is None and row["connects"] == 3


def test_jss_call_error_stores_null(conn, settings):
    settings.set("metrics.profile_key", KEY)
    fake = FakeBridge(jss=UpworkToolError("profile not found"))
    row = metrics.snapshot(fake, conn, settings, NOW, tz="UTC")
    assert row["jss"] is None and row["connects"] == 3


def test_second_batch_error_is_swallowed(conn, settings):
    class Flaky(FakeBridge):
        def call_many(self, calls):
            if calls[0][0] == "find_freelancers":
                raise RuntimeError("claude broke")
            return super().call_many(calls)

    row = metrics.snapshot(Flaky(), conn, settings, NOW, tz="UTC")
    assert row["jss"] is None and row["connects"] == 3


def test_second_batch_login_error_is_raised(conn, settings):
    class Expired(FakeBridge):
        def call_many(self, calls):
            if calls[0][0] == "find_freelancers":
                raise UpworkAuthError("expired")
            return super().call_many(calls)

    with pytest.raises(UpworkAuthError):
        metrics.snapshot(Expired(), conn, settings, NOW, tz="UTC")


def test_jss_switch_off_reads_the_dashboard_only(conn, settings):
    settings.set("metrics.jss_enabled", False)
    settings.set("rank.enabled", False)  # nothing needs my profile key
    fake = FakeBridge()
    row = metrics.snapshot(fake, conn, settings, NOW, tz="UTC")
    assert [n for n, _ in fake.calls] == ["get_freelancer_dashboard"]
    assert row["jss"] is None


def test_key_is_learned_for_rank_even_when_jss_is_off(conn, settings):
    settings.set("metrics.jss_enabled", False)  # rank.enabled stays on: it needs the key
    fake = FakeBridge()
    row = metrics.snapshot(fake, conn, settings, NOW, tz="UTC")
    assert [n for n, _ in fake.calls] == ["get_freelancer_dashboard", "get_profile"]
    assert settings.get("metrics.profile_key") == KEY
    assert row["jss"] is None  # no JSS read at all


def test_key_is_not_learned_when_neither_rank_nor_jss_need_it(conn, settings):
    settings.set("metrics.jss_enabled", False)
    settings.set("rank.enabled", False)
    metrics.snapshot(FakeBridge(), conn, settings, NOW, tz="UTC")
    assert settings.get("metrics.profile_key") == ""


def test_ensure_profile_key_learns_once(conn, settings):
    fake = FakeBridge()
    assert metrics.ensure_profile_key(fake, settings) == KEY
    assert metrics.ensure_profile_key(fake, settings) == KEY  # known now: no second call
    assert [n for n, _ in fake.calls] == ["get_profile"]


def test_ensure_profile_key_without_an_answer_is_none(conn, settings):
    fake = FakeBridge(profile={"data": {}})
    assert metrics.ensure_profile_key(fake, settings) is None
    assert settings.get("metrics.profile_key") == ""


def test_no_account_is_an_error(conn):
    with pytest.raises(metrics.MetricsError):
        metrics.snapshot(FakeBridge(), conn, Settings(conn), NOW)


def test_dashboard_failure_is_a_metrics_error(conn, settings):
    with pytest.raises(metrics.MetricsError):
        metrics.snapshot(FakeBridge(dashboard=UpworkToolError("boom")), conn, settings, NOW)


def test_login_error_is_raised(conn, settings):
    with pytest.raises(UpworkAuthError):
        metrics.snapshot(FakeBridge(dashboard=UpworkAuthError("x")), conn, settings, NOW)


def test_one_row_per_local_day_upsert(conn, settings):
    fake = FakeBridge()
    metrics.snapshot(fake, conn, settings, NOW, tz="UTC")
    d = load("freelancer_dashboard.json")
    d["data"]["connects"]["balance"]["connectsBalance"] = 40
    later = datetime(2026, 11, 6, 18, 0, tzinfo=UTC)
    row = metrics.snapshot(FakeBridge(dashboard=d, jss={"x": 1}), conn, settings, later, tz="UTC")
    assert conn.execute("SELECT COUNT(*) FROM metrics_daily").fetchone()[0] == 1
    assert row["connects"] == 40
    assert row["jss"] == 96.0  # a failed JSS read later in the day does not erase the value
    assert row["created_at"] == "2026-11-06T12:00:00+00:00"  # first snapshot of the day


def test_local_day_follows_the_time_zone(conn, settings):
    late = datetime(2026, 11, 6, 23, 30, tzinfo=UTC)  # already Nov 7 in Dhaka
    row = metrics.snapshot(FakeBridge(), conn, settings, late, tz="Asia/Dhaka")
    assert row["date"] == "2026-11-07"


def test_raw_json_has_no_client_text(conn, settings):
    row = metrics.snapshot(FakeBridge(), conn, settings, NOW, tz="UTC")
    assert "untrusted" not in row["raw_json"] and "thank you" not in row["raw_json"]
    assert json.loads(row["raw_json"])["jss_found"] is True


# ---------------------------------------------------------------- funnel


def test_funnel_from_the_proposals_table_without_extra_calls(conn, settings):
    add_proposal(conn, "p1", "2026-11-05T10:00:00+00:00", "Accepted", viewed=1)
    add_proposal(conn, "p2", "2026-11-04T10:00:00+00:00", "Offered", viewed=1)
    add_proposal(conn, "p3", "2026-11-03T10:00:00+00:00", "Hired", viewed=1)
    add_proposal(conn, "p4", "2026-11-02T10:00:00+00:00", "Activated")
    add_proposal(conn, "p5", "2026-11-01T10:00:00+00:00", "Declined")
    add_proposal(conn, "old", "2026-10-01T10:00:00+00:00", "Hired", viewed=1)  # outside 7 days
    fake = FakeBridge()
    row = metrics.snapshot(fake, conn, settings, NOW, tz="UTC")
    assert row["proposals_sent"] == 5 and row["proposals_viewed"] == 3
    assert row["interviews"] == 2  # Offered + Activated
    assert row["hires"] == 1
    assert all(n != "list_freelancer_proposals" for n, _ in fake.calls)


def test_funnel_window_and_statuses_are_settings(conn, settings):
    add_proposal(conn, "old", "2026-10-20T10:00:00+00:00", "Hired", viewed=1)
    settings.set("metrics.funnel_days", 30)
    settings.set("metrics.hire_statuses", ["Hired", "Activated"])
    row = metrics.snapshot(FakeBridge(), conn, settings, NOW, tz="UTC")
    assert row["proposals_sent"] == 1 and row["hires"] == 1


def test_interviews_also_count_new_interview_rooms(conn, settings):
    d = load("freelancer_dashboard.json")
    d["data"]["messages"]["rooms"][0]["createdAtDateTime"] = "2026-11-04T01:00:00+0000"
    row = metrics.snapshot(FakeBridge(dashboard=d), conn, settings, NOW, tz="UTC")
    assert row["interviews"] == 1  # no proposal status, but one interview room in the window


# ---------------------------------------------------------------- invitations


def test_new_invites_count_once_per_id(conn, settings):
    t1 = datetime(2026, 11, 6, 8, 0, tzinfo=UTC)
    r = metrics.snapshot(FakeBridge(dashboard_with_invites("i1", "i2")), conn, settings, t1)
    assert r["invites_open"] == 2 and r["invites_new"] == 2
    # same day, i1 answered, i3 arrived: i1 still counts as seen today
    t2 = datetime(2026, 11, 6, 14, 0, tzinfo=UTC)
    r = metrics.snapshot(FakeBridge(dashboard_with_invites("i2", "i3")), conn, settings, t2)
    assert r["invites_open"] == 2 and r["invites_new"] == 3
    # next day: i3 is old, i4 is new
    t3 = datetime(2026, 11, 7, 9, 0, tzinfo=UTC)
    r = metrics.snapshot(FakeBridge(dashboard_with_invites("i3", "i4")), conn, settings, t3)
    assert r["date"] == "2026-11-07" and r["invites_new"] == 1


def test_empty_dashboard_has_no_new_invites(conn, settings):
    row = metrics.snapshot(FakeBridge(), conn, settings, NOW)
    assert row["invites_open"] == 0 and row["invites_new"] == 0


# ---------------------------------------------------------------- averages and weekly


def put_day(conn, day, **cols):
    names = ["date", "created_at", *cols]
    conn.execute(
        f"INSERT INTO metrics_daily({','.join(names)}) VALUES ({','.join('?' * len(names))})",
        [day, day, *cols.values()],
    )


def test_seven_day_average(conn, settings):
    for i, value in enumerate([10, 20, 30]):
        put_day(conn, f"2026-11-0{i + 1}", connects=value)
    put_day(conn, "2026-11-04", connects=None)  # NULL days are skipped
    put_day(conn, "2026-10-20", connects=1000)  # outside the window
    assert metrics.average(conn, "connects", end=date(2026, 11, 7), days=7) == 20.0
    assert metrics.seven_day_average(conn, settings, "connects", end=date(2026, 11, 7)) == 20.0
    assert metrics.average(conn, "connects", end=date(2026, 11, 2), days=2) == 15.0
    assert metrics.average(conn, "hires", end=date(2026, 11, 7)) is None


def test_average_refuses_unknown_columns(conn):
    with pytest.raises(ValueError):
        metrics.average(conn, "raw_json; DROP TABLE metrics_daily", end=date(2026, 11, 7))


def test_week_start_is_monday():
    assert metrics.week_start(date(2026, 11, 6)) == date(2026, 11, 2)  # a Friday
    assert metrics.week_start(date(2026, 11, 2)) == date(2026, 11, 2)
    assert metrics.week_start(date(2026, 11, 8)) == date(2026, 11, 2)  # a Sunday


def test_save_profile_views(conn, settings):
    assert metrics.save_profile_views(conn, settings, 42, NOW, "UTC") == "2026-11-02"
    metrics.save_profile_views(conn, settings, 50, NOW, "UTC")  # same week: replaced
    rows = conn.execute("SELECT week_start, profile_views FROM metrics_weekly").fetchall()
    assert [(r[0], r[1]) for r in rows] == [("2026-11-02", 50)]
    for bad in (-1, 10**9, "7", True, 1.5):
        with pytest.raises(ValueError):
            metrics.save_profile_views(conn, settings, bad, NOW, "UTC")


# ---------------------------------------------------------------- daily wiring


class DailyBridge:
    """The daily test client plus the dashboard calls."""

    def __init__(self, settings, metrics_fake):
        self.inner = make_daily_client(settings)
        self.fake = metrics_fake

    def close(self):
        pass

    def call_many(self, calls):
        if calls and calls[0][0] in ("get_freelancer_dashboard", "find_freelancers"):
            return self.fake.call_many(calls)
        return self.inner.call_many(calls)


def run_daily(conn, settings, config, fake):
    return daily.run_daily(conn, settings, config, DailyBridge(settings, fake), now=NOW)


@pytest.mark.metrics
def test_daily_run_stores_a_metrics_row(conn, settings, config):
    result = run_daily(conn, settings, config, FakeBridge())
    assert result.status == "ok"
    row = conn.execute("SELECT * FROM metrics_daily").fetchone()
    assert row["date"] == "2026-11-06" and row["connects"] == 3 and row["jss"] == 96.0
    note = (config.memory_dir / "daily" / "2026-11-06.md").read_text()
    assert "Metrics 2026-11-06" in note


@pytest.mark.metrics
def test_daily_run_with_metrics_off_stores_nothing(conn, settings, config):
    settings.set("metrics.enabled", False)
    fake = FakeBridge()
    assert run_daily(conn, settings, config, fake).status == "ok"
    assert conn.execute("SELECT COUNT(*) FROM metrics_daily").fetchone()[0] == 0
    assert fake.calls == []


@pytest.mark.metrics
def test_metrics_error_is_an_alert_and_the_run_stays_ok(conn, settings, config):
    result = run_daily(conn, settings, config, FakeBridge(dashboard=UpworkToolError("boom")))
    assert result.status == "ok"
    alert = conn.execute("SELECT message FROM alerts WHERE key = 'metrics_failed'").fetchone()
    assert alert and "boom" in alert["message"]
    # a good snapshot later clears the alert
    run_daily(conn, settings, config, FakeBridge())
    assert (
        conn.execute("SELECT COUNT(*) FROM alerts WHERE key = 'metrics_failed'").fetchone()[0] == 0
    )


@pytest.mark.metrics
def test_metrics_login_error_does_not_fail_the_run(conn, settings, config):
    result = run_daily(conn, settings, config, FakeBridge(dashboard=UpworkAuthError("expired")))
    assert result.status == "ok"
    assert conn.execute("SELECT 1 FROM alerts WHERE key = 'metrics_failed'").fetchone()


@pytest.mark.metrics
def test_claude_cap_skips_the_snapshot_quietly(conn, settings, config):
    result = run_daily(conn, settings, config, FakeBridge(dashboard=BudgetCapReached("cap")))
    assert result.status == "ok"
    assert conn.execute("SELECT COUNT(*) FROM metrics_daily").fetchone()[0] == 0
    assert (
        conn.execute("SELECT COUNT(*) FROM alerts WHERE key = 'metrics_failed'").fetchone()[0] == 0
    )


def test_kill_switch_runs_no_metrics(conn, settings, config):
    settings.set("general.kill_switch", True)
    fake = FakeBridge()
    daily.run_daily(conn, settings, config, DailyBridge(settings, fake), now=NOW)
    assert fake.calls == []


# ---------------------------------------------------------------- dashboard


@pytest.fixture
def web(config):
    app, client = make_web_client(config)
    setup_admin(client)
    app.state.settings.set("upwork.talent_org_uid", ORG)
    return app, client


def test_dashboard_card_before_the_first_snapshot(web):
    _, client = web
    html = client.get("/").text
    assert "This week" in html and "No snapshot yet" in html and "Profile views" in html


def test_dashboard_card_shows_the_numbers(web):
    app, client = web
    s = app.state.settings
    add_proposal(app.state.db, "p1", datetime.now(UTC).isoformat(timespec="seconds"), viewed=1)
    metrics.snapshot(FakeBridge(), app.state.db, s, datetime.now(UTC), tz="UTC")
    html = client.get("/").text
    assert 'id="metrics-card"' in html
    import re

    def number(stat_id):
        return re.search(rf'id="{stat_id}".*?<strong>([^<]*)</strong>', html, re.S).group(1)

    assert number("m-connects") == "3"
    assert number("m-sent") == "1" and number("m-viewed") == "1"
    assert number("m-jss") == "96%"
    assert number("m-waiting") == "1"


def test_dashboard_card_says_unknown_without_a_score(web):
    app, client = web
    app.state.settings.set("metrics.jss_enabled", False)
    metrics.snapshot(FakeBridge(), app.state.db, app.state.settings, datetime.now(UTC), tz="UTC")
    html = client.get("/").text
    assert "unknown" in html


def test_dashboard_card_hidden_when_metrics_off(web):
    app, client = web
    app.state.settings.set("metrics.enabled", False)
    assert "This week" not in client.get("/").text


def post_views(client, value, token=None):
    token = token or csrf_from(client.get("/").text)
    return client.post("/metrics/profile-views", data={"views": value, "csrf_token": token})


def test_profile_views_box_saves_to_metrics_weekly(web):
    app, client = web
    r = post_views(client, "37")
    assert r.status_code == 303 and r.headers["location"] == "/?msg=views_saved"
    row = app.state.db.execute("SELECT * FROM metrics_weekly").fetchone()
    assert row["profile_views"] == 37
    assert metrics.week_start(datetime.now(UTC).date()).isoformat() == row["week_start"]
    html = client.get("/?msg=views_saved").text
    assert "Profile views saved" in html and 'value="37"' in html
    post_views(client, "40")  # same week: replaced
    assert app.state.db.execute("SELECT COUNT(*) FROM metrics_weekly").fetchone()[0] == 1


@pytest.mark.parametrize("value", ["", "abc", "-3", "1.5", "99999999999"])
def test_profile_views_rejects_bad_numbers(web, value):
    app, client = web
    r = post_views(client, value)
    assert r.status_code == 303 and r.headers["location"] == "/?msg=views_bad"
    assert app.state.db.execute("SELECT COUNT(*) FROM metrics_weekly").fetchone()[0] == 0


def test_profile_views_needs_csrf_and_login(web):
    app, client = web
    r = client.post("/metrics/profile-views", data={"views": "5"})
    assert r.status_code == 403
    assert app.state.db.execute("SELECT COUNT(*) FROM metrics_weekly").fetchone()[0] == 0
    token = csrf_from(client.get("/").text)
    client.cookies.clear()
    r = client.post("/metrics/profile-views", data={"views": "5", "csrf_token": token})
    assert r.status_code in (303, 403) and "views_saved" not in r.headers.get("location", "")
    assert app.state.db.execute("SELECT COUNT(*) FROM metrics_weekly").fetchone()[0] == 0
