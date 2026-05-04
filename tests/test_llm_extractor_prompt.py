"""Helper-LLM extractor prompt builder + parser tests (PRD §8.2 / M5)."""

from __future__ import annotations

import pytest

from epublate.errors import LLMResponseError
from epublate.llm.prompts.extractor import (
    ExtractedEntity,
    ExtractorTrace,
    build_extractor_messages,
    parse_extractor_response,
)
from epublate.llm.prompts.translator import GlossaryConstraint


def test_build_messages_has_system_and_user() -> None:
    messages = build_extractor_messages(
        source_lang="en",
        target_lang="pt",
        source_text="Élise opened the door.",
    )
    assert len(messages) == 2
    assert messages[0].role == "system"
    assert messages[1].role == "user"
    assert messages[1].content == "Élise opened the door."


def test_system_prompt_lists_languages_and_entity_taxonomy() -> None:
    [system, _] = build_extractor_messages(
        source_lang="en",
        target_lang="pt-BR",
        source_text="hello",
    )
    assert "en" in system.content
    assert "pt-BR" in system.content
    # PRD F-LB-1 entity types should all be advertised so the model knows
    # the expected vocabulary.
    for kind in ("character", "place", "organization", "event", "item"):
        assert kind in system.content
    assert "JSON" in system.content


def test_extractor_prompt_warns_against_raw_year_proposals() -> None:
    """The extractor prompt explicitly bans raw year references.

    The parser-side filter (:func:`_violates_extractor_caps` /
    :func:`_is_year_like`) is the load-bearing guard, but spelling
    the rule out in the prompt cuts the failure rate at the
    extractor: the model gets a worked example of "this isn't an
    entity" instead of being silently filtered out downstream. Lock
    the wording in so prompt drift is auditable, the same way the
    "no full sentences" rule is audited.
    """

    [system, _] = build_extractor_messages(
        source_lang="en",
        target_lang="pt",
        source_text="The Battle of 1066 changed everything.",
    )
    body = " ".join(system.content.split())
    assert "Never propose a raw year reference" in body
    # The example anchors what counts as "raw year" so the model
    # has zero ambiguity on year ranges and decades.
    assert "1066" in body
    assert "1939-1945" in body
    # The carve-out for *named* eras / phrases must travel too —
    # without it the model could over-correct and drop legitimate
    # entries like ``Year of the Four Emperors``.
    assert "named" in body
    assert "Year of the Four Emperors" in body


def test_glossary_block_lists_existing_locked_and_confirmed() -> None:
    glossary = [
        GlossaryConstraint(source_term="Élise", target_term="Elisa", status="locked"),
        GlossaryConstraint(
            source_term="London", target_term="Londres", status="confirmed"
        ),
    ]
    [system, _] = build_extractor_messages(
        source_lang="en",
        target_lang="pt",
        source_text="x",
        glossary=glossary,
    )
    assert "Existing glossary" in system.content
    assert "Élise → Elisa" in system.content
    assert "London → Londres" in system.content


def test_glossary_block_skips_proposed_status() -> None:
    glossary = [
        GlossaryConstraint(
            source_term="Coffer", target_term="Cofre", status="proposed"
        ),
    ]
    [system, _] = build_extractor_messages(
        source_lang="en",
        target_lang="pt",
        source_text="x",
        glossary=glossary,
    )
    assert "Coffer" not in system.content


def test_empty_source_rejected() -> None:
    with pytest.raises(ValueError):
        build_extractor_messages(source_lang="en", target_lang="pt", source_text="")
    with pytest.raises(ValueError):
        build_extractor_messages(source_lang="en", target_lang="pt", source_text="   ")


def test_parse_well_formed_json() -> None:
    payload = (
        '{"entities": [{"type": "character", "source": "Élise", '
        '"evidence": "Élise opened the door.", "confidence": 0.9}], '
        '"pov": "third_limited", "tense": "past"}'
    )
    trace = parse_extractor_response(payload)
    assert len(trace.entities) == 1
    ent = trace.entities[0]
    assert ent.type == "character"
    assert ent.source == "Élise"
    assert ent.evidence == "Élise opened the door."
    assert ent.confidence == 0.9
    assert trace.pov == "third_limited"
    assert trace.tense == "past"


def test_parse_recovers_from_prose_wrapping() -> None:
    payload = 'Sure! Here is the result:\n{"entities": []}\nThanks.'
    trace = parse_extractor_response(payload)
    assert trace.entities == []


def test_parse_clamps_confidence_into_range() -> None:
    payload = (
        '{"entities": ['
        '{"source": "A", "confidence": 5.0},'
        '{"source": "B", "confidence": -1.0}'
        "]}"
    )
    trace = parse_extractor_response(payload)
    assert trace.entities[0].confidence == 1.0
    assert trace.entities[1].confidence == 0.0


