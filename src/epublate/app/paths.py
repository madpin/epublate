"""Filesystem layout helpers for the TUI (PRD §4.6 / NFR-2).

Projects land in a single, predictable, per-user location so curators
can find their books in Finder / Nautilus without remembering where
the ``epublate`` process was running from. Legacy behavior
(everything defaults to ``$CWD``) is gone — tests and the CLI keep
working because they always pass explicit ``--out`` / ``out_dir``.

Resolution order for the projects root:

1. ``$EPUBLATE_PROJECTS_ROOT`` (wins; lets power users pin a drive).
2. ``ui.toml`` → ``[ui] projects_root = "…"`` (set from the Settings
   screen).
3. ``~/Documents/epublate`` when the Documents folder exists (typical
   macOS / Windows / Desktop Linux).
4. ``$XDG_DATA_HOME/epublate`` when set (XDG-respecting Linux boxes).
5. ``~/.local/share/epublate`` as the XDG fallback.

The module is side-effect free: nothing creates directories until a
caller (typically :class:`NewProjectModal` or ``epublate new``) asks
us to. :func:`ensure_projects_root` is the single write path.
"""

from __future__ import annotations

import contextlib
import os
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from epublate.app.config import UIConfig, xdg_config_home

ENV_PROJECTS_ROOT = "EPUBLATE_PROJECTS_ROOT"
CONFIG_KEY_PROJECTS_ROOT = "projects_root"

# Quick-access labels the FileBrowserModal exposes — ordered by how
# likely the curator is to want them.
_QUICK_LOCATIONS_ORDER: tuple[tuple[str, Path | None], ...] = ()


@dataclass(slots=True, frozen=True)
class QuickLocation:
    """One entry in the FileBrowserModal's sidebar."""

    label: str
    path: Path
    shortcut: str | None = None

    def exists(self) -> bool:
        try:
            return self.path.is_dir()
        except OSError:
            return False


def xdg_data_home() -> Path:
    """Return ``$XDG_DATA_HOME`` falling back to ``~/.local/share``."""

    raw = os.environ.get("XDG_DATA_HOME", "").strip()
    if raw:
        return Path(raw)
    return Path.home() / ".local" / "share"


def default_projects_root(*, ui_config: UIConfig | None = None) -> Path:
    """Resolve the canonical location for new projects.

    ``ui_config`` lets the caller inject an already-loaded config (the
    App does this once at boot); omit and the helper will re-read the
    file itself, which is fine for CLI callers that don't have one.
    """

    env = os.environ.get(ENV_PROJECTS_ROOT, "").strip()
    if env:
        return Path(env).expanduser().resolve()

    cfg = ui_config if ui_config is not None else UIConfig.load()
    stored = cfg.extras.get(CONFIG_KEY_PROJECTS_ROOT, "").strip()
    if stored:
        return Path(stored).expanduser().resolve()

    documents = Path.home() / "Documents"
    if documents.is_dir():
        return (documents / "epublate").resolve()

    return (xdg_data_home() / "epublate").resolve()


def set_projects_root(
    root: Path, *, ui_config: UIConfig | None = None, config_path: Path | None = None
) -> Path:
    """Persist ``root`` to ``ui.toml`` so it's the new default.

    Returns the resolved path so the caller can surface it in the UI
    without re-reading the file. The write fails silently on
    :class:`OSError` because UI preferences are a convenience; the
    next ``default_projects_root`` call will pick the same value
    from the env var if the caller sets one.
    """

    resolved = Path(root).expanduser().resolve()
    cfg = ui_config if ui_config is not None else UIConfig.load(config_path)
    cfg.extras[CONFIG_KEY_PROJECTS_ROOT] = str(resolved)
    with contextlib.suppress(OSError):
        cfg.save(config_path)
    return resolved


def ensure_projects_root(root: Path) -> Path:
    """``mkdir -p`` for the projects root. Returns the resolved path."""

    resolved = Path(root).expanduser()
    resolved.mkdir(parents=True, exist_ok=True)
    return resolved.resolve()


