"""Target-language extractor prompt builder + parser tests (Lore Books / F-LB-3)."""

from __future__ import annotations

import pytest

from epublate.errors import LLMResponseError
from epublate.llm.prompts.extractor_target import (
    TargetExtractorTrace,
    build_target_extractor_messages,
    parse_target_extractor_response,
)
from epublate.llm.prompts.translator import GlossaryConstraint


def test_build_target_messages_has_system_and_user() -> None:
    messages = build_target_extractor_messages(
        target_lang="pt",
        target_text="Geralt entrou em Kaer Morhen.",
    )
    assert len(messages) == 2
    assert messages[0].role == "system"
    assert messages[1].role == "user"
    assert messages[1].content == "Geralt entrou em Kaer Morhen."


def test_target_prompt_uses_target_language_and_target_field() -> None:
    [system, _] = build_target_extractor_messages(
        target_lang="pt-BR",
        target_text="Geralt entrou em Kaer Morhen.",
    )
    assert "pt-BR" in system.content
    # The schema must instruct the model to emit ``target`` (not
    # ``source``) so the Lore Book gets canonical target spellings.
    assert '"target":' in system.content
    assert '"aliases":' in system.content


def test_target_prompt_omits_glossary_when_empty() -> None:
    [system, _] = build_target_extractor_messages(
        target_lang="pt",
        target_text="x",
    )
    assert "empty" in system.content


def test_target_prompt_lists_existing_constraints() -> None:
    glossary = [
        GlossaryConstraint(source_term="Geralt", target_term="Geralt", status="locked"),
        GlossaryConstraint(
            source_term="Kaer Morhen",
            target_term="Kaer Morhen",
            status="confirmed",
        ),
    ]
    [system, _] = build_target_extractor_messages(
        target_lang="pt",
        target_text="x",
        glossary=glossary,
    )
    assert "Geralt" in system.content
    assert "Kaer Morhen" in system.content
    # Proposed entries must NOT appear (the curator hasn't vetted them).
    glossary_with_proposed = [
        *glossary,
        GlossaryConstraint(
            source_term="Yennefer", target_term="Yennefer", status="proposed"
        ),
    ]
    [system_with_proposed, _] = build_target_extractor_messages(
        target_lang="pt",
        target_text="x",
        glossary=glossary_with_proposed,
    )
    assert "Yennefer" not in system_with_proposed.content


def test_build_messages_rejects_empty_target_text() -> None:
    with pytest.raises(ValueError):
        build_target_extractor_messages(target_lang="pt", target_text="   ")


def test_parse_response_returns_typed_entities() -> None:
    payload = """
    {
      "entities": [
        {"type": "character", "target": "Geralt", "aliases": ["Bruxo"],
         "evidence": "Geralt levantou o medalhão.", "confidence": 0.92},
        {"type": "place", "target": "Kaer Morhen", "confidence": 0.8}
      ],
      "notes": "Predominantemente medieval."
    }
    """
    trace = parse_target_extractor_response(payload)
    assert isinstance(trace, TargetExtractorTrace)
    assert trace.notes == "Predominantemente medieval."
    assert len(trace.entities) == 2
    assert trace.entities[0].target == "Geralt"
    assert trace.entities[0].aliases == ["Bruxo"]
    assert trace.entities[0].type == "character"
    assert trace.entities[1].target == "Kaer Morhen"
    assert trace.entities[1].aliases == []


def test_parse_response_drops_entries_without_target() -> None:
    payload = """
    {"entities": [
      {"type": "character", "target": "Geralt"},
      {"type": "character"},
      {"type": "place", "target": "  "}
    ]}
    """
    trace = parse_target_extractor_response(payload)
    assert [e.target for e in trace.entities] == ["Geralt"]


def test_parse_response_recovers_json_from_prose() -> None:
    payload = 'Sure! Here\'s the JSON:\n{"entities": [{"target": "Yennefer"}]}'
    trace = parse_target_extractor_response(payload)
    assert trace.entities[0].target == "Yennefer"


def test_parse_response_rejects_invalid_aliases_shape() -> None:
    payload = '{"entities": [{"target": "X", "aliases": [1, 2, 3]}]}'
    with pytest.raises(LLMResponseError):
        parse_target_extractor_response(payload)


def test_parse_response_rejects_non_object_top_level() -> None:
    with pytest.raises(LLMResponseError):
        parse_target_extractor_response("[1, 2, 3]")


def test_parse_response_rejects_empty_payload() -> None:
    with pytest.raises(LLMResponseError):
        parse_target_extractor_response("   ")


def test_parse_response_rejects_invalid_confidence() -> None:
    payload = '{"entities": [{"target": "X", "confidence": true}]}'
    with pytest.raises(LLMResponseError):
        parse_target_extractor_response(payload)


def test_parse_response_clamps_confidence_to_unit_interval() -> None:
    payload = (
        '{"entities": ['
        '{"target": "X", "confidence": 1.7},'
        '{"target": "Y", "confidence": -0.4}'
        "]}"
    )
    trace = parse_target_extractor_response(payload)
    assert trace.entities[0].confidence == 1.0
    assert trace.entities[1].confidence == 0.0


def test_parse_response_normalizes_unknown_types_to_term() -> None:
    payload = '{"entities": [{"target": "X", "type": "outlaw"}]}'
    trace = parse_target_extractor_response(payload)
    assert trace.entities[0].type == "term"


def test_target_extracted_entity_strips_aliases_equal_to_target() -> None:
    # We trim aliases that duplicate the canonical target term so the
    # downstream upsert doesn't store the redundant alias.
    payload = '{"entities": [{"target": "Yennefer", "aliases": ["Yennefer", "Yen"]}]}'
    trace = parse_target_extractor_response(payload)
    assert trace.entities[0].aliases == ["Yen"]
