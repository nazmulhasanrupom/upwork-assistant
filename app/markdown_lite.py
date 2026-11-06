"""Tiny safe markdown to HTML for report bodies. Escapes everything first; no raw HTML passes."""

from __future__ import annotations

import re

from markupsafe import Markup, escape

_INLINE = [
    (re.compile(r"`([^`]+)`"), r"<code>\1</code>"),
    (re.compile(r"\*\*([^*]+)\*\*"), r"<strong>\1</strong>"),
    (re.compile(r"(?<![*\w])\*([^*\n]+)\*(?![*\w])"), r"<em>\1</em>"),
]


_SEP = re.compile(r"^\|?[\s:\-|]+$")


def _inline(text: str) -> str:
    out = str(escape(text))
    for rx, rep in _INLINE:
        out = rx.sub(rep, out)
    return out


def _cells(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def render(text: str) -> Markup:
    lines = (text or "").replace("\r\n", "\n").split("\n")
    html: list[str] = []
    para: list[str] = []
    items: list[str] = []
    list_tag = ""
    i = 0

    def flush() -> None:
        nonlocal list_tag
        if para:
            html.append("<p>" + _inline(" ".join(para)) + "</p>")
            para.clear()
        if items:
            html.append(
                f"<{list_tag}>" + "".join(f"<li>{x}</li>" for x in items) + f"</{list_tag}>"
            )
            items.clear()
            list_tag = ""

    while i < len(lines):
        line = lines[i]
        s = line.strip()
        if s.startswith("```"):
            flush()
            i += 1
            code: list[str] = []
            while i < len(lines) and not lines[i].strip().startswith("```"):
                code.append(lines[i])
                i += 1
            html.append("<pre><code>" + str(escape("\n".join(code))) + "</code></pre>")
        elif not s:
            flush()
        elif m := re.match(r"(#{1,3})\s+(.*)", s):
            flush()
            n = len(m.group(1))
            html.append(f"<h{n}>{_inline(m.group(2))}</h{n}>")
        elif (
            s.startswith("|")
            and i + 1 < len(lines)
            and re.match(r"^\|?[\s:\-|]+$", lines[i + 1].strip())
        ):
            flush()
            head = _cells(s)
            i += 2
            rows = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                rows.append(_cells(lines[i]))
                i += 1
            i -= 1
            html.append(
                "<table><thead><tr>"
                + "".join(f"<th>{_inline(c)}</th>" for c in head)
                + "</tr></thead><tbody>"
                + "".join(
                    "<tr>" + "".join(f"<td>{_inline(c)}</td>" for c in r) + "</tr>" for r in rows
                )
                + "</tbody></table>"
            )
        elif m := re.match(r"([-*]|\d+\.)\s+(.*)", s):
            tag = "ol" if m.group(1)[0].isdigit() else "ul"
            if items and tag != list_tag:
                flush()
            if para:
                flush()
            list_tag = tag
            items.append(_inline(m.group(2)))
        else:
            if items:
                flush()
            para.append(s)
        i += 1
    flush()
    return Markup("\n".join(html))
