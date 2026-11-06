"""L4: weekly consolidation. Fake claude only. Reading the week, the checks on the answer (cap,
verbatim job text, apply-once), skill patches as approval items, DREAMS.md, commits, the noise gate,
the task, the scheduler entry and the Run button."""

import json
import shutil
import subprocess
from datetime import UTC, datetime, timedelta

import pytest

from app import approvals, consolidation, memory, noise_gate, skills
from app.db import init_db
from app.settings import Settings
from app.tasks import lock, scheduler, trigger
from tests.test_auth import csrf_from, setup_admin
from tests.test_auth import make_client as make_web_client

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")

NOW = datetime(2026, 11, 9, 6, 0, tzinfo=UTC)  # a Monday
EM = "—"
CLIENT_TEXT = (
    "we need an expert who can migrate our whole legacy CRM into the new platform within "
    "three weeks and keep every single customer record intact during the move"
)  # 26 words
SKILL_OLD = "7. In the fixed version change only what is needed."


def words(text, n):
    return " ".join(text.split()[:n])


@pytest.fixture
def conn(config):
    c = init_db(config.db_path)
    yield c
    c.close()


@pytest.fixture
def settings(conn):
    return Settings(conn)


@pytest.fixture
def week(config, conn, settings):
    """A week with something in every list. Notes, one click, one diff, one outcome."""
    skills.ensure_skills(config, settings)
    memory.atomic_write(memory.memory_file(config, "USER.md"), "Freelancer. n8n.\n")
    with noise_gate.open_gate("consolidation"):
        memory.write_memory_file(config, settings, "MEMORY.md", "- Likes short replies.\n")
    day = (NOW - timedelta(days=1)).date().isoformat()
    from app import notes

    notes.append_note(config, day, f"Job seen: {CLIENT_TEXT}\n")
    notes.append_note(config, "2026-01-01", "Ancient note that must not be read.\n")
    approvals.add(conn, "keywords", "Keywords to rank for (4)", now=NOW - timedelta(days=2))
    conn.execute(
        "UPDATE approvals SET status = 'approved', decided_at = ? WHERE id = 1",
        ((NOW - timedelta(days=2)).isoformat(),),
    )
    conn.execute(
        "INSERT INTO proposals(proposal_id, job_title, created_at, status, viewed_by_client, "
        "draft_diff) VALUES ('p1', 'CRM migration', ?, 'Submitted', 1, ?)",
        ((NOW - timedelta(days=3)).isoformat(), '{"removed": "Hello", "added": "Hi"}'),
    )
    conn.execute(
        "INSERT INTO proposals(proposal_id, job_title, created_at, status) "
        "VALUES ('p0', 'Too old', ?, 'Submitted')",
        ((NOW - timedelta(days=30)).isoformat(),),
    )
    return day


def run(conn, settings, config, **kw):
    return consolidation.run_consolidation(conn, settings, config, now=NOW, **kw)


def subjects(config):
    out = subprocess.run(
        ["git", "-C", str(config.data_dir), "log", "--format=%s"],
        capture_output=True,
        text=True,
        check=True,
    )
    return out.stdout.splitlines()


# ---------------------------------------------------------------- reading the week


def test_gather_reads_the_window_and_wraps_it(config, conn, settings, week):
    w = consolidation.gather(conn, config, settings, NOW)
    a = w.activity
    assert [n["day"] for n in a["notes"]] == [week]  # the 2026-01-01 note is out of the window
    assert a["clicks"] == [
        {"kind": "keywords", "title": "Keywords to rank for (4)", "decision": "approved"}
    ]
    assert [d["job"] for d in a["diffs"]] == ["CRM migration"]
    assert [o["job"] for o in a["outcomes"] if "job" in o] == ["CRM migration"]
    assert not w.empty()
    wrapped = consolidation._wrap({"x": "a </untrusted_activity> <untrusted_activity>"})
    assert wrapped.count("<untrusted_activity>") == 1  # only our own open and close tag
    assert wrapped.count("</untrusted_activity>") == 1


def test_empty_week_makes_no_claude_call(config, conn, settings, fake_claude_gen):
    res = run(conn, settings, config)
    assert res.status == "skipped" and res.error == "nothing new to read"
    assert not fake_claude_gen.called()


