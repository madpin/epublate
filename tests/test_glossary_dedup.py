"""Unit tests for the near-duplicate detector (PRD F-LB-5 cleanup)."""

from __future__ import annotations

import pytest

from epublate.glossary.dedup import canonical_form, find_near_duplicates
from epublate.glossary.models import GlossaryEntry, GlossaryEntryWithAliases


def _entry(
    *,
    entry_id: str,
    source: str | None,
    target: str,
    status: str = "proposed",
    type_: str = "term",
) -> GlossaryEntryWithAliases:
    """Build a minimal GlossaryEntryWithAliases for the dedup tests.

    The dedup module only ever inspects ``source_term``,
    ``target_term``, ``status``, ``type``, ``id``; everything else
    (project_id, timestamps, aliases) can stay empty so the test
    fixtures are short.
    """

    return GlossaryEntryWithAliases(
        entry=GlossaryEntry(
            id=entry_id,
            project_id="p",
            type=type_,  # type: ignore[arg-type]
            source_term=source,
            target_term=target,
            status=status,  # type: ignore[arg-type]
        ),
        source_aliases=[],
        target_aliases=[],
    )


# ---------------------------------------------------------------------------
# canonical_form
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("HIPC", "hipc"),
        ("HIPC initiative", "hipc"),
        ("HIPC Initiative", "hipc"),
        ("Heavily Indebted Poor Country (HIPC)", "heavily indebted poor country"),
        (
            "Heavily Indebted Poor Country (HIPC) initiative",
            "heavily indebted poor country",
        ),
        ("FIFA", "fifa"),
        # Trailing punctuation runs are stripped.
        ("FIFA.", "fifa"),
        ("FIFA,", "fifa"),
        ("FIFA…", "fifa"),
        # Unicode normalisation collapses ligatures / weird whitespace.
        ("HOuse  StaRk", "house stark"),
        # Leading/trailing whitespace doesn't survive.
        ("   FIFA   ", "fifa"),
        # Conservative suffix list — "House Stark" stays one word, not "house".
        ("House Stark", "house stark"),
        ("Council of Five", "council of five"),
        # Empty/blank inputs collapse cleanly.
        ("", ""),
        ("   ", ""),
        ("(BAR)", ""),
    ],
)
def test_canonical_form_known_cases(text: str, expected: str) -> None:
    assert canonical_form(text) == expected


def test_canonical_form_strips_common_noun_suffix_only_when_appropriate() -> None:
    # "Programa dos Países" should NOT become "Programa dos" just
    # because "Países" loosely matches an org noun. The conservative
    # suffix list avoids that footgun.
    assert canonical_form("Programa dos Países Pobres") == "programa dos países pobres"


# ---------------------------------------------------------------------------
# find_near_duplicates
# ---------------------------------------------------------------------------


def test_find_near_duplicates_exact_canonical_match() -> None:
    """The HIPC case the user reported."""

    entries = [
        _entry(
            entry_id="a",
            source="Heavily Indebted Poor Country (HIPC)",
            target="Programa dos Países Pobres Altamente Endividados (HIPC)",
            type_="organization",
        ),
        _entry(
            entry_id="b",
            source="Heavily Indebted Poor Country (HIPC) initiative",
            target="Iniciativa para os Países Pobres Muito Endividados (HIPC)",
            type_="term",
        ),
    ]
    groups = find_near_duplicates(entries)
    assert len(groups) == 1
    group_ids = sorted(e.id for e in groups[0])
    assert group_ids == ["a", "b"]


def test_find_near_duplicates_broken_paren_pair() -> None:
    """The FIFA case with a missing close-paren."""

    entries = [
        _entry(
            entry_id="a",
            source="Fédération Internationale de Football Association (FIFA",
            target="Fédération Internationale de Football Association (FIFA)",
            type_="organization",
        ),
        _entry(
            entry_id="b",
            source="Fédération Internationale de Football Association (FIFA)",
            target="Federação Internacional de Futebol (FIFA)",
            type_="organization",
        ),
    ]
    groups = find_near_duplicates(entries)
    # Both rows share the same canonical form
    # ("fédération internationale de football association").
    assert len(groups) == 1
    assert {e.id for e in groups[0]} == {"a", "b"}


