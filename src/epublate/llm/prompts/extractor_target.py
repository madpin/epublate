"""Helper-LLM extractor prompt for *target-language* Lore Book ingest.

The translation pipeline's regular extractor (PRD §8.2) reads source
text and proposes ``proposed`` glossary entries keyed by their source
form. For a Lore Book built from an *already-translated* edition (PRD
F-LB-3 / F-LB-10) the situation is reversed: the curator only has the
target text, and we want the model to surface the *canonical target*
spellings so the translator pipeline can use them as soft-locked
constraints in future projects.

The prompt is structurally similar to the regular extractor, but:

* the input is in the **target language**,
* every emitted entity has a ``target`` field (no ``source``),
* aliases are also target-side (e.g. "Ciri" alongside "Cirilla"),
* the JSON schema is documented inline so a permissive endpoint
  doesn't drift away from the contract.

Like its sibling :mod:`epublate.llm.prompts.extractor`, this module is
pure: it builds messages and parses responses. The Lore Book ingest
helper owns the LLM call, the cache lookup, and the upserts.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from epublate.errors import LLMResponseError
from epublate.llm.base import Message, ResponseFormat
from epublate.llm.prompts.translator import GlossaryConstraint

DEFAULT_RESPONSE_FORMAT: ResponseFormat = ResponseFormat(type="json_object")
"""Default ``response_format`` for the target-language extractor.

Same rationale as :data:`epublate.llm.prompts.extractor.DEFAULT_RESPONSE_FORMAT`:
the prompt mandates JSON-only output, so we ask the endpoint to
constrain decoding to a JSON object. Reasoning helpers
(``gpt-oss-20b`` and friends) otherwise consume the visible-channel
budget on reasoning tokens and return empty content.
"""

EntityTypeLiteral = Literal[
    "character",
    "place",
    "organization",
    "event",
    "item",
    "date_or_time",
    "phrase",
    "term",
    "other",
]
"""Mirror of :data:`epublate.glossary.models.EntityType`."""

_VALID_TYPES: frozenset[str] = frozenset(
    [
        "character",
        "place",
        "organization",
        "event",
        "item",
        "date_or_time",
        "phrase",
        "term",
        "other",
    ]
)


class TargetExtractedEntity(BaseModel):
    """One target-only candidate entity returned by the helper LLM."""

    model_config = ConfigDict(extra="forbid")

    type: EntityTypeLiteral = "term"
    target: str
    aliases: list[str] = Field(default_factory=list)
    evidence: str | None = None
    confidence: float = 0.0


class TargetExtractorTrace(BaseModel):
    """Parsed helper-model response for a target-language pass."""

    model_config = ConfigDict(extra="forbid")

    entities: list[TargetExtractedEntity] = Field(default_factory=list)
    notes: str | None = None


_SYSTEM_PROMPT_TEMPLATE = """\
You are a literary entity extractor working on a long-form story book
that has already been translated. Your job is to read a chunk of the
TARGET-language text in {target_lang} and return a structured list of
the recurring proper-noun entities that appear in it. The translator
pipeline will reuse these canonical target spellings to keep future
translations of related books (e.g. other volumes in the same series)
consistent.

What to surface:

* characters (named people / beings),
* places (cities, regions, buildings, named geography),
* organizations, factions, guilds, families,
* events (battles, festivals, ceremonies),
* items (named weapons, artifacts, vehicles, books),
* date_or_time markers (named eras, calendars, recurring holidays),
* recurring phrases or in-world terms whose translation must stay
  identical across the series,
* anything else worth keeping in the lore book — use ``other``.

Hard rules:

1. Every ``target`` field MUST be the exact spelling that appears in
   the chunk I give you (in {target_lang}). Do not back-translate to
   {source_lang}; the source spelling is unknown for this entry.
2. Skip entries already present in the existing Lore Book glossary
   below — they are settled. Do not propose synonyms or aliases of
   ``locked`` terms.
3. ``aliases`` is an optional list of additional target-side spellings
   (nicknames, short forms, alternative transliterations) you observed
   in the chunk for the same entity.
4. ``confidence`` is a number between 0.0 and 1.0; use 1.0 only when
   the chunk makes the entity unambiguous.

{glossary_block}\
Respond with a single JSON object and nothing else:

{{
  "entities": [
    {{"type": "character|place|organization|event|item|date_or_time|phrase|term|other",
     "target": "<surface form as in the target text>",
     "aliases": ["<other target spelling>", ...],
     "evidence": "<short quote or paraphrase>",
     "confidence": 0.0}}
  ],
  "notes": "optional free-text observations for the curator, or omit"
}}

