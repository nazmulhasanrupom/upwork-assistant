import pytest

from app.db import init_db
from app.settings import Settings
from app.upwork import jobs as J
from app.upwork.client import UpworkToolError
from tests.upwork_fakes import FakeMCP, load

ORG = "1000000000000000001"


@pytest.fixture
def conn():
    return init_db(":memory:")


@pytest.fixture
def settings(conn):
    s = Settings(conn)
    s.set("upwork.talent_org_uid", ORG)
    return s


def test_parse_money():
    assert J.parse_money("$13,692.95") == 13692.95
    assert J.parse_money("$300.00") == 300.0
    assert J.parse_money(12) == 12.0
    assert J.parse_money(None) is None
    assert J.parse_money("") is None
    assert J.parse_money("no rate stated") is None
    assert J.parse_money(True) is None


def test_parse_budget():
    assert J.parse_budget("$8.00-$20.00") == (8.0, 20.0)
    assert J.parse_budget("$8.00 - $20.00 /hr") == (8.0, 20.0)
    assert J.parse_budget("$300.00") == (300.0, 300.0)
    assert J.parse_budget(None) == (None, None)


def test_cut_down_hourly_no_rate(settings):
    sj = load("smart_search_most_recent.json")["jobs"][0]
    job = J.cut_down(sj, settings=settings)
    assert job["id"] == "2118524057353559796"
    assert job["job_type"] == "hourly"
    assert job["budget_min"] is None and job["budget_max"] is None
    assert job["fixed_budget"] is None
    assert job["client_spend"] == 13692.95
    assert job["client_rating"] == 4.38
    assert job["client_reviews"] == 27
    assert job["client_hires"] == 38
    assert job["client_country"] == "Australia"
    assert job["proposals_tier"] == "10 to 15"
    assert "HighLevel" in job["skills"]
    assert job["payment_verified"] is True
    assert job["client_hours"] is None
    assert job["connects_cost"] is None and job["can_apply"] is None
    assert job["description"].startswith("<untrusted_participant_content>")
    assert job["description"].endswith("</untrusted_participant_content>")


def test_cut_down_missing_client_data(settings):
    sj = load("smart_search_most_recent.json")["jobs"][1]
    job = J.cut_down(sj, settings=settings)
    assert job["client_spend"] is None
    assert job["client_rating"] is None
    assert job["client_country"] == "FRA"


def test_cut_down_fixed(settings):
    sj = load("smart_search_most_recent.json")["jobs"][2]
    job = J.cut_down(sj, settings=settings)
    assert job["job_type"] == "fixed"
    assert job["fixed_budget"] == 300.0
    assert job["budget_max"] is None
    assert job["client_spend"] == 13543.74


def test_cut_down_with_detail(settings):
    sj = load("smart_search_most_recent.json")["jobs"][0]
    detail = J.parse_detail(load("get_job.json"))
    job = J.cut_down(sj, detail, settings=settings)
    assert job["client_spend"] == 13692.95
    assert job["client_hours"] == 1542
    assert job["hire_rate"] == 67
    assert job["connects_cost"] == 17
    assert job["connects_balance"] == 3
    assert job["can_apply"] is False
    assert job["client_rating"] == 4.38
    assert len(job["reviews"]) == 3
    assert job["reviews"][0]["score"] == 1
    assert "<untrusted_participant_content>" in job["reviews"][0]["comment"]
    assert len(job["screening_questions"]) == 1
    assert job["screening_questions"][0].startswith("<untrusted_participant_content>")
    assert job["experience_level"] == "EXPERT"
    assert job["proposals_tier"] == "10 to 15"
    # raw answer also accepted
    assert J.cut_down(sj, load("get_job.json"), settings=settings)["client_hours"] == 1542


def test_cut_down_truncates_and_keeps_tags(settings):
    settings.set("search.description_max_chars", 50)
    settings.set("search.review_comment_chars", 10)
    sj = {"id": "1", "job_type": "hourly", "client": {}}
    text = "<untrusted_participant_content>\n" + "x" * 500 + "\n</untrusted_participant_content>"
    detail = {
        "job_id": "1",
        "description": text,
        "reviews": [{"score": 5, "comment": text}],
    }
    job = J.cut_down(sj, detail, settings=settings)
    assert job["description"].startswith("<untrusted_participant_content>")
    assert job["description"].endswith("</untrusted_participant_content>")
    assert len(job["description"]) < 50 + 80
    assert "x" * 11 not in job["reviews"][0]["comment"]
    assert job["reviews"][0]["comment"].endswith("</untrusted_participant_content>")


def test_cut_down_default_limit_without_settings():
    sj = {"id": "1", "description_snippet": "z" * 9000, "client": {}}
    assert len(J.cut_down(sj)["description"]) <= 3003


