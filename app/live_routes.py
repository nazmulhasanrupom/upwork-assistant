"""Routes for the Claude jobs dock (plan L1). Login and CSRF are checked by the app-wide guard.

- GET  /claude/dock            the dock part (HTMX partial, polled)
- POST /claude/{id}/cancel     stop one running claude process and everything linked to it
"""

from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse, Response

from app.auth_routes import render
from app.llm import live

router = APIRouter()

MESSAGES = {
    "stopped": "Cancelled. The job was stopped.",
    "requested": "Cancel sent. The job stops in a moment.",
    "gone": "That job has already finished.",
}


@router.get("/claude/dock")
def dock(request: Request) -> Response:
    return render(request, "_claude_dock.html")


@router.post("/claude/{live_id}/cancel")
def cancel(request: Request, live_id: str) -> Response:
    app = request.app
    if not live.valid_id(live_id):
        raise HTTPException(status_code=404, detail="Unknown job")
    grace = float(app.state.settings.get("live.cancel_grace_seconds"))
    result = live.request_cancel(app.state.db, app.state.config.data_dir, live_id, grace)
    if result == "unknown":
        raise HTTPException(status_code=404, detail="Unknown job")
    if request.headers.get("hx-request"):
        return render(request, "_claude_dock.html")
    return RedirectResponse(f"/?msg={quote(MESSAGES[result])}", status_code=303)
