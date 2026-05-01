"""Translator prompt builder + response parser (PRD §8.1, §8.4 / F-LLM-3).

Pure, side-effect-free functions: the pipeline owns the LLM call and the
DB write; this module only turns project state into ``Message`` objects
and turns the model's response back into a typed ``TranslatorTrace``.

Inline tags never reach the model — :func:`build_translator_messages`
assumes the caller has already replaced them with ``[[T0]]…[[/T0]]``
placeholders via :mod:`epublate.core.segmentation` (format-handling rule).

The system prompt deliberately encodes:

* the placeholder-discipline contract (every issued placeholder must come
  back exactly once, no extras, no drops),
* the JSON response shape the parser expects,
* hard / soft glossary constraints (M3 plugs in real entries; M2 sends an
  empty list).
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from epublate.errors import LLMResponseError
from epublate.llm.base import Message

GlossaryStatus = Literal["proposed", "confirmed", "locked"]


class GlossaryConstraint(BaseModel):
    """One glossary constraint as it lands in the translator's system prompt.

    Kept independent of :mod:`epublate.glossary.models` (which arrives in
    M3) so M2 can produce empty lists without circular imports.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    source_term: str
    target_term: str
    type: str = "term"
    status: GlossaryStatus = "confirmed"
    notes: str | None = None


class TranslatorTrace(BaseModel):
    """Parsed translator response (PRD §8.1).

    ``new_entities`` is intentionally typed loosely (a list of dicts) — the
    extractor pipeline (M5) defines the canonical shape; this parser just
    forwards whatever the model returned for downstream review.
    """

    model_config = ConfigDict(extra="forbid")

    target: str
    used_entries: list[str] = Field(default_factory=list)
    new_entities: list[dict[str, Any]] = Field(default_factory=list)
    notes: str | None = None


class GroupTranslatorItem(BaseModel):
    """One item in a grouped translator response.

    The grouped call batches many short, placeholder-free segments (e.g.
    a table of contents, a glossary, a long list) into a single request
    so we amortize one LLM round-trip over dozens of segments. Each
    item still needs its own ``target`` and optional glossary trace so
    the pipeline can validate and persist them independently.
    """

    model_config = ConfigDict(extra="forbid")

    id: int
    target: str
    used_entries: list[str] = Field(default_factory=list)
    new_entities: list[dict[str, Any]] = Field(default_factory=list)
    notes: str | None = None


class GroupTranslatorTrace(BaseModel):
    """Parsed grouped translator response."""

    model_config = ConfigDict(extra="forbid")

    translations: list[GroupTranslatorItem]
    notes: str | None = None


_SYSTEM_PROMPT_TEMPLATE = """\
You are a literary translator working on a long ePub story book.

Translate the user's source segment from {source_lang} to {target_lang}.

Hard rules — these are not negotiable:

1. Inline formatting is encoded as opaque placeholders of the form
   `[[T0]]`, `[[/T0]]`, `[[T1]]`, etc. Every placeholder that appears in
   the source MUST appear exactly once in your translation, in the same
   relative order. Do not invent new placeholders. Do not drop any.
   Closing placeholders (`[[/T0]]`) must always pair with their opener
   (`[[T0]]`).
2. Translate naturally for the target audience but preserve narrative
   voice, tense, and POV. Do not paraphrase past the meaning of the
   source. Do not summarize.
3. Keep proper nouns, place names, and domain terms consistent across
   the book. The glossary below lists agreed translations.
4. Locked glossary entries are non-negotiable. Confirmed entries are
   strong defaults. Proposed entries are suggestions.

{style_guide_block}{glossary_block}\
Respond with a single JSON object and nothing else:

{{
  "target": "<translated text with placeholders preserved>",
  "used_entries": ["<source_term you used a glossary entry for>", ...],
  "new_entities": [
    {{"type": "character|place|...", "source": "...", "evidence": "..."}},
    ...
  ],
  "notes": "optional free-text notes for the curator, or omit"
}}

Do not wrap the JSON in code fences. Do not add commentary.\
"""


