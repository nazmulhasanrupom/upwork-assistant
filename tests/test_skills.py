"""L3: skills in <DATA_DIR>/skills, bundled starters copied once, summaries only in prompts, the
full text loaded for a matching task, and writes through the noise gate + versioning. Fakes only."""

import shutil
import subprocess

import pytest

from app import memory, noise_gate, skills, versioning
from app.db import init_db
from app.llm import prompting
from app.main import create_app
from app.settings import Settings

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")

BUNDLED = ("check-keyword-rank", "draft-profile-change", "review-proposal")


@pytest.fixture
def conn(config):
    return init_db(config.db_path)


@pytest.fixture
def settings(conn):
    return Settings(conn)


def git_subjects(config):
    out = subprocess.run(
        ["git", "-C", str(config.data_dir), "log", "--format=%s"],
        capture_output=True,
        text=True,
        check=True,
    )
    return out.stdout.splitlines()


def test_bundled_skills_are_generic_and_valid():
    names = sorted(p.name for p in skills.BUNDLED_DIR.iterdir() if p.is_dir())
    assert names == list(BUNDLED)
    for name in names:
        text = (skills.BUNDLED_DIR / name / "SKILL.md").read_text()
        head = skills.parse(text)
        assert head["name"] == name and head["summary"]
        assert skills.valid_name(name)
        assert "@" not in text and "http" not in text  # nothing personal, no links
        assert len(text) <= 3000


def test_first_run_copies_and_commits_once(config, settings):
    versioning.ensure_repo(config, settings)
    assert skills.ensure_skills(config, settings) == list(BUNDLED)
    for name in BUNDLED:
        assert (config.data_dir / "skills" / name / "SKILL.md").is_file()
    assert git_subjects(config)[0].startswith("Add starter skills")
    n = len(git_subjects(config))
    assert skills.ensure_skills(config, settings) == []  # second start: nothing
    assert len(git_subjects(config)) == n


def test_user_edited_skill_is_not_overwritten(config, settings):
    path = config.data_dir / "skills" / "review-proposal" / "SKILL.md"
    path.parent.mkdir(parents=True)
    path.write_text("---\nname: review-proposal\nsummary: Mine.\n---\nmy own steps\n")
    copied = skills.ensure_skills(config, settings)
    assert "review-proposal" not in copied and len(copied) == 2
    assert "my own steps" in path.read_text()


def test_app_start_copies_the_starter_skills(config):
    create_app(config)
    assert (config.data_dir / "skills" / "check-keyword-rank" / "SKILL.md").is_file()


def test_parse_header():
    text = "---\nname: a-b\nsummary: Does a thing.\nother: x\n---\n# Body\nstep\n"
    assert skills.parse(text) == {"name": "a-b", "summary": "Does a thing."}
    assert skills.body(text) == "# Body\nstep"
    assert skills.parse("no header") == {"name": "", "summary": ""}


def test_prompt_has_names_and_summaries_only(config, settings):
    skills.ensure_skills(config, settings)
    text = skills.summaries_text(config, settings)
    assert "- review-proposal: " in text and "- check-keyword-rank: " in text
    body = skills.body(skills.load_skill(config, "review-proposal"))
    assert body.splitlines()[2] not in text  # no step text
    assert "Read the job post first" not in text


def test_full_text_loads_when_the_task_matches(config, settings):
    skills.ensure_skills(config, settings)
    extra = skills.task_extra(config, settings, "review_proposal")
    assert "Skill for this task: review-proposal" in extra
    assert "Check each claim against the facts" in extra
    assert "draft-profile-change:" in extra  # summaries of the others are there too
    assert "Draft a profile change" not in extra  # but not their full text
    other = skills.task_extra(config, settings, "judge")  # no skill mapped
    assert "Skill for this task" not in other and "review-proposal:" in other
    prompt = prompting.build_prompt("prompts/review_proposal.md", extra, config=config)
    assert prompt.endswith(extra.strip()) and "Skills available" in prompt


def test_task_map_comes_from_settings(config, settings):
    skills.ensure_skills(config, settings)
    settings.set("skills.task_map", ["judge=check-keyword-rank", "broken", "x=", "=y"])
    assert skills.task_map(settings) == {"judge": "check-keyword-rank"}
    assert "Skill for this task: check-keyword-rank" in skills.task_extra(config, settings, "judge")


