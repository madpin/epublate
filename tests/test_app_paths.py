"""Unit tests for ``epublate.app.paths``."""

from __future__ import annotations

from pathlib import Path

import pytest

from epublate.app.config import UIConfig
from epublate.app.paths import (
    CONFIG_KEY_PROJECTS_ROOT,
    ENV_PROJECTS_ROOT,
    default_projects_root,
    ensure_projects_root,
    quick_locations,
    set_projects_root,
    unique_project_dir,
    unique_project_name,
    xdg_data_home,
)


def test_default_projects_root_honors_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "my-root"
    target.mkdir()
    monkeypatch.setenv(ENV_PROJECTS_ROOT, str(target))
    assert default_projects_root() == target.resolve()


def test_default_projects_root_prefers_stored_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # With no env var, ``ui.toml`` takes precedence over the platform
    # fallback so curators can pin a custom drive from the Settings
    # screen.
    monkeypatch.delenv(ENV_PROJECTS_ROOT, raising=False)
    config_path = tmp_path / "ui.toml"
    cfg = UIConfig()
    custom = tmp_path / "data-root"
    cfg.extras[CONFIG_KEY_PROJECTS_ROOT] = str(custom)
    cfg.save(config_path)
    reloaded = UIConfig.load(config_path)
    assert default_projects_root(ui_config=reloaded) == custom.resolve()


def test_default_projects_root_falls_back_to_xdg_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(ENV_PROJECTS_ROOT, raising=False)
    data_home = tmp_path / "data"
    data_home.mkdir()
    monkeypatch.setenv("XDG_DATA_HOME", str(data_home))
    # Make Documents-home absent by pointing HOME at tmp_path.
    monkeypatch.setenv("HOME", str(tmp_path))
    empty_cfg = UIConfig()
    assert default_projects_root(ui_config=empty_cfg) == (data_home / "epublate")


def test_set_projects_root_persists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "ui.toml"
    monkeypatch.delenv(ENV_PROJECTS_ROOT, raising=False)
    target = tmp_path / "store"
    set_projects_root(target, config_path=config_path)
    cfg = UIConfig.load(config_path)
    assert cfg.extras.get(CONFIG_KEY_PROJECTS_ROOT) == str(target.resolve())


def test_ensure_projects_root_creates_dir(tmp_path: Path) -> None:
    target = tmp_path / "deep" / "projects"
    out = ensure_projects_root(target)
    assert out.is_dir()


def test_unique_project_dir_returns_base_when_absent(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    picked = unique_project_dir(root, "Dictator")
    assert picked == root / "Dictator"


def test_unique_project_dir_handles_collisions(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    (root / "Dictator").mkdir()
    (root / "Dictator" / "junk.txt").write_text("x")
    (root / "Dictator-2").mkdir()
    (root / "Dictator-2" / "junk.txt").write_text("x")
    picked = unique_project_dir(root, "Dictator")
    assert picked == root / "Dictator-3"


def test_unique_project_dir_sanitizes_weird_stems(tmp_path: Path) -> None:
    # Paths with spaces / punctuation survive as shell-friendly stems.
    picked = unique_project_dir(tmp_path, "A Weird: Book / Name")
    assert picked.name == "A-Weird-Book-Name"


def test_unique_project_dir_sanitizes_empty_stem(tmp_path: Path) -> None:
    picked = unique_project_dir(tmp_path, "???")
    assert picked.name == "project"


def test_unique_project_name_returns_input_when_free() -> None:
    assert unique_project_name("Dictator", existing_names=["Other"]) == "Dictator"


def test_unique_project_name_appends_numeric_suffix() -> None:
    assert (
        unique_project_name("Dictator", existing_names=["dictator"]) == "Dictator (2)"
    )


def test_unique_project_name_respects_existing_suffix() -> None:
    assert (
        unique_project_name("Book (2)", existing_names=["book (2)", "book (3)"])
        == "Book (4)"
    )


def test_unique_project_name_trims_whitespace() -> None:
    assert unique_project_name("  Sample ", existing_names=["sample"]) == "Sample (2)"


def test_quick_locations_filters_missing_dirs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_home = tmp_path / "home"
    (fake_home / "Downloads").mkdir(parents=True)
    (fake_home / "Documents").mkdir()
    # Desktop deliberately omitted — quick_locations should drop it.
    monkeypatch.setenv("HOME", str(fake_home))
    locs = quick_locations(cwd=tmp_path)
    labels = [loc.label for loc in locs]
    assert "Current" in labels
    assert "Home" in labels
    assert "Documents" in labels
    assert "Downloads" in labels
    assert "Desktop" not in labels


def test_quick_locations_dedups_overlapping_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # If ``cwd == HOME`` we only emit one of them rather than both.
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    locs = quick_locations(cwd=fake_home)
    seen = {loc.path for loc in locs}
    assert fake_home.resolve() in seen
    # Exactly one entry for that path (either Current or Home).
    assert sum(1 for loc in locs if loc.path == fake_home.resolve()) == 1


def test_xdg_data_home_uses_env_if_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    assert xdg_data_home() == tmp_path / "xdg"
