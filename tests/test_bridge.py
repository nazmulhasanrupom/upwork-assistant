"""Claude bridge to Upwork (plan P1). A fake `claude` prints stream-json; nothing real runs."""

import json
from pathlib import Path

import pytest

from app.db import init_db
from app.llm.runner import BudgetCapReached, ClaudeFailed, ClaudeRateLimited
from app.settings import Settings
from app.tasks import daily
from app.upwork import bridge
from app.upwork.bridge import ClaudeUpworkBridge, ToolNotAllowed
from app.upwork.client import UpworkAuthError, UpworkToolError
from tests.upwork_fakes import load

SAMPLE = Path(__file__).parent / "fixtures" / "claude" / "stream_upwork_calls.jsonl"
ORG = "1000000000000000001"
SEARCH_ARGS = {
    "action": "smart_search",
    "org_uid": ORG,
    "params": {"mode": "most_recent", "days_posted": 1, "verified_payment_only": True, "limit": 2},
}


@pytest.fixture
def conn():
    return init_db(":memory:")


@pytest.fixture
def settings(conn):
    return Settings(conn)


@pytest.fixture
def br(config, settings, conn):
    return ClaudeUpworkBridge(config, settings, conn)


def write_rules(tmp_path, rules, monkeypatch):
    path = tmp_path / "rules.json"
    path.write_text(json.dumps(rules))
    monkeypatch.setenv("FAKE_UPWORK_RULES", str(path))


def reply(tool, payload, args=None, **extra):
    rule = {"tool": tool, "text": json.dumps(payload), **extra}
    if args:
        rule["args"] = args
    return rule


# ---------- the real sample ----------


def test_sample_stream_gives_both_results(br, fake_claude_stream, monkeypatch, conn):
    fake_claude_stream.mode("replay")
    monkeypatch.setenv("FAKE_CLAUDE_STREAM", str(SAMPLE))
    res = br.call_many([("list_accounts", {}), ("find_jobs", SEARCH_ARGS)])
    assert [r.name for r in res] == ["list_accounts", "find_jobs"]
    accounts = res[0].data()["accounts"]
    assert [a["role"] for a in accounts] == ["TALENT", "CLIENT", "FL_AGENCY"]
    jobs = res[1].data()
    assert jobs["hasMore"] is True and len(jobs["jobs"]) == 2
    assert not res[0].is_error and not res[1].is_error
    row = conn.execute("SELECT * FROM claude_calls").fetchone()
    assert row["task"] == "fetch" and row["model"] == "claude-haiku-5-5"
    assert row["cost_usd"] == pytest.approx(0.00494309) and row["created_at"]
    assert br.cost_usd == pytest.approx(0.00494309)


def test_sample_with_other_args_is_missing_not_a_crash(br, fake_claude_stream, monkeypatch):
    fake_claude_stream.mode("replay")
    monkeypatch.setenv("FAKE_CLAUDE_STREAM", str(SAMPLE))
    other = json.loads(json.dumps(SEARCH_ARGS))
    other["params"]["limit"] = 3
    res = br.call_many([("list_accounts", {}), ("find_jobs", other)])
    assert not res[0].is_error
    assert res[1].is_error and res[1].error_kind == "missing"


# ---------- the command ----------


def test_command_line(br, fake_claude_stream, config, tmp_path, monkeypatch):
    write_rules(
        tmp_path, [reply("list_accounts", {"accounts": []}), reply("find_jobs", {})], monkeypatch
    )
    br.call_many([("list_accounts", {}), ("find_jobs", SEARCH_ARGS)])
    log = fake_claude_stream.log()
    argv = log["argv"]

    def after(flag):
        return argv[argv.index(flag) + 1]

    assert argv[0] == "-p"
    assert after("--allowedTools") == (
        "mcp__upwork__upwork__list_accounts,mcp__upwork__upwork__find_jobs"
    )
    assert after("--disallowedTools") == "Bash,Edit,Write,WebFetch,WebSearch,Read,Glob,Grep,Task"
    assert after("--output-format") == "stream-json" and "--verbose" in argv
    assert after("--model") == "claude-haiku-5-5"
    assert after("--max-turns") == "5"  # 2 calls + 3
    assert after("--max-budget-usd") == "0.25"
    assert "--bare" not in argv
    prompt = argv[1]
    assert "1. mcp__upwork__upwork__list_accounts {}" in prompt
    assert "2. mcp__upwork__upwork__find_jobs " + json.dumps(SEARCH_ARGS) in prompt
    assert "untrusted" in prompt and "DONE" in prompt
    assert log["stdin"] == ""  # stdin is /dev/null
    assert Path(log["cwd"]).parent == config.runs_dir
    assert Path(log["cwd"]).name.startswith("fetch-")
    assert (Path(log["cwd"]) / "stream.jsonl").exists()


