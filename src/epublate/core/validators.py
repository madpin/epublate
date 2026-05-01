"""Mechanical post-translation validators (PRD §4.2 phase 5).

M2 ships the structural placeholder validator: every placeholder issued in
the source must appear exactly once in the target, openers must pair with
their closers, and void placeholders must not have a closer. This is the
last gate before the pipeline writes a translation back to the DB.

Glossary-aware validators (locked-term enforcement, length sanity,
source-language leak detection) land in M3 alongside the lore bible.

Format-handling rule §2:
    Every placeholder issued in the source must appear exactly once in
    the target. Mismatch → validator fails; the segment is flagged,
    never spliced.
"""

from __future__ import annotations

from epublate.core.segmentation import PLACEHOLDER_RE
from epublate.errors import FormatError
from epublate.formats.base import Segment


def validate_segment_placeholders(seg: Segment) -> None:
    """Hard-fail if ``seg``'s placeholders don't match its skeleton.

    Validates whichever of ``target_text`` / ``source_text`` is present
    (target preferred). Used both by the format adapter on reassembly and
    by the translation pipeline before persisting an LLM result.
    """

    text = seg.target_text if seg.target_text is not None else seg.source_text
    open_counts: dict[int, int] = {}
    close_counts: dict[int, int] = {}
    void_counts: dict[int, int] = {}
    for m in PLACEHOLDER_RE.finditer(text):
        idx = int(m.group(2))
        if idx < 0 or idx >= len(seg.inline_skeleton):
            raise FormatError(
                f"segment {seg.id}: placeholder [[T{idx}]] has no skeleton entry"
            )
        is_close = m.group(1) == "/"
        token = seg.inline_skeleton[idx]
        if token.kind == "void":
            if is_close:
                raise FormatError(
                    f"segment {seg.id}: void placeholder [[/T{idx}]] cannot close"
                )
            void_counts[idx] = void_counts.get(idx, 0) + 1
        elif is_close:
            close_counts[idx] = close_counts.get(idx, 0) + 1
        else:
            open_counts[idx] = open_counts.get(idx, 0) + 1

    for idx, token in enumerate(seg.inline_skeleton):
        if token.kind == "void":
            if void_counts.get(idx, 0) != 1:
                raise FormatError(
                    f"segment {seg.id}: void placeholder [[T{idx}]] "
                    "missing or duplicated"
                )
        else:
            if open_counts.get(idx, 0) != 1 or close_counts.get(idx, 0) != 1:
                raise FormatError(
                    f"segment {seg.id}: placeholder [[T{idx}]] is not "
                    "matched once-and-only-once"
                )

    for idx in close_counts:
        if idx not in open_counts:
            raise FormatError(
                f"segment {seg.id}: closing placeholder [[/T{idx}]] without opener"
            )


__all__ = ["validate_segment_placeholders"]
