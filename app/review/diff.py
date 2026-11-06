"""Word diff for the review page (plan R2). Output is HTML-safe.

Every piece of text is escaped with `html.escape` before it is put between our own `<ins>` and
`<del>` tags, and the result is marked safe only after that. Pasted text can never add markup.
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher
from html import escape

from markupsafe import Markup

_TOKEN_RE = re.compile(r"\s+|[^\s]+")


def _tokens(text: str) -> list[str]:
    return _TOKEN_RE.findall(text or "")


def _join(parts: list[tuple[str, str]]) -> Markup:
    out: list[str] = []
    for kind, text in parts:
        safe = escape(text, quote=True)
        out.append(f"<{kind}>{safe}</{kind}>" if kind in ("ins", "del") else safe)
    return Markup("".join(out))


def word_diff(original: str, fixed: str) -> tuple[Markup, Markup]:
    """(original with <del> on removed words, fixed with <ins> on added words).

    Text shared by both is left plain. The fixed side contains exactly the fixed text once the
    tags are ignored, so a Copy button can use its text. Same for the original side.
    """
    a, b = _tokens(original), _tokens(fixed)
    left: list[tuple[str, str]] = []
    right: list[tuple[str, str]] = []
    for op, i1, i2, j1, j2 in SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if op == "equal":
            left.append(("", "".join(a[i1:i2])))
            right.append(("", "".join(b[j1:j2])))
            continue
        if i2 > i1:
            left.append(("del", "".join(a[i1:i2])))
        if j2 > j1:
            right.append(("ins", "".join(b[j1:j2])))
    return _join(left), _join(right)
