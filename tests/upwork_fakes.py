"""Shared helpers for the jobs/tiers/flags tests: fixtures and a fake MCP client."""

from __future__ import annotations

import copy
import json
from pathlib import Path

from app.upwork.client import UpworkToolError

FIX = Path(__file__).parent / "fixtures" / "upwork"


def load(name: str):
    return json.loads((FIX / name).read_text())


class FakeMCP:
    """Stands in for UpworkMCP. `script` maps (action, mode-or-title) to a payload or Exception."""

    def __init__(self, settings, search=None, details=None, accounts=None):
        self.accounts = accounts  # payload (or Exception) returned by list_accounts
        self.settings = settings
        self.search = search or {}
        self.details = details or {}
        self.calls: list[tuple[str, dict]] = []

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


class FakeAccountsMCP:
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
