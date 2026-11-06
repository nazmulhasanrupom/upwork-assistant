"""Keyword rank (plan X2): paging, found on page 2, not found, boosted rows, missing profile
key, once per day, averages, the daily wiring and the dashboard table.
A fake bridge answers `find_freelancers search`; no Upwork and no Claude."""

import contextlib
import copy
from datetime import UTC, date, datetime

import pytest

from app import rank
from app.db import init_db
from app.settings import Settings
from app.tasks import daily
from app.upwork import bridge
from app.upwork.client import UpworkAuthError, UpworkToolError
from tests.test_auth import make_client as make_web_client
from tests.test_auth import setup_admin
from tests.test_daily import make_client as make_daily_client
from tests.upwork_fakes import load, to_result

ORG = "1000000000000000001"
MINE = "~01mmmmmmmmmmmmmmm1"  # the third row of the real-shaped search sample
NOW = datetime(2026, 11, 6, 12, 0, tzinfo=UTC)  # 12:00 UTC; run_hour_local is 6
TODAY = "2026-11-06"


def person(key, boosted=False):
    return {"profile_key": key, "boosted": boosted, "person_id": "9" + key[-6:], "name": "X"}


def page(*rows, more=False, next_offset=None):
    out = {"freelancers": list(rows), "hasMore": more, "status": "ok"}
    if next_offset is not None:
        out["next_offset"] = next_offset
    return out


class FakeSearch:
    """Answers `find_freelancers search` from `pages[keyword] = [page0, page1, ...]` (the page
    is picked by offset / page size). `batches` keeps every call_many request."""

    def __init__(self, pages=None, default=None):
        self.pages = pages or {}
        self.default = default if default is not None else page(person("~01other"))
        self.batches: list[list[tuple[str, dict]]] = []

    @property
    def calls(self):
        return [c for batch in self.batches for c in batch]

    def close(self):
        pass

    def call_many(self, calls):
        self.batches.append(copy.deepcopy(calls))
        out = []
        for name, args in calls:
            assert name == "find_freelancers" and args["action"] == "search"
            params = args["params"]
            plist = self.pages.get(params["query"])
            if plist is None:
                payload = self.default
            elif isinstance(plist, Exception):
                payload = plist
            else:
                # the page whose start offset equals the asked offset
                starts = [0]
                for p in plist[:-1]:
                    starts.append(p.get("next_offset", starts[-1] + len(p["freelancers"])))
                payload = plist[starts.index(params["offset"])]
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
    s.set("metrics.profile_key", MINE)
    s.set("rank.keywords", ["alpha", "beta"])
    return s


def stored(conn):
    return {
        r["keyword"]: dict(r)
        for r in conn.execute("SELECT * FROM rank_daily WHERE date = ?", (TODAY,))
    }


# ---------------------------------------------------------------- settings and bridge


def test_defaults_come_from_the_config_file(conn):
    s = Settings(conn)
    assert s.get("rank.enabled") is True
    assert s.get("rank.page_size") == 25 and s.get("rank.max_pages") == 4
    assert s.get("rank.run_hour_local") == 6
    assert rank.approved_keywords(conn, s) == [
        "n8n automation",
        "AI automation",
        "AI agent developer",
        "Make.com",
        "Zapier",
        "RAG chatbot",
        "Python automation",
        "workflow automation",
    ]


def test_approved_keywords_hook_cleans_the_setting(conn, settings):
    settings.set("rank.keywords", [" n8n   automation ", "N8N automation", "", 5, "Zapier"])
    assert rank.approved_keywords(conn, settings) == ["n8n automation", "Zapier"]
    settings.set("rank.max_keywords", 1)
    assert rank.approved_keywords(conn, settings) == ["n8n automation"]
    settings.set("rank.keywords", [])
    assert rank.approved_keywords(conn, settings) == []


def test_only_read_calls_with_the_search_shape(conn, settings):
    fake = FakeSearch()
    rank.check_ranks(fake, conn, settings, NOW, tz="UTC")
    for name, args in fake.calls:
        bridge.check_tools([name])
        assert args["action"] == "search" and args["org_uid"] == ORG
        assert set(args["params"]) == {"query", "limit", "offset"}
        assert args["params"]["limit"] == 25


