"""Ingest a *target-language* ePub into a Lore Book (PRD F-LB-3 / F-LB-10).

The use case: a curator already owns a translated edition of a series
(e.g. "Witcher Lore" target-PT) and wants to extract the canonical
target spellings of every recurring proper noun so the translator
pipeline can use them as soft-locked constraints when translating the
*next* book in the series.

Unlike :mod:`epublate.lore.ingest`, the Lore Book here receives
**target-only** entries: the model never sees source text and never
proposes a source spelling. The translator pipeline (Phase 3) picks
up the source mapping on the fly when it encounters the entity in a
later project.

Mechanics:

1. Parse the target ePub with :class:`epublate.formats.epub.EpubAdapter`.
2. Walk the chapters (capped by ``max_chapters``) and harvest the
   plain-text content (placeholders stripped — we only care about the
   prose).
3. Group the prose into chunks under ``chunk_max_chars`` (we use a
   character cap, not a token cap, because the helper LLM is reading
   the same language we'd translate *into* — token counts are
   provider-dependent and the per-chunk LLM call is the slow step
   anyway).
4. For each chunk, call the helper LLM with the
   :func:`epublate.llm.prompts.extractor_target.build_target_extractor_messages`
   prompt, parse the response, and upsert each entity as a target-only
   ``proposed`` glossary row.
5. Record one ``lore_source`` row with the rolled-up entries-added
   count and emit a ``lore.target_ingested`` event.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy.engine import Connection

from epublate.core.cache import cache_key_for_messages
from epublate.core.segmentation import PLACEHOLDER_RE, placeholderize
from epublate.db import repo, schema
from epublate.errors import ConfigurationError, LLMResponseError
from epublate.formats.epub import EpubAdapter
from epublate.glossary.enforcer import glossary_hash
from epublate.glossary.models import EntityType, GlossaryEntryWithAliases
from epublate.llm.base import LLMProvider, ResponseFormat
from epublate.llm.json_mode import chat_with_json_fallback
from epublate.llm.pricing import estimate_cost
from epublate.llm.prompts.extractor_target import (
    DEFAULT_RESPONSE_FORMAT as DEFAULT_TARGET_EXTRACTOR_RESPONSE_FORMAT,
)
from epublate.llm.prompts.extractor_target import (
    TargetExtractedEntity,
    TargetExtractorTrace,
    build_target_extractor_messages,
    parse_target_extractor_response,
)
from epublate.llm.prompts.translator import GlossaryConstraint
from epublate.lore import repo as lore_repo
from epublate.lore.lore import LoreBook

_logger = logging.getLogger(__name__)

PURPOSE_LORE_TARGET_EXTRACT = "lore_target_extract"
DEFAULT_TARGET_CHUNK_MAX_CHARS = 6000
DEFAULT_TARGET_MAX_CHAPTERS = 8
DEFAULT_TARGET_FAILURE_STREAK_LIMIT = 3
"""Abort the loop after this many *consecutive* failed chunks.

