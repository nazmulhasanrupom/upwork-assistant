"""Profile test loop (plan O3, SPEC2 4.C phase 2, 6 and 7).

One test at a time, every `profile_loop.every_days` days (the scheduler asks `due`; the "Start a
profile test" button on /approvals starts it by hand). Every profile write is ONE USER CLICK (D9):
nothing here applies a change by itself.

1. Gate: `playbook.can_start_test` (baseline running, JSS guard, another test) and no pending
   `profile_change` item.
2. Reads (READ bridge only): a fresh copy of my profile (`profile_versions.snapshot`) and the top
   profiles of the main keyword (`find_freelancers search`). Only a summary of patterns goes on;
   competitor titles are marked untrusted.
3. Opus (`models.think`, `prompts/pick_tactic.md`) picks one tactic from the PLAYBOOK candidates.
4. Sonnet (`models.write`, `prompts/draft_profile_change.md`) drafts the new value from USER.md
   facts. Python does not trust it: em dashes cleaned, markup refused, length limits from
   settings, no number that is in no source, no copy of a competitor title, no empty change.
5. ONE approvals item (kind `profile_change`, before and after). Approve (a click) saves a copy of
   the profile and asks Upwork for a PREVIEW. The user then confirms the preview with a second
   click on the history page (`confirm`, which uses `writes.confirm_profile_preview`; dry-run
   blocks it). The batch button "Approve all small changes" is one click for every small item.
6. A confirmed `profile_change` starts the experiment with its baseline
   (`playbook.start_experiment`).
7. After `playbook.test_days` the daily task calls `judge_due`: 7-day averages against the baseline
   -> worked (keep), hurt (a `profile_rollback` item is queued) or no effect (the next test picks
   another tactic). The result goes to PLAYBOOK.md through `playbook.finish_experiment`.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import statistics
import uuid
from collections import Counter
from contextlib import nullcontext
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

from app import approvals, mail, playbook, rank, skills
from app import profile_versions as pv
from app.base_url import get_base_url
from app.config import Config
from app.llm import prompting
from app.llm.runner import BudgetCapReached, ClaudeFailed, ClaudeRateLimited, run_claude
from app.review import service as review_service
from app.settings import Settings
from app.upwork import writes
from app.upwork.client import UpworkAuthError

log = logging.getLogger("profile_loop")

TASK = "profile_test"
KIND = "profile_change"  # the approvals kind
EVENT = "profile_change_ready"
PICK_TASK, PICK_PROMPT = "pick_tactic", "prompts/pick_tactic.md"
DRAFT_TASK, DRAFT_PROMPT = "draft_profile_change", "prompts/draft_profile_change.md"
COMPETITOR_TAG = "untrusted_competitor_data"
PROFILE_TAG = "profile_data"
FIELDS = tuple(writes.PROFILE_ACTIONS)  # title, description, skills
_TAG_RE = re.compile(r"<[^>]{1,200}>")


class ProfileLoopError(RuntimeError):
    """A problem with a message that is safe to show to the owner."""


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat(timespec="seconds")


def _wrap(tag: str, value: Any) -> str:
    text = json.dumps(value, ensure_ascii=False, indent=1)
    text = re.sub(rf"<(/?)\s*{tag}", r"&lt;\1" + tag, text, flags=re.IGNORECASE)
    return f"<{tag}>\n{text}\n</{tag}>"


def _parse(text: Any) -> datetime | None:
    try:
        dt = datetime.fromisoformat(str(text))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


# ---------------------------------------------------------------- when


def due(settings: Settings, now: datetime | None = None) -> bool:
    """True when `profile_loop.every_days` have passed since the last attempt (or never tried)."""
    last = _parse(settings.get("profile_loop.last_run"))
    if last is None:
        return True
    gap = timedelta(days=max(0.0, float(settings.get("profile_loop.every_days"))))
    return (now or datetime.now(UTC)) - last >= gap


def pending_change(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM approvals WHERE kind = ? AND status = 'pending' ORDER BY id DESC LIMIT 1",
        (KIND,),
    ).fetchone()


# ---------------------------------------------------------------- reads


def candidates(config: Config, settings: Settings) -> list[dict[str, Any]]:
    """Playbook tactics that may be tested now: writable, allowed type, eligible status."""
    writable = {int(n) for n in settings.get("profile_loop.writable_tactics")}
    types = {str(t).casefold() for t in settings.get("profile_loop.allowed_types")}
    statuses = {str(s).casefold() for s in settings.get("profile_loop.eligible_statuses")}
    return [
        t
        for t in playbook.tactics(config)
        if t["no"] in writable
        and t["type"].casefold() in types
        and t["status"].casefold() in statuses
    ]


def _money(value: Any) -> float | None:
    m = re.search(r"\d+(?:\.\d+)?", str(value or "").replace(",", ""))
    return float(m.group()) if m else None


def summarize_competitors(rows: list[Any]) -> dict[str, Any]:
    """Patterns of the top profiles: counts and averages, the titles apart (untrusted text)."""
    people = [r for r in rows if isinstance(r, dict)]
    titles = [pv._clean(p.get("title")) for p in people]
    titles = [t for t in titles if t]
    rates = [x for x in (_money(p.get("hourly_rate")) for p in people) if x is not None]
    jss = [p["job_success_score"] for p in people if isinstance(p.get("job_success_score"), int)]
    skills = [[s for s in (p.get("skills") or []) if isinstance(s, str)] for p in people]
    common = Counter(s for lst in skills for s in lst).most_common(10)
    n = len(people)
    return {
        "profiles": n,
        "title_avg_chars": round(statistics.mean(len(t) for t in titles), 1) if titles else None,
        "title_with_pipe_share": round(sum("|" in t for t in titles) / len(titles), 2)
        if titles
        else None,
        "rate_median": statistics.median(rates) if rates else None,
        "rate_min": min(rates) if rates else None,
        "rate_max": max(rates) if rates else None,
        "jss_avg": round(statistics.mean(jss), 1) if jss else None,
        "top_rated": sum(bool(p.get("top_rated")) for p in people),
        "boosted": sum(p.get("boosted") is True for p in people),
        "skills_avg_count": round(statistics.mean(len(s) for s in skills), 1) if n else None,
        "common_skills": [{"skill": s, "count": c} for s, c in common],
        "titles": titles,
    }


def top_profiles(bridge: Any, conn: sqlite3.Connection, settings: Settings) -> dict[str, Any]:
    """The top `profile_loop.top_n` freelancers of the main approved keyword(s), summarised.
    READ tools only, inside the task's own call budget."""
    org_uid = str(settings.get("upwork.talent_org_uid") or "")
    if not org_uid:
        raise ProfileLoopError("No freelancer account is chosen yet (see the Upwork page).")
    words = rank.approved_keywords(conn, settings)[
        : max(1, int(settings.get("profile_loop.top_keywords")))
    ]
    if not words:
        raise ProfileLoopError("No approved keyword to compare with.")
    limit = max(1, int(settings.get("profile_loop.top_n")))
    calls = [
        (rank.TOOL, {"action": "search", "org_uid": org_uid,
                     "params": {"query": w, "limit": limit, "offset": 0}})
        for w in words
    ]  # fmt: skip
    budget = getattr(bridge, "call_budget", None)
    rows: list[Any] = []
    with budget("profile_loop.max_mcp_calls") if budget else nullcontext():
        for answer in bridge.call_many(calls):
            data = answer.unwrap()
            found = data.get("freelancers") if isinstance(data, dict) else None
            rows.extend((found if isinstance(found, list) else [])[:limit])
    if not rows:
        raise ProfileLoopError("The search for top profiles returned no freelancers.")
    out = summarize_competitors(rows)
    out["keywords"] = words
    return out