# ---------------------------------------------------------------- positions


def test_found_on_page_one_with_a_boosted_row_before_me(conn, settings):
    sample = load("find_freelancers_search.json")  # rows: A, B (boosted), me
    fake = FakeSearch({"alpha": [sample]})
    res = rank.check_ranks(fake, conn, settings, NOW, tz="UTC")
    row = stored(conn)["alpha"]
    assert row["position"] == 3 and row["organic_position"] == 2
    assert row["pages_checked"] == 1 and row["total_seen"] == 3
    assert {r["keyword"] for r in res.rows} == {"alpha", "beta"}
    assert len(fake.batches) == 1  # beta is not found and has no next page


def test_my_own_boosted_row_still_counts_in_both(conn, settings):
    p = page(person("~01a", boosted=True), person(MINE, boosted=True), person("~01c"))
    rank.check_ranks(FakeSearch({"alpha": [p], "beta": [p]}), conn, settings, NOW, tz="UTC")
    assert stored(conn)["alpha"]["position"] == 2
    assert stored(conn)["alpha"]["organic_position"] == 1  # the boosted row before me is skipped


def test_found_on_page_two_asks_page_two_only_for_keywords_not_found(conn, settings):
    settings.set("rank.keywords", ["alpha", "beta", "gamma"])
    p1 = page(person("~01a"), person("~01b", True), more=True, next_offset=2)
    p2 = page(person("~01c"), person(MINE), more=True, next_offset=4)
    found_first = page(person(MINE))
    fake = FakeSearch({"alpha": [p1, p2], "beta": [found_first], "gamma": [p1, p2]})
    rank.check_ranks(fake, conn, settings, NOW, tz="UTC")
    assert len(fake.batches) == 2
    assert [a["params"]["query"] for _, a in fake.batches[0]] == ["alpha", "beta", "gamma"]
    assert [a["params"]["query"] for _, a in fake.batches[1]] == ["alpha", "gamma"]
    assert {a["params"]["offset"] for _, a in fake.batches[0]} == {0}
    assert {a["params"]["offset"] for _, a in fake.batches[1]} == {2}
    rows = stored(conn)
    assert (rows["alpha"]["position"], rows["alpha"]["organic_position"]) == (4, 3)
    assert rows["alpha"]["pages_checked"] == 2 and rows["alpha"]["total_seen"] == 4
    assert rows["beta"]["position"] == 1 and rows["beta"]["pages_checked"] == 1


def test_next_offset_missing_falls_back_to_rows_seen(conn, settings):
    p1 = page(person("~01a"), person("~01b"), more=True)  # no next_offset
    p2 = page(person(MINE))
    fake = FakeSearch({"alpha": [p1, p2]})
    settings.set("rank.keywords", ["alpha"])
    rank.check_ranks(fake, conn, settings, NOW, tz="UTC")
    assert fake.calls[1][1]["params"]["offset"] == 2
    assert stored(conn)["alpha"]["position"] == 3


def test_not_found_when_the_results_end(conn, settings):
    settings.set("rank.keywords", ["alpha"])
    fake = FakeSearch({"alpha": [page(person("~01a"), person("~01b"), more=False)]})
    res = rank.check_ranks(fake, conn, settings, NOW, tz="UTC")
    row = stored(conn)["alpha"]
    assert row["position"] is None and row["organic_position"] is None
    assert row["pages_checked"] == 1 and row["total_seen"] == 2
    assert res.failed == {}


def test_not_found_stops_at_max_pages(conn, settings):
    settings.set("rank.keywords", ["alpha"])
    settings.set("rank.max_pages", 3)
    pages = [page(person(f"~01x{i}"), more=True, next_offset=i + 1) for i in range(10)]
    fake = FakeSearch({"alpha": pages})
    rank.check_ranks(fake, conn, settings, NOW, tz="UTC")
    assert len(fake.batches) == 3
    row = stored(conn)["alpha"]
    assert row["position"] is None and row["pages_checked"] == 3 and row["total_seen"] == 3


