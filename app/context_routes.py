"""Profile context page `/context` (plan J2a, SPEC 4.2 step 4, SPEC 10.1).

- GET  /context          edit USER.md and style.md, see the character count against the cap
- POST /context          save both boxes (atomic write)
- POST /context/build    read the Upwork profile through the bridge, ask the writing model for a
                         proposed USER.md, and show it next to the current one
- POST /context/use      the user clicked "Use this": the proposed text becomes USER.md
- POST /context/discard  drop the proposed text

Nothing here ever overwrites USER.md without a click. Profile text comes from Upwork and is
treated as untrusted data: tags are stripped for reading, and the prompt marks it as data.
All POSTs need login + CSRF (the app-wide guard).
"""

from __future__ import annotations

import logging
import os
import re
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Form, Request
from fastapi.responses import Response

from app.auth_routes import render
from app.config import Config
from app.llm import judging, prompting
from app.llm.runner import (
    BudgetCapReached,
    ClaudeError,
    ClaudeFailed,
    ClaudeRateLimited,
    run_claude,
)
from app.upwork import accounts
from app.upwork.client import UpworkAuthError, UpworkMCPError
from app.upwork.routes import bridge_for

log = logging.getLogger("context")

router = APIRouter()

BUILD_PROMPT = "prompts/build_user.md"
BUILD_TASK = "build_user"
PROPOSED_NAME = "USER.proposed.md"
PROFILE_TAG = "untrusted_profile"

# Keys that are ids or noise, not facts about the person.
SKIP_KEYS = {"identity", "ciphertext", "id", "personid", "trace_id", "status", "profilestate"}

_TAG_RE = re.compile(r"</?\s*untrusted[a-z_]*\s*>", re.IGNORECASE)


class BuildError(RuntimeError):
    """A message that is safe to show to the admin."""


# ---------------------------------------------------------------- files


def memory_file(config: Config, name: str) -> Path:
    return config.memory_dir / name


