"""Shared helpers for the jobs/tiers/flags tests: fixtures and a fake MCP client."""

from __future__ import annotations

import copy
import json
from pathlib import Path

from app.upwork.client import CallBudgetExceeded, ToolResult, UpworkToolError

FIX = Path(__file__).parent / "fixtures" / "upwork"


def load(name: str):
    return json.loads((FIX / name).read_text())


def to_result(name, payload):
    """What the Claude bridge gives back for one call: a ToolResult.

    `payload` is the tool's JSON answer. A UpworkToolError becomes an error result, a
    CallBudgetExceeded a "budget" error result. Any other exception is raised, as the bridge
    raises UpworkAuthError and Claude errors for the whole batch.
    """
    if isinstance(payload, CallBudgetExceeded):
        text = str(payload)
        content = [{"type": "text", "text": text}]
        return ToolResult(name, content, is_error=True, error_kind="budget")
    if isinstance(payload, UpworkToolError):
        return ToolResult(name, [{"type": "text", "text": str(payload)}], is_error=True)
    if isinstance(payload, Exception):
        raise payload
    return ToolResult(name, [{"type": "text", "text": json.dumps(payload)}])


class CallManyMixin:
    """`call_many` built on `call_json`: each call is answered in order."""

    def call_many(self, calls):
        out = []
        for name, args in calls:
            try:
                payload = self.call_json(name, args)
            except Exception as e:
                payload = e
            out.append(to_result(name, payload))
        return out


class FakeMCP(CallManyMixin):
    """Stands in for the Claude bridge. `search` maps a mode or title to a payload or Exception;
    `details` maps a job id to a payload or Exception (see `to_result` for what each does)."""

    def __init__(self, settings, search=None, details=None, accounts=None):
        self.accounts = accounts  # payload (or Exception) returned by list_accounts
        self.settings = settings
        self.search = search or {}
        self.details = details or {}
        self.calls: list[tuple[str, dict]] = []

    def close(self):
        pass

    def call_json(self, name, arguments=None):
        self.calls.append((name, copy.deepcopy(arguments)))
        if name == "list_accounts":
            if isinstance(self.accounts, Exception):
                raise self.accounts
            return copy.deepcopy(self.accounts)
        assert name == "find_jobs"
        action = arguments["action"]
        params = arguments["params"]
        if action == "smart_search":
            res = self.search.get(params["mode"], {"status": "ok", "jobs": []})
        elif action == "search":
            res = self.search.get(params["title"], {"status": "ok", "jobs": []})
        else:
            res = self.details[params["job_id"]]
        if isinstance(res, Exception):
            raise res
        return copy.deepcopy(res)


def accounts_payload(*roles_and_names):
    """Build a list_accounts answer from fixture shape: [("TALENT", "Name", "uid"), ...]."""
    data = load("list_accounts.json")
    data["accounts"] = [
        {"name": n, "org_uid": u, "role": r, "role_label": r.title()} for r, n, u in roles_and_names
    ]
    return data


class FakeAccountsMCP(CallManyMixin):
    """Only list_accounts. `closed` shows the web code closed it."""

    def __init__(self, payload):
        self.payload = payload
        self.calls: list[tuple[str, dict]] = []
        self.closed = False

    def call_json(self, name, arguments=None):
        self.calls.append((name, arguments))
        if isinstance(self.payload, Exception):
            raise self.payload
        return copy.deepcopy(self.payload)

    def close(self):
        self.closed = True


__all__ = ["FakeAccountsMCP", "FakeMCP", "accounts_payload", "UpworkToolError", "load"]
