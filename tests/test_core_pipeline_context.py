"""Tests for ``ContextOptions`` and preceding-segment context (PRD §8.1).

The pipeline opt-in lets curators surface the last N preceding
segments from the same chapter to the translator's prompt. The
contract is: a segment is *never* split to fit ``max_chars``, the
oldest exceeders are dropped first, and the rendered block lists the
kept segments oldest-first so the LLM picks up the recency gradient.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

from epublate.core.pipeline import (
    ContextOptions,
    TranslateOptions,
    translate_segment,
)
from epublate.core.project import Project
from epublate.db import repo
from epublate.llm.mock import MockLLMProvider


def _all_segments_in_order(project: Project) -> list[repo.SegmentRow]:
    """Flatten every chapter's segments in book order."""

    out: list[repo.SegmentRow] = []
    for chap in repo.list_chapters(project.engine, project.project_id):
        out.extend(repo.list_segments(project.engine, chap.id))
    return out


def _seed_translation(project: Project, segment: repo.SegmentRow, target: str) -> None:
    """Drop a curator-approved target on a segment via the pipeline.

    Uses the real translator path (mock provider returning a fixed
    target) so the segment ends up with the same ``target_text``
    column the live curator workflow produces. Cheap and gives us a
    realistic preceding-segment shape for the context block test.
    """

    provider = MockLLMProvider()
    provider.set_responder(lambda msgs, model: json.dumps({"target": target}))
    translate_segment(
        engine=project.engine,
        project_id=project.project_id,
        source_lang="en",
        target_lang="pt",
        style_guide=None,
        segment=segment,
        provider=provider,
        options=TranslateOptions(model="gpt-mock"),
    )


def _system_prompt_for(
    project: Project,
    *,
    segment: repo.SegmentRow,
    options: TranslateOptions,
) -> str:
    """Translate one segment with a fresh mock provider, return the system prompt."""

    provider = MockLLMProvider()
    provider.set_responder(
        lambda msgs, model: json.dumps({"target": f"PT::{msgs[-1].content}"})
    )
    translate_segment(
        engine=project.engine,
        project_id=project.project_id,
        source_lang="en",
        target_lang="pt",
        style_guide=None,
        segment=segment,
        provider=provider,
        options=options,
    )
    last = provider.last_request
    assert last is not None
    return last.messages[0].content


def test_context_disabled_by_default(
    tiny_epub_factory: Callable[..., Path], tmp_path: Path
) -> None:
    """No ``context`` arg → no preceding segments in the prompt.

    Existing batches must not see a prompt change when curators don't
    opt in (regression guard against accidentally shipping context to
    every translation, which would burn tokens on every cache miss).
    """

    src = tiny_epub_factory(
        chapters=[
            (
                "Solo",
                "<h1>Solo</h1>"
                "<p>First sentence here.</p>"
                "<p>Second sentence here.</p>"
                "<p>Third sentence here.</p>",
            )
        ]
    )
    project = Project.create(
        src, out_dir=tmp_path / "proj", source_lang="en", target_lang="pt"
    )
    try:
        segs = _all_segments_in_order(project)
        target = next(s for s in segs if s.idx >= 2)
        system = _system_prompt_for(
            project, segment=target, options=TranslateOptions(model="gpt-mock")
        )
        assert "Preceding segments" not in system
    finally:
        project.close()


