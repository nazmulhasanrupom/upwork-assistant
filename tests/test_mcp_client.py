"""Upwork MCP client (plan D3). Every call is mocked with respx; no real network, no real sleep."""

import json
from datetime import timedelta

import httpx
import pytest
import respx

from app.db import init_db
from app.settings import Settings
from app.upwork import oauth as oauth_mod
from app.upwork.client import (
    CallBudgetExceeded,
    DryRunBlocked,
    UpworkAuthError,
    UpworkHTTPError,
    UpworkMCP,
    UpworkProtocolError,
    UpworkRPCError,
    UpworkToolError,
    parse_sse,
)
from app.upwork.oauth import OAuthError, Tokens, UpworkOAuth

URL = "https://mcp.upwork.com/mcp"
TOKEN_URL = "https://www.upwork.com/api/v3/oauth2/token"


class FakeTime:
    """One object is both the clock and the sleep: sleeping moves the clock."""

    def __init__(self):
        self.now = 1000.0
        self.sleeps: list[float] = []

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


class FakeOAuth:
    def __init__(self, tokens=("tok-1", "tok-2", "tok-3")):
        self.tokens = list(tokens)
        self.current = 0
        self.rejected_seen: list[str | None] = []

    def access_token(self, rejected=None):
        self.rejected_seen.append(rejected)
        if rejected is not None and rejected == self.tokens[self.current]:
            self.current += 1
        return self.tokens[self.current]


class Server:
    """A tiny fake MCP server. `handler` can be replaced to change behavior."""

    def __init__(self, router, sse=False, session_id="sess-1"):
        self.sse = sse
        self.session_id = session_id
        self.requests: list[httpx.Request] = []
        self.tool_replies: list[dict] = []  # result objects, used in order, then the last repeats
        self.tool_calls = 0
        router.post(URL).mock(side_effect=self.handle)

    def body(self, request):
        return json.loads(request.content)

    def reply(self, request, result=None, error=None, headers=None):
        msg = {"jsonrpc": "2.0", "id": self.body(request)["id"]}
        if error is not None:
            msg["error"] = error
        else:
            msg["result"] = result
        headers = dict(headers or {})
        if self.sse:
            text = (
                ": keep-alive\n\n"
                'data: {"jsonrpc":"2.0","method":"notifications/message","params":{}}\n\n'
                "event: message\r\ndata: " + json.dumps(msg) + "\r\n\r\n"
            )
            headers["content-type"] = "text/event-stream"
            return httpx.Response(200, text=text, headers=headers)
        return httpx.Response(200, json=msg, headers=headers)

    def handle(self, request):
        self.requests.append(request)
        body = self.body(request)
        method = body.get("method")
        if method == "initialize":
            headers = {"Mcp-Session-Id": self.session_id} if self.session_id else {}
            return self.reply(
                request,
                {"protocolVersion": "2025-06-18", "capabilities": {}, "serverInfo": {"name": "x"}},
                headers=headers,
            )
        if method == "notifications/initialized":
            return httpx.Response(202)
        if method == "tools/list":
            return self.reply(request, {"tools": [{"name": "find_jobs"}, {"name": "get_profile"}]})
        if method == "tools/call":
            i = min(self.tool_calls, len(self.tool_replies) - 1)
            self.tool_calls += 1
            return self.reply(request, self.tool_replies[i])
        return httpx.Response(400)

    def methods(self):
        return [self.body(r)["method"] for r in self.requests]


def ok_result(payload):
    return {"content": [{"type": "text", "text": json.dumps(payload)}]}


@pytest.fixture
def conn():
    return init_db(":memory:")


@pytest.fixture
def settings(conn):
    s = Settings(conn)
    s.set("upwork.mcp_calls_per_second", 0)  # no throttle unless a test wants it
    return s


@pytest.fixture
def ft():
    return FakeTime()


@pytest.fixture
def router():
    with respx.mock(assert_all_called=False) as r:
        yield r


def make(settings, conn, ft, oauth=None, run_id=None):
    return UpworkMCP(
        settings, oauth or FakeOAuth(), conn, run_id, sleep=ft.sleep, clock=ft.clock
    )


def new_run(conn, run_id="r1"):
    conn.execute("INSERT INTO runs(run_id, task, started) VALUES (?, 'daily', 'now')", (run_id,))
    return run_id


