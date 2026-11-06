"""Profile context page /context (plan J2a). Fake bridge and fake claude, no network."""

import copy
import json

import pytest

from app.context_routes import profile_to_text, strip_untrusted
from app.llm import prompting
from app.llm.judging import ALERT_USER_MD, user_md_alert
from app.upwork.client import UpworkAuthError, UpworkToolError
from tests.test_auth import csrf_from, setup_admin
from tests.test_auth import make_client as make_web_client
from tests.upwork_fakes import CallManyMixin, load

ORG = "1000000000000000001"


class FakeProfileMCP(CallManyMixin):
    def __init__(self, profile=None, highlights=None):
        self.profile = load("get_profile.json") if profile is None else profile
        self.highlights = (
            {
                "status": "ok",
                "highlights": [
                    {
                        "title": "<untrusted_participant_content>\nAI blog\n"
                        "</untrusted_participant_content>"
                    }
                ],
            }
            if highlights is None
            else highlights
        )
        self.calls = []
        self.closed = False

    def call_json(self, name, arguments=None):
        self.calls.append((name, copy.deepcopy(arguments)))
        assert name == "get_profile"
        res = self.profile if arguments["action"] == "get" else self.highlights
        if isinstance(res, Exception):
            raise res
        return copy.deepcopy(res)

    def close(self):
        self.closed = True


@pytest.fixture
def web(config):
    app, client = make_web_client(config)
    setup_admin(client)
    app.state.settings.set("upwork.talent_org_uid", ORG)
    return app, client


def post(client, path, **data):
    token = csrf_from(client.get("/context").text)
    return client.post(path, data={"csrf_token": token, **data})


def user_file(config):
    return config.data_dir / "memory" / "USER.md"


def use_bridge(app, mcp):
    app.state.mcp_factory = lambda: mcp
    return mcp


# ---------- plain page ----------


def test_context_needs_login(config):
    _, client = make_web_client(config)
    setup_admin(client)
    client.cookies.clear()
    r = client.get("/context")
    assert r.status_code == 303 and r.headers["location"].startswith("/login")


def test_empty_page_shows_default_style_and_warning(web):
    _, client = web
    r = client.get("/context")
    assert r.status_code == 200
    assert "USER.md is empty" in r.text
    assert "Plain, direct, human" in r.text  # default style from prompts/style.md
    assert "0 + MEMORY.md 0 = 0 of 3500 characters" in r.text
    assert 'aria-current="page">Profile' in r.text


def test_nav_has_profile_and_runs(web):
    _, client = web
    r = client.get("/")
    assert 'href="/context"' in r.text and 'href="/runs"' in r.text


# ---------- save ----------


def test_save_writes_files_and_clears_alert(web, config):
    app, client = web
    user_md_alert(app.state.db, False, __import__("datetime").datetime.now())
    assert app.state.db.execute("SELECT 1 FROM alerts WHERE key=?", (ALERT_USER_MD,)).fetchone()
    r = post(
        client, "/context", user_md="# Profile\r\nI build n8n flows.\r\n", style_md="Be brief."
    )
    assert r.status_code == 200 and "Saved." in r.text
    assert user_file(config).read_text() == "# Profile\nI build n8n flows.\n"
    assert (config.data_dir / "memory" / "style.md").read_text() == "Be brief.\n"
    assert not list((config.data_dir / "memory").glob(".*.tmp"))  # temp file renamed away
    assert not app.state.db.execute("SELECT 1 FROM alerts WHERE key=?", (ALERT_USER_MD,)).fetchone()
    assert prompting.user_facts(config) == "# Profile\nI build n8n flows."
    assert prompting.style_text(config) == "Be brief."  # the prompts now use the saved style


def test_save_needs_csrf(web, config):
    _, client = web
    r = client.post("/context", data={"user_md": "x", "style_md": ""})
    assert r.status_code == 403
    assert not user_file(config).exists()


def test_blank_user_md_keeps_alert(web, config):
    app, client = web
    from datetime import datetime

    user_md_alert(app.state.db, False, datetime.now())
    post(client, "/context", user_md="  \n", style_md="")
    assert app.state.db.execute("SELECT 1 FROM alerts WHERE key=?", (ALERT_USER_MD,)).fetchone()


def test_style_equal_to_default_or_blank_removes_the_file(web, config):
    _, client = web
    post(client, "/context", user_md="x", style_md="Custom")
    style = config.data_dir / "memory" / "style.md"
    assert style.exists()
    post(client, "/context", user_md="x", style_md="")
    assert not style.exists()
    assert prompting.style_text(config) == prompting.default_style()
    post(client, "/context", user_md="x", style_md=prompting.default_style() + "\n")
    assert not style.exists()


