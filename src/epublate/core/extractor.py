"""Helper-LLM extractor service (PRD §4.2 phase 3, §7.1, §8.2 / M5).

Two public flows live here:

* :func:`run_book_intake` — the one-shot pass at project creation
  (PRD §7.1 step 5). Reads the first few segments of a project,
  chunks them under a token budget, and asks the helper LLM for a
  draft glossary of likely entities plus narrative POV/tense.
* :func:`run_pre_pass` — the per-chapter pre-pass for batch mode
  (PRD §4.2 phase 3). Operates on an explicit list of segments so the
  caller (typically :func:`epublate.core.batch.run_batch`) can drive
  it once per chapter before the translator futures fire.

Both flows fan out to :func:`extract_entities`, which is the single
LLM-touching primitive: build the helper messages, look up the cache
(:mod:`epublate.core.cache`), call the provider on a miss, parse the
response, upsert ``proposed`` glossary entries, and persist one
``llm_call`` row + a structured trace event. ``purpose="extract"``
keeps the helper rows distinct from the translator's audit trail.

Hard rules respected here:

* **Cache hits never call the network** (PRD F-LLM-6) — the cache key
  folds the glossary state hash, so curator promotions invalidate
  stale extractor traces just like they do for translations.
* **Per-call audit** (LLM-integration rule §5) — every call (cache
  hit or miss) inserts an ``llm_call`` row with full prompt /
  response JSON and the cache key.
* **Atomic upserts** (db-and-persistence rule) — proposed entries,
  the ``llm_call`` row, and the ``intake.*`` event commit together;
  a crash mid-flow leaves a clean DB.
* **Best-effort, never blocks translation** — a malformed response
  is recorded as ``intake.failed`` and surfaced to the curator's
  Inbox, but doesn't raise out of :func:`run_book_intake` /
  :func:`run_pre_pass`.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any, cast

from sqlalchemy.engine import Engine

from epublate.core.cache import cache_key_for_messages
from epublate.core.style import suggest_style_profile
from epublate.db import repo
from epublate.errors import EpublateError, LLMResponseError
from epublate.glossary import io as glossary_io
from epublate.glossary.enforcer import build_constraints, glossary_hash
from epublate.glossary.models import EntityType, GlossaryEntryWithAliases
from epublate.llm.base import LLMProvider, ResponseFormat
from epublate.llm.pricing import estimate_cost
from epublate.llm.prompts.extractor import (
    ExtractedEntity,
    ExtractorTrace,
    build_extractor_messages,
    parse_extractor_response,
)
from epublate.llm.tokens import count_tokens

_logger = logging.getLogger(__name__)

PURPOSE_EXTRACT = "extract"

DEFAULT_INTAKE_MAX_SEGMENTS = 30
DEFAULT_CHUNK_MAX_TOKENS = 1500
"""Soft per-call source-token budget for one extractor message.

