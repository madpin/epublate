"""Pipeline integration tests for the glossary (PRD §4.2 / M3)."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

from epublate.core.pipeline import TranslateOptions, translate_segment
from epublate.core.project import Project
from epublate.db import repo
from epublate.db.schema import SegmentStatus
from epublate.llm.mock import MockLLMProvider


def _open_translatable_segment(
    project: Project,
) -> tuple[repo.ChapterRow, repo.SegmentRow]:
    chapters = repo.list_chapters(project.engine, project.project_id)
    for chap in chapters:
        for seg in repo.list_segments(project.engine, chap.id):
            return chap, seg
    raise AssertionError("fixture project has no segments")


def _segment_containing(project: Project, needle: str) -> repo.SegmentRow:
    """Pick the first segment whose source text contains ``needle``."""

    for chap in repo.list_chapters(project.engine, project.project_id):
        for seg in repo.list_segments(project.engine, chap.id):
            if needle in seg.source_text:
                return seg
    raise AssertionError(f"no segment contains {needle!r}")


def _basic_project(tiny_factory: Callable[..., Path], tmp_path: Path) -> Project:
    src = tiny_factory(
        chapters=[
            (
                "Solo",
                "<h1>Solo</h1><p>Élise smiled at Hugo.</p><p>Hugo nodded back.</p>",
            )
        ]
    )
    return Project.create(
        src, out_dir=tmp_path / "proj", source_lang="en", target_lang="pt"
    )


def test_locked_violation_flips_status_to_flagged(
    tiny_epub_factory: Callable[..., Path], tmp_path: Path
) -> None:
    project = _basic_project(tiny_epub_factory, tmp_path)
    try:
        repo.create_glossary_entry(
            project.engine,
            project_id=project.project_id,
            source_term="Élise",
            target_term="Elisa",
            type="character",
            status="locked",
        )
        provider = MockLLMProvider()
        # Target deliberately misses the locked target term ``Elisa``.
        provider.set_response(json.dumps({"target": "PT::Elise sorriu para Hugo."}))

        seg = _segment_containing(project, "Élise")
        outcome = translate_segment(
            engine=project.engine,
            project_id=project.project_id,
            source_lang="en",
            target_lang="pt",
            style_guide=None,
            segment=seg,
            provider=provider,
            options=TranslateOptions(model="gpt-mock"),
        )
        assert outcome.flagged is True
        assert any(v.severity == "error" for v in outcome.violations)

        refreshed = repo.get_segment(project.engine, seg.id)
        assert refreshed is not None
        assert refreshed.status == SegmentStatus.FLAGGED
        assert refreshed.target_text is not None  # still persisted

        events = repo.list_events(project.engine, project.project_id)
        assert any(e.kind == "segment.translation_flagged" for e in events)
    finally:
        project.close()


def test_glossary_change_invalidates_cache(
    tiny_epub_factory: Callable[..., Path], tmp_path: Path
) -> None:
    project = _basic_project(tiny_epub_factory, tmp_path)
    try:
        provider = MockLLMProvider()
        provider.set_responder(
            lambda msgs, model: json.dumps({"target": f"PT::{msgs[-1].content}"})
        )

        _chap, seg = _open_translatable_segment(project)
        opts = TranslateOptions(model="gpt-mock")

        first = translate_segment(
            engine=project.engine,
            project_id=project.project_id,
            source_lang="en",
            target_lang="pt",
            style_guide=None,
            segment=seg,
            provider=provider,
            options=opts,
        )
        assert provider.call_count == 1

        # Re-translate without changing anything → cache hit.
        second = translate_segment(
            engine=project.engine,
            project_id=project.project_id,
            source_lang="en",
            target_lang="pt",
            style_guide=None,
            segment=seg,
            provider=provider,
            options=opts,
        )
        assert provider.call_count == 1
        assert second.cache_hit is True
        assert second.cache_key == first.cache_key

        # Insert a glossary entry. The hash should change → next call misses.
        repo.create_glossary_entry(
            project.engine,
            project_id=project.project_id,
            source_term="Hugo",
            target_term="Hugo",
            status="locked",
        )

        third = translate_segment(
            engine=project.engine,
            project_id=project.project_id,
            source_lang="en",
            target_lang="pt",
            style_guide=None,
            segment=seg,
            provider=provider,
            options=opts,
        )
        assert provider.call_count == 2
        assert third.cache_hit is False
        assert third.cache_key != first.cache_key
    finally:
        project.close()


def test_auto_propose_dedupes_new_entities(
    tiny_epub_factory: Callable[..., Path], tmp_path: Path
) -> None:
    project = _basic_project(tiny_epub_factory, tmp_path)
    try:
        provider = MockLLMProvider()
        provider.set_responder(
            lambda msgs, model: json.dumps(
                {
                    "target": f"PT::{msgs[-1].content}",
                    "new_entities": [
                        {
                            "type": "character",
                            "source": "Mira",
                            "evidence": "first appearance",
                        },
                        {"type": "place", "source": "Riverbend"},
                        # Repeated source_term — must be deduped on insert.
                        {"type": "character", "source": "Mira"},
                    ],
                }
            )
        )

        _chap, seg = _open_translatable_segment(project)
        outcome = translate_segment(
            engine=project.engine,
            project_id=project.project_id,
            source_lang="en",
            target_lang="pt",
            style_guide=None,
            segment=seg,
            provider=provider,
            options=TranslateOptions(model="gpt-mock"),
        )
        # First call sees both ``Mira`` and ``Riverbend`` as new.
        assert len(outcome.proposed_entry_ids) == 2

        proposed = repo.list_glossary_entries(
            project.engine, project.project_id, status="proposed"
        )
        assert sorted(p.source_term for p in proposed) == ["Mira", "Riverbend"]
    finally:
        project.close()


def test_pipeline_records_mentions(
    tiny_epub_factory: Callable[..., Path], tmp_path: Path
) -> None:
    project = _basic_project(tiny_epub_factory, tmp_path)
    try:
        # Insert a glossary entry that should match the segment text.
        entry = repo.create_glossary_entry(
            project.engine,
            project_id=project.project_id,
            source_term="Élise",
            target_term="Elisa",
            status="confirmed",
        )
        provider = MockLLMProvider()
        provider.set_response(json.dumps({"target": "PT::Elisa sorriu para Hugo."}))

        seg = _segment_containing(project, "Élise")
        outcome = translate_segment(
            engine=project.engine,
            project_id=project.project_id,
            source_lang="en",
            target_lang="pt",
            style_guide=None,
            segment=seg,
            provider=provider,
            options=TranslateOptions(model="gpt-mock"),
        )
        assert outcome.flagged is False
        mentions = repo.list_mentions(project.engine, segment_id=seg.id)
        entry_ids = {m.entry_id for m in mentions}
        assert entry.id in entry_ids
    finally:
        project.close()


def _glossary_entry_lines(system_prompt: str) -> list[str]:
    """Extract the rendered ``- [type] source → target`` lines.

    The system prompt template has illustrative examples (``e.g.
    House → Câmara``) baked into its hard-rules section. Tests that
    inspect the *rendered* glossary block should look only at the
    bullet lines under it, not arbitrary substrings of the prompt.
    """

    return [
        line for line in system_prompt.splitlines() if line.lstrip().startswith("- [")
    ]


def test_pipeline_filters_glossary_to_segment_matches(
    tiny_epub_factory: Callable[..., Path], tmp_path: Path
) -> None:
    """Only entries whose source term occurs in the segment reach the prompt.

    The prompt used to receive the *whole* project glossary, which
    over-applied common-noun entries (the curator's "House → Câmara"
    bug) and bloated the cache key with irrelevant entries. With the
    per-segment filter, an entry that does NOT match the segment must
    not appear in the system prompt, while the validator still passes
    because no source term means no violation either.
    """

    project = _basic_project(tiny_epub_factory, tmp_path)
    try:
        repo.create_glossary_entry(
            project.engine,
            project_id=project.project_id,
            source_term="Élise",
            target_term="Elisa",
            type="character",
            status="locked",
        )
        # An "irrelevant" entry — common noun mapped to a specialized
        # target. The segment's source text does NOT contain "House",
        # so this entry must NOT show up in the rendered glossary
        # block (the prompt template *quotes* "House → Câmara" as an
        # illustrative example, so this test inspects only the
        # rendered bullet lines).
        repo.create_glossary_entry(
            project.engine,
            project_id=project.project_id,
            source_term="House",
            target_term="Câmara",
            type="organization",
            status="locked",
            notes="parliament chamber",
        )

        captured: dict[str, str] = {}

        def _responder(msgs: list, model: str) -> str:
            captured["system"] = msgs[0].content
            captured["user"] = msgs[-1].content
            return json.dumps({"target": "PT::Elisa sorriu para Hugo."})

        provider = MockLLMProvider()
        provider.set_responder(_responder)

        seg = _segment_containing(project, "Élise")
        translate_segment(
            engine=project.engine,
            project_id=project.project_id,
            source_lang="en",
            target_lang="pt",
            style_guide=None,
            segment=seg,
            provider=provider,
            options=TranslateOptions(model="gpt-mock"),
        )
        bullets = _glossary_entry_lines(captured["system"])
        assert any("Élise → Elisa" in line for line in bullets)
        assert not any("House" in line for line in bullets)
        assert not any("Câmara" in line for line in bullets)
    finally:
        project.close()


def test_pipeline_empty_glossary_block_when_no_entries_match(
    tiny_epub_factory: Callable[..., Path], tmp_path: Path
) -> None:
    """A segment with no glossary matches gets the "(empty)" block.

    Locked + confirmed entries that don't appear in the segment's
    source text are filtered out, so the LLM doesn't waste attention
    on irrelevant constraints. The "(empty for this segment)" marker
    in the prompt template is the cue we look for here.
    """

    project = _basic_project(tiny_epub_factory, tmp_path)
    try:
        repo.create_glossary_entry(
            project.engine,
            project_id=project.project_id,
            source_term="Saturn",
            target_term="Saturno",
            type="place",
            status="locked",
        )

        captured: dict[str, str] = {}

        def _responder(msgs: list, model: str) -> str:
            captured["system"] = msgs[0].content
            return json.dumps({"target": "PT::Hugo concordou."})

        provider = MockLLMProvider()
        provider.set_responder(_responder)

        seg = _segment_containing(project, "Hugo")
        translate_segment(
            engine=project.engine,
            project_id=project.project_id,
            source_lang="en",
            target_lang="pt",
            style_guide=None,
            segment=seg,
            provider=provider,
            options=TranslateOptions(model="gpt-mock"),
        )
        assert "Glossary: (empty for this segment)" in captured["system"]
        assert not any(
            "Saturn" in line for line in _glossary_entry_lines(captured["system"])
        )
    finally:
        project.close()