_GROUP_SYSTEM_PROMPT_TEMPLATE = """\
You are a literary translator working on a long ePub story book.

The user is sending a BATCH of short, independent segments (typically
items from a table of contents, glossary, list, index, or other
repetitive structure) so you can translate them in a single round-trip.
Translate each segment from {source_lang} to {target_lang}.

Hard rules — these are not negotiable:

1. Preserve the number and order of items. Your response's
   ``translations`` array must contain exactly one entry per input
   item, with the same ``id`` values — do not merge, drop, or invent
   items.
2. Keep each translation scoped to its own item. Do NOT bleed context
   from one item into the next.
3. Translate naturally for the target audience; do not paraphrase past
   the meaning of the source and do not summarize.
4. Keep proper nouns, place names, and domain terms consistent with
   the glossary below. Locked glossary entries are non-negotiable,
   confirmed entries are strong defaults, proposed entries are
   suggestions.
5. No inline placeholders (`[[T0]]`, etc.) should appear in the input
   for this batch; if you see any, treat them as literal characters
   that must survive verbatim in the output.

{style_guide_block}{glossary_block}\
Input format: the user message is a JSON object of the shape
``{{"items": [{{"id": 1, "source": "..."}}, ...]}}``. Respond with a
single JSON object and nothing else:

{{
  "translations": [
    {{
      "id": <the item id you were given>,
      "target": "<translated text>",
      "used_entries": ["<source_term you applied from the glossary>", ...],
      "new_entities": [
        {{"type": "character|place|...", "source": "...", "evidence": "..."}},
        ...
      ],
      "notes": "optional, omit when empty"
    }},
    ...
  ],
  "notes": "optional batch-level note, omit when empty"
}}

Do not wrap the JSON in code fences. Do not add commentary.\
"""


def build_translator_messages(
    *,
    source_lang: str,
    target_lang: str,
    source_text: str,
    style_guide: str | None = None,
    glossary: Sequence[GlossaryConstraint] = (),
) -> list[Message]:
    """Construct the chat messages for one translator call (PRD §8.1)."""

    if not source_text:
        raise ValueError("source_text must not be empty")

    style_guide_block = (
        f"Style guide:\n{style_guide.strip()}\n\n" if style_guide else ""
    )
    glossary_block = _format_glossary_block(glossary)

    system_content = _SYSTEM_PROMPT_TEMPLATE.format(
        source_lang=source_lang,
        target_lang=target_lang,
        style_guide_block=style_guide_block,
        glossary_block=glossary_block,
    )

    return [
        Message(role="system", content=system_content),
        Message(role="user", content=source_text),
    ]


def _format_glossary_block(glossary: Sequence[GlossaryConstraint]) -> str:
    if not glossary:
        return "Glossary: (empty for this segment).\n\n"
    by_status: dict[GlossaryStatus, list[GlossaryConstraint]] = {
        "locked": [],
        "confirmed": [],
        "proposed": [],
    }
    for entry in glossary:
        by_status.setdefault(entry.status, []).append(entry)

    lines = ["Glossary:"]
    for status in ("locked", "confirmed", "proposed"):
        bucket = by_status.get(status, [])
        if not bucket:
            continue
        lines.append(f"  {status} entries (must use the canonical target term):")
        for entry in bucket:
            note = f" — {entry.notes}" if entry.notes else ""
            lines.append(
                f"    - [{entry.type}] {entry.source_term} → {entry.target_term}{note}"
            )
    lines.append("")
    return "\n".join(lines) + "\n"


_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)


def parse_translator_response(content: str) -> TranslatorTrace:
    """Parse the translator's JSON response (PRD §8.1, F-LLM-3 fallback).

    Tries strict ``json.loads`` first. If that fails, attempts to recover
    the first ``{...}`` block in the payload — this catches the common
    failure mode where a permissive endpoint wraps JSON in prose despite
    the system prompt's instructions. Anything that still doesn't parse
    raises :class:`~epublate.errors.LLMResponseError`.
    """

    if not content or not content.strip():
        raise LLMResponseError("translator response was empty")

    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        match = _JSON_OBJECT_RE.search(content)
        if match is None:
            raise LLMResponseError(
                "translator response is not JSON and contains no JSON object"
            ) from None
        try:
            data = json.loads(match.group(0))
        except json.JSONDecodeError as exc:
            raise LLMResponseError(
                f"failed to recover JSON from translator response: {exc}"
            ) from exc

    if not isinstance(data, dict):
        raise LLMResponseError(
            "translator response top-level must be a JSON object; "
            f"got {type(data).__name__}"
        )
    if "target" not in data:
        raise LLMResponseError("translator response missing required 'target' field")
    target = data.get("target")
    if not isinstance(target, str):
        raise LLMResponseError("translator response 'target' must be a string")

    used_entries_raw = data.get("used_entries", [])
    new_entities_raw = data.get("new_entities", [])
    notes = data.get("notes")

    if not isinstance(used_entries_raw, list):
        raise LLMResponseError("translator response 'used_entries' must be a list")
    used_entries = [str(item) for item in used_entries_raw]

    if not isinstance(new_entities_raw, list):
        raise LLMResponseError("translator response 'new_entities' must be a list")
    new_entities: list[dict[str, Any]] = []
    for item in new_entities_raw:
        if not isinstance(item, dict):
            raise LLMResponseError("each entry in 'new_entities' must be a JSON object")
        new_entities.append(dict(item))

    if notes is not None and not isinstance(notes, str):
        raise LLMResponseError("translator response 'notes' must be a string if set")

    return TranslatorTrace(
        target=target,
        used_entries=used_entries,
        new_entities=new_entities,
        notes=notes,
    )


