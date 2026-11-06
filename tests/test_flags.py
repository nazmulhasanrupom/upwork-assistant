import pytest

from app.db import init_db
from app.filters.flags import flags
from app.settings import Settings
from app.upwork import jobs as J
from tests.upwork_fakes import load


@pytest.fixture
def settings():
    return Settings(init_db(":memory:"))


def test_spec_example_one_star(settings):
    j = {"job_type": "hourly", "budget_max": 20.0, "reviews": [{"score": 1}, {"score": 5}]}
    assert "1-star review" in flags(j, settings)


def test_fixture_get_job_flags(settings):
    sj = load("smart_search_most_recent.json")["jobs"][0]
    j = J.cut_down(sj, J.parse_detail(load("get_job.json")), settings=settings)
    out = flags(j, settings)
    assert "1-star review" in out
    assert any("17 Connects" in f and "3" in f for f in out)
    assert "cannot apply" in out
    assert "no rate stated" in out
    assert any(f.startswith("client rating 4.38") for f in out)
    assert not any(f.startswith("hire rate") for f in out)  # 67 >= 40


def test_clean_job_has_no_flags(settings):
    j = {
        "job_type": "hourly", "budget_max": 30.0, "client_rating": 4.9, "client_reviews": 20,
        "hire_rate": 80, "proposals_tier": "10 to 15", "connects_cost": 8,
        "connects_balance": 100, "can_apply": True, "reviews": [{"score": 5}],
    }
    assert flags(j, settings) == []


def test_rating_needs_min_reviews(settings):
    assert flags({"client_rating": 3.0, "client_reviews": 2}, settings) == []
    assert flags({"client_rating": 3.0, "client_reviews": 3}, settings) == [
        "client rating 3 (3 reviews)"
    ]
    assert flags({"client_rating": 4.5, "client_reviews": 30}, settings) == []


def test_review_scores(settings):
    assert flags({"reviews": [{"score": 3}]}, settings) == []
    assert "2-star review" in flags({"reviews": [{"score": 2}]}, settings)
    assert "1-star review" in flags({"reviews": [{"score": 2}, {"score": 1}]}, settings)
    assert flags({"reviews": [{"score": None, "comment": "x"}]}, settings) == []


def test_hire_rate_and_proposals(settings):
    assert flags({"hire_rate": 39}, settings) == ["hire rate 39%"]
    assert flags({"hire_rate": 40}, settings) == []
    assert flags({"proposals_tier": "50+"}, settings) == ["50+ proposals"]
    assert flags({"proposals_tier": "20 to 50"}, settings) == []


def test_connects(settings):
    assert flags({"connects_cost": 27, "connects_balance": 3}, settings)
    assert flags({"connects_cost": 3, "connects_balance": 3}, settings) == []
    assert flags({"connects_cost": 27}, settings) == []


def test_no_rate_only_for_hourly(settings):
    assert flags({"job_type": "hourly"}, settings) == ["no rate stated"]
    assert flags({"job_type": "fixed", "fixed_budget": 500.0}, settings) == []
    assert flags({"job_type": "hourly", "budget_max": 20.0}, settings) == []


def test_can_apply_false_only(settings):
    assert flags({"can_apply": False}, settings) == ["cannot apply"]
    assert flags({"can_apply": None}, settings) == []
    assert flags({"can_apply": True}, settings) == []


def test_flag_thresholds_from_settings(settings):
    settings.set("flags.hire_rate_min", 70)
    settings.set("flags.bad_review_score_max", 3)
    assert flags({"hire_rate": 67}, settings) == ["hire rate 67%"]
    assert "3-star review" in flags({"reviews": [{"score": 3}]}, settings)


def test_platform_default_rate_flag(settings):
    sj = load("smart_search_best_match.json")["jobs"][1]
    j = J.cut_down(sj, settings=settings)
    out = flags(j, settings)
    assert "platform default rate" in out
    assert "no rate stated" not in out
    j2 = J.cut_down(load("smart_search_best_match.json")["jobs"][3], settings=settings)
    assert "platform default rate" not in flags(j2, settings)
