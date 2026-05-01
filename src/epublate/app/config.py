"""Per-user UI preferences for the TUI (PRD §4.6 / M6 / F-STYLE-4).

The TUI persists a small set of curator preferences (theme choice,
auto tone-sniff) under an XDG-style config path so they survive across
runs without leaking into the per-project SQLite DB. Anything that
belongs in a project (budget, models, glossary, the active tone
preset) lives in the DB; anything that's a per-machine UI preference
lives here.

We keep the file format intentionally boring — TOML with a small handful
of keys — and hand-roll the writer so we don't grow a runtime dependency
on a TOML serializer. ``tomllib`` is in the stdlib for reading.
"""

from __future__ import annotations

import contextlib
import logging
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

_logger = logging.getLogger(__name__)

CONFIG_DIRNAME = "epublate"
CONFIG_FILENAME = "ui.toml"

ENV_AUTO_TONE_SNIFF = "EPUBLATE_AUTO_TONE_SNIFF"
"""Per-machine override for :attr:`UIConfig.auto_tone_sniff`.

Accepts ``1`` / ``true`` / ``yes`` / ``on`` (case-insensitive) to force
auto-detection on, ``0`` / ``false`` / ``no`` / ``off`` to force it off.
Anything else (including unset) falls back to the persisted UIConfig
value. Lets curators turn the helper-LLM call off in CI / scripted
runs without editing ``~/.config/epublate/ui.toml``."""

_TRUTHY = {"1", "true", "yes", "on"}
_FALSY = {"0", "false", "no", "off"}


def xdg_config_home() -> Path:
    """Return ``$XDG_CONFIG_HOME`` falling back to ``~/.config`` (PRD NFR-2).

    Windows users get the same XDG-style fallback; Textual already ships
    cross-platform there and a single config root keeps the code path
    identical across OSes.
    """

    raw = os.environ.get("XDG_CONFIG_HOME", "").strip()
    if raw:
        return Path(raw)
    return Path.home() / ".config"


# Backwards-compatible alias for the prior single-underscore name. The
# helper escaped the module the moment we added a recents store; rename
# without breaking callers that already imported the private form.
_xdg_config_home = xdg_config_home


def default_config_path() -> Path:
    return xdg_config_home() / CONFIG_DIRNAME / CONFIG_FILENAME


@dataclass(slots=True)
class UIConfig:
    """In-memory snapshot of the user's UI preferences."""

    theme: str | None = None
    auto_tone_sniff: bool = True
    extras: dict[str, str] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path | None = None) -> UIConfig:
        cfg_path = path or default_config_path()
        if not cfg_path.is_file():
            return cls()
        try:
            with cfg_path.open("rb") as fh:
                data = tomllib.load(fh)
        except (OSError, tomllib.TOMLDecodeError) as exc:
            _logger.warning("ignoring malformed UI config %s: %s", cfg_path, exc)
            return cls()
        ui = data.get("ui") if isinstance(data, dict) else None
        if not isinstance(ui, dict):
            return cls()
        theme_raw = ui.get("theme")
        theme = theme_raw.strip() if isinstance(theme_raw, str) else None
        auto_sniff_raw = ui.get("auto_tone_sniff")
        auto_sniff = (
            bool(auto_sniff_raw)
            if isinstance(auto_sniff_raw, bool)
            else _coerce_bool_or_default(auto_sniff_raw, default=True)
        )
        extras = {
            k: str(v)
            for k, v in ui.items()
            if k not in {"theme", "auto_tone_sniff"}
            and isinstance(v, str | int | float | bool)
        }
        return cls(theme=theme or None, auto_tone_sniff=auto_sniff, extras=extras)

    def save(self, path: Path | None = None) -> Path:
        cfg_path = path or default_config_path()
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        body = self._render()
        tmp = cfg_path.with_suffix(cfg_path.suffix + ".tmp")
        try:
            tmp.write_text(body, encoding="utf-8")
            os.replace(tmp, cfg_path)
        except OSError:
            if tmp.exists():
                with contextlib.suppress(OSError):
                    tmp.unlink()
            raise
        return cfg_path

    def _render(self) -> str:
        # Hand-rolled TOML writer: ``ui.theme = "..."`` plus extras. The
        # entire file is one table so we don't need an arbitrary serializer.
        lines = ["# epublate UI preferences (auto-managed by the TUI)", "[ui]"]
        if self.theme:
            lines.append(f'theme = "{_escape(self.theme)}"')
        lines.append(f"auto_tone_sniff = {str(self.auto_tone_sniff).lower()}")
        for key in sorted(self.extras):
            lines.append(f'{key} = "{_escape(self.extras[key])}"')
        return "\n".join(lines) + "\n"


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _coerce_bool_or_default(value: object, *, default: bool) -> bool:
    """Map a TOML scalar to a bool, falling back to ``default`` on garbage."""

    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value != 0
    if isinstance(value, str):
        norm = value.strip().lower()
        if norm in _TRUTHY:
            return True
        if norm in _FALSY:
            return False
    return default


def resolve_auto_tone_sniff(
    ui_config: UIConfig,
    *,
    env: dict[str, str] | None = None,
) -> bool:
    """Return whether the New Project modal should auto-sniff tone.

    Resolution order (PRD F-STYLE-4):

    1. ``EPUBLATE_AUTO_TONE_SNIFF`` env var, if it parses to a bool.
    2. The persisted :attr:`UIConfig.auto_tone_sniff` value.

    The env var wins so curators can pin behavior in CI / scripted
    flows without rewriting their ``ui.toml``. Unparseable values
    are ignored — we never want a typo to silently flip the toggle.
    """

    env_map = env if env is not None else os.environ
    raw = env_map.get(ENV_AUTO_TONE_SNIFF)
    if raw is not None and raw.strip():
        norm = raw.strip().lower()
        if norm in _TRUTHY:
            return True
        if norm in _FALSY:
            return False
    return ui_config.auto_tone_sniff


__all__ = [
    "CONFIG_DIRNAME",
    "CONFIG_FILENAME",
    "ENV_AUTO_TONE_SNIFF",
    "UIConfig",
    "default_config_path",
    "resolve_auto_tone_sniff",
    "xdg_config_home",
]