# ---------------------------------------------------------------- Claude: pick


def _claude(
    task, prompt, data, model_key, turns, budget, timeout, conn, config, settings, run_id, extra=""
):
    try:
        return run_claude(
            task,
            prompt,
            data,
            str(settings.get(model_key)),
            int(settings.get(turns)),
            float(settings.get(budget)),
            config=config,
            conn=conn,
            settings=settings,
            effort=settings.get(
                "models.think_effort" if model_key == "models.think" else "models.write_effort"
            )
            or None,
            timeout=int(settings.get(timeout)),
            prompt_text=prompting.build_prompt(prompt, extra, config=config),
            record_run_id=run_id,
        )
    except ClaudeFailed as e:
        raise ProfileLoopError(f"Claude could not do the {task} step: {str(e)[:200]}") from e


def pick_tactic(conn, config, settings, cands, current, metrics_now, top, run_id) -> dict[str, Any]:
    """Opus picks one of `cands`. Returns {tactic_no, tactic, type, field, reason}."""
    data = {
        "playbook": playbook.read(config),
        "candidates": cands,
        "profile": _wrap(PROFILE_TAG, current),
        "metrics": metrics_now,
        "competitors": _wrap(COMPETITOR_TAG, top),
        "fields": list(FIELDS),
        "reason_max_chars": int(settings.get("profile_loop.reason_max_chars")),
    }
    res = _claude(PICK_TASK, PICK_PROMPT, data, "models.think", "profile_loop.pick_max_turns",
                  "profile_loop.pick_max_budget_usd", "profile_loop.pick_timeout_seconds",
                  conn, config, settings, run_id)  # fmt: skip
    return validate_pick(res["output"], cands, settings)