# ---------- happy path ----------


def test_json_reply_and_handshake(router, settings, conn, ft):
    srv = Server(router)
    srv.tool_replies = [ok_result({"jobs": [1, 2]})]
    c = make(settings, conn, ft)
    result = c.call_tool("find_jobs", {"q": "python"})
    assert result.data() == {"jobs": [1, 2]}
    assert srv.methods() == ["initialize", "notifications/initialized", "tools/call"]

    init, notified, call = srv.requests
    assert init.headers["authorization"] == "Bearer tok-1"
    assert init.headers["accept"] == "application/json, text/event-stream"
    assert init.headers["content-type"].startswith("application/json")
    assert "mcp-protocol-version" not in init.headers
    assert "mcp-session-id" not in init.headers
    assert srv.body(init)["params"]["protocolVersion"] == "2025-06-18"
    for r in (notified, call):
        assert r.headers["mcp-session-id"] == "sess-1"
        assert r.headers["mcp-protocol-version"] == "2025-06-18"
    assert "id" not in srv.body(notified)
    assert srv.body(call)["params"] == {"name": "find_jobs", "arguments": {"q": "python"}}


def test_session_is_reused(router, settings, conn, ft):
    srv = Server(router)
    srv.tool_replies = [ok_result({"a": 1})]
    c = make(settings, conn, ft)
    c.call_tool("a")
    c.call_tool("b")
    assert srv.methods().count("initialize") == 1
    assert srv.methods().count("tools/call") == 2


def test_server_without_session_id(router, settings, conn, ft):
    srv = Server(router, session_id=None)
    srv.tool_replies = [ok_result({"a": 1})]
    make(settings, conn, ft).call_tool("a")
    assert all("mcp-session-id" not in r.headers for r in srv.requests)


def test_sse_reply(router, settings, conn, ft):
    srv = Server(router, sse=True)
    srv.tool_replies = [ok_result({"jobs": ["x"]})]
    assert make(settings, conn, ft).call_json("find_jobs") == {"jobs": ["x"]}


def test_parse_sse_multiline_data_and_noise():
    body = 'id: 1\ndata: {"a":\ndata: 1}\n\n:comment\n\ndata: not json\n\ndata:{"b": 2}\n\n'
    assert parse_sse(body) == [{"a": 1}, {"b": 2}]


def test_picks_matching_id_from_batch(router, settings, conn, ft):
    srv = Server(router)
    srv.tool_replies = [ok_result("unused")]
    orig = srv.handle

    def handle(request):
        resp = orig(request)
        if srv.body(request).get("method") == "tools/call":
            rid = srv.body(request)["id"]
            return httpx.Response(
                200,
                json=[
                    {"jsonrpc": "2.0", "id": rid + 100, "result": ok_result("wrong")},
                    {"jsonrpc": "2.0", "id": rid, "result": ok_result("right")},
                ],
            )
        return resp

    router.post(URL).mock(side_effect=handle)
    assert make(settings, conn, ft).call_json("t") == "right"


def test_no_matching_id_is_protocol_error(router, settings, conn, ft):
    Server(router)
    router.post(URL).mock(
        side_effect=lambda req: httpx.Response(
            200, json={"jsonrpc": "2.0", "id": 999, "result": {}}
        )
    )
    with pytest.raises(UpworkProtocolError):
        make(settings, conn, ft).call_tool("t")


def test_rpc_error(router, settings, conn, ft):
    srv = Server(router)
    orig = srv.handle

    def handle(request):
        if srv.body(request).get("method") == "tools/call":
            return srv.reply(request, error={"code": -32602, "message": "bad arguments"})
        return orig(request)

    router.post(URL).mock(side_effect=handle)
    with pytest.raises(UpworkRPCError) as e:
        make(settings, conn, ft).call_tool("t")
    assert e.value.code == -32602


def test_list_tools(router, settings, conn, ft):
    srv = Server(router)
    tools = make(settings, conn, ft).list_tools()
    assert [t["name"] for t in tools] == ["find_jobs", "get_profile"]
    assert "tools/list" in srv.methods()


# ---------- tool results ----------


