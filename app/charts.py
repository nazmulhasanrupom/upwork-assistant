"""Dashboard charts (plan X3): plain inline SVG built here, no JavaScript chart library.

- Funnel: per week, how many proposals were sent / viewed / led to an interview / hired. Counted
  from the `proposals` table by the week each proposal was sent (grouped bars).
- Keyword rank: one small line chart per keyword for the last N days. Lower is better, so the
  y axis is reversed (place 1 is at the top). A day with no check leaves a gap in the line; a day
  where I was not found gets a small cross on the bottom edge.

Colors come from CSS variables (`--chart-*` in app.css, with a dark set), so the charts follow
the light and dark theme. Every text that is not ours (a keyword) is escaped. Each chart has a
text summary (`aria-label` and `<desc>`) and the numbers also exist in tables on the page.
"""

from __future__ import annotations

import math
import sqlite3
from datetime import UTC, date, datetime, timedelta, tzinfo
from typing import Any

from markupsafe import Markup, escape

from app.metrics import week_start
from app.settings import Settings

FUNNEL_SERIES = (
    ("sent", "Sent", "c1"),
    ("viewed", "Viewed", "c2"),
    ("interviews", "Interviews", "c3"),
    ("hires", "Hires", "c4"),
)

# Chart geometry (viewBox units; the SVG scales to the page width).
FUNNEL_W, FUNNEL_H = 720, 260
RANK_W, RANK_H = 360, 170
PAD_L, PAD_R, PAD_T, PAD_B = 36, 12, 14, 30
RANK_PAD_L = 40


def _n(x: float) -> str:
    return f"{x:.1f}".rstrip("0").rstrip(".")


def _day_label(d: date) -> str:
    return f"{d:%b} {d.day}"


def nice_ceiling(value: float) -> int:
    """A round axis maximum at or above `value` (1, 2, 5, 10, 20, 50 ...)."""
    if value <= 1:
        return 1
    magnitude = 10 ** int(math.floor(math.log10(value)))
    for step in (1, 2, 5, 10):
        if value <= step * magnitude:
            return int(step * magnitude)
    return int(10 * magnitude)


def _ticks(lo: float, hi: float, count: int = 4) -> list[int]:
    """Whole-number axis ticks between lo and hi (at least two)."""
    span = max(1, hi - lo)
    step = max(1, math.ceil(span / count))
    first = int(math.ceil(lo / step) * step)
    ticks = list(range(first, int(hi) + 1, step))
    return ticks if len(ticks) >= 2 else [int(lo), int(hi)]


# ---------------------------------------------------------------- funnel


def funnel_data(
    conn: sqlite3.Connection, settings: Settings, tz: tzinfo, now: datetime
) -> dict[str, Any]:
    """Weekly funnel numbers for the last `ui.funnel_weeks` weeks (oldest first)."""
    weeks = max(1, int(settings.get("ui.funnel_weeks")))
    this_week = week_start(now.astimezone(tz).date())
    starts = [this_week - timedelta(days=7 * i) for i in range(weeks - 1, -1, -1)]
    index = {d: i for i, d in enumerate(starts)}
    rows_out = [{"week": d, "sent": 0, "viewed": 0, "interviews": 0, "hires": 0} for d in starts]
    interview = {str(s) for s in settings.get("metrics.interview_statuses")}
    hired = {str(s) for s in settings.get("metrics.hire_statuses")}
    for r in conn.execute(
        "SELECT created_at, status, viewed_by_client FROM proposals WHERE created_at >= ?",
        (datetime.combine(starts[0], datetime.min.time(), tzinfo=tz).astimezone(UTC).isoformat(),),
    ):
        try:
            when = datetime.fromisoformat(str(r["created_at"]).replace("Z", "+00:00"))
        except ValueError:
            continue
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        i = index.get(week_start(when.astimezone(tz).date()))
        if i is None:
            continue
        row = rows_out[i]
        row["sent"] += 1
        row["viewed"] += 1 if r["viewed_by_client"] else 0
        row["interviews"] += 1 if r["status"] in interview else 0
        row["hires"] += 1 if r["status"] in hired else 0
    total = sum(r["sent"] for r in rows_out)
    return {"weeks": rows_out, "total": total, "series": FUNNEL_SERIES}


