"""Glossary value objects (PRD §4.3 / M3).

These pydantic models are the in-memory view of the lore bible. They cross
module boundaries (matcher, enforcer, cascade, TUI) so the contract lives
here, not in :mod:`epublate.db.repo` (which owns persistence-layer rows).

The DB rows in :mod:`epublate.db.repo` (``GlossaryEntryRow`` etc.) are
intentionally projection-shaped (one row per table). The composite
:class:`GlossaryEntryWithAliases` is what every consumer actually wants:
the canonical entry plus its source-side and target-side aliases joined in.

Status semantics (``glossary-invariants.mdc``):

* ``locked`` — non-negotiable; passed to the LLM as a hard constraint and
  enforced by the post-translation validator.
* ``confirmed`` — strong default; surfaced in prompts, warned-about in the
  validator.
* ``proposed`` — auto-suggested or curator-drafted; not in the prompt and
  not enforced by the validator until promoted.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from epublate.db.schema import GlossaryStatus

EntityType = Literal[
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
"""Open-enum entity taxonomy (PRD F-LB-1)."""

GlossaryStatusLiteral = Literal["proposed", "confirmed", "locked"]

GenderTag = Literal[
    "feminine",
    "masculine",
    "neuter",
    "common",
    "unspecified",
]
"""Optional grammatical gender for gendered target languages (PRD §11 #2)."""

AliasSide = Literal["source", "target"]


class GlossaryEntry(BaseModel):
    """One canonical lore-bible row (PRD §4.3 / F-LB-2).

    ``target_term`` is non-optional even for ``proposed`` entries: the
    curator may not have decided yet, in which case the auto-proposer
    seeds it with the source term verbatim (a sentinel the curator is
    expected to overwrite before promoting).

    ``source_term`` is optional to support *target-only* entries that
    live in a Lore Book (PRD F-LB-3): the curator pins the canonical
    target form from an already-translated edition and lets the
    translator pipeline discover the source-side mapping at runtime.
    Project-scoped entries continue to require a source term — that
    boundary is enforced in :mod:`epublate.db.repo`.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    project_id: str
    type: EntityType = "term"
    source_term: str | None = None
    target_term: str
    gender: GenderTag | None = None
    status: GlossaryStatusLiteral = "proposed"
    notes: str | None = None
    first_seen_segment_id: str | None = None
    created_at: int = 0
    updated_at: int = 0
    source_known: bool = True
    """Whether this entry was authored with a known source spelling.

    Defaults to ``True`` so existing project-scoped code paths and
    legacy DB rows keep their original semantics. Set ``False`` for
    target-only Lore Book entries; the validator (PRD F-LB-9) treats
    locked target-only rows as a *soft* lock — warn rather than fail.
    """


class GlossaryAlias(BaseModel):
    """One alias for a glossary entry, on either source or target side."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    entry_id: str
    side: AliasSide
    text: str


class GlossaryRevision(BaseModel):
    """Immutable record of a meaningful change to an entry (PRD F-LB-6)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    entry_id: str
    prev_target_term: str | None = None
    new_target_term: str | None = None
    reason: str | None = None
    created_at: int = 0


class EntityMention(BaseModel):
    """One observed source-side match of a glossary entry (PRD §6.4)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    segment_id: str
    entry_id: str
    source_span_start: int | None = None
    source_span_end: int | None = None


class GlossaryEntryWithAliases(BaseModel):
    """Composite view: entry + its aliases joined in.

    The matcher, enforcer, prompt builder, and TUI all work with this shape
    so they don't need three separate joins. ``source_aliases`` and
    ``target_aliases`` are deduplicated and sorted for deterministic
    hashing (see :func:`epublate.glossary.enforcer.glossary_hash`).
    """

    model_config = ConfigDict(extra="forbid")

    entry: GlossaryEntry
    source_aliases: list[str] = Field(default_factory=list)
    target_aliases: list[str] = Field(default_factory=list)

    @property
    def id(self) -> str:
        return self.entry.id

    @property
    def status(self) -> GlossaryStatusLiteral:
        return self.entry.status

    @property
    def source_term(self) -> str | None:
        return self.entry.source_term

    @property
    def target_term(self) -> str:
        return self.entry.target_term

    @property
    def source_known(self) -> bool:
        return self.entry.source_known

    def all_source_terms(self) -> list[str]:
        """Canonical source term followed by its aliases (deduped, ordered).

        Target-only entries (``source_known=False`` / no ``source_term``)
        return only their non-empty aliases — the matcher uses this list
        to know which source spellings to look for, and a target-only
        entry is allowed to have zero of them.
        """

        seen: dict[str, None] = {}
        if self.entry.source_term:
            seen[self.entry.source_term] = None
        for alias in self.source_aliases:
            if alias and alias not in seen:
                seen[alias] = None
        return list(seen)

    def all_target_terms(self) -> list[str]:
        seen: dict[str, None] = {self.entry.target_term: None}
        for alias in self.target_aliases:
            if alias and alias not in seen:
                seen[alias] = None
        return list(seen)


__all__ = [
    "AliasSide",
    "EntityMention",
    "EntityType",
    "GenderTag",
    "GlossaryAlias",
    "GlossaryEntry",
    "GlossaryEntryWithAliases",
    "GlossaryRevision",
    "GlossaryStatus",
    "GlossaryStatusLiteral",
]