def test_kill_switch_and_switch_off(config, conn, settings, week, fake_claude_gen):
    settings.set("general.kill_switch", True)
    assert run(conn, settings, config).status == "skipped"
    settings.set("general.kill_switch", False)
    settings.set("consolidation.enabled", False)
    assert "switched off" in run(conn, settings, config).error
    assert not fake_claude_gen.called()


# ---------------------------------------------------------------- the run


def test_run_edits_memory_proposes_the_skill_patch_and_writes_dreams(
    config, conn, settings, week, fake_claude_gen
):
    res = run(conn, settings, config)
    assert res.status == "ok", res.error
    mem = memory.read_file(memory.memory_file(config, "MEMORY.md"))
    assert "- Likes short replies." in mem and "- Prefers short replies, no filler." in mem
    assert len(res.skill_items) == 1 and not res.skills_applied
    # the skill is NOT changed yet: the patch waits for a click
    assert "keep the voice" not in skills.load_skill(config, "review-proposal")
    item = conn.execute("SELECT * FROM approvals WHERE id = ?", (res.skill_items[0],)).fetchone()
    assert item["kind"] == "skill_patch" and item["status"] == "pending"
    assert item["ref_id"] == "review-proposal"
    assert "Skill patch: review-proposal" in item["title"]
    dreams = memory.read_file(consolidation.dreams_path(config))
    assert dreams.startswith("# Dreams") and "## 2026-11-09" in dreams
    assert "Skill patch waiting for your approval: review-proposal" in dreams
    assert subjects(config)[:2] == [
        "Weekly consolidation: summary 2026-11-09",
        "Weekly consolidation: 1 memory edit(s) 2026-11-09",
    ]
    row = conn.execute("SELECT * FROM runs WHERE run_id = ?", (res.run_id,)).fetchone()
    assert row["task"] == "consolidate" and row["status"] == "ok"
    assert noise_gate.holder() is None  # the gate is closed again


def test_run_uses_the_think_model_with_untrusted_tags(
    config, conn, settings, week, fake_claude_gen
):
    run(conn, settings, config)
    log = fake_claude_gen.log()
    assert log["model"] == settings.get("models.think")
    assert log["effort"] == settings.get("models.think_effort")
    assert "<untrusted_activity>" in log["input"]["activity"]
    assert CLIENT_TEXT in log["input"]["activity"]  # job text only inside the tags
    assert CLIENT_TEXT not in json.dumps(log["input"]["memory"])
    assert "Likes short replies" in log["input"]["memory"]
    assert {s["name"] for s in log["input"]["skills"]} >= {"review-proposal"}
    call = conn.execute("SELECT model, task FROM claude_calls").fetchone()
    assert call["task"] == "consolidate"


def test_second_run_adds_a_section_and_dreams_keep_is_respected(
    config, conn, settings, week, fake_claude_gen
):
    settings.set("consolidation.dreams_keep", 2)
    for i in range(3):
        consolidation.run_consolidation(conn, settings, config, now=NOW + timedelta(days=i))
    dreams = memory.read_file(consolidation.dreams_path(config))
    assert dreams.count("\n## ") == 2
    assert "## 2026-11-11" in dreams and "## 2026-11-10" in dreams and "## 2026-11-09" not in dreams


def test_approving_the_skill_patch_applies_it_with_a_commit(
    config, conn, settings, week, fake_claude_gen
):
    res = run(conn, settings, config)
    approvals.decide(conn, res.skill_items[0], "approved", NOW, {"config": config})
    assert "keep the voice" in skills.load_skill(config, "review-proposal")
    assert subjects(config)[0] == "Skill patch approved: review-proposal"
    assert noise_gate.holder() is None


def test_rejecting_the_skill_patch_changes_nothing(config, conn, settings, week, fake_claude_gen):
    res = run(conn, settings, config)
    before = skills.load_skill(config, "review-proposal")
    approvals.decide(conn, res.skill_items[0], "rejected", NOW, {"config": config})
    assert skills.load_skill(config, "review-proposal") == before


def test_stale_patch_stays_pending(config, conn, settings, week, fake_claude_gen):
    res = run(conn, settings, config)
    path = config.data_dir / "skills" / "review-proposal" / "SKILL.md"
    path.write_text(path.read_text().replace(SKILL_OLD, "7. Changed by hand."))
    with pytest.raises(approvals.ApprovalError, match="could not be applied"):
        approvals.decide(conn, res.skill_items[0], "approved", NOW, {"config": config})
    status = conn.execute("SELECT status FROM approvals WHERE id = ?", (res.skill_items[0],))
    assert status.fetchone()["status"] == "pending"


