"""Round 3 A1: the judge's eligibility answers and auto_send.eligible (a truth table).

Decision only: nothing here sends anything. No Upwork, no Claude except the fake."""

import json
from datetime import UTC, datetime

import pytest

from app import auto_send
from app.db import init_db
from app.llm import judging
from app.settings import Settings
from app.tasks import daily
from tests.test_daily import make_client

ORG = "1000000000000000001"
NOW = datetime(2026, 11, 7, 12, 0, tzinfo=UTC)


@pytest.fixture
def conn():
    return init_db(":memory:")


@pytest.fixture
def settings(conn):
    s = Settings(conn)
    s.set("upwork.talent_org_uid", ORG)
    s.set("general.dry_run", False)
    return s


def job(**over):
    base = {
        "job_id": "1",
        "client_rating": 4.9,
        "client_spend": 50000,
        "activity_label": "GOOD",
        "connects_cost": 16,
        "status": "drafted",
        "details": json.dumps({"payment_verified": True, "applied": False}),
    }
    base.update(over)
    return base


def judgement(**over):
    base = {"fit_score": 92, "skills_match_all": 1, "win_chance": "high", "client_quality": "good"}
    base.update(over)
    return base


def draft(**over):
    return {"rules_ok": 1, **over}


def check(conn, settings, j=None, g=None, d=None, balance=100):
    return auto_send.eligible(
        conn,
        j if j is not None else job(),
        g if g is not None else judgement(),
        d if d is not None else draft(),
        settings,
        balance,
        NOW,
    )


def test_a_great_job_is_eligible(conn, settings):
    assert check(conn, settings) == (True, [])


def test_defaults_are_the_agreed_numbers(settings):
    g = settings.get
    assert g("auto_send.enabled") is True and g("auto_send.min_fit") == 90
    assert g("auto_send.wait_minutes") == 120 and g("auto_send.check_minutes") == 5
    assert g("auto_send.allowed_labels") == ["GOOD", "POTENTIAL"]
    assert settings.get("general.dry_run") is False  # the fixture turned it off
    assert Settings(init_db(":memory:")).get("general.dry_run") is True  # on by default


CASES = [
    ("dry-run", lambda c, s: s.set("general.dry_run", True), {}, "dry-run"),
    ("kill switch", lambda c, s: s.set("general.kill_switch", True), {}, "kill switch"),
    ("disabled", lambda c, s: s.set("auto_send.enabled", False), {}, "auto-send is off"),
    ("low fit", None, {"g": judgement(fit_score=89)}, "fit 89"),
    ("no fit", None, {"g": judgement(fit_score=None)}, "fit unknown"),
    ("skills no", None, {"g": judgement(skills_match_all=0)}, "skills"),
    ("skills missing", None, {"g": judgement(skills_match_all=None)}, "skills"),
    ("win medium", None, {"g": judgement(win_chance="medium")}, "win chance"),
    ("win missing", None, {"g": judgement(win_chance=None)}, "win chance"),
    ("client ok", None, {"g": judgement(client_quality="ok")}, "client quality"),
    ("client missing", None, {"g": judgement(client_quality=None)}, "client quality"),
    ("no judgement", None, {"g": {}}, "fit unknown"),
    ("unpaid", None, {"j": job(details=json.dumps({"payment_verified": False}))}, "payment"),
    ("payment unknown", None, {"j": job(details="{}")}, "payment"),
    ("bad details", None, {"j": job(details="not json")}, "payment"),
    ("low rating", None, {"j": job(client_rating=4.4)}, "rating"),
    ("no rating", None, {"j": job(client_rating=None)}, "rating"),
    ("low spend", None, {"j": job(client_spend=9999)}, "spend"),
    ("no spend", None, {"j": job(client_spend=None)}, "spend"),
    ("label neutral", None, {"j": job(activity_label="NEUTRAL")}, "activity label"),
    ("label suspicious", None, {"j": job(activity_label="SUSPICIOUS")}, "activity label"),
    ("label missing", None, {"j": job(activity_label=None)}, "activity label"),
    (
        "applied",
        None,
        {"j": job(details=json.dumps({"payment_verified": True, "applied": True}))},
        "already applied",
    ),
    ("sent status", None, {"j": job(status="sent")}, "already applied"),
    ("few connects", None, {"balance": 35}, "Connects 35"),  # 16 cost + 20 kept = 36
    ("no balance", None, {"balance": None}, "unknown"),
    ("no cost", None, {"j": job(connects_cost=None)}, "unknown"),
    ("rules broken", None, {"d": draft(rules_ok=0)}, "proposal rules"),
    ("rules unchecked", None, {"d": draft(rules_ok=None)}, "proposal rules"),
    ("no draft", None, {"d": {}}, "proposal rules"),
]


