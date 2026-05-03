"""Helper-LLM extractor prompt builder + response parser (PRD §8.2 / M5).

The extractor is the cheap, helper-model side of the pipeline (PRD F-LLM-2).
It runs in two contexts:

* **Book intake** (PRD §7.1 step 5) — a one-shot pass over the first few
  segments of a freshly-created project, seeding the lore bible with
  ``proposed`` characters, places, and so on plus a draft narrative POV
  / tense.
* **Batch pre-pass** (PRD §4.2 step 3) — a per-chapter scan that runs
  before the translator loop so the translator's prompt sees the new
  candidates immediately.

Like :mod:`epublate.llm.prompts.translator`, this module is pure: it
turns project state into ``Message`` objects and turns the model's
response back into a typed :class:`ExtractorTrace`. The pipeline owns
the LLM call, the cache lookup, and the DB writes.

The prompt deliberately:

* lists the existing locked / confirmed glossary so the model does not
  re-propose terms the curator already vetted,
* enumerates the entity-type taxonomy from PRD F-LB-1 so the response
  shape stays predictable,
* requires a single JSON object response so :func:`parse_extractor_response`
  can stay strict.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Sequence
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from epublate.errors import LLMResponseError
from epublate.llm.base import Message, ResponseFormat
from epublate.llm.prompts.translator import GlossaryConstraint

_logger = logging.getLogger(__name__)

# Caps for an extractor candidate's ``source`` (or ``target``) field.
# A glossary entry is supposed to be a name or short fixed phrase the
# translator must keep consistent — anything longer than this is almost
# always a sentence the helper LLM mistakenly proposed. Real proper
# nouns ("Heavily Indebted Poor Country (HIPC) initiative") sit
# comfortably inside both caps; the eat-your-chickens example that
# motivated the caps blows past both. Tightening further has been
# observed to drop legitimate long compound names; loosening lets
# noisy sentences in.
EXTRACTOR_MAX_WORDS = 10
EXTRACTOR_MAX_CHARS = 100

# Sentence-final punctuation that signals "this candidate is a clause,
# not an entity". A single trailing period is sometimes part of a name
# ("Jr.", "St."), so we only reject when the period is followed by
# whitespace and more characters (i.e. mid-string punctuation), or
# when there are *two* sentence-final marks anywhere — ``"He left.
# Then she ran."`` is unambiguously a sentence pair.
_MULTI_SENTENCE_RE = re.compile(r"[.!?]\s+\S")
_TRAILING_SENTENCE_RE = re.compile(r"[!?](?:\s|$)")


def _looks_like_sentence(term: str) -> bool:
    """True if ``term`` reads as a clause/sentence rather than an entity."""

    if _MULTI_SENTENCE_RE.search(term):
        return True
    return bool(_TRAILING_SENTENCE_RE.search(term))


def _has_unbalanced_parens(term: str) -> bool:
    """True when parens / brackets don't close in ``term``.

    Catches the broken-paren auto-proposed entries the curator's been
    seeing (``Fédération Internationale de Football Association (FIFA``
    with no closing paren). Conservative: only counts ASCII pairs the
    extractor prompt's example shape uses; mismatched fancy quotes
    aren't enough to drop a candidate.
    """

    return (
        term.count("(") != term.count(")")
        or term.count("[") != term.count("]")
        or term.count("{") != term.count("}")
    )


def _violates_extractor_caps(term: str) -> str | None:
    """Return a debug-level reason string when ``term`` should be dropped.

    Used by both extractor parsers (source-language and target-language)
    so a sentence proposal, a runaway phrase, or a broken-paren
    candidate dies at the parser boundary. Returns ``None`` when the
    candidate is acceptable.
    """

    cleaned = term.strip()
    if not cleaned:
        return "empty after strip"
    if len(cleaned) > EXTRACTOR_MAX_CHARS:
        return f"length {len(cleaned)} > {EXTRACTOR_MAX_CHARS} chars"
    if len(cleaned.split()) > EXTRACTOR_MAX_WORDS:
        return f"word count {len(cleaned.split())} > {EXTRACTOR_MAX_WORDS}"
    if _looks_like_sentence(cleaned):
        return "looks like a sentence (punctuation pattern)"
    if _has_unbalanced_parens(cleaned):
        return "unbalanced brackets"
    return None


DEFAULT_RESPONSE_FORMAT: ResponseFormat = ResponseFormat(type="json_object")
"""Default ``response_format`` for the helper-LLM extractor (PRD F-LLM-3).

We pin ``json_object`` because the prompt asks for "a single JSON object
and nothing else" and the parser refuses prose. Without an explicit
structured-output hint, reasoning-style helpers (e.g. ``gpt-oss-20b``)
can spend the entire visible-channel budget on reasoning tokens and
return empty content; permissive endpoints can wrap the JSON in fences
or commentary that defeats the recovery regex. JSON mode constrains
decoding so the model has to emit a parseable object, eliminating both
failure modes for the bulk of OpenAI-compatible providers (OpenAI,
LiteLLM, vLLM, Ollama, llama.cpp). Endpoints that genuinely don't
support it raise a clean 400 the operator can surface and override
via ``ExtractOptions(response_format=ResponseFormat(type="text"))``.
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
"""Mirror of :data:`epublate.glossary.models.EntityType` (PRD F-LB-1).

Re-declared here so the prompt module stays free of cross-package imports
beyond the translator's :class:`GlossaryConstraint`."""

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


class ExtractedEntity(BaseModel):
    """One candidate entity returned by the helper LLM.

    ``confidence`` is best-effort — the model is asked to populate it
    but unreliable endpoints may return zeros. ``evidence`` is a short
    quote (or paraphrase) the curator can use to verify the candidate.
    """

    model_config = ConfigDict(extra="forbid")

    type: EntityTypeLiteral = "term"
    source: str
    target: str | None = None
    evidence: str | None = None
    confidence: float = 0.0


class ExtractorTrace(BaseModel):
    """Parsed helper-model response (PRD §8.2 / F-STYLE-3).

    ``narrative_register`` and ``narrative_audience`` are best-effort
    *style observations* the helper LLM is asked to surface alongside
    the glossary so the intake summary can co-propose a tone preset
    (:func:`epublate.core.style.suggest_style_profile`). They are
    free-form strings; the suggester normalizes them. The JSON keys on
    the wire stay ``register`` / ``audience`` (the helper LLM is
    prompted with those short names) — the attributes are namespaced
    in Python because pydantic's :class:`BaseModel` reserves
    ``register`` via the ABC metaclass.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    entities: list[ExtractedEntity] = Field(default_factory=list)
    pov: str | None = None
    tense: str | None = None
    narrative_register: str | None = Field(default=None, alias="register")
    narrative_audience: str | None = Field(default=None, alias="audience")
    notes: str | None = None


_SYSTEM_PROMPT_TEMPLATE = """\
You are a literary entity extractor working alongside a translator on a
long-form story book. Your job is to read a chunk of source text in
{source_lang} and return a structured list of recurring proper-noun
entities the translator will need to keep consistent in {target_lang}.

What to surface:

* characters (named people / beings),
* places (cities, regions, buildings, named geography),
* organizations, factions, guilds, families,
* events (battles, festivals, ceremonies),
* items (named weapons, artifacts, vehicles, books),
* date_or_time markers (named eras, calendars, recurring holidays),
* recurring phrases, in-world terms, slang, epithets, idiomatic
  insults, or compound coinages that recur and must spell the same
  way every time. Hyphenated compounds (e.g. ``boot-lickers``,
  ``half-elf``, ``self-aware``) and multi-word phrases
  (e.g. ``Council of Five``) count — keep the hyphen / spaces in
  the ``source`` exactly as written,
* anything else worth keeping in the lore bible — use ``other``.

Hard rules:

1. Only list entities that actually appear in the text I give you.
2. Skip entries that are already in the existing glossary below — they
   are settled. Do not propose synonyms or aliases of locked terms.
3. The ``source`` field must be the exact surface form as it appears
   in the source text — keep capitalization, hyphens, punctuation,
   and spacing. **It must be a noun phrase, named entity, or short
   fixed expression — at most 10 words and 100 characters. Never
   propose a full sentence, a clause with a verb chain, a
   description, or a quoted line of dialogue.** "Council of Five"
   is fine; "First you will eat your chickens, then your goats" is
   not — the second is a sentence and must not be proposed even
   if it recurs.
4. **When a name is commonly written ``Full Name (ACRONYM)``** (e.g.
   ``Heavily Indebted Poor Country (HIPC)``,
   ``Fédération Internationale de Football Association (FIFA)``):
   use the ACRONYM as the canonical ``source`` (and ``target``) and
   put the long form in ``aliases`` if the chunk shows it that way.
   Don't propose two separate entries for the long form and the
   acronym — they are the same entity.
5. The ``target`` field is your best-effort translation of the source
   term in {target_lang} — apply the language's spelling and
   capitalization conventions (e.g. ``Julius Caesar`` → ``Júlio
   César`` in Brazilian Portuguese). Leave it as an empty string
   only when no idiomatic translation exists (proper nouns that
   stay identical across languages).
6. ``confidence`` is a number between 0.0 and 1.0; use 1.0 only when
   the text makes the entity unambiguous.
7. Best-effort narrative metadata: detect the dominant point-of-view
   (``first``, ``second``, ``third_limited``, ``third_omniscient``, ...)
   and tense (``past``, ``present``, ...) from the chunk. Leave them
   ``null`` if the chunk is too short or mixed.
8. Best-effort style observations for the curator (used to co-propose
   a tone preset): ``register`` is a short tag for the tone of the
   prose — pick from ``literary``, ``genre`` (thriller / fantasy /
   SF / mystery), ``romance``, ``explicit`` (sexually explicit /
   erotic), ``technical`` (manuals, how-to), ``academic``,
   ``journalistic``, or ``neutral`` — and ``audience`` is the
   intended reader: ``children`` (picture book / early reader),
   ``middle_grade`` (8-12), ``young_adult`` (teen), ``adult``, or
   ``general``. Leave both ``null`` when the chunk is too short or
   ambiguous to call.

{glossary_block}\
Respond with a single JSON object and nothing else:

{{
  "entities": [
    {{"type": "character|place|organization|event|item|date_or_time|phrase|term|other",
     "source": "<surface form as in the text>",
     "target": "<best-effort translation in {target_lang}, or empty string>",
     "evidence": "<short quote or paraphrase>",
     "confidence": 0.0}}
  ],
  "pov": "first|third_limited|...|null",
  "tense": "past|present|...|null",
  "register": "literary|genre|romance|explicit|technical|academic|journalistic|neutral",
  "audience": "children|middle_grade|young_adult|adult|general",
  "notes": "optional free-text observations for the curator, or omit"
}}

Do not wrap the JSON in code fences. Do not add commentary.\
"""


def build_extractor_messages(
    *,
    source_lang: str,
    target_lang: str,
    source_text: str,
    glossary: Sequence[GlossaryConstraint] = (),
) -> list[Message]:
    """Construct the chat messages for one extractor call (PRD §8.2).

    ``glossary`` is the same shape the translator's prompt uses — pass
    only ``locked`` and ``confirmed`` entries so the model does not
    re-propose what the curator already settled. ``proposed`` entries
    are intentionally kept *out* of the prompt: re-surfacing them is
    fine (the auto-proposer dedupes downstream).
    """

    if not source_text or not source_text.strip():
        raise ValueError("source_text must not be empty")

    glossary_block = _format_glossary_block(glossary)
    system_content = _SYSTEM_PROMPT_TEMPLATE.format(
        source_lang=source_lang,
        target_lang=target_lang,
        glossary_block=glossary_block,
    )

    return [
        Message(role="system", content=system_content),
        Message(role="user", content=source_text),
    ]


def _format_glossary_block(glossary: Sequence[GlossaryConstraint]) -> str:
    if not glossary:
        return "Existing glossary: (empty — propose freely).\n\n"
    lines = ["Existing glossary (do not re-propose these):"]
    for entry in glossary:
        if entry.status == "proposed":
            continue
        lines.append(
            f"  - [{entry.type}] {entry.source_term} → {entry.target_term} "
            f"({entry.status})"
        )
    if len(lines) == 1:
        return "Existing glossary: (empty — propose freely).\n\n"
    lines.append("")
    return "\n".join(lines) + "\n"


_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)


def parse_extractor_response(content: str) -> ExtractorTrace:
    """Parse the helper LLM's JSON response (PRD §8.2 / F-LLM-3 fallback).

    Tries strict ``json.loads`` first; on failure attempts to recover
    the first ``{...}`` block in the payload — this catches the common
    failure mode where a permissive endpoint wraps JSON in prose despite
    the system prompt's instructions. Anything that still doesn't parse
    raises :class:`~epublate.errors.LLMResponseError`.
    """

    if not content or not content.strip():
        raise LLMResponseError("extractor response was empty")

    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        match = _JSON_OBJECT_RE.search(content)
        if match is None:
            raise LLMResponseError(
                "extractor response is not JSON and contains no JSON object"
            ) from None
        try:
            data = json.loads(match.group(0))
        except json.JSONDecodeError as exc:
            raise LLMResponseError(
                f"failed to recover JSON from extractor response: {exc}"
            ) from exc

    if not isinstance(data, dict):
        raise LLMResponseError(
            "extractor response top-level must be a JSON object; "
            f"got {type(data).__name__}"
        )

    entities_raw = data.get("entities", [])
    if not isinstance(entities_raw, list):
        raise LLMResponseError("extractor response 'entities' must be a list")

    entities: list[ExtractedEntity] = []
    for raw in entities_raw:
        normalized = _normalize_entity(raw)
        if normalized is None:
            continue
        entities.append(normalized)

    pov = _coerce_optional_str(data.get("pov"), field_name="pov")
    tense = _coerce_optional_str(data.get("tense"), field_name="tense")
    register_value = _coerce_optional_str(data.get("register"), field_name="register")
    audience_value = _coerce_optional_str(data.get("audience"), field_name="audience")
    notes = _coerce_optional_str(data.get("notes"), field_name="notes")

    # Build via ``model_validate`` so we can use the on-the-wire JSON
    # keys (``register`` / ``audience``) — these are pydantic aliases
    # for ``narrative_register`` / ``narrative_audience`` (the field
    # names sidestep ``BaseModel.register`` / ABCMeta shadowing). The
    # static-typed kwargs path on ``ExtractorTrace(...)`` doesn't
    # accept aliases, so the dict form keeps both pydantic and mypy
    # happy.
    return ExtractorTrace.model_validate(
        {
            "entities": entities,
            "pov": pov,
            "tense": tense,
            "register": register_value,
            "audience": audience_value,
            "notes": notes,
        }
    )


def _coerce_optional_str(value: Any, *, field_name: str) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        cleaned = value.strip()
        return cleaned or None
    raise LLMResponseError(
        f"extractor response {field_name!r} must be a string or null"
    )


def _normalize_entity(raw: Any) -> ExtractedEntity | None:
    """Coerce one ``entities`` item to :class:`ExtractedEntity` or drop it.

    Items missing a usable ``source`` are dropped silently — the
    extractor is best-effort, not a hard contract; we'd rather skip a
    malformed candidate than fail the whole intake on one stray dict.
    Other malformed shapes raise :class:`LLMResponseError` so a model
    that returns garbage for ``entities`` is loud about it.

    Entries that violate the extractor caps (full sentences, runaway
    phrases, broken parens — see :func:`_violates_extractor_caps`)
    are also dropped silently. The helper LLM occasionally proposes a
    quoted line of dialogue or a multi-clause description as a
    "phrase" when there's no real recurring entity in the chunk;
    rejecting them here keeps the lore bible focused on actual
    proper nouns. Logged at DEBUG so noisy endpoints can be spotted.
    """

    if not isinstance(raw, dict):
        raise LLMResponseError("each entry in 'entities' must be a JSON object")
    source = raw.get("source") or raw.get("source_term")
    if not isinstance(source, str):
        return None
    source = source.strip()
    if not source:
        return None
    cap_reason = _violates_extractor_caps(source)
    if cap_reason is not None:
        _logger.debug(
            "extractor: dropped candidate source=%r (%s)", source[:80], cap_reason
        )
        return None

    type_str = str(raw.get("type", "term")).strip().lower() or "term"
    if type_str not in _VALID_TYPES:
        type_str = "term"

    target_raw = raw.get("target") or raw.get("target_term")
    target: str | None
    if target_raw is None:
        target = None
    elif isinstance(target_raw, str):
        target = target_raw.strip() or None
    else:
        raise LLMResponseError("entity 'target' must be a string or null")
    if target is not None:
        target_cap_reason = _violates_extractor_caps(target)
        if target_cap_reason is not None:
            _logger.debug(
                "extractor: dropped candidate target=%r (%s); keeping source",
                target[:80],
                target_cap_reason,
            )
            target = None

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
        # bool is a subclass of int; a stray ``true`` should not silently
        # collapse into a 1.0 confidence — surface it.
        raise LLMResponseError("entity 'confidence' must be a number")
    if isinstance(confidence_raw, int | float):
        confidence = float(confidence_raw)
    else:
        try:
            confidence = float(confidence_raw)
        except (TypeError, ValueError) as exc:
            raise LLMResponseError("entity 'confidence' must be a number") from exc
    confidence = max(0.0, min(1.0, confidence))

    return ExtractedEntity(
        type=type_str,  # type: ignore[arg-type]
        source=source,
        target=target,
        evidence=evidence,
        confidence=confidence,
    )


__all__ = [
    "DEFAULT_RESPONSE_FORMAT",
    "EntityTypeLiteral",
    "ExtractedEntity",
    "ExtractorTrace",
    "build_extractor_messages",
    "parse_extractor_response",
]