def test_patches_apply_directly_when_settings_say_so(config, conn, settings, week, fake_claude_gen):
    settings.set("consolidation.skill_patches_need_approval", False)
    res = run(conn, settings, config)
    assert res.skill_items == [] and len(res.skills_applied) == 1
    assert "keep the voice" in skills.load_skill(config, "review-proposal")
    assert any(
        s.startswith("Weekly consolidation: skill review-proposal") for s in subjects(config)
    )


def test_nothing_worth_keeping_is_a_good_answer(config, conn, settings, week, fake_claude_gen):
    fake_claude_gen.env("FAKE_CONS_NOEDIT", "1")
    before = memory.read_file(memory.memory_file(config, "MEMORY.md"))
    res = run(conn, settings, config)
    assert res.status == "ok" and not res.memory_applied and not res.skill_items
    assert memory.read_file(memory.memory_file(config, "MEMORY.md")) == before
    assert "Nothing new." in memory.read_file(consolidation.dreams_path(config))


# ---------------------------------------------------------------- the checks on the answer


def test_job_text_over_the_limit_is_refused_in_memory_and_skills(
    config, conn, settings, week, fake_claude_gen
):
    fake_claude_gen.env("FAKE_CONS_QUOTE", CLIENT_TEXT)  # 26 words in a row
    before = memory.read_file(memory.memory_file(config, "MEMORY.md"))
    res = run(conn, settings, config)
    assert res.status == "ok"
    assert memory.read_file(memory.memory_file(config, "MEMORY.md")) == before
    assert res.skill_items == [] and len(res.rejected) == 2
    assert all("repeats job or client text" in r for r in res.rejected)
    assert "Refused: edit 1: repeats job or client text" in memory.read_file(
        consolidation.dreams_path(config)
    )
    assert skills.load_skill(config, "review-proposal").count("job or client") == 0


def test_twenty_words_of_job_text_are_allowed_twenty_one_are_not(
    config, conn, settings, week, fake_claude_gen
):
    fake_claude_gen.env("FAKE_CONS_QUOTE", words(CLIENT_TEXT, 20))
    res = run(conn, settings, config)
    assert res.rejected == [] and len(res.skill_items) == 1
    fake_claude_gen.env("FAKE_CONS_QUOTE", words(CLIENT_TEXT, 21))
    res = run(conn, settings, config)
    assert len(res.rejected) == 2


def test_word_limit_is_a_setting(config, conn, settings, week, fake_claude_gen):
    settings.set("consolidation.verbatim_max_words", 5)
    fake_claude_gen.env("FAKE_CONS_QUOTE", words(CLIENT_TEXT, 8))
    assert len(run(conn, settings, config).rejected) == 2


def test_quote_check_ignores_case_and_punctuation():
    grams = consolidation._grams(["Hello, World! one two three four"], 3)
    assert consolidation.quotes_untrusted("x hello world ONE two", grams, 2)
    assert not consolidation.quotes_untrusted("hello world, one", grams, 3)
    assert not consolidation.quotes_untrusted("", grams, 2)


def test_edit_over_the_cap_is_skipped(config, conn, settings, week, fake_claude_gen):
    fake_claude_gen.env("FAKE_CONS_BIGMEM", "1")
    before = memory.read_file(memory.memory_file(config, "MEMORY.md"))
    res = run(conn, settings, config)
    assert memory.read_file(memory.memory_file(config, "MEMORY.md")) == before
    assert any("over the 3500 character cap" in r for r in res.rejected)
    assert memory.usage(config, settings)["total"] <= 3500


def test_when_over_the_cap_only_shrinking_edits_pass(config, conn, settings, week, fake_claude_gen):
    memory.atomic_write(memory.memory_file(config, "USER.md"), "u" * 3490 + "\n")
    with noise_gate.open_gate("consolidation"):  # MEMORY.md was written when it fitted
        memory.atomic_write(
            memory.memory_file(config, "MEMORY.md"), "- Likes short replies.\n- Old line.\n"
        )
    assert memory.usage(config, settings)["over"]
    fake_claude_gen.env("FAKE_CONS_SHRINK", "1")  # adds a line, then removes the first line
    res = run(conn, settings, config)
    mem = memory.read_file(memory.memory_file(config, "MEMORY.md"))
    assert "Prefers short" not in mem  # the growing edit was skipped
    assert "Likes short replies" not in mem and "Old line" in mem  # the removal went through
    assert len(res.memory_applied) == 1 and len(res.rejected) == 1


