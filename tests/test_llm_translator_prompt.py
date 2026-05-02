"""Translator prompt builder + parser tests (PRD §8.1, F-LLM-3)."""

from __future__ import annotations

import pytest

from epublate.errors import LLMResponseError
from epublate.llm.prompts.translator import (
    GlossaryConstraint,
    TargetOnlyConstraint,
    build_group_translator_messages,
    build_translator_messages,
    parse_translator_response,
)


def test_build_messages_has_system_and_user() -> None:
    messages = build_translator_messages(
        source_lang="en",
        target_lang="pt",
        source_text="The [[T0]]old[[/T0]] man.",
    )
    assert len(messages) == 2
    assert messages[0].role == "system"
    assert messages[1].role == "user"
    assert messages[1].content == "The [[T0]]old[[/T0]] man."


def test_system_prompt_lists_languages_and_placeholder_rules() -> None:
    [system, _] = build_translator_messages(
        source_lang="en",
        target_lang="pt-BR",
        source_text="hello",
    )
    assert "en" in system.content
    assert "pt-BR" in system.content
    assert "[[T0]]" in system.content
    assert "JSON" in system.content


def test_glossary_block_segregates_statuses() -> None:
    glossary = [
        GlossaryConstraint(source_term="Élise", target_term="Elisa", status="locked"),
        GlossaryConstraint(
            source_term="London", target_term="Londres", status="confirmed"
        ),
        GlossaryConstraint(
            source_term="Coffer", target_term="Cofre", status="proposed"
        ),
    ]
    [system, _] = build_translator_messages(
        source_lang="en",
        target_lang="pt",
        source_text="x",
        glossary=glossary,
    )
    locked_idx = system.content.index("locked entries")
    confirmed_idx = system.content.index("confirmed entries")
    proposed_idx = system.content.index("proposed entries")
    assert locked_idx < confirmed_idx < proposed_idx
    assert "Élise → Elisa" in system.content
    assert "London → Londres" in system.content


def test_style_guide_appears_in_system_prompt() -> None:
    [system, _] = build_translator_messages(
        source_lang="en",
        target_lang="pt",
        source_text="x",
        style_guide="formal register; no contractions",
    )
    assert "formal register" in system.content


def test_empty_source_rejected() -> None:
    with pytest.raises(ValueError):
        build_translator_messages(source_lang="en", target_lang="pt", source_text="")


def test_target_only_block_renders_when_provided() -> None:
    target_only = [
        TargetOnlyConstraint(
            target_term="Geralt de Rívia",
            type="character",
            status="locked",
            notes="protagonist",
        ),
        TargetOnlyConstraint(
            target_term="Vesemir",
            type="character",
            status="confirmed",
            target_aliases=("velho lobo",),
        ),
    ]
    [system, _] = build_translator_messages(
        source_lang="en",
        target_lang="pt",
        source_text="x",
        target_only_glossary=target_only,
    )
    assert "Canonical target terms used in this work" in system.content
    assert "Geralt de Rívia" in system.content
    assert "Vesemir" in system.content
    assert "aliases: velho lobo" in system.content
    assert "MUST translate it using the canonical" in system.content
    locked_idx = system.content.index("locked target forms")
    confirmed_idx = system.content.index("confirmed target forms")
    assert locked_idx < confirmed_idx


def test_target_only_block_omitted_when_empty() -> None:
    [system, _] = build_translator_messages(
        source_lang="en",
        target_lang="pt",
        source_text="x",
    )
    assert "Canonical target terms used in this work" not in system.content


def test_target_only_block_skips_proposed_only() -> None:
    target_only = [
        TargetOnlyConstraint(
            target_term="Maybe-Term",
            type="term",
            status="proposed",
        ),
    ]
    [system, _] = build_translator_messages(
        source_lang="en",
        target_lang="pt",
        source_text="x",
        target_only_glossary=target_only,
    )
    assert "Canonical target terms used in this work" not in system.content


def test_group_messages_inject_target_only_block() -> None:
    target_only = [
        TargetOnlyConstraint(
            target_term="Ciri",
            type="character",
            status="locked",
        ),
    ]
    [system, _] = build_group_translator_messages(
        source_lang="en",
        target_lang="pt",
        source_items=[(1, "hello"), (2, "world")],
        target_only_glossary=target_only,
    )
    assert "Canonical target terms used in this work" in system.content
    assert "Ciri" in system.content


def test_parse_well_formed_json() -> None:
    payload = (
        '{"target": "Olá [[T0]]bravo[[/T0]] mundo.", '
        '"used_entries": [], "new_entities": [], "notes": null}'
    )
    trace = parse_translator_response(payload)
    assert trace.target == "Olá [[T0]]bravo[[/T0]] mundo."
    assert trace.used_entries == []
    assert trace.notes is None


def test_parse_recovers_from_prose_wrapping() -> None:
    payload = 'Sure! Here is the JSON:\n{"target": "ok"}\nThanks.'
    trace = parse_translator_response(payload)
    assert trace.target == "ok"


def test_parse_used_entries_and_new_entities() -> None:
    payload = (
        '{"target": "Elisa caminhou.", '
        '"used_entries": ["Élise"], '
        '"new_entities": [{"type": "place", "source": "Vale Verde"}], '
        '"notes": "first appearance"}'
    )
    trace = parse_translator_response(payload)
    assert trace.used_entries == ["Élise"]
    assert trace.new_entities == [{"type": "place", "source": "Vale Verde"}]
    assert trace.notes == "first appearance"


def test_parse_rejects_garbage() -> None:
    with pytest.raises(LLMResponseError):
        parse_translator_response("this is not even close to JSON")


def test_parse_rejects_empty() -> None:
    with pytest.raises(LLMResponseError):
        parse_translator_response("")


def test_parse_rejects_missing_target() -> None:
    with pytest.raises(LLMResponseError):
        parse_translator_response('{"used_entries": []}')


def test_parse_rejects_non_string_target() -> None:
    with pytest.raises(LLMResponseError):
        parse_translator_response('{"target": 123}')


def test_parse_rejects_non_object_root() -> None:
    with pytest.raises(LLMResponseError):
        parse_translator_response("[1, 2, 3]")


def test_parse_rejects_invalid_used_entries_type() -> None:
    with pytest.raises(LLMResponseError):
        parse_translator_response('{"target": "x", "used_entries": "nope"}')


def test_parse_rejects_invalid_new_entity_shape() -> None:
    with pytest.raises(LLMResponseError):
        parse_translator_response('{"target": "x", "new_entities": ["not-a-dict"]}')
