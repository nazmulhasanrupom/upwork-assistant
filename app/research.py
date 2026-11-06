"""Weekly web research (SPEC2 6, plan L5; decision D11 = allowed).

One Claude run that has only WebSearch and WebFetch (no Upwork tool, no bridge) looks for news
about how Upwork profiles and proposals work and proposes updates to the tactic playbook:

- `add_tactic`: a new tactic (status untested, last checked today),
- `mark_outdated`: an existing tactic that Upwork changed or removed,
- `still_valid`: tactics it checked and found true (one item "mark as checked").

Everything on the web is untrusted. Python trusts nothing in the answer: the operation must be
known, a tactic number must exist, a new tactic needs a type from `research.allowed_types` (never
Auto), a text of at most `research.text_max_chars`, no markup, and a source link that is plain
http or https. Each good proposal becomes one approval item (kind `playbook_update`); nothing in
PLAYBOOK.md changes until the freelancer approves it. Approving applies it through app/playbook.py
(one version-history commit). The switch is `research.enabled`; budget and turns come from settings.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

from app import approvals, playbook
from app.config import Config
from app.llm import prompting
from app.llm.runner import BudgetCapReached, ClaudeFailed, ClaudeRateLimited, run_claude
from app.settings import Settings

log = logging.getLogger("research")

TASK = "research"
KIND = "playbook_update"  # the approvals kind
PROMPT = "prompts/research.md"
TOOLS = "WebSearch,WebFetch,Read,Write"  # web tools only; Read/Write are for output.json
_MARKUP = re.compile(r"<[^>]{0,200}>")
_URL_OK = re.compile(r"^https?://[^\s<>\"']+$")


class ResearchError(RuntimeError):
    """A problem with a message that is safe to show to the owner."""


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat(timespec="seconds")


def _text(value: Any, limit: int) -> str:
    """One line of plain text: dashes cleaned, markup refused (returns '' when it has any)."""
    s = prompting.no_em_dash(" ".join(str(value or "").split()))
    if _MARKUP.search(s):
        return ""
    return s[:limit]


def _sources(value: Any, settings: Settings) -> list[str]:
    out: list[str] = []
    for u in value if isinstance(value, list) else []:
        u = str(u).strip()
        if _URL_OK.match(u) and urlparse(u).hostname and u not in out:
            out.append(u[:300])
    return out[: int(settings.get("research.max_sources"))]


def validate(output: Any, config: Config, settings: Settings) -> tuple[list[dict[str, Any]], str]:
    """Good proposals (dicts with `op`, `reason`, `sources` and the op fields) and a note listing
    what was refused. `still_valid` becomes one `mark_checked` proposal."""
    if not isinstance(output, dict) or not any(k in output for k in ("proposals", "still_valid")):
        raise ResearchError("Claude gave no usable answer.")
    known = {t["no"]: t for t in playbook.tactics(config)}
    types = [str(t) for t in settings.get("research.allowed_types")]
    text_cut = int(settings.get("research.text_max_chars"))
    reason_cut = int(settings.get("research.reason_max_chars"))
    good: list[dict[str, Any]] = []
    refused: list[str] = []
    raw = output.get("proposals")
    for i, p in enumerate(raw if isinstance(raw, list) else [], 1):
        if len(good) >= int(settings.get("research.max_proposals")):
            refused.append(f"proposal {i}: more than the limit")
            continue
        if not isinstance(p, dict):
            refused.append(f"proposal {i}: not valid")
            continue
        op = p.get("op")
        reason = _text(p.get("reason"), reason_cut)
        sources = _sources(p.get("sources"), settings)
        if not sources:
            refused.append(f"proposal {i}: no usable source link")
        elif not reason:
            refused.append(f"proposal {i}: no reason")
        elif op == "add_tactic":
            text = _text(p.get("text"), text_cut + 1)
            type_ = str(p.get("type") or "").strip()
            if not text or len(text) > text_cut:
                refused.append(f"proposal {i}: the tactic text is empty, too long or has markup")
            elif type_ not in types:
                refused.append(f"proposal {i}: type {type_!r} is not allowed")
            elif any(text.casefold() == t["tactic"].casefold() for t in known.values()):
                refused.append(f"proposal {i}: the tactic is already in the playbook")
            else:
                good.append(
                    {"op": op, "text": text, "type": type_, "reason": reason, "sources": sources}
                )
        elif op == "mark_outdated":
            no = p.get("tactic_no")
            row = known.get(no) if isinstance(no, int) and not isinstance(no, bool) else None
            if row is None:
                refused.append(f"proposal {i}: tactic {no!r} does not exist")
            elif row["status"] in (playbook.OUTDATED, playbook.TESTING):
                refused.append(f"proposal {i}: tactic #{no} is {row['status']}")
            else:
                good.append(
                    {
                        "op": op,
                        "tactic_no": no,
                        "tactic": row["tactic"],
                        "reason": reason,
                        "sources": sources,
                    }
                )
        else:
            refused.append(f"proposal {i}: unknown operation")
    valid = output.get("still_valid")
    nums = sorted(
        {n for n in (valid if isinstance(valid, list) else []) if isinstance(n, int) and n in known}
    )
    if nums:
        good.append(
            {"op": "mark_checked", "numbers": nums, "reason": "Still true after this week's check."}
        )
    return good, "; ".join(refused)


# ---------------------------------------------------------------- the approval items


def _title(p: dict[str, Any]) -> str:
    if p["op"] == "add_tactic":
        return f"Playbook: add a tactic: {p['text']}"[:150]
    if p["op"] == "mark_outdated":
        return f"Playbook: tactic #{p['tactic_no']} looks outdated"[:150]
    return f"Playbook: {len(p['numbers'])} tactic(s) still valid"


def create_items(
    conn: sqlite3.Connection, proposals: list[dict[str, Any]], run_id: str, now: datetime
) -> list[int]:
    """One approval item per proposal. Older research items still pending are expired first."""
    conn.execute(
        "UPDATE approvals SET status = 'expired', decided_at = ? "
        "WHERE kind = ? AND status = 'pending'",
        (_iso(now), KIND),
    )
    ids = []
    for p in proposals:
        shown = [f"{k}: {v}" for k, v in p.items() if k != "op" and v not in ("", None)]
        ids.append(
            approvals.add(
                conn,
                KIND,
                _title(p),
                ref_id=run_id,
                before=p.get("tactic"),
                after={**p, "summary": "\n".join(shown)},
                now=now,
            )
        )
    return ids


def apply_decision(row: sqlite3.Row, decision: str, conn: sqlite3.Connection) -> None:
    """Approvals handler for kind 'playbook_update'. Approve changes PLAYBOOK.md (one commit);
    reject records only. A bad item or a playbook problem raises, so the item stays pending."""
    if decision != "approved":
        return
    from app.config import load_config

    settings = Settings(conn)
    config = approvals.context().get("config") or load_config()
    try:
        p = json.loads(row["after_json"] or "")
        op = p["op"]
    except (ValueError, KeyError, TypeError) as e:
        raise ValueError("the item has no update") from e
    day = datetime.now(UTC).astimezone(_zone(settings)).date().isoformat()
    try:
        if op == "add_tactic":
            playbook.add_tactic(config, settings, p["text"], p["type"], day)
        elif op == "mark_outdated":
            playbook.mark_outdated(config, settings, int(p["tactic_no"]), p["reason"], day)
        elif op == "mark_checked":
            playbook.set_checked(
                config, settings, [int(n) for n in p["numbers"]], day, "Playbook: tactics checked"
            )
        else:
            raise ValueError(f"unknown update {op!r}")
    except playbook.PlaybookError as e:
        raise ValueError(str(e)) from e


approvals.register(KIND, apply_decision)


def _zone(settings: Settings) -> Any:
    from app import keywords

    return keywords._zone(settings)


# ---------------------------------------------------------------- the run


@dataclass
class ResearchResult:
    run_id: str
    status: str  # ok | skipped | error
    item_ids: list[int] = field(default_factory=list)
    refused: str = ""
    error: str | None = None


def _start_run(conn: sqlite3.Connection, now: datetime) -> str:
    run_id = f"{TASK}-{now.strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:6]}"
    conn.execute(
        "INSERT INTO runs(run_id, task, started, status) VALUES (?,?,?, 'running')",
        (run_id, TASK, _iso(now)),
    )
    return run_id


def _end_run(conn: sqlite3.Connection, run_id: str, status: str, error: str | None) -> None:
    conn.execute(
        "UPDATE runs SET ended = ?, status = ?, error = ? WHERE run_id = ?",
        (_iso(datetime.now(UTC)), status, error, run_id),
    )


def run_research(
    conn: sqlite3.Connection,
    settings: Settings,
    config: Config,
    *,
    now: datetime | None = None,
) -> ResearchResult:
    """The whole task. Never raises: the outcome is in the result and in the runs row."""
    now = now or datetime.now(UTC)
    run_id = _start_run(conn, now)
    result = ResearchResult(run_id=run_id, status="running")
    try:
        if settings.get("general.kill_switch"):
            result.status, result.error = "skipped", "kill switch is on"
        elif not settings.get("research.enabled"):
            result.status, result.error = "skipped", "weekly research is switched off"
        else:
            today = now.astimezone(_zone(settings)).date()
            data = {
                "playbook": playbook.read(config),
                "today": today.isoformat(),
                "limits": {
                    "max_proposals": int(settings.get("research.max_proposals")),
                    "text_max_chars": int(settings.get("research.text_max_chars")),
                    "allowed_types": list(settings.get("research.allowed_types")),
                },
            }
            res = run_claude(
                TASK,
                PROMPT,
                data,
                str(settings.get("models.write")),
                int(settings.get("research.max_turns")),
                float(settings.get("research.max_budget_usd")),
                config=config,
                conn=conn,
                settings=settings,
                effort=settings.get("models.write_effort") or None,
                timeout=int(settings.get("research.timeout_seconds")),
                prompt_text=prompting.build_prompt(PROMPT, config=config),
                record_run_id=run_id,
                tools=TOOLS,
            )
            good, result.refused = validate(res["output"], config, settings)
            result.item_ids = create_items(conn, good, run_id, now)
            settings.set("research.last_run", _iso(now))
            result.status = "ok"
    except (BudgetCapReached, ClaudeRateLimited, ClaudeFailed, ResearchError) as e:
        result.status, result.error = "error", str(e)[:500]
    except Exception as e:
        log.exception("Weekly research failed")
        result.status, result.error = "error", f"{type(e).__name__}: {e}"[:500]
    _end_run(conn, run_id, result.status, result.error)
    log.info("research %s: items=%d", result.status, len(result.item_ids))
    return result
