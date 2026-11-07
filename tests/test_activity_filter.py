"""Job activity filter (plan F1): every rule, both proposal sources, bad data, pipeline, page."""

from datetime import UTC, datetime, timedelta

import pytest

from app.db import init_db
from app.filters import activity as A
from app.settings import Settings
from app.upwork import jobs as J
from tests.upwork_fakes import load

NOW = datetime(2026, 11, 6, 12, 0, tzinfo=UTC)


@pytest.fixture
def settings():
    return Settings(init_db(":memory:"))


def ago(hours):
    return (NOW - timedelta(hours=hours)).isoformat().replace("+00:00", "Z")


def job(hours=30, tier="5 to 10", count=None, hired=0, offered=0, invited=0, seats=1, **extra):
    j = {
        "job_activity": {
            "invited_to_interview": invited,
            "total_hired": hired,
            "total_offered": offered,
        },
        "published_date": ago(hours) if hours is not None else None,
        "proposals_tier": tier,
        "freelancers_to_hire": seats,
    }
    if count is not None:
        j["proposal_count"] = count
    j.update(extra)
    return j


def test_no_activity_is_unknown(settings):
    assert A.label_job({"proposals_tier": "50+"}, settings, NOW) == A.UNKNOWN
    assert A.label_job({"job_activity": "x"}, settings, NOW) == A.UNKNOWN


def test_r1_applied_or_filled_hides(settings):
    assert A.classify(job(applied=True), settings, NOW) == (A.HIDE, A.RULE_APPLIED)
    assert A.classify(job(hired=1), settings, NOW) == (A.HIDE, A.RULE_FILLED)


def test_applied_hides_even_without_job_activity(settings):
    assert A.classify({"applied": True}, settings, NOW) == (A.HIDE, "applied")
    assert A.classify({"applied": False}, settings, NOW) == (A.UNKNOWN, "")
    assert A.classify({}, settings, NOW) == (A.UNKNOWN, "")
    assert A.label_job(job(hired=1), settings, NOW) == A.HIDE
    assert A.label_job(job(hired=1, offered=1, seats=3), settings, NOW) == A.NEUTRAL
    assert A.label_job(job(hired=2, offered=1, seats=3), settings, NOW) == A.HIDE
    assert A.label_job(job(offered=1, seats=None), settings, NOW) == A.HIDE  # seats default 1


def test_r2_old_and_crowded_hides(settings):
    assert A.classify(job(hours=49, tier="50+"), settings, NOW) == (A.HIDE, A.RULE_CROWDED)
    assert A.label_job(job(hours=49, count=50), settings, NOW) == A.HIDE
    assert A.label_job(job(hours=47, tier="50+"), settings, NOW) == A.NEUTRAL


def test_r3_invited_is_potential(settings):
    assert A.label_job(job(invited=2), settings, NOW) == A.POTENTIAL
    # R2 comes first
    assert A.label_job(job(hours=60, tier="50+", invited=2), settings, NOW) == A.HIDE


def test_r4_old_with_proposals_is_suspicious(settings):
    assert A.label_job(job(hours=49, tier="5 to 10"), settings, NOW) == A.SUSPICIOUS
    assert A.label_job(job(hours=49, tier="Fewer than 5"), settings, NOW) == A.NEUTRAL


def test_r5_fresh_and_few_is_good(settings):
    assert A.label_job(job(hours=2, tier="20 to 50"), settings, NOW) == A.GOOD
    assert A.label_job(job(hours=2, count=50), settings, NOW) == A.NEUTRAL
    assert A.label_job(job(hours=24, tier="5 to 10"), settings, NOW) == A.NEUTRAL


def test_proposals_from_count_or_tier(settings):
    assert A.proposals(job(count=7), settings) == 7
    assert A.proposals(job(count="12"), settings) == 12
    assert A.proposals(job(count="x"), settings) == 5  # bad count: tier
    for tier, low in [
        ("Fewer than 5", 0),
        ("5 to 10", 5),
        ("10 to 15", 10),
        ("15 to 20", 15),
        ("20 to 50", 20),
        ("50+", 50),
        ("  50+ ", 50),
        ("weird", 0),
        (None, 0),
    ]:
        assert A.proposals(job(tier=tier), settings) == low, tier


def test_missing_or_bad_date_skips_age_rules(settings):
    assert A.label_job(job(hours=None, tier="50+"), settings, NOW) == A.NEUTRAL
    assert A.label_job(job(published_date="not a date"), settings, NOW) == A.NEUTRAL
    j = job(hours=None, created_date=ago(60), tier="50+")
    assert A.label_job(j, settings, NOW) == A.HIDE  # created_date used
    j = job(hours=None, created_date="2026-11-06T11:00:00")  # naive = UTC
    assert A.label_job(j, settings, NOW) == A.GOOD


def test_bad_numbers_do_not_crash(settings):
    j = job()
    j["job_activity"] = {"total_hired": "x", "invited_to_interview": None}
    j["freelancers_to_hire"] = "many"
    assert A.label_job(j, settings, NOW) == A.NEUTRAL


def test_numbers_come_from_settings(settings):
    settings.set("activity.hide_age_hours", 10)
    assert A.label_job(job(hours=11, tier="5 to 10"), settings, NOW) == A.SUSPICIOUS
    settings.set("activity.tier_min", ["Odd=99"])
    assert A.proposals(job(tier="Odd"), settings) == 99


def test_order():
    ranks = [A.order_rank(x) for x in A.ORDER]
    assert ranks == sorted(ranks)
    assert A.order_rank(None) == A.order_rank(A.UNKNOWN)
    assert A.order_rank(A.HIDE) > A.order_rank(A.SUSPICIOUS)


