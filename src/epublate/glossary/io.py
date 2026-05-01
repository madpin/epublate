"""Glossary JSON import/export and auto-proposal upsert (PRD F-LB-8 / M3).

Three entry points:

* :func:`export_json` — produces a deterministic JSON snapshot of every
  glossary entry in a project. Stable enough to commit to a repo and
  diff between runs.
* :func:`import_json` — apply a snapshot to a project, with explicit
  ``conflict`` semantics so the curator can either keep their existing
  entries or overwrite them from the file.
* :func:`upsert_proposed` — used by the pipeline's auto-proposer to
  drop ``trace.new_entities`` candidates into the lore bible without
  duplicating already-known terms.

The on-disk shape is versioned (``"version": 1``) so future schema
changes can be migrated without surprises.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast

from sqlalchemy.engine import Connection, Engine

from epublate.db import repo
from epublate.errors import ConfigurationError
from epublate.glossary.models import (
    EntityType,
    GenderTag,
    GlossaryEntryWithAliases,
    GlossaryStatusLiteral,
)

GLOSSARY_FORMAT_VERSION = 1
ConflictStrategy = Literal["skip", "overwrite"]

_VALID_TYPES: frozenset[str] = frozenset(
    [
        "character",
        "place",
        "organization",
        "event",
        "item",
        "date_or_time",
        "phrase",
        "term",
        "other",
    ]
)
_VALID_STATUSES: frozenset[str] = frozenset(["proposed", "confirmed", "locked"])
_VALID_GENDERS: frozenset[str] = frozenset(
    ["feminine", "masculine", "neuter", "common", "unspecified"]
)


@dataclass(slots=True, frozen=True)
class ImportSummary:
    """Outcome of :func:`import_json`."""

    created: int
    updated: int
    skipped: int

    @property
    def total(self) -> int:
        return self.created + self.updated + self.skipped


def export_json(engine: Engine | Connection, project_id: str) -> dict[str, Any]:
    """Return a stable JSON-shaped dict of every glossary entry."""

    entries = repo.list_glossary_entries(engine, project_id)
    serialized = [_entry_to_payload(e) for e in entries]
    return {
        "version": GLOSSARY_FORMAT_VERSION,
        "project_id": project_id,
        "entries": serialized,
    }


def import_json(
    engine: Engine,
    *,
    project_id: str,
    payload: dict[str, Any],
    conflict: ConflictStrategy = "skip",
) -> ImportSummary:
    """Import ``payload`` into the project glossary.

    Conflict semantics:

    * ``"skip"`` (default): existing entries with the same
      ``source_term`` (and ``type`` if present) are left alone.
    * ``"overwrite"``: existing entries are updated to match the
      incoming row (target term, status, aliases, gender, notes); a
      revision is recorded automatically by
      :func:`epublate.db.repo.update_glossary_entry`.

    New entries are always inserted. The whole operation is *not*
    bundled into a single transaction because the JSON file may be
    large and the curator is better served by partial progress on
    crash than by an atomic all-or-nothing import.
    """

    _validate_payload_shape(payload)

    created = 0
    updated = 0
    skipped = 0

    for raw_entry in payload.get("entries", []):
        normalized = _normalize_entry_payload(raw_entry)
        existing = repo.find_glossary_entry_by_source_term(
            engine,
            project_id=project_id,
            source_term=normalized["source_term"],
            type=normalized["type"],
        )
        if existing is None:
            repo.create_glossary_entry(
                engine,
                project_id=project_id,
                source_term=normalized["source_term"],
                target_term=normalized["target_term"],
                type=normalized["type"],
                status=normalized["status"],
                gender=normalized["gender"],
                notes=normalized["notes"],
                source_aliases=normalized["source_aliases"],
                target_aliases=normalized["target_aliases"],
            )
            created += 1
            continue

        if conflict == "skip":
            skipped += 1
            continue

        repo.update_glossary_entry(
            engine,
            entry_id=existing.id,
            target_term=normalized["target_term"],
            status=normalized["status"],
            type=normalized["type"],
            gender=normalized["gender"],
            notes=normalized["notes"],
            reason="import_json:overwrite",
        )
        repo.set_aliases(
            engine,
            entry_id=existing.id,
            source_aliases=normalized["source_aliases"],
            target_aliases=normalized["target_aliases"],
        )
        updated += 1

    return ImportSummary(created=created, updated=updated, skipped=skipped)


def import_starter(
    engine: Engine | Connection,
    *,
    project_id: str,
    path: Path,
) -> ImportSummary:
    """Import a starter glossary at ``epublate new`` time (PRD §7.1).

    Designed to be called inside ``Project.create``'s import-chapters
    transaction (which is why this function accepts a ``Connection``
    too — :mod:`epublate.db.repo` helpers transparently support either).
    """

    if not path.is_file():
        raise ConfigurationError(f"starter glossary not found: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigurationError(
            f"starter glossary is not valid JSON: {path}: {exc}"
        ) from exc

    _validate_payload_shape(payload)

    created = 0
    skipped = 0
    for raw_entry in payload.get("entries", []):
        normalized = _normalize_entry_payload(raw_entry)
        existing = repo.find_glossary_entry_by_source_term(
            engine,
            project_id=project_id,
            source_term=normalized["source_term"],
            type=normalized["type"],
        )
        if existing is not None:
            skipped += 1
            continue
        repo.create_glossary_entry(
            engine,
            project_id=project_id,
            source_term=normalized["source_term"],
            target_term=normalized["target_term"],
            type=normalized["type"],
            status=normalized["status"],
            gender=normalized["gender"],
            notes=normalized["notes"],
            source_aliases=normalized["source_aliases"],
            target_aliases=normalized["target_aliases"],
        )
        created += 1

    return ImportSummary(created=created, updated=0, skipped=skipped)


def upsert_proposed(
    engine: Engine | Connection,
    *,
    project_id: str,
    source_term: str,
    type: EntityType = "term",
    notes: str | None = None,
    first_seen_segment_id: str | None = None,
) -> tuple[str, bool]:
    """Insert a ``proposed`` entry if no row exists yet for this source term.

    Returns ``(entry_id, created)`` so the pipeline can decide whether
    to emit an ``entity.proposed`` event (only on first sighting). If
    an entry already exists for ``(source_term, type)`` we do not
    touch it — the curator may have already promoted/edited it.
    """

    existing = repo.find_glossary_entry_by_source_term(
        engine,
        project_id=project_id,
        source_term=source_term,
        type=type,
    )
    if existing is not None:
        return existing.id, False
    entry = repo.create_glossary_entry(
        engine,
        project_id=project_id,
        source_term=source_term,
        target_term=source_term,  # placeholder; curator promotes/edits
        type=type,
        status="proposed",
        notes=notes,
        first_seen_segment_id=first_seen_segment_id,
    )
    return entry.id, True


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _entry_to_payload(entry: GlossaryEntryWithAliases) -> dict[str, Any]:
    return {
        "source_term": entry.source_term,
        "target_term": entry.target_term,
        "type": entry.entry.type,
        "status": entry.status,
        "gender": entry.entry.gender,
        "notes": entry.entry.notes,
        "source_aliases": list(entry.source_aliases),
        "target_aliases": list(entry.target_aliases),
    }


def _validate_payload_shape(payload: dict[str, Any]) -> None:
    if not isinstance(payload, dict):
        raise ConfigurationError("glossary payload must be a JSON object")
    version = payload.get("version")
    if version != GLOSSARY_FORMAT_VERSION:
        raise ConfigurationError(
            f"unsupported glossary file version: {version!r} "
            f"(expected {GLOSSARY_FORMAT_VERSION})"
        )
    entries = payload.get("entries")
    if not isinstance(entries, list):
        raise ConfigurationError("glossary payload 'entries' must be a list")


def _normalize_entry_payload(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ConfigurationError("each glossary entry must be a JSON object")
    source_term = raw.get("source_term")
    target_term = raw.get("target_term")
    if not isinstance(source_term, str) or not source_term.strip():
        raise ConfigurationError("entry missing non-empty 'source_term'")
    if not isinstance(target_term, str) or not target_term.strip():
        raise ConfigurationError(
            f"entry {source_term!r} missing non-empty 'target_term'"
        )

    type_str = str(raw.get("type", "term"))
    if type_str not in _VALID_TYPES:
        raise ConfigurationError(
            f"entry {source_term!r} has invalid type {type_str!r}; "
            f"expected one of {sorted(_VALID_TYPES)}"
        )
    status_str = str(raw.get("status", "proposed"))
    if status_str not in _VALID_STATUSES:
        raise ConfigurationError(
            f"entry {source_term!r} has invalid status {status_str!r}; "
            f"expected one of {sorted(_VALID_STATUSES)}"
        )
    gender_raw = raw.get("gender")
    gender: GenderTag | None
    if gender_raw is None:
        gender = None
    else:
        gender_str = str(gender_raw)
        if gender_str not in _VALID_GENDERS:
            raise ConfigurationError(
                f"entry {source_term!r} has invalid gender {gender_str!r}; "
                f"expected one of {[*sorted(_VALID_GENDERS), None]}"
            )
        gender = cast(GenderTag, gender_str)

    notes = raw.get("notes")
    if notes is not None and not isinstance(notes, str):
        raise ConfigurationError(
            f"entry {source_term!r} 'notes' must be a string if set"
        )
    src_aliases = _coerce_alias_list(raw.get("source_aliases", []), source_term)
    tgt_aliases = _coerce_alias_list(raw.get("target_aliases", []), source_term)

    return {
        "source_term": source_term,
        "target_term": target_term,
        "type": cast(EntityType, type_str),
        "status": cast(GlossaryStatusLiteral, status_str),
        "gender": gender,
        "notes": notes,
        "source_aliases": src_aliases,
        "target_aliases": tgt_aliases,
    }


def _coerce_alias_list(raw: Any, owner: str) -> list[str]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ConfigurationError(f"entry {owner!r}: alias lists must be JSON arrays")
    out: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            raise ConfigurationError(f"entry {owner!r}: aliases must be strings")
        if item.strip():
            out.append(item)
    return out


def write_export(path: Path, payload: dict[str, Any]) -> None:
    """Atomically write a JSON export to ``path`` (db-and-persistence rule)."""

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    tmp.replace(path)


def read_payload(path: Path) -> dict[str, Any]:
    """Load and shape-validate a glossary JSON file from disk."""

    if not path.is_file():
        raise ConfigurationError(f"glossary file not found: {path}")
    try:
        payload_obj = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigurationError(f"{path} is not valid JSON: {exc}") from exc
    _validate_payload_shape(payload_obj)
    return cast(dict[str, Any], payload_obj)


# Discoverability helper for callers that want the typed iterable shape:
def iter_entries(
    payload: dict[str, Any],
) -> Iterable[dict[str, Any]]:
    for raw in payload.get("entries", []):
        yield _normalize_entry_payload(raw)


__all__ = [
    "GLOSSARY_FORMAT_VERSION",
    "ConflictStrategy",
    "ImportSummary",
    "export_json",
    "import_json",
    "import_starter",
    "iter_entries",
    "read_payload",
    "upsert_proposed",
    "write_export",
]
