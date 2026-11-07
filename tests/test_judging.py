"""Plan G2: judge in batches, write drafts for good fits, with a fake claude. No network."""

import json

import pytest

from app.db import init_db
from app.llm import judging, prompting
from app.settings import Settings
from app.tasks import daily
from tests.test_daily import make_client

ORG = "1000000000000000001"
EM, EN = "—", "–"
pytestmark = pytest.mark.judging


@pytest.fixture
def conn():
    return init_db(":memory:")


@pytest.fixture
def settings(conn):
    s = Settings(conn)
    s.set("upwork.talent_org_uid", ORG)
    return s


@pytest.fixture
def user_md(config):
    (config.data_dir / "memory").mkdir(exist_ok=True)
    (config.data_dir / "memory" / "USER.md").write_text("USERFACT built 12 n8n flows for a CRM.")


def go(conn, settings, config, balance=50):
    return daily.run_daily(conn, settings, config, make_client(settings, balance=balance))


def calls(fake, task=None):
    out = [c for c in fake.logs() if task is None or c["task"] == task]
    return out


def ids(conn, table="judgements"):
    return {r["job_id"] for r in conn.execute(f"SELECT job_id FROM {table}")}


def test_judge_then_write_in_batches(conn, settings, config, fake_claude_gen, user_md):
    settings.set("claude.max_jobs_per_think_call", 2)
    settings.set("claude.max_proposals_per_write_call", 3)
    res = go(conn, settings, config)
    assert res.status == "ok" and res.kept == 5

    judge, write = calls(fake_claude_gen, "judge"), calls(fake_claude_gen, "write")
    assert [len(c["job_ids"]) for c in judge] == [2, 2, 1]
    assert [len(c["job_ids"]) for c in write] == [3, 2]
    assert {j for c in judge for j in c["job_ids"]} == ids(conn)
    assert all(c["model"] == "claude-opus-5-5" and c["effort"] == "medium" for c in judge)
    assert all(c["model"] == "claude-sonnet-5-5" and c["effort"] == "high" for c in write)
    assert len(ids(conn)) == 5 and len(ids(conn, "drafts")) == 5
    assert conn.execute("SELECT COUNT(*) FROM jobs WHERE status='drafted'").fetchone()[0] == 5
    # costs were recorded for each call
    rows = conn.execute("SELECT task, COUNT(*) n FROM claude_calls GROUP BY task").fetchall()
    assert {r["task"]: r["n"] for r in rows if r["task"] in ("judge", "write")} == {
        "judge": 3,
        "write": 2,
    }


def test_prompts_and_inputs_sent_to_claude(conn, settings, config, fake_claude_gen, user_md):
    go(conn, settings, config)
    judge, write = calls(fake_claude_gen, "judge")[0], calls(fake_claude_gen, "write")[0]
    rules = prompting.hard_rules()
    for c in (judge, write):
        assert c["prompt"].startswith(rules)
        assert "USERFACT" in c["prompt"] and c["prompt"].index("USERFACT") > len(rules)
        assert "untrusted" in c["prompt"]
    assert "# Task: judge jobs" in judge["prompt"] and "# Task: write proposals" in write["prompt"]
    first = judge["input"]["jobs"][0]
    assert first["untrusted_job_post"].startswith("<untrusted_job_post>")
    assert judge["input"]["connects_balance"] == 50
    assert "plans" in write["input"] and write["input"]["plans"][0]["instructions_in_post"]
    # the stored cut-down job reaches Claude only inside the tags
    assert "title" not in first


def test_threshold_decides_who_gets_a_draft(conn, settings, config, fake_claude_gen, user_md):
    probe = go(conn, settings, config)  # learn the ids
    assert probe.kept == 5
    all_ids = sorted(ids(conn))
    conn.execute("DELETE FROM judgements")
    conn.execute("DELETE FROM drafts")
    conn.execute("DELETE FROM jobs")
    fits = {all_ids[0]: 64, all_ids[1]: 65, all_ids[2]: 90, all_ids[3]: 10, all_ids[4]: 66}
    fake_claude_gen.env("FAKE_GEN_FIT", json.dumps(fits))
    go(conn, settings, config)
    assert ids(conn, "drafts") == {all_ids[1], all_ids[2], all_ids[4]}
    status = {r["job_id"]: r["status"] for r in conn.execute("SELECT job_id, status FROM jobs")}
    assert status[all_ids[0]] == "judged" and status[all_ids[3]] == "judged"
    assert status[all_ids[2]] == "drafted"

    # a higher fit_min later does not create drafts for a job that is judged lower
    settings.set("pay.fit_min", 95)
    conn.execute("DELETE FROM drafts")
    conn.execute("UPDATE jobs SET status = 'judged'")
    judging.judge_and_draft(conn, [], settings=settings, config=config)
    assert ids(conn, "drafts") == set()


