"""Weekly consolidation, "dreaming" (SPEC2 5.3, plan L4).

The only job that may promote notes to long-term memory (the noise gate, app/noise_gate.py).
Steps:

1. Python reads the last `consolidation.days` days: daily notes, the freelancer's approve and
   reject clicks, draft-vs-sent differences and outcomes. All of it can hold job or client text, so
   it goes to Claude inside `<untrusted_activity>` tags. The current MEMORY.md and the skills are
   trusted and go in plain. If there is nothing new, no Claude call is made.
2. One Opus call (`models.think`, `prompts/consolidate.md`) answers with small edits to MEMORY.md,
   small patches to skills and a summary.
3. Python does not trust the answer. Memory edits must apply exactly once, must not push
   USER.md + MEMORY.md over the cap (an edit that does is skipped), and never repeat more than
   `consolidation.verbatim_max_words` words of job or client text in a row. Skill patches follow the
   same rules and must give a valid skill.
4. MEMORY.md edits are committed (versioning). Skill patches become approval items (kind
   `skill_patch`) unless `consolidation.skill_patches_need_approval` is off. A summary section is
   written to `memory/DREAMS.md` (newest first, last `consolidation.dreams_keep` weeks kept).
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from app import approvals, memory, noise_gate, notes, skills, versioning
from app.config import Config
from app.llm import prompting
from app.llm.runner import BudgetCapReached, ClaudeFailed, ClaudeRateLimited, run_claude
from app.settings import Settings

log = logging.getLogger("consolidation")

TASK = "consolidate"
KIND = "skill_patch"  # the approvals kind
PROMPT = "prompts/consolidate.md"
TAG = "untrusted_activity"
DREAMS = "DREAMS.md"
_WORD = re.compile(r"\w+", re.UNICODE)
_TAG_OPEN = re.compile(rf"<(/?)\s*{TAG}", re.I)
_MARKUP = re.compile(r"<\s*/?\s*(untrusted|script|html|body|b|i)\b[^>]*>", re.I)


class ConsolidationError(RuntimeError):
    """A problem with a message that is safe to show to the owner."""


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat(timespec="seconds")


def _cut(text: Any, limit: int) -> str:
    s = " ".join(str(text or "").split())
    return s if len(s) <= limit else s[: max(0, limit - 3)] + "..."


# ---------------------------------------------------------------- reading the week


def _wrap(value: Any) -> str:
    body = _TAG_OPEN.sub(lambda m: "&lt;" + m.group(1) + TAG, json.dumps(value, ensure_ascii=False))
    return f"<{TAG}>\n{body}\n</{TAG}>"


@dataclass
class Week:
    activity: dict[str, Any]
    corpus: list[str] = field(default_factory=list)  # every untrusted text, for the copy check

    def empty(self) -> bool:
        return not any(self.activity.values())


def gather(conn: sqlite3.Connection, config: Config, settings: Settings, now: datetime) -> Week:
    days = max(1, int(settings.get("consolidation.days")))
    limit = max(1, int(settings.get("consolidation.signals_max_items")))
    item_cut = int(settings.get("consolidation.item_max_chars"))
    since = _iso(now - timedelta(days=days))
    corpus: list[str] = []

    day_names = [(now - timedelta(days=i)).date().isoformat() for i in range(days - 1, -1, -1)]
    note_rows = []
    budget = int(settings.get("consolidation.notes_max_chars"))
    for day in day_names:
        text = notes.read_note(config, day).strip()
        if text and budget > 0:
            text = text[:budget]
            budget -= len(text)
            note_rows.append({"day": day, "text": text})
            corpus.append(text)

    clicks = [
        {"kind": r["kind"], "title": _cut(r["title"], item_cut), "decision": r["status"]}
        for r in conn.execute(
            "SELECT kind, title, status FROM approvals WHERE decided_at >= ? "
            "AND status IN ('approved', 'rejected') ORDER BY decided_at DESC LIMIT ?",
            (since, limit),
        )
    ]
    proposals = conn.execute(
        "SELECT job_title, status, viewed_by_client, draft_diff FROM proposals "
        "WHERE created_at >= ? ORDER BY created_at DESC LIMIT ?",
        (since, limit),
    ).fetchall()
    diffs = [
        {"job": _cut(r["job_title"], item_cut), "diff": _cut(r["draft_diff"], item_cut)}
        for r in proposals
        if r["draft_diff"]
    ]
    outcomes: list[dict[str, Any]] = [
        {
            "job": _cut(r["job_title"], item_cut),
            "status": r["status"],
            "viewed_by_client": bool(r["viewed_by_client"]),
        }
        for r in proposals
    ]
    for r in conn.execute(
        "SELECT tactic, outcome, result FROM experiments WHERE status = 'finished' "
        "AND finished_at >= ? ORDER BY finished_at DESC LIMIT ?",
        (since, limit),
    ):
        outcomes.append(
            {"test": _cut(r["tactic"], item_cut), "outcome": r["outcome"], "result": r["result"]}
        )
    for group in (clicks, diffs, outcomes):
        for item in group:
            corpus.extend(str(v) for v in item.values() if isinstance(v, str))
    activity = {"notes": note_rows, "clicks": clicks, "diffs": diffs, "outcomes": outcomes}
    return Week(activity=activity, corpus=corpus)


# ---------------------------------------------------------------- checking the answer


def _words(text: str) -> list[str]:
    return _WORD.findall(text.casefold())


def _grams(corpus: list[str], n: int) -> set[tuple[str, ...]]:
    out: set[tuple[str, ...]] = set()
    for piece in corpus:
        w = _words(piece)
        out.update(tuple(w[i : i + n]) for i in range(len(w) - n + 1))
    return out


def quotes_untrusted(text: str, grams: set[tuple[str, ...]], max_words: int) -> bool:
    """True when `text` repeats MORE than `max_words` words in a row from the untrusted texts."""
    n = max_words + 1
    w = _words(text)
    return any(tuple(w[i : i + n]) in grams for i in range(len(w) - n + 1))


def _clean(text: Any) -> str:
    """Plain text from Claude: dashes cleaned, markup tags dropped."""
    return _MARKUP.sub("", prompting.no_em_dash(str(text or ""))).strip("\n")


@dataclass
class MemoryPlan:
    text: str
    applied: list[str] = field(default_factory=list)
    rejected: list[str] = field(default_factory=list)


def plan_memory(
    current: str,
    edits: Any,
    other_len: int,
    cap: int,
    grams: set[tuple[str, ...]],
    settings: Settings,
) -> MemoryPlan:
    """Apply memory edits one by one to `current`. A bad edit is skipped and named in `rejected`.
    An edit that leaves USER.md + MEMORY.md over `cap` and not smaller than before is skipped
    (that is the trim: removals and shortening edits always pass)."""
    plan = MemoryPlan(text=current)
    if not isinstance(edits, list):
        return plan
    max_words = int(settings.get("consolidation.verbatim_max_words"))
    reason_cut = int(settings.get("consolidation.reason_max_chars"))
    for i, edit in enumerate(edits[: int(settings.get("consolidation.max_memory_edits"))], 1):
        label = f"edit {i}"
        if not isinstance(edit, dict) or not all(
            isinstance(edit.get(k, ""), str) for k in ("old", "new")
        ):
            plan.rejected.append(f"{label}: not a valid edit")
            continue
        old, new = _clean(edit.get("old")), _clean(edit.get("new"))
        why = _cut(prompting.no_em_dash(str(edit.get("why") or "")), reason_cut)
        if not old and not new:
            plan.rejected.append(f"{label}: empty")
            continue
        if quotes_untrusted(new, grams, max_words):
            plan.rejected.append(f"{label}: repeats job or client text")
            continue
        if old:
            if plan.text.count(old) != 1:
                plan.rejected.append(f"{label}: the old text was not found exactly once")
                continue
            candidate = plan.text.replace(old, new, 1)
        else:
            candidate = plan.text.rstrip("\n") + ("\n" if plan.text.strip() else "") + new
        candidate = re.sub(r"\n{3,}", "\n\n", candidate)
        before_total = other_len + len(plan.text.strip())
        after_total = other_len + len(candidate.strip())
        if after_total > cap and after_total >= before_total:
            plan.rejected.append(f"{label}: over the {cap} character cap")
            continue
        plan.text = candidate
        plan.applied.append(f"{label}: {why or (new[:80] or 'removed a line')}")
    return plan


@dataclass
class SkillPatch:
    skill: str
    old: str
    new: str
    why: str


def plan_skills(
    config: Config, patches: Any, grams: set[tuple[str, ...]], settings: Settings
) -> tuple[list[SkillPatch], list[str]]:
    """Valid skill patches and the reasons the others were refused."""
    good: list[SkillPatch] = []
    bad: list[str] = []
    if not isinstance(patches, list):
        return good, bad
    max_words = int(settings.get("consolidation.verbatim_max_words"))
    reason_cut = int(settings.get("consolidation.reason_max_chars"))
    for i, p in enumerate(patches[: int(settings.get("consolidation.max_skill_patches"))], 1):
        label = f"skill patch {i}"
        if not isinstance(p, dict) or not isinstance(p.get("skill"), str):
            bad.append(f"{label}: not a valid patch")
            continue
        name = p["skill"].strip()
        old, new = _clean(p.get("old")), _clean(p.get("new"))
        if not isinstance(p.get("old", ""), str) or not isinstance(p.get("new", ""), str):
            bad.append(f"{label}: not a valid patch")
            continue
        if quotes_untrusted(new, grams, max_words):
            bad.append(f"{label} ({name}): repeats job or client text, so it cannot become a skill")
            continue
        try:
            skills.patched_text(config, settings, name, old, new)
        except skills.SkillError as e:
            bad.append(f"{label} ({name}): {e}")
            continue
        why = _cut(prompting.no_em_dash(str(p.get("why") or "")), reason_cut)
        good.append(SkillPatch(name, old, new, why))
    return good, bad


# ---------------------------------------------------------------- the approval item


def create_item(conn: sqlite3.Connection, patch: SkillPatch, run_id: str, now: datetime) -> int:
    title = f"Skill patch: {patch.skill}" + (f": {patch.why}" if patch.why else "")
    return approvals.add(
        conn,
        KIND,
        title[:150],
        ref_id=patch.skill,
        before=patch.old or None,
        after=patch.new,
        now=now,
    )


def apply_skill_item(row: sqlite3.Row, decision: str, conn: sqlite3.Connection) -> None:
    """Approvals handler for kind 'skill_patch'. Approve applies the patch (one commit). Reject
    records only. A patch that no longer applies raises, so the item stays pending."""
    if decision != "approved":
        return
    from app.config import load_config

    settings = Settings(conn)
    config = approvals.context().get("config") or load_config()
    old = json.loads(row["before_json"]) if row["before_json"] else ""
    new = json.loads(row["after_json"] or '""')
    with noise_gate.open_gate("approval"):
        skills.apply_patch(
            config,
            settings,
            row["ref_id"],
            old,
            new,
            message=f"Skill patch approved: {row['ref_id']}",
        )


approvals.register(KIND, apply_skill_item)


# ---------------------------------------------------------------- DREAMS.md


def dreams_path(config: Config) -> Any:
    return config.memory_dir / DREAMS


def write_dreams(
    config: Config, settings: Settings, day: str, lines: list[str], note: str | None = None
) -> None:
    """Add this week's section on top of DREAMS.md and keep the newest `dreams_keep` sections."""
    path = dreams_path(config)
    existing = memory.read_file(path)
    sections = re.split(r"(?m)^(?=## )", existing)
    old = [s.rstrip() for s in sections if s.startswith("## ")]
    new = f"## {day}\n\n" + "\n".join(lines).strip()
    keep = [new, *old][: max(1, int(settings.get("consolidation.dreams_keep")))]
    memory.atomic_write(
        path,
        "# Dreams\n\nWhat the weekly consolidation kept and changed.\n\n"
        + "\n\n".join(keep)
        + "\n",
    )
    versioning.commit(config, settings, note or f"Weekly consolidation: summary {day}", [path])