def validate_pick(output: Any, cands: list[dict[str, Any]], settings: Settings) -> dict[str, Any]:
    """Python decides what counts: the tactic must be a candidate (its text comes from the
    playbook, not from Claude) and the field one of the three writable fields."""
    if not isinstance(output, dict):
        raise ProfileLoopError("Claude's answer had no tactic.")
    by_no = {c["no"]: c for c in cands}
    no = output.get("tactic_no")
    if isinstance(no, bool) or not isinstance(no, int) or no not in by_no:
        raise ProfileLoopError("Claude picked a tactic that is not on the candidate list.")
    fld = output.get("field")
    if fld not in FIELDS:
        raise ProfileLoopError("Claude picked a profile field that cannot be changed here.")
    reason = output.get("reason")
    reason = prompting.no_em_dash(" ".join(reason.split())) if isinstance(reason, str) else ""
    cap = int(settings.get("profile_loop.reason_max_chars"))
    return {
        "tactic_no": no,
        "tactic": by_no[no]["tactic"],
        "type": by_no[no]["type"],
        "field": fld,
        "reason": reason[:cap],
    }


# ---------------------------------------------------------------- Claude: draft


def _field_text(field_name: str, value: Any) -> str:
    return "\n".join(value) if field_name == "skills" else str(value or "")


def _copied(text: str, titles: list[str], words: int) -> bool:
    """True when `text` repeats `words` words in a row from one of the competitor titles."""
    if words < 1:
        return False
    mine = re.findall(r"\w+", text.casefold())
    grams = {" ".join(mine[i : i + words]) for i in range(len(mine) - words + 1)}
    for title in titles:
        theirs = re.findall(r"\w+", title.casefold())
        for i in range(len(theirs) - words + 1):
            if " ".join(theirs[i : i + words]) in grams:
                return True
    return False


def validate_draft(
    output: Any,
    pick: dict[str, Any],
    current: Any,
    settings: Settings,
    sources: str,
    competitor_titles: list[str],
) -> tuple[Any, str]:
    """Check Claude's draft. Returns (new value, summary). Raises ProfileLoopError. The value is a
    string (title, description) or a list of names (skills). Em dashes are cleaned; everything
    else that fails is refused, never fixed up."""
    fld = pick["field"]
    if not isinstance(output, dict) or output.get("field") != fld:
        raise ProfileLoopError("Claude's draft was for the wrong field or had no value.")
    summary = output.get("summary")
    summary = prompting.no_em_dash(" ".join(summary.split())) if isinstance(summary, str) else ""
    if fld == "skills":
        raw = output.get("skills")
        if not isinstance(raw, list) or not all(isinstance(s, str) for s in raw):
            raise ProfileLoopError("Claude's draft had no skill list.")
        value: Any = []
        seen: set[str] = set()
        for s in raw:
            name = prompting.no_em_dash(" ".join(s.split()))
            if name and name.casefold() not in seen:
                seen.add(name.casefold())
                value.append(name)
        if not value:
            raise ProfileLoopError("Claude's draft had no skills.")
        if len(value) > int(settings.get("profile_loop.skills_max")):
            raise ProfileLoopError("The draft has more skills than the limit allows.")
        if any(len(s) > int(settings.get("profile_loop.skill_max_chars")) for s in value):
            raise ProfileLoopError("The draft has a skill name over the length limit.")
        if value == list(current or []):
            raise ProfileLoopError("The draft does not change the skills.")
    else:
        text = output.get("text")
        if not isinstance(text, str) or not text.strip():
            raise ProfileLoopError("Claude's draft had no text.")
        value = prompting.no_em_dash(text.replace("\r\n", "\n").strip())
        limit = int(settings.get(f"profile_loop.{fld}_max_chars"))
        if len(value) > limit:
            raise ProfileLoopError(
                f"The draft {fld} is {len(value)} characters; the limit is {limit}."
            )
        if value == str(current or "").strip():
            raise ProfileLoopError(f"The draft does not change the {fld}.")
    text_all = _field_text(fld, value)
    if prompting.has_em_dash(text_all):
        raise ProfileLoopError("The draft still has an em dash.")
    if _TAG_RE.search(text_all):
        raise ProfileLoopError("The draft has markup in it.")
    added = review_service.new_numbers([text_all], sources)
    if added:
        raise ProfileLoopError(
            "The draft has numbers that are not in USER.md or your profile: " + ", ".join(added[:5])
        )
    if _copied(text_all, competitor_titles, int(settings.get("profile_loop.copy_max_words"))):
        raise ProfileLoopError("The draft repeats words from another freelancer's title.")
    return value, summary[: int(settings.get("profile_loop.reason_max_chars"))]


