"""Daily steps 8 to 10 (SPEC 6.2, 7.3, 8.5): judge jobs, then write drafts (plan G2).

Judge: the thinking model, batches of `claude.max_jobs_per_think_call` (5). Saves `judgements`.
Write: the writing model, batches of `claude.max_proposals_per_write_call` (3), only for jobs
with fit >= `pay.fit_min`. Saves `drafts`. A job is judged once and drafted once.

Python does not trust the model: dashes are cleaned, `can_afford` is recomputed from the
Connects numbers, and a draft that skipped or may have missed a job instruction is flagged.
Work is picked from the database, so a job that failed in an earlier run (rate limit, cap, no
USER.md) is retried for `claude.retry_hours`.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from difflib import SequenceMatcher
from typing import Any

from app import proposal_rules, skills
from app.config import Config, load_config
from app.llm import prompting
from app.llm.runner import BudgetCapReached, ClaudeFailed, ClaudeRateLimited, run_claude
from app.settings import Settings

log = logging.getLogger("judging")

JUDGE_PROMPT = "prompts/judge.md"
WRITE_PROMPT = "prompts/write_proposal.md"
FIX_PROMPT = "prompts/fix_proposal.md"
TASK_JUDGE = "judge"
TASK_WRITE = "write"
TASK_FIX = "fix_rules"
WIN_CHANCES = ("low", "medium", "high")
CLIENT_QUALITIES = ("poor", "ok", "good")
ALERT_USER_MD = "user_md_missing"
ALERT_USER_MD_TEXT = "Add your profile facts (USER.md) to get drafts"
COVER_LETTER_MAX = 5000
MAX_FAILURES_IN_A_ROW = 2
TIER_SQL = "CASE j.tier WHEN 'HIGH' THEN 0 ELSE 1 END"


@dataclass
class JudgeResult:
    judged: list[dict[str, Any]] = field(default_factory=list)  # new judgements of this call
    drafted: int = 0
    calls: int = 0
    stopped: str | None = None  # why the Claude steps ended early, if they did
    notes: list[str] = field(default_factory=list)  # lines for the daily note


# ---------------------------------------------------------------- small helpers


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat(timespec="seconds")


def _json(value: str | None, default: Any) -> Any:
    try:
        return json.loads(value) if value else default
    except ValueError:
        return default


def text_lines(value: Any, limit: int = 8) -> list[str]:
    """A str or a list of str as clean short lines (dashes removed)."""
    if value is None:
        return []
    items = value if isinstance(value, list) else str(value).splitlines()
    out = [prompting.no_em_dash(str(x)).strip() for x in items if x is not None and str(x).strip()]
    return out[:limit]


def _items(output: Any, key: str) -> list[dict[str, Any]]:
    found = output.get(key) or output.get("results") if isinstance(output, dict) else output
    return [x for x in found if isinstance(x, dict)] if isinstance(found, list) else []


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", "", str(text).lower()).strip()


def _same_instruction(a: str, b: str) -> bool:
    na, nb = _norm(a), _norm(b)
    if not na or not nb:
        return False
    return na in nb or nb in na or SequenceMatcher(None, na, nb).ratio() >= 0.6


def check_instructions(
    instructions: list[str], handled: list[Any], skipped: list[Any]
) -> list[str]:
    """Flags for job instructions the writer skipped, or did not say it handled."""

    def names(entries: list[Any]) -> list[str]:
        out = []
        for e in entries:
            if isinstance(e, dict):
                out.append(str(e.get("instruction") or ""))
            elif isinstance(e, str):
                out.append(e)
        return out

    flags = []
    for e in skipped:
        if isinstance(e, dict):
            reason = str(e.get("reason") or "").strip()
            flags.append(
                f"Skipped instruction: {e.get('instruction') or '?'}"
                + (f" ({reason})" if reason else "")
            )
        elif isinstance(e, str) and e.strip():
            flags.append(f"Skipped instruction: {e.strip()}")
    done = names(handled) + names(skipped)
    for ins in instructions:
        if not any(_same_instruction(ins, d) for d in done):
            flags.append(f"Not confirmed handled: {ins}")
    return [prompting.no_em_dash(f) for f in flags]


def latest_balance(conn: sqlite3.Connection) -> int | None:
    row = conn.execute("SELECT balance FROM connects ORDER BY id DESC LIMIT 1").fetchone()
    return None if row is None else int(row["balance"])


def can_afford(cost: Any, balance: int | None) -> bool | None:
    if not isinstance(cost, (int, float)) or balance is None:
        return None
    return cost <= balance


def job_input(row: sqlite3.Row) -> dict[str, Any]:
    """The cut-down job sent to Claude (the runner wraps it in untrusted tags)."""
    details = _json(row["details"], {})
    out = {k: v for k, v in (details if isinstance(details, dict) else {}).items() if k != "id"}
    out.update(
        job_id=row["job_id"],
        title=row["title"] or out.get("title"),
        tier=row["tier"],
        tier_estimated=bool(row["tier_estimated"]),
        rate_paid=row["rate_paid"],
        flags=_json(row["red_flags"], []),
        connects_cost=row["connects_cost"],
    )
    return out


def user_md_alert(conn: sqlite3.Connection, present: bool, now: datetime) -> None:
    if present:
        conn.execute("DELETE FROM alerts WHERE key = ?", (ALERT_USER_MD,))
        return
    conn.execute(
        "INSERT INTO alerts(key, message, created_at) VALUES (?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET message = excluded.message",
        (ALERT_USER_MD, ALERT_USER_MD_TEXT, _iso(now)),
    )


# ---------------------------------------------------------------- the two steps


class _Stop(Exception):
    pass


class _Runner:
    """Runs batches and applies the stop rules (cap, rate limit, repeated failures)."""

    def __init__(self, conn, settings, config, result: JudgeResult, run_id: str | None = None):
        self.conn, self.settings, self.config, self.result = conn, settings, config, result
        self.run_id = run_id  # the runs row, so /runs can show the cost of this run
        self.failures = 0

    def call(
        self,
        task: str,
        prompt_file: str,
        input_obj: dict[str, Any],
        kind: str,
        skill_task: str = "",
    ) -> Any:
        s = self.settings
        extra = skills.task_extra(self.config, s, skill_task) if skill_task else ""
        model = s.get(f"models.{kind}")
        try:
            res = run_claude(
                task,
                prompt_file,
                input_obj,
                model,
                int(s.get(f"claude.{kind}_max_turns")),
                float(s.get(f"claude.{kind}_max_budget_usd")),
                config=self.config,
                conn=self.conn,
                settings=s,
                effort=s.get(f"models.{kind}_effort") or None,
                prompt_text=prompting.build_prompt(prompt_file, extra, config=self.config),
                record_run_id=self.run_id,
            )
        except BudgetCapReached as e:
            self._stop(f"monthly Claude cap reached: {e}")
        except ClaudeRateLimited as e:
            self._stop(f"Claude rate limited: {e}")
        except ClaudeFailed as e:
            self.failures += 1
            self.result.notes.append(f"{task} call failed: {e}"[:300])
            log.warning("%s call failed: %s", task, e)
            if self.failures >= MAX_FAILURES_IN_A_ROW:
                self._stop(f"{self.failures} {task} calls failed in a row")
            return None
        self.failures = 0
        self.result.calls += 1
        return res["output"]

    def _stop(self, reason: str) -> None:
        self.result.stopped = reason
        self.result.notes.append(f"Claude steps stopped for this run: {reason}")
        log.warning("Claude steps stopped: %s", reason)
        raise _Stop


def _pending_judge(conn: sqlite3.Connection, fresh_ids: set[str], cutoff: str) -> list[sqlite3.Row]:
    rows = conn.execute(
        "SELECT j.* FROM jobs j LEFT JOIN judgements g ON g.job_id = j.job_id "
        "WHERE g.job_id IS NULL AND j.status = 'candidate' "
        f"ORDER BY {TIER_SQL}, j.rate_paid DESC"
    ).fetchall()
    # A job in fresh_ids is judged at any tier (a job the owner added by hand can be LOW).
    return [
        r
        for r in rows
        if r["job_id"] in fresh_ids
        or (r["tier"] in ("HIGH", "MID") and (r["first_seen"] or "") >= cutoff)
    ]


def _pending_write(conn: sqlite3.Connection, fit_min: int, cutoff: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT j.*, g.fit_score, g.plan AS plan_json, g.why_apply, g.suggested_rate AS j_rate "
        "FROM jobs j JOIN judgements g ON g.job_id = j.job_id "
        "WHERE j.status = 'judged' AND g.fit_score >= ? AND j.first_seen >= ? "
        "AND NOT EXISTS (SELECT 1 FROM drafts d WHERE d.job_id = j.job_id) "
        "ORDER BY g.fit_score DESC",
        (fit_min, cutoff),
    ).fetchall()


def _bool_or_none(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in ("true", "yes", "false", "no"):
        return value.strip().lower() in ("true", "yes")
    return None


def _choice_or_none(value: Any, allowed: tuple[str, ...]) -> str | None:
    text = str(value).strip().lower() if isinstance(value, str) else ""
    return text if text in allowed else None


def eligibility_fields(item: dict[str, Any]) -> dict[str, Any]:
    """The judge's auto-send answers. A missing or odd field is None: never eligible."""
    reasons = {
        key: prompting.no_em_dash(str(item.get(f"{key}_reason") or "")).strip()[:300]
        for key in ("skills_match", "win_chance", "client_quality")
    }
    return {
        "skills_match_all": _bool_or_none(item.get("skills_match_all")),
        "win_chance": _choice_or_none(item.get("win_chance"), WIN_CHANCES),
        "client_quality": _choice_or_none(item.get("client_quality"), CLIENT_QUALITIES),
        "reasons": {k: v for k, v in reasons.items() if v},
    }


