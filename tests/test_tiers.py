import pytest

from app.db import init_db
from app.filters.tiers import prefilter, tier
from app.settings import Settings
from app.upwork import jobs as J
from tests.upwork_fakes import load


@pytest.fixture
def settings():
    return Settings(init_db(":memory:"))


def job(**kw):
    base = {
        "id": "1",
        "job_type": "hourly",
        "budget_min": None,
        "budget_max": None,
        "fixed_budget": None,
        "client_spend": 20000.0,
        "client_hours": 1000.0,
        "payment_verified": True,
    }
    base.update(kw)
    return base


def test_spec_example_is_mid(settings):
    # SPEC 3.2: 32547 / 1918 = 16.97, budget $8 to $20
    j = job(client_spend=32547.0, client_hours=1918.0, budget_min=8.0, budget_max=20.0)
    t, est, rate = tier(j, settings)
    assert t == "MID" and est is False
    assert rate == pytest.approx(16.97, abs=0.01)
    assert prefilter(j, settings)[0] is True


def test_fixture_get_job_is_low(settings):
    sj = load("smart_search_most_recent.json")["jobs"][0]
    j = J.cut_down(sj, J.parse_detail(load("get_job.json")), settings=settings)
    assert prefilter(j, settings) == (True, "ok")  # hourly, no rate stated: kept
    t, est, rate = tier(j, settings)
    assert t == "LOW" and est is False
    assert rate == pytest.approx(8.88, abs=0.01)


def test_fixture_fra_job_dropped_no_spend(settings):
    sj = load("smart_search_most_recent.json")["jobs"][1]
    j = J.cut_down(sj, settings=settings)
    assert prefilter(j, settings) == (False, "no spend data")


def test_fixture_fixed_300_kept(settings):
    sj = load("smart_search_most_recent.json")["jobs"][2]
    assert prefilter(J.cut_down(sj, settings=settings), settings)[0] is True


@pytest.mark.parametrize(
    "rate,expected",
    [
        (10.99, "LOW"),
        (11, "MID"),
        (15, "MID"),
        (20.5, "MID"),
        (20.99, "MID"),
        (21, "HIGH"),
        (80, "HIGH"),
    ],
)
def test_cut_points_no_gaps(settings, rate, expected):
    t, est, r = tier(job(client_spend=rate * 1000, client_hours=1000.0), settings)
    assert t == expected and est is False
    assert r == pytest.approx(rate)


def test_cut_points_follow_settings(settings):
    settings.set("pay.mid_rate_min", 15)
    settings.set("pay.high_rate_min", 30)
    assert tier(job(client_spend=14000.0, client_hours=1000.0), settings)[0] == "LOW"
    assert tier(job(client_spend=29000.0, client_hours=1000.0), settings)[0] == "MID"
    assert tier(job(client_spend=30000.0, client_hours=1000.0), settings)[0] == "HIGH"


def test_low_spend_is_low(settings):
    t, _, rate = tier(job(client_spend=4999.0, client_hours=100.0), settings)
    assert t == "LOW" and rate == pytest.approx(49.99)


def test_hourly_budget_max_below_min_is_low(settings):
    t, _, _ = tier(job(client_spend=40000.0, client_hours=1000.0, budget_max=10.0), settings)
    assert t == "LOW"
    t, _, _ = tier(job(client_spend=40000.0, client_hours=1000.0, budget_max=11.0), settings)
    assert t == "HIGH"


def test_zero_hours_uses_hourly_budget(settings):
    t, est, rate = tier(job(client_hours=0, budget_max=25.0), settings)
    assert (t, est, rate) == ("HIGH", True, 25.0)
    t, est, rate = tier(job(client_hours=0, budget_max=15.0), settings)
    assert (t, est, rate) == ("MID", True, 15.0)
    t, est, _ = tier(job(client_hours=0, budget_max=9.0), settings)
    assert (t, est) == ("LOW", True)
    t, est, _ = tier(job(client_hours=None, budget_max=15.0), settings)
    assert (t, est) == ("MID", True)


def test_zero_hours_fixed(settings):
    f = job(job_type="fixed", client_hours=0, fixed_budget=300.0)
    assert tier(f, settings)[:2] == ("MID", True)
    f = job(job_type="fixed", client_hours=0, fixed_budget=299.0)
    assert tier(f, settings)[:2] == ("LOW", True)


def test_zero_hours_unknown_budget_is_low_estimated(settings):
    assert tier(job(client_hours=0), settings) == ("LOW", True, None)


@pytest.mark.parametrize(
    "kw,keep,reason",
    [
        ({"payment_verified": False}, False, "payment not verified"),
        ({"client_spend": None}, False, "no spend data"),
        ({"client_spend": 4999.0}, False, "client spend"),
        ({"budget_max": 10.0}, False, "hourly budget max"),
        ({"budget_max": 11.0}, True, "ok"),
        ({}, True, "ok"),
        ({"job_type": "fixed", "fixed_budget": 299.0}, False, "fixed budget"),
        ({"job_type": "fixed", "fixed_budget": 300.0}, True, "ok"),
        ({"job_type": "fixed"}, True, "ok"),
        ({"payment_verified": None}, True, "ok"),
    ],
)
def test_prefilter_rules(settings, kw, keep, reason):
    k, r = prefilter(job(**kw), settings)
    assert k is keep and r.startswith(reason)


def test_prefilter_numbers_from_settings(settings):
    settings.set("pay.good_spending", 100)
    settings.set("pay.fixed_budget_min", 50)
    assert prefilter(job(client_spend=150.0), settings)[0] is True
    assert prefilter(job(job_type="fixed", fixed_budget=60.0, client_spend=150.0), settings)[0]


def _best_match(index):
    return load("smart_search_best_match.json")["jobs"][index]


def test_platform_default_budget_is_unknown(settings):
    # 8-25/hr is Upwork's default range, spend $10,457: must not be dropped for budget.
    j = J.cut_down(_best_match(1), settings=settings)
    assert j["hourly_budget_type"] == "platform default range"
    assert (j["budget_min"], j["budget_max"]) == (8.0, 25.0)  # kept for display
    assert prefilter(j, settings) == (True, "ok")


def test_platform_default_low_max_not_dropped_or_low(settings):
    j = job(budget_max=9.0, hourly_budget_type="platform default range")
    assert prefilter(j, settings) == (True, "ok")
    assert tier(j, settings)[:2] == ("MID", False)  # 20000 / 1000 = 20, budget ignored
    # a client-specified low max is still a drop
    j["hourly_budget_type"] = "client-specified range"
    assert prefilter(j, settings)[0] is False


def test_platform_default_no_hours_is_unknown_budget(settings):
    j = job(client_hours=0, budget_max=25.0, hourly_budget_type="platform default range")
    assert tier(j, settings) == ("LOW", True, None)