def test_page_size_and_max_pages_are_settings(conn, settings):
    settings.set("rank.keywords", ["alpha"])
    settings.set("rank.page_size", 10)
    fake = FakeSearch({"alpha": [page(person("~01a"))]})
    rank.check_ranks(fake, conn, settings, NOW, tz="UTC")
    assert fake.calls[0][1]["params"]["limit"] == 10


def test_empty_page_with_more_flag_does_not_loop(conn, settings):
    settings.set("rank.keywords", ["alpha"])
    fake = FakeSearch({"alpha": [page(more=True, next_offset=0)]})
    rank.check_ranks(fake, conn, settings, NOW, tz="UTC")
    assert len(fake.batches) == 1
    assert stored(conn)["alpha"]["position"] is None


def test_same_day_check_updates_the_row(conn, settings):
    settings.set("rank.keywords", ["alpha"])
    rank.check_ranks(FakeSearch({"alpha": [page(person("~01a"))]}), conn, settings, NOW, tz="UTC")
    rank.check_ranks(
        FakeSearch({"alpha": [page(person("~01a"), person(MINE))]}), conn, settings, NOW, tz="UTC"
    )
    assert conn.execute("SELECT COUNT(*) FROM rank_daily").fetchone()[0] == 1
    assert stored(conn)["alpha"]["position"] == 2


def test_local_day_follows_the_time_zone(conn, settings):
    settings.set("rank.keywords", ["alpha"])
    late = datetime(2026, 11, 6, 23, 30, tzinfo=UTC)
    rank.check_ranks(FakeSearch(), conn, settings, late, tz="Asia/Dhaka")  # already 7 Nov there
    assert conn.execute("SELECT date FROM rank_daily").fetchone()[0] == "2026-11-07"


# ---------------------------------------------------------------- problems


def test_missing_profile_key_is_skipped_and_makes_no_call(conn, settings):
    settings.set("metrics.profile_key", "")
    fake = FakeSearch()
    with pytest.raises(rank.RankSkipped, match="profile key"):
        rank.check_ranks(fake, conn, settings, NOW, tz="UTC")
    assert fake.calls == []
    assert conn.execute("SELECT COUNT(*) FROM rank_daily").fetchone()[0] == 0


def test_missing_account_is_skipped(conn, settings):
    settings.set("upwork.talent_org_uid", "")
    with pytest.raises(rank.RankSkipped):
        rank.check_ranks(FakeSearch(), conn, settings, NOW, tz="UTC")


def test_no_keywords_makes_no_call(conn, settings):
    settings.set("rank.keywords", [])
    fake = FakeSearch()
    assert rank.check_ranks(fake, conn, settings, NOW, tz="UTC").rows == []
    assert fake.calls == []


def test_a_failed_keyword_stores_no_row_and_the_others_go_on(conn, settings):
    fake = FakeSearch({"alpha": UpworkToolError("boom"), "beta": [page(person(MINE))]})
    res = rank.check_ranks(fake, conn, settings, NOW, tz="UTC")
    assert set(stored(conn)) == {"beta"}  # no false "not found" for alpha
    assert "boom" in res.failed["alpha"]


def test_a_malformed_answer_is_a_failed_keyword(conn, settings):
    fake = FakeSearch({"alpha": [{"status": "ok"}], "beta": [page(person(MINE))]})
    res = rank.check_ranks(fake, conn, settings, NOW, tz="UTC")
    assert set(res.failed) == {"alpha"} and set(stored(conn)) == {"beta"}


def test_a_login_error_is_raised_but_finished_keywords_stay(conn, settings):
    class Breaks(FakeSearch):
        def call_many(self, calls):
            if len(self.batches) == 1:
                raise UpworkAuthError("expired")
            return super().call_many(calls)

    p1 = page(person("~01a"), more=True, next_offset=1)
    fake = Breaks({"alpha": [p1, page(person(MINE))], "beta": [page(person(MINE))]})
    with pytest.raises(UpworkAuthError):
        rank.check_ranks(fake, conn, settings, NOW, tz="UTC")
    assert set(stored(conn)) == {"beta"}


# ---------------------------------------------------------------- once per day


