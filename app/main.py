"""FastAPI app factory. Run with: uvicorn app.main:create_app --factory"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app import approvals, auth, playbook, skills, versioning
from app.approvals_routes import router as approvals_router
from app.auth_routes import router as auth_router
from app.config import Config, load_config
from app.context_routes import router as context_router
from app.db import init_db
from app.gmail_routes import router as gmail_router
from app.inbox_routes import router as inbox_router
from app.job_routes import router as job_router
from app.learning_routes import router as learning_router
from app.pages import install_filters
from app.pages import router as pages_router
from app.playbook_routes import router as playbook_router
from app.profile_routes import router as profile_router
from app.review.routes import router as review_router
from app.runs_routes import router as runs_router
from app.settings import Settings
from app.settings_routes import router as settings_router
from app.setup_routes import router as setup_router
from app.trends_routes import router as trends_router
from app.upwork.routes import router as upwork_router

APP_DIR = Path(__file__).parent


def create_app(config: Config | None = None) -> FastAPI:
    config = config or load_config()
    logging.basicConfig(level=config.log_level.upper())

    app = FastAPI(
        title="Upwork Assistant",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        dependencies=[Depends(auth.guard)],  # CSRF on writes, login on non-public pages
    )
    app.state.config = config
    app.state.db = init_db(config.db_path)  # runs migrations on start
    app.state.settings = Settings(app.state.db)
    if not auth.admin_exists(app.state.db):
        # Plain print: it must show even when LOG_LEVEL is high. Only the hash is stored.
        code = auth.issue_setup_code(app.state.db, config.secret_key)
        print(f"SETUP CODE: {code}", flush=True)
    try:
        versioning.ensure_repo(config, app.state.settings)
    except Exception:
        logging.getLogger("versioning").exception("Could not start the version history")
    try:
        skills.ensure_skills(config, app.state.settings)
    except OSError:
        logging.getLogger("skills").exception("Could not copy the starter skills")
    try:
        playbook.ensure_seeded(config, settings=app.state.settings)
    except OSError:
        logging.getLogger("playbook").exception("Could not seed the playbook")
    app.state.templates = Jinja2Templates(directory=str(APP_DIR / "templates"))
    install_filters(app.state.templates, config.tz)

    def approvals_pending() -> int:
        """The number in the nav: items waiting for a click. Never breaks a page."""
        try:
            return approvals.pending_count(app.state.db, app.state.settings)
        except Exception:
            logging.getLogger("approvals").exception("Could not count pending approvals")
            return 0

    app.state.templates.env.globals["approvals_pending"] = approvals_pending
    app.mount("/static", StaticFiles(directory=str(APP_DIR / "static")), name="static")

    @app.middleware("http")
    async def session_cookie(request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        session = getattr(request.state, "session", None)
        if session is not None and session.dirty:
            flags = {"secure": True, "httponly": True, "samesite": "strict", "path": "/"}
            if session.data:
                hours = app.state.settings.get("security.session_hours")
                response.set_cookie(
                    auth.COOKIE_NAME,
                    auth.cookie_value(request, session),
                    max_age=hours * 3600,
                    **flags,
                )
            else:
                response.delete_cookie(auth.COOKIE_NAME, **flags)
        return response

    app.include_router(auth_router)
    app.include_router(setup_router)
    app.include_router(upwork_router)
    app.include_router(gmail_router)
    app.include_router(settings_router)
    app.include_router(context_router)
    app.include_router(runs_router)
    app.include_router(review_router)
    app.include_router(job_router)
    app.include_router(approvals_router)
    app.include_router(inbox_router)
    app.include_router(profile_router)
    app.include_router(playbook_router)
    app.include_router(learning_router)
    app.include_router(trends_router)
    app.include_router(pages_router)

    @app.get("/healthz")
    def healthz() -> dict[str, bool]:
        return {"ok": True}

    return app
