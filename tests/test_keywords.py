"""Keyword discovery (plan KW1): numbers per seed, the Claude pick and its checks, the approvals
item, approve / reject, the baseline helper, the email, the Find keywords button.
A fake bridge answers the Upwork reads and a fake claude answers the pick: no real calls."""

import contextlib
import copy
import json
import sys
from datetime import UTC, date, datetime, timedelta

import pytest

from app import approvals, keywords, mail, rank
from app.db import init_db
from app.llm import prompting
from app.settings import Settings
from app.tasks import lock, scheduler, trigger
from app.upwork.client import UpworkAuthError, UpworkToolError
from tests.test_auth import csrf_from, setup_admin
from tests.test_auth import make_client as make_web_client
from tests.upwork_fakes import to_result

ORG = "1000000000000000001"
MINE = "~01mmmmmmmmmmmmmmm1"
NOW = datetime(2026, 11, 6, 12, 0, tzinfo=UTC)
SEEDS = ["alpha", "beta", "gamma", "delta", "epsilon"]
DASH = "—"


def person(key):
    return {"profile_key": key, "boosted": False, "name": "X"}


def fpage(*rows, more=False, total=None):
    out = {"freelancers": list(rows), "hasMore": more, "status": "ok"}
    if total is not None:
        out["total"] = total
    return out


class FakeBridge:
    """The READ bridge. `jobs[keyword]` is a total (int), a payload dict or an Exception;
    `people[keyword]` is a freelancer page payload or an Exception. get_profile answers a
    profile key (or fails when `profile` is an Exception)."""

    def __init__(self, jobs=None, people=None, profile=None):
        self.jobs = jobs if jobs is not None else {}
        self.people = people if people is not None else {}
        self.profile = profile
        self.batches: list[list[tuple[str, dict]]] = []
        self.budgets: list[str] = []
        self.closed = False

    @property
    def calls(self):
        return [c for b in self.batches for c in b]

    @contextlib.contextmanager
    def call_budget(self, key):
        self.budgets.append(key)
        yield

    def close(self):
        self.closed = True

    def call_many(self, calls):
        self.batches.append(copy.deepcopy(calls))
        out = []
        for name, args in calls:
            if name == "find_jobs":
                assert args["action"] == "search" and args["org_uid"] == ORG
                value = self.jobs.get(args["params"]["title"], 100)
                payload = (
                    {"status": "ok", "jobs": [], "total_matching": value}
                    if isinstance(value, int)
                    else value
                )
            elif name == "find_freelancers":
                assert args["action"] == "search"
                payload = self.people.get(args["params"]["query"], fpage(person("~01other")))
            elif name == "get_profile":
                if isinstance(self.profile, Exception):
                    raise self.profile
                payload = {"data": {"identity": {"ciphertext": self.profile or MINE}}}
            else:
                raise AssertionError(name)
            if isinstance(payload, Exception) and not isinstance(payload, UpworkToolError):
                raise payload
            out.append(to_result(name, payload))
        return out


@pytest.fixture
def conn():
    return init_db(":memory:")


@pytest.fixture
def settings(conn):
    s = Settings(conn)
    s.set("upwork.talent_org_uid", ORG)
    s.set("metrics.profile_key", MINE)
    s.set("keywords.seeds", SEEDS)
    s.set("rank.keywords", ["old one", "old two"])
    return s


def good_bridge():
    return FakeBridge(
        jobs={"alpha": 900, "beta": 300, "gamma": 50, "delta": 700, "epsilon": 20},
        people={
            "alpha": fpage(person("~01a"), person(MINE), more=True, total=4000),
            "beta": fpage(person("~01b"), more=False),  # no total, last page: 1 freelancer
            "gamma": fpage(person("~01g"), more=True, total=120),
            "delta": fpage(person(MINE), total=800),
            "epsilon": fpage(person("~01e"), more=False, total=60),
        },
    )


@pytest.fixture
def user_md(config):
    mem = config.data_dir / "memory"
    mem.mkdir(parents=True, exist_ok=True)
    (mem / "USER.md").write_text("# Profile\nI build n8n flows.\n")


# ---------------------------------------------------------------- settings