def test_context_surfaces_preceding_segments_oldest_first(
    tiny_epub_factory: Callable[..., Path], tmp_path: Path
) -> None:
    """Opting in surfaces the earlier segments oldest-first in the prompt.

    Curator workflow: enable ``ContextOptions(max_segments=2)`` and
    translate a segment that has at least two predecessors. The
    rendered block must (a) carry both predecessors and (b) list the
    older one first so the LLM reads them in narrative order.
    """

    src = tiny_epub_factory(
        chapters=[
            (
                "Solo",
                "<h1>Solo</h1>"
                "<p>Alpha sentence one.</p>"
                "<p>Beta sentence two.</p>"
                "<p>Gamma sentence three.</p>"
                "<p>Delta sentence four.</p>",
            )
        ]
    )
    project = Project.create(
        src, out_dir=tmp_path / "proj", source_lang="en", target_lang="pt"
    )
    try:
        segs = _all_segments_in_order(project)
        target_seg = next(s for s in segs if "Delta" in s.source_text)
        beta = next(s for s in segs if "Beta" in s.source_text)
        gamma = next(s for s in segs if "Gamma" in s.source_text)
        _seed_translation(project, beta, "Versão Beta.")
        _seed_translation(project, gamma, "Versão Gamma.")

        system = _system_prompt_for(
            project,
            segment=target_seg,
            options=TranslateOptions(
                model="gpt-mock",
                context=ContextOptions(max_segments=2),
            ),
        )
        assert "Preceding segments" in system
        # Beta is older than Gamma; the prompt must list Beta first.
        beta_idx = system.index("Beta sentence two.")
        gamma_idx = system.index("Gamma sentence three.")
        assert beta_idx < gamma_idx
        # Curator-approved targets surface alongside the source so the
        # model sees the recently-chosen wording.
        assert "Versão Beta." in system
        assert "Versão Gamma." in system
    finally:
        project.close()


def test_context_char_cap_drops_oldest_without_splitting(
    tiny_epub_factory: Callable[..., Path], tmp_path: Path
) -> None:
    """``max_chars`` drops *whole* segments — never splits a segment.

    Build a chapter where the most recent preceding segment alone is
    long enough to dominate the cap and the older one is small. With
    a tight ``max_chars``, the older one survives but the newer one
    is dropped only if its inclusion would push the total past the
    cap. The "never split" rule means we drop entire segments rather
    than truncating one to fit.
    """

    short_text = "Short."
    long_text = "L" + ("o" * 200) + "ng paragraph."
    src = tiny_epub_factory(
        chapters=[
            (
                "Solo",
                "<h1>Solo</h1>"
                f"<p>{short_text}</p>"
                f"<p>{long_text}</p>"
                "<p>Final sentence here.</p>",
            )
        ]
    )
    project = Project.create(
        src, out_dir=tmp_path / "proj", source_lang="en", target_lang="pt"
    )
    try:
        segs = _all_segments_in_order(project)
        target_seg = next(s for s in segs if "Final" in s.source_text)
        # max_segments=2 lets both predecessors be candidates; max_chars
        # is set just under the long paragraph so it must be dropped
        # whole rather than truncated.
        system = _system_prompt_for(
            project,
            segment=target_seg,
            options=TranslateOptions(
                model="gpt-mock",
                context=ContextOptions(max_segments=2, max_chars=50),
            ),
        )
        # The short sibling survives; the long one was skipped.
        assert short_text in system
        assert long_text not in system
        # And the long paragraph was NOT truncated to fit — no prefix
        # of the long string should leak in either.
        assert "Lo" * 5 not in system
    finally:
        project.close()


def test_context_options_enabled_property() -> None:
    assert not ContextOptions().enabled
    assert not ContextOptions(max_segments=0, max_chars=500).enabled
    assert ContextOptions(max_segments=3).enabled
    assert ContextOptions(max_segments=1, max_chars=200).enabled


def test_context_skipped_for_first_segment(
    tiny_epub_factory: Callable[..., Path], tmp_path: Path
) -> None:
    """First segment of a chapter has nothing before it → no block."""

    src = tiny_epub_factory(
        chapters=[
            (
                "Solo",
                "<h1>Solo</h1><p>Only paragraph here.</p>",
            )
        ]
    )
    project = Project.create(
        src, out_dir=tmp_path / "proj", source_lang="en", target_lang="pt"
    )
    try:
        segs = _all_segments_in_order(project)
        first = segs[0]
        system = _system_prompt_for(
            project,
            segment=first,
            options=TranslateOptions(
                model="gpt-mock",
                context=ContextOptions(max_segments=4),
            ),
        )
        assert "Preceding segments" not in system
    finally:
        project.close()
