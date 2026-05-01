"""Group translator prompt + parser tests.

The grouped path batches short, placeholder-free segments into one
LLM round-trip so we amortize cost/latency for lists, indices, and
tables of contents. The prompt/parser pair is the narrow boundary
between the pipeline and the model; these tests pin the contract.
"""

from __future__ import annotations

import json

import pytest

from epublate.errors import LLMResponseError
from epublate.llm.prompts.translator import (
    GlossaryConstraint,
    build_group_translator_messages,
    parse_group_translator_response,
)


def test_group_builder_emits_system_plus_json_user() -> None:
    messages = build_group_translator_messages(
        source_lang="en",
        target_lang="pt",
        source_items=[(1, "Chapter 1"), (2, "Chapter 2"), (3, "Index")],
    )
    assert len(messages) == 2
    assert messages[0].role == "system"
    assert messages[1].role == "user"
    payload = json.loads(messages[1].content)
    assert payload == {
        "items": [
            {"id": 1, "source": "Chapter 1"},
            {"id": 2, "source": "Chapter 2"},
            {"id": 3, "source": "Index"},
        ]
    }


def test_group_system_prompt_mentions_batch_contract() -> None:
    [system, _] = build_group_translator_messages(
        source_lang="en",
        target_lang="pt",
        source_items=[(1, "Hello")],
    )
    assert "BATCH" in system.content
    assert "translations" in system.content
    assert "id" in system.content


def test_group_builder_forwards_glossary() -> None:
    [system, _] = build_group_translator_messages(
        source_lang="en",
        target_lang="pt",
        source_items=[(1, "London")],
        glossary=[
            GlossaryConstraint(
                source_term="London", target_term="Londres", status="locked"
            ),
        ],
    )
    assert "London → Londres" in system.content
    assert "locked entries" in system.content


def test_group_builder_rejects_empty_items() -> None:
    with pytest.raises(ValueError):
        build_group_translator_messages(
            source_lang="en", target_lang="pt", source_items=[]
        )


def test_group_builder_rejects_duplicate_ids() -> None:
    with pytest.raises(ValueError):
        build_group_translator_messages(
            source_lang="en",
            target_lang="pt",
            source_items=[(1, "a"), (1, "b")],
        )


def test_parse_group_response_happy_path() -> None:
    payload = json.dumps(
        {
            "translations": [
                {"id": 1, "target": "Capítulo 1"},
                {"id": 2, "target": "Capítulo 2"},
            ]
        }
    )
    parsed = parse_group_translator_response(payload, expected_ids=[1, 2])
    assert [item.id for item in parsed.translations] == [1, 2]
    assert parsed.translations[0].target == "Capítulo 1"


def test_parse_group_tolerates_extra_ids_but_rejects_missing() -> None:
    payload = json.dumps(
        {
            "translations": [
                {"id": 1, "target": "Capítulo 1"},
                {"id": 99, "target": "stray"},
            ]
        }
    )
    parsed = parse_group_translator_response(payload, expected_ids=[1])
    assert {item.id for item in parsed.translations} == {1, 99}

    with pytest.raises(LLMResponseError):
        parse_group_translator_response(payload, expected_ids=[1, 2])


def test_parse_group_rejects_duplicate_ids() -> None:
    payload = json.dumps(
        {
            "translations": [
                {"id": 1, "target": "a"},
                {"id": 1, "target": "b"},
            ]
        }
    )
    with pytest.raises(LLMResponseError):
        parse_group_translator_response(payload)


def test_parse_group_rejects_garbage_and_empty() -> None:
    with pytest.raises(LLMResponseError):
        parse_group_translator_response("")
    with pytest.raises(LLMResponseError):
        parse_group_translator_response("not json at all")


def test_parse_group_recovers_from_prose_wrapping() -> None:
    prose = (
        "Sure, here you go:\n"
        '{"translations": [{"id": 1, "target": "Olá"}]}\n'
        "Let me know if you need more."
    )
    parsed = parse_group_translator_response(prose, expected_ids=[1])
    assert parsed.translations[0].target == "Olá"


def test_parse_group_rejects_missing_translations_field() -> None:
    with pytest.raises(LLMResponseError):
        parse_group_translator_response('{"notes": "nope"}')


def test_parse_group_rejects_non_object_translations() -> None:
    with pytest.raises(LLMResponseError):
        parse_group_translator_response('{"translations": "oops"}')
