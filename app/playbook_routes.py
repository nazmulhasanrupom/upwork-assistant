"""Playbook page `/playbook` (plan O2): the tactic table, the tests and why a test is blocked.

Read-only. Starting and finishing tests belongs to the test loop (plan O3).
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Request
from fastapi.responses import Response

from app import playbook
from app.auth_routes import render

router = APIRouter()


@router.get("/playbook")
def playbook_page(request: Request) -> Response:
    s = request.app.state
    data = playbook.page_data(s.db, s.settings, s.config, datetime.now(UTC))
    return render(request, "playbook.html", **data)
