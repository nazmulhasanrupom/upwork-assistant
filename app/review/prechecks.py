"""Proposal pre-checks (plan R1, SPEC2 4.B). Pure Python, no Claude, 0 tokens.

`run_prechecks` looks at a draft (cover letter + screening answers + rate) and returns a list of
findings: {"check", "severity", "message", "span"?}. Severity is "error", "warn" or "info".
`span` is {"field", "start", "end"} (character offsets inside that field's text).

These checks are mechanical. Whether the letter follows the job's own instructions (for example
"start with the word Bots") is left to the model, which reads the job post.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from app.llm.prompting import has_em_dash

ERROR, WARN, INFO = "error", "warn", "info"

COVER = "cover_letter"

# A dash used as a dash: em dash, horizontal bar, en dash, figure dash.
_DASH_RE = re.compile("[—―⸺⸻–‒]+")
_SPACE_BEFORE_RE = re.compile(r"(?<=[^\s(])[ \t]+(?=[.,!?](?![.,!?\w]))")
_LOWER_I_RE = re.compile(r"(?<![\w\-/@#$%&+*=<>])i(?![\w\-/@#$%&+*=<>)\]]|\.\w)")
_DOUBLE_SPACE_RE = re.compile(r"(?<=\S)[ \t]{2,}(?=\S)")
_REPEAT_RE = re.compile(r"\b([A-Za-z]{2,})[ \t]+\1\b", re.IGNORECASE)
_ALLOWED_REPEATS = {"had", "that"}  # "had had" and "that that" can be correct
_NA_WORDS = {"", "na", "n/a", "none", "nil"}
_OPENER_RE = re.compile(
    r"^\W*(?:(?:hi|hello|hey|dear)\b[^,.!\n]{0,30}[,.!\s]+)?"
    r"(?:i\b|i[’']m\b|i[’']ve\b|i[’']d\b|i[’']ll\b|my\s+name\b)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Limits:
    length_warn: int = 1800
    length_max: int = 5000
    first_lines_max: int = 260
    first_lines_min: int = 40


def limits_from_settings(settings: Any) -> Limits:
    return Limits(
        length_warn=int(settings.get("review.length_warn_chars")),
        length_max=int(settings.get("review.length_max_chars")),
        first_lines_max=int(settings.get("review.first_lines_max_chars")),
        first_lines_min=int(settings.get("review.first_lines_min_chars")),
    )


def _item(
    check: str, severity: str, message: str, field: str | None = None, m: re.Match | None = None
) -> dict[str, Any]:
    out: dict[str, Any] = {"check": check, "severity": severity, "message": message}
    if m is not None and field:
        out["span"] = {"field": field, "start": m.start(), "end": m.end()}
    return out


def field_label(field: str) -> str:
    if field == COVER:
        return "Cover letter"
    m = re.fullmatch(r"answer_(\d+)", field)
    return f"Answer {m.group(1)}" if m else field


def _snippet(text: str, m: re.Match, pad: int = 18) -> str:
    start, end = max(0, m.start() - pad), min(len(text), m.end() + pad)
    s = " ".join(text[start:end].split())
    return ("..." if start else "") + s + ("..." if end < len(text) else "")


def _scan(
    out: list[dict[str, Any]],
    check: str,
    severity: str,
    pattern: re.Pattern,
    fields: list[tuple[str, str]],
    describe: str,
    advice: str,
    keep: Any = None,
) -> None:
    """One finding per field: count, first example, span of the first hit."""
    for field, text in fields:
        hits = [m for m in pattern.finditer(text) if keep is None or keep(m)]
        if not hits:
            continue
        n = len(hits)
        out.append(
            _item(
                check,
                severity,
                f"{field_label(field)}: {n} {describe}{'' if n == 1 else 's'} "
                f"(for example: {_snippet(text, hits[0])}). {advice}",
                field,
                hits[0],
            )
        )


def is_na(answer: str) -> bool:
    """True for an empty answer or "NA" / "N/A" / "n.a." / "none"."""
    return re.sub(r"[^a-z/]", "", (answer or "").lower()) in _NA_WORDS


def self_focused_opener(first_line: str) -> bool:
    """The letter starts with "I ...", "My name ..." or "Hi, I am ..."."""
    return bool(_OPENER_RE.match(first_line or ""))


def first_two_lines(text: str) -> str:
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    return "\n".join(lines[:2])


def parse_rate(value: Any) -> float | None:
    """ "$35", "35/hr", "35.5" -> number. Empty or unreadable -> None."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    m = re.search(r"\d[\d,]*\.?\d*", str(value or "").replace(" ", ""))
    if not m:
        return None
    try:
        return float(m.group(0).replace(",", ""))
    except ValueError:
        return None


def _money(value: float) -> str:
    return f"${value:,.0f}" if float(value).is_integer() else f"${value:,.2f}"


