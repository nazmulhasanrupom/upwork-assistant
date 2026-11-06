"""Pages `/trends` (skill chart, median budget) and `/reports` (weekly, monthly), plan I3.
Read-only. Charts are inline SVG from app/charts.py (no JavaScript library)."""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response

from app import charts, reports, trends
from app.auth_routes import render

router = APIRouter()


@router.get("/trends")
def trends_page(request: Request) -> Response:
    s = request.app.state
    today = trends.today_in(s.settings, datetime.now(UTC))
    series = trends.weekly_series(s.db, s.settings, today)
    weeks = series["weeks"]
    skills_chart = charts.bars_svg(
        series["top"], f"Jobs per skill, last {len(weeks)} weeks (all jobs seen, also dropped)"
    )
    fixed_chart = charts.line_svg(
        list(zip(weeks, series["median_fixed"], strict=True)),
        "Median fixed-price budget per week",
        "$",
    )
    hourly_chart = charts.line_svg(
        list(zip(weeks, series["median_hourly"], strict=True)),
        "Median hourly rate per week",
        "$",
    )
    comparison = trends.weekly_comparison(s.db, s.settings, today)
    return render(
        request,
        "trends.html",
        skills_chart=skills_chart,
        fixed_chart=fixed_chart,
        hourly_chart=hourly_chart,
        series=series,
        comparison=comparison,
        weeks_count=len(weeks),
    )


@router.get("/reports")
def reports_page(request: Request, kind: str = "") -> Response:
    s = request.app.state
    rows = reports.list_reports(s.db, kind if kind in reports.KINDS else None)
    return render(request, "reports.html", rows=rows, kind=kind if kind in reports.KINDS else "")


@router.get("/reports/{report_id}")
def report_page(request: Request, report_id: int) -> Response:
    row = reports.get(request.app.state.db, report_id)
    if row is None:
        raise HTTPException(status_code=404)
    return render(request, "report.html", r=row, stats=reports.stats_of(row))