def test_fixtures(settings):
    recent = load("smart_search_most_recent.json")["jobs"]
    cut = [J.cut_down(r, settings=settings) for r in recent]
    assert all(c["job_activity"] is not None and c["published_date"] for c in cut)
    labels = [A.label_job(c, settings, datetime(2026, 11, 6, 3, 0, tzinfo=UTC)) for c in cut]
    assert labels[2] == A.POTENTIAL  # invited_to_interview 3
    best = load("smart_search_best_match.json")["jobs"]
    assert all(
        A.label_job(J.cut_down(b, settings=settings), settings, NOW) == A.UNKNOWN for b in best
    )


def test_parse_detail_reads_applied():
    d = J.parse_detail(load("get_job.json"))
    assert d["applied"] is False
    assert d["job_activity"] is None


def test_apply_detail_overlays_applied(settings):
    full = J.apply_detail({"id": "1"}, {"applied": True, "job_activity": {"total_hired": 0}})
    assert full["applied"] is True and full["job_activity"] == {"total_hired": 0}


def test_save_job_stores_label_and_posted(settings):
    conn = settings.conn
    j = J.cut_down(load("smart_search_most_recent.json")["jobs"][0], settings=settings)
    J.save_job(conn, j, activity_label=A.GOOD)
    row = conn.execute("SELECT * FROM jobs WHERE job_id = ?", (j["id"],)).fetchone()
    assert row["activity_label"] == A.GOOD
    assert row["posted_at"].startswith("2026-11-06T02:01:22")
    J.save_job(conn, j)  # a later save without a label keeps the old one
    row = conn.execute("SELECT activity_label FROM jobs WHERE job_id = ?", (j["id"],)).fetchone()
    assert row["activity_label"] == A.GOOD


# ---------- pipeline and pages ----------

from app.tasks import daily  # noqa: E402
from tests.test_daily import GET_JOB_ID, ORG, make_client  # noqa: E402
from tests.test_pages import cards, web  # noqa: E402,F401

POTENTIAL_ID = "2118457549206214329"


def run_with(conn, settings, config, edit):
    settings.set("upwork.talent_org_uid", ORG)
    client = make_client(settings, balance=5)
    for item in client.search["most_recent"]["jobs"]:
        edit(item)
    return client, daily.run_daily(conn, settings, config, client)


def test_hide_is_dropped_with_reason_and_not_fetched(config):
    conn = init_db(":memory:")
    settings = Settings(conn)

    def edit(item):
        if item["id"] == GET_JOB_ID:
            item["job_activity"]["total_hired"] = 1
        item["freelancers_to_hire"] = item.get("freelancers_to_hire") or 1

    client, res = run_with(conn, settings, config, edit)
    assert res.status == "ok"
    row = conn.execute("SELECT * FROM jobs WHERE job_id = ?", (GET_JOB_ID,)).fetchone()
    assert row["status"] == "dropped"
    assert row["drop_reason"] == f"activity: {A.RULE_FILLED}"
    assert row["activity_label"] == A.HIDE
    assert GET_JOB_ID not in [a["params"].get("job_id") for _, a in client.calls]
    judged = conn.execute("SELECT COUNT(*) FROM judgements WHERE job_id = ?", (GET_JOB_ID,))
    assert judged.fetchone()[0] == 0
    keep = conn.execute("SELECT * FROM jobs WHERE job_id = ?", (POTENTIAL_ID,)).fetchone()
    assert keep["activity_label"] == A.POTENTIAL and keep["posted_at"]
    best = conn.execute("SELECT activity_label FROM jobs WHERE job_id = '2118527230669974661'")
    assert best.fetchone()[0] == A.UNKNOWN


def test_applied_in_detail_drops_after_fetch(config):
    conn = init_db(":memory:")
    settings = Settings(conn)
    settings.set("upwork.talent_org_uid", ORG)
    client = make_client(settings, balance=5)
    client.details[POTENTIAL_ID]["applied"] = True
    res = daily.run_daily(conn, settings, config, client)
    assert res.status == "ok"
    row = conn.execute("SELECT * FROM jobs WHERE job_id = ?", (POTENTIAL_ID,)).fetchone()
    assert row["status"] == "dropped" and row["drop_reason"].startswith("activity: ")


def test_jobs_page_pill_filter_and_sort(web):  # noqa: F811
    app, client = web
    conn = app.state.db
    for jid, label, seen in [
        ("a1", "SUSPICIOUS", "2026-11-06T10:00:00"),
        ("a2", "POTENTIAL", "2026-11-05T10:00:00"),
        ("a3", None, "2026-11-06T09:00:00"),
        ("a4", "GOOD", "2026-11-04T10:00:00"),
    ]:
        conn.execute(
            "INSERT INTO jobs(job_id, first_seen, title, status, tier, activity_label, posted_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (
                jid,
                seen,
                f"T {jid}",
                "candidate",
                "MID",
                label,
                "2026-11-01T08:00:00" if jid == "a4" else None,
            ),
        )
    conn.commit()
    html = client.get("/jobs").text
    order = [html.index(f'id="job-{j}"') for j in ("a2", "a4", "a3", "a1")]
    assert order == sorted(order)
    assert "Potential" in html and "Unknown" in html and "Suspicious" in html
    assert cards(client.get("/jobs?activity=good").text) == 1
    assert cards(client.get("/jobs?activity=unknown").text) == 1
    assert cards(client.get("/jobs?activity=bogus").text) == 4
    assert "Activity" in client.get("/jobs/a1").text