def test_defaults_and_seed_cleaning(conn):
    s = Settings(conn)
    assert keywords.seed_list(s) == [
        "n8n automation",
        "AI automation",
        "AI agent developer",
        "Make.com",
        "Zapier",
        "RAG chatbot",
        "Python automation",
        "workflow automation",
    ]
    assert s.get("keywords.baseline_days") == 14 and s.get("keywords.baseline_started") == ""
    assert (s.get("keywords.min_picks"), s.get("keywords.max_picks")) == (3, 5)
    s.set("keywords.seeds", ["  Zapier  ", "zapier", "", 7, "n8n   automation"])
    assert keywords.seed_list(s) == ["Zapier", "n8n automation"]
    s.set("keywords.max_seeds", 1)
    assert keywords.seed_list(s) == ["Zapier"]


# ---------------------------------------------------------------- numbers per seed


def test_numbers_are_gathered_per_seed(conn, settings):
    b = good_bridge()
    rows = {r["keyword"]: r for r in keywords.gather(b, conn, settings, NOW, tz="UTC")}
    assert list(rows) == SEEDS
    assert rows["alpha"]["demand"] == 900 and rows["alpha"]["competition"] == 4000
    assert rows["alpha"]["my_rank"] == 2 and rows["alpha"]["rank_status"] == "found"
    assert rows["delta"]["my_rank"] == 1 and rows["delta"]["competition"] == 800
    # no total field, but the first page was the last one: the rows seen are all there are
    assert rows["beta"]["competition"] == 1 and rows["beta"]["rank_status"] == "not_found"
    assert rows["beta"]["my_rank"] is None and rows["beta"]["rank_pages_seen"] == 1
    assert rows["gamma"]["competition"] == 120
    assert all(not r["errors"] for r in rows.values())


def test_calls_are_batched_inside_the_keywords_budget(conn, settings):
    b = good_bridge()
    keywords.gather(b, conn, settings, NOW)
    assert b.budgets == ["keywords.max_mcp_calls"]
    first = b.batches[0]
    assert [c[0] for c in first] == ["find_jobs"] * 5  # all demand searches in one batch
    assert first[0][1] == {
        "action": "search",
        "org_uid": ORG,
        "params": {"title": "alpha", "limit": settings.get("keywords.jobs_limit")},
    }
    freelancer = [c for c in b.calls if c[0] == "find_freelancers"]
    assert {c[1]["params"]["query"] for c in freelancer} == set(SEEDS)
    assert all(c[1]["params"]["offset"] in (0, 1, 2) for c in freelancer)


def test_rank_pages_setting_limits_the_freelancer_pages(conn, settings):
    settings.set("keywords.rank_pages", 1)
    b = FakeBridge(people={k: fpage(person("~01x"), more=True, total=9) for k in SEEDS})
    keywords.gather(b, conn, settings, NOW)
    assert len([c for c in b.calls if c[0] == "find_freelancers"]) == len(SEEDS)


def test_nothing_is_stored_in_rank_daily(conn, settings):
    keywords.gather(good_bridge(), conn, settings, NOW)
    assert conn.execute("SELECT COUNT(*) FROM rank_daily").fetchone()[0] == 0


def test_rank_check_still_stores_by_default(conn, settings):
    # the reused helper keeps its X2 behaviour
    rank.check_ranks(good_bridge(), conn, settings, NOW, keywords=["alpha"])
    assert conn.execute("SELECT COUNT(*) FROM rank_daily").fetchone()[0] == 1


def test_a_failed_seed_is_handled(conn, settings):
    b = good_bridge()
    b.jobs["beta"] = UpworkToolError("tool exploded")
    b.jobs["gamma"] = {"status": "ok", "jobs": []}  # no total_matching
    b.people["delta"] = UpworkToolError("search down")
    rows = {r["keyword"]: r for r in keywords.gather(b, conn, settings, NOW)}
    assert rows["beta"]["demand"] is None and "jobs:" in rows["beta"]["errors"][0]
    assert rows["beta"]["competition"] == 1  # its other number survived
    assert rows["gamma"]["demand"] is None and "no total_matching" in rows["gamma"]["errors"][0]
    assert rows["delta"]["competition"] is None and rows["delta"]["rank_status"] == "unknown"
    assert rows["delta"]["demand"] == 700
    assert rows["alpha"]["demand"] == 900 and not rows["alpha"]["errors"]


