"""Tests for the M5 helper-LLM extractor service (PRD §4.2 phase 3 / §7.1)."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from pathlib import Path

import pytest

from epublate.core.extractor import (
    PURPOSE_EXTRACT,
    ExtractOptions,
    IntakeOptions,
    extract_entities,
    run_book_intake,
    run_pre_pass,
)
from epublate.core.project import Project
from epublate.db import repo
from epublate.errors import LLMResponseError
from epublate.llm.base import ResponseFormat
from epublate.llm.mock import MockLLMProvider


def _extractor_response(*entities: dict[str, object], **kwargs: object) -> str:
    """Helper to build a deterministic extractor JSON response."""

    payload: dict[str, object] = {"entities": list(entities)}
    payload.update(kwargs)
    return json.dumps(payload)


@pytest.fixture
def fixture_project(tiny_epub_factory: Callable[..., Path], tmp_path: Path) -> Project:
    src = tiny_epub_factory(
        chapters=[
            (
                "Solo",
                "<h1>Solo</h1>"
                "<p>Élise opened the door of Vale Verde.</p>"
                "<p>The Order of the Coffer watched silently.</p>",
            )
        ]
    )
    project = Project.create(
        src, out_dir=tmp_path / "proj", source_lang="en", target_lang="pt"
    )
    return project


def test_extract_options_defaults_to_json_object_response_format() -> None:
    """The default ``ExtractOptions`` requests JSON mode so reasoning
    helpers (e.g. ``gpt-oss-20b``) don't burn the visible-channel
    budget on reasoning tokens and return empty content.

    The prompt module owns the canonical default; ``ExtractOptions``
    just plumbs it via ``field(default_factory=...)`` so the dataclass
    stays frozen-safe.
    """

    options = ExtractOptions(model="gpt-mock")
    assert options.response_format == ResponseFormat(type="json_object")


def test_extract_entities_forwards_response_format_to_provider(
    fixture_project: Project,
) -> None:
    """``extract_entities`` threads ``options.response_format`` straight
    onto the provider so the underlying chat-completion request gets
    ``{"type": "json_object"}`` (the OpenAI-compat JSON-mode contract).
    """

    project = fixture_project
    try:
        provider = MockLLMProvider()
        provider.set_response(
            _extractor_response(
                {"type": "character", "source": "Élise", "confidence": 0.9},
            )
        )

        extract_entities(
            engine=project.engine,
            project_id=project.project_id,
            source_lang="en",
            target_lang="pt",
            source_text="Élise opened the door.",
            provider=provider,
            options=ExtractOptions(model="gpt-mock"),
        )

        last = provider.last_request
        assert last is not None
        assert last.response_format == ResponseFormat(type="json_object")
    finally:
        project.close()


def test_extract_entities_honors_response_format_override(
    fixture_project: Project,
) -> None:
    """Passing ``response_format=ResponseFormat(type="text")`` lets a
    curator opt out of JSON mode for endpoints that reject it."""

    project = fixture_project
    try:
        provider = MockLLMProvider()
        provider.set_response(_extractor_response())

        extract_entities(
            engine=project.engine,
            project_id=project.project_id,
            source_lang="en",
            target_lang="pt",
            source_text="Élise opened the door.",
            provider=provider,
            options=ExtractOptions(
                model="gpt-mock",
                response_format=ResponseFormat(type="text"),
            ),
        )

        last = provider.last_request
        assert last is not None
        assert last.response_format == ResponseFormat(type="text")
    finally:
        project.close()


def test_extract_entities_recovers_from_endpoint_json_mode_rejection(
    fixture_project: Project,
) -> None:
    """End-to-end smoke test for the soft-fallback path
    (:func:`epublate.llm.json_mode.chat_with_json_fallback`).

    Reproduces the user's bug verbatim: an OpenAI-compatible endpoint
    (Groq via LiteLLM in the wild) hard-fails the first call with
    ``json_validate_failed`` because the reasoning helper exhausted
    its visible-channel budget on reasoning tokens and emitted no
    visible content. The wrapper retries once without
    ``response_format`` and we expect:

    * the second call to succeed with valid JSON,
    * ``extract_entities`` to return the parsed entities,
    * the audit row in ``llm_call`` to record the *successful* call
      (so ``llm_call.response_json`` is populated and the cache key
      is satisfied for future runs).
    """

    project = fixture_project
    try:
        provider = MockLLMProvider()
        attempts = {"n": 0}
        groq_error = (
            "OpenAI API status 400: Error code: 400 - {'error': "
            "{'message': 'litellm.BadRequestError: GroqException - "
            '{"error":{"message":"Failed to validate JSON. Please '
            "adjust your prompt. See 'failed_generation' for more "
            'details.","type":"invalid_request_error","code":'
            '"json_validate_failed","failed_generation":""}}"}}'
        )

        def _responder(messages: object, _model: str) -> str:
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise LLMResponseError(groq_error)
            return _extractor_response(
                {"type": "character", "source": "Élise", "confidence": 0.9},
            )

        provider.set_responder(_responder)

        outcome = extract_entities(
            engine=project.engine,
            project_id=project.project_id,
            source_lang="en",
            target_lang="pt",
            source_text="Élise opened the door of Vale Verde.",
            provider=provider,
            options=ExtractOptions(model="gpt-oss-20b"),
        )

        assert provider.call_count == 2
        assert provider.calls[0].response_format == ResponseFormat(type="json_object")
        assert provider.calls[1].response_format is None
        assert [e.source for e in outcome.trace.entities] == ["Élise"]
        assert outcome.proposed_entry_ids
        with project.engine.connect() as conn:
            row = conn.exec_driver_sql(
                "SELECT response_json FROM llm_call "
                "WHERE project_id = ? AND purpose = ?",
                (project.project_id, PURPOSE_EXTRACT),
            ).one()
        assert row is not None
        assert json.loads(row[0])["content"]
    finally:
        project.close()


def test_extract_entities_persists_audit_and_proposes(
    fixture_project: Project,
) -> None:
    project = fixture_project
    try:
        provider = MockLLMProvider()
        provider.set_response(
            _extractor_response(
                {"type": "character", "source": "Élise", "confidence": 0.9},
                {"type": "place", "source": "Vale Verde", "confidence": 0.8},
                pov="third_limited",
                tense="past",
            )
        )

        outcome = extract_entities(
            engine=project.engine,
            project_id=project.project_id,
            source_lang="en",
            target_lang="pt",
            source_text="Élise opened the door of Vale Verde.",
            provider=provider,
            options=ExtractOptions(model="gpt-mock"),
        )

        assert outcome.cache_hit is False
        assert {ent.source for ent in outcome.trace.entities} == {
            "Élise",
            "Vale Verde",
        }
        assert outcome.trace.pov == "third_limited"
        assert outcome.trace.tense == "past"
        assert len(outcome.proposed_entry_ids) == 2

        calls = repo.list_llm_calls(project.engine, project.project_id)
        extract_calls = [c for c in calls if c.purpose == PURPOSE_EXTRACT]
        assert len(extract_calls) == 1
        assert extract_calls[0].cache_key == outcome.cache_key
        assert extract_calls[0].cache_hit is False

        proposed = repo.list_glossary_entries(
            project.engine, project.project_id, status="proposed"
        )
        sources = {p.source_term for p in proposed}
        assert {"Élise", "Vale Verde"} <= sources
    finally:
        project.close()


def test_extract_entities_cache_hit_skips_provider(fixture_project: Project) -> None:
    """Same prompt + same glossary state must short-circuit the provider.

    ``auto_propose`` is disabled so the glossary state hash stays
    identical between calls; the cache key folds the glossary hash by
    design (PRD F-LLM-6), so a real auto-propose run would invalidate
    the cache. That's covered separately by the bypass-cache test.
    """

    project = fixture_project
    try:
        provider = MockLLMProvider()
        provider.set_response(
            _extractor_response(
                {"type": "character", "source": "Élise", "confidence": 0.9}
            )
        )

        first = extract_entities(
            engine=project.engine,
            project_id=project.project_id,
            source_lang="en",
            target_lang="pt",
            source_text="Élise opened the door.",
            provider=provider,
            options=ExtractOptions(model="gpt-mock", auto_propose=False),
        )
        assert first.cache_hit is False
        assert provider.call_count == 1

        second = extract_entities(
            engine=project.engine,
            project_id=project.project_id,
            source_lang="en",
            target_lang="pt",
            source_text="Élise opened the door.",
            provider=provider,
            options=ExtractOptions(model="gpt-mock", auto_propose=False),
        )
        assert second.cache_hit is True
        assert second.cache_key == first.cache_key
        assert provider.call_count == 1, "cache hit must not call the provider"

        calls = repo.list_llm_calls(project.engine, project.project_id)
        extract_calls = [c for c in calls if c.purpose == PURPOSE_EXTRACT]
        assert len(extract_calls) == 2
        assert any(c.cache_hit for c in extract_calls)
        assert not all(c.cache_hit for c in extract_calls)
    finally:
        project.close()


def test_extract_entities_dedupes_existing_proposed(
    fixture_project: Project,
) -> None:
    project = fixture_project
    try:
        repo.create_glossary_entry(
            project.engine,
            project_id=project.project_id,
            source_term="Élise",
            target_term="Élise",
            type="character",
            status="proposed",
        )

        provider = MockLLMProvider()
        provider.set_response(
            _extractor_response(
                {"type": "character", "source": "Élise"},
                {"type": "place", "source": "Vale Verde"},
            )
        )

        outcome = extract_entities(
            engine=project.engine,
            project_id=project.project_id,
            source_lang="en",
            target_lang="pt",
            source_text="Élise opened the door of Vale Verde.",
            provider=provider,
            options=ExtractOptions(model="gpt-mock"),
        )

        # Only Vale Verde is new; Élise already had a proposed row.
        assert len(outcome.proposed_entry_ids) == 1
    finally:
        project.close()


def test_extract_entities_records_failed_call_and_event(
    fixture_project: Project,
) -> None:
    project = fixture_project
    try:
        provider = MockLLMProvider()
        provider.set_response("not even close to JSON")

        with pytest.raises(LLMResponseError):
            extract_entities(
                engine=project.engine,
                project_id=project.project_id,
                source_lang="en",
                target_lang="pt",
                source_text="Élise opened the door.",
                provider=provider,
                options=ExtractOptions(model="gpt-mock"),
            )

        calls = repo.list_llm_calls(project.engine, project.project_id)
        extract_calls = [c for c in calls if c.purpose == PURPOSE_EXTRACT]
        assert len(extract_calls) == 1
        assert extract_calls[0].cache_hit is False

        events = [
            ev
            for ev in repo.list_events(project.engine, project.project_id)
            if ev.kind == "entity.extract_failed"
        ]
        assert len(events) == 1
    finally:
        project.close()


def test_extract_entities_bypass_cache_creates_new_row(
    fixture_project: Project,
) -> None:
    project = fixture_project
    try:
        provider = MockLLMProvider()
        provider.queue_responses(
            [
                _extractor_response({"source": "Élise"}),
                _extractor_response({"source": "Élise"}),
            ]
        )

        first = extract_entities(
            engine=project.engine,
            project_id=project.project_id,
            source_lang="en",
            target_lang="pt",
            source_text="Élise opened the door.",
            provider=provider,
            options=ExtractOptions(model="gpt-mock"),
        )
        second = extract_entities(
            engine=project.engine,
            project_id=project.project_id,
            source_lang="en",
            target_lang="pt",
            source_text="Élise opened the door.",
            provider=provider,
            options=ExtractOptions(model="gpt-mock", bypass_cache=True),
        )

        assert first.cache_key != second.cache_key
        assert provider.call_count == 2
    finally:
        project.close()


def test_run_book_intake_processes_chunks_and_emits_event(
    fixture_project: Project,
) -> None:
    project = fixture_project
    try:
        provider = MockLLMProvider()
        provider.set_responder(
            lambda msgs, model: _extractor_response(
                {"type": "character", "source": "Élise"},
                {"type": "place", "source": "Vale Verde"},
                pov="third_limited",
            )
        )

        summary = run_book_intake(
            engine=project.engine,
            project_id=project.project_id,
            source_lang="en",
            target_lang="pt",
            provider=provider,
            options=IntakeOptions(model="gpt-mock", max_segments=5),
        )

        assert summary.chunks >= 1
        assert summary.proposed_count >= 2
        assert summary.pov == "third_limited"
        assert summary.failed_chunks == 0

        kinds = [ev.kind for ev in repo.list_events(project.engine, project.project_id)]
        assert "intake.started" in kinds
        assert "intake.completed" in kinds
    finally:
        project.close()


def test_run_book_intake_zero_segments_emits_completed(
    fixture_project: Project,
) -> None:
    project = fixture_project
    try:
        provider = MockLLMProvider()
        summary = run_book_intake(
            engine=project.engine,
            project_id=project.project_id,
            source_lang="en",
            target_lang="pt",
            provider=provider,
            options=IntakeOptions(model="gpt-mock", max_segments=0),
        )
        assert summary.chunks == 0
        assert provider.call_count == 0
        kinds = [ev.kind for ev in repo.list_events(project.engine, project.project_id)]
        assert "intake.started" in kinds
        assert "intake.completed" in kinds
    finally:
        project.close()


def test_run_pre_pass_trips_circuit_breaker_after_consecutive_failures(
    tiny_epub_factory: Callable[..., Path],
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Endpoint-broken scenario: when every helper call raises, the
    pre-pass loop must abort after ``failure_streak_limit`` consecutive
    failures so it doesn't burn through the rest of the chapter on a
    fundamentally broken endpoint (the original bug: ``gpt-oss-20b``
    on Groq returning empty visible content for every chunk because
    reasoning tokens consume the entire visible-channel budget, even
    *after* the JSON-mode soft-fallback has retried without
    ``response_format``).

    Asserts:

    * the provider sees exactly ``failure_streak_limit`` calls (not
      one per chunk);
    * the loop's ``failed_chunks`` total reflects every chunk in scope
      (the processed-and-failed ones plus the skipped ones), so the
      summary is honest about what didn't run;
    * a ``batch.pre_pass_aborted`` event is emitted with the truncated
      last-error string;
    * the warning log line names the most likely root cause and a
      one-step fix (switching helper models).
    """

    paragraphs = "".join(f"<p>Paragraph number {i} of the loop.</p>" for i in range(8))
    src = tiny_epub_factory(chapters=[("Loop test", "<h1>Loop test</h1>" + paragraphs)])
    project = Project.create(
        src, out_dir=tmp_path / "loop", source_lang="en", target_lang="pt"
    )
    try:
        # Pick the chapter with the most translatable segments (the
        # TOC chapter is also stored, with ``kind='pair'`` rows we
        # don't want to feed to the helper).
        all_chapters = repo.list_chapters(project.engine, project.project_id)
        per_chapter = [
            (c, repo.list_segments(project.engine, c.id)) for c in all_chapters
        ]
        _chapter, segments = max(per_chapter, key=lambda pair: len(pair[1]))
        assert len(segments) >= 4

        provider = MockLLMProvider()

        def _always_empty(_messages: object, _model: str) -> str:
            # Exact failure shape from the user's bug: helper returns
            # empty visible content. The parser raises
            # ``extractor response was empty`` for every chunk.
            return ""

        provider.set_responder(_always_empty)

        with caplog.at_level(logging.WARNING, logger="epublate.core.extractor"):
            summary = run_pre_pass(
                engine=project.engine,
                project_id=project.project_id,
                source_lang="en",
                target_lang="pt",
                provider=provider,
                options=IntakeOptions(
                    model="gpt-oss-20b",
                    chunk_max_tokens=5,
                    failure_streak_limit=2,
                ),
                segments=segments,
            )

        assert provider.call_count == 2
        assert summary.failed_chunks >= 2
        assert summary.failed_chunks >= len(segments) // 2
        kinds = [ev.kind for ev in repo.list_events(project.engine, project.project_id)]
        assert "batch.pre_pass_aborted" in kinds
        assert "batch.pre_pass_completed" not in kinds
        aborted = next(
            ev
            for ev in repo.list_events(project.engine, project.project_id)
            if ev.kind == "batch.pre_pass_aborted"
        )
        assert aborted.payload["failure_streak"] == 2
        assert "empty" in aborted.payload["last_error"]
        warnings = [
            r.getMessage()
            for r in caplog.records
            if "pre-pass aborted" in r.getMessage()
        ]
        assert len(warnings) == 1
        assert "non-reasoning helper" in warnings[0]
        assert "$EPUBLATE_LLM_HELPER_MODEL" in warnings[0]
    finally:
        project.close()