def test_parse_detail_empty_and_bad():
    d = J.parse_detail({"status": "ok"})
    assert d["client_spend"] is None and d["reviews"] == []
    with pytest.raises(J.JobsError):
        J.parse_detail({"status": "error"})
    with pytest.raises(J.JobsError):
        J.parse_detail(None)


def test_fetch_detail_call_shape(settings):
    fake = FakeMCP(settings, details={"2118524057353559796": load("get_job.json")})
    d = J.fetch_detail(fake, "2118524057353559796")
    assert d["client_hours"] == 1542
    assert d["client_spend"] == 13692.95
    assert fake.calls == [
        (
            "find_jobs",
            {
                "action": "get",
                "org_uid": ORG,
                "params": {"job_id": "2118524057353559796"},
            },
        )
    ]


def test_fetch_detail_needs_org_uid(conn):
    fake = FakeMCP(Settings(conn))
    with pytest.raises(J.JobsError):
        J.fetch_detail(fake, "1")


def test_fetch_candidates_merge_dedupe_skip_seen(conn, settings):
    recent = load("smart_search_most_recent.json")
    jobs = recent["jobs"]
    best = {"status": "ok", "jobs": [jobs[0], {"id": "999", "title": "Other", "client": {}}]}
    kw = {"status": "ok", "jobs": [jobs[2], {"id": "888", "title": "Kw", "client": {}}]}
    settings.set("search.keywords", ["gohighlevel", "  ", "n8n"])
    fake = FakeMCP(
        settings,
        search={"most_recent": recent, "best_match": best, "gohighlevel": kw},
    )
    J.save_job(conn, J.cut_down(jobs[1], settings=settings), status="dropped")

    found = J.fetch_candidates(fake, settings, conn)
    ids = [j["id"] for j in found]
    assert ids == ["2118524057353559796", "2118457549206214329", "999", "888"]
    assert len(set(ids)) == len(ids)

    calls = [c[1] for c in fake.calls]
    assert len(calls) == 4  # recent, best, 2 keywords (blank skipped)
    for c in calls:
        assert c["org_uid"] == ORG
        assert c["params"]["verified_payment_only"] is True
    assert calls[0]["action"] == "smart_search"
    assert calls[0]["params"]["mode"] == "most_recent"
    assert calls[0]["params"]["days_posted"] == 1
    assert calls[1]["params"]["mode"] == "best_match"
    assert calls[2]["action"] == "search"
    assert calls[2]["params"]["title"] == "gohighlevel"
    assert calls[3]["params"]["title"] == "n8n"


def test_fetch_candidates_one_search_fails(conn, settings):
    recent = load("smart_search_most_recent.json")
    fake = FakeMCP(
        settings,
        search={"most_recent": UpworkToolError("boom"), "best_match": recent},
    )
    found = J.fetch_candidates(fake, settings, conn)
    assert len(found) == 3


def test_fetch_candidates_needs_org_uid(conn):
    with pytest.raises(J.JobsError):
        J.fetch_candidates(FakeMCP(Settings(conn)), Settings(conn), conn)


def test_save_job_and_skills(conn, settings):
    sj = load("smart_search_most_recent.json")["jobs"]
    job = J.cut_down(sj[2], settings=settings)
    J.save_job(
        conn, job, status="candidate", tier="MID", tier_estimated=True, rate_paid=17.0,
        red_flags=["x"],
    )
    row = conn.execute("SELECT * FROM jobs WHERE job_id=?", (job["id"],)).fetchone()
    assert row["status"] == "candidate" and row["tier"] == "MID"
    assert row["tier_estimated"] == 1
    assert row["budget_min"] == 300 and row["budget_max"] == 300
    assert row["job_type"] == "fixed"
    first = row["first_seen"]
    J.save_job(conn, job, status="dropped")
    row = conn.execute("SELECT * FROM jobs WHERE job_id=?", (job["id"],)).fetchone()
    assert row["status"] == "dropped" and row["first_seen"] == first
    assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1

    assert J.save_skills(conn, job["id"], job["skills"], week="2026-W45") == 5
    assert J.save_skills(conn, job["id"], job["skills"], week="2026-W45") == 0
    rows = conn.execute("SELECT week FROM skills_seen WHERE job_id=?", (job["id"],)).fetchall()
    assert {r["week"] for r in rows} == {"2026-W45"}


def test_iso_week():
    from datetime import date

    assert J.iso_week(date(2026, 11, 6)) == "2026-W45"
    assert J.iso_week(date(2027, 1, 1)) == "2026-W53"
    assert J.iso_week().startswith("20")