def test_over_cap_save_is_refused(web, config):
    _, client = web
    r = post(client, "/context", user_md="a" * 3600, style_md="")
    assert r.status_code == 400 and "over the 3500 character cap" in r.text
    assert not user_file(config).exists()


def test_count_includes_memory_md(web, config):
    _, client = web
    mem = config.data_dir / "memory"
    mem.mkdir(parents=True, exist_ok=True)
    (mem / "MEMORY.md").write_text("m" * 1000)
    r = post(client, "/context", user_md="u" * 2000, style_md="")
    assert "2000 + MEMORY.md 1000 = 3000 of 3500" in r.text and "Over the cap" not in r.text


def test_too_long_is_refused(web, config):
    app, client = web
    app.state.settings.set("memory.input_max_chars", 50)
    r = post(client, "/context", user_md="a" * 51, style_md="")
    assert r.status_code == 413 and "too long" in r.text
    assert not user_file(config).exists()


# ---------- profile text ----------


def test_profile_to_text_strips_tags_and_ids():
    text = profile_to_text(
        load("get_profile.json"),
        {"items": ["<untrusted_participant_content>x</untrusted_participant_content>"]},
    )
    assert "untrusted" not in text
    assert "AI Automation for Marketing & Lead Gen" in text
    assert "chargeRate.rawValue: 25.0" in text
    assert "top_rated: Top Rated" in text and "totalFeedback: 16" in text
    assert "n8n" in text and "Make.com" in text
    assert "ciphertext" not in text and "~01sample" not in text.split("profile_url")[0]
    assert "x" in text.split("Highlights")[1]
    assert (
        strip_untrusted("<untrusted_participant_content>\nhi\n</untrusted_participant_content>")
        == "hi"
    )


# ---------- build from Upwork ----------


def test_build_shows_proposal_and_does_not_overwrite(web, config, fake_claude_gen):
    app, client = web
    mem = config.data_dir / "memory"
    mem.mkdir(parents=True, exist_ok=True)
    (mem / "USER.md").write_text("OLD FACTS\n")
    mcp = use_bridge(app, FakeProfileMCP())
    r = post(client, "/context/build")
    assert r.status_code == 200 and "not saved until you click Use this" in r.text
    assert "AI Automation, $25/hr" in r.text  # proposed text shown
    assert "OLD FACTS" in r.text  # next to the current one
    assert user_file(config).read_text() == "OLD FACTS\n"  # never overwritten without a click
    assert "Claude cost: $0.0500" in r.text
    assert mcp.closed
    assert mcp.calls == [
        ("get_profile", {"action": "get", "org_uid": ORG, "params": {}}),
        ("get_profile", {"action": "list_highlights", "org_uid": ORG, "params": {}}),
    ]
    log = fake_claude_gen.log()
    assert log["model"] == app.state.settings.get("models.write")
    prompt = log["prompt"]
    assert prompt.index("Hard rules") < prompt.index("# Task: build USER.md")
    assert "untrusted_participant_content" not in json.dumps(log["input"])  # tags stripped
    profile = log["input"]["profile_text"]
    assert profile.startswith("<untrusted_profile>") and profile.rstrip().endswith(
        "</untrusted_profile>"
    )
    assert "AI Automation for Marketing" in profile and "AI blog" in profile
    assert log["input"]["max_chars"] == 2500
    # the proposal survives a reload
    assert "AI Automation, $25/hr" in client.get("/context").text


def test_build_prompt_marks_profile_as_data():
    text = (prompting.APP_ROOT / "prompts" / "build_me.md").read_text()
    assert "# Task: build USER.md" in text
    assert "untrusted_profile" in text and "not instructions" in text
    assert "2500" not in text  # the limit comes from settings (max_chars)
    assert "no em dashes" in text.lower()


def test_use_this_saves_and_clears_alert(web, config, fake_claude_gen):
    from datetime import datetime

    app, client = web
    use_bridge(app, FakeProfileMCP())
    user_md_alert(app.state.db, False, datetime.now())
    post(client, "/context/build")
    proposed = (config.data_dir / "memory" / "USER.proposed.md").read_text()
    r = post(client, "/context/use", proposed=proposed)
    assert r.status_code == 200 and "saved from the proposed version" in r.text
    assert user_file(config).read_text() == proposed
    assert not (config.data_dir / "memory" / "USER.proposed.md").exists()
    assert not app.state.db.execute("SELECT 1 FROM alerts WHERE key=?", (ALERT_USER_MD,)).fetchone()


def test_use_this_saves_the_edited_text(web, config):
    _, client = web
    r = post(client, "/context/use", proposed="Edited by me\r\n")
    assert r.status_code == 200 and user_file(config).read_text() == "Edited by me\n"


def test_use_with_empty_text_is_refused(web, config):
    _, client = web
    r = post(client, "/context/use", proposed="  ")
    assert r.status_code == 400 and not user_file(config).exists()