def test_is_due_follows_hour_enabled_and_todays_rows(conn, settings):
    early = datetime(2026, 11, 6, 5, 59, tzinfo=UTC)
    assert rank.is_due(conn, settings, early, "UTC") is False
    assert rank.is_due(conn, settings, NOW, "UTC") is True
    settings.set("rank.run_hour_local", 13)
    assert rank.is_due(conn, settings, NOW, "UTC") is False
    settings.set("rank.run_hour_local", 6)
    rank.check_ranks(FakeSearch(), conn, settings, NOW, tz="UTC")
    assert rank.is_due(conn, settings, NOW, "UTC") is False  # rows exist for today
    tomorrow = datetime(2026, 11, 7, 12, 0, tzinfo=UTC)
    assert rank.is_due(conn, settings, tomorrow, "UTC") is True
    settings.set("rank.enabled", False)
    assert rank.is_due(conn, settings, tomorrow, "UTC") is False


def test_is_due_uses_the_local_hour(conn, settings):
    # 23:30 UTC is 05:30 on the next day in Dhaka (UTC+6): too early there
    t = datetime(2026, 11, 6, 23, 30, tzinfo=UTC)
    assert rank.is_due(conn, settings, t, "Asia/Dhaka") is False
    t = datetime(2026, 11, 7, 0, 30, tzinfo=UTC)  # 06:30 in Dhaka
    assert rank.is_due(conn, settings, t, "Asia/Dhaka") is True


# ---------------------------------------------------------------- averages


def add_rank(conn, day, keyword, position, organic=None):
    conn.execute(
        "INSERT INTO rank_daily(date, keyword, position, organic_position, pages_checked, "
        "total_seen, created_at) VALUES (?,?,?,?,?,?,?)",
        (day, keyword, position, organic, 1, 25, "2026-11-01T00:00:00+00:00"),
    )


def test_avg_7d_ignores_null_and_the_window(conn):
    end = date(2026, 11, 6)
    add_rank(conn, "2026-10-29", "k", 1)  # outside: 7 days end 6 Nov = 31 Oct to 6 Nov
    add_rank(conn, "2026-10-31", "k", 10, 8)
    add_rank(conn, "2026-11-03", "k", None)
    add_rank(conn, "2026-11-06", "k", 20, 14)
    add_rank(conn, "2026-11-06", "other", 99)
    assert rank.avg_7d(conn, "k", end) == 15.0
    assert rank.avg_7d(conn, "k", end, organic=True) == 11.0
    assert rank.avg_7d(conn, "k", end, days=1) == 20.0
    assert rank.not_found_count(conn, "k", end) == 1


def test_avg_7d_is_none_without_data(conn):
    end = date(2026, 11, 6)
    assert rank.avg_7d(conn, "nope", end) is None
    add_rank(conn, "2026-11-05", "k", None)
    assert rank.avg_7d(conn, "k", end) is None  # only "not found" days
    assert rank.not_found_count(conn, "k", end) == 1
    assert rank.not_found_count(conn, "nope", end) == 0


# ---------------------------------------------------------------- daily wiring


class DailyBridge:
    def __init__(self, settings, search):
        self.inner = make_daily_client(settings)
        self.search = search

    def close(self):
        pass

    def call_many(self, calls):
        if calls and calls[0][0] == "find_freelancers":
            return self.search.call_many(calls)
        return self.inner.call_many(calls)


def run_daily(conn, settings, config, search, now=NOW):
    return daily.run_daily(conn, settings, config, DailyBridge(settings, search), now=now)


@pytest.mark.rank
def test_daily_run_stores_ranks_and_writes_the_note(conn, settings, config):
    search = FakeSearch({"alpha": [load("find_freelancers_search.json")]})
    assert run_daily(conn, settings, config, search).status == "ok"
    assert stored(conn)["alpha"]["position"] == 3
    note = (config.memory_dir / "daily" / f"{TODAY}.md").read_text()
    assert "Keyword rank" in note and "alpha: #3 (organic #2)" in note


@pytest.mark.rank
def test_second_daily_run_the_same_day_makes_no_search(conn, settings, config):
    search = FakeSearch()
    run_daily(conn, settings, config, search)
    first = len(search.calls)
    assert first > 0
    run_daily(conn, settings, config, search, now=datetime(2026, 11, 6, 14, 0, tzinfo=UTC))
    assert len(search.calls) == first
    next_day = datetime(2026, 11, 7, 12, 0, tzinfo=UTC)
    run_daily(conn, settings, config, search, now=next_day)
    assert len(search.calls) == first * 2


