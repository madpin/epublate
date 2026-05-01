"""Cascade re-translation flow (PRD §7.5 / M3, F-LB-7).

When a curator changes a confirmed/locked entry's ``target_term`` (or
promotes a proposed entry to locked), every previously translated
segment that touched the old terminology is potentially wrong. This
module computes the affected set and rolls them back to ``pending`` so
the next translate-loop produces a fresh, consistent result.

The flow has two phases by design:

1. :func:`compute_affected` — read-only, side-effect free, fast. The
   curator sees a count + a sample list before the cascade actually
   touches segments. This is the "preview" the TUI binds to.
2. :func:`cascade_retranslate` — runs in a single transaction
   (db-and-persistence rule §2): it captures each segment's prior
   translation in the event log so history is never lost
   (``glossary-invariants.mdc`` §4), flips the status to ``pending``,
   and inserts a ``glossary.cascaded`` event with the count.

Affected = source contains the entry's source-term/alias **OR** target
contains the previous target term (PRD F-LB-7). The matcher on the
source side avoids substring false-positives (``"Eli"`` in ``"Elise"``
shouldn't trigger a cascade for ``"Eli"``); the previous-target check
uses the same word-boundary regex.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy.engine import Engine

from epublate.db import repo, schema
from epublate.glossary.matcher import make_pattern
from epublate.glossary.models import GlossaryEntryWithAliases


@dataclass(slots=True, frozen=True)
class CascadeCandidate:
    """One segment a glossary change could plausibly invalidate.

    ``reason`` indicates *why* this segment was selected so the curator
    knows whether to expect source-side or target-side drift.
    """

    segment_id: str
    chapter_id: str
    idx: int
    source_text: str
    target_text: str | None
    status: str
    reason: str


def compute_affected(
    engine: Engine,
    *,
    project_id: str,
    entry: GlossaryEntryWithAliases,
    prev_target_term: str | None,
) -> list[CascadeCandidate]:
    """Return segments whose translation may need to be redone.

    A segment is included if either:

    * its ``source_text`` contains the entry's canonical source term or
      one of its source-side aliases (matched with word boundaries), OR
    * ``prev_target_term`` is non-empty and its ``target_text``
      contains that previous term (also matched with word boundaries).

    Already-pending segments are excluded — they're going to be
    re-translated anyway.
    """

    src_pattern = make_pattern(entry.all_source_terms())
    tgt_pattern = make_pattern([prev_target_term]) if prev_target_term else None
    if src_pattern is None and tgt_pattern is None:
        return []

    candidates: list[CascadeCandidate] = []
    for seg in repo.list_segments_for_project(engine, project_id):
        if seg.status == schema.SegmentStatus.PENDING:
            continue
        src_hit = bool(src_pattern and src_pattern.search(seg.source_text))
        tgt_hit = bool(
            tgt_pattern and seg.target_text and tgt_pattern.search(seg.target_text)
        )
        if not (src_hit or tgt_hit):
            continue
        if src_hit and tgt_hit:
            reason = "source+target match"
        elif src_hit:
            reason = "source match"
        else:
            reason = "previous target match"
        candidates.append(
            CascadeCandidate(
                segment_id=seg.id,
                chapter_id=seg.chapter_id,
                idx=seg.idx,
                source_text=seg.source_text,
                target_text=seg.target_text,
                status=seg.status,
                reason=reason,
            )
        )
    return candidates


def cascade_retranslate(
    engine: Engine,
    *,
    project_id: str,
    entry: GlossaryEntryWithAliases,
    prev_target_term: str | None,
    new_target_term: str | None,
    candidates: Sequence[CascadeCandidate],
    reason: str | None = None,
) -> int:
    """Apply the cascade: revert segments to ``pending`` and audit.

    The full operation runs in **one transaction** so a crash never
    leaves the project in a half-cascaded state (resumability rule).
    Each segment's prior ``target_text`` is preserved in an
    ``segment.cascaded`` event payload before its status flips, so the
    "preserve history" invariant holds even though we then null out the
    column for the next translate round.
    """

    if not candidates:
        return 0

    with engine.begin() as conn:
        for cand in candidates:
            repo.append_event(
                conn,
                project_id=project_id,
                kind="segment.cascaded",
                payload={
                    "segment_id": cand.segment_id,
                    "entry_id": entry.id,
                    "prev_target_term": prev_target_term,
                    "new_target_term": new_target_term,
                    "prior_target_text": cand.target_text,
                    "prior_status": cand.status,
                    "reason": cand.reason,
                },
            )
            repo.update_segment_translation(
                conn,
                segment_id=cand.segment_id,
                target_text=None,
                status=schema.SegmentStatus.PENDING,
            )
        repo.append_event(
            conn,
            project_id=project_id,
            kind="glossary.cascaded",
            payload={
                "entry_id": entry.id,
                "source_term": entry.source_term,
                "prev_target_term": prev_target_term,
                "new_target_term": new_target_term,
                "affected_count": len(candidates),
                "reason": reason,
            },
        )
    return len(candidates)


__all__ = [
    "CascadeCandidate",
    "cascade_retranslate",
    "compute_affected",
]