def test_parse_unknown_type_collapses_to_term() -> None:
    payload = '{"entities": [{"type": "spaceship", "source": "Vale Verde"}]}'
    trace = parse_extractor_response(payload)
    assert trace.entities[0].type == "term"


def test_parse_drops_entries_missing_source() -> None:
    payload = (
        '{"entities": [{"type": "character"}, {"source": "Elise"}, {"source": "   "}]}'
    )
    trace = parse_extractor_response(payload)
    assert len(trace.entities) == 1
    assert trace.entities[0].source == "Elise"


def test_parse_accepts_alias_source_term_key() -> None:
    payload = '{"entities": [{"source_term": "Atlas"}]}'
    trace = parse_extractor_response(payload)
    assert trace.entities[0].source == "Atlas"


def test_parse_rejects_garbage() -> None:
    with pytest.raises(LLMResponseError):
        parse_extractor_response("this is not even close to JSON")


def test_parse_rejects_empty() -> None:
    with pytest.raises(LLMResponseError):
        parse_extractor_response("")


def test_parse_rejects_non_object_root() -> None:
    with pytest.raises(LLMResponseError):
        parse_extractor_response("[1, 2, 3]")


def test_parse_rejects_invalid_entities_type() -> None:
    with pytest.raises(LLMResponseError):
        parse_extractor_response('{"entities": "nope"}')


def test_parse_rejects_non_dict_entity() -> None:
    with pytest.raises(LLMResponseError):
        parse_extractor_response('{"entities": ["not-a-dict"]}')


def test_parse_rejects_non_string_pov() -> None:
    with pytest.raises(LLMResponseError):
        parse_extractor_response('{"entities": [], "pov": 3}')


def test_parse_rejects_non_string_evidence() -> None:
    with pytest.raises(LLMResponseError):
        parse_extractor_response('{"entities": [{"source": "X", "evidence": 12}]}')


def test_parse_rejects_bool_confidence() -> None:
    with pytest.raises(LLMResponseError):
        parse_extractor_response('{"entities": [{"source": "X", "confidence": true}]}')


def test_extractor_trace_pydantic_validation() -> None:
    # Smoke-test: roundtrip through the model so type contracts are
    # enforced by pydantic, not just by the parser.
    trace = ExtractorTrace(
        entities=[ExtractedEntity(type="place", source="Vale Verde")],
        pov="third_limited",
    )
    assert trace.entities[0].source == "Vale Verde"
    assert trace.tense is None


def test_system_prompt_advertises_register_and_audience_taxonomy() -> None:
    """PRD F-STYLE-3: helper LLM is asked for a register + audience tag.

    The intake summary's ``suggested_style_profile`` derives from these
    two values via :func:`epublate.core.style.suggest_style_profile`,
    so the prompt has to enumerate the canonical answer set the
    suggester recognizes.
    """

    [system, _] = build_extractor_messages(
        source_lang="en", target_lang="pt", source_text="hello"
    )
    assert "register" in system.content
    assert "audience" in system.content
    for token in (
        "literary",
        "explicit",
        "technical",
        "academic",
        "journalistic",
    ):
        assert token in system.content
    for token in (
        "children",
        "middle_grade",
        "young_adult",
        "adult",
    ):
        assert token in system.content


def test_parse_extracts_register_and_audience() -> None:
    payload = (
        '{"entities": [], "pov": null, "tense": null, '
        '"register": "literary", "audience": "adult"}'
    )
    trace = parse_extractor_response(payload)
    assert trace.narrative_register == "literary"
    assert trace.narrative_audience == "adult"


def test_parse_tolerates_missing_register_audience() -> None:
    """Older (cached) responses won't have these fields — parser must cope."""

    payload = '{"entities": [], "pov": "first", "tense": "present"}'
    trace = parse_extractor_response(payload)
    assert trace.narrative_register is None
    assert trace.narrative_audience is None


def test_parse_register_must_be_string_or_null() -> None:
    """Pydantic-style strictness for the new fields."""

    with pytest.raises(LLMResponseError):
        parse_extractor_response('{"entities": [], "register": 7}')
    with pytest.raises(LLMResponseError):
        parse_extractor_response('{"entities": [], "audience": 3.14}')


def test_parse_register_audience_blank_string_collapses_to_none() -> None:
    payload = '{"entities": [], "register": "   ", "audience": ""}'
    trace = parse_extractor_response(payload)
    assert trace.narrative_register is None
    assert trace.narrative_audience is None