def test_never_judge_or_draft_twice(conn, settings, config, fake_claude_gen, user_md):
    go(conn, settings, config)
    n = len(fake_claude_gen.logs())
    assert n == 3  # one judge call (5 jobs) + two write calls (3 + 2)
    go(conn, settings, config)  # nothing new
    judging.judge_and_draft(conn, [{"id": next(iter(ids(conn)))}], settings=settings, config=config)
    assert len(fake_claude_gen.logs()) == n
    assert conn.execute("SELECT COUNT(*) FROM judgements").fetchone()[0] == 5
    assert conn.execute("SELECT COUNT(*) FROM drafts").fetchone()[0] == 5
    # a stored judgement is not replaced by a later insert
    before = conn.execute("SELECT fit_score FROM judgements LIMIT 1").fetchone()[0]
    conn.execute("UPDATE jobs SET status = 'candidate'")
    conn.execute("DELETE FROM drafts")
    fake_claude_gen.env("FAKE_GEN_FIT", "5")
    judging.judge_and_draft(conn, [], settings=settings, config=config)
    assert conn.execute("SELECT fit_score FROM judgements LIMIT 1").fetchone()[0] == before


def test_missing_user_md_judges_but_does_not_write(conn, settings, config, fake_claude_gen):
    res = go(conn, settings, config)
    assert res.status == "ok"
    assert len(calls(fake_claude_gen, "judge")) == 1 and not calls(fake_claude_gen, "write")
    assert "No user facts yet" in calls(fake_claude_gen, "judge")[0]["prompt"]
    assert len(ids(conn)) == 5 and not ids(conn, "drafts")
    alert = conn.execute("SELECT message FROM alerts WHERE key = 'user_md_missing'").fetchone()
    assert alert["message"] == "Add your profile facts (USER.md) to get drafts"

    # the user adds USER.md: the next run writes the waiting drafts, judges nothing again
    (config.data_dir / "memory").mkdir(exist_ok=True)
    (config.data_dir / "memory" / "USER.md").write_text("Real facts.")
    go(conn, settings, config)
    assert len(calls(fake_claude_gen, "judge")) == 1
    assert len(ids(conn, "drafts")) == 5
    assert not conn.execute("SELECT 1 FROM alerts WHERE key = 'user_md_missing'").fetchone()


def test_blank_user_md_counts_as_missing(conn, settings, config, fake_claude_gen):
    (config.data_dir / "memory").mkdir(exist_ok=True)
    (config.data_dir / "memory" / "USER.md").write_text("  \n\n")
    go(conn, settings, config)
    assert not ids(conn, "drafts") and ids(conn)
    assert conn.execute("SELECT 1 FROM alerts WHERE key = 'user_md_missing'").fetchone()


def test_monthly_cap_stops_claude_but_keeps_python_results(
    conn, settings, config, fake_claude_gen, user_md
):
    settings.set("claude.monthly_cap_usd", 1.0)
    conn.execute(
        "INSERT INTO claude_calls(run_id, model, cost_usd, created_at, task) "
        "VALUES ('x', 'm', 5.0, datetime('now'), 'write')"
    )
    # created_at must be this month in UTC text form
    conn.execute("UPDATE claude_calls SET created_at = ?", (daily._iso(daily._now()),))
    res = go(conn, settings, config)
    assert res.status == "ok" and res.kept == 5  # the Python part is done
    assert not fake_claude_gen.called()  # no claude run at all
    assert conn.execute("SELECT COUNT(*) FROM jobs WHERE status='candidate'").fetchone()[0] == 5
    assert not ids(conn)
    note = next((config.data_dir / "memory" / "daily").glob("*.md")).read_text()
    assert "Claude steps stopped" in note and "cap" in note


def test_rate_limit_stops_the_rest_and_next_run_continues(
    conn, settings, config, fake_claude_gen, user_md
):
    settings.set("claude.max_jobs_per_think_call", 2)
    fake_claude_gen.env("FAKE_GEN_LIMIT_AFTER", 1)
    res = go(conn, settings, config)
    assert res.status == "ok"
    assert len(fake_claude_gen.logs()) == 2  # one good judge call, one 429, then it stops
    assert len(ids(conn)) == 2 and not ids(conn, "drafts")
    note = next((config.data_dir / "memory" / "daily").glob("*.md")).read_text()
    assert "rate limited" in note

    # next run: the limit is gone. Only the 3 unjudged jobs are judged; all 5 get drafts.
    fake_claude_gen.env("FAKE_GEN_LIMIT_AFTER", 999)
    go(conn, settings, config)
    judge = calls(fake_claude_gen, "judge")
    assert sum(len(c["job_ids"]) for c in judge[2:]) == 3  # 3 new, none twice
    assert len(ids(conn)) == 5 and len(ids(conn, "drafts")) == 5