def test_dashes_are_cleaned(config, conn, settings, week, fake_claude_gen):
    fake_claude_gen.env("FAKE_CONS_EM", "1")
    res = run(conn, settings, config)
    assert EM not in memory.read_file(memory.memory_file(config, "MEMORY.md"))
    item = conn.execute("SELECT after_json FROM approvals WHERE id = ?", (res.skill_items[0],))
    assert EM not in item.fetchone()["after_json"]
    assert EM not in memory.read_file(consolidation.dreams_path(config))


def test_a_patch_that_does_not_apply_is_refused(config, conn, settings, week, fake_claude_gen):
    fake_claude_gen.env("FAKE_CONS_BADOLD", "1")
    res = run(conn, settings, config)
    assert res.skill_items == [] and "exactly once" in res.rejected[0]


def test_plan_memory_rules(settings):
    grams = set()
    plan = consolidation.plan_memory(
        "- a\n- b\n",
        [
            {"old": "- a", "new": "- A", "why": "case"},
            {"old": "zzz", "new": "q"},  # not found
            {"old": "-", "new": "+"},  # more than once
            {"old": "", "new": ""},  # empty
            "not a dict",
            {"old": 1, "new": "x"},
            {"old": "", "new": "- c"},
        ],
        0,
        3500,
        grams,
        settings,
    )
    assert plan.text == "- A\n- b\n- c"
    assert len(plan.applied) == 2 and len(plan.rejected) == 5
    assert consolidation.plan_memory("x", "nope", 0, 3500, grams, settings).text == "x"
    settings.set("consolidation.max_memory_edits", 1)
    two = [{"old": "", "new": "1"}, {"old": "", "new": "2"}]
    assert consolidation.plan_memory("", two, 0, 3500, grams, settings).text == "1"


def test_skill_patch_limits_and_shapes(config, settings):
    skills.ensure_skills(config, settings)
    settings.set("consolidation.max_skill_patches", 5)
    good, bad = consolidation.plan_skills(
        config,
        [
            {"skill": "review-proposal", "old": SKILL_OLD, "new": "7. Short.", "why": "w"},
            {"skill": "Bad Name", "old": "", "new": "x"},
            {"skill": "ghost", "old": "a", "new": "b"},
            {"nope": 1},
        ],
        set(),
        settings,
    )
    assert [g.skill for g in good] == ["review-proposal"] and len(bad) == 3
    settings.set("consolidation.max_skill_patches", 1)  # the rest of the list is dropped
    good, bad = consolidation.plan_skills(
        config, [{"skill": "review-proposal", "old": SKILL_OLD, "new": "7."}] * 3, set(), settings
    )
    assert len(good) == 1 and bad == []
    assert consolidation.plan_skills(config, "x", set(), settings) == ([], [])


# ---------------------------------------------------------------- failures


def test_unusable_answer_writes_nothing(config, conn, settings, week, fake_claude_gen):
    fake_claude_gen.env("FAKE_CONS_BAD", "1")
    res = run(conn, settings, config)
    assert res.status == "error" and "no usable answer" in res.error
    assert not consolidation.dreams_path(config).exists()
    assert conn.execute("SELECT status FROM runs").fetchone()["status"] == "error"


def test_claude_failure_and_rate_limit_are_errors(config, conn, settings, week, fake_claude_gen):
    fake_claude_gen.env("FAKE_GEN_FAIL", "1")
    assert run(conn, settings, config).status == "error"
    fake_claude_gen.env("FAKE_GEN_FAIL", "")
    fake_claude_gen.env("FAKE_GEN_LIMIT_AFTER", "0")
    res = run(conn, settings, config)
    assert res.status == "error" and "rate limited" in res.error


def test_monthly_cap_stops_the_call(config, conn, settings, week, fake_claude_gen):
    settings.set("claude.monthly_cap_usd", 0.01)
    conn.execute(
        "INSERT INTO claude_calls(run_id, model, input_tokens, output_tokens, cost_usd, "
        "created_at, task) VALUES ('r', 'm', 1, 1, 5, ?, 'x')",
        (datetime.now(UTC).isoformat(),),
    )
    res = consolidation.run_consolidation(conn, settings, config, now=datetime.now(UTC))
    assert res.status == "error" and "cap" in res.error
    assert not fake_claude_gen.called()


# ---------------------------------------------------------------- the noise gate