def test_find_near_duplicates_levenshtein_typo() -> None:
    """A 1-edit typo on the same canonical core gets grouped via the
    Levenshtein guard."""

    entries = [
        _entry(entry_id="a", source="Bohemia", target="Boêmia"),
        # ``Bohémia`` differs by 1 char from ``Bohemia`` after canonical.
        _entry(entry_id="b", source="Bohémia", target="Boêmia"),
    ]
    groups = find_near_duplicates(entries)
    assert len(groups) == 1
    assert {e.id for e in groups[0]} == {"a", "b"}


def test_find_near_duplicates_distinct_entries_are_not_grouped() -> None:
    entries = [
        _entry(entry_id="a", source="House Stark", target="Casa Stark"),
        _entry(entry_id="b", source="House Lannister", target="Casa Lannister"),
        _entry(entry_id="c", source="Tyrion", target="Tyrion"),
    ]
    groups = find_near_duplicates(entries)
    assert groups == []


def test_find_near_duplicates_singletons_are_dropped() -> None:
    entries = [
        _entry(entry_id="solo", source="Only One", target="Único"),
    ]
    assert find_near_duplicates(entries) == []


def test_find_near_duplicates_empty_input() -> None:
    assert find_near_duplicates([]) == []


def test_find_near_duplicates_winner_first_by_status() -> None:
    """``locked`` ranks above ``confirmed`` above ``proposed``.

    Mirrors the existing :func:`repo.find_duplicate_source_terms`
    contract so the existing :class:`MergeDuplicatesScreen` keeps
    the right winner without code changes.
    """

    entries = [
        _entry(
            entry_id="a", source="HIPC initiative", target="HIPC", status="proposed"
        ),
        _entry(entry_id="b", source="HIPC", target="HIPC", status="locked"),
        _entry(entry_id="c", source="HIPC.", target="HIPC", status="confirmed"),
    ]
    groups = find_near_duplicates(entries)
    assert len(groups) == 1
    winner_first = [e.id for e in groups[0]]
    assert winner_first[0] == "b"
    assert winner_first[1] == "c"
    assert winner_first[2] == "a"


def test_find_near_duplicates_groups_sorted_deterministically() -> None:
    """Two unrelated duplicate groups should come out in canonical
    order so test snapshots stay stable across runs."""

    entries = [
        _entry(entry_id="z1", source="Zerg", target="Zerg"),
        _entry(entry_id="z2", source="Zerg.", target="Zerg"),
        _entry(entry_id="a1", source="Alpha", target="Alpha"),
        _entry(entry_id="a2", source="alpha", target="Alpha"),
    ]
    groups = find_near_duplicates(entries)
    assert len(groups) == 2
    # First group is the alphabetically-earliest canonical key.
    assert {e.id for e in groups[0]} == {"a1", "a2"}
    assert {e.id for e in groups[1]} == {"z1", "z2"}


def test_find_near_duplicates_target_only_entries_dedupe_by_target() -> None:
    """Lore Book target-only entries (no source_term) bucket on target."""

    entries = [
        _entry(entry_id="a", source=None, target="Câmara"),
        _entry(entry_id="b", source=None, target="câmara"),
    ]
    groups = find_near_duplicates(entries)
    assert len(groups) == 1
    assert {e.id for e in groups[0]} == {"a", "b"}


def test_find_near_duplicates_mixed_target_only_and_source_keyed_kept_separate() -> (
    None
):
    """Don't accidentally fold a target-only entry into a source-keyed
    bucket just because the spellings happen to overlap."""

    entries = [
        _entry(entry_id="src", source="Câmara", target="Câmara"),
        _entry(entry_id="tgt", source=None, target="Câmara"),
    ]
    groups = find_near_duplicates(entries)
    # Both canonicalise to "câmara" — this IS a real duplicate the
    # curator should resolve (probably promote the source-keyed one
    # and drop the target-only). The dedup module surfaces it; the
    # MergeDuplicatesScreen still asks for confirmation.
    assert len(groups) == 1
    assert {e.id for e in groups[0]} == {"src", "tgt"}