def _save_judgement(
    conn: sqlite3.Connection,
    item: dict[str, Any],
    row: sqlite3.Row,
    balance: int | None,
    run_id: str | None,
    model: str,
) -> dict[str, Any] | None:
    try:
        fit = int(round(float(item.get("fit_score"))))
    except (TypeError, ValueError):
        return None
    fit = max(0, min(100, fit))
    why = text_lines(item.get("why_apply"), 5)
    risks = text_lines(item.get("risks"), 8)
    rate = item.get("suggested_rate")
    rate = float(rate) if isinstance(rate, (int, float)) and not isinstance(rate, bool) else None
    afford = can_afford(row["connects_cost"], balance)  # Python decides, not the model
    plan = {
        "proposal_plan": prompting.no_em_dash(str(item.get("proposal_plan") or "")).strip(),
        "materials": text_lines(item.get("materials"), 10),
        "instructions_in_post": text_lines(item.get("instructions_in_post"), 10),
        "can_afford": afford,
        "connects_cost": row["connects_cost"],
        "connects_balance": balance,
        "tier": row["tier"],
        "model": model,
    }
    elig = eligibility_fields(item)
    cur = conn.execute(  # a job is judged only once: a second insert is ignored
        "INSERT OR IGNORE INTO judgements(job_id, run_id, fit_score, why_apply, risks, plan, "
        "suggested_rate, skills_match_all, win_chance, client_quality, eligibility_reasons) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            row["job_id"],
            run_id,
            fit,
            "\n".join(why),
            "\n".join(risks),
            json.dumps(plan, ensure_ascii=False),
            rate,
            None if elig["skills_match_all"] is None else int(elig["skills_match_all"]),
            elig["win_chance"],
            elig["client_quality"],
            json.dumps(elig["reasons"], ensure_ascii=False),
        ),
    )
    if cur.rowcount == 0:
        return None
    conn.execute(
        "UPDATE jobs SET status = 'judged' WHERE job_id = ? AND status = 'candidate'",
        (row["job_id"],),
    )
    return {
        "job_id": row["job_id"],
        "title": row["title"],
        "tier": row["tier"],
        "fit_score": fit,
        "why_apply": why,
        "risks": risks,
        "can_afford": afford,
        "connects_cost": row["connects_cost"],
        "connects_balance": balance,
        "suggested_rate": rate,
        "skills_match_all": elig["skills_match_all"],
        "win_chance": elig["win_chance"],
        "client_quality": elig["client_quality"],
        "drafted": False,
    }


