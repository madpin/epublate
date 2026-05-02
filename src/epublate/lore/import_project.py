"""Import a project's curated glossary into a Lore Book (PRD F-LB-10).

Curators frequently want to bootstrap a Lore Book from a translation
project they've already curated — book 1 of a series begets the
Lore Book that drives books 2..N. This module ingests the source
project's ``glossary_entry`` rows into the destination Lore Book and
surfaces conflicts so the UI can prompt the curator to disambiguate.

Three conflict policies are supported:

* ``"skip"``     — keep the destination entry, silently drop the
                   incoming row.
* ``"overwrite"``— update the destination entry with the incoming
                   target term, status, gender, notes, and aliases.
* ``"collect"``  — return conflicts in the summary without applying
                   them. The UI then walks them one at a time and
                   calls :func:`apply_conflict_resolution` for each.

Matching rules mirror :mod:`epublate.glossary.io`:

* source-keyed entries (``source_term`` is a string) deduplicate on
  ``(source_term, type)``;
* target-only entries (``source_term`` is ``None``) are *always*
  inserted — different lore books may pin the same proper noun for
  unrelated entities, so silent merge would be wrong. They surface in
  the summary's ``target_only_inserts`` count for transparency.

The whole operation runs without holding a single transaction over the
destination Lore Book: the source list may be long, partial progress
is friendlier than atomic rollback, and the destination's own
``glossary_revision`` history captures every change for audit.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from epublate.core.project import Project
from epublate.db import repo
from epublate.glossary.io import export_json
from epublate.lore.lore import LoreBook

ProjectImportPolicy = Literal["skip", "overwrite", "collect"]


@dataclass(slots=True, frozen=True)
class ProjectImportConflict:
    """One conflict surfaced when importing project glossary into a Lore Book.

    ``existing`` and ``incoming`` are dict-shaped payloads (matching
    :func:`epublate.glossary.io.export_json` rows) so both the CLI and
    the TUI can render diffs without depending on the SQLAlchemy
    layer. ``existing_id`` is the destination Lore Book's
    :class:`epublate.db.repo.GlossaryEntryWithAliases` id; the UI
    passes it back to :func:`apply_conflict_resolution` along with
    the chosen action.
    """

    existing_id: str
    source_term: str
    type: str
    existing: dict[str, Any]
    incoming: dict[str, Any]

    @property
    def label(self) -> str:
        return f"{self.source_term} [{self.type}]"

    @property
    def existing_target(self) -> str:
        return str(self.existing.get("target_term", ""))

    @property
    def incoming_target(self) -> str:
        return str(self.incoming.get("target_term", ""))


@dataclass(slots=True)
class ProjectImportSummary:
    """Outcome of :func:`import_project_glossary`.

    ``conflicts`` is non-empty only when the policy was ``"collect"``;
    in ``"skip"`` and ``"overwrite"`` modes those rows show up in
    ``skipped`` / ``updated`` instead.
    """

    created: int = 0
    updated: int = 0
    skipped: int = 0
    target_only_inserts: int = 0
    conflicts: list[ProjectImportConflict] = field(default_factory=list)

    @property
    def total(self) -> int:
        return self.created + self.updated + self.skipped + len(self.conflicts)


ConflictAction = Literal["keep_existing", "use_incoming", "skip"]


def import_project_glossary(
    dest: LoreBook,
    *,
    src_project_dir: Path,
    policy: ProjectImportPolicy = "collect",
) -> ProjectImportSummary:
    """Import every glossary entry from ``src_project_dir`` into ``dest``.

    Opens the source project read-only, snapshots its glossary via
    :func:`export_json`, and walks each entry against the destination
    Lore Book. Returns a :class:`ProjectImportSummary` describing what
    happened. The source project's connection is closed before this
    function returns regardless of outcome.
    """

    src_project_dir = Path(src_project_dir).expanduser().resolve()
    src_project = Project.open(src_project_dir)
    try:
        payload = export_json(src_project.engine, src_project.project_id)
    finally:
        src_project.close()

    summary = ProjectImportSummary()
    raw_entries: Iterable[dict[str, Any]] = payload.get("entries") or []

    for raw in raw_entries:
        normalized = _normalize_for_import(raw)
        # Target-only rows can't dedupe by source_term — see module docstring.
        if normalized["source_term"] is None:
            _create_entry(dest, normalized)
            summary.created += 1
            summary.target_only_inserts += 1
            continue

        existing_id = _find_existing_id(
            dest,
            source_term=normalized["source_term"],
            type_=normalized["type"],
        )
        if existing_id is None:
            _create_entry(dest, normalized)
            summary.created += 1
            continue
        existing = repo.get_glossary_entry(dest.engine, existing_id)
        if existing is None:
            # Race: the entry vanished between the lookup and the load.
            # Treat it as not-found rather than crashing the whole import.
            _create_entry(dest, normalized)
            summary.created += 1
            continue

        # Identical-payload "conflicts" never count against the curator
        # — re-importing the same project should be a no-op.
        if _payload_equivalent(
            existing_payload=_existing_to_payload(existing),
            incoming=normalized,
        ):
            summary.skipped += 1
            continue

        if policy == "skip":
            summary.skipped += 1
            continue
        if policy == "overwrite":
            _overwrite_entry(dest, existing_id=existing.id, normalized=normalized)
            summary.updated += 1
            continue
        # policy == "collect"
        summary.conflicts.append(
            ProjectImportConflict(
                existing_id=existing.id,
                source_term=normalized["source_term"],
                type=normalized["type"],
                existing=_existing_to_payload(existing),
                incoming=normalized,
            )
        )

    return summary


def apply_conflict_resolution(
    dest: LoreBook,
    *,
    conflict: ProjectImportConflict,
    action: ConflictAction,
) -> None:
    """Apply the curator's choice for one conflict surfaced earlier.

    ``"keep_existing"`` and ``"skip"`` are no-ops on the DB; they exist
    so callers can tally outcomes without branching. ``"use_incoming"``
    overwrites the destination entry with the incoming row's target
    term, status, type, gender, notes, and aliases — the same path a
    bulk ``policy="overwrite"`` import would have taken.
    """

    if action in ("keep_existing", "skip"):
        return
    if action == "use_incoming":
        _overwrite_entry(
            dest, existing_id=conflict.existing_id, normalized=conflict.incoming
        )
        return
    raise ValueError(f"unknown conflict action: {action!r}")


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _normalize_for_import(raw: dict[str, Any]) -> dict[str, Any]:
    """Return a stable, mutation-safe copy of an entry payload row."""

    source_term = raw.get("source_term")
    return {
        "source_term": source_term,
        "target_term": str(raw.get("target_term", "")),
        "type": str(raw.get("type", "term")),
        "status": str(raw.get("status", "proposed")),
        "gender": raw.get("gender"),
        "notes": raw.get("notes"),
        "source_known": bool(raw.get("source_known", source_term is not None)),
        "source_aliases": list(raw.get("source_aliases") or []),
        "target_aliases": list(raw.get("target_aliases") or []),
    }


def _find_existing_id(dest: LoreBook, *, source_term: str, type_: str) -> str | None:
    found = repo.find_glossary_entry_by_source_term(
        dest.engine,
        project_id=dest.project_id,
        source_term=source_term,
        type=type_,  # type: ignore[arg-type]
    )
    return None if found is None else found.id


def _create_entry(dest: LoreBook, normalized: dict[str, Any]) -> None:
    repo.create_glossary_entry(
        dest.engine,
        project_id=dest.project_id,
        source_term=normalized["source_term"],
        target_term=normalized["target_term"],
        type=normalized["type"],
        status=normalized["status"],
        gender=normalized["gender"],
        notes=normalized["notes"],
        source_aliases=normalized["source_aliases"],
        target_aliases=normalized["target_aliases"],
        source_known=normalized["source_known"],
    )


def _overwrite_entry(
    dest: LoreBook,
    *,
    existing_id: str,
    normalized: dict[str, Any],
) -> None:
    repo.update_glossary_entry(
        dest.engine,
        entry_id=existing_id,
        target_term=normalized["target_term"],
        status=normalized["status"],
        type=normalized["type"],
        gender=normalized["gender"],
        notes=normalized["notes"],
        reason="import_project:overwrite",
    )
    repo.set_aliases(
        dest.engine,
        entry_id=existing_id,
        source_aliases=normalized["source_aliases"],
        target_aliases=normalized["target_aliases"],
    )


def _existing_to_payload(existing: Any) -> dict[str, Any]:
    """Render a destination ``GlossaryEntryWithAliases`` as a payload row.

    Mirrors :func:`epublate.glossary.io._entry_to_payload` but lives
    here so the import module stays self-contained and a future
    refactor of the export shape doesn't quietly break this matcher.
    """

    return {
        "source_term": existing.source_term,
        "target_term": existing.target_term,
        "type": existing.entry.type,
        "status": existing.status,
        "gender": existing.entry.gender,
        "notes": existing.entry.notes,
        "source_known": existing.source_known,
        "source_aliases": list(existing.source_aliases),
        "target_aliases": list(existing.target_aliases),
    }


def _payload_equivalent(
    *,
    existing_payload: dict[str, Any],
    incoming: dict[str, Any],
) -> bool:
    """True when the destination entry already matches the incoming row.

    Aliases are compared as sets so order differences don't trigger
    spurious "conflicts". ``status`` is intentionally part of the
    comparison: re-importing a project that has been promoted from
    ``confirmed`` to ``locked`` should *not* be a silent no-op — the
    curator wants to see (and resolve) that change explicitly.
    """

    keys = ("source_term", "target_term", "type", "status", "gender", "notes")
    for key in keys:
        if existing_payload.get(key) != incoming.get(key):
            return False
    if set(existing_payload.get("source_aliases") or []) != set(
        incoming.get("source_aliases") or []
    ):
        return False
    return set(existing_payload.get("target_aliases") or []) == set(
        incoming.get("target_aliases") or []
    )


__all__ = [
    "ConflictAction",
    "ProjectImportConflict",
    "ProjectImportPolicy",
    "ProjectImportSummary",
    "apply_conflict_resolution",
    "import_project_glossary",
]
