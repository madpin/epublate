"""Tests for :mod:`epublate.app.dotenv` (``.env`` loader for the CLI)."""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from epublate.app.dotenv import (
    ENV_DISABLE,
    _running_under_pytest,
    load_dotenv_files,
)

_ENV_VAR = "EPUBLATE_TEST_DOTENV_KEY"
_ENV_VAR_2 = "EPUBLATE_TEST_DOTENV_KEY_2"


@pytest.fixture(autouse=True)
def _scrub_env() -> Iterator[None]:
    """Ensure the loader's scratch keys don't leak across tests.

    ``load_dotenv()`` writes straight to ``os.environ``, so monkeypatch
    can't roll those changes back. We pop the keys at both entry and
    exit to keep the wider suite hermetic regardless of test order.
    """

    for key in (_ENV_VAR, _ENV_VAR_2, ENV_DISABLE):
        os.environ.pop(key, None)
    try:
        yield
    finally:
        for key in (_ENV_VAR, _ENV_VAR_2, ENV_DISABLE):
            os.environ.pop(key, None)


def _write_env(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def test_skips_under_pytest_by_default(tmp_path: Path) -> None:
    _write_env(tmp_path / ".env", f"{_ENV_VAR}=from-cwd\n")
    loaded = load_dotenv_files(cwd=tmp_path)
    assert loaded == []
    assert _ENV_VAR not in os.environ


def test_running_under_pytest_detected() -> None:
    assert _running_under_pytest() is True


def test_force_loads_cwd_env(tmp_path: Path) -> None:
    _write_env(tmp_path / ".env", f"{_ENV_VAR}=from-cwd\n")
    loaded = load_dotenv_files(cwd=tmp_path, force=True)
    assert loaded == [tmp_path / ".env"]
    assert os.environ[_ENV_VAR] == "from-cwd"


def test_force_loads_project_env_when_distinct(tmp_path: Path) -> None:
    cwd = tmp_path / "cwd"
    proj = tmp_path / "proj"
    _write_env(cwd / ".env", f"{_ENV_VAR}=from-cwd\n")
    _write_env(proj / ".env", f"{_ENV_VAR_2}=from-project\n")

    loaded = load_dotenv_files(cwd=cwd, project_dir=proj, force=True)

    assert loaded == [cwd / ".env", proj / ".env"]
    assert os.environ[_ENV_VAR] == "from-cwd"
    assert os.environ[_ENV_VAR_2] == "from-project"


def test_cwd_env_wins_over_project_env(tmp_path: Path) -> None:
    cwd = tmp_path / "cwd"
    proj = tmp_path / "proj"
    _write_env(cwd / ".env", f"{_ENV_VAR}=from-cwd\n")
    _write_env(proj / ".env", f"{_ENV_VAR}=from-project\n")

    load_dotenv_files(cwd=cwd, project_dir=proj, force=True)

    assert os.environ[_ENV_VAR] == "from-cwd"


def test_real_env_wins_over_dotenv(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(_ENV_VAR, "from-shell")
    _write_env(tmp_path / ".env", f"{_ENV_VAR}=from-cwd\n")

    load_dotenv_files(cwd=tmp_path, force=True)

    assert os.environ[_ENV_VAR] == "from-shell"


def test_missing_files_are_silent(tmp_path: Path) -> None:
    loaded = load_dotenv_files(
        cwd=tmp_path,
        project_dir=tmp_path / "does-not-exist",
        force=True,
    )
    assert loaded == []
    assert _ENV_VAR not in os.environ


def test_same_path_loaded_only_once(tmp_path: Path) -> None:
    _write_env(tmp_path / ".env", f"{_ENV_VAR}=from-cwd\n")
    loaded = load_dotenv_files(cwd=tmp_path, project_dir=tmp_path, force=True)
    assert loaded == [tmp_path / ".env"]


def test_disable_env_var_short_circuits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(ENV_DISABLE, "1")
    _write_env(tmp_path / ".env", f"{_ENV_VAR}=from-cwd\n")

    # Even outside pytest the disable flag must win — but ``force=True``
    # still bypasses both gates so loader unit tests remain feasible.
    monkeypatch.delenv("PYTEST_VERSION", raising=False)
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setattr("epublate.app.dotenv._running_under_pytest", lambda: False)

    loaded = load_dotenv_files(cwd=tmp_path)
    assert loaded == []
    assert _ENV_VAR not in os.environ

    forced = load_dotenv_files(cwd=tmp_path, force=True)
    assert forced == [tmp_path / ".env"]
    assert os.environ[_ENV_VAR] == "from-cwd"