def _judge_step(runner: _Runner, rows: list[sqlite3.Row], run_id: str | None) -> None:
    conn, s, result = runner.conn, runner.settings, runner.result
    size = max(1, int(s.get("claude.max_jobs_per_think_call")))
    balance = latest_balance(conn)
    for start in range(0, len(rows), size):
        batch = rows[start : start + size]
        by_id = {r["job_id"]: r for r in batch}
        out = runner.call(
            TASK_JUDGE,
            JUDGE_PROMPT,
            {"jobs": [job_input(r) for r in batch], "connects_balance": balance},
            "think",
        )
        if out is None:
            continue
        for item in _items(out, "judgements"):
            row = by_id.get(str(item.get("job_id")))
            if row is None:
                continue
            saved = _save_judgement(conn, item, row, balance, run_id, s.get("models.think"))
            if saved:
                result.judged.append(saved)
        missing = len(batch) - sum(1 for j in result.judged if j["job_id"] in by_id)
        if missing:
            result.notes.append(f"judge: {missing} job(s) came back without a usable judgement")


def _draft_fields(
    item: dict[str, Any], row: sqlite3.Row, instructions: list[str]
) -> dict[str, Any] | None:
    letter = prompting.no_em_dash(str(item.get("cover_letter") or "")).strip()
    if not letter:
        return None
    details = _json(row["details"], {})
    questions = details.get("screening_questions") if isinstance(details, dict) else None
    answers = []
    for a in item.get("screening_answers") or []:
        if isinstance(a, dict) and (a.get("answer") or a.get("question")):
            answers.append(
                {
                    "question": prompting.no_em_dash(str(a.get("question") or "")).strip(),
                    "answer": prompting.no_em_dash(str(a.get("answer") or "")).strip(),
                }
            )
    flags = check_instructions(
        instructions,
        item.get("instructions_handled") or [],
        item.get("skipped_instructions") or [],
    )
    if len(letter) > COVER_LETTER_MAX:
        flags.append(f"Cover letter is {len(letter)} characters (Upwork limit {COVER_LETTER_MAX})")
    if any(re.fullmatch(r"\s*(n/?a|none)\.?\s*", a["answer"], re.I) for a in answers):
        flags.append("A screening answer is empty or 'NA'")
    if questions and len(answers) < len(questions):
        flags.append(f"Only {len(answers)} of {len(questions)} screening questions answered")
    rate = item.get("suggested_rate")
    rate = float(rate) if isinstance(rate, (int, float)) and not isinstance(rate, bool) else None
    return {
        "cover_letter": letter,
        "answers": answers,
        "materials": text_lines(item.get("materials"), 10),
        "suggested_rate": rate,
        "flags": flags,
    }


