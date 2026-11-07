"""Round 3 A1: proposal rules validator, the one rewrite pass (fake claude), the page warning."""

import json

import pytest

from app import proposal_rules as pr
from app.db import init_db
from app.llm import judging
from app.settings import Settings
from app.tasks import daily
from tests.test_daily import make_client
from tests.test_h2_job_actions import JOB, page, post, seed, web  # noqa: F401

ORG = "1000000000000000001"
EM, EN = "—", "–"
pytestmark = pytest.mark.judging


@pytest.mark.parametrize(
    "text",
    [
        "Your CRM leaks leads. I fix that in two weeks.",
        "Range 2.5 to 10 hours, about 10.50 USD per hour, version v2.0.",
        "Bots. e.g. n8n flows for U.S. firms, i.e. fast ones.",
        "It costs $1,000.00 and I start on Monday.",
        "",
    ],
)
def test_clean_text_passes(text):
    assert pr.check(text) == []


@pytest.mark.parametrize(
    ("text", "word"),
    [
        ("Visit example.com today.", "web address"),
        ("I build with Node.js daily.", "web address"),
        ("Done.Next I start.", "web address"),
        ("See https://example.com/work now.", "link"),
        ("See www.example.com now.", "link"),
        (f"Fast {EM} and cheap.", "dash"),
        (f"Range 5{EN}10.", "dash"),
        ("Great work 🙂", "emoji"),
        ("A check mark ✅ here", "emoji"),
        ("This is **bold** text.", "markup"),
        ("This is _italic_ text.", "markdown"),
        ("# Heading\nText", "markdown"),
        ("Use `code` here", "markdown"),
        ("See [this](x) ok", "markdown"),
        ("- one\n- two", "list"),
        ("1. one\n2. two", "list"),
        ("• one", "list"),
        ("Hello <b>there</b>", "HTML"),
        ("a > b", "HTML"),
        ("𝗕𝗼𝗹𝗱 letters", "bold"),
    ],
)
def test_each_rule_breaks(text, word):
    problems = pr.check(text)
    assert problems, text
    assert any(word in p for p in problems), problems


def test_decimal_is_not_a_domain_but_word_dot_word_is():
    assert pr.check("It took 2.5 days.") == []
    assert pr.check("It took 25.5 days, v1.2.3 shipped.") == []
    assert pr.check("see something.something here")


def test_check_draft_names_where_the_problem_is():
    out = pr.check_draft("Fine.", [{"question": "q", "answer": "Go to a.com"}, {"answer": "ok"}])
    assert len(out) == 1 and out[0].startswith("Answer 1: ")
    assert pr.check_draft("**x**", [])[0].startswith("Cover letter: ")
    assert pr.check_draft("Fine.", None) == []


# ------------------------------------------------------------ the draft path with a fake claude


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


def go(conn, settings, config):
    return daily.run_daily(conn, settings, config, make_client(settings, balance=50))


def tasks(fake):
    return [c["task"] for c in fake.logs()]


def test_clean_draft_needs_no_rewrite(conn, settings, config, fake_claude_gen, user_md):
    go(conn, settings, config)
    assert "fixrules" not in tasks(fake_claude_gen)
    rows = conn.execute("SELECT rules_ok, rules_problems FROM drafts").fetchall()
    assert len(rows) == 5
    assert all(r["rules_ok"] == 1 and json.loads(r["rules_problems"]) == [] for r in rows)


def test_rule_breaking_draft_is_rewritten_once(conn, settings, config, fake_claude_gen, user_md):
    fake_claude_gen.env("FAKE_GEN_BADRULES", 1)
    go(conn, settings, config)
    log = fake_claude_gen.logs()
    fixes = [c for c in log if c["task"] == "fixrules"]
    assert len(fixes) == 5  # one rewrite per draft, never a second
    first = fixes[0]
    assert "# Task: fix proposal rules" in first["prompt"]
    assert first["input"]["problems"] and "**" in first["input"]["draft"]["cover_letter"]
    assert first["model"] == "claude-sonnet-5-5"
    rows = conn.execute("SELECT * FROM drafts").fetchall()
    assert len(rows) == 5
    for r in rows:
        assert r["rules_ok"] == 1 and json.loads(r["rules_problems"]) == []
        assert pr.check(r["cover_letter"]) == []
        assert "Bots" in r["cover_letter"]  # the client's word is still first