def read_file(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def atomic_write(path: Path, text: str) -> None:
    """Write to a temp file in the same folder, then rename over the target."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def normalize(text: str) -> str:
    """One newline style, no trailing blanks at the end, one final newline if not empty."""
    text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    return text + "\n" if text else ""


def counts(config: Config, settings: Any, user_text: str | None = None) -> dict[str, Any]:
    """Characters of USER.md and MEMORY.md against the shared cap (SPEC2 5.1)."""
    cap = int(settings.get("memory.total_cap_chars"))
    user = len(
        (user_text if user_text is not None else read_file(memory_file(config, "USER.md"))).strip()
    )
    memory = len(read_file(memory_file(config, "MEMORY.md")).strip())
    total = user + memory
    return {
        "user": user,
        "memory": memory,
        "total": total,
        "cap": cap,
        "over": total > cap,
        "percent": min(100, round(100 * total / cap)) if cap else 0,
    }


# ---------------------------------------------------------------- profile to plain text


def strip_untrusted(text: str) -> str:
    return _TAG_RE.sub("", text).strip()


def _flatten(value: Any, label: str, out: list[str]) -> None:
    if isinstance(value, dict):
        for key, sub in value.items():
            if str(key).lower() in SKIP_KEYS:
                continue
            _flatten(sub, f"{label}.{key}" if label else str(key), out)
    elif isinstance(value, list):
        for i, sub in enumerate(value, 1):
            _flatten(sub, f"{label}[{i}]", out)
    elif isinstance(value, bool):
        out.append(f"{label}: {'yes' if value else 'no'}")
    elif isinstance(value, (int, float)):
        out.append(f"{label}: {value}")
    elif isinstance(value, str):
        text = strip_untrusted(value)
        if text:
            out.append(f"{label}: {text}")


def profile_to_text(profile: Any, highlights: Any = None, limit: int = 12000) -> str:
    """The profile (and highlights) as plain text lines. Untrusted tags are removed.

    This is only for reading: the text still goes to Claude inside an untrusted wrapper."""
    lines: list[str] = []
    _flatten(profile, "", lines)
    if highlights:
        lines.append("")
        lines.append("Highlights (portfolio, certificates, past jobs):")
        _flatten(highlights, "highlights", lines)
    text = "\n".join(lines).strip()
    return text[:limit]


def wrap_profile(text: str) -> str:
    """Wrap in our own untrusted tag; the text cannot close or reopen it."""
    safe = re.sub(rf"<(/?)\s*{PROFILE_TAG}", r"&lt;\1" + PROFILE_TAG, text, flags=re.IGNORECASE)
    return f"<{PROFILE_TAG}>\n{safe}\n</{PROFILE_TAG}>"


# ---------------------------------------------------------------- build from Upwork


def fetch_profile(client: Any, settings: Any) -> tuple[Any, Any]:
    """get_profile get (must work) and get_profile list_highlights (may fail)."""
    org_uid = str(settings.get("upwork.talent_org_uid") or "")
    if not org_uid:
        res = accounts.detect_and_save(client, settings)
        if res.status != "saved":
            raise BuildError(res.message + " Open the Upwork page and pick one.")
        org_uid = str(settings.get("upwork.talent_org_uid"))
    calls = [
        ("get_profile", {"action": "get", "org_uid": org_uid, "params": {}}),
        ("get_profile", {"action": "list_highlights", "org_uid": org_uid, "params": {}}),
    ]
    results = client.call_many(calls)
    profile = results[0].unwrap()
    try:
        highlights = results[1].unwrap()
    except UpworkMCPError as e:
        log.info("Highlights not available: %s", str(e)[:200])
        highlights = None
    return profile, highlights


def build_proposal(request: Request) -> tuple[str, float]:
    """Returns (proposed USER.md text, Claude cost in USD). Raises BuildError."""
    s = request.app.state
    config, settings, conn = s.config, s.settings, s.db
    client = None
    try:
        client = bridge_for(request)
        profile, highlights = fetch_profile(client, settings)
    except UpworkAuthError as e:
        raise BuildError(str(e)) from e
    except (UpworkMCPError, ClaudeError, accounts.AccountError) as e:
        raise BuildError(f"Could not read the Upwork profile: {str(e)[:200]}") from e
    finally:
        if client is not None:
            try:
                client.close()
            except Exception:
                log.exception("Could not close the Upwork bridge")

    text = profile_to_text(profile, highlights, int(settings.get("memory.build_profile_max_chars")))
    if not text:
        raise BuildError("The Upwork profile came back empty, so there is nothing to build from.")
    max_chars = int(settings.get("memory.user_build_max_chars"))
    task_prompt = (prompting.APP_ROOT / BUILD_PROMPT).read_text(encoding="utf-8").strip()
    prompt = prompting.SECTION_SEP.join([prompting.hard_rules(), task_prompt])
    try:
        res = run_claude(
            BUILD_TASK,
            BUILD_PROMPT,
            {"max_chars": max_chars, "profile_text": wrap_profile(text)},
            str(settings.get("models.write")),
            int(settings.get("memory.build_max_turns")),
            float(settings.get("memory.build_max_budget_usd")),
            config=config,
            conn=conn,
            settings=settings,
            effort=settings.get("models.write_effort") or None,
            prompt_text=prompt,
        )
    except BudgetCapReached as e:
        raise BuildError(str(e)) from e
    except ClaudeRateLimited as e:
        raise BuildError(f"Claude is rate limited. Try again later. ({str(e)[:120]})") from e
    except ClaudeFailed as e:
        raise BuildError(f"Claude could not write USER.md: {str(e)[:200]}") from e
    out = res["output"]
    proposed = out.get("user_md") if isinstance(out, dict) else None
    if not isinstance(proposed, str) or not proposed.strip():
        raise BuildError("Claude's answer had no USER.md text. Try again.")
    return normalize(prompting.no_em_dash(proposed)), float(res["cost_usd"])


# ---------------------------------------------------------------- page


def _page(request: Request, status: int = 200, **ctx: Any) -> Response:
    s = request.app.state
    config, settings = s.config, s.settings
    user_md = ctx.pop("user_md", None)
    if user_md is None:
        user_md = read_file(memory_file(config, "USER.md"))
    style_md = ctx.pop("style_md", None)
    if style_md is None:
        style_md = read_file(memory_file(config, "style.md")) or prompting.style_text(config)
    proposed = ctx.pop("proposed", None)
    if proposed is None:
        proposed = read_file(memory_file(config, PROPOSED_NAME)) or None
    ctx.setdefault("notice", "")
    ctx.setdefault("error", "")
    ctx.setdefault("cost", None)
    return render(
        request,
        "context.html",
        status,
        user_md=user_md,
        style_md=style_md,
        proposed=proposed,
        proposed_chars=len(proposed.strip()) if proposed else 0,
        current_chars=len(user_md.strip()),
        build_max=int(settings.get("memory.user_build_max_chars")),
        counts=counts(config, settings, user_md),
        input_max=int(settings.get("memory.input_max_chars")),
        has_style_file=memory_file(config, "style.md").exists(),
        **ctx,
    )


def _too_long(request: Request, *texts: str) -> bool:
    limit = int(request.app.state.settings.get("memory.input_max_chars"))
    return any(len(t) > limit for t in texts)


def _clear_alert(request: Request) -> None:
    judging.user_md_alert(request.app.state.db, True, datetime.now(UTC))


@router.get("/context")
def context_page(request: Request) -> Response:
    return _page(request)


@router.post("/context")
def context_save(request: Request, user_md: str = Form(""), style_md: str = Form("")) -> Response:
    config = request.app.state.config
    if _too_long(request, user_md, style_md):
        return _page(
            request, 413, user_md=user_md, style_md=style_md, error="That text is too long to save."
        )
    user_text, style_text = normalize(user_md), normalize(style_md)
    try:
        atomic_write(memory_file(config, "USER.md"), user_text)
        style_path = memory_file(config, "style.md")
        default = prompting.default_style()
        if style_text.strip() and style_text.strip() != default:
            atomic_write(style_path, style_text)
        elif style_path.exists():
            style_path.unlink()  # empty or the default text: use the default again
    except OSError:
        log.exception("Could not save the profile context")
        return _page(
            request, 500, user_md=user_md, style_md=style_md, error="Could not save. See the log."
        )
    if user_text.strip():
        _clear_alert(request)
    return _page(request, notice="Saved.")


@router.post("/context/build")
def context_build(request: Request) -> Response:
    config = request.app.state.config
    try:
        proposed, cost = build_proposal(request)
    except BuildError as e:
        return _page(request, 400, error=str(e))
    except Exception:
        log.exception("Build USER.md failed")
        return _page(request, 500, error="Could not build USER.md. See the log.")
    try:
        atomic_write(memory_file(config, PROPOSED_NAME), proposed)
    except OSError:
        log.exception("Could not keep the proposed USER.md")
    return _page(
        request,
        proposed=proposed,
        cost=cost,
        notice="Here is a proposed USER.md. It is not saved until you click Use this.",
    )


@router.post("/context/use")
def context_use(request: Request, proposed: str = Form("")) -> Response:
    config = request.app.state.config
    if not proposed.strip():
        return _page(request, 400, error="There is no proposed text to use.")
    if _too_long(request, proposed):
        return _page(request, 413, error="That text is too long to save.")
    try:
        atomic_write(memory_file(config, "USER.md"), normalize(proposed))
        memory_file(config, PROPOSED_NAME).unlink(missing_ok=True)
    except OSError:
        log.exception("Could not save USER.md")
        return _page(request, 500, error="Could not save. See the log.")
    _clear_alert(request)
    return _page(request, proposed="", notice="USER.md saved from the proposed version.")


@router.post("/context/discard")
def context_discard(request: Request) -> Response:
    memory_file(request.app.state.config, PROPOSED_NAME).unlink(missing_ok=True)
    return _page(request, proposed="", notice="Proposed version discarded.")