def _fix_rules(
    runner: _Runner, fields: dict[str, Any], problems: list[str]
) -> tuple[dict[str, Any], list[str]]:
    """One Claude rewrite with the problem list. Returns (fields, problems left).

    The rewrite is used only when it is not worse and still fits the Upwork limit. Raises _Stop
    when Claude is capped or rate limited (the caller saves the draft first).
    """
    out = runner.call(
        TASK_FIX,
        FIX_PROMPT,
        {
            "draft": {
                "cover_letter": fields["cover_letter"],
                "screening_answers": fields["answers"],
            },
            "problems": problems,
        },
        "write",
        "write_proposal",
    )
    if not isinstance(out, dict):
        return fields, problems
    letter = prompting.no_em_dash(str(out.get("cover_letter") or "")).strip()
    given = out.get("screening_answers")
    answers = []
    for i, old in enumerate(fields["answers"]):
        new = given[i] if isinstance(given, list) and i < len(given) else None
        text = new.get("answer") if isinstance(new, dict) else None
        answers.append(
            {
                "question": old["question"],
                "answer": prompting.no_em_dash(str(text)).strip() if text else old["answer"],
            }
        )
    if not letter or len(letter) > COVER_LETTER_MAX:
        return fields, problems
    left = proposal_rules.check_draft(letter, answers)
    if len(left) > len(problems):
        return fields, problems
    return {**fields, "cover_letter": letter, "answers": answers}, left


