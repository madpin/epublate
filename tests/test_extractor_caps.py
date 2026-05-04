"""Parser caps for the helper-LLM extractor (PRD §8.2 / glossary quality)."""

from __future__ import annotations

import pytest

from epublate.llm.prompts.extractor import (
    EXTRACTOR_MAX_CHARS,
    EXTRACTOR_MAX_WORDS,
    parse_extractor_response,
)
from epublate.llm.prompts.extractor_target import parse_target_extractor_response


def _wrap(entities: list[dict[str, object]]) -> str:
    """Wrap entity dicts in the extractor's expected envelope."""

    import json

    return json.dumps({"entities": entities})


def _wrap_target(entities: list[dict[str, object]]) -> str:
    return _wrap(entities)


# ---------------------------------------------------------------------------
# Source-language extractor
# ---------------------------------------------------------------------------


def test_parser_drops_eat_your_chickens_sentence() -> None:
    """The exact long-sentence example the user reported."""

    payload = _wrap(
        [
            {
                "type": "phrase",
                "source": (
                    "First you will eat your chickens, then your goats, "
                    "then your cattle, then your donkeys."
                ),
                "target": (
                    "Primeiro vocês comerão suas galinhas, depois suas cabras, "
                    "depois seu gado, depois seus jumentos."
                ),
            }
        ]
    )
    trace = parse_extractor_response(payload)
    assert trace.entities == []


def test_parser_keeps_long_compound_name_under_caps() -> None:
    """``Heavily Indebted Poor Country (HIPC)`` is 5 words / 39 chars
    and should pass."""

    payload = _wrap(
        [
            {
                "type": "organization",
                "source": "Heavily Indebted Poor Country (HIPC)",
                "target": "Programa dos Países Pobres Altamente Endividados (HIPC)",
            }
        ]
    )
    trace = parse_extractor_response(payload)
    assert len(trace.entities) == 1
    assert trace.entities[0].source == "Heavily Indebted Poor Country (HIPC)"


def test_parser_drops_unbalanced_paren_source() -> None:
    """``Fédération … (FIFA`` with a missing close paren is rejected."""

    payload = _wrap(
        [
            {
                "type": "organization",
                "source": "Fédération Internationale de Football Association (FIFA",
                "target": "Federação Internacional de Futebol (FIFA)",
            }
        ]
    )
    trace = parse_extractor_response(payload)
    assert trace.entities == []


def test_parser_drops_source_over_word_cap() -> None:
    too_long = " ".join(["word"] * (EXTRACTOR_MAX_WORDS + 1))
    payload = _wrap([{"source": too_long}])
    trace = parse_extractor_response(payload)
    assert trace.entities == []


def test_parser_drops_source_over_char_cap() -> None:
    too_long = "x" * (EXTRACTOR_MAX_CHARS + 1)
    payload = _wrap([{"source": too_long}])
    trace = parse_extractor_response(payload)
    assert trace.entities == []


def test_parser_drops_multi_sentence_source() -> None:
    """Two sentence-final marks separated by a space — clearly a clause pair."""

    payload = _wrap([{"source": "He left. Then she ran."}])
    trace = parse_extractor_response(payload)
    assert trace.entities == []


def test_parser_keeps_jr_with_trailing_period() -> None:
    """A single trailing period that's part of a name (``Jr.``) is
    accepted: only ``!``/``?`` at end-of-string and any sentence
    punctuation followed by whitespace+content trigger the
    sentence reject."""

    payload = _wrap([{"source": "Sammy Davis Jr."}])
    trace = parse_extractor_response(payload)
    assert len(trace.entities) == 1
    assert trace.entities[0].source == "Sammy Davis Jr."


def test_parser_drops_target_when_target_violates_caps() -> None:
    """A bad ``target`` doesn't kill the whole entity — it just clears
    the field, so the curator can still curate the source side."""

    payload = _wrap(
        [
            {
                "source": "FIFA",
                "target": (
                    "Federação Internacional de Futebol Association is the "
                    "global governing body for football and runs many tournaments."
                ),
            }
        ]
    )
    trace = parse_extractor_response(payload)
    assert len(trace.entities) == 1
    assert trace.entities[0].source == "FIFA"
    assert trace.entities[0].target is None


def test_parser_drops_blank_after_strip() -> None:
    payload = _wrap([{"source": "   "}])
    trace = parse_extractor_response(payload)
    assert trace.entities == []


# ---------------------------------------------------------------------------
# Target-language extractor (Lore Book ingest)
# ---------------------------------------------------------------------------


def test_target_parser_drops_long_dialogue_quote() -> None:
    """A long quoted line that the helper might mistake for an entity."""

    payload = _wrap_target(
        [
            {
                "type": "phrase",
                "target": (
                    "Primeiro vocês comerão suas galinhas, depois suas cabras, "
                    "depois seu gado."
                ),
            }
        ]
    )
    trace = parse_target_extractor_response(payload)
    assert trace.entities == []


def test_target_parser_drops_unbalanced_paren_target() -> None:
    payload = _wrap_target(
        [
            {
                "type": "organization",
                "target": "Federação Internacional de Futebol (FIFA",
            }
        ]
    )
    trace = parse_target_extractor_response(payload)
    assert trace.entities == []


