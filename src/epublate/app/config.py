"""Per-user UI preferences for the TUI (PRD §4.6 / M6 / F-STYLE-4).

The TUI persists a small set of curator preferences (theme choice,
auto tone-sniff, intake + concurrency defaults) under an XDG-style
config path so they survive across runs without leaking into the
per-project SQLite DB. Anything that belongs in a project (budget,
models, glossary, the active tone preset, per-project LLM overrides)
lives in the DB; anything that's a per-machine UI preference lives
here.

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

# Defaults for the new Settings panels. Pulled out as module constants
# so ``IntakeModal`` and the BatchModal can import them without dragging
# in a UIConfig instance just to read a number.
DEFAULT_INTAKE_MAX_SEGMENTS = 30
DEFAULT_BATCH_CONCURRENCY = 1
DEFAULT_BATCH_RETRIES = 1

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


_CORE_KEYS: frozenset[str] = frozenset(
    {
        "theme",
        "auto_tone_sniff",
        "intake_helper_model",
        "intake_max_segments",
        "intake_run_after_new",
        "batch_concurrency",
        "batch_retries",
    }
)


@dataclass(slots=True)
class UIConfig:
    """In-memory snapshot of the user's UI preferences.

    The ``intake_*`` and ``batch_*`` fields back the Settings screen's
    Intake and Concurrency panels (PRD §4.6 / M6). They land here
    because they are per-machine defaults: the curator might prefer
    ``concurrency=4`` on their workstation but ``concurrency=1`` on a
    laptop, regardless of which book they're translating. Per-project
    overrides for the LLM endpoint / models live on the project row
    instead (see :func:`epublate.db.repo.set_llm_overrides`).
    """

    theme: str | None = None
    auto_tone_sniff: bool = True
    intake_helper_model: str | None = None
    intake_max_segments: int = DEFAULT_INTAKE_MAX_SEGMENTS
    intake_run_after_new: bool = False
    batch_concurrency: int = DEFAULT_BATCH_CONCURRENCY
    batch_retries: int = DEFAULT_BATCH_RETRIES
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
        helper_raw = ui.get("intake_helper_model")
        helper = (
            helper_raw.strip()
            if isinstance(helper_raw, str) and helper_raw.strip()
            else None
        )
        intake_max = _coerce_positive_int(
            ui.get("intake_max_segments"), default=DEFAULT_INTAKE_MAX_SEGMENTS
        )
        run_after_raw = ui.get("intake_run_after_new")
        run_after = (
            bool(run_after_raw)
            if isinstance(run_after_raw, bool)
            else _coerce_bool_or_default(run_after_raw, default=False)
        )
        concurrency = _coerce_positive_int(
            ui.get("batch_concurrency"), default=DEFAULT_BATCH_CONCURRENCY
        )
        retries = _coerce_non_negative_int(
            ui.get("batch_retries"), default=DEFAULT_BATCH_RETRIES
        )
        extras = {
            k: str(v)
            for k, v in ui.items()
            if k not in _CORE_KEYS and isinstance(v, str | int | float | bool)
        }
        return cls(
            theme=theme or None,
            auto_tone_sniff=auto_sniff,
            intake_helper_model=helper,
            intake_max_segments=intake_max,
            intake_run_after_new=run_after,
            batch_concurrency=concurrency,
            batch_retries=retries,
            extras=extras,
        )

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
        if self.intake_helper_model:
            lines.append(f'intake_helper_model = "{_escape(self.intake_helper_model)}"')
        lines.append(f"intake_max_segments = {self.intake_max_segments}")
        lines.append(f"intake_run_after_new = {str(self.intake_run_after_new).lower()}")
        lines.append(f"batch_concurrency = {self.batch_concurrency}")
        lines.append(f"batch_retries = {self.batch_retries}")
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


def _coerce_positive_int(value: object, *, default: int) -> int:
    """Coerce ``value`` to a strictly-positive int, falling back to ``default``.

    A typo in ``ui.toml`` should never accidentally pin concurrency to
    zero (which would deadlock the batch worker) — we clamp non-positive
    values back to the default.
    """

    coerced = _coerce_int_or_default(value, default=default)
    return coerced if coerced > 0 else default


def _coerce_non_negative_int(value: object, *, default: int) -> int:
    """Coerce ``value`` to a non-negative int (zero is allowed).

    Used by ``batch_retries`` where ``0`` is a legal "no retries" value
    but a negative number is nonsense.
    """

    coerced = _coerce_int_or_default(value, default=default)
    return coerced if coerced >= 0 else default


def _coerce_int_or_default(value: object, *, default: int) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return default
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