def _write_step(runner: _Runner, rows: list[sqlite3.Row], run_id: str | None) -> None:
    conn, s, result = runner.conn, runner.settings, runner.result
    size = max(1, int(s.get("claude.max_proposals_per_write_call")))
    for start in range(0, len(rows), size):
        batch = rows[start : start + size]
        by_id = {r["job_id"]: r for r in batch}
        plans = []
        for r in batch:
            plan = _json(r["plan_json"], {})
            plans.append(
                {
                    "job_id": r["job_id"],
                    "proposal_plan": plan.get("proposal_plan"),
                    "materials": plan.get("materials") or [],
                    "instructions_in_post": plan.get("instructions_in_post") or [],
                    "why_apply": (r["why_apply"] or "").splitlines(),
                    "suggested_rate": r["j_rate"],
                }
            )
        out = runner.call(
            TASK_WRITE,
            WRITE_PROMPT,
            {"jobs": [job_input(r) for r in batch], "plans": plans},
            "write",
            "write_proposal",
        )
        if out is None:
            continue
        stop = False
        for item in _items(out, "proposals"):
            row = by_id.get(str(item.get("job_id")))
            if row is None:
                continue
            instructions = (_json(row["plan_json"], {}).get("instructions_in_post")) or []
            fields = _draft_fields(item, row, instructions)
            if fields is None:
                continue
            if conn.execute("SELECT 1 FROM drafts WHERE job_id = ?", (row["job_id"],)).fetchone():
                continue
            problems = proposal_rules.check_draft(fields["cover_letter"], fields["answers"])
            if problems and not stop:  # one rewrite pass; what is left blocks auto-send
                try:
                    fields, problems = _fix_rules(runner, fields, problems)
                except _Stop:
                    stop = True
            if problems:
                fields["flags"] = [*fields["flags"], "Breaks the proposal rules, see the warning"]
            conn.execute(
                "INSERT INTO drafts(job_id, cover_letter, answers, materials, edited_by_user, "
                "suggested_rate, flags, created_at, run_id, rules_ok, rules_problems) "
                "VALUES (?,?,?,?,0,?,?,?,?,?,?)",
                (
                    row["job_id"],
                    fields["cover_letter"],
                    json.dumps(fields["answers"], ensure_ascii=False),
                    json.dumps(fields["materials"], ensure_ascii=False),
                    fields["suggested_rate"],
                    json.dumps(fields["flags"], ensure_ascii=False),
                    _iso(datetime.now(UTC)),
                    run_id,
                    0 if problems else 1,
                    json.dumps(problems, ensure_ascii=False),
                ),
            )
            conn.execute(
                "UPDATE jobs SET status = 'drafted' WHERE job_id = ? AND status = 'judged'",
                (row["job_id"],),
            )
            result.drafted += 1
            for j in result.judged:
                if j["job_id"] == row["job_id"]:
                    j["drafted"] = True
            if fields["flags"]:
                note = f"draft for {row['job_id']} flagged: {fields['flags'][0]}"
                result.notes.append(note[:300])
        if stop:
            raise _Stop


# ---------------------------------------------------------------- entry point


def judge_and_draft(
    conn: sqlite3.Connection,
    candidates: list[dict[str, Any]],
    *,
    settings: Settings | None = None,
    config: Config | None = None,
    run_id: str | None = None,
    now: datetime | None = None,
) -> JudgeResult:
    """Judge new MID/HIGH candidates, then draft the good ones. Never raises for Claude errors.

    `candidates` are the jobs of this run (dicts with an `id`). Older jobs that still wait for
    a judgement or a draft (within `claude.retry_hours`) are handled too.
    """
    settings = settings or Settings(conn)
    config = config or load_config()
    now = now or datetime.now(UTC)
    result = JudgeResult()
    cutoff = _iso(now - timedelta(hours=float(settings.get("claude.retry_hours"))))
    fresh = {str(c["id"]) for c in candidates if c.get("id")}
    fit_min = int(settings.get("pay.fit_min"))
    has_user = bool(prompting.user_facts(config))

    runner = _Runner(conn, settings, config, result, run_id)
    todo = _pending_judge(conn, fresh, cutoff)
    write_rows: list[sqlite3.Row] = []
    try:
        if todo:
            _judge_step(runner, todo, run_id)
        write_rows = _pending_write(conn, fit_min, cutoff)
        if write_rows and has_user:
            _write_step(runner, write_rows, run_id)
    except _Stop:
        pass
    if has_user:
        user_md_alert(conn, True, now)
    elif todo or write_rows:  # still judged above; only the drafts are skipped
        user_md_alert(conn, False, now)
        if write_rows:
            result.notes.append(
                f"{len(write_rows)} job(s) fit for a draft, skipped: {ALERT_USER_MD_TEXT}"
            )
    return result