def test_writes_need_the_noise_gate(config, settings):
    skills.ensure_skills(config, settings)
    with pytest.raises(noise_gate.NoiseGateError):
        skills.write_skill(config, settings, "new-skill", "---\nsummary: x\n---\nstep\n")
    with pytest.raises(noise_gate.NoiseGateError):
        skills.apply_patch(config, settings, "review-proposal", "Read the job", "Read")
    assert not (config.data_dir / "skills" / "new-skill").exists()
    with pytest.raises(noise_gate.NoiseGateError):
        memory.write_memory_file(config, settings, "MEMORY.md", "fact\n")
    memory.write_memory_file(config, settings, "USER.md", "me\n")  # the owner's file is free


def test_gate_is_closed_again_after_the_block():
    with noise_gate.open_gate("approval"):
        assert noise_gate.holder() == "approval"
    assert noise_gate.holder() is None
    with pytest.raises(ValueError), noise_gate.open_gate("daily"):
        pass


def test_write_is_one_commit_with_a_message(config, settings, gate_open):
    skills.ensure_skills(config, settings)
    sha = skills.write_skill(
        config, settings, "new-skill", "---\nname: new-skill\nsummary: New.\n---\nstep\n"
    )
    assert sha and git_subjects(config)[0] == "Update skill new-skill"
    assert {"name": "new-skill", "summary": "New."} in skills.list_skills(config)


def test_patch_changes_only_the_matching_lines(config, settings, gate_open):
    skills.ensure_skills(config, settings)
    before = skills.load_skill(config, "review-proposal")
    skills.apply_patch(
        config,
        settings,
        "review-proposal",
        "7. In the fixed version change only what is needed. Keep the freelancer's voice.",
        "7. Change only what is needed.",
    )
    after = skills.load_skill(config, "review-proposal")
    assert "7. Change only what is needed." in after
    assert after.replace("7. Change only what is needed.", "") != before
    assert after.splitlines()[:8] == before.splitlines()[:8]
    assert git_subjects(config)[0] == "Patch skill review-proposal"


def test_bad_patches_are_refused(config, settings, gate_open):
    skills.ensure_skills(config, settings)
    with pytest.raises(skills.SkillError, match="exactly once"):
        skills.apply_patch(config, settings, "review-proposal", "not in the file", "x")
    with pytest.raises(skills.SkillError, match="exactly once"):
        skills.apply_patch(config, settings, "review-proposal", "the", "x")  # appears many times
    with pytest.raises(skills.SkillError, match="does not exist"):
        skills.apply_patch(config, settings, "nope", "a", "b")
    with pytest.raises(skills.SkillError, match="exists"):
        skills.apply_patch(config, settings, "review-proposal", "", "---\nsummary: x\n---\n")
    head = skills.load_skill(config, "review-proposal").splitlines()[2]
    with pytest.raises(skills.SkillError, match="summary"):
        skills.apply_patch(config, settings, "review-proposal", head, "")


def test_patch_with_empty_old_makes_a_new_skill(config, settings, gate_open):
    skills.apply_patch(
        config, settings, "fresh-one", "", "---\nname: fresh-one\nsummary: Fresh.\n---\ndo it\n"
    )
    assert "Fresh." in skills.load_skill(config, "fresh-one")
    assert git_subjects(config)[0] == "Add skill fresh-one"


def test_no_skills_gives_empty_summaries(config, settings):
    assert skills.summaries_text(config, settings) == ""
    assert skills.task_extra(config, settings, "review_proposal") == ""


def test_names_and_size_are_checked(config, settings, gate_open):
    for bad in ("../evil", "Upper", "a b", "", "x" * 41, "-lead"):
        with pytest.raises(skills.SkillError):
            skills.write_skill(config, settings, bad, "---\nsummary: x\n---\n")
    settings.set("skills.max_chars", 50)
    with pytest.raises(skills.SkillError, match="longer"):
        skills.write_skill(config, settings, "big", "---\nsummary: x\n---\n" + "y" * 100)
    assert skills.load_skill(config, "../evil") == ""
