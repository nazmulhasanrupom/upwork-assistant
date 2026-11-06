"""I3: /trends and /reports pages and the SVG charts. No JavaScript library, text is escaped."""

from datetime import UTC, date, datetime, timedelta

import pytest

from app import charts, reports, trends
from tests.test_auth import make_client as make_web_client
from tests.test_auth import setup_admin
from tests.test_weekly import add_job


@pytest.fixture
def web(config):
    app, client = make_web_client(config)
    setup_admin(client)
    return app, client


def test_pages_need_login(config):
    app, client = make_web_client(config)
    for path in ("/trends", "/reports", "/reports/1"):
        r = client.get(path)
        assert (
            r.status_code in (302, 303, 307)
            and "/login" in r.headers["location"]
            or "/setup" in r.headers["location"]
        )


def test_trends_empty(web):
    _, client = web
    html = client.get("/trends").text
    assert "trends-skills-empty" in html and "No fixed-price budgets yet" in html
    assert "<script" not in html.split("</header>")[1].split("</main>")[0]


def test_trends_shows_skill_chart_and_median_lines(web):
    app, client = web
    today = datetime.now(UTC).date()
    for i, b in enumerate([100, 200, 300, 400]):
        add_job(app.state.db, f"t{i}", today, ["n8n", "<b>Evil</b>"], budget=b)
    for i, b in enumerate([100, 200, 300, 400]):
        add_job(app.state.db, f"u{i}", today - timedelta(days=7), ["n8n"], budget=b)
    html = client.get("/trends").text
    assert 'id="trends-skills"' in html and 'class="chart"' in html and "<svg" in html
    assert 'id="trends-fixed"' in html and 'class="line"' in html
    assert "<b>Evil</b>" not in html and "&lt;b&gt;Evil" in html
    assert 'id="trends-table"' in html and "$250" in html
    assert "chart.js" not in html.lower() and "d3" not in html.split("<main")[0].lower()


def test_nav_has_both_links(web):
    _, client = web
    html = client.get("/trends").text
    assert 'href="/trends"' in html and 'href="/reports"' in html


def test_reports_list_filter_and_detail(web):
    app, client = web
    db = app.state.db
    w = reports.save(
        db, "weekly", "2026-W47", "Weekly report 2026-W47", "Line <i>one</i>", {"a": 1}, None
    )
    m = reports.save(db, "monthly", "2026-10", "Monthly report 2026-10", "Plan", {}, None)
    html = client.get("/reports").text
    assert "Weekly report 2026-W47" in html and "Monthly report 2026-10" in html
    only = client.get("/reports?kind=monthly").text
    assert "Monthly report 2026-10" in only and "Weekly report 2026-W47" not in only
    d = client.get(f"/reports/{w}").text
    assert "Line &lt;i&gt;one&lt;/i&gt;" in d and "<i>one</i>" not in d and "Numbers behind" in d
    assert client.get(f"/reports/{m}").status_code == 200
    assert client.get("/reports/9999").status_code == 404


def test_reports_empty(web):
    _, client = web
    assert "reports-empty" in client.get("/reports").text


def test_save_replaces_same_period_and_rejects_unknown_kind(web):
    db = web[0].state.db
    a = reports.save(db, "weekly", "p", "t1", "b1", {}, None)
    b = reports.save(db, "weekly", "p", "t2", "b2", {}, None)
    assert a == b and reports.get(db, a)["title"] == "t2"
    with pytest.raises(ValueError):
        reports.save(db, "daily", "p", "t", "b", {}, None)


def test_bars_svg_escapes_and_handles_empty():
    assert charts.bars_svg([], "t") is None and charts.bars_svg([("a", 0)], "t") is None
    svg = str(charts.bars_svg([("<script>x</script>", 3), ("n8n", 1)], "Skills"))
    assert "<script>" not in svg and "&lt;script&gt;" in svg and 'role="img"' in svg


def test_line_svg_gaps_and_empty():
    d = [date(2026, 11, 2), date(2026, 11, 9), date(2026, 11, 16), date(2026, 11, 23)]
    assert charts.line_svg([(x, None) for x in d], "t") is None
    svg = str(charts.line_svg(list(zip(d, [100, None, 200, 300], strict=True)), "Median", "$"))
    assert svg.count("<circle") == 3 and svg.count('class="line"') == 1  # the gap breaks the line


def test_weekly_series_covers_the_configured_weeks(config):
    from app.db import init_db
    from app.settings import Settings

    conn = init_db(":memory:")
    s = Settings(conn)
    s.set("trends.weeks", 5)
    series = trends.weekly_series(conn, s, date(2026, 11, 18))
    assert len(series["weeks"]) == 5 and series["weeks"][-1] == date(2026, 11, 16)
    assert series["top"] == []