def draft_change(conn, config, settings, pick, current_fields, top, run_id) -> tuple[Any, str]:
    fld = pick["field"]
    stats = {k: v for k, v in top.items() if k != "titles"}  # patterns only, no competitor text
    data = {
        "tactic": pick["tactic"],
        "reason": pick["reason"],
        "field": fld,
        "current": current_fields[fld],
        "profile": _wrap(PROFILE_TAG, current_fields),
        "keywords": top.get("keywords", []),
        "patterns": stats,
        "limits": {
            "title_max_chars": int(settings.get("profile_loop.title_max_chars")),
            "description_max_chars": int(settings.get("profile_loop.description_max_chars")),
            "skills_max": int(settings.get("profile_loop.skills_max")),
            "skill_max_chars": int(settings.get("profile_loop.skill_max_chars")),
        },
    }
    res = _claude(DRAFT_TASK, DRAFT_PROMPT, data, "models.write", "profile_loop.draft_max_turns",
                  "profile_loop.draft_max_budget_usd", "profile_loop.draft_timeout_seconds",
                  conn, config, settings, run_id,
                  skills.task_extra(config, settings, "draft_profile_change"))  # fmt: skip
    sources = "\n".join(
        [prompting.user_facts(config), *(_field_text(f, current_fields[f]) for f in current_fields)]
    )
    return validate_draft(
        res["output"], pick, current_fields[fld], settings, sources, top.get("titles", [])
    )


# ---------------------------------------------------------------- the approval item


def create_item(
    conn: sqlite3.Connection,
    run_id: str,
    pick: dict[str, Any],
    current: dict[str, Any],
    value: Any,
    summary: str,
    now: datetime,
) -> int:
    fld = pick["field"]
    after = {
        fld: value,
        "_meta": {
            "tactic_no": pick["tactic_no"],
            "tactic": pick["tactic"],
            "field": fld,
            "reason": pick["reason"],
            "summary": summary,
        },
    }
    return approvals.add(
        conn,
        KIND,
        f"Test: {pick['tactic']}",
        ref_id=run_id,
        before={fld: current[fld]},
        after=after,
        now=now,
    )


def meta(row: Any) -> dict[str, Any]:
    """The `_meta` block of a profile_change item (row or pending-item dict)."""
    raw = row["after_json"] if isinstance(row, sqlite3.Row) else json.dumps(row.get("after_data"))
    try:
        m = json.loads(raw or "{}").get("_meta")
    except (ValueError, AttributeError):
        return {}
    return m if isinstance(m, dict) else {}


def notify(conn, config, settings, item_id: int, pick, now) -> str:
    text, html = mail.build_email(
        "A profile test is ready",
        [
            f"Tactic: {pick['tactic']}",
            "Open the panel to read the before and after. Nothing changes on Upwork until you "
            "approve it and then confirm the preview.",
        ],
        link_path="/approvals",
        base_url=get_base_url(settings, config),
        button_label="Open approvals",
    )
    return mail.send_event(
        conn, EVENT, str(item_id), "Profile test ready for your approval", text, html,
        config=config, settings=settings, now=now,
    )  # fmt: skip


