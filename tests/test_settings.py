import pytest

from app.db import init_db
from app.settings import Settings, load_defaults


@pytest.fixture
def s():
    return Settings(init_db(":memory:"))


def test_defaults_load():
    d = load_defaults()
    assert d["general.dry_run"] is True
    assert d["general.kill_switch"] is False
    assert d["upwork.max_mcp_calls_per_run"] == 40
    assert d["upwork.mcp_calls_per_second"] == 1
    assert d["pay.fit_min"] == 65
    assert d["pay.good_spending"] == 5000
    assert d["models.think"] == "claude-opus-5-5"
    assert d["models.write"] == "claude-sonnet-5-5"
    assert d["schedules.daily"] == "0 */2 * * *"
    assert d["claude.think_max_budget_usd"] == 1.0
    assert d["claude.write_max_budget_usd"] == 0.5


def test_get_falls_back_to_default(s):
    assert s.get("pay.fit_min") == 65


def test_set_get_roundtrip_types(s):
    s.set("pay.fit_min", 70)
    s.set("general.dry_run", False)
    s.set("search.keywords", ["python", "fastapi"])
    s.set("claude.monthly_cap_usd", 20)
    assert s.get("pay.fit_min") == 70
    assert s.get("general.dry_run") is False
    assert s.get("search.keywords") == ["python", "fastapi"]
    assert s.get("claude.monthly_cap_usd") == 20.0
    assert isinstance(s.get("claude.monthly_cap_usd"), float)


def test_set_overwrites_and_reset(s):
    s.set("pay.fit_min", 70)
    s.set("pay.fit_min", 80)
    assert s.get("pay.fit_min") == 80
    s.reset("pay.fit_min")
    assert s.get("pay.fit_min") == 65


def test_unknown_key_and_bad_type(s):
    with pytest.raises(KeyError):
        s.get("nope.nothing")
    with pytest.raises(KeyError):
        s.set("nope.nothing", 1)
    with pytest.raises(TypeError):
        s.set("pay.fit_min", "high")
    with pytest.raises(TypeError):
        s.set("general.dry_run", 1)


def test_all_merges(s):
    s.set("pay.fit_min", 90)
    a = s.all()
    assert a["pay.fit_min"] == 90
    assert a["pay.good_spending"] == 5000
