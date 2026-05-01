"""Pre-create tone sniff (PRD F-STYLE-4).

The sniff reads a small **spread** of translatable blocks from an ePub
*without* persisting anything (no project row, no segments, no
``llm_call`` audit) and asks the helper LLM the same
``register`` / ``audience`` question we ask during intake. The result
plugs into :func:`epublate.core.style.suggest_style_profile` so the
New Project modal can pre-select a tone preset before the curator
hits Create.

This module is intentionally side-effect-free at the storage layer:

* No DB writes — :class:`~epublate.formats.epub.EpubAdapter.load`
  returns an in-memory book; we never call :func:`Project.create`.
* No cache lookup — the curator picked "no cache" in the M7.1 design
  call so the helper is always live. (Caching by ePub-content hash is
  a future PR if cost becomes an issue.)
* No retries / backoff — that's the caller's job. The sniff is a
  best-effort UX nicety; failure means "no suggestion shown" not
  "modal blows up".

The sample strategy is parameterized: callers ask for a number of
blocks from the head, middle, and tail of the book (in that priority
order — head is weighted heaviest because the opening pages set the
register most explicitly). All counts are *targets*; smaller books
just feed whatever they've got.

Hard rules respected:

* Format-handling rule: the segments fed to the helper still go
  through the placeholderizer in :mod:`epublate.formats.epub`, so
  inline tags (``<em>``, footnote markers, etc.) reach the LLM as
  opaque ``[[T0]]`` placeholders, not raw HTML.
* Privacy invariant (PRD NFR-3 / F-T-1): book contents only leave
  the machine via the user-configured helper endpoint. No third
  parties involved.
"""

from __future__ import annotations

import logging
import re
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from epublate.core.segmentation import PLACEHOLDER_RE
from epublate.core.style import suggest_style_profile
from epublate.errors import EpublateError, FormatError, LLMResponseError
from epublate.formats.base import Segment
from epublate.formats.epub import EpubAdapter
from epublate.llm.base import LLMProvider
from epublate.llm.pricing import estimate_cost
from epublate.llm.prompts.extractor import (
    build_extractor_messages,
    parse_extractor_response,
)

_logger = logging.getLogger(__name__)

PURPOSE_TONE_SNIFF = "tone_sniff"
"""``llm_call.purpose`` slug callers can record if they ever choose to
audit sniff calls. The sniff itself never writes to the DB, but a
caller wrapping it in a transaction (e.g. a future "snapshot tone
sniff to project" feature) would use this constant."""


@dataclass(slots=True, frozen=True)
class SampleStrategy:
    """Per-call recipe for picking sample blocks from a book.

    The defaults front-load the head of the book because:

    * Openings declare register loudly (children's books open with a
      sing-song refrain; technical manuals open with a TL;DR; literary
      novels open with a settled narrator voice).
    * Middles tend toward action / dialogue, which is informative but
      noisier than the opening.
    * Tails are short windows that catch epilogue / acknowledgments —
      useful but lowest weight.

    All counts are upper bounds; if a book is shorter than
    ``head + middle + tail`` we degrade gracefully and feed everything
    we have. ``max_chars_per_block`` clips a single very long block
    (a multi-page paragraph) so one outlier can't blow the budget.
    ``max_total_chars`` is a hard ceiling on the concatenated sample.
    """

    head: int = 5
    middle: int = 3
    tail: int = 2
    max_chars_per_block: int = 1500
    max_total_chars: int = 12_000

    @property
    def total_blocks(self) -> int:
        return self.head + self.middle + self.tail


DEFAULT_SAMPLE_STRATEGY = SampleStrategy()


@dataclass(slots=True, frozen=True)
class ToneSniff:
    """Result of one :func:`sniff_tone` call.

    ``profile`` is the suggester's verdict — ``None`` means
    "no clear signal, keep whatever the curator has". ``register`` /
    ``audience`` are the raw helper observations (free-form strings)
    so the UI can also show them verbatim when the suggester returned
    ``None`` ("helper saw 'literary' / 'adult' but no preset matches").
    """

    profile: str | None
    register: str | None
    audience: str | None
    sample_block_count: int
    sample_chars: int
    prompt_tokens: int
    completion_tokens: int
    cost_usd: float
    model: str

    @property
    def has_suggestion(self) -> bool:
        return self.profile is not None


def sniff_tone(
    *,
    epub_path: Path,
    provider: LLMProvider,
    helper_model: str,
    source_lang: str,
    target_lang: str,
    sample_strategy: SampleStrategy = DEFAULT_SAMPLE_STRATEGY,
    chapter_max_tokens: int = 800,
) -> ToneSniff:
    """Read a spread of blocks from ``epub_path`` and ask the helper for tone.

    Raises:
        FormatError: the ePub failed to load / parse.
        ValueError: the file produced zero translatable blocks.
        LLMResponseError: the helper's response wasn't parseable.
    """

    if not epub_path.is_file():
        raise FormatError(f"source ePub not found: {epub_path}")

    blocks = _collect_translatable_blocks(
        epub_path, chapter_max_tokens=chapter_max_tokens
    )
    if not blocks:
        raise ValueError(f"no translatable blocks found in {epub_path}")

    chosen = _pick_spread(blocks, strategy=sample_strategy)
    sample_text, sample_chars = _build_sample_text(chosen, strategy=sample_strategy)

    messages = build_extractor_messages(
        source_lang=source_lang,
        target_lang=target_lang,
        source_text=sample_text,
        glossary=(),
    )

    chat = provider.chat(
        messages,
        model=helper_model,
        temperature=0.0,
        seed=7,
    )
    trace = parse_extractor_response(chat.content)
    profile = suggest_style_profile(
        register=trace.narrative_register,
        audience=trace.narrative_audience,
    )
    cost = estimate_cost(chat.model, chat.prompt_tokens, chat.completion_tokens)
    return ToneSniff(
        profile=profile,
        register=trace.narrative_register,
        audience=trace.narrative_audience,
        sample_block_count=len(chosen),
        sample_chars=sample_chars,
        prompt_tokens=chat.prompt_tokens,
        completion_tokens=chat.completion_tokens,
        cost_usd=cost,
        model=chat.model,
    )