def _rate_check(rate: float | None, budget: dict[str, Any] | None) -> dict[str, Any] | None:
    budget = budget or {}
    lo, hi = budget.get("min"), budget.get("max")
    kind = str(budget.get("kind") or "").lower()
    fixed = str(budget.get("job_type") or "").lower() == "fixed"
    unit = "" if fixed else "/hr"
    word = "bid" if fixed else "rate"
    if kind.startswith("platform default") or (lo is None and hi is None):
        why = "Upwork's default range, not the client's" if kind.startswith("platform") else ""
        text = (
            f"The job's budget range is {why}, so your {word} cannot be compared."
            if why
            else f"The job has no budget range, so your {word} cannot be compared."
        )
        return _item("rate", INFO, text)
    if rate is None:
        return _item("rate", INFO, f"No {word} entered. The job's range is not checked.")
    lo = hi if lo is None else lo
    hi = lo if hi is None else hi
    span = _money(lo) if lo == hi else f"{_money(lo)}-{_money(hi)}"
    mine = f"{_money(rate)}{unit}"
    if rate < lo:
        return _item(
            "rate", WARN, f"Your {word} {mine} is below the client's minimum ({span}{unit})."
        )
    if rate > hi:
        return _item(
            "rate", WARN, f"Your {word} {mine} is above the client's maximum ({span}{unit})."
        )
    return _item("rate", INFO, f"Your {word} {mine} is inside the client's range ({span}{unit}).")


def run_prechecks(
    cover_letter: str,
    answers: list[dict[str, str]] | None = None,
    *,
    rate: Any = None,
    budget: dict[str, Any] | None = None,
    questions: list[str] | None = None,
    limits: Limits | None = None,
) -> list[dict[str, Any]]:
    """All pre-checks for one draft.

    `answers` is a list of {"question", "answer"} in the order of the job's screening questions.
    `budget` is {"min", "max", "job_type", "kind"} of the job, or None when the job is unknown.
    `questions` are the job's screening questions (None or empty: no question check).
    """
    limits = limits or Limits()
    answers = answers or []
    cover = cover_letter or ""
    out: list[dict[str, Any]] = []

    fields: list[tuple[str, str]] = [(COVER, cover)]
    fields += [(f"answer_{i}", a.get("answer") or "") for i, a in enumerate(answers, 1)]

    # 5. typos and spacing, 1. dashes
    _scan(
        out,
        "dash",
        ERROR,
        _DASH_RE,
        fields,
        "dash",
        "Use a comma, a period or a colon instead. No em or en dashes.",
        keep=lambda m: has_em_dash(m.group(0)),
    )
    _scan(
        out,
        "space_before_punctuation",
        WARN,
        _SPACE_BEFORE_RE,
        fields,
        "space before . , ! or ?",
        "Remove the space.",
    )
    _scan(
        out,
        "lowercase_i",
        WARN,
        _LOWER_I_RE,
        fields,
        'lowercase "i" on its own',
        'Write "I".',
    )
    _scan(
        out,
        "double_space",
        WARN,
        _DOUBLE_SPACE_RE,
        fields,
        "double space",
        "Use one space.",
    )
    _scan(
        out,
        "repeated_word",
        WARN,
        _REPEAT_RE,
        fields,
        "repeated word",
        "Remove the extra word.",
        keep=lambda m: m.group(1).lower() not in _ALLOWED_REPEATS,
    )

    # 3. screening answers
    qs = [q for q in (questions or []) if isinstance(q, str)]
    for i, q in enumerate(qs, 1):
        given = answers[i - 1].get("answer", "") if i <= len(answers) else ""
        if not (given or "").strip():
            out.append(
                _item(
                    "missing_answer",
                    ERROR,
                    f"Screening question {i} has no answer: {' '.join(q.split())[:120]}",
                )
            )
    for i, a in enumerate(answers, 1):
        text = a.get("answer") or ""
        if text.strip() and is_na(text):
            out.append(
                _item(
                    "na_answer",
                    ERROR,
                    f'Answer {i} is "{text.strip()[:20]}". Give a real answer from your '
                    "experience.",
                    f"answer_{i}",
                    re.search(r"\S(?:[\s\S]*\S)?", text),
                )
            )

    # 7. length
    n = len(cover)
    if not cover.strip():
        out.append(_item("length", ERROR, "The cover letter is empty."))
    elif n > limits.length_max:
        out.append(
            _item(
                "length",
                ERROR,
                f"The cover letter is {n:,} characters. Upwork allows at most "
                f"{limits.length_max:,}. Cut it.",
            )
        )
    elif n > limits.length_warn:
        out.append(
            _item(
                "length",
                WARN,
                f"The cover letter is {n:,} characters. Aim for {limits.length_warn:,} or fewer: "
                "short and clear.",
            )
        )

    # 2. first two lines (the client sees these first)
    if cover.strip():
        head = first_two_lines(cover)
        first = head.splitlines()[0]
        if self_focused_opener(first):
            out.append(
                _item(
                    "opener",
                    WARN,
                    'The letter starts with "I" / "My name" / "Hi, I am". Start with the '
                    "client's problem instead.",
                    COVER,
                    _OPENER_RE.match(cover.lstrip()),
                )
            )
        if len(head) > limits.first_lines_max:
            out.append(
                _item(
                    "first_lines",
                    WARN,
                    f"The first 2 lines are {len(head)} characters. The client sees only the "
                    f"start. Keep them under {limits.first_lines_max}.",
                )
            )
        elif len(head) < limits.first_lines_min:
            out.append(
                _item(
                    "first_lines",
                    INFO,
                    f"The first 2 lines are only {len(head)} characters. Use them to speak to "
                    "the client's problem.",
                )
            )

    # 6. rate vs budget
    rate_item = _rate_check(parse_rate(rate), budget)
    if rate_item:
        out.append(rate_item)

    return out


def counts(findings: list[dict[str, Any]]) -> dict[str, int]:
    return {s: sum(1 for f in findings if f.get("severity") == s) for s in (ERROR, WARN, INFO)}