def funnel_svg(data: dict[str, Any]) -> Markup | None:
    """Grouped bars, one group per week. None when no proposal falls in the weeks shown."""
    if not data["total"]:
        return None
    weeks = data["weeks"]
    top = nice_ceiling(max(max(w[k] for k, _, _ in FUNNEL_SERIES) for w in weeks))
    plot_w = FUNNEL_W - PAD_L - PAD_R
    plot_h = FUNNEL_H - PAD_T - PAD_B
    group_w = plot_w / len(weeks)
    bar_w = min(26.0, group_w * 0.8 / len(FUNNEL_SERIES))
    cluster = bar_w * len(FUNNEL_SERIES)

    def y(v: float) -> float:
        return PAD_T + plot_h * (1 - v / top)

    parts = [
        f'<svg class="chart" viewBox="0 0 {FUNNEL_W} {FUNNEL_H}" role="img" '
        f'aria-labelledby="funnel-title funnel-desc" xmlns="http://www.w3.org/2000/svg">',
        '<title id="funnel-title">Proposal funnel by week</title>',
    ]
    desc = "; ".join(
        f"week of {_day_label(w['week'])}: "
        + ", ".join(f"{label.lower()} {w[key]}" for key, label, _ in FUNNEL_SERIES)
        for w in weeks
    )
    parts.append(f'<desc id="funnel-desc">{escape(desc)}</desc>')
    for t in _ticks(0, top):
        ty = _n(y(t))
        parts.append(
            f'<line class="grid" x1="{PAD_L}" x2="{FUNNEL_W - PAD_R}" y1="{ty}" y2="{ty}"/>'
            f'<text class="tick" x="{PAD_L - 6}" y="{_n(y(t) + 4)}" text-anchor="end">{t}</text>'
        )
    for gi, w in enumerate(weeks):
        gx = PAD_L + gi * group_w + (group_w - cluster) / 2
        for si, (key, label, cls) in enumerate(FUNNEL_SERIES):
            v = w[key]
            x = gx + si * bar_w
            h = plot_h * v / top
            title = f"<title>{escape(label)}, week of {_day_label(w['week'])}: {v}</title>"
            parts.append(
                f'<rect class="bar {cls}" x="{_n(x + 1)}" y="{_n(y(v))}" '
                f'width="{_n(max(1, bar_w - 2))}" height="{_n(h)}" rx="2">{title}</rect>'
            )
            if v and bar_w >= 14:
                parts.append(
                    f'<text class="val" x="{_n(x + bar_w / 2)}" y="{_n(y(v) - 3)}" '
                    f'text-anchor="middle">{v}</text>'
                )
        cx = PAD_L + gi * group_w + group_w / 2
        parts.append(
            f'<text class="tick" x="{_n(cx)}" y="{FUNNEL_H - 10}" text-anchor="middle">'
            f"{escape(_day_label(w['week']))}</text>"
        )
    parts.append(
        f'<line class="axis" x1="{PAD_L}" x2="{FUNNEL_W - PAD_R}" y1="{_n(y(0))}" y2="{_n(y(0))}"/>'
    )
    parts.append("</svg>")
    return Markup("".join(parts))


# ---------------------------------------------------------------- keyword rank


def rank_data(
    conn: sqlite3.Connection,
    settings: Settings,
    tz: tzinfo,
    now: datetime,
    keywords: list[str],
) -> list[dict[str, Any]]:
    """Per keyword: the checks of the last `ui.rank_chart_days` days. `points` has one entry per
    day (oldest first): {date, position, seen}; position None = checked, not found; a day with no
    check at all is not in the list."""
    days = max(2, int(settings.get("ui.rank_chart_days")))
    today = now.astimezone(tz).date()
    first = today - timedelta(days=days - 1)
    out = []
    for keyword in keywords:
        rows = conn.execute(
            "SELECT date, position, total_seen FROM rank_daily "
            "WHERE keyword = ? AND date >= ? AND date <= ? ORDER BY date",
            (keyword, first.isoformat(), today.isoformat()),
        ).fetchall()
        out.append(
            {
                "keyword": keyword,
                "first": first,
                "last": today,
                "days": days,
                "points": [
                    {
                        "date": date.fromisoformat(r["date"]),
                        "position": r["position"],
                        "seen": r["total_seen"],
                    }
                    for r in rows
                ],
            }
        )
    return out


