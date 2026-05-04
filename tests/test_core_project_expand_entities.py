"""Tests for :meth:`Project.expand_typographic_entities` (in-place migration).

Before the typographic-entity-expansion fix, the segmenter encoded
``&rsquo;`` / ``&nbsp;`` / ``&hellip;`` / curly-quote / dash entity
references as ``entity`` skeleton tokens plus ``[[Tk]]`` placeholders
in the segment's ``source_text``. The translator prompt then asked
the LLM to drop the underlying typographic character when going to a
target language that does not use it (French elision apostrophes →
no apostrophe in Portuguese), so the model dropped the placeholder
along with it and the structural validator hard-failed
``"entity placeholder [[Tk]] missing or duplicated"``.

The new segmenter expands those entities inline. This migration is
the in-place equivalent for projects already segmented with the old
behavior — it walks every stored segment and rewrites ``source_text``
/ ``source_hash`` / ``target_text`` / ``inline_skeleton`` together.
These tests cover:

* the pure rewrite function (``expand_text_entity_placeholders``);
* idempotency on already-migrated projects;
* preservation of curator-touched fields (``status``, target text);
* the per-chapter audit event so a future Logs screen can surface it.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from pathlib import Path

from epublate.core.project import Project
from epublate.core.segmentation import expand_text_entity_placeholders
from epublate.db import repo, schema
from epublate.formats.base import InlineToken


def _entity_token(name: str) -> InlineToken:
    return InlineToken(tag=f"&{name};", kind="entity", attrs={"name": name})


def _pair_token(tag: str) -> InlineToken:
    return InlineToken(tag=tag, kind="pair", attrs={})


# ---------------------------------------------------------------------------
# Pure function — :func:`expand_text_entity_placeholders`
# ---------------------------------------------------------------------------


def test_pure_rewrite_returns_unchanged_when_no_text_entities() -> None:
    """Skeletons without text-entity tokens are returned as-is."""

    skeleton = [_pair_token("em"), _entity_token("copy")]  # ``&copy;`` not in whitelist
    text = "before [[T0]]bold[[/T0]] after [[T1]] end"
    new_source, new_target, new_skel, changed = expand_text_entity_placeholders(
        source_text=text,
        target_text=None,
        skeleton=skeleton,
    )
    assert changed is False
    assert new_source == text
    assert new_target is None
    assert new_skel == skeleton


def test_pure_rewrite_expands_entities_and_renumbers_remaining_placeholders() -> None:
    """A skeleton with mixed entity + pair tokens collapses correctly.

    Layout: rsquo + pair(em) + nbsp + pair(strong)
       indices: 0      1         2     3
    Source: ``j[[T0]]ai [[T1]]un[[/T1]] mot[[T2]]: [[T3]]ok[[/T3]]``
    Expected after rewrite: rsquo and nbsp expanded inline, pair tokens
    renumbered to 0 and 1, skeleton trimmed to [pair em, pair strong].
    """

    skeleton = [
        _entity_token("rsquo"),
        _pair_token("em"),
        _entity_token("nbsp"),
        _pair_token("strong"),
    ]
    source = "j[[T0]]ai [[T1]]un[[/T1]] mot[[T2]]: [[T3]]ok[[/T3]]"
    new_source, new_target, new_skel, changed = expand_text_entity_placeholders(
        source_text=source,
        target_text=None,
        skeleton=skeleton,
    )
    assert changed is True
    assert new_source == "j\u2019ai [[T0]]un[[/T0]] mot\u00a0: [[T1]]ok[[/T1]]"
    assert new_target is None
    assert [t.tag for t in new_skel] == ["em", "strong"]
    assert all(t.kind == "pair" for t in new_skel)


def test_pure_rewrite_applies_substitution_to_target_text_in_lockstep() -> None:
    """Target text gets the same entity → char + renumber treatment."""

    skeleton = [_entity_token("rsquo"), _pair_token("em")]
    source = "j[[T0]]ai dit [[T1]]bonjour[[/T1]]"
    target = "Eu [[T0]]disse [[T1]]olá[[/T1]]"  # LLM kept the apostrophe placeholder
    new_source, new_target, new_skel, changed = expand_text_entity_placeholders(
        source_text=source,
        target_text=target,
        skeleton=skeleton,
    )
    assert changed is True
    assert new_source == "j\u2019ai dit [[T0]]bonjour[[/T0]]"
    assert new_target == "Eu \u2019disse [[T0]]olá[[/T0]]"
    assert [t.tag for t in new_skel] == ["em"]


def test_pure_rewrite_handles_target_with_dropped_placeholders() -> None:
    """When the LLM dropped an entity placeholder (the bug case), rewriting works.

    The LLM's translation legitimately dropped the ``[[T0]]``
    apostrophe placeholder when emitting Portuguese (``Tenho`` not
    ``Eu'tenho``). After the migration the skeleton no longer has an
    entity at index 0, so the absence of ``[[T0]]`` in the target is
    no longer a violation.
    """

    skeleton = [_entity_token("rsquo"), _pair_token("em")]
    source = "J[[T0]]ai une excuse [[T1]]sérieuse[[/T1]]"
    target = "Tenho uma desculpa [[T1]]séria[[/T1]]"  # missing [[T0]]
    new_source, new_target, new_skel, changed = expand_text_entity_placeholders(
        source_text=source,
        target_text=target,
        skeleton=skeleton,
    )
    assert changed is True
    assert new_source == "J\u2019ai une excuse [[T0]]sérieuse[[/T0]]"
    assert new_target == "Tenho uma desculpa [[T0]]séria[[/T0]]"
    assert [t.tag for t in new_skel] == ["em"]


def test_pure_rewrite_petit_prince_dedication_shape() -> None:
    """The original failing segment's shape collapses to a placeholder-free row.

    Reproduction of segment ``33e26094…`` from the curator-reported
    ``petitprince`` project: 11 ``&rsquo;`` + 4 ``&nbsp;`` entity
    tokens turn into 15 ``[[Tk]]`` placeholders. After migration, the
    skeleton is empty and the source carries literal U+2019 / U+00A0
    characters where the entities used to be.
    """

    skeleton = [
        _entity_token("rsquo"),  # 0  d'avoir
        _entity_token("rsquo"),  # 1  J'ai
        _entity_token("nbsp"),  # 2  sérieuse:
        _entity_token("rsquo"),  # 3  j'ai
    ]
    source = (
        "Je demande pardon aux enfants d[[T0]]avoir dédié ce livre. "
        "J[[T1]]ai une excuse sérieuse[[T2]]: cette grande personne "
        "est le meilleur ami que j[[T3]]ai au monde."
    )
    new_source, _new_target, new_skel, changed = expand_text_entity_placeholders(
        source_text=source,
        target_text=None,
        skeleton=skeleton,
    )
    assert changed is True
    assert new_skel == []
    assert "[[T" not in new_source
    assert "d\u2019avoir" in new_source
    assert "J\u2019ai" in new_source
    assert "j\u2019ai" in new_source
    assert "sérieuse\u00a0:" in new_source


# ---------------------------------------------------------------------------
# Integration — :meth:`Project.expand_typographic_entities`
# ---------------------------------------------------------------------------


def _force_old_style_segment(
    project: Project,
    *,
    chapter_id: str,
    skeleton: list[InlineToken],
    source_text: str,
    target_text: str | None,
    status: str,
) -> str:
    """Replace one segment row with a hand-crafted old-style payload.

    Used to simulate segments that were authored before the
    typographic-entity-expansion fix landed. We update an existing
    row (rather than insert a new one) so we don't have to invent a
    fresh ``idx`` / ``host_path`` and risk colliding with the
    chapter's existing segments.
    """

    rows = repo.list_segments(project.engine, chapter_id)
    assert rows, f"chapter {chapter_id} has no existing segments to mutate"
    target_row = rows[0]

    new_hash = hashlib.sha256(source_text.encode("utf-8")).hexdigest()
    repo.rewrite_segment_source(
        project.engine,
        segment_id=target_row.id,
        source_text=source_text,
        source_hash=new_hash,
        target_text=target_text,
        skeleton=skeleton,
        host_path=target_row.host_path,
        host_part=target_row.host_part,
        host_total_parts=target_row.host_total_parts,
    )
    if target_text is not None:
        repo.update_segment_translation(
            project.engine,
            segment_id=target_row.id,
            target_text=target_text,
            status=status,
        )
    else:
        repo.update_segment_status(
            project.engine, segment_id=target_row.id, status=status
        )
    return target_row.id


def test_expand_entities_is_noop_on_fresh_project(
    tiny_epub_factory: Callable[..., Path], tmp_path: Path
) -> None:
    """A project segmented with the new behavior has nothing to rewrite."""

    src = tiny_epub_factory()
    project = Project.create(
        src, out_dir=tmp_path / "proj", source_lang="en", target_lang="pt"
    )
    try:
        summary = project.expand_typographic_entities()
        assert summary.rewritten == 0
        assert summary.chapters == []
    finally:
        project.close()


def test_expand_entities_rewrites_old_style_segment_preserving_translation(
    tiny_epub_factory: Callable[..., Path], tmp_path: Path
) -> None:
    """Pre-fix segments get rewritten without losing the translated target."""

    src = tiny_epub_factory()
    project = Project.create(
        src, out_dir=tmp_path / "proj", source_lang="fr", target_lang="pt"
    )
    try:
        chapters = [
            c
            for c in repo.list_chapters(project.engine, project.project_id)
            if c.href != "nav.xhtml"
        ]
        chapter_id = chapters[0].id
        seg_id = _force_old_style_segment(
            project,
            chapter_id=chapter_id,
            skeleton=[_entity_token("rsquo"), _pair_token("em")],
            source_text="J[[T0]]ai une excuse [[T1]]sérieuse[[/T1]]",
            target_text="Tenho uma desculpa [[T1]]séria[[/T1]]",
            status=schema.SegmentStatus.FLAGGED,
        )

        summary = project.expand_typographic_entities()

        assert summary.rewritten >= 1
        assert any(o.chapter_id == chapter_id for o in summary.chapters)

        rewritten = repo.get_segment(project.engine, seg_id)
        assert rewritten is not None
        assert rewritten.source_text == "J\u2019ai une excuse [[T0]]sérieuse[[/T0]]"
        assert rewritten.target_text == "Tenho uma desculpa [[T0]]séria[[/T0]]"
        # Status MUST survive the migration — it reflects a curator
        # decision (this segment was flagged) and a pure-text rewrite
        # is not supposed to touch that.
        assert rewritten.status == schema.SegmentStatus.FLAGGED
        assert [t.tag for t in rewritten.inline_skeleton] == ["em"]
        # source_hash must follow the new source_text.
        assert (
            rewritten.source_hash
            == hashlib.sha256(rewritten.source_text.encode("utf-8")).hexdigest()
        )
    finally:
        project.close()


def test_expand_entities_is_idempotent(
    tiny_epub_factory: Callable[..., Path], tmp_path: Path
) -> None:
    """Running the migration twice in a row leaves the second pass empty."""

    src = tiny_epub_factory()
    project = Project.create(
        src, out_dir=tmp_path / "proj", source_lang="fr", target_lang="pt"
    )
    try:
        chapters = [
            c
            for c in repo.list_chapters(project.engine, project.project_id)
            if c.href != "nav.xhtml"
        ]
        _force_old_style_segment(
            project,
            chapter_id=chapters[0].id,
            skeleton=[_entity_token("nbsp")],
            source_text="hello[[T0]]world",
            target_text=None,
            status=schema.SegmentStatus.PENDING,
        )

        first = project.expand_typographic_entities()
        assert first.rewritten == 1

        second = project.expand_typographic_entities()
        assert second.rewritten == 0
        assert second.chapters == []
    finally:
        project.close()


def test_expand_entities_records_per_chapter_event(
    tiny_epub_factory: Callable[..., Path], tmp_path: Path
) -> None:
    """The migration appends a ``chapter.entities_expanded`` event row.

    Lets the Logs screen surface the migration in the project's
    activity feed (PRD §7.6 audit-trail invariant).
    """

    src = tiny_epub_factory()
    project = Project.create(
        src, out_dir=tmp_path / "proj", source_lang="fr", target_lang="pt"
    )
    try:
        chapters = [
            c
            for c in repo.list_chapters(project.engine, project.project_id)
            if c.href != "nav.xhtml"
        ]
        chapter_id = chapters[0].id
        _force_old_style_segment(
            project,
            chapter_id=chapter_id,
            skeleton=[_entity_token("rsquo")],
            source_text="hi[[T0]]ya",
            target_text=None,
            status=schema.SegmentStatus.PENDING,
        )

        project.expand_typographic_entities()

        events = repo.list_events(project.engine, project.project_id)
        ee = [e for e in events if e.kind == "chapter.entities_expanded"]
        assert len(ee) == 1
        payload = ee[0].payload
        assert payload["chapter_id"] == chapter_id
        assert payload["rewritten"] == 1
    finally:
        project.close()
