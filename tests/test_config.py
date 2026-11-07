import stat
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.config import MIN_SECRET_LENGTH, ConfigError, load_config

GOOD = "x" * 40


def test_good_key_loads():
    cfg = load_config({"SECRET_KEY": GOOD, "PUBLIC_BASE_URL": "https://a.example/"})
    assert cfg.secret_key == GOOD
    assert cfg.public_base_url == "https://a.example"
    assert str(cfg.data_dir) == "/data"
    assert cfg.tz == "UTC"
    assert cfg.log_level == "info"


def test_data_dir_override():
    cfg = load_config({"SECRET_KEY": GOOD, "DATA_DIR": "/tmp/x"})
    assert cfg.db_path.as_posix() == "/tmp/x/app.db"


@pytest.mark.parametrize("value", ["short", "x" * 31, "change-me-long-random-string"])
def test_bad_env_key_refused(value, tmp_path):
    with pytest.raises(ConfigError):
        load_config({"SECRET_KEY": value, "DATA_DIR": str(tmp_path)})


@pytest.mark.parametrize("value", [None, ""])
def test_no_key_makes_one(value, tmp_path):
    env = {"DATA_DIR": str(tmp_path)}
    if value is not None:
        env["SECRET_KEY"] = value
    cfg = load_config(env)
    path = tmp_path / "secret_key"
    assert len(cfg.secret_key) >= MIN_SECRET_LENGTH
    assert path.read_text() == cfg.secret_key
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_key_is_reused(tmp_path):
    env = {"DATA_DIR": str(tmp_path)}
    assert load_config(env).secret_key == load_config(env).secret_key


def test_env_key_wins_and_writes_no_file(tmp_path):
    (tmp_path / "secret_key").write_text("f" * 50)
    cfg = load_config({"SECRET_KEY": GOOD, "DATA_DIR": str(tmp_path)})
    assert cfg.secret_key == GOOD
    empty = tmp_path / "other"
    load_config({"SECRET_KEY": GOOD, "DATA_DIR": str(empty)})
    assert not empty.exists()


def test_existing_key_file_is_read(tmp_path):
    (tmp_path / "secret_key").write_text("abc" * 20 + "\n")
    assert load_config({"DATA_DIR": str(tmp_path)}).secret_key == "abc" * 20


def test_short_key_file_refused(tmp_path):
    (tmp_path / "secret_key").write_text("tiny")
    with pytest.raises(ConfigError):
        load_config({"DATA_DIR": str(tmp_path)})


def test_key_race_all_processes_get_the_same_key(tmp_path):
    env = {"DATA_DIR": str(tmp_path / "new")}
    with ThreadPoolExecutor(8) as pool:
        keys = {f.result().secret_key for f in [pool.submit(load_config, env) for _ in range(16)]}
    assert len(keys) == 1


def test_key_is_not_logged(tmp_path, caplog):
    caplog.set_level("DEBUG")
    cfg = load_config({"DATA_DIR": str(tmp_path)})
    assert cfg.secret_key not in caplog.text


def test_unwritable_data_dir_is_a_clear_error(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    with pytest.raises(ConfigError):
        load_config({"DATA_DIR": str(blocker / "sub")})