def test_config_dir_default_setting_and_env(br, fake_claude_stream, config, settings, monkeypatch):
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    br.call_many([("list_accounts", {})])
    assert fake_claude_stream.log()["config_dir"] == str(config.data_dir / "claude")
    assert (config.data_dir / "claude").is_dir()

    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config.data_dir / "from-env"))
    br.call_many([("list_accounts", {})])
    assert fake_claude_stream.logs()[-1]["config_dir"] == str(config.data_dir / "from-env")

    settings.set("claude.config_dir", str(config.data_dir / "from-setting"))
    br.call_many([("list_accounts", {})])
    assert fake_claude_stream.logs()[-1]["config_dir"] == str(config.data_dir / "from-setting")


def test_model_and_budget_come_from_settings(br, settings, fake_claude_stream):
    settings.set("models.fetch", "some-other-model")
    settings.set("claude.fetch_max_budget_usd", 0.1)
    br.call_many([("list_accounts", {})])
    argv = fake_claude_stream.log()["argv"]
    assert argv[argv.index("--model") + 1] == "some-other-model"
    assert argv[argv.index("--max-budget-usd") + 1] == "0.10"


def test_token_is_passed_through(config, settings, conn, fake_claude_stream, monkeypatch):
    # the child gets the token from Config (never printed); the fake only proves the call ran
    from dataclasses import replace

    cfg = replace(config, claude_code_oauth_token="tok")
    ClaudeUpworkBridge(cfg, settings, conn).call_many([("list_accounts", {})])
    assert fake_claude_stream.called()


# ---------- read tools only ----------


def test_read_tools_hold_no_write_tool():
    assert "list_accounts" in bridge.READ_TOOLS and "find_jobs" in bridge.READ_TOOLS
    for name in bridge.READ_TOOLS:
        assert not bridge._FORBIDDEN_RE.match(name), name
        assert not name.startswith(("manage_", "update_", "send_"))
        assert name != "confirm_preview"


@pytest.mark.parametrize(
    "tool",
    [
        "confirm_preview",
        "manage_proposals",
        "update_profile",
        "send_message",
        "accept_invitation",
        "Bash",
        "",
        "list_accounts2",
    ],
)
def test_other_tools_are_refused_before_claude_runs(br, fake_claude_stream, tool):
    with pytest.raises(ToolNotAllowed):
        br.call_many([("list_accounts", {}), (tool, {})])
    assert not fake_claude_stream.called()
    with pytest.raises(ToolNotAllowed):
        br.call_json(tool, {})
    with pytest.raises(ToolNotAllowed):
        bridge.build_command("p", [tool], "m", 4, 0.1)


def test_a_write_tool_added_to_the_list_by_mistake_still_fails(br, monkeypatch):
    monkeypatch.setattr(bridge, "READ_TOOLS", (*bridge.READ_TOOLS, "confirm_preview", "manage_x"))
    for tool in ("confirm_preview", "manage_x"):
        with pytest.raises(ToolNotAllowed):
            bridge.check_tools([tool])


def test_allowed_tools_only_those_in_the_batch(br, fake_claude_stream):
    br.call_many([("list_accounts", {}), ("list_accounts", {})])
    argv = fake_claude_stream.log()["argv"]
    assert argv[argv.index("--allowedTools") + 1] == "mcp__upwork__upwork__list_accounts"


# ---------- matching ----------


def test_extra_calls_are_ignored_and_order_is_kept(br, fake_claude_stream, tmp_path, monkeypatch):
    write_rules(
        tmp_path,
        [
            reply("find_jobs", {"which": "recent"}, {"params": {"mode": "most_recent"}}),
            reply("find_jobs", {"which": "best"}, {"params": {"mode": "best_match"}}),
            reply("list_accounts", {"which": "accounts"}),
        ],
        monkeypatch,
    )
    monkeypatch.setenv("FAKE_EXTRA", "1")
    monkeypatch.setenv("FAKE_REORDER", "1")
    best = {"action": "smart_search", "org_uid": ORG, "params": {"mode": "best_match"}}
    recent = {"action": "smart_search", "org_uid": ORG, "params": {"mode": "most_recent"}}
    res = br.call_many([("find_jobs", recent), ("list_accounts", {}), ("find_jobs", best)])
    assert [r.data()["which"] for r in res] == ["recent", "accounts", "best"]


def test_two_identical_calls_use_two_results(br, fake_claude_stream, tmp_path, monkeypatch):
    write_rules(tmp_path, [reply("list_accounts", {"n": 1})], monkeypatch)
    res = br.call_many([("list_accounts", {}), ("list_accounts", {})])
    assert [r.is_error for r in res] == [False, False]


