"""Every shown setting has a plain label; no label is a raw dotted key."""

import re

from app.settings import load_defaults
from app.settings_labels import _load, key_label, section_title
from app.settings_routes import is_secret, section_of
from tests.test_settings_page import web  # noqa: F401

DOTTED = re.compile(r"\b[a-z_]+\.[a-z_]+\b")


def test_every_shown_key_has_a_plain_label():
    keys = [k for k in load_defaults() if not is_secret(k)]
    assert keys
    for key in keys:
        label = key_label(key)
        assert label and not DOTTED.search(label), key
        assert "_" not in label, key
    for sec in {section_of(k) for k in keys}:
        title = section_title(sec)
        assert title and "_" not in title and "." not in title, sec


def test_label_file_has_no_unknown_keys():
    known = set(load_defaults())
    assert set(_load()["keys"]) <= known
    assert set(_load()["sections"]) <= {section_of(k) for k in known}


def test_fallback_humanizes_last_part():
    assert key_label("nope.good_spending") == "Good spending"
    assert section_title("some_section") == "Some section"


def test_page_labels_are_plain(web):  # noqa: F811
    _, client = web
    html = client.get("/settings").text
    # visible label spans carry the plain label; the raw key is only in title/name
    spans = re.findall(r"<span title=\"[^\"]*\">([^<]*)", html)
    assert len(spans) > 100
    for text in spans:
        assert not DOTTED.search(text), text
    assert "Good client spend" in html