def test_run_pre_pass_breaker_streak_resets_on_success(
    tiny_epub_factory: Callable[..., Path],
    tmp_path: Path,
) -> None:
    """A successful chunk in between two failure runs resets the
    streak so a flaky endpoint doesn't trip the breaker prematurely."""

    paragraphs = "".join(f"<p>Paragraph {i} of the mixed run.</p>" for i in range(6))
    src = tiny_epub_factory(chapters=[("Mixed", "<h1>Mixed</h1>" + paragraphs)])
    project = Project.create(
        src, out_dir=tmp_path / "mixed", source_lang="en", target_lang="pt"
    )
    try:
        all_chapters = repo.list_chapters(project.engine, project.project_id)
        per_chapter = [
            (c, repo.list_segments(project.engine, c.id)) for c in all_chapters
        ]
        _chapter, segments = max(per_chapter, key=lambda pair: len(pair[1]))

        provider = MockLLMProvider()
        # fail, fail, success, fail, fail, fail → trips on the second 3-streak
        provider.queue_responses(
            [
                "",
                "",
                _extractor_response({"type": "place", "source": "Vale Verde"}),
                "",
                "",
                "",
            ]
        )

        summary = run_pre_pass(
            engine=project.engine,
            project_id=project.project_id,
            source_lang="en",
            target_lang="pt",
            provider=provider,
            options=IntakeOptions(
                model="gpt-mock",
                chunk_max_tokens=5,
                failure_streak_limit=3,
            ),
            segments=segments,
        )

        # 6 calls: two fail, one succeeds (streak resets), three fail
        # in a row → breaker trips after the third post-reset failure.
        assert provider.call_count == 6
        assert summary.chunks == 1
        assert summary.proposed_count >= 1
        kinds = [ev.kind for ev in repo.list_events(project.engine, project.project_id)]
        assert "batch.pre_pass_aborted" in kinds
    finally:
        project.close()


