"""The public address of the panel. No .env needed.

Order: the PUBLIC_BASE_URL variable (if set), then the setting `general.public_base_url`.
When both are empty the web app saves the origin of the first real request (see
`detect_origin` and the middleware in app/main.py). Task processes read the setting from
the database, so emails get the same address as the panel.
"""

from __future__ import annotations

import re
from typing import Any

from app.config import Config

KEY = "general.public_base_url"
_HOST_OK = re.compile(r"^[A-Za-z0-9.\-]+(:\d{1,5})?$|^\[[0-9A-Fa-f:]+\](:\d{1,5})?$")


def _clean(value: Any) -> str:
    return value.strip().rstrip("/") if isinstance(value, str) else ""


def get_base_url(settings: Any, config: Config) -> str:
    """Env first, then the saved setting. Empty string when neither is known."""
    env = _clean(config.public_base_url)
    if env:
        return env
    if settings is None:
        return ""
    try:
        return _clean(settings.get(KEY))
    except Exception:
        return ""


def _first(value: str | None) -> str:
    return (value or "").split(",")[0].strip()


def detect_origin(request: Any) -> str:
    """scheme://host of a request, honouring X-Forwarded-Proto / X-Forwarded-Host.
    Empty when the host looks wrong."""
    headers = request.headers
    scheme = _first(headers.get("x-forwarded-proto")).lower() or request.url.scheme
    host = _first(headers.get("x-forwarded-host")) or _first(headers.get("host"))
    if scheme not in ("http", "https") or not host or not _HOST_OK.match(host):
        return ""
    return f"{scheme}://{host}"


def remember_origin(settings: Any, config: Config, request: Any) -> bool:
    """Save the request origin as the panel address when none is known yet."""
    if get_base_url(settings, config):
        return False
    origin = detect_origin(request)
    if not origin:
        return False
    settings.set(KEY, origin)
    return True
