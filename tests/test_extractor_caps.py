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