def test_run_pre_pass_breaker_disabled_when_limit_zero(
    fixture_project: Project,
) -> None:
    """Setting ``failure_streak_limit=0`` reverts to legacy "best-effort,
    never abort" semantics — useful for tests that need every audit row
    even when every chunk fails by design."""

    project = fixture_project
    try:
        all_chapters = repo.list_chapters(project.engine, project.project_id)
        per_chapter = [
            (c, repo.list_segments(project.engine, c.id)) for c in all_chapters
        ]
        _chapter, segments = max(per_chapter, key=lambda pair: len(pair[1]))

        provider = MockLLMProvider()

        def _always_empty(_messages: object, _model: str) -> str:
            return ""

        provider.set_responder(_always_empty)

        summary = run_pre_pass(
            engine=project.engine,
            project_id=project.project_id,
            source_lang="en",
            target_lang="pt",
            provider=provider,
            options=IntakeOptions(
                model="gpt-mock",
                chunk_max_tokens=5,
                failure_streak_limit=0,
            ),
            segments=segments,
        )

        # Every segment got its own call; no abort.
        assert provider.call_count == len(segments)
        assert summary.failed_chunks == len(segments)
        kinds = [ev.kind for ev in repo.list_events(project.engine, project.project_id)]
        assert "batch.pre_pass_completed" in kinds
        assert "batch.pre_pass_aborted" not in kinds
    finally:
        project.close()