def test_a_seed_with_no_number_is_left_out_of_the_pick(conn, settings):
    b = good_bridge()
    b.jobs["epsilon"] = UpworkToolError("x")
    b.people["epsilon"] = UpworkToolError("y")
    rows = keywords.gather(b, conn, settings, NOW)
    assert [r["keyword"] for r in keywords.usable(rows)] == SEEDS[:4]


def test_unknown_profile_key_keeps_the_totals(conn, settings):
    settings.set("metrics.profile_key", "")
    b = good_bridge()
    b.profile = RuntimeError("profile read failed")
    rows = {r["keyword"]: r for r in keywords.gather(b, conn, settings, NOW)}
    assert rows["alpha"]["competition"] == 4000
    assert rows["alpha"]["rank_status"] == "unknown" and rows["alpha"]["my_rank"] is None


def test_profile_key_is_learned_once(conn, settings):
    settings.set("metrics.profile_key", "")
    keywords.gather(good_bridge(), conn, settings, NOW)
    assert settings.get("metrics.profile_key") == MINE


def test_login_problem_is_raised(conn, settings):
    b = good_bridge()
    b.jobs["alpha"] = UpworkAuthError("login expired")
    with pytest.raises(UpworkAuthError):
        keywords.gather(b, conn, settings, NOW)


def test_no_account_or_no_seeds(conn, settings):
    settings.set("upwork.talent_org_uid", "")
    with pytest.raises(keywords.KeywordsError, match="No freelancer account"):
        keywords.gather(good_bridge(), conn, settings, NOW)
    settings.set("upwork.talent_org_uid", ORG)
    settings.set("keywords.seeds", [])
    with pytest.raises(keywords.KeywordsError, match="seed list is empty"):
        keywords.gather(good_bridge(), conn, settings, NOW)


@pytest.mark.parametrize(
    "data,expected",
    [
        ({"total_matching": 18}, 18),
        ({"data": {"total_matching": 5}}, 5),
        ({"total_matching": "18"}, None),
        ({"total_matching": True}, None),
        ({"total_matching": -1}, None),
        ({"jobs": []}, None),
        ("text", None),
    ],
)
def test_jobs_total_parsing(data, expected):
    assert keywords.jobs_total(data) == expected


# ---------------------------------------------------------------- validating the pick


def out(*items):
    return {"keywords": [{"keyword": k, "reason": r} for k, r in items]}


def test_validate_drops_unknown_duplicates_and_cleans(settings):
    got = keywords.validate_picks(
        out(
            ("nonsense", "x"),
            ("ALPHA", f"big demand {DASH} low rank"),
            ("alpha", "again"),
            ("beta", "fine"),
            ("gamma", "ok"),
        ),
        SEEDS,
        settings,
    )
    assert [p["keyword"] for p in got] == ["alpha", "beta", "gamma"]  # spelled as the input
    assert DASH not in got[0]["reason"] and "big demand, low rank" in got[0]["reason"]


def test_validate_too_few_is_an_error(settings):
    with pytest.raises(keywords.KeywordsError, match="only 2"):
        keywords.validate_picks(
            out(("alpha", "a"), ("nonsense", "b"), ("beta", "c")), SEEDS, settings
        )
    for bad in (None, {}, {"keywords": "x"}, {"keywords": [1, {"reason": "x"}]}):
        with pytest.raises(keywords.KeywordsError):
            keywords.validate_picks(bad, SEEDS, settings)


def test_validate_cuts_to_max_and_cuts_reasons(settings):
    settings.set("keywords.max_picks", 4)
    settings.set("keywords.reason_max_chars", 10)
    got = keywords.validate_picks(
        out(*[(s, "a very long reason text") for s in SEEDS]), SEEDS, settings
    )
    assert len(got) == 4 and all(len(p["reason"]) <= 10 for p in got)
    got = keywords.validate_picks(
        out(("alpha", None), ("beta", 5), ("gamma", "x")), SEEDS, settings
    )
    assert [p["reason"] for p in got] == ["", "", "x"]


# ---------------------------------------------------------------- the Claude call


def measured(conn, settings):
    return keywords.usable(keywords.gather(good_bridge(), conn, settings, NOW))


