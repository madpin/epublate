"""Translation pipeline (PRD §4.2).

Phases live here so persistence rules are enforced in a single place
(db-and-persistence rule §2: state changes are transactional). The
Reader screen and (later) the batch worker pool are both supposed to
go through :func:`translate_segment`.

M3 wires the lore bible into every translate call:

* Phase 2 (resolve entities) — :func:`epublate.glossary.enforcer.build_constraints`
  filters the project's locked + confirmed entries into the system
  prompt, and the source-side matcher records every mention in
  ``entity_mention``.
* Phase 5 (validate) — the structural placeholder validator still runs,
  and :func:`epublate.glossary.enforcer.validate_target` checks that
  every locked entry hit in the source is honored in the target.
* Cache key — the glossary state is folded into the cache key
  (PRD F-LLM-6) so a glossary edit invalidates stale translations.
* Phase 3 (auto-propose) — every ``new_entities`` candidate the
  translator returns is upserted as a ``proposed`` glossary entry so the
  curator can promote/reject it from the Glossary screen later.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, cast

from epublate.core.cache import cache_key_for_messages
from epublate.core.segmentation import PLACEHOLDER_RE, is_trivially_empty
from epublate.core.validators import validate_segment_placeholders
from epublate.db import repo, schema
from epublate.errors import EpublateError
from epublate.formats.base import Segment
from epublate.glossary import io as glossary_io
from epublate.glossary.enforcer import (
    Violation,
    build_constraints,
    build_target_only_constraints,
    find_mentions,
    glossary_hash,
    has_locked_violation,
    validate_target,
)
from epublate.glossary.matcher import match_source
from epublate.glossary.models import EntityType, GlossaryEntryWithAliases
from epublate.llm.base import LLMProvider, ResponseFormat
from epublate.llm.pricing import estimate_cost
from epublate.llm.prompts.translator import (
    GlossaryConstraint,
    GroupTranslatorItem,
    TargetOnlyConstraint,
    TranslatorTrace,
    build_group_translator_messages,
    build_translator_messages,
    parse_group_translator_response,
    parse_translator_response,
)
from epublate.llm.tokens import count_tokens

_logger = logging.getLogger(__name__)

PURPOSE_TRANSLATE = "translate"

# Default grouping parameters — see ``translate_segments_grouped``.
GROUP_DEFAULT_MAX_ITEMS = 50
GROUP_DEFAULT_MAX_SOURCE_CHARS = 240
# Cap on placeholders per item in a grouped call. The grouped prompt
# scopes placeholder ids per-item so each TOC link / index entry can
# carry its own ``[[T0]]…[[/T0]]`` pair without colliding with siblings.
# The cap exists because (a) reliability of the JSON response degrades
# faster than linearly with placeholder density and (b) longer items
# already get rejected by the char-budget check, which is the more useful
# signal for "this isn't a TOC entry, send it solo". 8 placeholders is
# enough for an anchor wrapping a chapter number plus a title, which is
# the worst case in real-world TOCs we've sampled.
GROUP_DEFAULT_MAX_PLACEHOLDERS = 8

# When the translator's trace returns a candidate ``type`` we don't
# recognize, we collapse to ``term`` so the auto-proposer can still
# record the source string for the curator.
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
class TranslateOptions:
    """Per-call knobs that don't live on the project row.

    Defaults pin ``temperature=0.0`` and ``seed=7`` so tests with a real
    OpenAI-compatible endpoint stay reproducible (NFR-5). ``bypass_cache``
    is used by the Reader's "retry" action.

    ``glossary`` is an explicit override used by tests and CLI tools
    that want to bypass the DB-loaded set; when ``None``, the pipeline
    loads the project's glossary from the DB (the normal path).
    ``auto_propose`` controls whether ``trace.new_entities`` candidates
    are upserted as ``proposed`` entries (M3 default: on).
    """

    model: str
    temperature: float | None = 0.0
    seed: int | None = 7
    glossary: tuple[GlossaryConstraint, ...] | None = None
    bypass_cache: bool = False
    response_format: ResponseFormat | None = None
    auto_propose: bool = True


@dataclass(slots=True)
class TranslateOutcome:
    """Result of one ``translate_segment`` call.

    ``violations`` lists every glossary violation surfaced by the
    enforcer; if any of them is locked-severity, ``flagged`` is true and
    the segment's persisted status is ``flagged`` instead of
    ``translated`` (PRD §4.3 / glossary-invariants rule §1).
    ``proposed_entry_ids`` are auto-created glossary rows from
    ``trace.new_entities``; the Glossary screen surfaces them in the
    "proposed" filter.
    """

    segment_id: str
    target_text: str
    trace: TranslatorTrace
    cache_hit: bool
    prompt_tokens: int
    completion_tokens: int
    cost_usd: float
    llm_call_id: str
    cache_key: str
    violations: tuple[Violation, ...] = ()
    flagged: bool = False
    mention_entry_ids: tuple[str, ...] = ()
    proposed_entry_ids: tuple[str, ...] = ()
    extra: dict[str, Any] = field(default_factory=dict)


class PipelineError(EpublateError):
    """Pipeline failed past the configured retry budget."""


def translate_segment(
    *,
    engine: Any,
    project_id: str,
    source_lang: str,
    target_lang: str,
    style_guide: str | None,
    segment: repo.SegmentRow,
    provider: LLMProvider,
    options: TranslateOptions,
) -> TranslateOutcome:
    """Translate one segment and persist the result in a single transaction.

    Caller responsibilities:

    * The segment must already exist in the DB (i.e. ``Project.create``
      has imported the source ePub). The pipeline does not create rows.
    * On success the segment's ``status`` flips to
      :data:`SegmentStatus.TRANSLATED` (or ``FLAGGED`` if a locked
      glossary violation was detected). The Reader / Inbox screen is
      responsible for promoting it to ``approved``.

    Pipeline guarantees:

    * Cache hits never call the provider; the cache key folds in the
      glossary state hash so a glossary edit invalidates stale entries.
    * Misses validate inline-tag round-trip before writing the target;
      a malformed translation never lands in the DB (format-handling
      rule §2).
    * Locked glossary violations flip the segment to ``flagged`` and
      record the violation list on a ``segment.translation_flagged``
      event so the curator can find it in the Inbox (M4).
    * The segment update, the ``llm_call`` audit row, mentions, the
      auto-proposed entries, and every event commit together — one
      transaction, crash safe.
    * Trivially empty segments (whitespace / NBSP / zero-width content
      after stripping placeholders) short-circuit *before* any LLM /
      glossary work: the pipeline copies ``source_text`` to
      ``target_text`` and records a ``segment.translated_trivial``
      event. No ``llm_call`` row is written because nothing was
      called, and the segment counts as translated for budget /
      progress purposes.
    """

    if is_trivially_empty(segment.source_text):
        return _short_circuit_trivial(engine, project_id=project_id, segment=segment)

    glossary_view = _load_glossary_view(engine, project_id=project_id)
    project_entries = glossary_view.entries
    if options.glossary is not None:
        constraints = list(options.glossary)
        target_only_constraints: list[TargetOnlyConstraint] = []
    else:
        relevant_entries = _entries_relevant_to_text(
            segment.source_text, project_entries
        )
        constraints = build_constraints(relevant_entries)
        target_only_constraints = build_target_only_constraints(project_entries)
    # The cache key folds the *full* project glossary (PRD F-LLM-6 / the
    # cascade flow). The per-segment prompt only ships the relevant
    # subset — see ``_entries_relevant_to_text`` — but a curator edit
    # anywhere in the lore bible must still invalidate cached
    # translations downstream of the cascade.
    g_hash = glossary_hash(project_entries)

    messages = build_translator_messages(
        source_lang=source_lang,
        target_lang=target_lang,
        source_text=segment.source_text,
        style_guide=style_guide,
        glossary=constraints,
        target_only_glossary=target_only_constraints,
    )
    key = cache_key_for_messages(
        model=options.model, messages=messages, glossary_hash=g_hash
    )
    if options.bypass_cache:
        # Salt the key so retries on the same segment never pick up the
        # earlier (presumably bad) translation, while still being
        # deterministic for the same retry chain.
        key = f"{key}:retry"

    request_payload = {
        "model": options.model,
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
            outcome = _replay_from_cache(
                engine,
                project_id=project_id,
                segment=segment,
                key=key,
                hit=hit,
                request_json=request_json,
                options=options,
                entries=project_entries,
                glossary_view=glossary_view,
            )
            glossary_view.close()
            return outcome

    chat_result = provider.chat(
        messages,
        model=options.model,
        response_format=options.response_format,
        temperature=options.temperature,
        seed=options.seed,
    )

    try:
        trace = parse_translator_response(chat_result.content)
    except EpublateError:
        # Re-raise — the LLM-integration rule says past the retry budget
        # we surface a typed error. Persist the failed call for audit.
        _record_failed_call(
            engine,
            project_id=project_id,
            segment_id=segment.id,
            model=chat_result.model,
            request_json=request_json,
            response_json=json.dumps(
                chat_result.raw, ensure_ascii=False, sort_keys=True
            ),
            key=key,
            prompt_tokens=chat_result.prompt_tokens,
            completion_tokens=chat_result.completion_tokens,
        )
        glossary_view.close()
        raise

    spliced = _splice_target(segment, target=trace.target)
    validate_segment_placeholders(spliced)

    violations = validate_target(
        source_text=segment.source_text,
        target_text=trace.target,
        entries=project_entries,
    )
    flagged = has_locked_violation(violations)
    final_status = (
        schema.SegmentStatus.FLAGGED if flagged else schema.SegmentStatus.TRANSLATED
    )

    mentions = find_mentions(segment.source_text, project_entries)
    mention_entry_ids = tuple(dict.fromkeys(m.entry_id for m in mentions))
    raw_mentions: list[tuple[str, int | None, int | None]] = [
        (m.entry_id, m.start, m.end) for m in mentions
    ]
    mention_payload = glossary_view.filter_mentions(raw_mentions)

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
        repo.update_segment_translation(
            conn,
            segment_id=segment.id,
            target_text=trace.target,
            status=final_status,
        )
        repo.record_mentions(conn, segment_id=segment.id, mentions=mention_payload)
        repo.insert_llm_call(
            conn,
            repo.LLMCallRow(
                id=llm_call_id,
                project_id=project_id,
                segment_id=segment.id,
                purpose=PURPOSE_TRANSLATE,
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
        repo.append_event(
            conn,
            project_id=project_id,
            kind="segment.translated",
            payload={
                "segment_id": segment.id,
                "model": chat_result.model,
                "prompt_tokens": chat_result.prompt_tokens,
                "completion_tokens": chat_result.completion_tokens,
                "cost_usd": cost,
                "cache_hit": False,
                "llm_call_id": llm_call_id,
                "mention_entry_ids": list(mention_entry_ids),
                "flagged": flagged,
            },
        )
        if flagged:
            repo.append_event(
                conn,
                project_id=project_id,
                kind="segment.translation_flagged",
                payload={
                    "segment_id": segment.id,
                    "violations": [_violation_to_payload(v) for v in violations],
                },
            )
        if options.auto_propose and glossary_view.writable_lore_engine is None:
            proposed_ids.extend(
                _auto_propose_entities(
                    conn,
                    project_id=project_id,
                    segment_id=segment.id,
                    trace=trace,
                )
            )
    if options.auto_propose and glossary_view.writable_lore_engine is not None:
        # Run write-back AFTER the project transaction commits so a
        # lore-book write failure can never roll back the segment write.
        proposed_ids.extend(
            _auto_propose_entities(
                None,
                project_id=project_id,
                segment_id=segment.id,
                trace=trace,
                write_back_engine=glossary_view.writable_lore_engine,
                write_back_project_id=glossary_view.writable_lore_project_id,
            )
        )

    glossary_view.close()
    _logger.info(
        "translated segment %s (%d→%d tokens, $%.6f) via %s%s",
        segment.id,
        chat_result.prompt_tokens,
        chat_result.completion_tokens,
        cost,
        chat_result.model,
        " [flagged]" if flagged else "",
    )
    return TranslateOutcome(
        segment_id=segment.id,
        target_text=trace.target,
        trace=trace,
        cache_hit=False,
        prompt_tokens=chat_result.prompt_tokens,
        completion_tokens=chat_result.completion_tokens,
        cost_usd=cost,
        llm_call_id=llm_call_id,
        cache_key=key,
        violations=tuple(violations),
        flagged=flagged,
        mention_entry_ids=mention_entry_ids,
        proposed_entry_ids=tuple(proposed_ids),
    )


def _replay_from_cache(
    engine: Any,
    *,
    project_id: str,
    segment: repo.SegmentRow,
    key: str,
    hit: repo.LLMCallRow,
    request_json: str,
    options: TranslateOptions,
    entries: Sequence[GlossaryEntryWithAliases],
    glossary_view: _GlossaryView | None = None,
) -> TranslateOutcome:
    """Hydrate a translation from a cached ``llm_call`` row.

    A second cache hit still gets a fresh ``llm_call`` insertion so it's
    distinguishable from the original miss (LLM-integration rule §5).
    The cache key already incorporates the glossary state hash, so a
    hit means *the same glossary produced the same prompt*; we re-run
    the validator anyway as defensive belt-and-braces.

    ``glossary_view`` carries attached-Lore-Book metadata (entry origin
    set + write-back engine). When ``None`` we fall back to the
    project-only behaviour for callers (and tests) that haven't
    migrated yet.
    """

    payload = json.loads(hit.response_json or "{}")
    trace_data = payload.get("trace") if isinstance(payload, dict) else None
    if not isinstance(trace_data, dict):
        # Older / external rows might only persist ``content``; recover.
        content = payload.get("content") if isinstance(payload, dict) else None
        if not isinstance(content, str):
            raise PipelineError(
                f"cached llm_call {hit.id} has no usable response payload"
            )
        trace = parse_translator_response(content)
    else:
        trace = TranslatorTrace.model_validate(trace_data)

    spliced = _splice_target(segment, target=trace.target)
    validate_segment_placeholders(spliced)

    violations = validate_target(
        source_text=segment.source_text,
        target_text=trace.target,
        entries=entries,
    )
    flagged = has_locked_violation(violations)
    final_status = (
        schema.SegmentStatus.FLAGGED if flagged else schema.SegmentStatus.TRANSLATED
    )

    mentions = find_mentions(segment.source_text, entries)
    mention_entry_ids = tuple(dict.fromkeys(m.entry_id for m in mentions))
    raw_mentions: list[tuple[str, int | None, int | None]] = [
        (m.entry_id, m.start, m.end) for m in mentions
    ]
    mention_payload = (
        glossary_view.filter_mentions(raw_mentions)
        if glossary_view is not None
        else raw_mentions
    )

    response_json = hit.response_json or json.dumps(
        {"content": trace.target, "trace": trace.model_dump()},
        ensure_ascii=False,
        sort_keys=True,
    )
    new_id = uuid.uuid4().hex
    proposed_ids: list[str] = []
    with engine.begin() as conn:
        repo.update_segment_translation(
            conn,
            segment_id=segment.id,
            target_text=trace.target,
            status=final_status,
        )
        repo.record_mentions(conn, segment_id=segment.id, mentions=mention_payload)
        repo.insert_llm_call(
            conn,
            repo.LLMCallRow(
                id=new_id,
                project_id=project_id,
                segment_id=segment.id,
                purpose=PURPOSE_TRANSLATE,
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
        repo.append_event(
            conn,
            project_id=project_id,
            kind="segment.translated",
            payload={
                "segment_id": segment.id,
                "model": hit.model,
                "cache_hit": True,
                "llm_call_id": new_id,
                "mention_entry_ids": list(mention_entry_ids),
                "flagged": flagged,
            },
        )
        if flagged:
            repo.append_event(
                conn,
                project_id=project_id,
                kind="segment.translation_flagged",
                payload={
                    "segment_id": segment.id,
                    "violations": [_violation_to_payload(v) for v in violations],
                },
            )
        if options.auto_propose and (
            glossary_view is None or glossary_view.writable_lore_engine is None
        ):
            proposed_ids.extend(
                _auto_propose_entities(
                    conn,
                    project_id=project_id,
                    segment_id=segment.id,
                    trace=trace,
                )
            )
    if (
        options.auto_propose
        and glossary_view is not None
        and glossary_view.writable_lore_engine is not None
    ):
        proposed_ids.extend(
            _auto_propose_entities(
                None,
                project_id=project_id,
                segment_id=segment.id,
                trace=trace,
                write_back_engine=glossary_view.writable_lore_engine,
                write_back_project_id=glossary_view.writable_lore_project_id,
            )
        )

    return TranslateOutcome(
        segment_id=segment.id,
        target_text=trace.target,
        trace=trace,
        cache_hit=True,
        prompt_tokens=hit.prompt_tokens or 0,
        completion_tokens=hit.completion_tokens or 0,
        cost_usd=0.0,
        llm_call_id=new_id,
        cache_key=key,
        violations=tuple(violations),
        flagged=flagged,
        mention_entry_ids=mention_entry_ids,
        proposed_entry_ids=tuple(proposed_ids),
    )


def _short_circuit_trivial(
    engine: Any,
    *,
    project_id: str,
    segment: repo.SegmentRow,
) -> TranslateOutcome:
    """Persist a "translated" segment without paying for an LLM call.

    Used for segments whose source is wholly placeholders + invisible
    glue characters (``&nbsp;``, BOM, zero-width spaces). The original
    text is round-tripped verbatim so the reassembled ePub keeps its
    structural separators intact (PRD F-IO-2 / F-IO-5). We *don't*
    write an ``llm_call`` row: nothing was called, so the audit log
    shouldn't pretend otherwise. The ``segment.translated_trivial``
    event keeps the breadcrumb for the Inbox / Dashboard activity
    feed, and stats queries that aggregate over ``llm_call`` keep
    their cost / token math correct because the row simply doesn't
    contribute.
    """

    target = segment.source_text
    spliced = _splice_target(segment, target=target)
    validate_segment_placeholders(spliced)

    with engine.begin() as conn:
        repo.update_segment_translation(
            conn,
            segment_id=segment.id,
            target_text=target,
            status=schema.SegmentStatus.TRANSLATED,
        )
        repo.append_event(
            conn,
            project_id=project_id,
            kind="segment.translated_trivial",
            payload={
                "segment_id": segment.id,
                "char_count": len(target),
                "reason": "trivially_empty",
            },
        )

    _logger.info("translated segment %s (trivial / no LLM call)", segment.id)
    trace = TranslatorTrace(target=target)
    return TranslateOutcome(
        segment_id=segment.id,
        target_text=target,
        trace=trace,
        cache_hit=False,
        prompt_tokens=0,
        completion_tokens=0,
        cost_usd=0.0,
        llm_call_id="",
        cache_key="",
        violations=(),
        flagged=False,
        mention_entry_ids=(),
        proposed_entry_ids=(),
        extra={"trivial": True},
    )


def _splice_target(segment: repo.SegmentRow, *, target: str) -> Segment:
    """Reconstruct a runtime :class:`Segment` with the LLM target attached.

    The pipeline never mutates the stored ``SegmentRow`` directly — the
    DB write goes through :func:`repo.update_segment_translation` — but
    we need a :class:`Segment` value object to feed the structural
    validator (it already knows the ``inline_skeleton`` shape).
    """

    return Segment(
        id=segment.id,
        chapter_id=segment.chapter_id,
        idx=segment.idx,
        source_text=segment.source_text,
        source_hash=segment.source_hash,
        target_text=target,
        inline_skeleton=list(segment.inline_skeleton),
        host_path=segment.host_path,
        host_part=segment.host_part,
        host_total_parts=segment.host_total_parts,
    )


def _record_failed_call(
    engine: Any,
    *,
    project_id: str,
    segment_id: str,
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
                segment_id=segment_id,
                purpose=PURPOSE_TRANSLATE,
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
            kind="segment.translation_failed",
            payload={"segment_id": segment_id, "model": model},
        )


def estimate_segment_tokens(
    *,
    source_lang: str,
    target_lang: str,
    source_text: str,
    style_guide: str | None,
    glossary: Sequence[GlossaryConstraint] = (),
    target_only_glossary: Sequence[TargetOnlyConstraint] = (),
    model: str | None = None,
) -> int:
    """Coarse upper-bound on prompt tokens for one segment (PRD F-LLM-4)."""

    messages = build_translator_messages(
        source_lang=source_lang,
        target_lang=target_lang,
        source_text=source_text,
        style_guide=style_guide,
        glossary=glossary,
        target_only_glossary=target_only_glossary,
    )
    return sum(count_tokens(m.content, model=model) for m in messages)


def _load_glossary(engine: Any, *, project_id: str) -> list[GlossaryEntryWithAliases]:
    """Read every glossary entry for ``project_id`` (proposed included).

    The matcher needs proposed entries too — they don't constrain the
    prompt or the validator, but they should still record mentions so
    the curator can see how often a candidate name appears. The hash
    (``glossary_hash``) likewise covers the proposed set so cache keys
    invalidate when the curator promotes or merges entries.

    .. note::
       This helper now only returns *project*-owned entries. Use
       :func:`_load_glossary_view` to also fold in entries from
       attached Lore Books — the broader pipeline does so since
       PRD §4.3 / F-LB-10 phase 3.
    """

    return list(repo.list_glossary_entries(engine, project_id))


def _entries_relevant_to_text(
    text: str,
    entries: Sequence[GlossaryEntryWithAliases],
) -> list[GlossaryEntryWithAliases]:
    """Filter ``entries`` to those whose source term actually occurs in ``text``.

    The pipeline used to ship the *whole* project glossary as
    constraints to every segment, which (a) drowned locked entries in
    noise as the lore bible grew and (b) over-applied common-noun
    entries: "House → Câmara" would be enforced on segments where
    "house" is just a building, even though the matcher would have
    skipped them. By filtering to entries the matcher actually hits in
    this segment, the LLM only sees relevant constraints and the
    validator only fires on entries that were actually in scope.

    Order is preserved from ``entries`` so cache keys are stable.
    Target-only entries (``source_term is None``) are excluded — they
    have no source-side regex to match on. Callers that need them
    keep using :func:`build_target_only_constraints` over the full
    project entries.
    """

    if not entries:
        return []
    matches = match_source(text, entries)
    matched_ids = {m.entry_id for m in matches}
    return [e for e in entries if e.id in matched_ids]


@dataclass(slots=True)
class _GlossaryView:
    """Bundle of glossary state used by one translate call.

    ``entries`` is the merged matcher view (project + attached Lore
    Books, in priority order); ``own_entry_ids`` keeps track of which
    of those entries originate in the project DB so the pipeline can
    avoid recording dangling ``entity_mention`` rows for entries that
    live in another SQLite file. ``writable_lore_engine`` is the
    highest-priority writable Lore Book engine, used for write-back
    routing of auto-proposed entries (PRD F-LB-10 phase 3).

    Lore Book engines are opened lazily by :func:`_load_glossary_view`
    and must be closed via :meth:`close` once the caller is done.
    """

    entries: list[GlossaryEntryWithAliases] = field(default_factory=list)
    own_entry_ids: set[str] = field(default_factory=set)
    writable_lore_engine: Any | None = None
    writable_lore_project_id: str | None = None
    _opened_books: list[Any] = field(default_factory=list, repr=False)

    def filter_mentions(
        self,
        mentions: Sequence[tuple[str, int | None, int | None]],
    ) -> list[tuple[str, int | None, int | None]]:
        """Strip mention tuples whose entry_id is not in this project.

        ``entity_mention`` has a FK on ``glossary_entry.id`` in the
        project DB; mentions for entries that come from an attached
        Lore Book would dangle (or fail the FK with foreign_keys=on).
        We still surface them in the matcher / validator paths — only
        the persisted audit trail is restricted to local entries.
        """

        return [m for m in mentions if m[0] in self.own_entry_ids]

    def close(self) -> None:
        """Dispose of any Lore Book engines opened during load."""

        for book in self._opened_books:
            try:
                book.close()
            except Exception:  # pragma: no cover — best effort cleanup
                _logger.warning("error closing attached lore book", exc_info=True)
        self._opened_books = []
        self.writable_lore_engine = None
        self.writable_lore_project_id = None


def _load_glossary_view(engine: Any, *, project_id: str) -> _GlossaryView:
    """Build a :class:`_GlossaryView` for ``project_id``.

    Steps:

    1. Load the project's own glossary entries.
    2. Resolve attached Lore Books in priority order.
    3. For each Lore Book, open its DB and append its entries to the
       merged list, skipping entries whose ``(source_term, target_term, type)``
       triple is already covered by a higher-priority entry. The
       triple-key dedupe prevents the matcher from double-counting the
       same canonical translation that lives in both a project and an
       attached Lore Book.
    4. Pick the highest-priority writable Lore Book (lowest priority
       number, mode == ``writable``) as the auto-propose write-back
       target.

    A broken or missing Lore Book is logged and skipped — the
    pipeline never throws because the curator detached a folder.
    """

    own_entries = list(repo.list_glossary_entries(engine, project_id))
    attached_rows = repo.list_attached_lore(engine, project_id=project_id)
    if not attached_rows:
        return _GlossaryView(
            entries=own_entries,
            own_entry_ids={e.id for e in own_entries},
        )

    seen_keys: set[tuple[str, str, str]] = {
        (ent.source_term or "", ent.target_term, ent.entry.type) for ent in own_entries
    }
    merged: list[GlossaryEntryWithAliases] = list(own_entries)
    opened_books: list[Any] = []
    writable_engine: Any | None = None
    writable_project_id: str | None = None

    # Imported lazily to avoid a hard dependency cycle: ``epublate.lore``
    # imports ``epublate.core.extractor`` and friends, which in turn
    # touch this module via ``epublate.core.pipeline``. Top-level
    # imports here would form a circular import at module load time.
    from epublate.lore import LoreBook

    for attached in attached_rows:
        from pathlib import Path as _Path

        lore_dir = _Path(attached.lore_path)
        try:
            book = LoreBook.open(lore_dir)
        except Exception as exc:
            _logger.warning(
                "skipping attached lore book %s: %s", attached.lore_path, exc
            )
            continue
        opened_books.append(book)
        if (
            attached.mode == schema.AttachedLoreMode.WRITABLE
            and writable_engine is None
        ):
            writable_engine = book.engine
            writable_project_id = book.project_id
        try:
            for ent in repo.list_glossary_entries(book.engine, book.project_id):
                key = (ent.source_term or "", ent.target_term, ent.entry.type)
                if key in seen_keys:
                    continue
                seen_keys.add(key)
                merged.append(ent)
        except Exception as exc:  # pragma: no cover — best-effort
            _logger.warning(
                "could not read entries from %s: %s", attached.lore_path, exc
            )

    return _GlossaryView(
        entries=merged,
        own_entry_ids={e.id for e in own_entries},
        writable_lore_engine=writable_engine,
        writable_lore_project_id=writable_project_id,
        _opened_books=opened_books,
    )


def _violation_to_payload(violation: Violation) -> dict[str, Any]:
    return {
        "entry_id": violation.entry_id,
        "source_term": violation.source_term,
        "target_term": violation.target_term,
        "matched_source": violation.matched_source,
        "severity": violation.severity,
        "message": violation.message,
    }


def _auto_propose_entities(
    conn: Any,
    *,
    project_id: str,
    segment_id: str,
    trace: TranslatorTrace,
    write_back_engine: Any | None = None,
    write_back_project_id: str | None = None,
) -> list[str]:
    """Upsert ``trace.new_entities`` candidates as ``proposed`` glossary rows.

    By default runs inside the caller's transaction (``conn`` is a
    Connection) so the auto-proposal commits atomically with the
    segment write. We de-dup against existing rows by
    ``(source_term, type)``; an ``entity.proposed`` event is appended
    for each *new* row so a future Inbox screen can surface the
    curator's worklist.

    When ``write_back_engine`` is provided (PRD F-LB-10 phase 3),
    proposals are routed to that engine in a *separate* transaction
    instead of the project DB. Write-back failures are logged but never
    abort the segment commit — auto-proposal is best-effort and the
    canonical translation has already been recorded by the caller.
    """

    created_ids: list[str] = []

    if write_back_engine is not None:
        # ``segment_id`` and ``first_seen_segment_id`` would dangle in
        # the lore book DB (the segment lives in another file), so we
        # null them out for the write-back path. The lore book event
        # log keeps the breadcrumb via ``payload['segment_id']``.
        try:
            with write_back_engine.begin() as lore_conn:
                target_pid = write_back_project_id or project_id
                for raw in trace.new_entities:
                    candidate = _normalize_new_entity(raw)
                    if candidate is None:
                        continue
                    source_term, type_, target_term = candidate
                    entry_id, created = glossary_io.upsert_proposed(
                        lore_conn,
                        project_id=target_pid,
                        source_term=source_term,
                        type=type_,
                        first_seen_segment_id=None,
                        notes=None,
                        target_term=target_term,
                    )
                    if not created:
                        continue
                    created_ids.append(entry_id)
                    repo.append_event(
                        lore_conn,
                        project_id=target_pid,
                        kind="lore.entity_proposed",
                        payload={
                            "entry_id": entry_id,
                            "source_project_id": project_id,
                            "source_segment_id": segment_id,
                            "source_term": source_term,
                            "type": type_,
                        },
                    )
        except Exception as exc:
            _logger.warning(
                "write-back to attached lore book failed for segment %s: %s",
                segment_id,
                exc,
            )
        return created_ids

    for raw in trace.new_entities:
        candidate = _normalize_new_entity(raw)
        if candidate is None:
            continue
        source_term, type_, target_term = candidate
        entry_id, created = glossary_io.upsert_proposed(
            conn,
            project_id=project_id,
            source_term=source_term,
            type=type_,
            first_seen_segment_id=segment_id,
            notes=None,
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
                "segment_id": segment_id,
                "source_term": source_term,
                "type": type_,
            },
        )
    return created_ids


def _normalize_new_entity(
    raw: dict[str, Any],
) -> tuple[str, EntityType, str | None] | None:
    """Coerce one ``new_entities`` item to ``(source, type, target)`` or ``None``.

    The translator prompt asks for
    ``{"type": ..., "source": ..., "target": ..., "evidence": ...}``
    but we accept the ``"_term"`` variants too in case the model uses
    the longer keys. Anything missing or empty is dropped silently —
    auto-proposal is best-effort, not a hard contract. ``target`` is
    optional and falls back to ``None`` when the model omitted it; the
    glossary IO layer treats ``None`` and ``source_term``-equal as
    "no real translation observed yet".
    """

    if not isinstance(raw, dict):
        return None
    source_term = raw.get("source") or raw.get("source_term")
    if not isinstance(source_term, str):
        return None
    source_term = source_term.strip()
    if not source_term:
        return None
    type_str = str(raw.get("type", "term")).strip().lower() or "term"
    if type_str not in _VALID_ENTITY_TYPES:
        type_str = "term"
    target_raw = raw.get("target") or raw.get("target_term")
    if isinstance(target_raw, str):
        target_term: str | None = target_raw.strip() or None
    else:
        target_term = None
    return source_term, cast(EntityType, type_str), target_term


def is_group_eligible(
    segment: repo.SegmentRow,
    *,
    max_source_chars: int = GROUP_DEFAULT_MAX_SOURCE_CHARS,
    max_placeholders: int = GROUP_DEFAULT_MAX_PLACEHOLDERS,
) -> bool:
    """Cheap check: is ``segment`` safe to translate in a batched call?

    The grouped call path trades one round-trip per N items for a
    slightly less context-rich prompt. It's only a win for segments
    that are (a) short enough to fit N of them in a single prompt,
    (b) light on inline-tag placeholders so a parse failure doesn't
    risk format corruption across many segments, and (c) still
    ``pending`` (grouping already-translated content wastes tokens).

    Light placeholder use *is* allowed (TOC / index links wrap their
    text in a single ``<a>`` and end up with one ``[[T0]]…[[/T0]]``
    pair). The grouped prompt scopes placeholder ids per item; a
    response that mangles them still falls back to per-segment
    translation via :func:`translate_segments_grouped`, so the cap on
    ``max_placeholders`` is really a heuristic for "this is too markup-
    heavy to risk batching".
    """

    if segment.status != schema.SegmentStatus.PENDING:
        return False
    text = segment.source_text or ""
    # Skip whitespace-only / placeholder-only items: they're handled by
    # the trivial short-circuit in ``translate_segment`` and grouping
    # them just clutters the batch payload.
    if is_trivially_empty(text):
        return False
    if len(text) > max_source_chars:
        return False
    placeholder_count = sum(1 for _ in PLACEHOLDER_RE.finditer(text))
    return placeholder_count <= max_placeholders


def translate_segments_grouped(
    *,
    engine: Any,
    project_id: str,
    source_lang: str,
    target_lang: str,
    style_guide: str | None,
    segments: Sequence[repo.SegmentRow],
    provider: LLMProvider,
    options: TranslateOptions,
) -> list[TranslateOutcome]:
    """Translate a batch of short, placeholder-free segments in ONE LLM call.

    Intended for dense list-like content (table of contents, index
    entries, glossary labels) where per-segment round-trips dominate
    cost and latency. The caller guarantees every input segment has
    already been filtered through :func:`is_group_eligible`; passing a
    segment with placeholders or long text is a programmer error and
    the function falls back to per-segment translation for those rows.

    Persistence invariants:

    * Each input segment still ends up with its own ``llm_call`` row
      and its own ``segment.translated`` event. Tokens and cost are
      allocated proportional to completion text length so a future
      audit query can re-derive spend per segment.
    * Cache keys are computed per segment exactly like
      :func:`translate_segment` would — so a second run with the same
      glossary finds the cached rows and skips the group call entirely.
    * Locked glossary violations flag the segment individually (one
      bad item doesn't taint the whole batch).

    Failure modes:

    * If the LLM response fails to parse, or is missing any of the
      requested ids, or one item fails the placeholder validator
      (should not happen by construction — defensive), that item is
      re-translated through the per-segment path. The rest of the
      batch still commits.
    """

    glossary_view = _load_glossary_view(engine, project_id=project_id)
    project_entries = glossary_view.entries
    explicit_constraints = options.glossary is not None
    fallback_target_only_constraints = (
        [] if explicit_constraints else build_target_only_constraints(project_entries)
    )
    g_hash = glossary_hash(project_entries)

    outcomes: list[TranslateOutcome | None] = [None] * len(segments)
    group_indices: list[int] = []
    group_segments: list[repo.SegmentRow] = []
    group_keys: list[str] = []

    # Pass 1 — try the cache per segment (exactly like translate_segment
    # would) so any previously translated item short-circuits without
    # paying for the whole group call. Each segment gets its own
    # constraint subset so cache keys match what
    # :func:`translate_segment` would compute for the same input.
    for idx, seg in enumerate(segments):
        if not is_group_eligible(seg):
            outcomes[idx] = translate_segment(
                engine=engine,
                project_id=project_id,
                source_lang=source_lang,
                target_lang=target_lang,
                style_guide=style_guide,
                segment=seg,
                provider=provider,
                options=options,
            )
            continue

        if explicit_constraints:
            seg_constraints = list(options.glossary or ())
            seg_target_only: list[TargetOnlyConstraint] = []
        else:
            seg_relevant = _entries_relevant_to_text(seg.source_text, project_entries)
            seg_constraints = build_constraints(seg_relevant)
            seg_target_only = fallback_target_only_constraints

        messages = build_translator_messages(
            source_lang=source_lang,
            target_lang=target_lang,
            source_text=seg.source_text,
            style_guide=style_guide,
            glossary=seg_constraints,
            target_only_glossary=seg_target_only,
        )
        key = cache_key_for_messages(
            model=options.model, messages=messages, glossary_hash=g_hash
        )
        if options.bypass_cache:
            key = f"{key}:retry"

        if not options.bypass_cache:
            hit = repo.find_llm_call_by_cache_key(
                engine, project_id=project_id, cache_key=key
            )
            if hit is not None and hit.response_json:
                request_payload = {
                    "model": options.model,
                    "messages": [m.model_dump() for m in messages],
                    "temperature": options.temperature,
                    "seed": options.seed,
                    "glossary_hash": g_hash,
                }
                request_json = json.dumps(
                    request_payload, ensure_ascii=False, sort_keys=True
                )
                outcomes[idx] = _replay_from_cache(
                    engine,
                    project_id=project_id,
                    segment=seg,
                    key=key,
                    hit=hit,
                    request_json=request_json,
                    options=options,
                    entries=project_entries,
                    glossary_view=glossary_view,
                )
                continue

        group_indices.append(idx)
        group_segments.append(seg)
        group_keys.append(key)

    if not group_segments:
        # Every segment was a cache hit or ineligible — nothing to do.
        glossary_view.close()
        return [o for o in outcomes if o is not None]

    # Pass 2 — one LLM call for the surviving misses. The batch's
    # system prompt ships the union of every relevant entry across the
    # surviving items so each item still sees the constraints it needs
    # without re-introducing irrelevant entries from elsewhere in the
    # project. The cache key for each item was computed in pass 1 from
    # its *own* relevant subset; pass 2's union doesn't enter the cache
    # key — it only governs what the batched LLM call actually sees.
    source_items: list[tuple[int, str]] = [
        (idx, seg.source_text) for idx, seg in enumerate(group_segments)
    ]
    if explicit_constraints:
        group_constraints = list(options.glossary or ())
        group_target_only: list[TargetOnlyConstraint] = []
    else:
        union_text = "\n\n".join(seg.source_text for seg in group_segments)
        union_relevant = _entries_relevant_to_text(union_text, project_entries)
        group_constraints = build_constraints(union_relevant)
        group_target_only = fallback_target_only_constraints
    group_messages = build_group_translator_messages(
        source_lang=source_lang,
        target_lang=target_lang,
        source_items=source_items,
        style_guide=style_guide,
        glossary=group_constraints,
        target_only_glossary=group_target_only,
    )
    request_payload = {
        "model": options.model,
        "messages": [m.model_dump() for m in group_messages],
        "temperature": options.temperature,
        "seed": options.seed,
        "glossary_hash": g_hash,
        "group_size": len(group_segments),
    }
    group_request_json = json.dumps(request_payload, ensure_ascii=False, sort_keys=True)

    try:
        chat_result = provider.chat(
            group_messages,
            model=options.model,
            response_format=options.response_format,
            temperature=options.temperature,
            seed=options.seed,
        )
    except Exception as exc:
        _logger.warning(
            "group translate call failed (%d items): %s — falling back per-segment",
            len(group_segments),
            exc,
        )
        glossary_view.close()
        return _fill_fallback(
            engine=engine,
            project_id=project_id,
            source_lang=source_lang,
            target_lang=target_lang,
            style_guide=style_guide,
            segments=segments,
            outcomes=outcomes,
            group_indices=group_indices,
            provider=provider,
            options=options,
        )

    expected_ids = [idx for idx, _ in source_items]
    try:
        parsed = parse_group_translator_response(
            chat_result.content, expected_ids=expected_ids
        )
    except EpublateError as exc:
        _logger.warning(
            "group translate parse failed — falling back per-segment (%s)", exc
        )
        glossary_view.close()
        return _fill_fallback(
            engine=engine,
            project_id=project_id,
            source_lang=source_lang,
            target_lang=target_lang,
            style_guide=style_guide,
            segments=segments,
            outcomes=outcomes,
            group_indices=group_indices,
            provider=provider,
            options=options,
        )

    items_by_id: dict[int, GroupTranslatorItem] = {
        item.id: item for item in parsed.translations
    }

    def _target_len(item_id: int) -> int:
        item = items_by_id.get(item_id)
        return len(item.target) if item is not None else 0

    token_weights = [max(1, _target_len(i)) for i in expected_ids]
    total_weight = sum(token_weights)
    per_prompt = [
        _split_tokens(chat_result.prompt_tokens, w, total_weight) for w in token_weights
    ]
    per_completion = [
        _split_tokens(chat_result.completion_tokens, w, total_weight)
        for w in token_weights
    ]

    per_idx_cost = [
        estimate_cost(chat_result.model, p, c)
        for p, c in zip(per_prompt, per_completion, strict=True)
    ]

    # Per-item commit: each surviving (non-cache-hit) segment lands
    # its own ``llm_call`` row so future queries still get per-segment
    # tokens / cost. The response_json is the full batch payload so
    # replay semantics are unchanged; the cache_key is the per-segment
    # key computed during pass 1.
    for local_idx, (global_idx, seg) in enumerate(
        zip(group_indices, group_segments, strict=True)
    ):
        target_item = items_by_id.get(local_idx)
        if target_item is None:
            # Ask for a re-translation individually; the LLM response
            # for this id was invalid even though the overall batch
            # parsed. Shouldn't happen given ``expected_ids`` is
            # validated, but belt-and-braces.
            outcomes[global_idx] = translate_segment(
                engine=engine,
                project_id=project_id,
                source_lang=source_lang,
                target_lang=target_lang,
                style_guide=style_guide,
                segment=seg,
                provider=provider,
                options=options,
            )
            continue

        trace = TranslatorTrace(
            target=target_item.target,
            used_entries=target_item.used_entries,
            new_entities=target_item.new_entities,
            notes=target_item.notes,
        )
        try:
            spliced = _splice_target(seg, target=trace.target)
            validate_segment_placeholders(spliced)
        except EpublateError as exc:
            _logger.warning(
                "group item %d failed placeholder validation (%s); "
                "falling back per-segment",
                local_idx,
                exc,
            )
            outcomes[global_idx] = translate_segment(
                engine=engine,
                project_id=project_id,
                source_lang=source_lang,
                target_lang=target_lang,
                style_guide=style_guide,
                segment=seg,
                provider=provider,
                options=options,
            )
            continue

        # Persist a ``TranslatorTrace``-shaped payload (no ``id`` field)
        # so :func:`_replay_from_cache` can rehydrate the entry on a
        # subsequent run without special-casing the grouped shape.
        per_item_response_json = json.dumps(
            {
                "content": chat_result.content,
                "trace": trace.model_dump(),
                "raw": chat_result.raw,
                "batch_size": len(group_segments),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        outcomes[global_idx] = _commit_group_item(
            engine=engine,
            project_id=project_id,
            segment=seg,
            key=group_keys[local_idx],
            trace=trace,
            prompt_tokens=per_prompt[local_idx],
            completion_tokens=per_completion[local_idx],
            cost=per_idx_cost[local_idx],
            model=chat_result.model,
            request_json=group_request_json,
            batch_response_json=per_item_response_json,
            entries=project_entries,
            options=options,
            glossary_view=glossary_view,
        )

    assert all(o is not None for o in outcomes), "group fill left a hole"
    glossary_view.close()
    return [cast(TranslateOutcome, o) for o in outcomes]


def _fill_fallback(
    *,
    engine: Any,
    project_id: str,
    source_lang: str,
    target_lang: str,
    style_guide: str | None,
    segments: Sequence[repo.SegmentRow],
    outcomes: list[TranslateOutcome | None],
    group_indices: Sequence[int],
    provider: LLMProvider,
    options: TranslateOptions,
) -> list[TranslateOutcome]:
    """Complete ``outcomes`` by running ``translate_segment`` for every miss.

    Called when a group LLM call / parse fails. The cache-hit entries
    already sit in ``outcomes``; this just fills the remaining slots.
    """

    for idx in group_indices:
        if outcomes[idx] is not None:
            continue
        outcomes[idx] = translate_segment(
            engine=engine,
            project_id=project_id,
            source_lang=source_lang,
            target_lang=target_lang,
            style_guide=style_guide,
            segment=segments[idx],
            provider=provider,
            options=options,
        )
    assert all(o is not None for o in outcomes), "fallback left a hole"
    return [cast(TranslateOutcome, o) for o in outcomes]


def _commit_group_item(
    *,
    engine: Any,
    project_id: str,
    segment: repo.SegmentRow,
    key: str,
    trace: TranslatorTrace,
    prompt_tokens: int,
    completion_tokens: int,
    cost: float,
    model: str,
    request_json: str,
    batch_response_json: str,
    entries: Sequence[GlossaryEntryWithAliases],
    options: TranslateOptions,
    glossary_view: _GlossaryView | None = None,
) -> TranslateOutcome:
    """Persist one item from a successful group call.

    Mirrors the non-cache branch of :func:`translate_segment`: runs
    glossary validation + auto-propose, writes the segment update +
    ``llm_call`` audit row + events in a single transaction.

    ``glossary_view`` carries attached-Lore-Book metadata so we can
    skip mentions for entries that live in another DB and route
    auto-proposed entries to a writable Lore Book when one is
    configured (PRD F-LB-10 phase 3).
    """

    violations = validate_target(
        source_text=segment.source_text,
        target_text=trace.target,
        entries=entries,
    )
    flagged = has_locked_violation(violations)
    final_status = (
        schema.SegmentStatus.FLAGGED if flagged else schema.SegmentStatus.TRANSLATED
    )

    mentions = find_mentions(segment.source_text, entries)
    mention_entry_ids = tuple(dict.fromkeys(m.entry_id for m in mentions))
    raw_mentions: list[tuple[str, int | None, int | None]] = [
        (m.entry_id, m.start, m.end) for m in mentions
    ]
    mention_payload = (
        glossary_view.filter_mentions(raw_mentions)
        if glossary_view is not None
        else raw_mentions
    )

    llm_call_id = uuid.uuid4().hex
    proposed_ids: list[str] = []
    with engine.begin() as conn:
        repo.update_segment_translation(
            conn,
            segment_id=segment.id,
            target_text=trace.target,
            status=final_status,
        )
        repo.record_mentions(conn, segment_id=segment.id, mentions=mention_payload)
        repo.insert_llm_call(
            conn,
            repo.LLMCallRow(
                id=llm_call_id,
                project_id=project_id,
                segment_id=segment.id,
                purpose=PURPOSE_TRANSLATE,
                model=model,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                cost_usd=cost,
                cache_hit=False,
                cache_key=key,
                request_json=request_json,
                response_json=batch_response_json,
            ),
        )
        repo.append_event(
            conn,
            project_id=project_id,
            kind="segment.translated",
            payload={
                "segment_id": segment.id,
                "model": model,
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "cost_usd": cost,
                "cache_hit": False,
                "llm_call_id": llm_call_id,
                "mention_entry_ids": list(mention_entry_ids),
                "flagged": flagged,
                "grouped": True,
            },
        )
        if flagged:
            repo.append_event(
                conn,
                project_id=project_id,
                kind="segment.translation_flagged",
                payload={
                    "segment_id": segment.id,
                    "violations": [_violation_to_payload(v) for v in violations],
                },
            )
        if options.auto_propose and (
            glossary_view is None or glossary_view.writable_lore_engine is None
        ):
            proposed_ids.extend(
                _auto_propose_entities(
                    conn,
                    project_id=project_id,
                    segment_id=segment.id,
                    trace=trace,
                )
            )
    if (
        options.auto_propose
        and glossary_view is not None
        and glossary_view.writable_lore_engine is not None
    ):
        proposed_ids.extend(
            _auto_propose_entities(
                None,
                project_id=project_id,
                segment_id=segment.id,
                trace=trace,
                write_back_engine=glossary_view.writable_lore_engine,
                write_back_project_id=glossary_view.writable_lore_project_id,
            )
        )
    _logger.info(
        "translated segment %s (grouped, %d→%d tokens, $%.6f) via %s%s",
        segment.id,
        prompt_tokens,
        completion_tokens,
        cost,
        model,
        " [flagged]" if flagged else "",
    )
    return TranslateOutcome(
        segment_id=segment.id,
        target_text=trace.target,
        trace=trace,
        cache_hit=False,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        cost_usd=cost,
        llm_call_id=llm_call_id,
        cache_key=key,
        violations=tuple(violations),
        flagged=flagged,
        mention_entry_ids=mention_entry_ids,
        proposed_entry_ids=tuple(proposed_ids),
        extra={"grouped": True},
    )


def _split_tokens(total: int, weight: int, total_weight: int) -> int:
    if total_weight <= 0 or weight <= 0:
        return 0
    return max(0, (total * weight) // total_weight)


__all__ = [
    "GROUP_DEFAULT_MAX_ITEMS",
    "GROUP_DEFAULT_MAX_PLACEHOLDERS",
    "GROUP_DEFAULT_MAX_SOURCE_CHARS",
    "PURPOSE_TRANSLATE",
    "PipelineError",
    "TranslateOptions",
    "TranslateOutcome",
    "estimate_segment_tokens",
    "is_group_eligible",
    "translate_segment",
    "translate_segments_grouped",
]
