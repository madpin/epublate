"""Book-level metadata extraction for the Dashboard book panel (M6).

The Dashboard's left column shows the book's own metadata (title,
authors, publisher, publish date, languages, description, word count,
cover slot) as a quick orientation aid for the curator. The data lives
in the ePub's OPF (``dc:*`` elements) and is read once per Dashboard
mount via :func:`extract_book_metadata`.

Cover image rendering is intentionally lightweight: we *locate* the
cover so the Dashboard can show its filename + byte count, but we do
not pull a TUI image library into the runtime dependency set. A future
M6+ change can swap the placeholder Static for ``textual-image`` (or
similar) without touching this module.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from ebooklib import ITEM_DOCUMENT, ITEM_IMAGE, epub
from lxml import etree
from sqlalchemy.engine import Engine

from epublate.db import repo, schema

_logger = logging.getLogger(__name__)


@dataclass(slots=True, frozen=True)
class CoverImage:
    """Locator for the cover image inside the ePub container.

    ``data`` carries the raw bytes so the UI can downsample / encode
    them however it likes (Sixel, kitty, ASCII art, …) without re-opening
    the ePub. ``media_type`` lets the renderer decide which encoder to
    pick. The fields are deliberately kept "data + metadata only" — no
    rendering happens here so the helper stays usable from headless
    contexts (CLI ``open`` no-TUI mode, future tests).
    """

    item_id: str
    href: str
    media_type: str
    bytes_size: int
    data: bytes = field(repr=False)


@dataclass(slots=True, frozen=True)
class BookMetadata:
    """All the curator-relevant fields the OPF can give us.

    Every field is best-effort: missing OPF entries fall back to ``None``
    so the Dashboard can render placeholders rather than blowing up on
    barebones ePubs.
    """

    title: str | None = None
    authors: tuple[str, ...] = ()
    publisher: str | None = None
    publish_date: str | None = None
    language: str | None = None
    description: str | None = None
    rights: str | None = None
    identifier: str | None = None
    word_count: int | None = None
    chapter_count: int | None = None
    cover: CoverImage | None = None

    def author_line(self) -> str:
        if not self.authors:
            return "(unknown author)"
        if len(self.authors) <= 2:
            return ", ".join(self.authors)
        return f"{', '.join(self.authors[:2])} +{len(self.authors) - 2} more"

    def short_description(self, *, max_chars: int = 320) -> str | None:
        """Truncate the description for the collapsed dashboard view.

        Many ePubs ship long marketing blurbs in ``dc:description`` —
        rendering them in full would push every other panel off-screen.
        We cut at the first sentence boundary past ``max_chars`` so the
        snippet still reads naturally, and append an ellipsis when we
        truncate.
        """

        if not self.description:
            return None
        text = " ".join(self.description.split())
        if len(text) <= max_chars:
            return text
        cut = text.rfind(".", 0, max_chars + 80)
        if cut == -1 or cut < max_chars - 80:
            cut = max_chars
        return text[: cut + (1 if text[cut : cut + 1] == "." else 0)].rstrip() + "…"


def extract_book_metadata(epub_path: Path) -> BookMetadata:
    """Read OPF metadata + cover + word count from an ePub on disk.

    Designed to fail soft: any IO/parse error is logged and a
    placeholder :class:`BookMetadata` is returned so the Dashboard
    keeps working on weird ePubs (unsigned, missing cover, etc.).
    """

    try:
        book = epub.read_epub(str(epub_path))
    except Exception as exc:  # broad: ebooklib can raise many kinds
        _logger.warning("could not read OPF metadata for %s: %s", epub_path, exc)
        return BookMetadata()

    title = _first_dc(book, "title")
    language = _first_dc(book, "language")
    publisher = _first_dc(book, "publisher")
    publish_date = _first_dc(book, "date")
    description = _first_dc(book, "description")
    rights = _first_dc(book, "rights")
    identifier = _first_dc(book, "identifier")
    authors = tuple(_all_dc(book, "creator"))

    word_count = _count_words_in_spine(book)
    chapter_count = sum(
        1 for entry in book.spine if _spine_doc(book, entry) is not None
    )
    cover = _find_cover(book)

    return BookMetadata(
        title=title,
        authors=authors,
        publisher=publisher,
        publish_date=publish_date,
        language=language,
        description=description,
        rights=rights,
        identifier=identifier,
        word_count=word_count,
        chapter_count=chapter_count,
        cover=cover,
    )


@dataclass(slots=True, frozen=True)
class GlossaryStats:
    """Glossary status counts surfaced on the Dashboard's right column.

    The schema only exposes three statuses today (``proposed`` /
    ``confirmed`` / ``locked``); ``other`` catches any future enum
    additions so a status rename never silently drops a count.
    """

    locked: int = 0
    confirmed: int = 0
    proposed: int = 0
    other: int = 0

    @property
    def total(self) -> int:
        return self.locked + self.confirmed + self.proposed + self.other


def compute_glossary_stats(engine: Engine, project_id: str) -> GlossaryStats:
    """Aggregate ``glossary_entry.status`` for the Dashboard glossary panel.

    Single grouped query (PRD F-T-2): no per-status round-trip. Status
    strings come from :data:`schema.GlossaryStatus` so a future enum
    rename here will fail loudly.
    """

    from sqlalchemy import func, select

    stmt = (
        select(
            schema.glossary_entry.c.status,
            func.count().label("n"),
        )
        .where(schema.glossary_entry.c.project_id == project_id)
        .group_by(schema.glossary_entry.c.status)
    )
    counts: dict[str, int] = {}
    with engine.begin() as conn:
        for row in conn.execute(stmt).mappings():
            counts[str(row["status"])] = int(row["n"] or 0)
    locked = counts.pop(schema.GlossaryStatus.LOCKED, 0)
    confirmed = counts.pop(schema.GlossaryStatus.CONFIRMED, 0)
    proposed = counts.pop(schema.GlossaryStatus.PROPOSED, 0)
    other = sum(counts.values())
    return GlossaryStats(
        locked=locked,
        confirmed=confirmed,
        proposed=proposed,
        other=other,
    )


@dataclass(slots=True, frozen=True)
class IntakeStatus:
    """Latest intake summary for the Dashboard header strip.

    ``proposed_count`` and ``cost_usd`` are pulled from the most recent
    ``intake.completed`` event so the header reflects the actual run,
    not just a "ran at some point" boolean.
    """

    has_run: bool
    last_run_at: int | None = None
    chunks: int | None = None
    proposed_count: int | None = None
    cost_usd: float | None = None
    pov: str | None = None
    tense: str | None = None


def get_intake_status(engine: Engine, project_id: str) -> IntakeStatus:
    """Read the most recent ``intake.completed`` event, if any."""

    events = repo.list_events(engine, project_id=project_id)
    for ev in reversed(events):
        if ev.kind == "intake.completed":
            payload = ev.payload
            return IntakeStatus(
                has_run=True,
                last_run_at=ev.ts,
                chunks=_safe_int(payload.get("chunks")),
                proposed_count=_safe_int(payload.get("proposed_count")),
                cost_usd=_safe_float(payload.get("cost_usd")),
                pov=str(payload.get("pov")) if payload.get("pov") else None,
                tense=str(payload.get("tense")) if payload.get("tense") else None,
            )
    return IntakeStatus(has_run=False)


# ---------------------------------------------------------------------------
# OPF helpers (private)
# ---------------------------------------------------------------------------


def _first_dc(book: epub.EpubBook, key: str) -> str | None:
    items = book.get_metadata("DC", key)
    if not items:
        return None
    raw, _attrs = items[0]
    text = (str(raw).strip() if raw is not None else "") or None
    return text


def _all_dc(book: epub.EpubBook, key: str) -> Iterable[str]:
    for raw, _attrs in book.get_metadata("DC", key):
        if raw is None:
            continue
        text = str(raw).strip()
        if text:
            yield text


def _spine_doc(book: epub.EpubBook, spine_entry: object) -> epub.EpubItem | None:
    """Return the spine entry's ``EpubItem`` only if it's an XHTML doc."""

    if isinstance(spine_entry, tuple):
        item_id = spine_entry[0]
    elif isinstance(spine_entry, str):
        item_id = spine_entry
    else:
        item_id = getattr(spine_entry, "id", None)
    if item_id is None:
        return None
    item = book.get_item_with_id(item_id)
    if item is None or item.get_type() != ITEM_DOCUMENT:
        return None
    return item


def _count_words_in_spine(book: epub.EpubBook) -> int | None:
    """Approximate word count by walking every spine document.

    "Approximate" because we count XHTML text node tokens — close enough
    for the curator's "is this 50k or 200k?" gut check, but not a
    promise to match an editor's word counter to the digit. We bail out
    on any parse error and return ``None`` so the panel can render
    "(?)" instead of pretending to know.
    """

    parser = etree.XMLParser(
        resolve_entities=False, no_network=True, load_dtd=False, recover=True
    )
    total = 0
    counted_anything = False
    for entry in book.spine:
        item = _spine_doc(book, entry)
        if item is None:
            continue
        try:
            raw = (
                item.content if isinstance(item.content, bytes) else item.get_content()
            )
            if not raw:
                continue
            tree = etree.fromstring(raw, parser)
        except Exception:
            continue
        text_runs = tree.xpath(".//text()") if tree is not None else []
        for run in text_runs:
            for word in str(run).split():
                if any(ch.isalnum() for ch in word):
                    total += 1
                    counted_anything = True
    return total if counted_anything else None


def _find_cover(book: epub.EpubBook) -> CoverImage | None:
    """Locate the cover image in the OPF and return its bytes.

    Resolution order matches the ePub spec's tolerated variations:

    1. ``<meta name="cover" content="<id>">`` from the OPF metadata
       block — the common ePub 2 convention still emitted by most
       publishers.
    2. The first manifest item with ``properties="cover-image"`` — the
       canonical ePub 3 form.
    3. The first image whose ID *starts with* "cover".

    Returns ``None`` when nothing matches; the Dashboard renders an
    "(no cover)" placeholder.
    """

    cover_id: str | None = None
    for raw, attrs in book.get_metadata("OPF", "cover"):
        if isinstance(attrs, dict):
            candidate = attrs.get("content")
            if candidate:
                cover_id = str(candidate)
                break
        if raw:
            cover_id = str(raw)
            break

    candidates: list[epub.EpubItem] = []
    if cover_id:
        item = book.get_item_with_id(cover_id)
        if item is not None and item.get_type() == ITEM_IMAGE:
            candidates.append(item)

    for item in book.get_items_of_type(ITEM_IMAGE):
        properties = (
            getattr(item, "properties", None) or []
        )  # ebooklib leaves the attr unset on some inputs
        if isinstance(properties, str):
            properties = properties.split()
        if any("cover" in str(p).lower() for p in properties) or (
            item.id and item.id.lower().startswith("cover")
        ):
            candidates.append(item)

    seen: set[str] = set()
    for item in candidates:
        if item.id in seen:
            continue
        seen.add(item.id)
        data = item.content if isinstance(item.content, bytes) else item.get_content()
        if not data:
            continue
        return CoverImage(
            item_id=str(item.id),
            href=str(item.file_name or ""),
            media_type=str(item.media_type or ""),
            bytes_size=len(data),
            data=data,
        )
    return None


def _safe_int(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, str | float):
        try:
            return int(value)
        except (TypeError, ValueError):
            return None
    return None


def _safe_float(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except (TypeError, ValueError):
            return None
    return None


__all__ = [
    "BookMetadata",
    "CoverImage",
    "GlossaryStats",
    "IntakeStatus",
    "compute_glossary_stats",
    "extract_book_metadata",
    "get_intake_status",
]
