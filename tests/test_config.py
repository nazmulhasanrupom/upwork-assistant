import pytest

from app.config import ConfigError, load_config

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


@pytest.mark.parametrize(
    "value", [None, "", "short", "x" * 31, "change-me-long-random-string"]
)
def test_bad_key_refused(value):
    env = {} if value is None else {"SECRET_KEY": value}
    with pytest.raises(ConfigError):
        load_config(env)
