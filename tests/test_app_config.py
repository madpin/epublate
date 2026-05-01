"""Tests for :mod:`epublate.app.config` (PRD §4.6 / M6 / F-STYLE-4)."""

from __future__ import annotations

from pathlib import Path

import pytest

from epublate.app.config import (
    ENV_AUTO_TONE_SNIFF,
    UIConfig,
    resolve_auto_tone_sniff,
)


def test_load_returns_empty_when_file_missing(tmp_path: Path) -> None:
    cfg = UIConfig.load(tmp_path / "missing.toml")
    assert cfg.theme is None
    assert cfg.extras == {}


def test_save_then_load_round_trips_theme(tmp_path: Path) -> None:
    cfg_path = tmp_path / "ui.toml"
    cfg = UIConfig(theme="textual-light")
    cfg.save(cfg_path)
    assert cfg_path.is_file()

    reloaded = UIConfig.load(cfg_path)
    assert reloaded.theme == "textual-light"


def test_save_is_atomic_no_tmp_left_behind(tmp_path: Path) -> None:
    cfg_path = tmp_path / "ui.toml"
    UIConfig(theme="epublate-contrast").save(cfg_path)
    assert cfg_path.is_file()
    siblings = list(tmp_path.iterdir())
    assert all(p.suffix != ".tmp" for p in siblings)


def test_load_ignores_malformed_file(tmp_path: Path) -> None:
    cfg_path = tmp_path / "ui.toml"
    cfg_path.write_text("this is = not valid toml [[", encoding="utf-8")
    cfg = UIConfig.load(cfg_path)
    assert cfg.theme is None


def test_save_escapes_quotes_and_backslashes(tmp_path: Path) -> None:
    cfg_path = tmp_path / "ui.toml"
    UIConfig(theme='weird"theme\\name').save(cfg_path)
    reloaded = UIConfig.load(cfg_path)
    assert reloaded.theme == 'weird"theme\\name'


# ---------------------------------------------------------------------------
# auto_tone_sniff round-trip + env override (PRD F-STYLE-4)
# ---------------------------------------------------------------------------


def test_auto_tone_sniff_defaults_on() -> None:
    cfg = UIConfig()
    assert cfg.auto_tone_sniff is True


def test_auto_tone_sniff_round_trips_false(tmp_path: Path) -> None:
    cfg_path = tmp_path / "ui.toml"
    UIConfig(theme="textual-dark", auto_tone_sniff=False).save(cfg_path)
    reloaded = UIConfig.load(cfg_path)
    assert reloaded.auto_tone_sniff is False


def test_auto_tone_sniff_persists_alongside_theme(tmp_path: Path) -> None:
    cfg_path = tmp_path / "ui.toml"
    UIConfig(theme="textual-light", auto_tone_sniff=False).save(cfg_path)
    body = cfg_path.read_text(encoding="utf-8")
    assert 'theme = "textual-light"' in body
    assert "auto_tone_sniff = false" in body


def test_auto_tone_sniff_load_handles_legacy_files(tmp_path: Path) -> None:
    """Older ui.toml files without the toggle must default to ``True``."""

    cfg_path = tmp_path / "ui.toml"
    cfg_path.write_text(
        '# epublate UI preferences\n[ui]\ntheme = "textual-dark"\n',
        encoding="utf-8",
    )
    cfg = UIConfig.load(cfg_path)
    assert cfg.theme == "textual-dark"
    assert cfg.auto_tone_sniff is True


def test_resolve_auto_tone_sniff_returns_config_when_env_unset() -> None:
    cfg = UIConfig(auto_tone_sniff=False)
    assert resolve_auto_tone_sniff(cfg, env={}) is False
    assert resolve_auto_tone_sniff(UIConfig(auto_tone_sniff=True), env={}) is True


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "On"])
def test_resolve_auto_tone_sniff_env_truthy_wins(value: str) -> None:
    cfg = UIConfig(auto_tone_sniff=False)
    assert resolve_auto_tone_sniff(cfg, env={ENV_AUTO_TONE_SNIFF: value}) is True


@pytest.mark.parametrize("value", ["0", "false", "FALSE", "no", "Off"])
def test_resolve_auto_tone_sniff_env_falsy_wins(value: str) -> None:
    cfg = UIConfig(auto_tone_sniff=True)
    assert resolve_auto_tone_sniff(cfg, env={ENV_AUTO_TONE_SNIFF: value}) is False


def test_resolve_auto_tone_sniff_ignores_garbage_env() -> None:
    cfg = UIConfig(auto_tone_sniff=True)
    # A typo must not silently flip the toggle in either direction.
    assert resolve_auto_tone_sniff(cfg, env={ENV_AUTO_TONE_SNIFF: "maybe"}) is True
    cfg2 = UIConfig(auto_tone_sniff=False)
    assert resolve_auto_tone_sniff(cfg2, env={ENV_AUTO_TONE_SNIFF: ""}) is False
