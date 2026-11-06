from app.db import MIGRATIONS_DIR, connect, current_version, init_db, migrate

LATEST = max(int(p.name.split("_")[0]) for p in MIGRATIONS_DIR.glob("*.sql"))

SPEC_TABLES = {
    "settings", "jobs", "skills_seen", "judgements", "drafts", "approvals",
    "invites", "messages", "runs", "claude_calls", "emails", "schema_version",
}


def tables(conn):
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    return {r["name"] for r in rows}


def test_all_spec_tables_created():
    conn = init_db(":memory:")
    assert SPEC_TABLES <= tables(conn)
    assert current_version(conn) == LATEST


def test_migrations_idempotent():
    conn = connect(":memory:")
    assert migrate(conn) == list(range(1, LATEST + 1))
    assert migrate(conn) == []
    assert conn.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0] == LATEST


def test_new_migration_applied_and_data_kept(tmp_path):
    conn = connect(":memory:")
    migrate(conn)
    conn.execute("INSERT INTO settings(key, value) VALUES ('a', '1')")
    mdir = tmp_path / "m"
    mdir.mkdir()
    (mdir / "001_init.sql").write_text("SELECT 1;")
    (mdir / "900_extra.sql").write_text("CREATE TABLE extra (id INTEGER);")
    assert migrate(conn, mdir) == [900]
    assert "extra" in tables(conn)
    assert conn.execute("SELECT value FROM settings WHERE key='a'").fetchone()[0] == "1"
    assert current_version(conn) == 900


def test_file_db_persists(tmp_path):
    path = tmp_path / "sub" / "app.db"
    init_db(path).close()
    conn = init_db(path)
    assert current_version(conn) == LATEST