def test_run_book_intake_trips_circuit_breaker_after_consecutive_failures(
    tiny_epub_factory: Callable[..., Path],
    tmp_path: Path,
) -> None:
    """``run_book_intake`` mirrors ``run_pre_pass`` for the breaker —
    same root cause (broken helper endpoint) needs the same protection
    on the New-Project intake path so a curator with a bad helper
    model (``$EPUBLATE_LLM_HELPER_MODEL`` or the project override)
    doesn't burn the entire intake budget."""

    paragraphs = "".join(f"<p>Paragraph {i} of the intake.</p>" for i in range(8))
    src = tiny_epub_factory(
        chapters=[("Intake loop", "<h1>Intake loop</h1>" + paragraphs)]
    )
    project = Project.create(
        src, out_dir=tmp_path / "intake-loop", source_lang="en", target_lang="pt"
    )
    try:
        provider = MockLLMProvider()

        def _always_empty(_messages: object, _model: str) -> str:
            return ""

        provider.set_responder(_always_empty)

        summary = run_book_intake(
            engine=project.engine,
            project_id=project.project_id,
            source_lang="en",
            target_lang="pt",
            provider=provider,
            options=IntakeOptions(
                model="gpt-oss-20b",
                max_segments=20,
                chunk_max_tokens=5,
                failure_streak_limit=2,
            ),
        )

        assert provider.call_count == 2
        assert summary.failed_chunks >= 2
        kinds = [ev.kind for ev in repo.list_events(project.engine, project.project_id)]
        assert "intake.aborted" in kinds
        assert "intake.completed" not in kinds
    finally:
        project.close()


