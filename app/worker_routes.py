"""Routes for the in-app worker (plan W1). Login is checked by the app-wide guard.

- GET /actions        the top bar part: "Working (n)" list and notices for finished actions
- GET /actions/{id}   one action as a small status (HTMX partial). While it runs the partial asks
                      again every few seconds. When it is done: HX-Redirect to its result_url,
                      or HX-Refresh when it has none.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response

from app.auth_routes import render
from app.worker import safe_local_url

router = APIRouter()


@router.get("/actions")
def actions_bar(request: Request) -> Response:
    return render(request, "_actions_bar.html")


@router.get("/actions/{action_id}")
def action_status(request: Request, action_id: str) -> Response:
    action = request.app.state.worker.get(action_id)
    if action is None:
        raise HTTPException(status_code=404, detail="Unknown action")
    response = render(request, "_action.html", action_id=action_id)
    if action["status"] == "done" and request.headers.get("hx-request"):
        url = safe_local_url(action["result_url"])
        if url:
            response.headers["HX-Redirect"] = url
        else:
            response.headers["HX-Refresh"] = "true"
    return response