def test_target_parser_keeps_canonical_acronym() -> None:
    payload = _wrap_target(
        [
            {
                "type": "organization",
                "target": "FIFA",
                "aliases": ["Federação Internacional de Futebol"],
            }
        ]
    )
    trace = parse_target_extractor_response(payload)
    assert len(trace.entities) == 1
    assert trace.entities[0].target == "FIFA"
    assert "Federação Internacional de Futebol" in trace.entities[0].aliases


def test_target_parser_drops_alias_over_caps() -> None:
    too_long = " ".join(["alias"] * (EXTRACTOR_MAX_WORDS + 5))
    payload = _wrap_target(
        [
            {
                "type": "organization",
                "target": "FIFA",
                "aliases": ["Short ok", too_long],
            }
        ]
    )
    trace = parse_target_extractor_response(payload)
    assert trace.entities[0].aliases == ["Short ok"]


@pytest.mark.parametrize(
    "bad_source",
    [
        "Foo (Bar",
        "Foo) Bar",
        "[Foo Bar",
        "Foo {Bar",
    ],
)
def test_parser_rejects_all_unbalanced_bracket_shapes(bad_source: str) -> None:
    payload = _wrap([{"source": bad_source}])
    trace = parse_extractor_response(payload)
    assert trace.entities == []


# ---------------------------------------------------------------------------
# Year filter — raw year references must not pollute the lore bible
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "year_like_source",
    [
        # Pure 4-digit years, the curator-reported case.
        "1066",
        "1905",
        "2024",
        # 1-3 digit years (early CE / BCE history).
        "44",
        "476",
        # Year ranges with hyphen, en-dash, em-dash.
        "1939-1945",
        "1939\u20131945",
        "1939\u20141945",
        "1939-45",
        # Decades.
        "1990s",
        "1990S",
        # Era markers.
        "1066 AD",
        "44 BC",
        "44 BCE",
        "476 CE",
        # Era markers in other languages (PT/ES/FR a.C./d.C.; DE n./v.Chr.).
        "44 a.C.",
        "1066 d.C.",
        "44 v. Chr.",
        # Circa / approximate qualifiers.
        "c. 1066",
        "ca. 1905",
        "circa 1905",
        "approx. 2024",
        # Parenthesised year notation.
        "(1066)",
        "(1939-1945)",
        # Trailing punctuation (parser strips before checking).
        "1066.",
        "1066,",
    ],
)
def test_parser_drops_raw_year_source(year_like_source: str) -> None:
    """Years are date references, not entities — drop them at the parser.

    The helper LLM occasionally proposes a recurring year as a
    ``date_or_time`` entity, but the lore bible only tracks *named*
    eras (``the Long Night``, ``Yule``). Plain dates are handled
    inline by the translator and bloat the curator's review queue.
    Both the source-language and target-language extractors share
    :func:`_violates_extractor_caps` so the same predicate runs on
    every channel.
    """

    payload = _wrap(
        [
            {
                "type": "date_or_time",
                "source": year_like_source,
                "target": year_like_source,
            }
        ]
    )
    trace = parse_extractor_response(payload)
    assert trace.entities == [], (
        f"expected year-like source {year_like_source!r} to be dropped"
    )


@pytest.mark.parametrize(
    "kept_source",
    [
        # Year embedded in a phrase: kept (the textual prefix carries
        # entity meaning the curator may want tracked).
        "Year of the Four Emperors",
        "Battle of 1066",
        "World War 1939",
        "Apollo 11",
        "Order 66",
        # Single non-year number in a name.
        "Section 9",
        # Non-year number that happens to look small.
        "Catch-22",
    ],
)
def test_parser_keeps_year_alongside_text(kept_source: str) -> None:
    """A year prefixed or suffixed by entity text survives the filter.

    The year filter is conservative on purpose: only PURE year
    references (year, range, decade, with optional era / circa /
    paren wrappers) are dropped. Anything with non-numeric text in
    it stays so we don't accidentally strip ``Apollo 11`` or
    ``Year of the Four Emperors`` from the lore bible.
    """

    payload = _wrap([{"type": "event", "source": kept_source}])
    trace = parse_extractor_response(payload)
    assert len(trace.entities) == 1
    assert trace.entities[0].source == kept_source


def test_parser_drops_year_target_keeps_source() -> None:
    """A year-shaped ``target`` doesn't kill the entity.

    Mirrors the existing "long-target" path: a bad target is
    cleared (so the curator can fill it in by hand), but the
    source-side proposal still surfaces in the Inbox.
    """

    payload = _wrap(
        [
            {
                "type": "event",
                "source": "World War II",
                "target": "1939-1945",
            }
        ]
    )
    trace = parse_extractor_response(payload)
    assert len(trace.entities) == 1
    assert trace.entities[0].source == "World War II"
    assert trace.entities[0].target is None


def test_target_parser_drops_year_target() -> None:
    """The target-language extractor inherits the same filter."""

    payload = _wrap_target([{"type": "date_or_time", "target": "1939-1945"}])
    trace = parse_target_extractor_response(payload)
    assert trace.entities == []
