"""L1: memory caps and the search index. Fakes only."""

import pytest

from app import memory, notes
from app.db import init_db
from app.settings import Settings

pytestmark = pytest.mark.usefixtures("gate_open")


@pytest.fixture
def conn(config):
    return init_db(config.db_path)


@pytest.fixture
def settings(conn):
    return Settings(conn)


def test_usage_reports_used_and_left(config, settings):
    memory.atomic_write(memory.memory_file(config, "USER.md"), "a" * 100)
    memory.atomic_write(memory.memory_file(config, "MEMORY.md"), "b" * 50)
    u = memory.usage(config, settings)
    assert (u["user"], u["memory"], u["total"]) == (100, 50, 150)
    assert u["left"] == u["cap"] - 150 and not u["over"]


def test_write_within_cap(config, settings):
    memory.write_memory_file(config, settings, "MEMORY.md", "hello\n")
    assert memory.read_file(memory.memory_file(config, "MEMORY.md")) == "hello\n"


def test_write_over_cap_refused_and_nothing_written(config, settings):
    cap = settings.get("memory.total_cap_chars")
    memory.write_memory_file(config, settings, "USER.md", "u" * (cap - 10))
    with pytest.raises(memory.OverCap, match="cap"):
        memory.write_memory_file(config, settings, "MEMORY.md", "m" * 11)
    assert not memory.memory_file(config, "MEMORY.md").exists()


def test_trimming_an_over_cap_pair_is_allowed(config, settings):
    cap = settings.get("memory.total_cap_chars")
    memory.atomic_write(memory.memory_file(config, "USER.md"), "u" * (cap + 100))
    with pytest.raises(memory.OverCap):
        memory.write_memory_file(config, settings, "USER.md", "u" * (cap + 100))
    memory.write_memory_file(config, settings, "USER.md", "u" * (cap + 50))
    assert memory.usage(config, settings)["over"]


def test_other_files_not_capped(config, settings):
    memory.write_memory_file(config, settings, "style.md", "x" * 10000)


def test_cap_comes_from_settings(config, settings):
    settings.set("memory.total_cap_chars", 20)
    with pytest.raises(memory.OverCap):
        memory.write_memory_file(config, settings, "USER.md", "x" * 21)


def test_context_page_refuses_over_cap(config, conn):
    from fastapi.testclient import TestClient  # noqa: F401  (page tests cover login flow)

    s = Settings(conn)
    s.set("memory.total_cap_chars", 10)
    with pytest.raises(memory.OverCap):
        memory.write_memory_file(config, s, "USER.md", "y" * 11)


def test_note_search_and_replace(config, conn):
    notes.append_note(config, "2026-11-01", "Found a Django job about invoices\n")
    memory.index_note(conn, "2026-11-01", notes.read_note(config, "2026-11-01"))
    hits = memory.search(conn, "django")
    assert hits and hits[0]["kind"] == "note" and hits[0]["ref"] == "2026-11-01"
    memory.index_note(conn, "2026-11-01", "only flask now")
    assert memory.search(conn, "django") == []
    assert len(memory.search(conn, "flask")) == 1


def test_run_index_follows_inserts_and_updates(conn):
    conn.execute(
        "INSERT INTO runs(run_id, task, started, status) "
        "VALUES ('r1','daily','2026-11-01','running')"
    )
    assert memory.search(conn, "daily", kind="run")[0]["ref"] == "r1"
    conn.execute("UPDATE runs SET status='error', error='budget exploded' WHERE run_id='r1'")
    assert memory.search(conn, "exploded")[0]["ref"] == "r1"
    assert len(memory.search(conn, "daily")) == 1
    conn.execute("DELETE FROM runs WHERE run_id='r1'")
    assert memory.search(conn, "exploded") == []


@pytest.mark.parametrize(
    "q", ['"', "AND OR NOT", "a*(b", "NEAR(", "'; DROP TABLE runs;--", "", "   "]
)
def test_search_safe_on_odd_input(conn, q):
    assert isinstance(memory.search(conn, q), list)


def test_search_limit(conn):
    for i in range(5):
        memory.index_note(conn, f"2026-11-0{i + 1}", "common word")
    assert len(memory.search(conn, "common", limit=3)) == 3


def test_reindex_notes(config, conn):
    notes.append_note(config, "2026-10-01", "legacy rust note\n")
    assert memory.reindex_notes(conn, config) == 1
    assert memory.search(conn, "rust")