Do not wrap the JSON in code fences. Do not add commentary.\
"""


def build_target_extractor_messages(
    *,
    target_lang: str,
    target_text: str,
    glossary: Sequence[GlossaryConstraint] = (),
) -> list[Message]:
    """Build the chat messages for one target-language extractor call."""

    if not target_text or not target_text.strip():
        raise ValueError("target_text must not be empty")

    glossary_block = _format_glossary_block(glossary)
    system_content = _SYSTEM_PROMPT_TEMPLATE.format(
        target_lang=target_lang,
        source_lang="the source language",
        glossary_block=glossary_block,
    )

    return [
        Message(role="system", content=system_content),
        Message(role="user", content=target_text),
    ]


def _format_glossary_block(glossary: Sequence[GlossaryConstraint]) -> str:
    if not glossary:
        return "Existing Lore Book glossary: (empty — propose freely).\n\n"
    lines = ["Existing Lore Book glossary (do not re-propose these):"]
    for entry in glossary:
        if entry.status == "proposed":
            continue
        lines.append(f"  - [{entry.type}] {entry.target_term} ({entry.status})")
    if len(lines) == 1:
        return "Existing Lore Book glossary: (empty — propose freely).\n\n"
    lines.append("")
    return "\n".join(lines) + "\n"


_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)


def parse_target_extractor_response(content: str) -> TargetExtractorTrace:
    """Parse the helper LLM's JSON response, with a permissive fallback."""

    if not content or not content.strip():
        raise LLMResponseError("target extractor response was empty")

    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        match = _JSON_OBJECT_RE.search(content)
        if match is None:
            raise LLMResponseError(
                "target extractor response is not JSON and contains no JSON object"
            ) from None
        try:
            data = json.loads(match.group(0))
        except json.JSONDecodeError as exc:
            raise LLMResponseError(
                f"failed to recover JSON from target extractor response: {exc}"
            ) from exc

    if not isinstance(data, dict):
        raise LLMResponseError(
            "target extractor response top-level must be a JSON object; "
            f"got {type(data).__name__}"
        )

    entities_raw = data.get("entities", [])
    if not isinstance(entities_raw, list):
        raise LLMResponseError("target extractor 'entities' must be a list")
    entities: list[TargetExtractedEntity] = []
    for raw in entities_raw:
        normalized = _normalize_entity(raw)
        if normalized is not None:
            entities.append(normalized)

    notes = data.get("notes")
    if notes is not None and not isinstance(notes, str):
        raise LLMResponseError("target extractor 'notes' must be a string")

    return TargetExtractorTrace(entities=entities, notes=notes)


def _normalize_entity(raw: Any) -> TargetExtractedEntity | None:
    """Coerce one ``entities`` item to :class:`TargetExtractedEntity` or drop it."""

    if not isinstance(raw, dict):
        raise LLMResponseError("each entry in 'entities' must be a JSON object")
    target = raw.get("target") or raw.get("target_term")
    if not isinstance(target, str):
        return None
    target = target.strip()
    if not target:
        return None

    type_str = str(raw.get("type", "term")).strip().lower() or "term"
    if type_str not in _VALID_TYPES:
        type_str = "term"

    raw_aliases = raw.get("aliases", [])
    if raw_aliases is None:
        aliases: list[str] = []
    elif isinstance(raw_aliases, list):
        aliases = []
        for item in raw_aliases:
            if not isinstance(item, str):
                raise LLMResponseError("entity 'aliases' must be a list of strings")
            cleaned = item.strip()
            if cleaned and cleaned != target:
                aliases.append(cleaned)
    else:
        raise LLMResponseError("entity 'aliases' must be a list of strings")

    evidence_raw = raw.get("evidence")
    evidence: str | None
    if evidence_raw is None:
        evidence = None
    elif isinstance(evidence_raw, str):
        evidence = evidence_raw.strip() or None
    else:
        raise LLMResponseError("entity 'evidence' must be a string or null")

    confidence_raw = raw.get("confidence", 0.0)
    if isinstance(confidence_raw, bool):
        raise LLMResponseError("entity 'confidence' must be a number")
    if isinstance(confidence_raw, int | float):
        confidence = float(confidence_raw)
    else:
        try:
            confidence = float(confidence_raw)
        except (TypeError, ValueError) as exc:
            raise LLMResponseError("entity 'confidence' must be a number") from exc
    confidence = max(0.0, min(1.0, confidence))

    return TargetExtractedEntity(
        type=type_str,  # type: ignore[arg-type]
        target=target,
        aliases=aliases,
        evidence=evidence,
        confidence=confidence,
    )


__all__ = [
    "DEFAULT_RESPONSE_FORMAT",
    "EntityTypeLiteral",
    "TargetExtractedEntity",
    "TargetExtractorTrace",
    "build_target_extractor_messages",
    "parse_target_extractor_response",
]