def test_old_jobs_are_not_retried_forever(conn, settings, config, fake_claude_gen, user_md):
    settings.set("claude.monthly_cap_usd", 0.0)
    go(conn, settings, config)
    assert not fake_claude_gen.called()
    conn.execute("UPDATE jobs SET first_seen = '2020-01-01T00:00:00+00:00'")
    settings.set("claude.monthly_cap_usd", 50.0)
    go(conn, settings, config)
    assert not fake_claude_gen.called()  # older than claude.retry_hours


def test_failed_calls_stop_after_two_in_a_row(conn, settings, config, fake_claude_gen, user_md):
    settings.set("claude.max_jobs_per_think_call", 1)
    fake_claude_gen.env("FAKE_GEN_FAIL", 1)
    res = go(conn, settings, config)
    assert res.status == "ok"
    assert len(fake_claude_gen.logs()) == 2  # not five
    assert not ids(conn)


def test_em_dashes_are_removed_before_saving(conn, settings, config, fake_claude_gen, user_md):
    fake_claude_gen.env("FAKE_GEN_EM", 1)
    go(conn, settings, config)
    texts = []
    for r in conn.execute("SELECT * FROM judgements"):
        texts += [r["why_apply"], r["risks"], r["plan"]]
    for r in conn.execute("SELECT * FROM drafts"):
        texts += [r["cover_letter"], r["answers"], r["materials"], r["flags"]]
    assert texts
    for t in texts:
        assert EM not in t and EN not in t and "\\u2014" not in t and "\\u2013" not in t
    letter = conn.execute("SELECT cover_letter FROM drafts").fetchone()[0]
    assert letter == "Bots. Your CRM leaks leads, I fix that. Range 5-10."


def test_skipped_instruction_is_kept_but_flagged(conn, settings, config, fake_claude_gen, user_md):
    fake_claude_gen.env("FAKE_GEN_SKIP", 1)
    go(conn, settings, config)
    row = conn.execute("SELECT * FROM drafts").fetchone()
    assert row["cover_letter"]  # kept
    flags = json.loads(row["flags"])
    assert any(
        f.startswith("Skipped instruction: Start the proposal with the word Bots") for f in flags
    )


def test_unconfirmed_instruction_is_flagged_and_handled_one_is_not(
    conn, settings, config, fake_claude_gen, user_md
):
    go(conn, settings, config)
    assert all(json.loads(r["flags"]) == [] for r in conn.execute("SELECT flags FROM drafts"))
    conn.execute("DELETE FROM drafts")
    conn.execute("UPDATE jobs SET status = 'judged'")
    fake_claude_gen.env("FAKE_GEN_NOHANDLED", 1)
    judging.judge_and_draft(conn, [], settings=settings, config=config)
    flags = json.loads(conn.execute("SELECT flags FROM drafts").fetchone()["flags"])
    assert flags and flags[0].startswith("Not confirmed handled")


def test_check_instructions_unit():
    f = judging.check_instructions
    assert f(["Start with the word Bots"], [{"instruction": "start with the word bots"}], []) == []
    assert f(["Say your rate"], [], [{"instruction": "Say your rate", "reason": "unknown"}]) == [
        "Skipped instruction: Say your rate (unknown)"
    ]
    assert f(["Say your rate"], [], []) == ["Not confirmed handled: Say your rate"]
    assert f([], [], []) == []


def test_can_afford_is_computed_by_python(conn, settings, config, fake_claude_gen, user_md):
    go(conn, settings, config, balance=10)  # the fake claims can_afford = true for every job
    plans = [json.loads(r["plan"]) for r in conn.execute("SELECT plan FROM judgements")]
    assert len(plans) == 5
    for p in plans:
        assert p["can_afford"] == (p["connects_cost"] <= 10)
        assert p["connects_balance"] == 10
    assert {p["can_afford"] for p in plans} == {True, False}


def test_writer_prompt_holds_write_proposal_skill(
    conn, settings, config, fake_claude_gen, user_md
):
    from app import skills

    path = skills.skill_path(config, "write-proposal")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "---\nname: write-proposal\nsummary: Writing rules.\n---\nSKILLMARK open first.\n"
    )
    go(conn, settings, config)
    assert "SKILLMARK" in calls(fake_claude_gen, "write")[0]["prompt"]
    assert "SKILLMARK" not in calls(fake_claude_gen, "judge")[0]["prompt"]


def test_writer_works_without_the_skill(conn, settings, config, fake_claude_gen, user_md):
    go(conn, settings, config)
    assert calls(fake_claude_gen, "write")
    assert "SKILLMARK" not in calls(fake_claude_gen, "write")[0]["prompt"]