def test_run_book_intake_continues_after_chunk_failure(
    fixture_project: Project,
) -> None:
    project = fixture_project
    try:
        provider = MockLLMProvider()
        provider.queue_responses(
            [
                "garbage",
                _extractor_response({"source": "Élise"}),
            ]
        )

        summary = run_book_intake(
            engine=project.engine,
            project_id=project.project_id,
            source_lang="en",
            target_lang="pt",
            provider=provider,
            options=IntakeOptions(
                model="gpt-mock",
                max_segments=10,
                # Tiny budget forces multiple chunks so we definitely have
                # at least two helper calls — the first fails to parse.
                chunk_max_tokens=5,
            ),
        )

        assert summary.failed_chunks >= 1
        # The successful chunk(s) should still have proposed entries.
        kinds = [ev.kind for ev in repo.list_events(project.engine, project.project_id)]
        assert "intake.completed" in kinds
        assert "entity.extract_failed" in kinds
    finally:
        project.close()


def test_run_pre_pass_seeds_proposed_for_chapter(
    fixture_project: Project,
) -> None:
    project = fixture_project
    try:
        chapters = repo.list_chapters(project.engine, project.project_id)
        segments = repo.list_segments(project.engine, chapters[0].id)

        provider = MockLLMProvider()
        provider.set_response(
            _extractor_response({"type": "place", "source": "Vale Verde"})
        )

        summary = run_pre_pass(
            engine=project.engine,
            project_id=project.project_id,
            source_lang="en",
            target_lang="pt",
            provider=provider,
            options=IntakeOptions(model="gpt-mock"),
            segments=segments,
        )

        assert summary.chunks >= 1
        assert summary.proposed_count >= 1

        kinds = [ev.kind for ev in repo.list_events(project.engine, project.project_id)]
        assert "batch.pre_pass_started" in kinds
        assert "batch.pre_pass_completed" in kinds

        proposed = repo.list_glossary_entries(
            project.engine, project.project_id, status="proposed"
        )
        assert any(p.source_term == "Vale Verde" for p in proposed)
    finally:
        project.close()