def test_rewrite_that_still_breaks_marks_the_draft(
    conn, settings, config, fake_claude_gen, user_md
):
    fake_claude_gen.env("FAKE_GEN_BADRULES", 1)
    fake_claude_gen.env("FAKE_FIX_STILL", 1)
    go(conn, settings, config)
    assert tasks(fake_claude_gen).count("fixrules") == 5  # still only one pass each
    rows = conn.execute("SELECT * FROM drafts").fetchall()
    assert len(rows) == 5  # the draft is kept, not dropped
    for r in rows:
        assert r["rules_ok"] == 0
        problems = json.loads(r["rules_problems"])
        assert problems and problems[0].startswith("Cover letter: ")
        assert any("proposal rules" in f for f in json.loads(r["flags"]))


def test_unusable_rewrite_answer_marks_the_draft(conn, settings, config, fake_claude_gen, user_md):
    fake_claude_gen.env("FAKE_GEN_BADRULES", 1)
    fake_claude_gen.env("FAKE_FIX_BAD", 1)
    go(conn, settings, config)
    assert {r["rules_ok"] for r in conn.execute("SELECT rules_ok FROM drafts")} == {0}


def test_rewrite_failure_never_loses_the_draft(conn, settings, config, fake_claude_gen, user_md):
    fake_claude_gen.env("FAKE_GEN_BADRULES", 1)
    fake_claude_gen.env("FAKE_GEN_LIMIT_AFTER", 2)  # judge + first write ok, rewrites get 429
    go(conn, settings, config)
    rows = conn.execute("SELECT rules_ok FROM drafts").fetchall()
    assert len(rows) == 3 and {r["rules_ok"] for r in rows} == {0}  # the first batch, all kept
    assert tasks(fake_claude_gen).count("fixrules") == 1  # the 429 stopped the rest


def test_prompts_carry_the_rules():
    for path in ("prompts/write_proposal.md", "prompts/fix_proposal.md"):
        text = open(path).read()
        assert "web address" in text and "em dashes" in text and "emoji" in text
    assert "Truthful only" in open("prompts/write_proposal.md").read()
    assert judging.FIX_PROMPT == "prompts/fix_proposal.md"


# ------------------------------------------------------------ the page


def test_page_warns_when_the_draft_breaks_the_rules(web):  # noqa: F811
    app, client = web
    app.state.db.execute(
        "UPDATE drafts SET rules_ok = 0, rules_problems = ? WHERE job_id = ?",
        (json.dumps(['Cover letter: Has a link or URL: "x.com". Remove it.']), JOB),
    )
    html = page(client).text
    assert "breaks the proposal rules" in html and "Has a link or URL" in html


def test_page_has_no_warning_for_a_clean_or_unchecked_draft(web):  # noqa: F811
    app, client = web
    assert "breaks the proposal rules" not in page(client).text  # rules_ok is NULL (old draft)
    app.state.db.execute("UPDATE drafts SET rules_ok = 1 WHERE job_id = ?", (JOB,))
    assert "breaks the proposal rules" not in page(client).text


def test_manual_edit_rechecks_the_rules(web):  # noqa: F811
    app, client = web
    post(client, "draft", cover_letter="See **this** at demo.com", answers="Fine.", rate="30")
    row = app.state.db.execute("SELECT * FROM drafts WHERE job_id = ?", (JOB,)).fetchone()
    assert row["rules_ok"] == 0 and json.loads(row["rules_problems"])
    assert "breaks the proposal rules" in page(client).text
    post(client, "draft", cover_letter="A plain letter.", answers="Fine.", rate="30")
    row = app.state.db.execute("SELECT * FROM drafts WHERE job_id = ?", (JOB,)).fetchone()
    assert row["rules_ok"] == 1 and json.loads(row["rules_problems"]) == []
    assert seed  # imported for the fixture module