# ---------------------------------------------------------------- approve handler


def apply_change(row: sqlite3.Row, decision: str, conn: sqlite3.Connection) -> None:
    """Approvals handler for `profile_change`. Reject records only. Approve saves a copy of the
    profile and makes ONE Upwork preview. It never confirms. The item stays pending when a test
    cannot start or no preview worked."""
    if decision != "approved":
        return
    try:
        after = json.loads(row["after_json"])
        fld = after["_meta"]["field"]
        value = after[fld]
    except (TypeError, ValueError, KeyError) as e:
        raise ProfileLoopError("The change item has no data.") from e
    if fld not in FIELDS:
        raise ProfileLoopError("The change item names a field that cannot be changed.")
    settings = approvals.context().get("settings")
    if settings is None:
        raise ProfileLoopError("A profile change can only be approved from the web page.")
    reason = playbook.can_start_test(conn, settings)
    if reason:
        raise ProfileLoopError(reason)
    pv.preview_item(row, conn, [fld], {fld: value})


approvals.register(KIND, apply_change)


# ---------------------------------------------------------------- confirm (the second click)


@dataclass
class ConfirmOutcome:
    confirm: writes.ProfileConfirm
    experiment_id: int | None = None
    note: str = ""


def _preview_row(conn: sqlite3.Connection, row_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM profile_previews WHERE id = ?", (row_id,)).fetchone()


def confirm(
    click: writes.UserClick,
    conn: sqlite3.Connection,
    config: Config,
    settings: Settings,
    writer: Any,
    preview_row_id: int,
    now: datetime | None = None,
) -> ConfirmOutcome:
    """Confirm one profile preview (profile_change or profile_rollback) with a user click. A
    confirmed `profile_change` starts the experiment with its baseline."""
    res = writes.confirm_profile_preview(
        click, conn=conn, config=config, settings=settings, writer=writer, row_id=preview_row_id
    )
    if res.status == "dry_run_blocked":
        return ConfirmOutcome(res, note="Dry-run is on: nothing was sent to Upwork.")
    if res.status != "confirmed":
        return ConfirmOutcome(res, note=res.message or "Upwork did not confirm the change.")
    row = _preview_row(conn, preview_row_id)
    item = None
    if row is not None and row["approval_id"] is not None:
        item = conn.execute(
            "SELECT * FROM approvals WHERE id = ?", (row["approval_id"],)
        ).fetchone()
    if item is None or item["kind"] != KIND:
        return ConfirmOutcome(res, note="The profile change is live on Upwork.")
    info = meta(item)
    try:
        exp_id = playbook.start_experiment(
            conn, settings, config, str(info.get("tactic") or item["title"]),
            info.get("tactic_no") if isinstance(info.get("tactic_no"), int) else None, now,
        )  # fmt: skip
    except playbook.PlaybookError as e:
        log.warning("Profile change is live but no test could start: %s", e)
        return ConfirmOutcome(res, note=f"The change is live, but the test did not start: {e}")
    conn.execute(
        "UPDATE experiments SET approval_id = ?, version_id = ?, field = ? WHERE id = ?",
        (item["id"], row["before_version_id"], row["field"], exp_id),
    )
    return ConfirmOutcome(res, exp_id, "The change is live and the test has started.")


# ---------------------------------------------------------------- batch: approve all small changes


def _words(item: dict[str, Any], settings: Settings) -> int | None:
    before, after = item.get("before_data"), item.get("after_data")
    if not isinstance(before, dict) or not isinstance(after, dict):
        return None
    total = 0
    for f in pv.fields_diff(dict(before), dict(after), settings):
        if f["changed"] and f["restorable"]:
            total += int(f["added_words"]) + int(f["removed_words"])
    return total


def small_items(
    conn: sqlite3.Connection, settings: Settings, now: datetime | None = None
) -> list[dict[str, Any]]:
    """Pending profile items (change or roll back) of at most `profile_loop.small_change_max_words`
    added plus removed words."""
    cap = int(settings.get("profile_loop.small_change_max_words"))
    out = []
    for it in approvals.pending_items(conn, settings, now):
        if it["kind"] not in (KIND, pv.KIND):
            continue
        n = _words(it, settings)
        if n is not None and n <= cap:
            out.append(it)
    return out


@dataclass
class BatchResult:
    approved: int = 0
    confirmed: int = 0
    blocked: int = 0
    errors: list[str] = field(default_factory=list)


def approve_small(
    conn: sqlite3.Connection,
    settings: Settings,
    config: Config,
    context: dict[str, Any],
    now: datetime | None = None,
) -> BatchResult:
    """The batch button: ONE user click for all small profile items. Per item: approve (copy of the
    profile + preview), then, when `profile_loop.batch_confirms` is on, confirm each of its
    previews with a click made for this request. Dry-run blocks every confirm. An item that fails
    stays pending; the others go on."""
    result = BatchResult()
    for it in small_items(conn, settings, now):
        try:
            approvals.decide(conn, it["id"], "approved", now, context)
        except approvals.ApprovalError as e:
            result.errors.append(f"{it['title']}: {e}")
            continue
        result.approved += 1
        if not settings.get("profile_loop.batch_confirms"):
            continue
        rows = conn.execute(
            "SELECT id FROM profile_previews WHERE approval_id = ? AND status = 'previewed' "
            "AND confirmed_at IS NULL ORDER BY id",
            (it["id"],),
        ).fetchall()
        for r in rows:
            try:
                out = confirm(context["confirm_click"](), conn, config, settings,
                              context["writer"](), int(r["id"]), now)  # fmt: skip
            except Exception as e:  # one failed confirm must not stop the others
                log.warning("Batch confirm failed for preview %s: %s", r["id"], e)
                result.errors.append(f"{it['title']}: {str(e)[:150]}")
                continue
            if out.confirm.status == "confirmed":
                result.confirmed += 1
            elif out.confirm.status == "dry_run_blocked":
                result.blocked += 1
            else:
                result.errors.append(f"{it['title']}: {out.note}")
    return result


# ---------------------------------------------------------------- judge


@dataclass
class Verdict:
    outcome: str  # worked | no effect | hurt
    text: str
    changes: list[dict[str, Any]] = field(default_factory=list)


def _pct(old: float, new: float, lower_better: bool) -> float:
    """Percent change, positive = better. A zero baseline counts as +100 when it grew."""
    if old == 0:
        return 0.0 if new == 0 else (-100.0 if lower_better else 100.0)
    change = (new - old) / abs(old) * 100.0
    return -change if lower_better else change


def compare(baseline: dict[str, Any], now_vals: dict[str, Any], settings: Settings) -> Verdict:
    """7-day averages now against the baseline. A metric counts when it moved by at least
    `profile_loop.change_pct` percent. A JSS drop of `profile_loop.jss_drop_points` is hurt at
    once. More worse than better = hurt; more better = worked; otherwise no effect."""
    threshold = float(settings.get("profile_loop.change_pct"))
    changes: list[dict[str, Any]] = []
    for group, lower_better in (("metrics", False), ("rank", True)):
        old_all, new_all = baseline.get(group) or {}, now_vals.get(group) or {}
        for key, old in old_all.items():
            new = new_all.get(key)
            if not isinstance(old, (int, float)) or not isinstance(new, (int, float)):
                continue
            changes.append(
                {"key": key, "old": old, "new": new, "pct": round(_pct(old, new, lower_better), 1)}
            )
    better = [c for c in changes if c["pct"] >= threshold]
    worse = [c for c in changes if c["pct"] <= -threshold]
    jss = next((c for c in changes if c["key"] == "jss"), None)
    jss_drop = jss is not None and jss["old"] - jss["new"] >= float(
        settings.get("profile_loop.jss_drop_points")
    )
    if jss_drop or len(worse) > len(better):
        outcome = "hurt"
    elif len(better) > len(worse):
        outcome = "worked"
    else:
        outcome = "no effect"
    if not changes:
        text = "Not enough numbers to compare."
    else:
        parts = [f"{c['key']} {c['old']:.1f} to {c['new']:.1f} ({c['pct']:+.0f}%)" for c in changes]
        text = f"{len(better)} better, {len(worse)} worse: " + "; ".join(parts)
    return Verdict(outcome, text, changes)


def judge_due(
    conn: sqlite3.Connection,
    settings: Settings,
    config: Config,
    now: datetime | None = None,
) -> list[str]:
    """Judge the active test when its end date is reached. Returns lines for the daily note.
    Never starts a write: a bad result only queues a `profile_rollback` approval."""
    now = now or datetime.now(UTC)
    active = playbook.active_experiment(conn)
    if active is None:
        return []
    today = playbook._today(settings, now)
    try:
        ends = date.fromisoformat(active["ends"])
    except ValueError:
        return []
    if today < ends:
        return []
    try:
        baseline = json.loads(active["baseline_json"] or "{}")
    except ValueError:
        baseline = {}
    verdict = compare(baseline, playbook.baseline_metrics(conn, settings, now), settings)
    text = verdict.text
    action = "kept"
    if verdict.outcome == "hurt":
        action = "no roll back possible"
        if active["version_id"]:
            try:
                item, _ = pv.request_rollback(conn, int(active["version_id"]), now)
                action = f"roll back waits in approvals (item {item})"
            except pv.ProfileVersionError as e:
                action = f"no roll back item: {e}"
        text = f"{text} Roll back: {action}."
    elif verdict.outcome == "no effect":
        action = "kept, next test picks another tactic"
    playbook.finish_experiment(conn, settings, config, verdict.outcome, text, now)
    return [f"Profile test finished: {active['tactic']}: {verdict.outcome} ({action})."]


# ---------------------------------------------------------------- the run


@dataclass
class LoopResult:
    run_id: str
    status: str  # ok | skipped | error | auth_expired
    item_id: int | None = None
    pick: dict[str, Any] | None = None
    email: str | None = None
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


def run_profile_test(
    conn: sqlite3.Connection,
    settings: Settings,
    config: Config,
    bridge: Any = None,
    *,
    now: datetime | None = None,
) -> LoopResult:
    """The whole task. Never raises: the outcome is in the result and in the runs row."""
    from app.upwork.bridge import ClaudeUpworkBridge

    now = now or datetime.now(UTC)
    run_id = _start_run(conn, now)
    result = LoopResult(run_id=run_id, status="running")

    def skip(reason: str) -> LoopResult:
        result.status, result.error = "skipped", reason
        _end_run(conn, run_id, result.status, result.error)
        return result

    if settings.get("general.kill_switch"):
        return skip("kill switch is on")
    reason = playbook.can_start_test(conn, settings, now)
    if reason:
        return skip(reason)
    if pending_change(conn) is not None:
        return skip("An earlier profile change still waits in approvals.")
    cands = candidates(config, settings)
    if not cands:
        return skip("No untested tactic can be tested with the profile write tools.")

    owns = bridge is None
    try:
        if bridge is None:
            bridge = ClaudeUpworkBridge(config, settings, conn, run_id)
        settings.set("profile_loop.last_run", _iso(now))
        pv.snapshot(bridge, conn, settings, config, now, reason="test_loop")
        latest = pv.latest(conn)
        current = {f: pv.row_fields(latest)[f] for f in FIELDS}
        top = top_profiles(bridge, conn, settings)
        metrics_now = playbook.baseline_metrics(conn, settings, now)
        pick = pick_tactic(conn, config, settings, cands, current, metrics_now, top, run_id)
        result.pick = pick
        value, summary = draft_change(conn, config, settings, pick, current, top, run_id)
        result.item_id = create_item(conn, run_id, pick, current, value, summary, now)
        result.email = notify(conn, config, settings, result.item_id, pick, now)
        result.status = "ok"
    except UpworkAuthError as e:
        result.status, result.error = "auth_expired", str(e)
    except (BudgetCapReached, ClaudeRateLimited, ProfileLoopError, pv.ProfileVersionError) as e:
        result.status, result.error = "error", str(e)[:500]
    except Exception as e:
        log.exception("Profile test failed")
        result.status, result.error = "error", f"{type(e).__name__}: {e}"[:500]
    finally:
        if owns and bridge is not None:
            bridge.close()
    _end_run(conn, run_id, result.status, result.error)
    log.info("profile_test %s: item=%s", result.status, result.item_id)
    return result