The helper model typically has a generous context window, but we cap the
chunk size so a single huge chapter doesn't blow the budget on one call
and to keep cache keys stable as projects grow."""

_VALID_ENTITY_TYPES: frozenset[str] = frozenset(
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


@dataclass(slots=True, frozen=True)
class ExtractOptions:
    """Per-call knobs for one :func:`extract_entities` invocation.

    Defaults pin ``temperature=0.0`` / ``seed=7`` so reproducible runs
    against deterministic-supporting endpoints stay stable (NFR-5).
    """

    model: str
    temperature: float | None = 0.0
    seed: int | None = 7
    bypass_cache: bool = False
    response_format: ResponseFormat | None = None
    auto_propose: bool = True


@dataclass(slots=True)
class ExtractOutcome:
    """Result of one :func:`extract_entities` call."""

    trace: ExtractorTrace
    cache_hit: bool
    prompt_tokens: int
    completion_tokens: int
    cost_usd: float
    llm_call_id: str
    cache_key: str
    proposed_entry_ids: tuple[str, ...] = ()


@dataclass(slots=True, frozen=True)
class IntakeOptions:
    """Knobs for :func:`run_book_intake` / :func:`run_pre_pass`.

    ``max_segments`` is the cap on how many segments we feed the helper
    in total (per ``run_book_intake`` call, or per chapter for
    :func:`run_pre_pass`). ``chunk_max_tokens`` is the soft per-call
    cap; consecutive segments are bundled under it to maximize context
    while staying cacheable.
    """

    model: str
    max_segments: int = DEFAULT_INTAKE_MAX_SEGMENTS
    chunk_max_tokens: int = DEFAULT_CHUNK_MAX_TOKENS
    bypass_cache: bool = False
    auto_propose: bool = True


@dataclass(slots=True)
class IntakeSummary:
    """Aggregate result of one :func:`run_book_intake` call.

    ``register`` / ``audience`` are first-non-empty observations from
    the helper across every chunk (PRD §8.2 / F-STYLE-3); they are
    carried through so the curator can see what the helper inferred
    about the book's tone. ``suggested_style_profile`` is the preset
    id :func:`epublate.core.style.suggest_style_profile` derives from
    that pair — surfaced on the dashboard so a tone choice is one
    keystroke away after intake.
    """

    chunks: int = 0
    cached_chunks: int = 0
    proposed_count: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0
    failed_chunks: int = 0
    pov: str | None = None
    tense: str | None = None
    register: str | None = None
    audience: str | None = None
    suggested_style_profile: str | None = None
    notes: list[str] = field(default_factory=list)
    proposed_entry_ids: list[str] = field(default_factory=list)


def extract_entities(
    *,
    engine: Engine,
    project_id: str,
    source_lang: str,
    target_lang: str,
    source_text: str,
    provider: LLMProvider,
    options: ExtractOptions,
    first_seen_segment_id: str | None = None,
    glossary: Sequence[GlossaryEntryWithAliases] | None = None,
) -> ExtractOutcome:
    """Run one helper-LLM extractor call on ``source_text`` and persist.

    Caller responsibilities:

    * The project must already exist in the DB.
    * ``provider`` is the helper LLM client (often the same instance
      as the translator's; PRD F-LLM-2 explicitly allows reuse).
    * If ``glossary`` is omitted, the function loads the current
      glossary from the DB so the helper sees up-to-date constraints.
    * On success, every detected entity is upserted as a ``proposed``
      glossary entry (when ``options.auto_propose`` is on).

    Returns an :class:`ExtractOutcome` capturing the parsed trace plus
    the cost / cache audit so the caller can roll the values into a
    higher-level summary.
    """

    if not source_text or not source_text.strip():
        raise ValueError("source_text must not be empty")

    project_entries = list(
        glossary
        if glossary is not None
        else repo.list_glossary_entries(engine, project_id)
    )
    constraints = build_constraints(project_entries)
    g_hash = glossary_hash(project_entries)

    messages = build_extractor_messages(
        source_lang=source_lang,
        target_lang=target_lang,
        source_text=source_text,
        glossary=constraints,
    )
    key = cache_key_for_messages(
        model=options.model, messages=messages, glossary_hash=g_hash
    )
    if options.bypass_cache:
        key = f"{key}:retry"

    request_payload = {
        "model": options.model,
        "purpose": PURPOSE_EXTRACT,
        "messages": [m.model_dump() for m in messages],
        "temperature": options.temperature,
        "seed": options.seed,
        "glossary_hash": g_hash,
    }
    request_json = json.dumps(request_payload, ensure_ascii=False, sort_keys=True)

    if not options.bypass_cache:
        hit = repo.find_llm_call_by_cache_key(
            engine, project_id=project_id, cache_key=key
        )
        if hit is not None and hit.response_json:
            outcome = _replay_extract_from_cache(
                engine,
                project_id=project_id,
                key=key,
                hit=hit,
                request_json=request_json,
                options=options,
                first_seen_segment_id=first_seen_segment_id,
            )
            return outcome

    chat_result = provider.chat(
        messages,
        model=options.model,
        response_format=options.response_format,
        temperature=options.temperature,
        seed=options.seed,
    )

    try:
        trace = parse_extractor_response(chat_result.content)
    except LLMResponseError:
        _record_failed_extract(
            engine,
            project_id=project_id,
            model=chat_result.model,
            request_json=request_json,
            response_json=json.dumps(
                chat_result.raw, ensure_ascii=False, sort_keys=True
            ),
            key=key,
            prompt_tokens=chat_result.prompt_tokens,
            completion_tokens=chat_result.completion_tokens,
        )
        raise

    cost = estimate_cost(
        chat_result.model,
        chat_result.prompt_tokens,
        chat_result.completion_tokens,
    )
    response_payload = {
        "content": chat_result.content,
        "trace": trace.model_dump(),
        "raw": chat_result.raw,
    }
    response_json = json.dumps(response_payload, ensure_ascii=False, sort_keys=True)

    llm_call_id = uuid.uuid4().hex
    proposed_ids: list[str] = []
    with engine.begin() as conn:
        repo.insert_llm_call(
            conn,
            repo.LLMCallRow(
                id=llm_call_id,
                project_id=project_id,
                segment_id=first_seen_segment_id,
                purpose=PURPOSE_EXTRACT,
                model=chat_result.model,
                prompt_tokens=chat_result.prompt_tokens,
                completion_tokens=chat_result.completion_tokens,
                cost_usd=cost,
                cache_hit=False,
                cache_key=key,
                request_json=request_json,
                response_json=response_json,
            ),
        )
        if options.auto_propose:
            proposed_ids.extend(
                _auto_propose(
                    conn,
                    project_id=project_id,
                    trace=trace,
                    first_seen_segment_id=first_seen_segment_id,
                )
            )
        repo.append_event(
            conn,
            project_id=project_id,
            kind="entity.extracted",
            payload={
                "llm_call_id": llm_call_id,
                "model": chat_result.model,
                "cache_hit": False,
                "entities": len(trace.entities),
                "proposed": len(proposed_ids),
                "pov": trace.pov,
                "tense": trace.tense,
                "register": trace.narrative_register,
                "audience": trace.narrative_audience,
            },
        )

    _logger.info(
        "extract: %d entities, %d proposed (%d→%d tokens, $%.6f) via %s",
        len(trace.entities),
        len(proposed_ids),
        chat_result.prompt_tokens,
        chat_result.completion_tokens,
        cost,
        chat_result.model,
    )
    return ExtractOutcome(
        trace=trace,
        cache_hit=False,
        prompt_tokens=chat_result.prompt_tokens,
        completion_tokens=chat_result.completion_tokens,
        cost_usd=cost,
        llm_call_id=llm_call_id,
        cache_key=key,
        proposed_entry_ids=tuple(proposed_ids),
    )


def _replay_extract_from_cache(
    engine: Engine,
    *,
    project_id: str,
    key: str,
    hit: repo.LLMCallRow,
    request_json: str,
    options: ExtractOptions,
    first_seen_segment_id: str | None,
) -> ExtractOutcome:
    """Hydrate a helper-call outcome from a cached ``llm_call`` row.

    Always inserts a fresh ``llm_call`` row with ``cache_hit=1`` /
    ``cost_usd=0`` so the audit trail distinguishes hits from the
    original miss (LLM-integration rule §5). The cache key already
    folds the glossary hash so the trace is consistent with the
    project's current state.
    """

    payload = json.loads(hit.response_json or "{}")
    trace_data = payload.get("trace") if isinstance(payload, dict) else None
    if not isinstance(trace_data, dict):
        content = payload.get("content") if isinstance(payload, dict) else None
        if not isinstance(content, str):
            raise EpublateError(
                f"cached llm_call {hit.id} has no usable extractor payload"
            )
        trace = parse_extractor_response(content)
    else:
        trace = ExtractorTrace.model_validate(trace_data)

    response_json = hit.response_json or json.dumps(
        {"content": "", "trace": trace.model_dump()},
        ensure_ascii=False,
        sort_keys=True,
    )
    new_id = uuid.uuid4().hex
    proposed_ids: list[str] = []
    with engine.begin() as conn:
        repo.insert_llm_call(
            conn,
            repo.LLMCallRow(
                id=new_id,
                project_id=project_id,
                segment_id=first_seen_segment_id,
                purpose=PURPOSE_EXTRACT,
                model=hit.model,
                prompt_tokens=hit.prompt_tokens,
                completion_tokens=hit.completion_tokens,
                cost_usd=0.0,
                cache_hit=True,
                cache_key=key,
                request_json=request_json,
                response_json=response_json,
            ),
        )
        if options.auto_propose:
            proposed_ids.extend(
                _auto_propose(
                    conn,
                    project_id=project_id,
                    trace=trace,
                    first_seen_segment_id=first_seen_segment_id,
                )
            )
        repo.append_event(
            conn,
            project_id=project_id,
            kind="entity.extracted",
            payload={
                "llm_call_id": new_id,
                "model": hit.model,
                "cache_hit": True,
                "entities": len(trace.entities),
                "proposed": len(proposed_ids),
                "pov": trace.pov,
                "tense": trace.tense,
                "register": trace.narrative_register,
                "audience": trace.narrative_audience,
            },
        )

    return ExtractOutcome(
        trace=trace,
        cache_hit=True,
        prompt_tokens=hit.prompt_tokens or 0,
        completion_tokens=hit.completion_tokens or 0,
        cost_usd=0.0,
        llm_call_id=new_id,
        cache_key=key,
        proposed_entry_ids=tuple(proposed_ids),
    )


def _record_failed_extract(
    engine: Engine,
    *,
    project_id: str,
    model: str,
    request_json: str,
    response_json: str,
    key: str,
    prompt_tokens: int,
    completion_tokens: int,
) -> None:
    """Persist a failed parse for audit (LLM-integration rule §5)."""

    cost = estimate_cost(model, prompt_tokens, completion_tokens)
    with engine.begin() as conn:
        repo.insert_llm_call(
            conn,
            repo.LLMCallRow(
                id=uuid.uuid4().hex,
                project_id=project_id,
                segment_id=None,
                purpose=PURPOSE_EXTRACT,
                model=model,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                cost_usd=cost,
                cache_hit=False,
                cache_key=key,
                request_json=request_json,
                response_json=response_json,
            ),
        )
        repo.append_event(
            conn,
            project_id=project_id,
            kind="entity.extract_failed",
            payload={"model": model},
        )


def _auto_propose(
    conn: Any,
    *,
    project_id: str,
    trace: ExtractorTrace,
    first_seen_segment_id: str | None,
) -> list[str]:
    """Upsert ``trace.entities`` candidates as ``proposed`` glossary rows.

    Mirrors :func:`epublate.core.pipeline._auto_propose_entities` so the
    extractor and translator paths share the same dedup semantics:
    ``(source_term, type)`` keys, an ``entity.proposed`` event per new
    row, and a no-op for entries the curator already promoted/edited.
    """

    created_ids: list[str] = []
    for ent in trace.entities:
        candidate = _normalize_entity(ent)
        if candidate is None:
            continue
        source_term, type_, notes, target_term = candidate
        entry_id, created = glossary_io.upsert_proposed(
            conn,
            project_id=project_id,
            source_term=source_term,
            type=type_,
            first_seen_segment_id=first_seen_segment_id,
            notes=notes,
            target_term=target_term,
        )
        if not created:
            continue
        created_ids.append(entry_id)
        repo.append_event(
            conn,
            project_id=project_id,
            kind="entity.proposed",
            payload={
                "entry_id": entry_id,
                "segment_id": first_seen_segment_id,
                "source_term": source_term,
                "type": type_,
                "source": "extractor",
            },
        )
    return created_ids


def _normalize_entity(
    ent: ExtractedEntity,
) -> tuple[str, EntityType, str | None, str | None] | None:
    """Coerce one :class:`ExtractedEntity` into ``(source, type, notes, target)``.

    The pydantic model already validated the shape (the prompt parser
    rejects garbage); we only need to drop empties and downcast the
    type literal to :data:`EntityType` for the glossary-IO call. ``target``
    is best-effort — the helper LLM is asked for an idiomatic
    translation but unreliable endpoints may still omit it; in that
    case we hand ``None`` to the glossary IO layer and the entry stays
    in placeholder form until the translator observes a real
    translation in a segment.
    """

    source_term = ent.source.strip()
    if not source_term:
        return None
    type_str = ent.type.strip().lower() if ent.type else "term"
    if type_str not in _VALID_ENTITY_TYPES:
        type_str = "term"
    notes = ent.evidence.strip() if ent.evidence else None
    target = ent.target.strip() if ent.target else None
    return source_term, cast(EntityType, type_str), notes, target or None


# ---------------------------------------------------------------------------
# Higher-level flows
# ---------------------------------------------------------------------------


def run_book_intake(
    *,
    engine: Engine,
    project_id: str,
    source_lang: str,
    target_lang: str,
    provider: LLMProvider,
    options: IntakeOptions,
) -> IntakeSummary:
    """Run the first-pass intake on a freshly created project (PRD §7.1).

    Reads at most ``options.max_segments`` segments from the start of
    the book (sorted by spine_idx then segment idx, skipping anything
    that no longer needs translating), groups them into chunks under
    ``options.chunk_max_tokens``, and asks the helper LLM for a draft
    glossary plus a POV/tense observation.

    Per-chunk failures are recorded as ``entity.extract_failed`` events
    and counted on the summary so the curator can see what didn't land,
    but they don't abort the intake — best-effort by design.
    """

    segments = _initial_segments(
        engine, project_id=project_id, max_segments=options.max_segments
    )
    summary = IntakeSummary()
    repo.append_event(
        engine,
        project_id=project_id,
        kind="intake.started",
        payload={
            "model": options.model,
            "max_segments": options.max_segments,
            "chunk_max_tokens": options.chunk_max_tokens,
            "segment_count": len(segments),
        },
    )
    if not segments:
        repo.append_event(
            engine,
            project_id=project_id,
            kind="intake.completed",
            payload=_intake_payload(summary),
        )
        return summary

    chunks = _chunk_segments(
        segments,
        chunk_max_tokens=options.chunk_max_tokens,
        model=options.model,
    )
    project_entries = list(repo.list_glossary_entries(engine, project_id))

    extract_options = ExtractOptions(
        model=options.model,
        bypass_cache=options.bypass_cache,
        auto_propose=options.auto_propose,
    )

    for chunk in chunks:
        try:
            outcome = extract_entities(
                engine=engine,
                project_id=project_id,
                source_lang=source_lang,
                target_lang=target_lang,
                source_text=chunk.text,
                provider=provider,
                options=extract_options,
                first_seen_segment_id=chunk.first_segment_id,
                glossary=project_entries,
            )
        except Exception as exc:  # helper boundary — never abort the intake
            _logger.warning("intake chunk failed: %s", exc)
            summary.failed_chunks += 1
            continue

        _accumulate_chunk(summary, outcome)

        # The glossary changed under our feet; refresh so the next
        # chunk's prompt sees the freshly proposed entries (and the
        # cache key folds them in correctly).
        if outcome.proposed_entry_ids:
            project_entries = list(repo.list_glossary_entries(engine, project_id))

    summary.suggested_style_profile = suggest_style_profile(
        register=summary.register, audience=summary.audience
    )
    repo.append_event(
        engine,
        project_id=project_id,
        kind="intake.completed",
        payload=_intake_payload(summary),
    )
    return summary


def run_pre_pass(
    *,
    engine: Engine,
    project_id: str,
    source_lang: str,
    target_lang: str,
    provider: LLMProvider,
    options: IntakeOptions,
    segments: Sequence[repo.SegmentRow],
) -> IntakeSummary:
    """Run the helper extractor over an explicit segment list.

    Thin wrapper around :func:`run_book_intake` semantics, without the
    "first ~N segments" selection — the caller decides which segments
    are in scope (typically a chapter's pending ones during batch).
    Emits ``batch.pre_pass_completed`` so the Inbox surfaces the run.
    """

    summary = IntakeSummary()
    if not segments:
        repo.append_event(
            engine,
            project_id=project_id,
            kind="batch.pre_pass_completed",
            payload=_intake_payload(summary),
        )
        return summary

    chunks = _chunk_segments(
        list(segments),
        chunk_max_tokens=options.chunk_max_tokens,
        model=options.model,
    )
    project_entries = list(repo.list_glossary_entries(engine, project_id))

    extract_options = ExtractOptions(
        model=options.model,
        bypass_cache=options.bypass_cache,
        auto_propose=options.auto_propose,
    )

    repo.append_event(
        engine,
        project_id=project_id,
        kind="batch.pre_pass_started",
        payload={
            "model": options.model,
            "segment_count": len(segments),
            "chunk_count": len(chunks),
        },
    )

    for chunk in chunks:
        try:
            outcome = extract_entities(
                engine=engine,
                project_id=project_id,
                source_lang=source_lang,
                target_lang=target_lang,
                source_text=chunk.text,
                provider=provider,
                options=extract_options,
                first_seen_segment_id=chunk.first_segment_id,
                glossary=project_entries,
            )
        except Exception as exc:
            _logger.warning("pre-pass chunk failed: %s", exc)
            summary.failed_chunks += 1
            continue

        _accumulate_chunk(summary, outcome)
        if outcome.proposed_entry_ids:
            project_entries = list(repo.list_glossary_entries(engine, project_id))

    summary.suggested_style_profile = suggest_style_profile(
        register=summary.register, audience=summary.audience
    )
    repo.append_event(
        engine,
        project_id=project_id,
        kind="batch.pre_pass_completed",
        payload=_intake_payload(summary),
    )
    return summary


def _accumulate_chunk(summary: IntakeSummary, outcome: ExtractOutcome) -> None:
    """Roll one extractor outcome into the running ``summary``.

    Shared by :func:`run_book_intake` and :func:`run_pre_pass` so the
    aggregation contract (first-non-empty wins for narrative metadata,
    cumulative for token / cost counters) stays in lockstep across the
    intake and pre-pass entry points.
    """

    summary.chunks += 1
    summary.prompt_tokens += outcome.prompt_tokens
    summary.completion_tokens += outcome.completion_tokens
    summary.cost_usd += outcome.cost_usd
    summary.proposed_count += len(outcome.proposed_entry_ids)
    summary.proposed_entry_ids.extend(outcome.proposed_entry_ids)
    if outcome.cache_hit:
        summary.cached_chunks += 1
    if outcome.trace.pov and summary.pov is None:
        summary.pov = outcome.trace.pov
    if outcome.trace.tense and summary.tense is None:
        summary.tense = outcome.trace.tense
    if outcome.trace.narrative_register and summary.register is None:
        summary.register = outcome.trace.narrative_register
    if outcome.trace.narrative_audience and summary.audience is None:
        summary.audience = outcome.trace.narrative_audience
    if outcome.trace.notes:
        summary.notes.append(outcome.trace.notes)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class _Chunk:
    """One ``(text, first_segment_id)`` pair fed to the helper LLM."""

    text: str
    first_segment_id: str | None


def _chunk_segments(
    segments: Iterable[repo.SegmentRow],
    *,
    chunk_max_tokens: int,
    model: str | None,
) -> list[_Chunk]:
    """Group consecutive segments into chunks under ``chunk_max_tokens``.

    Single-segment chunks are kept even if they exceed the budget — the
    helper LLM and the validator handle truncation gracefully, and
    splitting mid-segment isn't worth the complexity for an opt-in
    intake flow. The chunk text uses a blank-line separator so the
    helper sees clear paragraph boundaries.
    """

    chunks: list[_Chunk] = []
    current: list[str] = []
    current_tokens = 0
    current_first: str | None = None
    for seg in segments:
        text = seg.source_text.strip()
        if not text:
            continue
        seg_tokens = max(1, count_tokens(text, model=model))
        if current and current_tokens + seg_tokens > chunk_max_tokens:
            chunks.append(
                _Chunk(text="\n\n".join(current), first_segment_id=current_first)
            )
            current = []
            current_tokens = 0
            current_first = None
        if not current:
            current_first = seg.id
        current.append(text)
        current_tokens += seg_tokens

    if current:
        chunks.append(_Chunk(text="\n\n".join(current), first_segment_id=current_first))
    return chunks


def _initial_segments(
    engine: Engine,
    *,
    project_id: str,
    max_segments: int,
) -> list[repo.SegmentRow]:
    """Pick the first ``max_segments`` segments in book order.

    Pulls every segment for the project (not just ``pending``) because
    the curator might re-run intake after a partial translation; we
    don't want to skip a chapter that was already translated. The
    matcher / cache layer dedupes proposed entries downstream.
    """

    if max_segments <= 0:
        return []
    rows = repo.list_segments_for_project(engine, project_id)
    return rows[:max_segments]


def _intake_payload(summary: IntakeSummary) -> dict[str, object]:
    return {
        "chunks": summary.chunks,
        "cached_chunks": summary.cached_chunks,
        "proposed_count": summary.proposed_count,
        "prompt_tokens": summary.prompt_tokens,
        "completion_tokens": summary.completion_tokens,
        "cost_usd": summary.cost_usd,
        "failed_chunks": summary.failed_chunks,
        "pov": summary.pov,
        "tense": summary.tense,
        "register": summary.register,
        "audience": summary.audience,
        "suggested_style_profile": summary.suggested_style_profile,
    }


__all__ = [
    "DEFAULT_CHUNK_MAX_TOKENS",
    "DEFAULT_INTAKE_MAX_SEGMENTS",
    "PURPOSE_EXTRACT",
    "ExtractOptions",
    "ExtractOutcome",
    "IntakeOptions",
    "IntakeSummary",
    "extract_entities",
    "run_book_intake",
    "run_pre_pass",
]