# ---------------------------------------------------------------- the run


@dataclass
class ConsolidationResult:
    run_id: str
    status: str  # ok | skipped | error
    memory_applied: list[str] = field(default_factory=list)
    rejected: list[str] = field(default_factory=list)
    skill_items: list[int] = field(default_factory=list)
    skills_applied: list[str] = field(default_factory=list)
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


def _ask(
    conn: sqlite3.Connection, config: Config, settings: Settings, week: Week, run_id: str
) -> dict[str, Any]:
    usage = memory.usage(config, settings)
    data = {
        "memory": memory.read_file(memory.memory_file(config, "MEMORY.md")),
        "memory_usage": usage,
        "skills": [
            {"name": s["name"], "text": skills.load_skill(config, s["name"])}
            for s in skills.list_skills(config, settings)
        ],
        "activity": _wrap(week.activity),
        "limits": {
            "max_memory_edits": int(settings.get("consolidation.max_memory_edits")),
            "max_skill_patches": int(settings.get("consolidation.max_skill_patches")),
            "verbatim_max_words": int(settings.get("consolidation.verbatim_max_words")),
        },
    }
    res = run_claude(
        TASK,
        PROMPT,
        data,
        str(settings.get("models.think")),
        int(settings.get("consolidation.max_turns")),
        float(settings.get("consolidation.max_budget_usd")),
        config=config,
        conn=conn,
        settings=settings,
        effort=settings.get("models.think_effort") or None,
        timeout=int(settings.get("consolidation.timeout_seconds")),
        prompt_text=prompting.build_prompt(PROMPT, config=config),
        record_run_id=run_id,
    )
    out = res["output"]
    if not isinstance(out, dict) or not any(
        k in out for k in ("summary", "memory_edits", "skill_patches")
    ):
        raise ConsolidationError("Claude gave no usable answer.")
    return out