@pytest.mark.parametrize(("name", "prep", "args", "reason"), CASES, ids=[c[0] for c in CASES])
def test_each_condition_alone_blocks(conn, settings, name, prep, args, reason):
    if prep:
        prep(conn, settings)
    ok, why = check(
        conn, settings, j=args.get("j"), g=args.get("g"), d=args.get("d"),
        **({"balance": args["balance"]} if "balance" in args else {}),
    )  # fmt: skip
    assert ok is False
    assert any(reason in w for w in why), why


def test_boundaries_pass(conn, settings):
    ok, why = check(
        conn,
        settings,
        j=job(client_rating=4.5, client_spend=10000, activity_label="POTENTIAL"),
        g=judgement(fit_score=90),
        balance=36,
    )
    assert (ok, why) == (True, [])


def test_thresholds_come_from_settings(conn, settings):
    settings.set("auto_send.min_fit", 95)
    settings.set("auto_send.min_client_rating", 4.95)
    settings.set("auto_send.min_client_spend", 60000)
    settings.set("auto_send.keep_connects", 100)
    settings.set("auto_send.allowed_labels", ["POTENTIAL"])
    ok, why = check(conn, settings)
    assert not ok and len(why) == 5  # fit, rating, spend, label, connects
    settings.set("auto_send.allowed_labels", ["good"])  # case does not matter
    assert not any("activity label" in w for w in check(conn, settings)[1])


def test_all_reasons_are_listed_together(conn, settings):
    settings.set("general.dry_run", True)
    ok, why = check(
        conn, settings, g=judgement(fit_score=10, win_chance="low"), d=draft(rules_ok=0)
    )
    assert not ok and len(why) == 4


def test_daily_cap_counts_todays_auto_sends(conn, settings):
    assert auto_send.started_today(conn, NOW) == 0

    def add(day, status):
        conn.execute(
            "INSERT INTO auto_sends(job_id, created, deadline, status) VALUES (?, ?, ?, ?)",
            (f"j{conn.execute('SELECT COUNT(*) FROM auto_sends').fetchone()[0]}", day, day, status),
        )

    add("2026-11-06T23:59:00", "sent")  # yesterday
    add("2026-11-07T01:00:00", "stopped")  # stopped: does not count
    add("2026-11-07T02:00:00", "cancelled")
    add("2026-11-07T03:00:00", "failed")
    assert auto_send.started_today(conn, NOW) == 0
    add("2026-11-07T04:00:00", "sent")
    add("2026-11-07T05:00:00", "waiting")
    assert auto_send.started_today(conn, NOW) == 2
    assert check(conn, settings)[0] is True  # cap is 3
    add("2026-11-07T06:00:00", "sending")
    ok, why = check(conn, settings)
    assert not ok and any("daily limit of 3" in w for w in why)
    settings.set("auto_send.max_per_day", 4)
    assert check(conn, settings)[0] is True


# ------------------------------------------------------------ the judge output and the parser


@pytest.mark.judging
def test_judge_eligibility_is_stored(conn, settings, config, fake_claude_gen):
    daily.run_daily(conn, settings, config, make_client(settings))
    rows = conn.execute("SELECT * FROM judgements").fetchall()
    assert len(rows) == 5
    for r in rows:
        assert r["skills_match_all"] == 1
        assert r["win_chance"] == "high" and r["client_quality"] == "good"
        reasons = json.loads(r["eligibility_reasons"])
        assert set(reasons) == {"skills_match", "win_chance", "client_quality"}
        assert "—" not in reasons["skills_match"]  # dashes cleaned


@pytest.mark.judging
def test_missing_eligibility_fields_default_to_not_eligible(
    conn, settings, config, fake_claude_gen
):
    fake_claude_gen.env("FAKE_GEN_NOELIG", 1)
    daily.run_daily(conn, settings, config, make_client(settings))
    rows = conn.execute("SELECT * FROM judgements").fetchall()
    assert len(rows) == 5  # the judgement is still saved
    for r in rows:
        assert r["skills_match_all"] is None and r["win_chance"] is None
        assert r["client_quality"] is None and json.loads(r["eligibility_reasons"]) == {}
        ok, why = auto_send.eligible(conn, job(), r, draft(), settings, 100, NOW)
        assert not ok and len(why) == 4  # fit 80, skills, win chance, client quality


PARSER_CASES = [
    (
        dict(skills_match_all=True, win_chance="High", client_quality=" GOOD "),
        (True, "high", "good"),
    ),
    (
        dict(skills_match_all="yes", win_chance="medium", client_quality="ok"),
        (True, "medium", "ok"),
    ),
    (
        dict(skills_match_all="false", win_chance="low", client_quality="poor"),
        (False, "low", "poor"),
    ),
    (dict(skills_match_all=1, win_chance="great", client_quality=5), (None, None, None)),
    (dict(skills_match_all=None), (None, None, None)),
    ({}, (None, None, None)),
]


@pytest.mark.parametrize(("item", "expect"), PARSER_CASES)
def test_parser_is_tolerant(item, expect):
    f = judging.eligibility_fields(item)
    assert (f["skills_match_all"], f["win_chance"], f["client_quality"]) == expect
