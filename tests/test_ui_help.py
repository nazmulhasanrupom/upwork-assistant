"""Every info("key") in a template exists in help.toml, and every help.toml key is used."""

import re
import tomllib
from pathlib import Path

APP = Path(__file__).resolve().parent.parent / "app"
KEYS = set(tomllib.loads((APP / "help.toml").read_text()))
USED_RE = re.compile(r"""(?<![\w.])info\(\s*["']([\w.\-]+)["']""")
PAGE_HEADER_RE = re.compile(r"""page_header\(\s*["'][^"']*["']\s*,\s*["']([\w.\-]+)["']""")
HELP_KEY_RE = re.compile(r"""help_key\s*=\s*["']([\w.\-]+)["']""")


def _used() -> set[str]:
    used: set[str] = set()
    for path in (APP / "templates").glob("*.html"):
        text = path.read_text()
        used |= set(USED_RE.findall(text)) | set(HELP_KEY_RE.findall(text))
        # page_header("Title", "key"), card("Title", help_key...) positional use
        used |= set(PAGE_HEADER_RE.findall(text))
    return used


def test_used_keys_exist():
    assert _used() - KEYS == set()


def test_every_key_is_used():
    assert KEYS - _used() == set()


def test_help_text_is_short():
    for key, text in tomllib.loads((APP / "help.toml").read_text()).items():
        assert 0 < len(text) <= 200, key
