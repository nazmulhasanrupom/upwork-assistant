"""Skills (SPEC2 5.1 layer 3, plan L3): how-to files the assistant can follow.

`<DATA_DIR>/skills/<name>/SKILL.md` starts with a small header:

    ---
    name: review-proposal
    summary: One line that says what the skill is for.
    ---

Prompts get only the names and one-line summaries (`summaries_text`). The full text is loaded by
Python when the task matches (`task_extra`, using the `skills.task_map` setting). The bundled
starter skills in the repo `skills/` folder are copied on first run; a skill that already exists
in the data folder is never overwritten, so the owner's edits stay.

Writes (`write_skill`, `apply_patch`) pass the noise gate (only the weekly consolidation or an
approved item may write) and end in one version-history commit (app/versioning.py).
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

from app import noise_gate, versioning
from app.config import Config
from app.memory import atomic_write

log = logging.getLogger("skills")

BUNDLED_DIR = Path(__file__).resolve().parent.parent / "skills"
FILE_NAME = "SKILL.md"
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_HEADER_RE = re.compile(r"\A---\n(.*?)\n---\n?", re.S)


class SkillError(ValueError):
    """A message that is safe to show to the user."""


def skills_dir(config: Config) -> Path:
    return config.data_dir / "skills"


def valid_name(name: Any, settings: Any = None) -> bool:
    limit = int(settings.get("skills.name_max_chars")) if settings is not None else 40
    return isinstance(name, str) and len(name) <= limit and bool(_NAME_RE.match(name))


def skill_path(config: Config, name: str) -> Path:
    if not valid_name(name):
        raise SkillError("A skill name uses lower case letters, digits and hyphens only.")
    return skills_dir(config) / name / FILE_NAME


def parse(text: str) -> dict[str, str]:
    """The header fields (`name`, `summary`) of a SKILL.md. Missing fields are ''."""
    out = {"name": "", "summary": ""}
    m = _HEADER_RE.match(text.replace("\r\n", "\n"))
    if m:
        for line in m.group(1).splitlines():
            key, sep, value = line.partition(":")
            if sep and key.strip() in out:
                out[key.strip()] = value.strip()
    return out


def body(text: str) -> str:
    """The text after the header."""
    return _HEADER_RE.sub("", text.replace("\r\n", "\n"), count=1).strip()


def ensure_skills(config: Config, settings: Any = None, source: Path | None = None) -> list[str]:
    """Copy bundled skills that are missing in the data folder. Returns the names copied.
    Never overwrites a skill folder that exists. One commit for the whole copy."""
    src = source or BUNDLED_DIR
    copied: list[str] = []
    if not src.is_dir():
        return copied
    for folder in sorted(p for p in src.iterdir() if (p / FILE_NAME).is_file()):
        target = skills_dir(config) / folder.name / FILE_NAME
        if target.exists() or not valid_name(folder.name):
            continue
        atomic_write(target, (folder / FILE_NAME).read_text(encoding="utf-8"))
        copied.append(folder.name)
    if copied:
        versioning.commit(
            config,
            settings,
            "Add starter skills: " + ", ".join(copied),
            [skills_dir(config)],
        )
    return copied


def list_skills(config: Config, settings: Any = None) -> list[dict[str, str]]:
    """Name and one-line summary of every skill, by name."""
    cut = int(settings.get("skills.summary_max_chars")) if settings is not None else 160
    out = []
    root = skills_dir(config)
    if not root.is_dir():
        return out
    for folder in sorted(root.iterdir()):
        path = folder / FILE_NAME
        if not path.is_file() or not valid_name(folder.name):
            continue
        try:
            head = parse(path.read_text(encoding="utf-8"))
        except OSError:
            continue
        summary = " ".join(head["summary"].split())[:cut]
        out.append({"name": folder.name, "summary": summary})
    return out


def summaries_text(config: Config, settings: Any = None) -> str:
    """The names + one-line summaries block for prompts. '' when there are no skills."""
    rows = list_skills(config, settings)
    if not rows:
        return ""
    lines = [f"- {r['name']}: {r['summary']}" for r in rows]
    return "# Skills available (names only; the full text is added when the task needs it)\n\n" + (
        "\n".join(lines)
    )


def load_skill(config: Config, name: str) -> str:
    """Full text of one skill, or '' when it does not exist."""
    try:
        return skill_path(config, name).read_text(encoding="utf-8")
    except (OSError, SkillError):
        return ""


def task_map(settings: Any) -> dict[str, str]:
    out = {}
    for entry in settings.get("skills.task_map") or []:
        task, sep, name = str(entry).partition("=")
        if sep and task.strip() and name.strip():
            out[task.strip()] = name.strip()
    return out


def task_extra(config: Config, settings: Any, task: str) -> str:
    """The prompt `extra` for a task: all skill summaries, then the full text of the skill that
    matches the task (if the map names one and it exists). '' when there is nothing."""
    parts = [summaries_text(config, settings)]
    name = task_map(settings).get(task)
    full = load_skill(config, name) if name else ""
    if full.strip():
        parts.append(f"# Skill for this task: {name}\n\n{body(full)}")
    return "\n\n".join(p for p in parts if p)


# ---------------------------------------------------------------- writes (noise gate)


def check_text(text: str, settings: Any) -> str:
    """The text of a skill, checked: a header with the same fields, not too long. Returns it
    normalized. Raises SkillError."""
    text = text.replace("\r\n", "\n").strip() + "\n"
    if len(text) > int(settings.get("skills.max_chars")):
        raise SkillError(f"The skill is longer than {settings.get('skills.max_chars')} characters.")
    if not parse(text)["summary"]:
        raise SkillError("The skill needs a header with a one-line summary.")
    return text


def write_skill(
    config: Config, settings: Any, name: str, text: str, message: str | None = None
) -> str | None:
    """Write one SKILL.md. Needs the noise gate open. Returns the commit sha (None: no change)."""
    noise_gate.require("A skill")
    path = skill_path(config, name)
    if not valid_name(name, settings):
        raise SkillError("That skill name is too long.")
    atomic_write(path, check_text(text, settings))
    return versioning.commit(config, settings, message or f"Update skill {name}", [path.parent])


def patched_text(config: Config, settings: Any, name: str, old: str, new: str) -> str:
    """The text a patch would give (nothing is written). `old` must appear exactly once and
    becomes `new`. An empty `old` makes a skill that does not exist yet, `new` being its whole text.
    Raises SkillError when the patch does not apply or the result is not a valid skill."""
    if not valid_name(name, settings):
        raise SkillError("A skill name uses lower case letters, digits and hyphens only.")
    current = load_skill(config, name)
    if not old:
        if current:
            raise SkillError(f"Skill {name} exists: a patch needs the lines it replaces.")
        return check_text(new, settings)
    if not current:
        raise SkillError(f"Skill {name} does not exist.")
    if current.count(old) != 1:
        raise SkillError(f"The lines to replace were not found exactly once in skill {name}.")
    return check_text(current.replace(old, new, 1), settings)


def apply_patch(
    config: Config,
    settings: Any,
    name: str,
    old: str,
    new: str,
    message: str | None = None,
) -> str | None:
    """Apply a small patch (see `patched_text`) and commit it. Needs the noise gate open."""
    noise_gate.require("A skill")
    text = patched_text(config, settings, name, old, new)
    default = f"Patch skill {name}" if old else f"Add skill {name}"
    return write_skill(config, settings, name, text, message or default)