def build_group_translator_messages(
    *,
    source_lang: str,
    target_lang: str,
    source_items: Sequence[tuple[int, str]],
    style_guide: str | None = None,
    glossary: Sequence[GlossaryConstraint] = (),
) -> list[Message]:
    """Construct chat messages for a *grouped* translator call.

    ``source_items`` is a sequence of ``(id, source_text)`` tuples. The
    ids are echoed back by the model so the pipeline can map translations
    to the originating :class:`~epublate.db.repo.SegmentRow`-s without
    relying on positional alignment (the model occasionally re-orders).
    The caller is expected to filter segments down to the
    grouping-eligible subset (short, placeholder-free) before calling
    this — see ``epublate.core.pipeline.translate_segments_grouped``.
    """

    if not source_items:
        raise ValueError("source_items must not be empty")
    seen: set[int] = set()
    for item_id, text in source_items:
        if item_id in seen:
            raise ValueError(f"duplicate item id {item_id} in source_items")
        if not text:
            raise ValueError(f"source_items[{item_id}] has empty source_text")
        seen.add(item_id)

    style_guide_block = (
        f"Style guide:\n{style_guide.strip()}\n\n" if style_guide else ""
    )
    glossary_block = _format_glossary_block(glossary)

    system_content = _GROUP_SYSTEM_PROMPT_TEMPLATE.format(
        source_lang=source_lang,
        target_lang=target_lang,
        style_guide_block=style_guide_block,
        glossary_block=glossary_block,
    )

    user_payload = {
        "items": [{"id": item_id, "source": text} for item_id, text in source_items]
    }
    user_content = json.dumps(user_payload, ensure_ascii=False)

    return [
        Message(role="system", content=system_content),
        Message(role="user", content=user_content),
    ]


def parse_group_translator_response(
    content: str,
    *,
    expected_ids: Sequence[int] | None = None,
) -> GroupTranslatorTrace:
    """Parse a grouped translator response.

    Mirror of :func:`parse_translator_response`. Raises
    :class:`~epublate.errors.LLMResponseError` if the payload isn't
    valid JSON, doesn't have the expected shape, or — when
    ``expected_ids`` is provided — omits any of the expected ids.
    Extra ids are tolerated but silently dropped by downstream callers.
    """

    if not content or not content.strip():
        raise LLMResponseError("group translator response was empty")

    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        match = _JSON_OBJECT_RE.search(content)
        if match is None:
            raise LLMResponseError(
                "group translator response is not JSON and contains no JSON object"
            ) from None
        try:
            data = json.loads(match.group(0))
        except json.JSONDecodeError as exc:
            raise LLMResponseError(
                f"failed to recover JSON from group translator response: {exc}"
            ) from exc

    if not isinstance(data, dict):
        raise LLMResponseError(
            "group translator response top-level must be a JSON object"
        )
    translations_raw = data.get("translations")
    if not isinstance(translations_raw, list):
        raise LLMResponseError("group translator response missing 'translations' list")

    items: list[GroupTranslatorItem] = []
    seen_ids: set[int] = set()
    for raw in translations_raw:
        if not isinstance(raw, dict):
            raise LLMResponseError("each 'translations' entry must be a JSON object")
        try:
            item = GroupTranslatorItem.model_validate(raw)
        except Exception as exc:  # pydantic surface
            raise LLMResponseError(f"invalid 'translations' entry: {exc}") from exc
        if item.id in seen_ids:
            raise LLMResponseError(
                f"duplicate item id {item.id} in group translator response"
            )
        seen_ids.add(item.id)
        items.append(item)

    if expected_ids is not None:
        missing = [i for i in expected_ids if i not in seen_ids]
        if missing:
            raise LLMResponseError(
                f"group translator response missing ids: {missing!r}"
            )

    notes = data.get("notes")
    if notes is not None and not isinstance(notes, str):
        raise LLMResponseError("group translator response 'notes' must be a string")

    return GroupTranslatorTrace(translations=items, notes=notes)


__all__ = [
    "GlossaryConstraint",
    "GroupTranslatorItem",
    "GroupTranslatorTrace",
    "TranslatorTrace",
    "build_group_translator_messages",
    "build_translator_messages",
    "parse_group_translator_response",
    "parse_translator_response",
]