def test_pick_calls_the_think_model_with_rules_first(
    conn, settings, config, fake_claude_gen, user_md
):
    rows = measured(conn, settings)
    picks = keywords.pick(conn, config, settings, rows, "keywords-run-1")
    assert [p["keyword"] for p in picks] == SEEDS[:4]
    call = fake_claude_gen.log()
    assert call["model"] == settings.get("models.think") and call["effort"] == "medium"
    assert call["prompt"].startswith(prompting.hard_rules())
    assert call["prompt"].index("# Task: pick keywords") > call["prompt"].index("I build n8n flows")
    data = call["input"]
    assert data["keywords"].startswith("<keyword_data>") and data["keywords"].endswith(
        "</keyword_data>"
    )
    listed = json.loads(data["keywords"].split("\n", 1)[1].rsplit("\n", 1)[0])
    assert [k["keyword"] for k in listed] == SEEDS and listed[0]["demand"] == 900
    assert "errors" not in listed[0]
    assert (data["min_picks"], data["max_picks"]) == (3, 5)


def test_pick_cost_is_saved_with_the_run_id(conn, settings, config, fake_claude_gen):
    keywords.pick(conn, config, settings, measured(conn, settings), "keywords-run-1")
    row = conn.execute("SELECT * FROM claude_calls").fetchone()
    assert row["run_id"] == "keywords-run-1" and row["task"] == "pick_keywords"
    assert row["cost_usd"] == pytest.approx(0.05) and row["model"] == settings.get("models.think")


def test_pick_validates_what_claude_returns(conn, settings, config, fake_claude_gen):
    fake_claude_gen.env("FAKE_PICK_UNKNOWN", 1)
    fake_claude_gen.env("FAKE_PICK_DUP", 1)
    fake_claude_gen.env("FAKE_PICK_EM", 1)
    fake_claude_gen.env("FAKE_PICK_CASE", 1)
    got = keywords.pick(conn, config, settings, measured(conn, settings), "r")
    assert [p["keyword"] for p in got] == SEEDS[:4]  # unknown + duplicate dropped, case fixed
    assert all(DASH not in p["reason"] for p in got)


def test_pick_with_too_few_valid_keywords_is_an_error(conn, settings, config, fake_claude_gen):
    fake_claude_gen.env("FAKE_PICK_N", 2)
    with pytest.raises(keywords.KeywordsError, match="only 2"):
        keywords.pick(conn, config, settings, measured(conn, settings), "r")
    fake_claude_gen.env("FAKE_PICK_N", 4)
    fake_claude_gen.env("FAKE_PICK_BAD", 1)
    with pytest.raises(keywords.KeywordsError, match="no keyword list"):
        keywords.pick(conn, config, settings, measured(conn, settings), "r")


def test_pick_needs_enough_measured_seeds(conn, settings, config, fake_claude_gen):
    with pytest.raises(keywords.KeywordsError, match="could be measured"):
        keywords.pick(conn, config, settings, measured(conn, settings)[:2], "r")
    assert not fake_claude_gen.called()


# ---------------------------------------------------------------- the whole run


@pytest.fixture
def smtp(monkeypatch, settings):
    sent = []
    monkeypatch.setattr(
        mail, "_send_smtp", lambda s, subject, text, html: sent.append((subject, text, html))
    )
    settings.set("email.smtp_host", "smtp.test")
    settings.set("email.to_address", "me@test.example")
    return sent


def items(conn, status="pending"):
    return conn.execute(
        "SELECT * FROM approvals WHERE kind = 'keywords' AND status = ?", (status,)
    ).fetchall()


def test_run_creates_one_approval_item_and_one_email(
    conn, settings, config, fake_claude_gen, smtp, user_md
):
    b = good_bridge()
    res = keywords.run_keywords(conn, settings, config, b, now=NOW)
    assert res.status == "ok" and res.error is None and res.email == mail.SENT
    [item] = items(conn)
    assert res.item_id == item["id"] and item["ref_id"] == res.run_id
    before, after = json.loads(item["before_json"]), json.loads(item["after_json"])
    assert before == ["old one", "old two"]
    assert [p["keyword"] for p in after["picks"]] == SEEDS[:4]
    first = after["picks"][0]
    assert (first["demand"], first["competition"], first["my_rank"]) == (900, 4000, 2)
    assert first["reason"] and [n["keyword"] for n in after["numbers"]] == SEEDS
    # the runs row, and the cost under the run id
    run = conn.execute("SELECT * FROM runs WHERE run_id = ?", (res.run_id,)).fetchone()
    assert run["task"] == "keywords" and run["status"] == "ok" and run["ended"]
    call = conn.execute("SELECT * FROM claude_calls").fetchone()
    assert call["run_id"] == res.run_id
    # settings were NOT changed yet
    assert settings.get("rank.keywords") == ["old one", "old two"]
    assert settings.get("keywords.baseline_started") == ""
    # one email with the picks and a link to /approvals
    [(subject, text, html)] = smtp
    assert "Keywords ready" in subject and "alpha" in text and "900 jobs" in text
    assert "https://test.example/approvals" in text and "/approvals" in html


