"""``run_batch`` falls back to the project's stored style guide (F-STYLE-2)."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

from epublate.core.batch import BatchOptions, run_batch
from epublate.core.project import Project
from epublate.llm.mock import MockLLMProvider


def _record_system_prompts() -> tuple[MockLLMProvider, list[str]]:
    """Return a mock provider that captures every system prompt it sees."""

    seen: list[str] = []

    def _responder(messages: list, model: str) -> str:
        # First message is always the system prompt; capture it so the test
        # can assert the project's style guide was forwarded.
        seen.append(messages[0].content)
        # Echo the source text back so the placeholder validator passes.
        return json.dumps({"target": messages[-1].content})

    provider = MockLLMProvider()
    provider.set_responder(_responder)
    return provider, seen


def test_run_batch_threads_project_style_guide_into_translator_prompt(
    tiny_epub_factory: Callable[..., Path], tmp_path: Path
) -> None:
    src = tiny_epub_factory(
        chapters=[
            (
                "Solo",
                "<h1>Solo</h1><p>Hello, <em>brave</em> world.</p>",
            )
        ]
    )
    project = Project.create(
        src,
        out_dir=tmp_path / "p",
        source_lang="en",
        target_lang="pt",
        style_profile="children_picture",
    )
    try:
        provider, seen = _record_system_prompts()
        run_batch(
            engine=project.engine,
            project_id=project.project_id,
            source_lang=project.source_lang,
            target_lang=project.target_lang,
            provider=provider,
            options=BatchOptions(model="gpt-mock", group_small_segments=False),
        )
    finally:
        project.close()

    assert seen, "expected at least one translator call"
    # The children-picture preset's prompt block is verbatim in the
    # system message because the batch runner read project.style_guide
    # off the row when options.style_guide was None.
    assert any("young readers" in prompt or "children" in prompt for prompt in seen)


def test_run_batch_explicit_style_guide_overrides_project(
    tiny_epub_factory: Callable[..., Path], tmp_path: Path
) -> None:
    src = tiny_epub_factory()
    project = Project.create(
        src,
        out_dir=tmp_path / "p",
        source_lang="en",
        target_lang="pt",
        style_profile="literary_fiction",
    )
    try:
        provider, seen = _record_system_prompts()
        run_batch(
            engine=project.engine,
            project_id=project.project_id,
            source_lang=project.source_lang,
            target_lang=project.target_lang,
            provider=provider,
            options=BatchOptions(
                model="gpt-mock",
                style_guide="ZZZ-OVERRIDE-MARKER",
                group_small_segments=False,
            ),
        )
    finally:
        project.close()

    assert seen
    assert any("ZZZ-OVERRIDE-MARKER" in prompt for prompt in seen)