Mirrors :data:`epublate.core.extractor.DEFAULT_FAILURE_STREAK_LIMIT`.
Set to ``0`` to disable the breaker and keep the legacy "best-effort,
never abort" behavior.
"""

_TARGET_ERROR_TRUNCATION = 240

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
class _TargetChunk:
    """One ``(text, chapter_index)`` pair fed to the helper LLM."""

    text: str
    chapter_index: int


@dataclass(slots=True)
class TargetIngestSummary:
    """Aggregate result of one :func:`ingest_target_epub` call."""

    chunks: int = 0
    cached_chunks: int = 0
    proposed_count: int = 0
    failed_chunks: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0
    proposed_entry_ids: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def ingest_target_epub(
    lore_book: LoreBook,
    *,
    epub_path: Path,
    provider: LLMProvider,
    helper_model: str,
    max_chapters: int = DEFAULT_TARGET_MAX_CHAPTERS,
    chunk_max_chars: int = DEFAULT_TARGET_CHUNK_MAX_CHARS,
    bypass_cache: bool = False,
    notes: str | None = None,
    response_format: ResponseFormat | None = DEFAULT_TARGET_EXTRACTOR_RESPONSE_FORMAT,
    failure_streak_limit: int = DEFAULT_TARGET_FAILURE_STREAK_LIMIT,
) -> tuple[lore_repo.LoreSourceRow, TargetIngestSummary]:
    """Run a target-language extractor pass against ``epub_path``.

    Steps:

    1. Stash the ePub under the Lore Book's ``sources/`` dir.
    2. Walk the first ``max_chapters`` chapters and chunk the prose.
    3. Call the helper LLM on each chunk and upsert the entities.
    4. Insert a ``lore_source`` row with the totals and emit a
       ``lore.target_ingested`` event.

    Returns the ``lore_source`` row plus a summary the CLI / TUI can
    surface to the curator. Per-chunk LLM failures are recorded as
    ``lore.target_extract_failed`` events and counted, but never
    abort the whole ingest — best-effort by design.
    """

    epub_path = Path(epub_path).resolve()
    if not epub_path.is_file():
        raise ConfigurationError(f"target ePub not found: {epub_path}")

    stash_path = lore_book.stash_source(epub_path)

    chunks = _chunk_target_epub(
        stash_path,
        target_lang=lore_book.target_lang,
        max_chapters=max_chapters,
        chunk_max_chars=chunk_max_chars,
    )

    summary = TargetIngestSummary()
    repo.append_event(
        lore_book.engine,
        project_id=lore_book.project_id,
        kind="lore.target_ingest_started",
        payload={
            "epub_path": str(stash_path),
            "model": helper_model,
            "max_chapters": max_chapters,
            "chunk_count": len(chunks),
        },
    )

    if not chunks:
        source_row = lore_repo.insert_lore_source(
            lore_book.engine,
            project_id=lore_book.project_id,
            kind=schema.LoreSourceKind.TARGET,
            epub_path=str(stash_path),
            status=schema.LoreSourceStatus.INGESTED,
            entries_added=0,
            notes=notes,
        )
        repo.append_event(
            lore_book.engine,
            project_id=lore_book.project_id,
            kind="lore.target_ingested",
            payload={
                "source_id": source_row.id,
                "epub_path": str(stash_path),
                "proposed_count": 0,
                "status": schema.LoreSourceStatus.INGESTED,
            },
        )
        return source_row, summary

    glossary_entries = repo.list_glossary_entries(
        lore_book.engine, lore_book.project_id
    )

    streak = 0
    last_error: str | None = None
    aborted = False
    for idx, chunk in enumerate(chunks):
        try:
            outcome = _extract_target_chunk(
                lore_book=lore_book,
                chunk=chunk,
                provider=provider,
                helper_model=helper_model,
                bypass_cache=bypass_cache,
                response_format=response_format,
                glossary_constraints_hash=glossary_hash(glossary_entries),
                glossary_constraints_json=_constraints_for_prompt(glossary_entries),
            )
        except LLMResponseError as exc:
            _logger.warning("lore target ingest chunk failed: %s", exc)
            summary.failed_chunks += 1
            repo.append_event(
                lore_book.engine,
                project_id=lore_book.project_id,
                kind="lore.target_extract_failed",
                payload={
                    "chapter_index": chunk.chapter_index,
                    "reason": str(exc),
                },
            )
            streak += 1
            last_error = _short_target_error(exc)
            if _should_trip_target_breaker(streak, failure_streak_limit):
                aborted = True
                _log_target_abort(
                    streak=streak,
                    remaining=len(chunks) - (idx + 1),
                    last_error=last_error,
                )
                summary.failed_chunks += len(chunks) - (idx + 1)
                break
            continue
        except Exception as exc:
            _logger.warning("lore target ingest chunk failed: %s", exc)
            summary.failed_chunks += 1
            streak += 1
            last_error = _short_target_error(exc)
            if _should_trip_target_breaker(streak, failure_streak_limit):
                aborted = True
                _log_target_abort(
                    streak=streak,
                    remaining=len(chunks) - (idx + 1),
                    last_error=last_error,
                )
                summary.failed_chunks += len(chunks) - (idx + 1)
                break
            continue

        streak = 0
        summary.chunks += 1
        summary.prompt_tokens += outcome.prompt_tokens
        summary.completion_tokens += outcome.completion_tokens
        summary.cost_usd += outcome.cost_usd
        if outcome.cache_hit:
            summary.cached_chunks += 1
        if outcome.trace.notes:
            summary.notes.append(outcome.trace.notes)
        summary.proposed_count += len(outcome.proposed_entry_ids)
        summary.proposed_entry_ids.extend(outcome.proposed_entry_ids)

        if outcome.proposed_entry_ids:
            glossary_entries = repo.list_glossary_entries(
                lore_book.engine, lore_book.project_id
            )

    source_row = lore_repo.insert_lore_source(
        lore_book.engine,
        project_id=lore_book.project_id,
        kind=schema.LoreSourceKind.TARGET,
        epub_path=str(stash_path),
        status=schema.LoreSourceStatus.INGESTED,
        entries_added=summary.proposed_count,
        notes=notes,
    )
    repo.append_event(
        lore_book.engine,
        project_id=lore_book.project_id,
        kind=("lore.target_ingest_aborted" if aborted else "lore.target_ingested"),
        payload={
            "source_id": source_row.id,
            "epub_path": str(stash_path),
            "proposed_count": summary.proposed_count,
            "failed_chunks": summary.failed_chunks,
            "status": schema.LoreSourceStatus.INGESTED,
            "cost_usd": summary.cost_usd,
            **({"failure_streak": streak, "last_error": last_error} if aborted else {}),
        },
    )
    return source_row, summary


@dataclass(slots=True, frozen=True)
class _TargetExtractOutcome:
    """Per-chunk outcome for the internal accumulator."""

    trace: TargetExtractorTrace
    cache_hit: bool
    prompt_tokens: int
    completion_tokens: int
    cost_usd: float
    proposed_entry_ids: tuple[str, ...]


def _should_trip_target_breaker(streak: int, limit: int) -> bool:
    """Whether the failure streak crossed the configured circuit-breaker limit.

    Mirrors :func:`epublate.core.extractor._should_trip_breaker`. A
    non-positive ``limit`` disables the breaker.
    """

    return limit > 0 and streak >= limit


def _short_target_error(exc: BaseException) -> str:
    """Truncate an exception message for an event payload / log line."""

    text = str(exc)
    if len(text) <= _TARGET_ERROR_TRUNCATION:
        return text
    return text[: _TARGET_ERROR_TRUNCATION - 1] + "\u2026"


def _log_target_abort(*, streak: int, remaining: int, last_error: str) -> None:
    """Single-line warning for a tripped target-ingest circuit breaker.

    Calls out the most likely root cause (reasoning helper on a hosted
    endpoint that returns empty visible content) so the curator has a
    one-step fix to try.
    """

    _logger.warning(
        "lore target ingest aborted after %d consecutive failed chunks "
        "(skipping %d remaining). Last error: %s. If you're using a "
        "reasoning-style helper (gpt-oss-*, deepseek-r1, ...), it may "
        "be returning empty visible content because reasoning tokens "
        "consume the entire visible-channel budget. Try a non-reasoning "
        "helper via $EPUBLATE_LLM_HELPER_MODEL or the project override.",
        streak,
        remaining,
        last_error,
    )


def _extract_target_chunk(
    *,
    lore_book: LoreBook,
    chunk: _TargetChunk,
    provider: LLMProvider,
    helper_model: str,
    bypass_cache: bool,
    response_format: ResponseFormat | None,
    glossary_constraints_hash: str,
    glossary_constraints_json: str,
) -> _TargetExtractOutcome:
    """Run one helper-LLM target-extraction call and persist the audit row."""

    glossary_entries = repo.list_glossary_entries(
        lore_book.engine, lore_book.project_id
    )
    messages = build_target_extractor_messages(
        target_lang=lore_book.target_lang,
        target_text=chunk.text,
        glossary=_target_glossary_constraints(glossary_entries),
    )
    key = cache_key_for_messages(
        model=helper_model,
        messages=messages,
        glossary_hash=glossary_constraints_hash,
    )
    if bypass_cache:
        key = f"{key}:retry"

    request_payload = {
        "model": helper_model,
        "purpose": PURPOSE_LORE_TARGET_EXTRACT,
        "messages": [m.model_dump() for m in messages],
        "glossary_hash": glossary_constraints_hash,
        "glossary_constraints": glossary_constraints_json,
        "chapter_index": chunk.chapter_index,
    }
    request_json = json.dumps(request_payload, ensure_ascii=False, sort_keys=True)

    if not bypass_cache:
        hit = repo.find_llm_call_by_cache_key(
            lore_book.engine, project_id=lore_book.project_id, cache_key=key
        )
        if hit is not None and hit.response_json:
            return _replay_target_from_cache(
                lore_book=lore_book,
                key=key,
                hit=hit,
                request_json=request_json,
            )

    chat_result = chat_with_json_fallback(
        provider,
        messages,
        model=helper_model,
        response_format=response_format,
        temperature=0.0,
        seed=7,
    )
    trace = parse_target_extractor_response(chat_result.content)
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
    with lore_book.engine.begin() as conn:
        repo.insert_llm_call(
            conn,
            repo.LLMCallRow(
                id=llm_call_id,
                project_id=lore_book.project_id,
                segment_id=None,
                purpose=PURPOSE_LORE_TARGET_EXTRACT,
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
        proposed_ids.extend(
            _upsert_target_entities(conn, project_id=lore_book.project_id, trace=trace)
        )
        repo.append_event(
            conn,
            project_id=lore_book.project_id,
            kind="lore.target_chunk_extracted",
            payload={
                "llm_call_id": llm_call_id,
                "chapter_index": chunk.chapter_index,
                "model": chat_result.model,
                "cache_hit": False,
                "entities": len(trace.entities),
                "proposed": len(proposed_ids),
            },
        )

    return _TargetExtractOutcome(
        trace=trace,
        cache_hit=False,
        prompt_tokens=chat_result.prompt_tokens,
        completion_tokens=chat_result.completion_tokens,
        cost_usd=cost,
        proposed_entry_ids=tuple(proposed_ids),
    )


def _replay_target_from_cache(
    *,
    lore_book: LoreBook,
    key: str,
    hit: repo.LLMCallRow,
    request_json: str,
) -> _TargetExtractOutcome:
    """Hydrate a target-extract outcome from a cached ``llm_call`` row."""

    payload = json.loads(hit.response_json or "{}")
    trace_data = payload.get("trace") if isinstance(payload, dict) else None
    if not isinstance(trace_data, dict):
        content = payload.get("content") if isinstance(payload, dict) else None
        if not isinstance(content, str):
            raise LLMResponseError(
                f"cached llm_call {hit.id} has no usable target-extractor payload"
            )
        trace = parse_target_extractor_response(content)
    else:
        trace = TargetExtractorTrace.model_validate(trace_data)

    response_json = hit.response_json or json.dumps(
        {"content": "", "trace": trace.model_dump()},
        ensure_ascii=False,
        sort_keys=True,
    )
    new_id = uuid.uuid4().hex
    proposed_ids: list[str] = []
    with lore_book.engine.begin() as conn:
        repo.insert_llm_call(
            conn,
            repo.LLMCallRow(
                id=new_id,
                project_id=lore_book.project_id,
                segment_id=None,
                purpose=PURPOSE_LORE_TARGET_EXTRACT,
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
        proposed_ids.extend(
            _upsert_target_entities(conn, project_id=lore_book.project_id, trace=trace)
        )

    return _TargetExtractOutcome(
        trace=trace,
        cache_hit=True,
        prompt_tokens=hit.prompt_tokens or 0,
        completion_tokens=hit.completion_tokens or 0,
        cost_usd=0.0,
        proposed_entry_ids=tuple(proposed_ids),
    )


def _upsert_target_entities(
    conn: Connection,
    *,
    project_id: str,
    trace: TargetExtractorTrace,
) -> list[str]:
    """Insert each candidate as a target-only proposed glossary entry.

    Dedup key is ``(target_term, type)``: if a row with the same
    target spelling and entity kind already exists in this Lore Book
    we leave it alone (the curator may have already promoted it). New
    rows land with ``source_term=None`` / ``source_known=False`` so
    the validator (Phase 3) treats them as soft locks.
    """

    created_ids: list[str] = []
    existing = repo.list_glossary_entries(conn, project_id)
    by_key = {(e.target_term, e.entry.type): e for e in existing}
    for ent in trace.entities:
        candidate = _normalize_entity(ent)
        if candidate is None:
            continue
        target_term, type_, aliases, evidence = candidate
        if (target_term, type_) in by_key:
            continue
        entry = repo.create_glossary_entry(
            conn,
            project_id=project_id,
            source_term=None,
            target_term=target_term,
            type=type_,
            status="proposed",
            notes=evidence,
            source_known=False,
            source_aliases=(),
            target_aliases=aliases,
        )
        created_ids.append(entry.id)
        repo.append_event(
            conn,
            project_id=project_id,
            kind="lore.entity_proposed",
            payload={
                "entry_id": entry.id,
                "target_term": target_term,
                "type": type_,
                "source": "lore_target_extractor",
            },
        )
    return created_ids


def _normalize_entity(
    ent: TargetExtractedEntity,
) -> tuple[str, EntityType, list[str], str | None] | None:
    target_term = ent.target.strip()
    if not target_term:
        return None
    type_str = ent.type.strip().lower() if ent.type else "term"
    if type_str not in _VALID_ENTITY_TYPES:
        type_str = "term"
    notes = ent.evidence.strip() if ent.evidence else None
    aliases = [a for a in ent.aliases if a and a != target_term]
    return target_term, type_str, aliases, notes  # type: ignore[return-value]


def _target_glossary_constraints(
    entries: Iterable[GlossaryEntryWithAliases],
) -> list[GlossaryConstraint]:
    """Build prompt-shape constraints for a target-language pass.

    The target extractor only uses ``status`` (to skip ``proposed``) and
    ``target_term`` to render the "do not re-propose these" block.
    Source-keyed entries hand their canonical target term over verbatim;
    target-only entries do too. We synthesize a placeholder source term
    for target-only rows because :class:`GlossaryConstraint` requires
    one — it never reaches the prompt rendering code path that cares.
    """

    out: list[GlossaryConstraint] = []
    for ent in entries:
        if ent.status == "proposed":
            continue
        out.append(
            GlossaryConstraint(
                source_term=ent.entry.source_term or f"<target:{ent.target_term}>",
                target_term=ent.target_term,
                type=ent.entry.type,
                status=ent.status,
                notes=ent.entry.notes,
            )
        )
    return out


def _constraints_for_prompt(
    entries: Iterable[GlossaryEntryWithAliases],
) -> str:
    """Stable JSON projection of the constraints, for the cache key payload."""

    out: list[dict[str, str]] = []
    for ent in entries:
        out.append(
            {
                "type": ent.entry.type,
                "target_term": ent.target_term,
                "status": ent.status,
            }
        )
    out.sort(key=lambda r: (r["type"], r["target_term"], r["status"]))
    return json.dumps(out, ensure_ascii=False, sort_keys=True)


def _chunk_target_epub(
    epub_path: Path,
    *,
    target_lang: str,
    max_chapters: int,
    chunk_max_chars: int,
) -> list[_TargetChunk]:
    """Walk the first ``max_chapters`` chapters and chunk their prose."""

    if max_chapters <= 0 or chunk_max_chars <= 0:
        return []

    adapter = EpubAdapter(target_lang=target_lang)
    book = adapter.load(epub_path)

    chunks: list[_TargetChunk] = []
    chapter_index = -1
    current_text: list[str] = []
    current_len = 0
    current_chapter_index = -1

    def _flush() -> None:
        nonlocal current_text, current_len, current_chapter_index
        if not current_text:
            return
        text = "\n\n".join(current_text).strip()
        if text:
            chunks.append(_TargetChunk(text=text, chapter_index=current_chapter_index))
        current_text = []
        current_len = 0

    for chapter_index, doc in enumerate(adapter.iter_chapters(book)):
        if chapter_index >= max_chapters:
            break
        if doc.tree is None:
            continue
        for paragraph in _iter_target_paragraphs(doc.tree, target_lang=target_lang):
            text = paragraph.strip()
            if not text:
                continue
            if current_text and current_len + len(text) > chunk_max_chars:
                _flush()
            if not current_text:
                current_chapter_index = chapter_index
            current_text.append(text)
            current_len += len(text)
    _flush()
    return chunks


def _iter_target_paragraphs(
    tree: object,
    *,
    target_lang: str,
) -> Iterable[str]:
    """Yield the placeholder-stripped prose of every translatable host.

    The Lore Book ingest reads from an *already-translated* ePub. We
    pass ``target_lang=None`` to the walker so the regular adapter's
    "skip nodes already in the target language" filter doesn't drop
    every paragraph (PRD §6.5).
    """

    # Imported lazily because the ePub adapter pulls in lxml — keeping
    # the top of this module light helps test startup time.
    del target_lang
    from epublate.formats.epub import _find_translatable_hosts

    for host in _find_translatable_hosts(tree, target_lang=None):
        text, _ = placeholderize(host)
        cleaned = PLACEHOLDER_RE.sub("", text)
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        if cleaned:
            yield cleaned


__all__ = [
    "DEFAULT_TARGET_CHUNK_MAX_CHARS",
    "DEFAULT_TARGET_MAX_CHAPTERS",
    "PURPOSE_LORE_TARGET_EXTRACT",
    "TargetIngestSummary",
    "ingest_target_epub",
]
