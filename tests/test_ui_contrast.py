"""WCAG AA contrast for the text/background pairs, light and dark, parsed from app.css."""

import re
from pathlib import Path

import pytest

CSS = (Path(__file__).resolve().parent.parent / "app" / "static" / "app.css").read_text()


def _block(selector_start: str) -> dict[str, str]:
    """Custom properties declared in the first block that starts with this selector."""
    i = CSS.index(selector_start)
    j = CSS.index("{", i)
    depth, k = 1, j + 1
    while depth:
        depth += {"{": 1, "}": -1}.get(CSS[k], 0)
        k += 1
    return dict(re.findall(r"(--[\w-]+):\s*([^;]+);", CSS[j + 1 : k - 1]))


LIGHT = _block(":root {")
DARK = _block(':root[data-theme="dark"]')
AUTO_DARK = _block(':root:not([data-theme="light"])')


def _lum(hex_color: str) -> float:
    h = hex_color.lstrip("#")
    rgb = [int(h[i : i + 2], 16) / 255 for i in (0, 2, 4)]
    lin = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in rgb]
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


def _ratio(a: str, b: str) -> float:
    hi, lo = sorted((_lum(a), _lum(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


# (text token, background token): body text needs 4.5
PAIRS = [
    ("text", "bg"),
    ("text", "surface"),
    ("text", "surface-2"),
    ("muted", "bg"),
    ("muted", "surface"),
    ("muted", "surface-2"),
    ("accent", "bg"),
    ("accent", "surface"),
    ("accent", "accent-soft"),
    ("accent-text", "accent"),
    ("accent-text", "accent-hover"),
    ("ok", "ok-soft"),
    ("ok", "surface"),
    ("warn", "warn-soft"),
    ("warn", "surface"),
    ("bad", "bad-soft"),
    ("bad", "surface"),
    ("info", "info-soft"),
    ("info", "surface"),
    ("tip-text", "tip-bg"),
]


THEMES = [("light", LIGHT), ("dark", DARK), ("auto-dark", AUTO_DARK)]


@pytest.mark.parametrize("theme,tokens", THEMES)
@pytest.mark.parametrize("fg,bg", PAIRS)
def test_pair_passes_aa(theme, tokens, fg, bg):
    merged = {**LIGHT, **tokens}
    assert _ratio(merged[f"--{fg}"], merged[f"--{bg}"]) >= 4.5, f"{theme}: {fg} on {bg}"


def test_dark_blocks_match():
    assert DARK.keys() >= {k for k in AUTO_DARK if k != "--shadow"} - {"color-scheme"}
    for key, value in AUTO_DARK.items():
        assert DARK[key] == value, key