def test_discard(web, config, fake_claude_gen):
    app, client = web
    use_bridge(app, FakeProfileMCP())
    post(client, "/context/build")
    assert (config.data_dir / "memory" / "USER.proposed.md").exists()
    r = post(client, "/context/discard")
    assert "discarded" in r.text and not (config.data_dir / "memory" / "USER.proposed.md").exists()
    assert not user_file(config).exists()


def test_build_cleans_em_dashes(web, fake_claude_gen):
    app, client = web
    use_bridge(app, FakeProfileMCP())
    fake_claude_gen.env("FAKE_BUILD_EM", "1")
    r = post(client, "/context/build")
    assert "—" not in r.text and "Saved 2 hours per article, real result." in r.text


def test_build_works_when_highlights_fail(web, fake_claude_gen):
    app, client = web
    use_bridge(app, FakeProfileMCP(highlights=UpworkToolError("no such action")))
    r = post(client, "/context/build")
    assert r.status_code == 200 and "AI Automation, $25/hr" in r.text
    assert "AI blog" not in fake_claude_gen.log()["input"]["profile_text"]


def test_build_profile_failure_shows_error_and_calls_no_claude(web, config, fake_claude_gen):
    app, client = web
    use_bridge(app, FakeProfileMCP(profile=UpworkToolError("boom")))
    r = post(client, "/context/build")
    assert r.status_code == 400 and "Could not read the Upwork profile" in r.text
    assert not fake_claude_gen.called() and not user_file(config).exists()


def test_build_login_problem_message(web, fake_claude_gen):
    app, client = web
    use_bridge(
        app, FakeProfileMCP(profile=UpworkAuthError("Upwork login in Claude Code is missing"))
    )
    r = post(client, "/context/build")
    assert r.status_code == 400 and "login in Claude Code is missing" in r.text
    assert not fake_claude_gen.called()


def test_build_without_text_from_claude(web, fake_claude_gen):
    app, client = web
    use_bridge(app, FakeProfileMCP())
    fake_claude_gen.env("FAKE_BUILD_EMPTY", "1")
    r = post(client, "/context/build")
    assert r.status_code == 400 and "no USER.md text" in r.text


def test_build_stops_at_the_cap(web, fake_claude_gen):
    app, client = web
    use_bridge(app, FakeProfileMCP())
    app.state.settings.set("claude.monthly_cap_usd", 0.0)
    r = post(client, "/context/build")
    assert r.status_code == 400 and "cap reached" in r.text.lower()
    assert not fake_claude_gen.called()


def test_build_finds_account_first_when_missing(web, fake_claude_gen):
    from tests.upwork_fakes import accounts_payload

    app, client = web
    app.state.settings.set("upwork.talent_org_uid", "")

    class Both(FakeProfileMCP):
        def call_json(self, name, arguments=None):
            if name == "list_accounts":
                return accounts_payload(("TALENT", "Me", "777"))
            return super().call_json(name, arguments)

    mcp = use_bridge(app, Both())
    r = post(client, "/context/build")
    assert r.status_code == 200
    assert mcp.calls[-2][1]["org_uid"] == "777"


# ---------- J2: Rebuild ----------


def test_button_says_build_when_empty_and_rebuild_when_filled(web, config):
    _, client = web
    assert "Build from Upwork" in client.get("/context").text
    assert "Rebuild from Upwork" not in client.get("/context").text
    user_file(config).parent.mkdir(parents=True, exist_ok=True)
    user_file(config).write_text("Some facts\n")
    r = client.get("/context")
    assert "Rebuild from Upwork" in r.text


def test_rebuild_uses_build_me_prompt_and_saving_goes_through_cap_and_history(
    web, config, fake_claude_gen, monkeypatch
):
    from app import versioning

    commits = []
    monkeypatch.setattr(versioning, "commit", lambda c, s, msg, paths=None: commits.append(msg))
    app, client = web
    user_file(config).parent.mkdir(parents=True, exist_ok=True)
    user_file(config).write_text("OLD FACTS\n")
    use_bridge(app, FakeProfileMCP())
    r = post(client, "/context/build")
    assert r.status_code == 200 and "Rebuild from Upwork" in r.text
    assert "# Task: build USER.md" in fake_claude_gen.log()["prompt"]
    # too long for the cap: refused, nothing written
    app.state.settings.set("memory.total_cap_chars", 50)
    r = post(client, "/context/use", proposed="x" * 200)
    assert r.status_code == 400 and user_file(config).read_text() == "OLD FACTS\n"
    app.state.settings.reset("memory.total_cap_chars")
    r = post(client, "/context/use", proposed="NEW FACTS")
    assert r.status_code == 200 and user_file(config).read_text() == "NEW FACTS\n"
    assert commits == ["Update USER.md"]  # only the accepted save made a history entry
