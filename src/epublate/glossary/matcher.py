"""Glossary matcher (PRD §4.2 phase 2 / M3).

Pure functions that find every glossary entry mentioned in a source
segment, and check whether a translated target text honored the
canonical translation. The pipeline calls this both before the LLM
call (to build the constraint list) and after (to validate locked
entries — see :mod:`epublate.glossary.enforcer`).

Matching strategy:

* The canonical source term and every registered source-side alias are
  candidates. We sort all candidates by length descending so the longer
  phrase wins ("Saint-Élise" beats "Élise" within the same hit).
* We anchor the regex with Unicode-aware boundary lookarounds
  (``(?<!\\w)`` / ``(?!\\w)``) instead of plain ``\\b`` because Python's
  ``\\b`` treats non-ASCII letters inconsistently in some locales. The
  matcher must work on names like "Élise", "Müller", "オーロラ".
* Matching is **case-sensitive** by default. Names with uppercase
  initial letters in literary fiction are deliberately distinct from
  the same word lowercased ("Hope" the character vs "hope" the noun),
  and the ``glossary-invariants.mdc`` rules treat them as different
  entries.
* Overlaps are resolved by the longest-first / leftmost-wins rule,
  which mirrors :mod:`re`'s default scanning behavior once we feed it a
  sorted alternation.

The output never contains DB rows: the matcher takes
:class:`~epublate.glossary.models.GlossaryEntryWithAliases` (the
projected, alias-joined view) so it stays trivially testable.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from epublate.glossary.models import GlossaryEntryWithAliases


@dataclass(slots=True, frozen=True)
class Match:
    """One source-side match of a glossary entry.

    ``term`` is the literal that triggered the match (the canonical
    source term or one of its aliases). ``span`` is a half-open
    ``(start, end)`` index pair into the original source string.
    """

    entry_id: str
    term: str
    start: int
    end: int

    @property
    def span(self) -> tuple[int, int]:
        return (self.start, self.end)


def make_pattern(terms: Sequence[str]) -> re.Pattern[str] | None:
    """Compile an alternation matching any of ``terms`` with word boundaries.

    Empty input → ``None`` (caller short-circuits). Terms are sorted by
    length descending so the longest match wins under leftmost-longest
    semantics in ``re.finditer``.

    Public so the cascade module can reuse the same boundary semantics
    when scanning previous target text without re-implementing the
    Unicode-aware lookarounds.
    """

    cleaned = [t for t in terms if t]
    if not cleaned:
        return None
    cleaned = sorted(set(cleaned), key=lambda s: (-len(s), s))
    alternation = "|".join(re.escape(t) for t in cleaned)
    # ``(?<!\w)`` / ``(?!\w)`` are Unicode-aware in Python 3 by default
    # (re uses \w == [a-zA-Z0-9_] *plus* Unicode letters/digits in str
    # patterns) which is what we want for non-ASCII proper nouns.
    return re.compile(rf"(?<!\w)(?:{alternation})(?!\w)")


def match_source(
    source_text: str,
    entries: Iterable[GlossaryEntryWithAliases],
) -> list[Match]:
    """Find every glossary mention in ``source_text``.

    Returns matches in the order they appear in the source. An entry
    that matches multiple times yields one :class:`Match` per occurrence
    so the validator and cascade can quote precise spans.
    """

    matches: list[Match] = []
    for ent in entries:
        pattern = make_pattern(ent.all_source_terms())
        if pattern is None:
            continue
        for m in pattern.finditer(source_text):
            matches.append(
                Match(
                    entry_id=ent.id,
                    term=m.group(0),
                    start=m.start(),
                    end=m.end(),
                )
            )
    matches.sort(key=lambda x: (x.start, x.end, x.entry_id))
    return matches


def target_uses(
    target_text: str,
    entry: GlossaryEntryWithAliases,
    *,
    accept_aliases: bool = True,
) -> bool:
    """Return ``True`` iff ``target_text`` contains ``entry``'s target term.

    If ``accept_aliases`` is set, target-side aliases also satisfy the
    check. Lookups use the same word-boundary regex as the source
    matcher so ``"Eli"`` does not satisfy a target term ``"Elisa"``.
    """

    candidates = entry.all_target_terms() if accept_aliases else [entry.target_term]
    pattern = make_pattern(candidates)
    if pattern is None:
        return False
    return pattern.search(target_text) is not None


__all__ = [
    "Match",
    "make_pattern",
    "match_source",
    "target_uses",
]