def test_only_the_job_or_an_approval_writes_long_term_memory(config, conn, settings):
    with pytest.raises(noise_gate.NoiseGateError):
        memory.write_memory_file(config, settings, "MEMORY.md", "from a daily run\n")
    assert not memory.memory_file(config, "MEMORY.md").exists()
    with pytest.raises(noise_gate.NoiseGateError):
        skills.write_skill(config, settings, "x-y", "---\nsummary: s\n---\n")


# ---------------------------------------------------------------- task, scheduler, page


def test_task_main_uses_the_task_lock(config, monkeypatch):
    from app.tasks import consolidate as task

    monkeypatch.setenv("SECRET_KEY", config.secret_key)
    monkeypatch.setenv("DATA_DIR", str(config.data_dir))
    fd = lock.acquire(config.data_dir, "consolidate")
    try:
        assert task.main() == 0
    finally:
        import os

        os.close(fd)


def test_task_main_runs_end_to_end(config, monkeypatch, fake_claude_gen):
    from app.tasks import consolidate as task

    monkeypatch.setenv("SECRET_KEY", config.secret_key)
    monkeypatch.setenv("DATA_DIR", str(config.data_dir))
    assert task.main() == 0  # empty data: skipped, not an error


def test_scheduler_starts_it_weekly(config, conn, settings, monkeypatch):
    started = []
    monkeypatch.setattr(trigger, "start", lambda cfg, name: started.append(name))
    monkeypatch.setattr(trigger, "is_built", lambda name: name in ("consolidate", "research"))
    scheduler.tick(settings, config, datetime(2026, 11, 9, 6, 30, tzinfo=UTC))
    assert sorted(started) == ["consolidate", "research"]
    started.clear()
    scheduler.tick(settings, config, datetime(2026, 11, 9, 6, 35, tzinfo=UTC))
    assert started == []  # the same slot is not started twice
    assert "consolidate" in trigger.MANUAL_TASKS and trigger.is_built("consolidate")


@pytest.fixture
def web(config, fake_claude_gen):
    app, client = make_web_client(config)
    setup_admin(client)
    return app, client


def post(client, path):
    return client.post(path, data={"csrf_token": csrf_from(client.get("/learning").text)})


def test_learning_page_shows_jobs_dreams_and_skills(web, config, week, conn, fake_claude_gen):
    app, client = web
    res = consolidation.run_consolidation(app.state.db, app.state.settings, config, now=NOW)
    assert res.status in ("ok", "skipped")
    html = client.get("/learning").text
    assert "Weekly jobs" in html and "/learning/run/consolidate" in html
    assert "/learning/run/research" in html
    assert "review-proposal" in html and "Dreams" in html
    assert "Nothing yet." in html or "learning-dreams" in html


def test_run_button(web, monkeypatch):
    app, client = web
    started = []
    monkeypatch.setattr(trigger, "start", lambda cfg, name: started.append(name))
    assert client.post("/learning/run/consolidate").status_code == 403  # no CSRF token
    r = post(client, "/learning/run/consolidate")
    assert r.status_code == 303 and r.headers["location"] == "/learning?done=consolidate_started"
    assert started == ["consolidate"]
    assert "consolidation started" in client.get("/learning?done=consolidate_started").text
    assert post(client, "/learning/run/other").status_code == 404

    def busy(cfg, name):
        raise lock.TaskBusy(name)

    monkeypatch.setattr(trigger, "start", busy)
    assert post(client, "/learning/run/consolidate").headers["location"].endswith("_busy")
    app.state.settings.set("general.kill_switch", True)
    assert post(client, "/learning/run/consolidate").headers["location"].endswith("_killed")
    app.state.settings.set("general.kill_switch", False)
    app.state.settings.set("consolidation.enabled", False)
    assert post(client, "/learning/run/consolidate").headers["location"].endswith("_off")


def test_skill_patch_item_shows_on_approvals_and_approves(web, config, week, fake_claude_gen):
    app, client = web
    res = consolidation.run_consolidation(
        app.state.db, app.state.settings, config, now=datetime.now(UTC)
    )
    html = client.get("/approvals").text
    assert "Skill patch: review-proposal" in html and "skill_patch" in html
    token = csrf_from(html)
    r = client.post(f"/approvals/{res.skill_items[0]}/approve", data={"csrf_token": token})
    assert r.status_code == 303
    assert "keep the voice" in skills.load_skill(config, "review-proposal")
