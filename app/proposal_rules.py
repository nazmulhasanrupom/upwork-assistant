"""Proposal rules (round 3, A1): what a cover letter or screening answer must never contain.

Plain text only. No bold, markdown, HTML, emoji, link, domain-shaped word, em dash or en dash.
`check` is pure Python and returns short problem lines; an empty list means the text is clean.
The draft path asks Claude to rewrite once with the problem list, then checks again
(app/llm/judging.py). Used for every proposal draft, manual or automatic.
"""

from __future__ import annotations

import re
from typing import Any

DASHES = "—―⸺⸻–‒"  # em dash, horizontal bar, two-em and three-em dash, en dash, figure dash

_URL_RE = re.compile(r"(?i)\b(?:https?://|ftp://|www\.|mailto:)\S+|\bhttps?\b\s*:\s*//")
# word.word: the right side starts with 2 or more letters, so decimals (2.5), versions (v2.0),
# initials (U.S.) and "e.g." are fine, but example.com and Node.js are not.
_DOMAIN_RE = re.compile(
    r"(?<![\w.])[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?\.[A-Za-z]{2}[A-Za-z0-9-]*"
)
_HTML_RE = re.compile(r"</?[A-Za-z!][^>]*>|&[A-Za-z]{2,8};|&#\d+;")
_BULLET_RE = re.compile(r"^[ \t]*(?:[-+*•▪◦‣●][ \t]+|\d{1,3}[.)][ \t]+)", re.M)
_EMOJI_RE = re.compile(
    "["
    "\U0001f000-\U0001faff"  # pictographs, emoticons, symbols, flags
    "←-⇿"  # arrows
    "⌀-⏿"  # technical symbols (watch, hourglass)
    "①-⓿"  # circled digits and letters
    "■-➿"  # shapes, dingbats, check marks, stars
    "⤀-⥿"
    "⬀-⯿"  # arrows, stars
    "〰〽㊗㊙"
    "️‍⃣"  # variation selector, joiner, keycap
    "]"
)
_MATH_BOLD_RE = re.compile("[\U0001d400-\U0001d7ff]")  # "bold" letters made of math symbols


def _sample(found: list[str], limit: int = 3) -> str:
    seen: list[str] = []
    for item in found:
        if item not in seen:
            seen.append(item)
    return ", ".join(f'"{x[:30]}"' for x in seen[:limit])


def check(text: str | None) -> list[str]:
    """Problems in one text, each a short sentence. Empty list = follows the rules."""
    text = text or ""
    problems: list[str] = []
    if any(ch in text for ch in DASHES):
        problems.append("Has an em dash or en dash. Use a comma, a period or the word to.")
    if _MATH_BOLD_RE.search(text):
        problems.append("Has bold or fancy styled letters. Use plain letters.")
    if "*" in text or "__" in text:
        problems.append("Has bold or italic markup (asterisks or underscores). Use plain words.")
    html = _HTML_RE.findall(text)
    if html or "<" in text or ">" in text:
        shown = _sample(html) or '"< >"'
        problems.append(f"Has HTML or angle brackets: {shown}. Remove them.")
    marks = sorted({c for c in text if c in "#`[]~|" or (c == "_" and "__" not in text)})
    if marks:
        problems.append(f"Has markdown characters: {' '.join(marks)}. Use plain words.")
    items = [m.group(0) + text[m.end() :].split("\n", 1)[0] for m in _BULLET_RE.finditer(text)]
    if len(items) >= 2:  # one line that starts with a number or dash is not a list
        shown = _sample([" ".join(i.split())[:40] for i in items], 2)
        problems.append(
            f"Has a list (lines that start with a dash or number): {shown}. Write sentences."
        )
    if _EMOJI_RE.search(text):
        problems.append("Has an emoji or symbol picture. Use words only.")
    urls = [m.group(0) for m in _URL_RE.finditer(text)]
    if urls:
        problems.append(f"Has a link or URL: {_sample(urls)}. Remove it.")
    domains = [
        m.group(0)
        for m in _DOMAIN_RE.finditer(_URL_RE.sub(" ", text))
        if not re.fullmatch(r"\d+\.\d+", m.group(0))
    ]
    if domains:
        problems.append(
            f"Has a word shaped like a web address: {_sample(domains)}. "
            "Rewrite it, or add a space after the period."
        )
    return problems


def check_draft(cover_letter: str | None, answers: list[Any] | None = None) -> list[str]:
    """Problems in a whole draft, each line starts with where it was found."""
    out = [f"Cover letter: {p}" for p in check(cover_letter)]
    for i, a in enumerate(answers or [], start=1):
        text = a.get("answer") if isinstance(a, dict) else a
        out += [f"Answer {i}: {p}" for p in check(str(text or ""))]
    return out