def rank_svg(entry: dict[str, Any]) -> Markup | None:
    """One line chart for one keyword. None when there is no check in the period."""
    points = entry["points"]
    if not points:
        return None
    found = [p["position"] for p in points if p["position"] is not None]
    seen = max((p["seen"] or 0) for p in points)
    first, days = entry["first"], entry["days"]
    # y domain: from the best place to the worst place, a little padding, never below place 1.
    # A "not found" day sits one place after the last freelancer looked at.
    worst = max(found + ([seen + 1] if len(found) < len(points) else []), default=1)
    best = min(found, default=worst)
    pad = max(1, round((worst - best) * 0.15))
    lo, hi = max(1, best - pad), worst + pad
    plot_w = RANK_W - RANK_PAD_L - PAD_R
    plot_h = RANK_H - PAD_T - PAD_B

    def x(d: date) -> float:
        return RANK_PAD_L + plot_w * ((d - first).days / max(1, days - 1))

    def y(v: float) -> float:  # reversed: a small place number is drawn high
        return PAD_T + plot_h * ((v - lo) / max(1, hi - lo))

    name = str(escape(entry["keyword"]))
    summary = ", ".join(
        f"{_day_label(p['date'])}: " + (f"place {p['position']}" if p["position"] else "not found")
        for p in points
    )
    parts = [
        f'<svg class="chart" viewBox="0 0 {RANK_W} {RANK_H}" role="img" '
        f'aria-label="Rank of {name}, lower is better" xmlns="http://www.w3.org/2000/svg">',
        f"<title>Rank of {name} (lower is better)</title>",
        f"<desc>{escape(summary)}</desc>",
    ]
    for t in _ticks(lo, hi):
        ty = _n(y(t))
        label_y = _n(y(t) + 4)
        parts.append(
            f'<line class="grid" x1="{RANK_PAD_L}" x2="{RANK_W - PAD_R}" y1="{ty}" y2="{ty}"/>'
            f'<text class="tick" x="{RANK_PAD_L - 6}" y="{label_y}" text-anchor="end">#{t}</text>'
        )
    # line: checked days with a place are joined; a "not found" day breaks the line (a day with
    # no check at all is simply skipped over)
    segments: list[list[tuple[float, float]]] = [[]]
    for p in points:
        if p["position"] is None:
            segments.append([])
        else:
            segments[-1].append((x(p["date"]), y(p["position"])))
    for seg in segments:
        if len(seg) > 1:
            d = "M" + " L".join(f"{_n(px)} {_n(py)}" for px, py in seg)
            parts.append(f'<path class="line" d="{d}"/>')
    for p in points:
        if p["position"] is None:
            cx, cy = x(p["date"]), y(hi)
            parts.append(
                f'<path class="miss" d="M{_n(cx - 3)} {_n(cy - 3)} L{_n(cx + 3)} {_n(cy + 3)} '
                f'M{_n(cx - 3)} {_n(cy + 3)} L{_n(cx + 3)} {_n(cy - 3)}">'
                f"<title>{_day_label(p['date'])}: not found in the top {p['seen']}</title></path>"
            )
        else:
            parts.append(
                f'<circle class="dot" cx="{_n(x(p["date"]))}" cy="{_n(y(p["position"]))}" r="3">'
                f"<title>{_day_label(p['date'])}: place {p['position']}</title></circle>"
            )
    parts.append(
        f'<text class="tick" x="{RANK_PAD_L}" y="{RANK_H - 10}" text-anchor="start">'
        f"{_day_label(first)}</text>"
        f'<text class="tick" x="{RANK_W - PAD_R}" y="{RANK_H - 10}" text-anchor="end">'
        f"{_day_label(entry['last'])}</text>"
    )
    parts.append("</svg>")
    return Markup("".join(parts))


# ---------------------------------------------------------------- trends (plan I3)

TREND_W = 720