def test_email_is_deduped_and_has_a_switch(conn, settings, config, smtp):
    picks = [
        {
            "keyword": "alpha",
            "reason": "r",
            "demand": 1,
            "competition": 2,
            "my_rank": None,
            "rank_status": "not_found",
            "rank_pages_seen": 50,
        }
    ]
    assert keywords.notify(conn, config, settings, 7, picks, NOW) == mail.SENT
    assert keywords.notify(conn, config, settings, 7, picks, NOW) == mail.DUPLICATE
    assert keywords.notify(conn, config, settings, 8, picks, NOW) == mail.SENT  # a new item
    assert len(smtp) == 2 and "not in the first 50" in smtp[0][1]
    row = conn.execute(
        "SELECT * FROM emails WHERE event = 'keywords_ready' AND ref_id = '7'"
    ).fetchone()
    assert row is not None
    settings.set("email.events.keywords_ready", False)
    assert keywords.notify(conn, config, settings, 9, picks, NOW) == mail.OFF
    assert "keywords_ready" in mail.EVENTS


def test_run_without_a_mail_transport_still_makes_the_item(conn, settings, config, fake_claude_gen):
    res = keywords.run_keywords(conn, settings, config, good_bridge(), now=NOW)
    assert res.status == "ok" and res.email == mail.NO_TRANSPORT and len(items(conn)) == 1


def test_too_few_picks_makes_no_item_and_no_email(conn, settings, config, fake_claude_gen, smtp):
    fake_claude_gen.env("FAKE_PICK_N", 2)
    res = keywords.run_keywords(conn, settings, config, good_bridge(), now=NOW)
    assert res.status == "error" and "at least 3" in res.error
    assert items(conn) == [] and smtp == []
    run = conn.execute("SELECT * FROM runs WHERE run_id = ?", (res.run_id,)).fetchone()
    assert run["status"] == "error" and "at least 3" in run["error"]


def test_a_new_run_replaces_the_pending_item(conn, settings, config, fake_claude_gen):
    first = keywords.run_keywords(conn, settings, config, good_bridge(), now=NOW)
    second = keywords.run_keywords(
        conn, settings, config, good_bridge(), now=NOW + timedelta(hours=1)
    )
    assert [r["id"] for r in items(conn)] == [second.item_id]
    assert [r["id"] for r in items(conn, "expired")] == [first.item_id]


def test_login_problem_ends_the_run(conn, settings, config):
    b = good_bridge()
    b.jobs["alpha"] = UpworkAuthError("login expired")
    res = keywords.run_keywords(conn, settings, config, b, now=NOW)
    assert res.status == "auth_expired" and items(conn) == []


def test_kill_switch_skips(conn, settings, config):
    settings.set("general.kill_switch", True)
    b = good_bridge()
    res = keywords.run_keywords(conn, settings, config, b, now=NOW)
    assert res.status == "skipped" and b.calls == []


def test_claude_cap_is_an_error_not_a_crash(conn, settings, config, fake_claude_gen):
    settings.set("claude.monthly_cap_usd", 0.0)
    conn.execute(
        "INSERT INTO claude_calls(run_id, model, input_tokens, output_tokens, cost_usd, "
        "created_at) VALUES ('x','m',1,1,1.0,?)",
        (datetime.now(UTC).isoformat(),),
    )
    res = keywords.run_keywords(conn, settings, config, good_bridge())
    assert res.status == "error" and items(conn) == []


# ---------------------------------------------------------------- approve / reject / baseline


@pytest.fixture
def web(config, fake_claude_gen):
    app, client = make_web_client(config)
    setup_admin(client)
    s = app.state.settings
    s.set("upwork.talent_org_uid", ORG)
    s.set("metrics.profile_key", MINE)
    s.set("keywords.seeds", SEEDS)
    s.set("rank.keywords", ["old one", "old two"])
    return app, client