def test_skipped_call_is_an_error_result_only_for_that_call(
    br, fake_claude_stream, tmp_path, monkeypatch
):
    write_rules(tmp_path, [reply("list_accounts", {"ok": 1})], monkeypatch)
    monkeypatch.setenv("FAKE_SKIP", "2")
    res = br.call_many([("list_accounts", {}), ("find_jobs", SEARCH_ARGS), ("list_accounts", {})])
    assert [r.is_error for r in res] == [False, True, False]
    assert res[1].error_kind == "missing" and "did not make this call" in res[1].text
    with pytest.raises(UpworkToolError):
        res[1].unwrap()


def test_tool_error_comes_back_as_error_result(br, fake_claude_stream, tmp_path, monkeypatch):
    write_rules(
        tmp_path,
        [
            {"tool": "find_jobs", "text": "job not found", "is_error": True},
            reply("list_accounts", {"ok": 1}),
        ],
        monkeypatch,
    )
    res = br.call_many([("find_jobs", SEARCH_ARGS), ("list_accounts", {})])
    assert res[0].is_error and res[0].error_kind is None and res[0].text == "job not found"
    assert res[1].data() == {"ok": 1}
    with pytest.raises(UpworkToolError, match="job not found"):
        br.call_json("find_jobs", SEARCH_ARGS)
    assert br.call_json("list_accounts") == {"ok": 1}


def test_result_text_is_not_json(br, fake_claude_stream, tmp_path, monkeypatch):
    write_rules(tmp_path, [{"tool": "list_accounts", "text": "plain words"}], monkeypatch)
    assert br.call_json("list_accounts") == "plain words"


def test_empty_batch_runs_nothing(br, fake_claude_stream):
    assert br.call_many([]) == []
    assert not fake_claude_stream.called()


# ---------- failures ----------


def test_server_needs_login(br, fake_claude_stream, monkeypatch):
    monkeypatch.setenv("FAKE_SERVER_STATUS", "needs-auth")
    with pytest.raises(UpworkAuthError, match="Log in"):
        br.call_many([("list_accounts", {})])


@pytest.mark.parametrize(
    "text", ["HTTP 401 Unauthorized", "This server needs auth", "Token expired"]
)
def test_auth_text_in_a_tool_error(br, fake_claude_stream, tmp_path, monkeypatch, text):
    write_rules(tmp_path, [{"tool": "list_accounts", "text": text, "is_error": True}], monkeypatch)
    with pytest.raises(UpworkAuthError):
        br.call_many([("list_accounts", {})])


def test_job_text_that_says_unauthorized_is_not_an_auth_error(
    br, fake_claude_stream, tmp_path, monkeypatch
):
    body = {"jobs": [{"title": "Fix 401 Unauthorized errors, needs auth work"}]}
    write_rules(tmp_path, [reply("find_jobs", body)], monkeypatch)
    assert br.call_json("find_jobs", SEARCH_ARGS) == body


def test_rate_limited(br, fake_claude_stream):
    fake_claude_stream.mode("rate_limit")
    with pytest.raises(ClaudeRateLimited):
        br.call_many([("list_accounts", {})])


def test_claude_gives_no_result(br, fake_claude_stream):
    fake_claude_stream.mode("no_result")
    with pytest.raises(ClaudeFailed, match="no result"):
        br.call_many([("list_accounts", {})])


def test_claude_not_installed(br, monkeypatch):
    monkeypatch.setenv("PATH", "/nonexistent")
    with pytest.raises(ClaudeFailed, match="not found"):
        br.call_many([("list_accounts", {})])


def test_monthly_cap_stops_before_claude(br, conn, fake_claude_stream, settings):
    settings.set("claude.monthly_cap_usd", 1.0)
    from app.llm.runner import save_call

    save_call(conn, "old", "m", {"total_cost_usd": 2.0}, "judge")
    with pytest.raises(BudgetCapReached):
        br.call_many([("list_accounts", {})])
    assert not fake_claude_stream.called()


# ---------- batches and the call limit ----------


def test_batches_of_eight(br, fake_claude_stream, tmp_path, monkeypatch):
    write_rules(tmp_path, [reply("list_accounts", {"ok": 1})], monkeypatch)
    res = br.call_many([("list_accounts", {})] * 10)
    assert len(res) == 10 and not any(r.is_error for r in res)
    logs = fake_claude_stream.logs()
    assert len(logs) == 2
    sizes = [x["argv"][1].count("mcp__upwork__upwork__list_accounts {}") for x in logs]
    assert sizes == [8, 2]
    assert br.claude_runs == 2


def test_batch_size_is_a_setting(br, settings, fake_claude_stream):
    settings.set("claude.fetch_batch_size", 2)
    br.call_many([("list_accounts", {})] * 5)
    assert len(fake_claude_stream.logs()) == 3


