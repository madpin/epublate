"""Tests for glossary matcher / target_uses (PRD §4.2 phase 2 / M3)."""

from __future__ import annotations

from epublate.glossary.matcher import Match, make_pattern, match_source, target_uses
from epublate.glossary.models import (
    GlossaryEntry,
    GlossaryEntryWithAliases,
)


def _entry(
    source: str,
    target: str,
    *,
    status: str = "confirmed",
    src_aliases: list[str] | None = None,
    tgt_aliases: list[str] | None = None,
    type_: str = "character",
    eid: str = "e1",
) -> GlossaryEntryWithAliases:
    return GlossaryEntryWithAliases(
        entry=GlossaryEntry(
            id=eid,
            project_id="p",
            type=type_,  # type: ignore[arg-type]
            source_term=source,
            target_term=target,
            status=status,  # type: ignore[arg-type]
        ),
        source_aliases=src_aliases or [],
        target_aliases=tgt_aliases or [],
    )


def test_exact_word_boundary_match() -> None:
    entry = _entry("Élise", "Elisa")
    matches = match_source("It was Élise who arrived first.", [entry])
    assert len(matches) == 1
    m = matches[0]
    assert m.entry_id == "e1"
    assert m.term == "Élise"
    assert m.span[0] == 7  # "It was " is 7 chars


def test_no_match_when_substring_only() -> None:
    entry = _entry("Eli", "Eli")
    # ``Elise`` should not match ``Eli`` because of word boundary.
    matches = match_source("Elise walked in.", [entry])
    assert matches == []


def test_alias_matches_too() -> None:
    entry = _entry("Élise", "Elisa", src_aliases=["Lise"])
    matches = match_source("Hi Lise! And later, Élise.", [entry])
    terms = sorted(m.term for m in matches)
    assert terms == ["Lise", "Élise"]


def test_longest_alternation_wins() -> None:
    entry = _entry(
        "Saint-Élise",
        "Santa-Elisa",
        src_aliases=["Élise"],
    )
    matches = match_source("They met Saint-Élise yesterday.", [entry])
    terms = [m.term for m in matches]
    # Saint-Élise (longer) should win over the bare Élise alias.
    assert terms == ["Saint-Élise"]


def test_multiple_entries_and_overlapping_text() -> None:
    a = _entry("Élise", "Elisa", eid="a")
    b = _entry("Hugo", "Hugo", eid="b")
    matches = match_source("Hugo and Élise sat. Élise smiled.", [a, b])
    assert sorted((m.entry_id, m.term, m.start) for m in matches) == sorted(
        [
            ("b", "Hugo", 0),
            ("a", "Élise", 9),
            ("a", "Élise", 20),
        ]
    )


def test_target_uses_with_alias() -> None:
    entry = _entry("Élise", "Elisa", tgt_aliases=["Eli"])
    assert target_uses("Eli was tired.", entry)
    assert target_uses("Elisa was tired.", entry)
    assert not target_uses("Mary was tired.", entry)


def test_target_uses_word_boundary() -> None:
    entry = _entry("Élise", "Eli")
    assert not target_uses("Elise was tired.", entry)
    assert target_uses("Eli was tired.", entry)


def test_make_pattern_handles_empty() -> None:
    assert make_pattern([]) is None
    assert make_pattern([""]) is None


def test_match_object_is_frozen_dataclass() -> None:
    m = Match(entry_id="e", term="t", start=0, end=1)
    assert m.span == (0, 1)


def test_hyphenated_compound_matches_as_single_term() -> None:
    """A hyphenated compound entry like ``boot-lickers`` matches as one unit.

    Regression test for the user-reported "boot-lickers" miss: once an
    extractor proposes the compound term, the matcher must surface it
    in segments that contain it.
    """

    entry = _entry("boot-lickers", "puxa-sacos", type_="phrase", status="proposed")
    matches = match_source("Look at these boot-lickers around the king.", [entry])
    assert len(matches) == 1
    assert matches[0].term == "boot-lickers"
    assert matches[0].span == (14, 26)

    # And the bare suffix entry must still tokenize correctly when it
    # is genuinely the only thing in the glossary — we accept the bare
    # ``lickers`` matching inside a hyphenated compound (regex word
    # boundary semantics) so the extractor's prompt remains the right
    # place to ask for the longer canonical form.
    bare = _entry("lickers", "lambedores", type_="phrase")
    bare_matches = match_source("Look at these boot-lickers around the king.", [bare])
    assert [m.term for m in bare_matches] == ["lickers"]
