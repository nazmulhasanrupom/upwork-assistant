"""Find the freelancer (TALENT) account with `list_accounts` (SPEC 3.1, plan F0a).

`list_accounts` takes no arguments and returns every account of the user with a role:
TALENT (freelancer), CLIENT, or FL_AGENCY. Only TALENT is used.

- exactly one TALENT: saved as `upwork.talent_org_uid` + `upwork.talent_name`
- several: saved as `upwork.talent_candidates`; the admin picks one on /upwork
- none: nothing is changed and the result is an error message
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from app.settings import Settings

log = logging.getLogger("upwork.accounts")

TOOL = "list_accounts"
ROLE_TALENT = "TALENT"

MSG_NONE = "No freelancer account found. Agency accounts are not supported."
MSG_MANY = "More than one freelancer account was found. Pick the one to use."
NAME_MAX = 200


class AccountError(RuntimeError):
    """Safe to show to the admin."""


@dataclass
class DetectResult:
    status: str  # saved | choose | none
    message: str
    accounts: list[dict[str, str]] = field(default_factory=list)


def parse_accounts(payload: Any) -> list[dict[str, str]]:
    """Keep only TALENT accounts that have an org_uid. Returns [{org_uid, name}]."""
    raw = payload.get("accounts") if isinstance(payload, dict) else payload
    if not isinstance(raw, list):
        raise AccountError("Upwork sent an answer to list_accounts that was not understood.")
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict) or str(item.get("role", "")).upper() != ROLE_TALENT:
            continue
        org_uid = str(item.get("org_uid") or "").strip()
        if not org_uid or org_uid in seen:
            continue
        seen.add(org_uid)
        name = str(item.get("name") or "").strip()[:NAME_MAX] or "Freelancer account"
        out.append({"org_uid": org_uid, "name": name})
    return out


def detect_and_save(client: Any, settings: Settings) -> DetectResult:
    """Call list_accounts once and store the outcome. Tool/network errors are not caught here."""
    accounts = parse_accounts(client.call_json(TOOL, {}))
    if not accounts:
        return DetectResult("none", MSG_NONE)
    if len(accounts) == 1:
        choose(settings, accounts[0])
        settings.set("upwork.talent_candidates", [])
        return DetectResult("saved", f"Using freelancer account: {accounts[0]['name']}.", accounts)
    settings.set("upwork.talent_candidates", accounts)
    return DetectResult("choose", MSG_MANY, accounts)


def choose(settings: Settings, account: dict[str, str]) -> None:
    settings.set("upwork.talent_org_uid", account["org_uid"])
    settings.set("upwork.talent_name", account["name"])


def pick_candidate(settings: Settings, org_uid: str) -> dict[str, str]:
    """Save one of the stored candidates. Never trusts the posted value on its own."""
    for cand in settings.get("upwork.talent_candidates"):
        if isinstance(cand, dict) and cand.get("org_uid") == org_uid:
            choose(settings, {"org_uid": cand["org_uid"], "name": cand.get("name", "")})
            settings.set("upwork.talent_candidates", [])
            return cand
    raise AccountError("That account is not one of the detected freelancer accounts.")


def current(settings: Settings) -> dict[str, str] | None:
    org_uid = str(settings.get("upwork.talent_org_uid") or "")
    if not org_uid:
        return None
    return {"org_uid": org_uid, "name": str(settings.get("upwork.talent_name") or "")}


def candidates(settings: Settings) -> list[dict[str, str]]:
    return [c for c in settings.get("upwork.talent_candidates") if isinstance(c, dict)]
