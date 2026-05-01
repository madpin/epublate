"""Recently-opened-projects store for the TUI (PRD §4.6).

The Projects screen needs to feel like the home of an actual desktop
app: launching ``epublate`` should land on a list of the projects you
were recently working with, not a stub that points back to the CLI.
We persist that list as JSON under ``~/.config/epublate/recents.json``
so it survives across runs without leaking into any per-project
SQLite DB.

Design choices:

* JSON, not TOML, because the entries are simple records with mixed
  types (string + float) that map cleanly to a list of dicts.
* Newest-first ordering, capped at :data:`MAX_RECENTS` to keep the
  Projects screen's table readable.
* Stable equality on the resolved project directory; re-opening the
  same project bumps it to the top instead of duplicating it.
* Stale entries (project directory removed) are *kept* in the file
  but flagged when read, so the screen can let the curator prune
  them without surprise behavior on disk.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import time
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path

from epublate.app.config import xdg_config_home

_logger = logging.getLogger(__name__)

RECENTS_FILENAME = "recents.json"
SCHEMA_VERSION = 1
MAX_RECENTS = 25


@dataclass(slots=True, frozen=True)
class RecentProject:
    """One row in the recents store."""

    project_dir: str
    name: str
    source_lang: str
    target_lang: str
    last_opened: float = field(default_factory=time.time)

    @property
    def path(self) -> Path:
        return Path(self.project_dir)

    def exists(self) -> bool:
        try:
            return Path(self.project_dir).is_dir()
        except OSError:
            return False


def default_recents_path() -> Path:
    return xdg_config_home() / "epublate" / RECENTS_FILENAME


@dataclass(slots=True)
class RecentsStore:
    """In-memory snapshot of the recents file."""

    entries: list[RecentProject] = field(default_factory=list)
    schema_version: int = SCHEMA_VERSION

    @classmethod
    def load(cls, path: Path | None = None) -> RecentsStore:
        cfg_path = path or default_recents_path()
        if not cfg_path.is_file():
            return cls()
        try:
            data = json.loads(cfg_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            _logger.warning("ignoring malformed recents store %s: %s", cfg_path, exc)
            return cls()
        if not isinstance(data, dict):
            return cls()
        raw_entries = data.get("entries", [])
        entries: list[RecentProject] = []
        for raw in raw_entries:
            if not isinstance(raw, dict):
                continue
            project_dir = raw.get("project_dir")
            if not isinstance(project_dir, str) or not project_dir:
                continue
            entries.append(
                RecentProject(
                    project_dir=project_dir,
                    name=str(raw.get("name") or Path(project_dir).name),
                    source_lang=str(raw.get("source_lang") or "und"),
                    target_lang=str(raw.get("target_lang") or "und"),
                    last_opened=float(raw.get("last_opened") or 0.0),
                )
            )
        # Newest first; the file *should* already be sorted but fix it
        # cheaply on load so the UI doesn't have to.
        entries.sort(key=lambda e: e.last_opened, reverse=True)
        version = int(data.get("schema_version") or SCHEMA_VERSION)
        return cls(entries=entries[:MAX_RECENTS], schema_version=version)

    def save(self, path: Path | None = None) -> Path:
        cfg_path = path or default_recents_path()
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": self.schema_version,
            "entries": [asdict(entry) for entry in self.entries],
        }
        body = json.dumps(payload, indent=2, sort_keys=True) + "\n"
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

    def upsert(self, entry: RecentProject) -> RecentsStore:
        """Move ``entry`` to the top of the list, deduped on ``project_dir``."""

        normalized = replace(entry, project_dir=str(Path(entry.project_dir).resolve()))
        kept = [
            existing
            for existing in self.entries
            if str(Path(existing.project_dir).resolve()) != normalized.project_dir
        ]
        self.entries = [normalized, *kept][:MAX_RECENTS]
        return self

    def remove(self, project_dir: str | Path) -> bool:
        """Drop the entry matching ``project_dir``; return whether it existed."""

        target = str(Path(project_dir).resolve())
        before = len(self.entries)
        self.entries = [
            entry
            for entry in self.entries
            if str(Path(entry.project_dir).resolve()) != target
        ]
        return len(self.entries) != before

    def prune_missing(self) -> list[RecentProject]:
        """Drop entries whose project directory no longer exists; return them."""

        missing = [entry for entry in self.entries if not entry.exists()]
        if not missing:
            return []
        gone = {str(Path(e.project_dir).resolve()) for e in missing}
        self.entries = [
            entry
            for entry in self.entries
            if str(Path(entry.project_dir).resolve()) not in gone
        ]
        return missing


def record_project(
    *,
    project_dir: Path,
    name: str,
    source_lang: str,
    target_lang: str,
    path: Path | None = None,
) -> RecentProject:
    """Convenience wrapper: load → upsert → save in one call.

    Used by :class:`epublate.core.project.Project` so the TUI's recents
    list stays accurate even when projects are created or opened from
    the CLI.
    """

    store = RecentsStore.load(path)
    entry = RecentProject(
        project_dir=str(Path(project_dir).resolve()),
        name=name,
        source_lang=source_lang,
        target_lang=target_lang,
        last_opened=time.time(),
    )
    store.upsert(entry)
    try:
        store.save(path)
    except OSError as exc:
        _logger.warning("could not persist recents store: %s", exc)
    return entry


__all__ = [
    "MAX_RECENTS",
    "RECENTS_FILENAME",
    "RecentProject",
    "RecentsStore",
    "default_recents_path",
    "record_project",
]