def test_run_pre_pass_no_segments_short_circuits(
    fixture_project: Project,
) -> None:
    project = fixture_project
    try:
        provider = MockLLMProvider()
        summary = run_pre_pass(
            engine=project.engine,
            project_id=project.project_id,
            source_lang="en",
            target_lang="pt",
            provider=provider,
            options=IntakeOptions(model="gpt-mock"),
            segments=[],
        )
        assert summary.chunks == 0
        assert provider.call_count == 0
        kinds = [ev.kind for ev in repo.list_events(project.engine, project.project_id)]
        assert "batch.pre_pass_completed" in kinds
    finally:
        project.close()


def test_run_book_intake_aggregates_register_and_suggests_profile(
    fixture_project: Project,
) -> None:
    """PRD F-STYLE-3: helper observations are surfaced + a tone is suggested.

    A single chunk that reports ``audience=children`` must end up with
    ``IntakeSummary.suggested_style_profile == 'children_picture'`` so
    the dashboard can offer a one-click "Apply suggestion" later.
    """

    project = fixture_project
    try:
        provider = MockLLMProvider()
        provider.set_response(
            _extractor_response(
                {"type": "place", "source": "Vale Verde"},
                pov="third_limited",
                register="literary",
                audience="children",
            )
        )

        summary = run_book_intake(
            engine=project.engine,
            project_id=project.project_id,
            source_lang="en",
            target_lang="pt",
            provider=provider,
            options=IntakeOptions(model="gpt-mock", max_segments=5),
        )

        assert summary.register == "literary"
        assert summary.audience == "children"
        assert summary.suggested_style_profile == "children_picture"

        completed = [
            ev
            for ev in repo.list_events(project.engine, project.project_id)
            if ev.kind == "intake.completed"
        ]
        assert completed
        payload = completed[-1].payload
        assert payload["register"] == "literary"
        assert payload["audience"] == "children"
        assert payload["suggested_style_profile"] == "children_picture"
    finally:
        project.close()