@pytest.mark.rank
def test_daily_run_before_the_hour_makes_no_search(conn, settings, config):
    search = FakeSearch()
    run_daily(conn, settings, config, search, now=datetime(2026, 11, 6, 4, 0, tzinfo=UTC))
    assert search.calls == []


@pytest.mark.rank
def test_rank_off_makes_no_search(conn, settings, config):
    settings.set("rank.enabled", False)
    search = FakeSearch()
    assert run_daily(conn, settings, config, search).status == "ok"
    assert search.calls == []


@pytest.mark.rank
def test_missing_profile_key_is_an_alert_and_the_run_stays_ok(conn, settings, config):
    settings.set("metrics.profile_key", "")
    search = FakeSearch()
    result = run_daily(conn, settings, config, search)
    assert result.status == "ok" and search.calls == []
    alert = conn.execute("SELECT message FROM alerts WHERE key = 'rank_failed'").fetchone()
    assert alert and "profile key" in alert["message"]
    # the key arrives later: the check runs and the alert is gone
    settings.set("metrics.profile_key", MINE)
    run_daily(conn, settings, config, search, now=datetime(2026, 11, 6, 14, 0, tzinfo=UTC))
    assert conn.execute("SELECT COUNT(*) FROM alerts WHERE key = 'rank_failed'").fetchone()[0] == 0
    assert stored(conn)


@pytest.mark.rank
def test_a_failed_keyword_is_an_alert_and_the_run_stays_ok(conn, settings, config):
    search = FakeSearch({"alpha": UpworkToolError("boom")})
    assert run_daily(conn, settings, config, search).status == "ok"
    alert = conn.execute("SELECT message FROM alerts WHERE key = 'rank_failed'").fetchone()
    assert alert and "alpha" in alert["message"]
    assert set(stored(conn)) == {"beta"}


@pytest.mark.rank
def test_a_login_error_in_rank_does_not_fail_the_run(conn, settings, config):
    search = FakeSearch({"alpha": UpworkAuthError("expired"), "beta": UpworkAuthError("x")})
    assert run_daily(conn, settings, config, search).status == "ok"
    assert conn.execute("SELECT 1 FROM alerts WHERE key = 'rank_failed'").fetchone()


class BudgetClient:
    """A daily client that knows `call_budget` (like the real bridge) and records its use."""

    def __init__(self, search):
        self.search = search
        self.scopes: list[str] = []
        self.inside: list[bool] = []
        self._open = False

    def close(self):
        pass

    @contextlib.contextmanager
    def call_budget(self, key):
        self.scopes.append(key)
        self._open = True
        try:
            yield
        finally:
            self._open = False

    def call_many(self, calls):
        self.inside.append(self._open)
        if calls[0][0] == "get_profile":
            return self.profile(calls)
        return self.search.call_many(calls)

    def profile(self, calls):
        return [to_result(n, load("get_profile.json")) for n, _ in calls]


@pytest.mark.rank
def test_rank_runs_inside_its_own_call_budget(conn, settings, config):
    client = BudgetClient(FakeSearch())
    assert daily.rank_part(conn, settings, config, client, NOW)
    assert client.scopes == ["rank.max_mcp_calls"]
    assert client.inside and all(client.inside)  # every rank call was made inside the scope
    assert settings.get("rank.max_mcp_calls") == 32


@pytest.mark.rank
def test_rank_learns_the_profile_key_itself_even_when_metrics_are_off(conn, settings, config):
    settings.set("metrics.profile_key", "")
    settings.set("metrics.enabled", False)
    client = BudgetClient(FakeSearch())
    lines = daily.rank_part(conn, settings, config, client, NOW)
    assert settings.get("metrics.profile_key") == "~01sample"
    assert stored(conn) and lines
    assert conn.execute("SELECT COUNT(*) FROM alerts WHERE key = 'rank_failed'").fetchone()[0] == 0


