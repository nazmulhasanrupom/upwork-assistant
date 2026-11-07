"""FastAPI app factory. Run with: uvicorn app.main:create_app --factory"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app import approvals, auth, playbook, skills, versioning
from app.approvals_routes import router as approvals_router
from app.auth_routes import router as auth_router
from app.chat_routes import router as chat_router
from app.config import Config, load_config
from app.context_routes import router as context_router
from app.db import init_db
from app.gmail_routes import router as gmail_router
from app.help import help_text
from app.inbox_routes import router as inbox_router
from app.job_routes import router as job_router
from app.learning_routes import router as learning_router
from app.live_routes import router as live_router
from app.llm import live
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
from app.worker import Worker
from app.worker_routes import router as worker_router

APP_DIR = Path(__file__).parent


def make_static_url(root: Path):
    """static_url('app.css') -> '/static/app.css?v=<hash>' so browsers reload changed files."""
    cache: dict[str, str] = {}

    def static_url(path: str) -> str:
        if path not in cache:
            try:
                digest = hashlib.sha256((root / path).read_bytes()).hexdigest()[:10]
            except OSError:
                digest = "0"
            cache[path] = f"/static/{path}?v={digest}"
        return cache[path]

    return static_url


def create_app(config: Config | None = None) -> FastAPI:
    config = config or load_config()
    logging.basicConfig(level=config.log_level.upper())

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        yield
        app.state.worker.stop()  # waits a little for running actions

    app = FastAPI(
        lifespan=lifespan,
        title="Upwork Assistant",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        dependencies=[Depends(auth.guard)],  # CSRF on writes, login on non-public pages
    )
    app.state.config = config
    app.state.db = init_db(config.db_path)  # runs migrations on start
    app.state.settings = Settings(app.state.db)
    app.state.worker = Worker(app.state.db, app.state.settings, config.db_path)
    app.state.worker.start()  # actions left from the last run become "stopped", never re-run
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

    def _safe(fn, fallback):
        """A template helper that never breaks a page."""

        def call(*args):
            try:
                return fn(*args)
            except Exception:
                logging.getLogger("worker").exception("Could not read the actions")
                return fallback

        return call

    def _flag(key: str):
        def read() -> bool:
            try:
                return bool(app.state.settings.get(key))
            except Exception:
                return False

        return read

    app.state.templates.env.globals["approvals_pending"] = approvals_pending
    app.state.templates.env.globals["nav_dry_run"] = _flag("general.dry_run")
    app.state.templates.env.globals["nav_kill_switch"] = _flag("general.kill_switch")
    app.state.templates.env.globals["actions_summary"] = _safe(
        lambda: app.state.worker.summary(), {"working": [], "recent": [], "count": 0, "poll": 2}
    )
    app.state.templates.env.globals["action_row"] = _safe(lambda i: app.state.worker.get(i), None)
    app.state.templates.env.globals["worker_poll"] = _safe(
        lambda: max(1, int(app.state.settings.get("worker.poll_seconds"))), 2
    )

    def claude_dock() -> dict:
        """Running Claude jobs for the dock, and how often to ask again."""
        st = app.state.settings
        try:
            jobs = live.running_rows(app.state.db, config.data_dir, st)
        except Exception:
            logging.getLogger("claude.live").exception("Could not read the Claude jobs")
            jobs = []
        key = "live.poll_seconds" if jobs else "live.idle_poll_seconds"
        return {"jobs": jobs, "poll": max(1, int(st.get(key)))}

    app.state.templates.env.globals["claude_dock"] = claude_dock
    app.state.templates.env.globals["help_text"] = help_text
    app.state.templates.env.globals["static_url"] = make_static_url(APP_DIR / "static")
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
    app.include_router(chat_router)
    app.include_router(approvals_router)
    app.include_router(inbox_router)
    app.include_router(profile_router)
    app.include_router(playbook_router)
    app.include_router(learning_router)
    app.include_router(trends_router)
    app.include_router(worker_router)
    app.include_router(live_router)
    app.include_router(pages_router)

    @app.get("/healthz")
    def healthz() -> dict[str, bool]:
        return {"ok": True}

    return app
