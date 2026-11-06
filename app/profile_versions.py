"""Profile versions (plan O1, SPEC2 4.C phase 2): saved copies of my Upwork profile.

- `snapshot` reads `get_profile get` through the READ bridge and saves the answer as
  `<data_dir>/profile_versions/<timestamp>.json` plus one row in `profile_versions`. Nothing is
  saved when the editable content (title, overview, skills, rate) equals the newest version.
  It runs daily (the daily task, never fails the run) and by hand ("Save a copy now"). Phase O3
  calls it with `reason="before_change"` before it changes anything.
- The history page diffs two versions with `app.review.diff.compare`.
- "Roll back" never writes to Upwork. It puts ONE item (kind `profile_rollback`) in the approvals
  queue. Approving it makes a copy of the current profile, then asks Upwork for a PREVIEW of each
  changed field (`writes.preview_profile_update`, one user click each). The previews are shown on
  the history page. No `confirm_preview` is made here: the confirm is a separate user click on
  that page (`writes.confirm_profile_preview`, wired by `app.profile_loop`, plan O3).
Retention: `profile_versions.keep` newest versions.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from app import approvals
from app.config import Config
from app.review import diff
from app.settings import Settings
from app.upwork import writes

log = logging.getLogger("profile_versions")

KIND = "profile_rollback"  # the approvals kind
ALERT_KEY = "profile_snapshot_failed"
FIELDS = ("title", "description", "skills", "rate")  # all compared and shown
RESTORABLE = tuple(writes.PROFILE_ACTIONS)  # title, description, skills (rate has no write tool)
LABELS = {"title": "Title", "description": "Overview", "skills": "Skills", "rate": "Hourly rate"}
FOLDER = "profile_versions"

_TAG_RE = re.compile(r"</?untrusted_[a-z_]+>")


class ProfileVersionError(RuntimeError):
    """A message that is safe to show to the user."""


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat(timespec="seconds")


def _clean(value: Any) -> str:
    """Text of one profile field without the untrusted-content tags Upwork wraps it in."""
    if not isinstance(value, str):
        return ""
    return _TAG_RE.sub("", value).strip()


def extract_fields(payload: Any) -> dict[str, Any]:
    """The editable parts of a `get_profile get` answer: title, description, skills, rate."""
    data = payload.get("data") if isinstance(payload, dict) else None
    data = data if isinstance(data, dict) else {}
    personal = data.get("personalData")
    personal = personal if isinstance(personal, dict) else {}
    rate = personal.get("chargeRate")
    rate = rate.get("rawValue") if isinstance(rate, dict) else None
    skills: list[str] = []
    raw_skills = data.get("skills")
    for item in raw_skills if isinstance(raw_skills, list) else []:
        name = item.get("prettyName") if isinstance(item, dict) else item
        name = _clean(name)
        if name and name not in skills:
            skills.append(name)
    return {
        "title": _clean(personal.get("title")),
        "description": _clean(personal.get("description")),
        "skills": skills,
        "rate": "" if rate is None else str(rate).strip(),
    }


def content_hash(fields: dict[str, Any]) -> str:
    canon = json.dumps({k: fields.get(k) for k in FIELDS}, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


def row_fields(row: sqlite3.Row | None) -> dict[str, Any]:
    if row is None:
        return {"title": "", "description": "", "skills": [], "rate": ""}
    try:
        skills = json.loads(row["skills_json"] or "[]")
    except ValueError:
        skills = []
    return {
        "title": row["title"],
        "description": row["description"],
        "skills": skills if isinstance(skills, list) else [],
        "rate": row["rate"],
    }


# ---------------------------------------------------------------- reading and saving


def versions_dir(config: Config):
    return config.data_dir / FOLDER


def latest(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM profile_versions ORDER BY id DESC LIMIT 1").fetchone()


def get_version(conn: sqlite3.Connection, version_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM profile_versions WHERE id = ?", (version_id,)).fetchone()


def list_versions(conn: sqlite3.Connection, limit: int = 100) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM profile_versions ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()


def fetch_profile(bridge: Any, settings: Settings) -> Any:
    """`get_profile get` through the READ bridge. Raises ProfileVersionError without an account.
    A login problem (UpworkAuthError) and a failed call (UpworkMCPError) are raised as they are."""
    org_uid = str(settings.get("upwork.talent_org_uid") or "")
    if not org_uid:
        raise ProfileVersionError("No freelancer account is chosen yet (see the Upwork page).")
    [result] = bridge.call_many(
        [("get_profile", {"action": "get", "org_uid": org_uid, "params": {}})]
    )
    return result.unwrap()


@dataclass
class SnapshotResult:
    saved: bool
    version_id: int
    reason: str
    note: str  # "saved" | "unchanged"


def _prune(conn: sqlite3.Connection, config: Config, settings: Settings) -> None:
    keep = max(1, int(settings.get("profile_versions.keep")))
    old = conn.execute(
        "SELECT id, file FROM profile_versions ORDER BY id DESC LIMIT -1 OFFSET ?", (keep,)
    ).fetchall()
    for r in old:
        conn.execute("DELETE FROM profile_versions WHERE id = ?", (r["id"],))
        if r["file"]:
            try:
                (versions_dir(config) / r["file"]).unlink(missing_ok=True)
            except OSError:
                log.warning("Could not delete the old profile file %s", r["file"])


def snapshot(
    bridge: Any,
    conn: sqlite3.Connection,
    settings: Settings,
    config: Config,
    now: datetime | None = None,
    reason: str = "daily",
) -> SnapshotResult:
    """Read my profile and save it as a new version unless the content equals the newest one."""
    now = now or _now()
    payload = fetch_profile(bridge, settings)
    fields = extract_fields(payload)
    if not fields["title"] and not fields["description"]:
        raise ProfileVersionError("The profile answer had no title and no overview.")
    digest = content_hash(fields)
    last = latest(conn)
    if last is not None and last["content_hash"] == digest:
        return SnapshotResult(False, int(last["id"]), reason, "unchanged")
    folder = versions_dir(config)
    folder.mkdir(parents=True, exist_ok=True)
    stem = now.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    name, n = f"{stem}.json", 1
    while (folder / name).exists():
        n += 1
        name = f"{stem}-{n}.json"
    (folder / name).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    cur = conn.execute(
        "INSERT INTO profile_versions(created_at, reason, content_hash, file, title, description, "
        "skills_json, rate) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            _iso(now),
            reason,
            digest,
            name,
            fields["title"],
            fields["description"],
            json.dumps(fields["skills"], ensure_ascii=False),
            fields["rate"],
        ),
    )
    _prune(conn, config, settings)
    return SnapshotResult(True, int(cur.lastrowid or 0), reason, "saved")


# ---------------------------------------------------------------- compare


def _text(field: str, value: Any) -> str:
    if field == "skills":
        return "\n".join(value or [])
    return str(value or "")


def fields_diff(before: dict[str, Any], after: dict[str, Any], settings: Settings) -> list[dict]:
    """One entry per field: label, changed, and the word diff (`ops`) from `diff.compare`."""
    cap = int(settings.get("profile_versions.diff_max_chars"))
    out = []
    for f in FIELDS:
        a, b = _text(f, before.get(f))[:cap], _text(f, after.get(f))[:cap]
        cmp = diff.compare(a, b)
        out.append(
            {
                "field": f,
                "label": LABELS[f],
                "changed": bool(cmp["changed"]),
                "ops": cmp["ops"],
                "added_words": cmp["added_words"],
                "removed_words": cmp["removed_words"],
                "restorable": f in RESTORABLE,
            }
        )
    return out


# ---------------------------------------------------------------- roll back


def request_rollback(
    conn: sqlite3.Connection, version_id: int, now: datetime | None = None
) -> tuple[int, bool]:
    """Put a roll back item in the approvals queue. Returns (approval id, already_there).
    Raises ProfileVersionError for an unknown version or when it matches the newest one."""
    target = get_version(conn, version_id)
    if target is None:
        raise ProfileVersionError("That version does not exist.")
    newest = latest(conn)
    before, after = row_fields(newest), row_fields(target)
    if all(before[f] == after[f] for f in RESTORABLE):
        raise ProfileVersionError("This version already matches the newest saved profile.")
    existing = conn.execute(
        "SELECT id FROM approvals WHERE kind = ? AND ref_id = ? AND status = 'pending'",
        (KIND, str(version_id)),
    ).fetchone()
    if existing:
        return int(existing["id"]), True
    after = {f: after[f] for f in RESTORABLE}
    before = {f: before[f] for f in RESTORABLE}
    item = approvals.add(
        conn,
        KIND,
        f"Roll back the profile to the version of {target['created_at'][:10]}",
        ref_id=str(version_id),
        before=before,
        after=after,
        now=now,
    )
    return item, False


def preview_item(
    row: sqlite3.Row,
    conn: sqlite3.Connection,
    fields: list[str],
    values: dict[str, Any],
    *,
    version_id: int | None = None,
) -> list[writes.ProfilePreview]:
    """Save a copy of the current profile, then make one Upwork PREVIEW per field (one user click
    each). Never confirms. Needs the context from the route (`issue_click`, `settings`, `config`,
    `reader`, `writer`); raises when a part is missing or when no preview worked."""
    ctx = approvals.context()
    try:
        issue_click, settings, config = ctx["issue_click"], ctx["settings"], ctx["config"]
        make_reader, make_writer = ctx["reader"], ctx["writer"]
    except KeyError as e:
        raise ProfileVersionError("A profile change can only be approved from the web page.") from e
    reader = make_reader()
    try:
        before = snapshot(reader, conn, settings, config, reason="before_change")
    except Exception as e:  # no copy of the current profile = no preview (the user can retry)
        raise ProfileVersionError(f"Could not save a copy of the current profile first: {e}") from e
    finally:
        getattr(reader, "close", lambda: None)()
    writer = make_writer()
    results = []
    for f in fields:
        res = writes.preview_profile_update(
            issue_click(),
            conn,
            settings,
            writer,
            f,
            values[f],
            approval_id=int(row["id"]),
            version_id=version_id,
        )
        conn.execute(
            "UPDATE profile_previews SET before_version_id = ? WHERE id = ?",
            (before.version_id, res.row_id),
        )
        results.append(res)
    if not any(r.status == "previewed" for r in results):
        raise ProfileVersionError(results[0].message or "No preview could be made.")
    return results


def apply_rollback(row: sqlite3.Row, decision: str, conn: sqlite3.Connection) -> None:
    """Approvals handler. Reject: nothing. Approve: save a copy of the current profile, then make
    one Upwork PREVIEW per changed field. Never confirms (the confirm is a separate click on the
    history page, see `profile_loop.confirm`). When no preview worked the item stays pending."""
    if decision != "approved":
        return
    try:
        before, after = json.loads(row["before_json"]), json.loads(row["after_json"])
    except (TypeError, ValueError) as e:
        raise ProfileVersionError("The roll back item has no data.") from e
    fields = [f for f in RESTORABLE if after.get(f) and after.get(f) != before.get(f)]
    if not fields:
        raise ProfileVersionError("Nothing to restore.")
    version_id = int(row["ref_id"]) if str(row["ref_id"]).isdigit() else None
    preview_item(row, conn, fields, after, version_id=version_id)


approvals.register(KIND, apply_rollback)


def previews_for_page(
    conn: sqlite3.Connection, limit: int = 20, settings: Settings | None = None
) -> list[dict[str, Any]]:
    """Profile previews, newest first. `confirmable` = a good preview that is not confirmed yet and
    (when `settings` is given) not too old."""
    rows = conn.execute(
        "SELECT p.*, a.title AS item_title, a.kind AS item_kind FROM profile_previews p "
        "LEFT JOIN approvals a ON a.id = p.approval_id ORDER BY p.id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    out = []
    for r in rows:
        open_ = r["status"] == "previewed" and r["confirmed_at"] is None
        out.append(
            {
                "id": r["id"],
                "label": LABELS.get(r["field"], r["field"]),
                "status": r["status"],
                "message": r["message"] or "",
                "created_at": r["created_at"],
                "item_title": r["item_title"] or "",
                "item_kind": r["item_kind"] or "",
                "preview_id": r["preview_id"] or "",
                "confirm_status": r["confirm_status"] or "",
                "confirmable": open_ and (settings is None or not writes.is_expired(r, settings)),
            }
        )
    return out