def bars_svg(rows: list[tuple[str, int]], title: str) -> Markup | None:
    """Horizontal bars, one per (label, value). None when there is nothing to draw."""
    rows = [(str(label), int(v)) for label, v in rows if v > 0]
    if not rows:
        return None
    label_w, row_h, pad_r = 170, 24, 48
    height = PAD_T + row_h * len(rows) + 8
    top = max(v for _, v in rows)
    plot_w = TREND_W - label_w - pad_r
    summary = ", ".join(f"{label}: {v}" for label, v in rows)
    parts = [
        f'<svg class="chart" viewBox="0 0 {TREND_W} {height}" role="img" '
        f'aria-label="{escape(title)}" xmlns="http://www.w3.org/2000/svg">',
        f"<title>{escape(title)}</title><desc>{escape(summary)}</desc>",
    ]
    for i, (label, v) in enumerate(rows):
        y = PAD_T + i * row_h
        w = max(2.0, plot_w * v / top)
        parts.append(
            f'<text class="tick" x="{label_w - 6}" y="{_n(y + 14)}" text-anchor="end">'
            f"{escape(label[:26])}</text>"
            f'<rect class="bar c1" x="{label_w}" y="{_n(y + 2)}" width="{_n(w)}" height="16" '
            f'rx="2"><title>{escape(label)}: {v}</title></rect>'
            f'<text class="val" x="{_n(label_w + w + 5)}" y="{_n(y + 14)}">{v}</text>'
        )
    parts.append("</svg>")
    return Markup("".join(parts))


def line_svg(points: list[tuple[date, float | None]], title: str, unit: str = "") -> Markup | None:
    """A line over weeks. A week without a value leaves a gap. None when no week has a value."""
    have = [(d, v) for d, v in points if v is not None]
    if not have:
        return None
    height = 200
    lo, hi = 0.0, float(max(v for _, v in have))
    hi = float(nice_ceiling(hi)) if hi > 0 else 1.0
    plot_w = TREND_W - RANK_PAD_L - PAD_R
    plot_h = height - PAD_T - PAD_B
    count = max(1, len(points) - 1)

    def x(i: int) -> float:
        return RANK_PAD_L + plot_w * i / count

    def y(v: float) -> float:
        return PAD_T + plot_h * (1 - (v - lo) / (hi - lo))

    summary = ", ".join(f"{_day_label(d)}: {v}{unit}" for d, v in have)
    parts = [
        f'<svg class="chart" viewBox="0 0 {TREND_W} {height}" role="img" '
        f'aria-label="{escape(title)}" xmlns="http://www.w3.org/2000/svg">',
        f"<title>{escape(title)}</title><desc>{escape(summary)}</desc>",
    ]
    for t in _ticks(lo, hi):
        ty = _n(y(t))
        parts.append(
            f'<line class="grid" x1="{RANK_PAD_L}" x2="{TREND_W - PAD_R}" y1="{ty}" y2="{ty}"/>'
            f'<text class="tick" x="{RANK_PAD_L - 6}" y="{_n(y(t) + 4)}" text-anchor="end">'
            f"{unit}{t}</text>"
        )
    segments: list[list[tuple[float, float]]] = [[]]
    for i, (_, v) in enumerate(points):
        if v is None:
            segments.append([])
        else:
            segments[-1].append((x(i), y(v)))
    for seg in segments:
        if len(seg) > 1:
            d = "M" + " L".join(f"{_n(px)} {_n(py)}" for px, py in seg)
            parts.append(f'<path class="line" d="{d}"/>')
    for i, (d, v) in enumerate(points):
        if v is not None:
            parts.append(
                f'<circle class="dot" cx="{_n(x(i))}" cy="{_n(y(v))}" r="3">'
                f"<title>{_day_label(d)}: {unit}{v}</title></circle>"
            )
    parts.append(
        f'<text class="tick" x="{RANK_PAD_L}" y="{height - 10}" text-anchor="start">'
        f"{_day_label(points[0][0])}</text>"
        f'<text class="tick" x="{TREND_W - PAD_R}" y="{height - 10}" text-anchor="end">'
        f"{_day_label(points[-1][0])}</text></svg>"
    )
    return Markup("".join(parts))