def run_consolidation(
    conn: sqlite3.Connection,
    settings: Settings,
    config: Config,
    *,
    now: datetime | None = None,
) -> ConsolidationResult:
    """The whole task. Never raises: the outcome is in the result and in the runs row."""
    now = now or datetime.now(UTC)
    run_id = _start_run(conn, now)
    result = ConsolidationResult(run_id=run_id, status="running")
    try:
        if settings.get("general.kill_switch"):
            result.status, result.error = "skipped", "kill switch is on"
        elif not settings.get("consolidation.enabled"):
            result.status, result.error = "skipped", "consolidation is switched off"
        else:
            week = gather(conn, config, settings, now)
            if week.empty():
                result.status, result.error = "skipped", "nothing new to read"
            else:
                _consolidate(conn, config, settings, week, run_id, now, result)
                result.status = "ok"
    except (BudgetCapReached, ClaudeRateLimited, ClaudeFailed, ConsolidationError) as e:
        result.status, result.error = "error", str(e)[:500]
    except Exception as e:
        log.exception("Consolidation failed")
        result.status, result.error = "error", f"{type(e).__name__}: {e}"[:500]
    _end_run(conn, run_id, result.status, result.error)
    log.info(
        "consolidation %s: memory=%d items=%d",
        result.status,
        len(result.memory_applied),
        len(result.skill_items),
    )
    return result


