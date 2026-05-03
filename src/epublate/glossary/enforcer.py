"""Glossary enforcer (PRD §4.2 phase 5 / M3).

Three concerns live here:

1. **Build prompt constraints.** Convert
   :class:`~epublate.glossary.models.GlossaryEntryWithAliases` rows to
   :class:`~epublate.llm.prompts.translator.GlossaryConstraint`
   objects the translator system prompt understands. ``proposed``
   entries are dropped — the LLM should not constrain against an
   un-vetted suggestion (``glossary-invariants.mdc``).

2. **Validate translations.** After the LLM call the pipeline must
   confirm that every locked entry whose source term appeared in the
   source segment is honored in the target. Confirmed entries warn
   only. Returns a list of :class:`Violation` objects so the pipeline
   can either persist them on the segment for the curator (M3 / Inbox
   in M4) or, in the locked case, downgrade segment status to
   ``flagged``.

3. **Hash the glossary state.** The cache key
   ``(model, system_hash, user_hash, glossary_hash)`` (PRD F-LLM-6)
   needs ``glossary_hash`` to stay stable across runs and to flip
   whenever any LLM-visible glossary attribute changes. We compute it
   here so the pipeline can call it identically for the cache key and
   for any future sanity-check code path.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Literal

from epublate.glossary.matcher import Match, match_source, target_uses
from epublate.glossary.models import (
    GlossaryEntryWithAliases,
    GlossaryStatusLiteral,
)
from epublate.glossary.normalize import find_doubled_particles
from epublate.llm.prompts.translator import GlossaryConstraint, TargetOnlyConstraint

ViolationSeverity = Literal["error", "warning"]
ViolationKind = Literal["missing_locked_term", "doubled_particle"]


@dataclass(slots=True, frozen=True)
class Violation:
    """One glossary violation detected by :func:`validate_target`.

    ``severity`` is ``"error"`` for locked entries (the segment must be
    flagged) and ``"warning"`` for confirmed entries (the curator is
    informed but the translation is still considered valid).

    ``kind`` distinguishes *missing-term* violations (the historical
    behaviour: a locked source term appears in source but its target
    is missing) from *doubled-particle* warnings (PRD F-LB-3): the
    LLM emitted ``"na na Europa"`` style runs, typically because the
    glossary or the surrounding prose pushed it that way. Doubled
    particles always flag the segment for human review even though
    their severity is ``"warning"`` — see
    :func:`has_flagging_violation`.
    """

    entry_id: str
    source_term: str
    target_term: str
    matched_source: str
    severity: ViolationSeverity
    message: str
    kind: ViolationKind = "missing_locked_term"


# Statuses that the LLM ever sees. Order matters: the prompt sorts
# locked first so the model treats them as hardest constraints.
_PROMPT_STATUSES: tuple[GlossaryStatusLiteral, ...] = ("locked", "confirmed")


def build_constraints(
    entries: Iterable[GlossaryEntryWithAliases],
) -> list[GlossaryConstraint]:
    """Project entries to LLM-prompt-shape :class:`GlossaryConstraint` rows.

    ``proposed`` entries are excluded entirely (per
    ``glossary-invariants.mdc``: they don't constrain anything until a
    curator promotes them). The output is sorted ``locked`` before
    ``confirmed``, then alphabetically by source term, so identical
    glossaries always produce identical prompts (cache-stability).

    Target-only entries (``source_known=False`` / ``source_term`` is
    ``None``) are excluded too — they need a different prompt block
    that lives in :func:`build_target_only_constraints` /
    :func:`epublate.llm.prompts.translator._format_target_only_block`
    (introduced in Phase 3 of the Lore Books rollout). Skipping them
    here keeps the existing source-keyed constraint format intact.
    """

    by_status: dict[GlossaryStatusLiteral, list[GlossaryEntryWithAliases]] = {
        s: [] for s in _PROMPT_STATUSES
    }
    for ent in entries:
        if ent.status not in by_status:
            continue
        if ent.source_term is None:
            continue
        by_status[ent.status].append(ent)

    out: list[GlossaryConstraint] = []
    for status in _PROMPT_STATUSES:
        bucket = sorted(
            by_status[status],
            key=lambda e: (e.source_term or "", e.id),
        )
        for ent in bucket:
            assert ent.source_term is not None  # filtered above
            out.append(
                GlossaryConstraint(
                    source_term=ent.source_term,
                    target_term=ent.target_term,
                    type=ent.entry.type,
                    status=status,
                    notes=ent.entry.notes,
                    gender=ent.entry.gender,
                )
            )
    return out


def build_target_only_constraints(
    entries: Iterable[GlossaryEntryWithAliases],
) -> list[TargetOnlyConstraint]:
    """Project target-only entries to ``TargetOnlyConstraint`` rows.

    Filters in only entries whose ``source_term`` is ``None`` and
    ``status`` is ``locked`` or ``confirmed`` (proposed entries never
    constrain anything per ``glossary-invariants.mdc``). Output is
    sorted ``locked`` before ``confirmed``, then alphabetically by
    target term so identical glossaries hash identically (PRD F-LLM-6).
    """

    by_status: dict[GlossaryStatusLiteral, list[GlossaryEntryWithAliases]] = {
        s: [] for s in _PROMPT_STATUSES
    }
    for ent in entries:
        if ent.status not in by_status:
            continue
        if ent.source_term is not None:
            continue
        by_status[ent.status].append(ent)

    out: list[TargetOnlyConstraint] = []
    for status in _PROMPT_STATUSES:
        bucket = sorted(
            by_status[status],
            key=lambda e: (e.target_term, e.id),
        )
        for ent in bucket:
            target_aliases = tuple(sorted(set(ent.target_aliases)))
            out.append(
                TargetOnlyConstraint(
                    target_term=ent.target_term,
                    type=ent.entry.type,
                    status=status,
                    notes=ent.entry.notes,
                    target_aliases=target_aliases,
                    gender=ent.entry.gender,
                )
            )
    return out


def validate_target(
    *,
    source_text: str,
    target_text: str,
    entries: Sequence[GlossaryEntryWithAliases],
) -> list[Violation]:
    """Return every locked/confirmed glossary violation in ``target_text``.

    Algorithm:

    1. Run the source matcher to find which entries are actually
       relevant for this segment. We only flag an entry if its source
       term (or an alias) actually appears in the source — otherwise
       there's nothing to translate.
    2. For every relevant entry, check that its target term (or a
       target-side alias) appears in ``target_text``.
    3. Locked → ``severity="error"``; confirmed → ``severity="warning"``.

    Proposed entries are skipped entirely — they don't constrain
    anything yet (PRD F-LB-5).
    """

    relevant = _entries_with_source_match(source_text, entries)
    violations: list[Violation] = []

    for ent, hit in relevant:
        if ent.status == "proposed":
            continue
        if target_uses(target_text, ent):
            continue
        # Target-only locked entries (PRD F-LB-9) downgrade to a
        # warning: the curator pinned the canonical target form but
        # never authored a source spelling, so a missed match is more
        # likely a cross-language ambiguity than a translator bug.
        if ent.status == "locked" and not ent.source_known:
            severity: ViolationSeverity = "warning"
        else:
            severity = "error" if ent.status == "locked" else "warning"
        source_label = ent.source_term or hit.term
        violations.append(
            Violation(
                entry_id=ent.id,
                source_term=source_label,
                target_term=ent.target_term,
                matched_source=hit.term,
                severity=severity,
                message=(
                    f"{severity}: {ent.status} entry "
                    f"{source_label!r} → {ent.target_term!r} "
                    f"missing from target (matched source as {hit.term!r})"
                ),
            )
        )
    return violations


def has_locked_violation(violations: Iterable[Violation]) -> bool:
    """True if any of ``violations`` is locked-severity (PRD §4.3 F-LB-3)."""

    return any(v.severity == "error" for v in violations)


def has_flagging_violation(violations: Iterable[Violation]) -> bool:
    """True if any violation should flip the segment to ``flagged``.

    The pipeline's flag rule is "anything that should land on the
    curator's Inbox" — that's locked errors *plus* the
    ``doubled_particle`` warnings introduced by PRD F-LB-3. A
    confirmed-entry warning still does not flag (it just decorates
    the segment for the curator's review screen).
    """

    seen = list(violations)
    return any(v.severity == "error" for v in seen) or any(
        v.kind == "doubled_particle" for v in seen
    )


def find_target_doubled_particles(
    target_text: str,
    *,
    target_lang: str | None,
) -> list[Violation]:
    """Soft-warn when the LLM's target repeats a function word.

    Examples we want to catch (Portuguese):

    * ``"Na na Europa, há…"`` — symmetry-violating glossary entry
      ``Europe → na Europa`` doubled the preposition + article when
      the source said ``"In Europe, there is…"``.
    * ``"da da Câmara"`` — a similar collision around contractions.

    English equivalents (``"the the X"``, ``"a a X"``) are also
    flagged. Each violation is severity ``"warning"`` but ``kind``
    ``"doubled_particle"``, so :func:`has_flagging_violation` flips
    the segment to ``flagged`` even though no locked term was
    missing — the curator should look at the output.
    """

    out: list[Violation] = []
    for particle, offset in find_doubled_particles(target_text, lang=target_lang):
        out.append(
            Violation(
                entry_id="",
                source_term="",
                target_term=particle,
                matched_source="",
                severity="warning",
                kind="doubled_particle",
                message=(
                    f"target repeats the function word {particle!r} at "
                    f"char {offset} (e.g. '{particle} {particle}…') — "
                    "likely a glossary particle-symmetry mismatch "
                    "(PRD F-LB-3)."
                ),
            )
        )
    return out


def glossary_hash(entries: Iterable[GlossaryEntryWithAliases]) -> str:
    """Deterministic hash of the LLM-visible glossary state.

    Used as the ``glossary_hash`` component of the cache key
    (``epublate.core.cache``). The hash includes only fields the model
    or the validator depends on:

    * ``status`` (``proposed`` entries don't reach the prompt but they
      still affect the validator's "skip" set, so they're hashed),
    * canonical source/target term + sorted aliases on each side,
    * ``type`` and ``gender`` (sometimes surfaced in the prompt),
    * ``notes`` (we ship them to the model in the constraint block).

    Crucially, ``id``, ``created_at`` and ``updated_at`` are excluded
    so that re-importing the same glossary doesn't bust the cache.
    """

    canonical = []
    sorted_entries = sorted(
        entries,
        key=lambda e: (e.source_term or "", e.entry.target_term, e.id),
    )
    for ent in sorted_entries:
        canonical.append(
            {
                "type": ent.entry.type,
                "source_term": ent.source_term,
                "target_term": ent.target_term,
                "status": ent.status,
                "gender": ent.entry.gender,
                "notes": ent.entry.notes,
                "source_known": ent.source_known,
                "source_aliases": sorted(ent.source_aliases),
                "target_aliases": sorted(ent.target_aliases),
            }
        )
    payload = json.dumps(canonical, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def find_mentions(
    source_text: str,
    entries: Sequence[GlossaryEntryWithAliases],
) -> list[Match]:
    """Convenience wrapper around :func:`match_source` for the pipeline."""

    return match_source(source_text, entries)


def _entries_with_source_match(
    source_text: str,
    entries: Sequence[GlossaryEntryWithAliases],
) -> list[tuple[GlossaryEntryWithAliases, Match]]:
    by_id: dict[str, GlossaryEntryWithAliases] = {e.id: e for e in entries}
    seen: set[str] = set()
    out: list[tuple[GlossaryEntryWithAliases, Match]] = []
    for hit in match_source(source_text, entries):
        if hit.entry_id in seen:
            continue
        seen.add(hit.entry_id)
        out.append((by_id[hit.entry_id], hit))
    return out


__all__ = [
    "Violation",
    "ViolationKind",
    "ViolationSeverity",
    "build_constraints",
    "build_target_only_constraints",
    "find_mentions",
    "find_target_doubled_particles",
    "glossary_hash",
    "has_flagging_violation",
    "has_locked_violation",
    "validate_target",
]