def test_structured_content_wins():
    from app.upwork.client import ToolResult

    r = ToolResult("t", [{"type": "text", "text": '{"a": 1}'}], structured={"b": 2})
    assert r.data() == {"b": 2}


def test_text_json_and_plain_text():
    from app.upwork.client import ToolResult

    assert ToolResult("t", [{"type": "text", "text": '{"a": 1}'}]).data() == {"a": 1}
    assert ToolResult("t", [{"type": "text", "text": "hello"}]).data() == "hello"
    assert ToolResult("t", []).data() is None


def test_structured_content_from_server(router, settings, conn, ft):
    srv = Server(router)
    srv.tool_replies = [
        {"content": [{"type": "text", "text": "{}"}], "structuredContent": {"n": 5}}
    ]
    assert make(settings, conn, ft).call_json("t") == {"n": 5}


def test_is_error_raises_tool_error(router, settings, conn, ft):
    srv = Server(router)
    srv.tool_replies = [{"isError": True, "content": [{"type": "text", "text": "Job not found"}]}]
    with pytest.raises(UpworkToolError, match="Job not found"):
        make(settings, conn, ft).call_tool("get")


# ---------- auth ----------


def test_401_refreshes_once_and_retries(router, settings, conn, ft):
    srv = Server(router)
    srv.tool_replies = [ok_result({"ok": True})]
    orig = srv.handle
    state = {"rejected_once": False}

    def handle(request):
        if request.headers["authorization"] == "Bearer tok-1":
            srv.requests.append(request)
            state["rejected_once"] = True
            return httpx.Response(401)
        return orig(request)

    router.post(URL).mock(side_effect=handle)
    oauth = FakeOAuth()
    assert make(settings, conn, ft, oauth).call_json("t") == {"ok": True}
    assert state["rejected_once"]
    # initialize (rejected), then a fresh token check for each later request
    assert oauth.rejected_seen == [None, "tok-1", None, None]
    assert srv.requests[-1].headers["authorization"] == "Bearer tok-2"


def test_second_401_raises_auth_error(router, settings, conn, ft):
    n = {"calls": 0}

    def handle(request):
        n["calls"] += 1
        return httpx.Response(401)

    router.post(URL).mock(side_effect=handle)
    oauth = FakeOAuth()
    with pytest.raises(UpworkAuthError):
        make(settings, conn, ft, oauth).call_tool("t")
    assert n["calls"] == 2  # one try, one retry after the refresh
    assert oauth.rejected_seen == [None, "tok-1"]


def test_oauth_failure_is_auth_error(settings, conn, ft):
    class Broken:
        def access_token(self, rejected=None):
            raise OAuthError("Upwork is not connected.")

    with pytest.raises(UpworkAuthError, match="not connected"):
        make(settings, conn, ft, Broken()).call_tool("t")


def test_401_with_real_oauth_refresh(router, config, settings, conn, ft):
    """The real UpworkOAuth: a 401 forces a refresh even when the expiry looks fine."""
    oa = UpworkOAuth(config, settings, conn)
    future = (oauth_mod.utcnow() + timedelta(hours=1)).isoformat(timespec="seconds")
    oa.store.save(Tokens("old-at", "rt", future, ""))
    router.post(TOKEN_URL).mock(
        return_value=httpx.Response(
            200, json={"access_token": "new-at", "refresh_token": "rt2", "expires_in": 3600}
        )
    )
    srv = Server(router)
    srv.tool_replies = [ok_result({"ok": 1})]
    orig = srv.handle

    def handle(request):
        if request.headers["authorization"] == "Bearer old-at":
            return httpx.Response(401)
        return orig(request)

    router.post(URL).mock(side_effect=handle)
    assert make(settings, conn, ft, oa).call_json("t") == {"ok": 1}
    assert oa.store.load().access_token == "new-at"
    assert srv.requests[-1].headers["authorization"] == "Bearer new-at"


# ---------- session loss ----------


def test_404_with_session_starts_new_session_once(router, settings, conn, ft):
    srv = Server(router)
    srv.tool_replies = [ok_result({"ok": 1})]
    orig = srv.handle
    state = {"dropped": False}

    def handle(request):
        body = srv.body(request)
        if body["method"] == "tools/call" and not state["dropped"]:
            state["dropped"] = True
            srv.requests.append(request)
            return httpx.Response(404)
        if body["method"] == "initialize":
            srv.session_id = "sess-2" if state["dropped"] else "sess-1"
        return orig(request)

    router.post(URL).mock(side_effect=handle)
    assert make(settings, conn, ft).call_json("t") == {"ok": 1}
    assert srv.methods().count("initialize") == 2
    assert srv.requests[-1].headers["mcp-session-id"] == "sess-2"