def test_run_book_intake_keeps_first_observation_when_chunks_disagree(
    fixture_project: Project,
) -> None:
    """First non-empty register/audience wins (mirrors pov / tense)."""

    project = fixture_project
    try:
        provider = MockLLMProvider()
        provider.queue_responses(
            [
                _extractor_response(
                    {"source": "Élise"},
                    register="literary",
                    audience="adult",
                ),
                _extractor_response(
                    {"source": "Coffer"},
                    register="technical",
                    audience="children",
                ),
            ]
        )

        summary = run_book_intake(
            engine=project.engine,
            project_id=project.project_id,
            source_lang="en",
            target_lang="pt",
            provider=provider,
            options=IntakeOptions(
                model="gpt-mock",
                max_segments=10,
                # Tiny budget forces multiple chunks.
                chunk_max_tokens=5,
            ),
        )

        assert summary.register == "literary"
        assert summary.audience == "adult"
        assert summary.suggested_style_profile == "literary_fiction"
    finally:
        project.close()


def test_run_book_intake_no_observations_yields_no_suggestion(
    fixture_project: Project,
) -> None:
    """Helper omits register/audience → suggester returns ``None``."""

    project = fixture_project
    try:
        provider = MockLLMProvider()
        provider.set_response(_extractor_response({"source": "Élise"}))
        summary = run_book_intake(
            engine=project.engine,
            project_id=project.project_id,
            source_lang="en",
            target_lang="pt",
            provider=provider,
            options=IntakeOptions(model="gpt-mock", max_segments=5),
        )
        assert summary.register is None
        assert summary.audience is None
        assert summary.suggested_style_profile is None
    finally:
        project.close()


def test_run_pre_pass_propagates_register_and_audience(
    fixture_project: Project,
) -> None:
    project = fixture_project
    try:
        chapters = repo.list_chapters(project.engine, project.project_id)
        segments = repo.list_segments(project.engine, chapters[0].id)

        provider = MockLLMProvider()
        provider.set_response(
            _extractor_response(
                {"type": "place", "source": "Vale Verde"},
                register="genre",
                audience="adult",
            )
        )

        summary = run_pre_pass(
            engine=project.engine,
            project_id=project.project_id,
            source_lang="en",
            target_lang="pt",
            provider=provider,
            options=IntakeOptions(model="gpt-mock"),
            segments=segments,
        )

        assert summary.register == "genre"
        assert summary.audience == "adult"
        assert summary.suggested_style_profile == "genre_fiction"
    finally:
        project.close()


def test_extract_entities_records_register_and_audience_in_event(
    fixture_project: Project,
) -> None:
    """The ``entity.extracted`` event payload must carry the new tags.

    Inbox / dashboard surfaces consume these payloads; if the extractor
    forgets to forward them, downstream UI never learns the helper's
    observation even when the trace itself is fine.
    """

    project = fixture_project
    try:
        provider = MockLLMProvider()
        provider.set_response(
            _extractor_response(
                {"source": "Élise"},
                register="literary",
                audience="adult",
            )
        )
        extract_entities(
            engine=project.engine,
            project_id=project.project_id,
            source_lang="en",
            target_lang="pt",
            source_text="Élise opened the door.",
            provider=provider,
            options=ExtractOptions(model="gpt-mock"),
        )
        events = [
            ev
            for ev in repo.list_events(project.engine, project.project_id)
            if ev.kind == "entity.extracted"
        ]
        assert events
        payload = events[-1].payload
        assert payload["register"] == "literary"
        assert payload["audience"] == "adult"
    finally:
        project.close()