@pytest.mark.rank
def test_rank_has_calls_left_when_the_shared_budget_is_used_up(
    conn, settings, config, fake_claude_stream, tmp_path, monkeypatch
):
    """Real bridge + fake claude: the jobs part used all of upwork.max_mcp_calls_per_run, and
    the rank check still runs on its own budget. The run total counts both."""
    import json as _json

    from app.upwork.bridge import ClaudeUpworkBridge

    rules = [
        {
            "tool": "find_freelancers",
            "args": {"action": "search"},
            "text": _json.dumps(page(person("~01other"), person(MINE))),
        }
    ]
    rules_file = tmp_path / "rules.json"
    rules_file.write_text(_json.dumps(rules))
    monkeypatch.setenv("FAKE_UPWORK_RULES", str(rules_file))
    settings.set("upwork.max_mcp_calls_per_run", 5)
    conn.execute(
        "INSERT INTO runs(run_id, task, started, status, mcp_calls) "
        "VALUES ('r1','daily','x','running', 5)"
    )  # the jobs part has used everything
    b = ClaudeUpworkBridge(config, settings, conn, "r1")
    daily.rank_part(conn, settings, config, b, NOW)
    assert set(stored(conn)) == {"alpha", "beta"}
    assert conn.execute("SELECT mcp_calls FROM runs").fetchone()[0] == 5 + 2


@pytest.mark.rank
def test_rank_budget_limit_leaves_the_rest_for_the_next_run(
    conn, settings, config, fake_claude_stream, tmp_path, monkeypatch
):
    import json as _json

    from app.upwork.bridge import ClaudeUpworkBridge

    rules_file = tmp_path / "rules.json"
    rules_file.write_text(
        _json.dumps(
            [
                {
                    "tool": "find_freelancers",
                    "args": {"action": "search"},
                    "text": _json.dumps(page(person("~01other"))),
                }
            ]
        )
    )
    monkeypatch.setenv("FAKE_UPWORK_RULES", str(rules_file))
    settings.set("rank.max_mcp_calls", 1)  # alpha only
    b = ClaudeUpworkBridge(config, settings, conn, None)
    lines = daily.rank_part(conn, settings, config, b, NOW)
    assert set(stored(conn)) == {"alpha"}
    assert conn.execute("SELECT 1 FROM alerts WHERE key = 'rank_failed'").fetchone()
    assert any("Not checked: beta" in line for line in lines)


def test_other_daily_tests_stay_unchanged_by_default(conn, settings, config):
    # without the marker the autouse stub keeps the rank part out of the daily run
    assert daily.rank_part(conn, settings, config, object(), NOW) == []


@pytest.mark.rank
def test_kill_switch_runs_no_rank(conn, settings, config):
    settings.set("general.kill_switch", True)
    search = FakeSearch()
    run_daily(conn, settings, config, search)
    assert search.calls == []


# ---------------------------------------------------------------- dashboard


@pytest.fixture
def web(config):
    app, client = make_web_client(config)
    setup_admin(client)
    app.state.settings.set("rank.keywords", ["alpha", "beta"])
    return app, client


def test_dashboard_table_before_any_check(web):
    _, client = web
    html = client.get("/").text
    assert 'id="rank-table"' in html and "alpha" in html and "No rank check yet" in html


def test_dashboard_table_shows_today_average_and_organic(web):
    app, client = web
    conn = app.state.db
    today = datetime.now(UTC).date()
    for back, pos, org in ((2, 10, 8), (1, None, None), (0, 6, 4)):
        add_rank(conn, (today.fromordinal(today.toordinal() - back)).isoformat(), "alpha", pos, org)
    html = client.get("/").text
    assert "#6" in html and "#8.0" in html and "#4" in html
    assert "1 not found" in html
    assert "No rank check yet" not in html


def test_dashboard_table_not_found_today(web):
    app, client = web
    add_rank(app.state.db, datetime.now(UTC).date().isoformat(), "beta", None)
    assert "not in top 25" in client.get("/").text


def test_dashboard_table_hidden_when_rank_is_off(web):
    app, client = web
    app.state.settings.set("rank.enabled", False)
    assert 'id="rank-table"' not in client.get("/").text