def test_404_twice_gives_up(router, settings, conn, ft):
    srv = Server(router)
    orig = srv.handle

    def handle(request):
        if srv.body(request)["method"] == "tools/call":
            return httpx.Response(404)
        return orig(request)

    router.post(URL).mock(side_effect=handle)
    with pytest.raises(UpworkHTTPError) as e:
        make(settings, conn, ft).call_tool("t")
    assert e.value.status == 404
    assert srv.methods().count("initialize") == 2


def test_404_without_session_is_plain_error(router, settings, conn, ft):
    router.post(URL).mock(return_value=httpx.Response(404))
    with pytest.raises(UpworkHTTPError) as e:
        make(settings, conn, ft).call_tool("t")
    assert e.value.status == 404


# ---------- retries and rate ----------


def flaky(srv, statuses, headers=None):
    """Make tools/call fail with these statuses first, then succeed."""
    orig = srv.handle
    queue = list(statuses)

    def handle(request):
        if srv.body(request)["method"] == "tools/call" and queue:
            return httpx.Response(queue.pop(0), headers=headers or {})
        return orig(request)

    return handle


def test_429_backoff_with_fake_sleep(router, settings, conn, ft):
    srv = Server(router)
    srv.tool_replies = [ok_result({"ok": 1})]
    router.post(URL).mock(side_effect=flaky(srv, [429, 503]))
    assert make(settings, conn, ft).call_json("t") == {"ok": 1}
    assert ft.sleeps == [1.0, 2.0]  # base 1s, doubled


def test_retry_after_is_respected_and_capped(router, settings, conn, ft):
    srv = Server(router)
    srv.tool_replies = [ok_result({"ok": 1})]
    router.post(URL).mock(side_effect=flaky(srv, [429], headers={"Retry-After": "7"}))
    make(settings, conn, ft).call_tool("t")
    assert ft.sleeps == [7.0]

    ft.sleeps.clear()
    srv2_router = router
    srv2_router.post(URL).mock(side_effect=flaky(srv, [429], headers={"Retry-After": "9999"}))
    make(settings, conn, ft).call_tool("t")
    assert ft.sleeps == [60.0]


def test_gives_up_after_three_tries(router, settings, conn, ft):
    n = {"calls": 0}

    def handle(request):
        n["calls"] += 1
        return httpx.Response(500)

    router.post(URL).mock(side_effect=handle)
    with pytest.raises(UpworkHTTPError) as e:
        make(settings, conn, ft).call_tool("t")
    assert e.value.status == 500
    assert n["calls"] == 3
    assert ft.sleeps == [1.0, 2.0]


def test_network_error_is_retried(router, settings, conn, ft):
    srv = Server(router)
    srv.tool_replies = [ok_result({"ok": 1})]
    orig = srv.handle
    state = {"failed": False}

    def handle(request):
        if srv.body(request)["method"] == "initialize" and not state["failed"]:
            state["failed"] = True
            raise httpx.ConnectError("boom")
        return orig(request)

    router.post(URL).mock(side_effect=handle)
    assert make(settings, conn, ft).call_json("t") == {"ok": 1}
    assert ft.sleeps == [1.0]


def test_other_4xx_is_not_retried(router, settings, conn, ft):
    router.post(URL).mock(return_value=httpx.Response(400))
    with pytest.raises(UpworkHTTPError) as e:
        make(settings, conn, ft).call_tool("t")
    assert e.value.status == 400
    assert ft.sleeps == []


def test_one_request_per_second(router, settings, conn, ft):
    settings.set("upwork.mcp_calls_per_second", 1)
    srv = Server(router)
    srv.tool_replies = [ok_result({"ok": 1})]
    c = make(settings, conn, ft)
    c.call_tool("a")
    c.call_tool("b")
    # 4 requests (initialize, initialized, call, call): each starts >= 1s after the one before.
    assert len(srv.requests) == 4
    assert ft.sleeps == [1.0, 1.0, 1.0]