def _consolidate(
    conn: sqlite3.Connection,
    config: Config,
    settings: Settings,
    week: Week,
    run_id: str,
    now: datetime,
    result: ConsolidationResult,
) -> None:
    out = _ask(conn, config, settings, week, run_id)
    grams = _grams(week.corpus, int(settings.get("consolidation.verbatim_max_words")) + 1)
    usage = memory.usage(config, settings)
    current = memory.read_file(memory.memory_file(config, "MEMORY.md"))
    plan = plan_memory(
        current, out.get("memory_edits"), usage["user"], usage["cap"], grams, settings
    )
    patches, bad = plan_skills(config, out.get("skill_patches"), grams, settings)
    result.rejected = [*plan.rejected, *bad]
    day = now.astimezone(UTC).date().isoformat()

    with noise_gate.open_gate("consolidation"):
        if plan.applied:
            memory.write_memory_file(
                config,
                settings,
                "MEMORY.md",
                memory.normalize(plan.text),
                message=f"Weekly consolidation: {len(plan.applied)} memory edit(s) {day}",
            )
            result.memory_applied = plan.applied
        for patch in patches:
            if settings.get("consolidation.skill_patches_need_approval"):
                result.skill_items.append(create_item(conn, patch, run_id, now))
            else:
                skills.apply_patch(
                    config,
                    settings,
                    patch.skill,
                    patch.old,
                    patch.new,
                    message=f"Weekly consolidation: skill {patch.skill} {day}",
                )
                result.skills_applied.append(f"{patch.skill}: {patch.why}".strip(": "))
        cut = int(settings.get("consolidation.summary_max_chars"))
        lines = [_cut(prompting.no_em_dash(str(out.get("summary") or "")), cut) or "No summary."]
        lines += [f"- Memory: {a}" for a in result.memory_applied]
        lines += [
            f"- Skill patch waiting for your approval: {p.skill}"
            for p in patches
            if settings.get("consolidation.skill_patches_need_approval")
        ]
        lines += [f"- Skill changed: {a}" for a in result.skills_applied]
        lines += [f"- Refused: {r}" for r in result.rejected]
        write_dreams(config, settings, day, lines)