def test_run_row_counts_calls_and_limit_stops_the_rest(
    config, settings, conn, fake_claude_stream, tmp_path, monkeypatch
):
    write_rules(tmp_path, [reply("list_accounts", {"ok": 1})], monkeypatch)
    conn.execute(
        "INSERT INTO runs(run_id, task, started, status) VALUES ('r1','daily','x','running')"
    )
    settings.set("upwork.max_mcp_calls_per_run", 10)
    b = ClaudeUpworkBridge(config, settings, conn, "r1")
    res = b.call_many([("list_accounts", {})] * 12)
    assert [r.is_error for r in res] == [False] * 10 + [True] * 2
    assert [r.error_kind for r in res[10:]] == ["budget", "budget"]
    assert conn.execute("SELECT mcp_calls FROM runs").fetchone()[0] == 10
    # the claude_calls rows carry the run id of the caller
    assert {r["run_id"] for r in conn.execute("SELECT run_id FROM claude_calls")} == {"r1"}
    again = b.call_many([("list_accounts", {})])
    assert again[0].error_kind == "budget" and len(fake_claude_stream.logs()) == 2


# ---------- last fetch ----------


def test_last_fetch(br, conn, fake_claude_stream, monkeypatch):
    assert bridge.last_fetch(conn) is None
    monkeypatch.setenv("FAKE_COST", "0.0123")
    br.call_many([("list_accounts", {})])
    last = bridge.last_fetch(conn)
    assert last["cost_usd"] == pytest.approx(0.0123) and last["model"] == "claude-haiku-5-5"


# ---------- the daily task through the bridge ----------


def daily_rules():
    rules = [
        reply(
            "find_jobs", load("smart_search_most_recent.json"), {"params": {"mode": "most_recent"}}
        ),
        reply(
            "find_jobs", load("smart_search_best_match.json"), {"params": {"mode": "best_match"}}
        ),
    ]
    details = {
        "2118524057353559796": load("get_job.json"),
    }
    from tests.test_daily import make_details

    details.update(make_details())
    for job_id, payload in details.items():
        rules.append(reply("find_jobs", payload, {"action": "get", "params": {"job_id": job_id}}))
    return rules


def test_daily_on_fixtures_through_the_bridge(
    config, settings, conn, fake_claude_stream, tmp_path, monkeypatch
):
    write_rules(tmp_path, daily_rules(), monkeypatch)
    settings.set("upwork.talent_org_uid", ORG)
    res = daily.run_daily(conn, settings, config)  # no client: the real bridge + fake claude
    assert res.status == "ok", res.error
    assert (res.seen, res.kept, res.dropped) == (8, 5, 3)
    assert res.tiers == {"HIGH": 2, "MID": 3}
    row = conn.execute("SELECT * FROM runs WHERE run_id = ?", (res.run_id,)).fetchone()
    assert row["mcp_calls"] == 2 + 7  # two searches + seven job details
    runs = fake_claude_stream.logs()
    assert len(runs) == 2  # one batch for the searches, one for the details
    for r in runs:
        argv = r["argv"]
        assert set(argv[argv.index("--allowedTools") + 1].split(",")) == {
            "mcp__upwork__upwork__find_jobs"
        }
    cost = conn.execute(
        "SELECT SUM(cost_usd), COUNT(*) FROM claude_calls WHERE task='fetch'"
    ).fetchone()
    assert cost[1] == 2 and cost[0] == pytest.approx(0.01)
    assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 8


def test_daily_detects_the_account_through_the_bridge(
    config, settings, conn, fake_claude_stream, tmp_path, monkeypatch
):
    rules = daily_rules() + [reply("list_accounts", load("list_accounts.json"))]
    write_rules(tmp_path, rules, monkeypatch)
    res = daily.run_daily(conn, settings, config)
    assert res.status == "ok", res.error
    assert settings.get("upwork.talent_org_uid") == ORG
    first = fake_claude_stream.logs()[0]["argv"]
    assert "mcp__upwork__upwork__list_accounts" in first[first.index("--allowedTools") + 1]


def test_daily_survives_one_skipped_detail(
    config, settings, conn, fake_claude_stream, tmp_path, monkeypatch
):
    write_rules(tmp_path, daily_rules(), monkeypatch)
    settings.set("upwork.talent_org_uid", ORG)
    # "Claude" forgets call 3. The search batch has only two calls, so only the detail batch is hit.
    monkeypatch.setenv("FAKE_SKIP", "3")
    res = daily.run_daily(conn, settings, config)
    assert res.status == "ok", res.error
    assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 7  # one job left for later
    note = next((config.data_dir / "memory" / "daily").glob("*.md")).read_text()
    assert "tool error on job" in note and "did not make this call" in note
