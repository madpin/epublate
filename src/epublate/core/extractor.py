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
import threading
import time
import uuid
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any, cast

from sqlalchemy.engine import Engine

from epublate.core.cache import cache_key_for_messages
from epublate.core.style import suggest_style_profile
from epublate.db import repo
from epublate.db.schema import IntakeRunKind, IntakeRunStatus
from epublate.errors import EpublateError, LLMRateLimitError, LLMResponseError
from epublate.glossary import io as glossary_io
from epublate.glossary.enforcer import build_constraints, glossary_hash
from epublate.glossary.models import EntityType, GlossaryEntryWithAliases
from epublate.llm.base import LLMProvider, ResponseFormat
from epublate.llm.json_mode import chat_with_json_fallback
from epublate.llm.pricing import estimate_cost
from epublate.llm.prompts.extractor import (
    DEFAULT_RESPONSE_FORMAT as DEFAULT_EXTRACTOR_RESPONSE_FORMAT,
)
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
DEFAULT_FAILURE_STREAK_LIMIT = 3
"""Default number of consecutive helper-LLM failures that aborts a loop.

When the helper endpoint is fundamentally broken for the curator's
configuration (e.g. a reasoning helper on Groq that returns empty
visible content because reasoning tokens consume the entire
visible-channel budget), the per-chunk error handler in
``run_book_intake`` / ``run_pre_pass`` would otherwise burn through
every chunk, flooding the Inbox and the curator's terminal with the
same error. Three consecutive failures is generous enough to absorb
a transient glitch and tight enough to give up quickly on a systemic
problem.
"""
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
    against deterministic-supporting endpoints stay stable (NFR-5), and
    ``response_format`` to ``json_object`` so the helper actually
    produces parseable JSON instead of empty content (the failure mode
    we hit with reasoning helpers like ``gpt-oss-20b`` that consume the
    visible-channel budget on reasoning tokens). Pass
    ``ResponseFormat(type="text")`` here on the rare endpoint that
    rejects ``response_format``; the prompt's "respond with JSON only"
    instruction still applies, just unconstrained.
    """

    model: str
    temperature: float | None = 0.0
    seed: int | None = 7
    bypass_cache: bool = False
    response_format: ResponseFormat | None = field(
        default_factory=lambda: DEFAULT_EXTRACTOR_RESPONSE_FORMAT
    )
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
    failure_streak_limit: int = DEFAULT_FAILURE_STREAK_LIMIT
    """Abort the chunk loop after this many *consecutive* failed chunks.

    Defaults to :data:`DEFAULT_FAILURE_STREAK_LIMIT` (3). Set to ``0``
    to disable the circuit breaker and keep the legacy "best-effort,
    never abort" behavior — useful in tests where every chunk fails
    by design and we still want the per-chunk audit rows.
    """


@dataclass(slots=True, frozen=True)
class PrePassChunkEvent:
    """One per-chunk progress tick fired by :func:`run_pre_pass`.

    The orchestrator (:func:`epublate.core.batch.run_batch`) forwards
    these to the UI so the dashboard meter can show "pre-pass: chapter
    1/3 chunk 2/2" while the helper LLM is grinding — without this
    callback the meter sits at ``0 / N`` for the whole pre-pass and
    looks frozen on slow helper endpoints (PRD §4.6 / batch UI).

    ``error`` is set when the chunk failed; in that case ``success`` is
    ``False`` and the chunk's contribution to the running summary is
    a bumped ``failed_chunks`` counter (no entities, no cost).
    """

    chunk_index: int
    chunk_count: int
    success: bool
    error: str | None
    proposed_count: int
    cache_hit: bool


PrePassChunkCallback = Callable[[PrePassChunkEvent], None]


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
                source_lang=source_lang,
                target_lang=target_lang,
            )
            return outcome

    chat_result = chat_with_json_fallback(
        provider,
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
                    source_lang=source_lang,
                    target_lang=target_lang,
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
    source_lang: str | None = None,
    target_lang: str | None = None,
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
                    source_lang=source_lang,
                    target_lang=target_lang,
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
    source_lang: str | None = None,
    target_lang: str | None = None,
) -> list[str]:
    """Upsert ``trace.entities`` candidates as ``proposed`` glossary rows.

    Mirrors :func:`epublate.core.pipeline._auto_propose_entities` so the
    extractor and translator paths share the same dedup semantics:
    ``(source_term, type)`` keys, an ``entity.proposed`` event per new
    row, and a no-op for entries the curator already promoted/edited.

    ``source_lang`` / ``target_lang`` are passed through to
    :func:`upsert_proposed` so leading articles / prepositions are
    stripped down to lemma form on insert (PRD F-LB-3) — the helper
    LLM still loves to propose ``Europe → na Europa``-style asymmetric
    pairs.
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
            source_lang=source_lang,
            target_lang=target_lang,
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
    started_at = int(time.time())
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
        _persist_intake_run(
            engine,
            project_id=project_id,
            summary=summary,
            kind=IntakeRunKind.BOOK_INTAKE,
            helper_model=options.model,
            started_at=started_at,
            status=IntakeRunStatus.COMPLETED,
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

    streak = 0
    last_error: str | None = None
    aborted = False
    for idx, chunk in enumerate(chunks):
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
            streak += 1
            last_error = _short_error(exc)
            if _should_trip_breaker(streak, options.failure_streak_limit):
                remaining = len(chunks) - (idx + 1)
                summary.failed_chunks += remaining
                aborted = True
                _logger.warning(
                    "intake aborted after %d consecutive failed chunks "
                    "(skipping %d remaining). Last error: %s. If you're "
                    "using a reasoning-style helper (gpt-oss-*, "
                    "deepseek-r1, ...), it may be returning empty visible "
                    "content because reasoning tokens consume the entire "
                    "visible-channel budget. Try a non-reasoning helper "
                    "via $EPUBLATE_LLM_HELPER_MODEL or the project override.",
                    streak,
                    remaining,
                    last_error,
                )
                break
            continue

        streak = 0
        _accumulate_chunk(summary, outcome)

        # The glossary changed under our feet; refresh so the next
        # chunk's prompt sees the freshly proposed entries (and the
        # cache key folds them in correctly).
        if outcome.proposed_entry_ids:
            project_entries = list(repo.list_glossary_entries(engine, project_id))

    summary.suggested_style_profile = suggest_style_profile(
        register=summary.register, audience=summary.audience
    )
    if aborted:
        repo.append_event(
            engine,
            project_id=project_id,
            kind="intake.aborted",
            payload={
                **_intake_payload(summary),
                "failure_streak": streak,
                "last_error": last_error,
            },
        )
        _persist_intake_run(
            engine,
            project_id=project_id,
            summary=summary,
            kind=IntakeRunKind.BOOK_INTAKE,
            helper_model=options.model,
            started_at=started_at,
            status=IntakeRunStatus.ABORTED,
            error=last_error,
        )
    else:
        repo.append_event(
            engine,
            project_id=project_id,
            kind="intake.completed",
            payload=_intake_payload(summary),
        )
        _persist_intake_run(
            engine,
            project_id=project_id,
            summary=summary,
            kind=IntakeRunKind.BOOK_INTAKE,
            helper_model=options.model,
            started_at=started_at,
            status=IntakeRunStatus.COMPLETED,
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
    cancel_event: threading.Event | None = None,
    on_chunk: PrePassChunkCallback | None = None,
) -> IntakeSummary:
    """Run the helper extractor over an explicit segment list.

    Thin wrapper around :func:`run_book_intake` semantics, without the
    "first ~N segments" selection — the caller decides which segments
    are in scope (typically a chapter's pending ones during batch).
    Emits ``batch.pre_pass_completed`` so the Inbox surfaces the run.

    ``cancel_event`` is checked between chunks; when set we stop early,
    emit ``batch.pre_pass_cancelled`` (so the audit log can distinguish
    a curator Cancel from a circuit-breaker abort), and return the
    partial summary. ``on_chunk`` fires once per chunk (success or
    failure) so the orchestrator can surface "pre-pass: chunk X / Y"
    progress to the dashboard meter — without it the meter sits at
    ``0 / N`` for the whole pre-pass and looks frozen on slow helper
    endpoints.
    """

    summary = IntakeSummary()
    started_at = int(time.time())
    chapter_id = _segments_chapter_id(segments)
    if not segments:
        repo.append_event(
            engine,
            project_id=project_id,
            kind="batch.pre_pass_completed",
            payload=_intake_payload(summary),
        )
        _persist_intake_run(
            engine,
            project_id=project_id,
            summary=summary,
            kind=IntakeRunKind.CHAPTER_PRE_PASS,
            helper_model=options.model,
            started_at=started_at,
            status=IntakeRunStatus.COMPLETED,
            chapter_id=chapter_id,
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

    streak = 0
    last_error: str | None = None
    aborted = False
    cancelled = False
    for idx, chunk in enumerate(chunks):
        if cancel_event is not None and cancel_event.is_set():
            cancelled = True
            break
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
        except LLMRateLimitError as exc:
            # Endpoint is depleted (free-tier daily quota, monthly cap,
            # …). Don't burn the rest of the chapter's chunks on the
            # same 429 — surface the error so the orchestrator (the
            # batch runner) can pause cleanly. Emit a marker event so
            # the audit log distinguishes "we stopped because the
            # endpoint refused us" from "we stopped because the curator
            # cancelled" or "we tripped the empty-content breaker".
            _logger.warning("pre-pass rate-limited; aborting helper loop: %s", exc)
            if on_chunk is not None:
                _emit_chunk_event(
                    on_chunk,
                    chunk_index=idx,
                    chunk_count=len(chunks),
                    success=False,
                    error=_short_error(exc),
                    proposed_count=0,
                    cache_hit=False,
                )
            summary.suggested_style_profile = suggest_style_profile(
                register=summary.register, audience=summary.audience
            )
            payload = {
                **_intake_payload(summary),
                "provider_message": exc.provider_message or str(exc),
            }
            if exc.retry_after_seconds is not None:
                payload["retry_after_seconds"] = exc.retry_after_seconds
            repo.append_event(
                engine,
                project_id=project_id,
                kind="batch.pre_pass_rate_limited",
                payload=payload,
            )
            _persist_intake_run(
                engine,
                project_id=project_id,
                summary=summary,
                kind=IntakeRunKind.CHAPTER_PRE_PASS,
                helper_model=options.model,
                started_at=started_at,
                status=IntakeRunStatus.RATE_LIMITED,
                chapter_id=chapter_id,
                error=exc.provider_message or str(exc),
            )
            raise
        except Exception as exc:
            _logger.warning("pre-pass chunk failed: %s", exc)
            summary.failed_chunks += 1
            streak += 1
            last_error = _short_error(exc)
            if on_chunk is not None:
                _emit_chunk_event(
                    on_chunk,
                    chunk_index=idx,
                    chunk_count=len(chunks),
                    success=False,
                    error=last_error,
                    proposed_count=0,
                    cache_hit=False,
                )
            if _should_trip_breaker(streak, options.failure_streak_limit):
                remaining = len(chunks) - (idx + 1)
                summary.failed_chunks += remaining
                aborted = True
                _logger.warning(
                    "pre-pass aborted after %d consecutive failed chunks "
                    "(skipping %d remaining). Last error: %s. If you're "
                    "using a reasoning-style helper (gpt-oss-*, "
                    "deepseek-r1, ...), it may be returning empty visible "
                    "content because reasoning tokens consume the entire "
                    "visible-channel budget. Try a non-reasoning helper "
                    "via $EPUBLATE_LLM_HELPER_MODEL or the project override.",
                    streak,
                    remaining,
                    last_error,
                )
                break
            continue

        streak = 0
        _accumulate_chunk(summary, outcome)
        if outcome.proposed_entry_ids:
            project_entries = list(repo.list_glossary_entries(engine, project_id))
        if on_chunk is not None:
            _emit_chunk_event(
                on_chunk,
                chunk_index=idx,
                chunk_count=len(chunks),
                success=True,
                error=None,
                proposed_count=len(outcome.proposed_entry_ids),
                cache_hit=outcome.cache_hit,
            )

    summary.suggested_style_profile = suggest_style_profile(
        register=summary.register, audience=summary.audience
    )
    if cancelled:
        repo.append_event(
            engine,
            project_id=project_id,
            kind="batch.pre_pass_cancelled",
            payload=_intake_payload(summary),
        )
        _persist_intake_run(
            engine,
            project_id=project_id,
            summary=summary,
            kind=IntakeRunKind.CHAPTER_PRE_PASS,
            helper_model=options.model,
            started_at=started_at,
            status=IntakeRunStatus.CANCELLED,
            chapter_id=chapter_id,
        )
    elif aborted:
        repo.append_event(
            engine,
            project_id=project_id,
            kind="batch.pre_pass_aborted",
            payload={
                **_intake_payload(summary),
                "failure_streak": streak,
                "last_error": last_error,
            },
        )
        _persist_intake_run(
            engine,
            project_id=project_id,
            summary=summary,
            kind=IntakeRunKind.CHAPTER_PRE_PASS,
            helper_model=options.model,
            started_at=started_at,
            status=IntakeRunStatus.ABORTED,
            chapter_id=chapter_id,
            error=last_error,
        )
    else:
        repo.append_event(
            engine,
            project_id=project_id,
            kind="batch.pre_pass_completed",
            payload=_intake_payload(summary),
        )
        _persist_intake_run(
            engine,
            project_id=project_id,
            summary=summary,
            kind=IntakeRunKind.CHAPTER_PRE_PASS,
            helper_model=options.model,
            started_at=started_at,
            status=IntakeRunStatus.COMPLETED,
            chapter_id=chapter_id,
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


_ERROR_MESSAGE_TRUNCATION = 240


def _should_trip_breaker(streak: int, limit: int) -> bool:
    """Whether the failure streak crossed the configured circuit-breaker limit.

    ``limit <= 0`` disables the breaker entirely (legacy "best-effort,
    never abort" behavior, useful in tests where every chunk fails by
    design and we still want every audit row recorded).
    """

    return limit > 0 and streak >= limit


def _short_error(exc: BaseException) -> str:
    """Render an exception for an event payload / log line, truncated.

    Some upstream providers (LiteLLM, OpenAI gateways) attach long
    fallback / diagnostic blobs to the exception message. Truncating
    keeps the Inbox readable; the full exception is still in the
    structured-logging stream if the curator needs it.
    """

    text = str(exc)
    if len(text) <= _ERROR_MESSAGE_TRUNCATION:
        return text
    return text[: _ERROR_MESSAGE_TRUNCATION - 1] + "\u2026"


def _emit_chunk_event(
    callback: PrePassChunkCallback,
    *,
    chunk_index: int,
    chunk_count: int,
    success: bool,
    error: str | None,
    proposed_count: int,
    cache_hit: bool,
) -> None:
    """Build a :class:`PrePassChunkEvent` and hand it to ``callback``.

    Wraps the construction in a try/except so a misbehaving listener
    can't take down the helper loop — the pre-pass is best-effort by
    design and the chunk has already been persisted by the time we
    fire this event.
    """

    try:
        callback(
            PrePassChunkEvent(
                chunk_index=chunk_index,
                chunk_count=chunk_count,
                success=success,
                error=error,
                proposed_count=proposed_count,
                cache_hit=cache_hit,
            )
        )
    except Exception:  # pragma: no cover — defensive
        _logger.debug("pre-pass progress callback raised; ignoring")


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


def _persist_intake_run(
    engine: Engine,
    *,
    project_id: str,
    summary: IntakeSummary,
    kind: str,
    helper_model: str,
    started_at: int,
    status: str,
    chapter_id: str | None = None,
    error: str | None = None,
) -> str | None:
    """Land one ``intake_run`` row + its proposed-entry links.

    Best-effort by design: a write failure here must not turn a
    successful intake / pre-pass into a curator-visible failure
    (the audit ``event`` row is the source of truth for "the helper
    finished cleanly"). On failure we log + return ``None`` so the
    caller can keep going.

    Returns the freshly-minted ``intake_run.id`` on success so tests
    and downstream callers can correlate it to the audit event.
    """

    finished_at = int(time.time())
    try:
        row = repo.record_intake_run(
            engine,
            project_id=project_id,
            kind=kind,
            chapter_id=chapter_id,
            helper_model=helper_model,
            started_at=int(started_at),
            finished_at=finished_at,
            status=status,
            chunks=summary.chunks,
            cached_chunks=summary.cached_chunks,
            proposed_count=summary.proposed_count,
            failed_chunks=summary.failed_chunks,
            prompt_tokens=summary.prompt_tokens,
            completion_tokens=summary.completion_tokens,
            cost_usd=summary.cost_usd,
            pov=summary.pov,
            tense=summary.tense,
            narrative_register=summary.register,
            audience=summary.audience,
            suggested_style_profile=summary.suggested_style_profile,
            notes=summary.notes,
            error=error,
        )
        if summary.proposed_entry_ids:
            repo.attach_intake_run_entries(
                engine,
                intake_run_id=row.id,
                entry_ids=summary.proposed_entry_ids,
            )
        return row.id
    except Exception as exc:  # never crash the helper loop on bookkeeping
        _logger.warning(
            "could not persist intake_run (kind=%s, project=%s): %s",
            kind,
            project_id,
            exc,
        )
        return None


def _segments_chapter_id(segments: Sequence[repo.SegmentRow]) -> str | None:
    """Resolve the chapter for a pre-pass segment list.

    All members of one ``run_pre_pass`` invocation share a chapter
    by construction (the batch worker fans out per chapter), so
    picking the first segment's parent is correct. Returns ``None``
    when the input is empty so the empty-segments terminal branch
    can still call us without a guard.
    """

    if not segments:
        return None
    return segments[0].chapter_id


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
