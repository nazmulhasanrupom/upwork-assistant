from fastapi.testclient import TestClient

from app.db import current_version
from app.main import create_app


def test_healthz(config):
    client = TestClient(create_app(config))
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"ok": True}


def test_migrations_run_on_start(config):
    app = create_app(config)
    assert current_version(app.state.db) >= 3
    assert (config.data_dir / "app.db").exists()


def test_htmx_vendored_and_base_layout(config):
    app = create_app(config)
    client = TestClient(app)
    r = client.get("/static/htmx.min.js")
    assert r.status_code == 200
    assert "htmx" in r.text
    html = app.state.templates.get_template("base.html").render()
    assert "/static/htmx.min.js" in html
    assert "unpkg" not in html and "cdn" not in html.lower()