def make_item(app, config):
    res = keywords.run_keywords(app.state.db, app.state.settings, config, good_bridge())
    assert res.status == "ok"
    return res.item_id


def post(client, path, **data):
    token = csrf_from(client.get("/approvals").text)
    return client.post(path, data={"csrf_token": token, **data})


def test_approve_saves_the_keywords_and_starts_the_baseline(web, config):
    app, client = web
    item_id = make_item(app, config)
    r = post(client, f"/approvals/{item_id}/approve")
    assert r.status_code == 303
    s = app.state.settings
    assert s.get("rank.keywords") == SEEDS[:4]
    assert s.get("keywords.baseline_started") == datetime.now(UTC).date().isoformat()
    assert keywords.baseline_active(s)
    status = app.state.db.execute(
        "SELECT status FROM approvals WHERE id = ?", (item_id,)
    ).fetchone()
    assert status["status"] == "approved"
    # the next rank check uses the approved keywords
    assert rank.approved_keywords(app.state.db, s) == SEEDS[:4]


def test_reject_changes_nothing(web, config):
    app, client = web
    item_id = make_item(app, config)
    r = post(client, f"/approvals/{item_id}/reject")
    assert r.status_code == 303
    s = app.state.settings
    assert s.get("rank.keywords") == ["old one", "old two"]
    assert s.get("keywords.baseline_started") == "" and not keywords.baseline_active(s)
    status = app.state.db.execute(
        "SELECT status FROM approvals WHERE id = ?", (item_id,)
    ).fetchone()
    assert status["status"] == "rejected"


def test_a_double_approve_decides_once(web, config):
    app, client = web
    item_id = make_item(app, config)
    post(client, f"/approvals/{item_id}/approve")
    app.state.settings.set("keywords.baseline_started", "2020-01-01")
    again = post(client, f"/approvals/{item_id}/approve")
    assert again.status_code == 409
    assert app.state.settings.get("keywords.baseline_started") == "2020-01-01"


def test_a_broken_item_stays_pending(web):
    app, client = web
    item_id = approvals.add(app.state.db, "keywords", "Broken", after={"picks": []})
    r = post(client, f"/approvals/{item_id}/approve")
    assert r.status_code == 409 and "could not be applied" in r.text
    row = app.state.db.execute("SELECT status FROM approvals WHERE id = ?", (item_id,)).fetchone()
    assert row["status"] == "pending"
    assert app.state.settings.get("rank.keywords") == ["old one", "old two"]


def test_handler_is_registered():
    assert approvals.HANDLERS["keywords"] is keywords.apply_decision


def test_baseline_helpers(conn):
    s = Settings(conn)
    today = date(2026, 11, 20)
    # never started: not "active", but profile changes are still blocked
    assert not keywords.baseline_active(s, today) and keywords.baseline_days_left(s, today) is None
    assert keywords.profile_changes_blocked(s, today)
    s.set("keywords.baseline_started", "2026-11-10")
    assert keywords.baseline_active(s, today) and keywords.baseline_days_left(s, today) == 4
    assert keywords.profile_changes_blocked(s, today)
    assert keywords.baseline_active(s, date(2026, 11, 23))  # day 13
    assert not keywords.baseline_active(s, date(2026, 11, 24))  # day 14: over
    assert not keywords.profile_changes_blocked(s, date(2026, 11, 24))
    s.set("keywords.baseline_days", 30)
    assert keywords.baseline_active(s, date(2026, 11, 24))
    s.set("keywords.baseline_started", "not a date")
    assert not keywords.baseline_active(s, today) and keywords.profile_changes_blocked(s, today)


# ---------------------------------------------------------------- page and button


def test_page_shows_the_keywords_item_as_a_table(web, config):
    app, client = web
    make_item(app, config)
    html = client.get("/approvals").text
    assert 'id="keyword-discovery"' in html and "Find keywords" in html
    assert "kw-table" in html and "Demand (jobs)" in html and "Competition (freelancers)" in html
    assert (
        '<th scope="row">alpha</th>' in html and "900" in html and "4000" in html and "#2" in html
    )
    assert "Good demand" in html
    assert "not in top 1" in html  # beta: checked 1 freelancer, I was not among them
    assert "old one, old two" in html  # what is checked today


def test_page_without_an_item_still_has_the_button(web):
    _, client = web
    html = client.get("/approvals").text
    assert "Find keywords" in html and "/approvals/keywords/run" in html


