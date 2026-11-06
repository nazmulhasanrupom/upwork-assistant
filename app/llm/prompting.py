"""Builds every Claude prompt with a stable prefix (SPEC2 2, 5.7; plan HR1).

Order, always: HARD_RULES.md, USER.md, style, then the task prompt, then `extra`.
The first three change rarely, so prompt caching works. The rules file lives in the repo
(read-only image) and is never read from DATA_DIR, so the agent cannot change it.

Also here: `no_em_dash`, applied to every text Claude writes for the user before it is saved.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from app.config import Config

APP_ROOT = Path(__file__).resolve().parent.parent.parent
RULES_PATH = APP_ROOT / "rules" / "HARD_RULES.md"
DEFAULT_STYLE_PATH = APP_ROOT / "prompts" / "style.md"

NO_USER_FACTS = (
    "No user facts yet. memory/USER.md is missing or empty. "
    "Do not invent any facts about the freelancer."
)
SECTION_SEP = "\n\n---\n\n"


def hard_rules() -> str:
    return RULES_PATH.read_text(encoding="utf-8").strip()


def _data_dir(config: Config | None) -> Path:
    return config.data_dir if config is not None else Path(os.environ.get("DATA_DIR") or "/data")


def user_md_path(config: Config | None = None) -> Path:
    return _data_dir(config) / "memory" / "USER.md"


def user_facts(config: Config | None = None) -> str:
    """Text of memory/USER.md, or "" when it is missing or blank."""
    try:
        return user_md_path(config).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def style_md_path(config: Config | None = None) -> Path:
    return _data_dir(config) / "memory" / "style.md"


def default_style() -> str:
    try:
        return DEFAULT_STYLE_PATH.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def style_text(config: Config | None = None) -> str:
    """The user's style file (<DATA_DIR>/memory/style.md, or the older context/style.md), else
    the generic default."""
    for custom in (style_md_path(config), _data_dir(config) / "context" / "style.md"):
        try:
            text = custom.read_text(encoding="utf-8").strip()
            if text:
                return text
        except OSError:
            pass
    return default_style()


def build_prompt(
    task_prompt_file: str | Path, extra: str = "", *, config: Config | None = None
) -> str:
    """HARD_RULES, USER.md, style, task prompt, then `extra` (if any)."""
    path = Path(task_prompt_file)
    if not path.is_absolute():
        path = APP_ROOT / path
    parts = [
        hard_rules(),
        "# Facts about the user (memory/USER.md)\n\n" + (user_facts(config) or NO_USER_FACTS),
        "# Writing style\n\n" + style_text(config),
        path.read_text(encoding="utf-8").strip(),
    ]
    if extra and extra.strip():
        parts.append(extra.strip())
    return SECTION_SEP.join(p for p in parts if p)


# ---------------------------------------------------------------- dashes

_DASHES = "—―⸺⸻"  # em dash, horizontal bar, two-em and three-em dash
_EN = "–‒"  # en dash, figure dash
_EM_RE = re.compile(rf"[ \t]*[{_DASHES}]+[ \t]*")
_EN_SPACED_RE = re.compile(rf"[ \t]+[{_EN}]+[ \t]+")
_EN_TIGHT_RE = re.compile(rf"[{_EN}]")
_CLOSERS = ".,;:!?)]}"


def has_em_dash(text: Any) -> bool:
    """True if the text has an em dash or an en dash (the checker for the hard rule)."""
    return isinstance(text, str) and any(c in text for c in _DASHES + _EN)


def _em_sub(m: re.Match[str]) -> str:
    s, e = m.start(), m.end()
    text = m.string
    before = text[s - 1] if s > 0 else "\n"
    after = text[e] if e < len(text) else "\n"
    if before in "\n(" or after == "\n":
        return ""  # dash at the start or end of a line or bracket: just drop it
    if after in _CLOSERS or before in ",;:":
        return ""  # a closing mark is already there
    return ", "


def no_em_dash(text: str) -> str:
    """Replace em dashes and dash-like en dashes with plain punctuation.

    "a - b" style: " — " becomes ", ". An en dash with spaces around it is a dash too (", ").
    An en dash inside a range or compound ("5–10", "A–B") becomes a hyphen.
    """
    if not isinstance(text, str) or not has_em_dash(text):
        return text
    out = _EM_RE.sub(_em_sub, text)
    out = _EN_SPACED_RE.sub(lambda m: _em_sub(m), out)
    out = _EN_TIGHT_RE.sub("-", out)
    out = re.sub(r",\s*,", ",", out)
    return re.sub(r"[ \t]{2,}", " ", out)


def clean_texts(value: Any) -> Any:
    """no_em_dash on every string inside dicts and lists. Returns a new structure."""
    if isinstance(value, str):
        return no_em_dash(value)
    if isinstance(value, list):
        return [clean_texts(v) for v in value]
    if isinstance(value, dict):
        return {k: clean_texts(v) for k, v in value.items()}
    return value
