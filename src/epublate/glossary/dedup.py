"""Near-duplicate glossary entry detection (PRD §4.3 / F-LB-5 cleanup).

The auto-proposer used to dedupe by exact ``source_term`` match,
which let near-misses past:

* "Heavily Indebted Poor Country (HIPC)" vs.
  "Heavily Indebted Poor Country (HIPC) initiative",
* "Fédération Internationale de Football Association (FIFA"
  (broken paren) vs.
  "Fédération Internationale de Football Association (FIFA)",
* "House Lannister" vs. "house lannister".

The post-fix in :mod:`epublate.glossary.io` (``upsert_proposed``)
prevents *new* near-duplicates by canonicalising before lookup. This
module is the **cleanup** half — given a project's existing entries,
return groups of near-duplicates the curator should review through
the standard ``MergeDuplicatesScreen``. It is a pure function over
:class:`GlossaryEntryWithAliases`; no DB access, fully unit-testable.

Two-stage matching:

1. **Canonical-form bucketing** — :func:`canonical_form` strips case,
   trailing punctuation, parenthesised acronym suffixes, and a small
   list of common-noun suffixes ("initiative", "council", "company",
   "society", "program", …). Buckets of size > 1 are duplicate
   groups.
2. **Levenshtein guard for residuals** — entries whose canonical
   forms aren't equal but are within 2 edits and share a 3+
   character prefix get merged into the same group. This catches
   typos that survive canonicalisation (``HIPC`` vs ``HIPCs``,
   ``Bohemia`` vs ``Bohémia``).

Per ``glossary-invariants.mdc`` §4 ("no silent merges"), this module
NEVER mutates anything — it only surfaces candidate groups. The
``GlossaryScreen`` action that calls it routes the result through
:class:`MergeDuplicatesScreen` so the curator confirms each group.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable

from epublate.glossary.models import GlossaryEntryWithAliases

# Common-noun suffixes the helper LLM tends to attach to organisation
# / event names ("X initiative", "X program"). Matched case-insensitively
# at end-of-string after the trailing acronym is removed. Conservative
# list — adding too many turns "House Stark" into "House" and merges
# unrelated rows.
_TRAILING_NOUN_SUFFIXES: tuple[str, ...] = (
    "initiative",
    "initiatives",
    "program",
    "programs",
    "programme",
    "programmes",
    "council",
    "committee",
    "council of",
    "society",
    "association",
    "organization",
    "organisation",
    "company",
    "corporation",
    "incorporated",
    "limited",
)

_TRAILING_NOUN_RE = re.compile(
    r"\s+(?:" + "|".join(re.escape(s) for s in _TRAILING_NOUN_SUFFIXES) + r")$",
    re.IGNORECASE,
)

# Strip a trailing parenthesised acronym suffix: ``"Foo (BAR)"`` -> ``"Foo"``.
# Matched on the lowercased+stripped form so it catches ``"Foo (bar)"`` too.
# The second alternative handles the broken-paren case the user
# reported (``"Foo (BAR"`` with no close paren) — without it, the
# raw and broken spellings end up in different canonical buckets.
_PAREN_ACRONYM_RE = re.compile(r"\s*\([^()]*\)\s*$|\s*\([^()]*$")

# Trailing punctuation we always strip before comparing canonical
# forms. Includes ASCII and the most common typographic equivalents
# (em-dash, en-dash, ellipsis, middle dot). Built up from explicit
# escapes / chars rather than ``\u2026...`` literals so ``ruff``'s
# ambiguous-character check doesn't flag the chained typographic
# characters.
_TRAILING_PUNCT = ".,;:!?·-" + "\u2026\u2014\u2013"


def canonical_form(term: str) -> str:
    """Return the canonical comparison form for ``term``.

    The same string is used both by the live ``upsert_proposed`` dedup
    path (so two near-duplicates never both land as new entries) and
    by :func:`find_near_duplicates` (so historical entries that
    sneaked in before this fix get surfaced for cleanup). Keeping the
    helper here in ``glossary/`` rather than ``glossary/io.py`` lets
    both callers import it without circular dependency risk.

    Steps, in order:

    1. NFKC unicode normalisation so ``"FIFA"`` and the ligature
       ``"ﬁ FA"`` collapse to the same string.
    2. Lowercase.
    3. Strip leading/trailing whitespace.
    4. Strip a trailing punctuation run (``"hipc."`` -> ``"hipc"``).
    5. Collapse internal whitespace runs to a single space.
    6. **Repeatedly** strip the trailing common-noun suffix
       (``"foo initiative"`` -> ``"foo"``) and the trailing
       parenthesised acronym suffix (``"foo (bar)"`` -> ``"foo"``).
       Done in a small fixed-point loop because the two strips
       interact: ``"foo (bar) initiative"`` needs the suffix stripped
       first to expose the parens, but ``"(foo) initiative"`` needs
       the parens stripped first to expose ``"initiative"``.
    7. Strip trailing whitespace + trailing punctuation again, in case
       the suffix-strip exposed a comma the curator left behind.

    Returns an empty string when nothing meaningful is left. Empty
    canonical forms are excluded from bucketing in
    :func:`find_near_duplicates` so two empty-string entries don't
    get falsely grouped together.
    """

    if not term:
        return ""
    text = unicodedata.normalize("NFKC", term).strip().lower()
    text = text.rstrip(_TRAILING_PUNCT).strip()
    text = re.sub(r"\s+", " ", text)
    # Fixed-point loop: each pass may expose a previously-hidden
    # suffix the next pass can strip. Bounded to a small number of
    # iterations as a safety net — three suffix layers in a row is
    # already pathological for a real entity name.
    for _ in range(4):
        before = text
        text = _TRAILING_NOUN_RE.sub("", text).strip()
        text = _PAREN_ACRONYM_RE.sub("", text).strip()
        if text == before:
            break
    return text.rstrip(_TRAILING_PUNCT).strip()


def _levenshtein(a: str, b: str, *, cap: int = 3) -> int:
    """Bounded Levenshtein distance.

    Returns ``cap`` immediately if the two strings differ in length by
    more than ``cap``; otherwise computes the standard edit distance.
    Cap defaults to 3 because the only fuzzy bucket we care about is
    "≤ 2 edits" (one transposition, one missing character, one
    accent), and giving the loop the extra slack lets us short-circuit
    on the first row that exceeds it.
    """

    if a == b:
        return 0
    if abs(len(a) - len(b)) >= cap:
        return cap
    if len(a) > len(b):
        a, b = b, a
    previous = list(range(len(a) + 1))
    for j, cb in enumerate(b, start=1):
        current = [j]
        row_min = j
        for i, ca in enumerate(a, start=1):
            insert = current[i - 1] + 1
            delete = previous[i] + 1
            substitute = previous[i - 1] + (0 if ca == cb else 1)
            value = min(insert, delete, substitute)
            current.append(value)
            if value < row_min:
                row_min = value
        if row_min >= cap:
            # Whole row is >= cap, so no path through can finish under cap.
            return cap
        previous = current
    return min(previous[-1], cap)


def _key_side(entry: GlossaryEntryWithAliases) -> str:
    """Pick the canonical-form key for ``entry``.

    Project-scoped entries dedupe on the source term (the contract
    the auto-proposer actually enforces). Target-only entries (Lore
    Book ingest) dedupe on the target term — they have no source by
    definition.
    """

    if entry.source_term:
        return canonical_form(entry.source_term)
    return canonical_form(entry.target_term)


def find_near_duplicates(
    entries: Iterable[GlossaryEntryWithAliases],
    *,
    fuzzy_distance: int = 2,
    fuzzy_prefix: int = 3,
) -> list[list[GlossaryEntryWithAliases]]:
    """Group ``entries`` by canonical form + a Levenshtein guard.

    Returns one list per duplicate group, each group sorted with the
    "winner" first using the same criteria as
    :func:`epublate.db.repo.find_duplicate_source_terms`:

    * status priority (``locked`` > ``confirmed`` > ``proposed``),
    * specific type (anything other than ``term``) over generic
      ``term``,
    * older entry first as a tiebreaker.

    The same shape lets the existing
    :class:`epublate.app.screens.glossary.MergeDuplicatesScreen`
    render the result without modification — the curator sees the
    "Cleanup duplicates" flow as a strict superset of the existing
    "Merge dupes" flow (which only catches exact source-term
    matches).

    Singletons (canonical bucket of size 1 with no fuzzy neighbour)
    are omitted. The order of the returned groups is stable: groups
    are sorted by canonical form for deterministic test snapshots.
    """

    materialised = list(entries)
    if not materialised:
        return []

    # 1. Canonical-form bucketing.
    buckets: dict[str, list[GlossaryEntryWithAliases]] = {}
    keys_for: dict[str, str] = {}
    for entry in materialised:
        key = _key_side(entry)
        if not key:
            continue
        keys_for[entry.id] = key
        buckets.setdefault(key, []).append(entry)

    # 2. Fuzzy union-find over the bucket keys. Every bucket starts
    # as its own group; if two keys are within ``fuzzy_distance`` and
    # share the first ``fuzzy_prefix`` chars we merge them.
    parent: dict[str, str] = {key: key for key in buckets}

    def _find(k: str) -> str:
        while parent[k] != k:
            parent[k] = parent[parent[k]]
            k = parent[k]
        return k

    def _union(a: str, b: str) -> None:
        ra, rb = _find(a), _find(b)
        if ra == rb:
            return
        # Deterministic root: lexicographically smaller wins, so the
        # group ordering survives test runs unchanged.
        if ra < rb:
            parent[rb] = ra
        else:
            parent[ra] = rb

    keys = sorted(buckets)
    if fuzzy_distance > 0:
        for i, k1 in enumerate(keys):
            for k2 in keys[i + 1 :]:
                if k1 == k2:
                    continue
                if not k1 or not k2:
                    continue
                # Cheap prefix gate: avoid running Levenshtein on
                # everything when most pairs are obviously different.
                prefix = min(fuzzy_prefix, len(k1), len(k2))
                if k1[:prefix] != k2[:prefix]:
                    continue
                if _levenshtein(k1, k2, cap=fuzzy_distance + 1) <= fuzzy_distance:
                    _union(k1, k2)

    # 3. Collapse buckets that union-find merged into the same root.
    grouped: dict[str, list[GlossaryEntryWithAliases]] = {}
    for key, items in buckets.items():
        root = _find(key)
        grouped.setdefault(root, []).extend(items)

    status_rank = {"locked": 0, "confirmed": 1, "proposed": 2}

    def _winner_key(e: GlossaryEntryWithAliases) -> tuple[int, int, str]:
        return (
            status_rank.get(e.status, 99),
            0 if e.entry.type != "term" else 1,
            e.id,
        )

    groups: list[list[GlossaryEntryWithAliases]] = []
    for root in sorted(grouped):
        members = grouped[root]
        if len(members) < 2:
            continue
        members.sort(key=_winner_key)
        groups.append(members)
    return groups


__all__ = [
    "canonical_form",
    "find_near_duplicates",
]