def quick_locations(*, cwd: Path | None = None) -> list[QuickLocation]:
    """Return existing quick-access directories for the file browser.

    Order: current working directory, home, Documents, Downloads,
    Desktop, projects root. Non-existing entries are filtered so the
    browser never offers a dead link. ``cwd`` is injectable for tests
    and to accommodate callers that have their own notion of "current".
    """

    here = (cwd or Path.cwd()).resolve()
    home = Path.home()
    candidates: tuple[tuple[str, Path, str | None], ...] = (
        ("Current", here, "1"),
        ("Home", home, "2"),
        ("Documents", home / "Documents", "3"),
        ("Downloads", home / "Downloads", "4"),
        ("Desktop", home / "Desktop", "5"),
    )
    result: list[QuickLocation] = []
    seen: set[Path] = set()
    for label, path, shortcut in candidates:
        try:
            resolved = path.resolve()
        except OSError:
            continue
        if not resolved.is_dir() or resolved in seen:
            continue
        seen.add(resolved)
        result.append(QuickLocation(label=label, path=resolved, shortcut=shortcut))
    root: Path | None
    try:
        root = default_projects_root()
    except (OSError, ValueError):
        root = None
    if root is not None and root not in seen and root.is_dir():
        seen.add(root)
        result.append(QuickLocation(label="Projects", path=root, shortcut="6"))
    return result


def _is_dir_occupied(path: Path) -> bool:
    """Treat any non-empty directory as "occupied" for collision logic."""

    try:
        if not path.exists():
            return False
        if not path.is_dir():
            return True
        return any(path.iterdir())
    except OSError:
        # Unreadable paths are a "can't use" case; surface the collision
        # to the caller rather than silently overwriting.
        return True


def unique_project_dir(parent: Path, stem: str) -> Path:
    """Pick the first non-occupied ``parent/<stem>[-N]`` folder.

    Used by :class:`NewProjectModal` (and the CLI) so curators don't
    see a raw "directory already exists" error when their previous
    project under the same name is still on disk.
    """

    parent = Path(parent).expanduser()
    base_stem = _sanitize_stem(stem)
    candidate = parent / base_stem
    if not _is_dir_occupied(candidate):
        return candidate
    for n in range(2, 1000):
        candidate = parent / f"{base_stem}-{n}"
        if not _is_dir_occupied(candidate):
            return candidate
    # In the pathological case where 1000 collisions exist, we still
    # return *something* sensible so the caller can surface an error.
    return parent / f"{base_stem}-{os.getpid()}"


def unique_project_name(name: str, existing_names: Iterable[str]) -> str:
    """Suggest ``name (2)``, ``name (3)`` etc. on collision.

    Used for the display name (``projects.name`` column) shown in
    recents. Kept separate from :func:`unique_project_dir` because
    humans often want a nicer pattern for the name than the disk
    suffixes (``Name (2)`` vs ``name-2``).
    """

    taken = {n.strip().casefold() for n in existing_names if n}
    base = name.strip()
    if not base:
        return base
    if base.casefold() not in taken:
        return base
    stripped, existing_suffix = _strip_name_suffix(base)
    start = existing_suffix + 1 if existing_suffix is not None else 2
    for n in range(start, 1000):
        candidate = f"{stripped} ({n})"
        if candidate.casefold() not in taken:
            return candidate
    return f"{stripped} (new)"


_NAME_SUFFIX_RE = re.compile(r"^(.*)\s*\((\d+)\)\s*$")


def _strip_name_suffix(name: str) -> tuple[str, int | None]:
    m = _NAME_SUFFIX_RE.match(name)
    if not m:
        return name, None
    stem = m.group(1).rstrip()
    try:
        return stem, int(m.group(2))
    except ValueError:
        return name, None


_SANITIZE_RE = re.compile(r"[^A-Za-z0-9._\-]+")


def _sanitize_stem(stem: str) -> str:
    """Keep disk-friendly characters; collapse everything else to ``-``.

    ePub stems are often human-titled and contain spaces, punctuation,
    or non-ASCII runes. We preserve Unicode in the project *display*
    name but normalize the *directory* so shells don't have to quote.
    """

    cleaned = _SANITIZE_RE.sub("-", stem).strip("-._")
    return cleaned or "project"


__all__ = [
    "CONFIG_KEY_PROJECTS_ROOT",
    "ENV_PROJECTS_ROOT",
    "QuickLocation",
    "default_projects_root",
    "ensure_projects_root",
    "quick_locations",
    "set_projects_root",
    "unique_project_dir",
    "unique_project_name",
    "xdg_config_home",
    "xdg_data_home",
]