def test_button_needs_csrf(web, monkeypatch):
    app, client = web
    started = []
    monkeypatch.setattr(trigger, "start", lambda config, name: started.append(name))
    assert client.post("/approvals/keywords/run").status_code == 403
    assert client.post("/approvals/keywords/run", data={"csrf_token": "wrong"}).status_code == 403
    assert started == []


def test_button_starts_the_task(web, monkeypatch):
    app, client = web
    started = []
    monkeypatch.setattr(trigger, "start", lambda config, name: started.append(name))
    r = post(client, "/approvals/keywords/run")
    assert r.status_code == 303 and r.headers["location"] == "/approvals?done=started"
    assert started == ["keywords"]
    assert "Keyword search started" in client.get("/approvals?done=started").text


def test_button_when_busy_or_killed(web, monkeypatch):
    app, client = web

    def busy(config, name):
        raise lock.TaskBusy(name)

    monkeypatch.setattr(trigger, "start", busy)
    r = post(client, "/approvals/keywords/run")
    assert r.headers["location"] == "/approvals?done=busy"
    assert "already running" in client.get("/approvals?done=busy").text
    app.state.settings.set("general.kill_switch", True)
    started = []
    monkeypatch.setattr(trigger, "start", lambda config, name: started.append(name))
    r = post(client, "/approvals/keywords/run")
    assert r.headers["location"] == "/approvals?done=killed" and started == []
    page = client.get("/approvals").text
    assert "kill switch is on" in page and "/approvals/keywords/run" not in page


def test_page_shows_running_state(web, config):
    app, client = web
    fd = lock.acquire(config.data_dir, "keywords")
    try:
        html = client.get("/approvals").text
        assert "running since" in html and "/approvals/keywords/run" not in html
    finally:
        import os

        os.close(fd)


def test_baseline_text_on_the_page(web):
    app, client = web
    s = app.state.settings
    s.set("keywords.baseline_started", (datetime.now(UTC).date() - timedelta(days=3)).isoformat())
    assert "A baseline is running now" in client.get("/approvals").text


def test_dashboard_run_button_for_the_keywords_row_works(web, monkeypatch):
    app, client = web
    app.state.db.execute(
        "INSERT INTO runs(run_id, task, started, status) VALUES ('keywords-1','keywords',?, 'ok')",
        (datetime.now(UTC).isoformat(),),
    )
    assert 'action="/tasks/keywords/run"' in client.get("/").text
    started = []
    monkeypatch.setattr(trigger, "start", lambda config, name: started.append(name))
    token = csrf_from(client.get("/").text)
    r = client.post("/tasks/keywords/run", data={"csrf_token": token})
    assert r.status_code == 303 and started == ["keywords"]
    r = client.post("/tasks/nonsense/run", data={"csrf_token": token})
    assert r.status_code == 404


# ---------------------------------------------------------------- trigger and scheduler


def test_keywords_is_a_manual_task_never_scheduled(config, conn, monkeypatch):
    assert "keywords" not in trigger.TASKS and "keywords" in trigger.MANUAL_TASKS
    assert trigger.is_built("keywords")
    assert not hasattr(scheduler, "keywords")
    s = Settings(conn)
    started = []
    monkeypatch.setattr(trigger, "start", lambda config, name: started.append(name))
    scheduler.tick(s, config, NOW)
    assert "keywords" not in started


def test_trigger_starts_the_keywords_process(config, monkeypatch):
    monkeypatch.setenv("SECRET_KEY", config.secret_key)
    calls = {}

    class FakePopen:
        def __init__(self, cmd, **kw):
            calls["cmd"] = cmd

        def wait(self):
            return 0

    monkeypatch.setattr(trigger.subprocess, "Popen", FakePopen)
    log = trigger.start(config, "keywords")
    assert calls["cmd"] == [sys.executable, "-m", "app.tasks.keywords"]
    assert log.name.startswith("keywords-")


def test_task_main_uses_the_task_lock(config, monkeypatch):
    from app.tasks import keywords as task

    monkeypatch.setenv("SECRET_KEY", config.secret_key)
    monkeypatch.setenv("DATA_DIR", str(config.data_dir))
    fd = lock.acquire(config.data_dir, "keywords")
    try:
        assert task.main() == 0  # busy: nothing started, no crash
    finally:
        import os

        os.close(fd)