# ---------------------------------------------------------------------------
# Sample selection
# ---------------------------------------------------------------------------


def _collect_translatable_blocks(
    epub_path: Path,
    *,
    chapter_max_tokens: int,
) -> list[Segment]:
    """Walk the spine and return every translatable :class:`Segment`.

    We use the existing ``EpubAdapter.segment`` so the placeholder
    discipline (PRD F-IO-2) and the skip-list (PRD F-IO-3) match what
    the post-create pipeline would do — the helper sees the same
    inputs it would see during intake.

    Raises :class:`~epublate.errors.FormatError` from the adapter on
    bad inputs (DRM, malformed XHTML, missing OPF). The caller is
    expected to swallow it and proceed without a suggestion.
    """

    adapter = EpubAdapter()
    book = adapter.load(epub_path)
    blocks: list[Segment] = []
    for chapter_doc in adapter.iter_chapters(book):
        if chapter_doc.tree is None:
            continue
        chapter_id = uuid.uuid4().hex  # synthetic — we never persist this
        try:
            segs = adapter.segment(
                chapter_doc, chapter_id=chapter_id, max_tokens=chapter_max_tokens
            )
        except Exception as exc:  # the adapter is M1; defend against surprises
            _logger.debug("style_sniff: skipping unparseable chapter: %s", exc)
            continue
        blocks.extend(segs)
    return blocks


def _pick_spread(
    blocks: list[Segment],
    *,
    strategy: SampleStrategy,
) -> list[Segment]:
    """Select a head/middle/tail spread from ``blocks`` per ``strategy``.

    Always preserves book order in the returned list (so the helper
    sees the opening before the middle before the closing). The middle
    samples are chosen at evenly spaced positions inside the
    inter-quartile range; this works gracefully when a book has very
    few blocks (the slice degenerates to "take a few from the middle
    of whatever you have").
    """

    n = len(blocks)
    if n == 0:
        return []
    if n <= strategy.total_blocks:
        return list(blocks)

    head_count = min(strategy.head, n)
    head = blocks[:head_count]
    head_ids = {id(b) for b in head}

    tail_count = min(strategy.tail, n - head_count)
    tail = blocks[n - tail_count :] if tail_count else []
    tail_ids = {id(b) for b in tail}

    # Middle: pick from the slice between head and tail, avoiding overlap.
    middle_zone = (
        blocks[head_count : n - tail_count] if tail_count else blocks[head_count:]
    )
    middle_count = min(strategy.middle, len(middle_zone))
    middle: list[Segment] = []
    if middle_count > 0:
        # Evenly spaced indices into ``middle_zone`` so consecutive
        # middle picks aren't adjacent paragraphs.
        step = max(1, len(middle_zone) // (middle_count + 1))
        for i in range(1, middle_count + 1):
            idx = min(i * step, len(middle_zone) - 1)
            candidate = middle_zone[idx]
            if id(candidate) in head_ids or id(candidate) in tail_ids:
                continue
            middle.append(candidate)

    chosen = [*head, *middle, *tail]
    # Re-sort by original position so the helper sees them in book order.
    pos = {id(b): i for i, b in enumerate(blocks)}
    chosen.sort(key=lambda b: pos[id(b)])
    return chosen


def _build_sample_text(
    blocks: Iterable[Segment],
    *,
    strategy: SampleStrategy,
) -> tuple[str, int]:
    """Serialize ``blocks`` into a single helper-LLM payload.

    Each block is clipped to ``max_chars_per_block`` and separated by a
    blank-line + horizontal rule (``\\n\\n---\\n\\n``) so the helper
    sees clear boundaries between non-contiguous samples. The
    placeholders the segmenter inserted for inline tags are stripped
    out — the helper doesn't need to reason about formatting at all,
    just register and audience.

    The total payload is hard-capped at ``max_total_chars``.
    """

    chunks: list[str] = []
    total = 0
    sep = "\n\n---\n\n"
    for block in blocks:
        text = _strip_placeholders(block.source_text).strip()
        if not text:
            continue
        if len(text) > strategy.max_chars_per_block:
            text = text[: strategy.max_chars_per_block].rstrip() + "…"
        if total + len(text) + len(sep) > strategy.max_total_chars:
            break
        chunks.append(text)
        total += len(text) + len(sep)
    if not chunks:
        raise ValueError("style_sniff: no usable text in selected blocks")
    body = sep.join(chunks)
    return body, len(body)


def _strip_placeholders(text: str) -> str:
    """Drop ``[[T0]]`` / ``[[/T0]]`` markers from a segment's source.

    The helper LLM only sees plain prose for the sniff; placeholders
    would just add noise to a register / audience read. Whitespace
    introduced by removed placeholders is collapsed.
    """

    cleaned = PLACEHOLDER_RE.sub("", text)
    return re.sub(r"[ \t]+", " ", cleaned)


# Public re-export for callers that want to record a sniff failure under
# a typed exception umbrella without importing every leaf module.
__all__ = [
    "DEFAULT_SAMPLE_STRATEGY",
    "PURPOSE_TONE_SNIFF",
    "EpublateError",
    "FormatError",
    "LLMResponseError",
    "SampleStrategy",
    "ToneSniff",
    "sniff_tone",
]
