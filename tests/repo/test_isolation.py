"""Every test runs under its own HOME and XDG folders, with a vault named in its config, and the
database tests still reach Postgres: libpq is pointed at the real ~/.pgpass."""

import os
import pwd
import tomllib
from pathlib import Path

import pytest

from riffle import config

pytest_plugins = ["pytester"]
ROOT = Path(__file__).resolve().parents[2]
REAL_PGPASS = """\
import os
import pwd
from pathlib import Path


def test_libpq_reads_the_real_pgpass():
    real = Path(pwd.getpwuid(os.getuid()).pw_dir)
    assert Path.home() != real
    assert os.environ["PGPASSFILE"] == str(real / ".pgpass")
"""
OWN_PGPASS = """\
import os


def test_the_user_s_own_setting_is_kept():
    assert os.environ["PGPASSFILE"] == "/somewhere/else/.pgpass"
"""


def real_home() -> Path:
    """The user's home from the password database, whatever HOME says."""
    return Path(pwd.getpwuid(os.getuid()).pw_dir)


def test_home_and_the_xdg_folders_are_the_test_s_own(tmp_path_factory):
    base = tmp_path_factory.getbasetemp()
    assert Path.home() != real_home()
    assert Path.home().is_relative_to(base)
    assert Path(os.environ["HOME"]) == Path.home()
    assert config.config_path().is_relative_to(base) and config.data_dir().is_relative_to(base)
    assert config.load().downloads.is_relative_to(base)  # ~/Downloads is the scratch home's


def test_the_config_names_a_vault_under_the_test_s_folder(tmp_path_factory):
    assert tomllib.loads(config.config_path().read_text(encoding="utf-8"))["vault"]
    cfg = config.load()
    assert cfg.vault is not None and cfg.vault.is_relative_to(tmp_path_factory.getbasetemp())


def test_libpq_s_password_file_is_never_under_the_test_s_home():
    pgpass = os.environ.get("PGPASSFILE")
    assert pgpass, "PGPASSFILE unset: libpq would look under the test's HOME"
    assert not Path(pgpass).is_relative_to(Path.home())


def test_the_fixture_points_libpq_at_the_real_pgpass_only_when_unset(pytester, monkeypatch):
    pytester.makeconftest((ROOT / "tests" / "conftest.py").read_text(encoding="utf-8"))
    pytester.makepyfile(test_real=REAL_PGPASS, test_own=OWN_PGPASS)
    monkeypatch.delenv("PGPASSFILE", raising=False)
    pytester.runpytest_inprocess("-p", "no:cacheprovider", "-q", "test_real.py").assert_outcomes(passed=1)
    monkeypatch.setenv("PGPASSFILE", "/somewhere/else/.pgpass")
    pytester.runpytest_inprocess("-p", "no:cacheprovider", "-q", "test_own.py").assert_outcomes(passed=1)


@pytest.mark.postgres
def test_the_database_tests_still_connect_under_the_fixture(pg):
    from sqlalchemy import text

    assert Path.home() != real_home()
    assert pg.execute(text("select 1")).scalar() == 1