# ---------- budget ----------


def test_budget_cap_counts_in_runs_table(router, settings, conn, ft):
    settings.set("upwork.max_mcp_calls_per_run", 3)
    srv = Server(router)
    srv.tool_replies = [ok_result({"ok": 1})]
    run = new_run(conn)
    c = make(settings, conn, ft, run_id=run)
    for _ in range(3):
        c.call_tool("t")
    assert conn.execute("SELECT mcp_calls FROM runs WHERE run_id=?", (run,)).fetchone()[0] == 3
    before = len(srv.requests)
    with pytest.raises(CallBudgetExceeded):
        c.call_tool("t")
    assert len(srv.requests) == before  # nothing was sent
    assert c.calls_made == 3


def test_budget_is_shared_between_client_objects(router, settings, conn, ft):
    settings.set("upwork.max_mcp_calls_per_run", 2)
    srv = Server(router)
    srv.tool_replies = [ok_result({"ok": 1})]
    run = new_run(conn)
    make(settings, conn, ft, run_id=run).call_tool("t")
    make(settings, conn, ft, run_id=run).call_tool("t")
    with pytest.raises(CallBudgetExceeded):
        make(settings, conn, ft, run_id=run).call_tool("t")


def test_budget_without_run_id_uses_memory(router, settings, conn, ft):
    settings.set("upwork.max_mcp_calls_per_run", 1)
    srv = Server(router)
    srv.tool_replies = [ok_result({"ok": 1})]
    c = make(settings, conn, ft)
    c.call_tool("t")
    with pytest.raises(CallBudgetExceeded):
        c.call_tool("t")


def test_default_budget_is_forty(settings):
    assert settings.get("upwork.max_mcp_calls_per_run") == 40


def test_retries_and_handshake_do_not_use_budget(router, settings, conn, ft):
    srv = Server(router)
    srv.tool_replies = [ok_result({"ok": 1})]
    router.post(URL).mock(side_effect=flaky(srv, [429, 429]))
    run = new_run(conn)
    make(settings, conn, ft, run_id=run).call_tool("t")
    assert conn.execute("SELECT mcp_calls FROM runs WHERE run_id=?", (run,)).fetchone()[0] == 1


def test_list_tools_counts_as_a_call(router, settings, conn, ft):
    Server(router)
    run = new_run(conn)
    make(settings, conn, ft, run_id=run).list_tools()
    assert conn.execute("SELECT mcp_calls FROM runs WHERE run_id=?", (run,)).fetchone()[0] == 1


def test_unknown_run_id(router, settings, conn, ft):
    Server(router)
    from app.upwork.client import UpworkMCPError

    with pytest.raises(UpworkMCPError, match="Unknown run"):
        make(settings, conn, ft, run_id="nope").call_tool("t")


# ---------- dry-run ----------


def test_confirm_preview_blocked_in_dry_run(router, settings, conn, ft):
    srv = Server(router)
    assert settings.get("general.dry_run") is True  # the default
    run = new_run(conn)
    c = make(settings, conn, ft, run_id=run)
    with pytest.raises(DryRunBlocked):
        c.call_tool("confirm_preview", {"preview_id": "p1"})
    assert srv.requests == []  # nothing left the machine
    assert conn.execute("SELECT mcp_calls FROM runs WHERE run_id=?", (run,)).fetchone()[0] == 0


def test_confirm_preview_allowed_when_dry_run_off(router, settings, conn, ft):
    settings.set("general.dry_run", False)
    srv = Server(router)
    srv.tool_replies = [ok_result({"confirmed": True})]
    assert make(settings, conn, ft).call_json("confirm_preview", {"preview_id": "p1"}) == {
        "confirmed": True
    }


def test_other_tools_work_in_dry_run(router, settings, conn, ft):
    srv = Server(router)
    srv.tool_replies = [ok_result({"preview_id": "p1"})]
    assert make(settings, conn, ft).call_json("create_proposal") == {"preview_id": "p1"}


def test_token_not_in_error_messages(router, settings, conn, ft):
    router.post(URL).mock(return_value=httpx.Response(500))
    with pytest.raises(UpworkHTTPError) as e:
        make(settings, conn, ft).call_tool("t")
    assert "tok-" not in str(e.value)
