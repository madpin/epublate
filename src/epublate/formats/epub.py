"""ePub adapter (PRD §4.1, §6.5 / F-IO-1..7).

Implements :class:`epublate.formats.base.FormatAdapter` against ``ebooklib``
and ``lxml``. Round-trip identity for untranslated documents is the contract
this module defends; the rest of the system relies on it (format-handling
rule).

Design notes:

* ``ebooklib.epub.EpubHtml.get_content()`` re-renders chapters through a
  template: it strips everything outside the ``<body>`` and re-emits the
  ``<html>`` envelope using the item's own ``lang`` / ``title`` attributes.
  We therefore parse the *body* of the rendered chapter, mutate that
  subtree, then write the serialized full XHTML back via ``set_content`` and
  let ebooklib's writer normalize the envelope on save.
* The skeleton stores tag names in lxml Clark notation (``{ns}local``) so
  namespaces survive reassembly without manual nsmap juggling.
* All write paths are atomic: serialize to ``out.tmp`` then ``os.replace``.
"""

from __future__ import annotations

import hashlib
import os
import uuid
from collections.abc import Iterable
from pathlib import Path
from typing import Any, ClassVar

from ebooklib import ITEM_DOCUMENT, epub
from lxml import etree

from epublate import __version__
from epublate.core.segmentation import (
    PLACEHOLDER_RE,
    apply_parts_to_host,
    placeholderize,
)
from epublate.core.validators import validate_segment_placeholders
from epublate.errors import FormatError
from epublate.formats.base import Book, ChapterDoc, Segment

XHTML_NS = "http://www.w3.org/1999/xhtml"
XML_NS = "http://www.w3.org/XML/1998/namespace"
DC_NS = "http://purl.org/dc/elements/1.1/"

# Block-level *paragraph* hosts: any leaf-level (no inner block host) match
# becomes one segment. ``div`` and ``section`` are included because real-world
# ePubs frequently use them as paragraph wrappers — Calibre-converted books
# typically emit ``<div class="calibreXX"><span>…</span></div>`` instead of
# ``<p>``, and HTML5-native books wrap chapters in ``<section>``. Outer
# wrappers that contain other block hosts are filtered out by
# :func:`_has_inner_block_host` so we only ever segment the innermost host.
_BLOCK_HOST_TAGS: frozenset[str] = frozenset(
    {
        "p",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "li",
        "td",
        "th",
        "figcaption",
        "blockquote",
        "dt",
        "dd",
        "caption",
        "div",
        "section",
        "article",
        "aside",
        "header",
        "footer",
        "main",
    }
)
_SKIP_TAGS: frozenset[str] = frozenset({"code", "pre", "script", "style"})


def _local_name(tag: str | bytes) -> str:
    s = tag.decode() if isinstance(tag, bytes) else tag
    if s.startswith("{"):
        return s.split("}", 1)[1]
    return s


def _xpath_to(elem: etree._Element) -> str:
    """Stable XPath from the document root to ``elem`` (lxml's getpath)."""

    return str(elem.getroottree().getpath(elem))


def _is_skipped(elem: etree._Element) -> bool:
    if _local_name(str(elem.tag)) in _SKIP_TAGS:
        return True
    for ancestor in elem.iterancestors():
        if _local_name(str(ancestor.tag)) in _SKIP_TAGS:
            return True
    return False


def _has_inner_block_host(elem: etree._Element) -> bool:
    for descendant in elem.iterdescendants():
        if _local_name(str(descendant.tag)) in _BLOCK_HOST_TAGS:
            return True
    return False


def _find_translatable_hosts(
    tree: etree._Element, *, target_lang: str | None
) -> list[etree._Element]:
    hosts: list[etree._Element] = []
    for elem in tree.iter():
        if _local_name(str(elem.tag)) not in _BLOCK_HOST_TAGS:
            continue
        if _is_skipped(elem):
            continue
        if _has_inner_block_host(elem):
            continue
        if target_lang and _matches_lang(elem, target_lang):
            continue
        hosts.append(elem)
    return hosts


def _matches_lang(elem: etree._Element, target_lang: str) -> bool:
    """True if the element (or its inherited xml:lang) equals ``target_lang``."""

    target = target_lang.lower()
    for node in (elem, *elem.iterancestors()):
        lang = node.get(f"{{{XML_NS}}}lang") or node.get("lang")
        if lang:
            return bool(lang.split("-", 1)[0].lower() == target.split("-", 1)[0])
    return False


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _make_xml_parser() -> etree.XMLParser:
    return etree.XMLParser(
        resolve_entities=False, no_network=True, load_dtd=False, recover=False
    )


def _set_html_lang(tree: etree._Element, lang: str) -> None:
    """Update ``<html lang>`` and ``xml:lang`` on the chapter root.

    ebooklib normally writes these in its chapter template via
    ``item.lang``; we bypass that template (see :meth:`EpubAdapter.save`)
    to preserve the original ``<head>``, so the language attributes
    have to be set on the parsed tree directly. We only mutate the
    root element — descendant ``xml:lang`` overrides (e.g. quoted
    foreign-language passages the original author marked up) stay
    untouched.
    """

    root = tree
    while root.getparent() is not None:
        root = root.getparent()
    if _local_name(str(root.tag)).lower() != "html":
        return
    root.set("lang", lang)
    root.set(f"{{{XML_NS}}}lang", lang)


def _spine_item(book: epub.EpubBook, spine_entry: object) -> epub.EpubItem | None:
    if isinstance(spine_entry, tuple):
        item_id = spine_entry[0]
    elif isinstance(spine_entry, str):
        item_id = spine_entry
    else:
        item_id = getattr(spine_entry, "id", None)
    if item_id is None:
        return None
    return book.get_item_with_id(item_id)


class EpubAdapter:
    """ePub format adapter (PRD §6.5 / F-IO-*)."""

    name: ClassVar[str] = "epub"

    def __init__(self, *, target_lang: str | None = None) -> None:
        self.target_lang = target_lang

    def load(self, path: Path) -> Book:
        epub_book = epub.read_epub(str(path))
        title = _first_metadata(epub_book, "title", default=path.stem)
        language = _first_metadata(epub_book, "language", default="und")
        return Book(
            path=path,
            title=title,
            language=language,
            container=epub_book,
            extras={"chapters": []},
        )

    def iter_chapters(self, book: Book) -> Iterable[ChapterDoc]:
        epub_book: epub.EpubBook = book.container
        chapters: list[ChapterDoc] = []
        book.extras["chapters"] = chapters
        for spine_idx, entry in enumerate(epub_book.spine):
            item = _spine_item(epub_book, entry)
            if item is None:
                continue
            media_type = item.media_type or ""
            href = item.file_name or ""
            title = getattr(item, "title", None) or None
            # ``EpubHtml.get_content()`` runs the chapter template,
            # which rebuilds ``<head>`` from ``item.title``/``metas``/
            # ``links`` (all empty after ``read_epub`` because
            # ``_load_manifest`` never populates them) and copies only
            # ``<body>`` from ``item.content``. Reading via that path
            # silently drops the original stylesheet ``<link>``,
            # inline ``<style>`` blocks, ``<title>``, etc. — the
            # parsed ``doc.tree`` would already be missing them and
            # no save-side fix could bring them back. Read the raw
            # zip bytes that ``read_epub`` cached on ``item.content``
            # instead so the head round-trips intact (PRD §4.1 /
            # F-IO-1).
            raw_xml = (
                item.content if isinstance(item.content, bytes) else item.get_content()
            )
            tree: etree._Element | None = None
            if item.get_type() == ITEM_DOCUMENT:
                try:
                    tree = etree.fromstring(raw_xml, _make_xml_parser())
                except etree.XMLSyntaxError as exc:
                    raise FormatError(
                        f"failed to parse XHTML for spine item {item.id}: {exc}"
                    ) from exc
                if not title and tree is not None:
                    # ebooklib only sets ``item.title`` when the OPF
                    # spine entry carries one; many real-world ePubs
                    # leave it blank. Fall back to the first heading
                    # (or ``<head><title>``) so the curator's UI shows
                    # a meaningful chapter name instead of a filename.
                    title = _extract_chapter_title(tree)
            doc = ChapterDoc(
                spine_idx=spine_idx,
                href=href,
                title=title,
                media_type=media_type,
                raw_xml=raw_xml,
                tree=tree,
            )
            chapters.append(doc)
            yield doc

    def segment(
        self,
        doc: ChapterDoc,
        *,
        chapter_id: str,
        max_tokens: int = 800,
    ) -> list[Segment]:
        if doc.tree is None:
            return []
        segments: list[Segment] = []
        for host in _find_translatable_hosts(doc.tree, target_lang=self.target_lang):
            source_text, skeleton = placeholderize(host)
            if not source_text.strip():
                continue
            # ``<p><img/></p>`` and friends placeholderize to a single
            # ``[[T0]]`` (or ``[[T0]][[/T0]]`` for empty inline pairs)
            # with no human-readable text. They round-trip fine when
            # left untouched in the DOM, so segmenting them just bloats
            # the queue and confuses the Reader (PRD §4.6).
            if not PLACEHOLDER_RE.sub("", source_text).strip():
                continue
            host_path = _xpath_to(host)
            seg = Segment(
                id=uuid.uuid4().hex,
                chapter_id=chapter_id,
                idx=len(segments),
                source_text=source_text,
                source_hash=_sha256(source_text),
                target_text=None,
                inline_skeleton=skeleton,
                host_path=host_path,
                host_part=0,
                host_total_parts=1,
            )
            segments.append(seg)
        return segments

    def reassemble(self, doc: ChapterDoc, translated: list[Segment]) -> ChapterDoc:
        if doc.tree is None:
            return doc
        by_path: dict[str, list[Segment]] = {}
        for seg in translated:
            by_path.setdefault(seg.host_path, []).append(seg)

        for host_path, segs in by_path.items():
            segs.sort(key=lambda s: s.host_part)
            elements = doc.tree.xpath(host_path)
            if not elements:
                raise FormatError(
                    f"reassemble: host_path {host_path!r} not found in chapter"
                )
            host = elements[0]
            for seg in segs:
                validate_segment_placeholders(seg)
            parts = [
                (seg.target_text or seg.source_text, seg.inline_skeleton)
                for seg in segs
            ]
            apply_parts_to_host(host, parts)
        return doc

    def save(self, book: Book, out_path: Path) -> None:
        epub_book: epub.EpubBook = book.container
        target_lang = book.extras.get("target_lang")

        for doc in book.extras.get("chapters", []):
            if doc.tree is None:
                continue
            if target_lang:
                _set_html_lang(doc.tree, target_lang)
            xml_bytes = etree.tostring(
                doc.tree.getroottree(),
                xml_declaration=True,
                encoding="utf-8",
                pretty_print=False,
            )
            item = _spine_item(epub_book, epub_book.spine[doc.spine_idx])
            if item is None:
                continue
            item.content = xml_bytes
            # ebooklib's ``EpubHtml.get_content()`` runs the chapter
            # template: it rebuilds ``<head>`` from ``item.title /
            # metas / links`` (all empty after ``read_epub`` because
            # ``_load_manifest`` never populates them) and copies only
            # ``<body>`` from ``item.content``. That silently drops
            # the original stylesheet ``<link>``, inline ``<style>``
            # blocks, ``<title>``, etc. — Calibre cover pages whose
            # ``<img>`` is sized/centered by ``.calibre3`` /
            # ``.calibre4`` then render unstyled and look cropped, and
            # body chapters lose all per-class styling. The format-
            # preservation invariant (PRD §4.1 / F-IO-1) requires the
            # XHTML to round-trip; bind a per-instance ``get_content``
            # that returns our serialized tree so the writer emits
            # exactly what we built (head and all).
            item.get_content = lambda default=None, _bytes=xml_bytes: _bytes
            if target_lang:
                item.lang = target_lang

        if target_lang:
            _replace_dc_metadata(epub_book, "language", target_lang)
            _add_provenance(epub_book)

        _normalize_toc(epub_book.toc)

        out_path = Path(out_path)
        tmp_path = out_path.with_suffix(out_path.suffix + ".tmp")
        try:
            epub.write_epub(str(tmp_path), epub_book)
            os.replace(tmp_path, out_path)
        except Exception:
            if tmp_path.exists():
                tmp_path.unlink()
            raise


_TITLE_HEADING_TAGS: tuple[str, ...] = ("h1", "h2", "h3", "title")

# Cap on how many top-level body children we scan when doing the
# Calibre-style heading fallback below. Real chapter headings are
# always among the first few elements; pushing further turns the
# heuristic into "first bold text in the chapter", which is wrong
# for body prose that uses bold for emphasis.
_CALIBRE_HEADING_SCAN_LIMIT = 8

# Hard cap on the length of a Calibre fallback title. Heading text is
# almost always a short clause; longer matches are pretty reliable
# evidence the heuristic latched onto a paragraph instead of a header.
_CALIBRE_HEADING_MAX_CHARS = 200

# Threshold below which we treat a Calibre-style heading as a bare
# chapter-number label (``"1"``, ``"23"``, ``"II"``, ``"1."``) rather
# than a real title, and try to glue it to the next styled heading
# that follows within the scan window.
_CALIBRE_NUMBER_LABEL_MAX_CHARS = 5


def _extract_chapter_title(tree: etree._Element) -> str | None:
    """Best-effort chapter title from a parsed XHTML chapter.

    Walks ``<h1>`` → ``<h2>`` → ``<h3>`` → ``<head><title>`` in order
    and returns the first non-empty trimmed text. When that fails we
    try a Calibre-specific fallback (see :func:`_extract_calibre_heading`)
    because Calibre converts real chapter headings to
    ``<div><span class="bold">…</span></div>`` blocks that no spec-style
    heading scan would catch. Used by the adapter to enrich rows in the
    chapter table when ebooklib didn't supply a title from the OPF
    spine, which is the common case for real ePubs.
    """

    for tag in _TITLE_HEADING_TAGS:
        for elem in tree.iter():
            if _local_name(str(elem.tag)).lower() != tag:
                continue
            text = " ".join(elem.itertext()).strip()
            if text:
                return _normalize_whitespace(text)
    return _extract_calibre_heading(tree)


def _extract_calibre_heading(tree: etree._Element) -> str | None:
    """Calibre-style chapter heading fallback for converted ePubs.

    Calibre's ePub converter often emits chapter headings as
    ``<div class="calibreNN"><span class="boldM">Title</span></div>``
    instead of an actual ``<h1>``/``<h2>``. The standard heading scan
    misses these and we end up with a ``title=None`` row even when the
    chapter has an obvious heading the reader would expect to see.

    The heuristic is intentionally conservative:

    * Only the first :data:`_CALIBRE_HEADING_SCAN_LIMIT` direct children
      of ``<body>`` are considered. Real headings sit at the very top
      of the document; any bold span deeper in the chapter is almost
      certainly inline emphasis inside a paragraph.
    * A candidate must contain a ``<span>`` (or descendant) whose class
      list includes a ``bold``-prefixed token. Plain bold runs that
      authors rely on for emphasis pass through ``<strong>`` /
      ``<b>``, not class-styled spans, so this is a strong hint the
      element is actually a styled heading.
    * The captured text is capped at :data:`_CALIBRE_HEADING_MAX_CHARS`;
      anything longer is more likely a paragraph than a heading.
    * When the first match is a bare chapter number (e.g. ``"1"`` or
      ``"II"``) and a longer heading follows in the next styled div,
      we glue them together (``"1 — The Rules of Politics"``). Calibre
      routinely splits chapter-number and chapter-title across two
      sibling divs, and the curator wants to see both.
    """

    body: etree._Element | None = None
    for elem in tree.iter():
        if _local_name(str(elem.tag)).lower() == "body":
            body = elem
            break
    if body is None:
        return None

    candidates: list[str] = []
    for idx, child in enumerate(body):
        if idx >= _CALIBRE_HEADING_SCAN_LIMIT:
            break
        for span in child.iter():
            if _local_name(str(span.tag)).lower() != "span":
                continue
            classes = (span.get("class") or "").split()
            if not any(c.lower().startswith("bold") for c in classes):
                continue
            text = _normalize_whitespace(" ".join(span.itertext()))
            if 1 <= len(text) <= _CALIBRE_HEADING_MAX_CHARS:
                candidates.append(text)
                break  # only the first bold span per body child
        if len(candidates) >= 2:
            break

    if not candidates:
        return None
    first = candidates[0]
    if len(candidates) >= 2 and len(first) <= _CALIBRE_NUMBER_LABEL_MAX_CHARS:
        second = candidates[1]
        if len(second) > len(first):
            return f"{first} \u2014 {second}"
    return first


def toc_title_map(book: Book) -> dict[str, str]:
    """Map ``href -> title`` from the ePub's table of contents.

    Walks the (possibly nested) ``book.toc`` tree ebooklib parses out of
    the OPF/NCX/nav and returns the first title encountered for each
    spine href. Anchors are stripped (``foo.html#sec1`` → ``foo.html``)
    so the map keys line up with what :meth:`EpubAdapter.iter_chapters`
    yields. Used by the project layer to enrich chapter rows whose
    in-document heading scan came up empty (e.g. Calibre splits where
    only the first sub-section has a styled heading and the parent
    file's "Chapter 1" label lives only in the NCX).
    """

    out: dict[str, str] = {}

    def _walk(items: object) -> None:
        if not isinstance(items, list | tuple):
            return
        for entry in items:
            if isinstance(entry, tuple) and len(entry) == 2:
                section, children = entry
                href = getattr(section, "href", None)
                title = getattr(section, "title", None)
                if href and title:
                    out.setdefault(_strip_anchor(str(href)), str(title))
                _walk(children)
            elif hasattr(entry, "href"):
                href = entry.href
                title = getattr(entry, "title", None)
                if href and title:
                    out.setdefault(_strip_anchor(str(href)), str(title))

    container = book.container
    toc = getattr(container, "toc", None)
    if toc is not None:
        _walk(toc)
    return out


def _strip_anchor(href: str) -> str:
    return href.split("#", 1)[0]


def _normalize_whitespace(text: str) -> str:
    return " ".join(text.split())


def _first_metadata(epub_book: epub.EpubBook, key: str, *, default: str) -> str:
    items = epub_book.get_metadata("DC", key)
    if not items:
        return default
    value, _attrs = items[0]
    return str(value) if value else default


def _replace_dc_metadata(epub_book: epub.EpubBook, key: str, value: str) -> None:
    """Replace all ``dc:<key>`` entries with a single value.

    ebooklib's ``set_language`` *appends*; the OPF would then carry both the
    source and target languages and break F-IO-5. We clear and re-add.
    """

    if DC_NS in epub_book.metadata and key in epub_book.metadata[DC_NS]:
        epub_book.metadata[DC_NS][key] = []
    epub_book.add_metadata("DC", key, value)


_PROVENANCE_ID = "epublate-provenance"


def _add_provenance(epub_book: epub.EpubBook) -> None:
    """Add a ``dc:contributor`` provenance entry once per book."""

    existing = epub_book.metadata.get(DC_NS, {}).get("contributor", [])
    for value, attrs in existing:
        if isinstance(attrs, dict) and attrs.get("id") == _PROVENANCE_ID:
            return
        if value and value.startswith("epublate "):
            return
    epub_book.add_metadata(
        "DC",
        "contributor",
        f"epublate {__version__}",
        others={"id": _PROVENANCE_ID},
    )


def _normalize_toc(toc: Any, *, counter: list[int] | None = None) -> None:
    """Backfill ``uid`` on Link nodes parsed from an existing ePub.

    ``ebooklib.epub.read_epub`` produces ``Link`` instances with ``uid=None``;
    ``write_epub`` then crashes inside the NCX builder. We synthesize stable
    ids per node so saving stays a no-op when the user only mutated content.
    """

    if counter is None:
        counter = [0]
    for entry in toc:
        if isinstance(entry, tuple) and len(entry) == 2:
            section, children = entry
            if getattr(section, "uid", None) in (None, ""):
                counter[0] += 1
                section.uid = f"toc-{counter[0]}"
            _normalize_toc(children, counter=counter)
        elif hasattr(entry, "uid"):
            if entry.uid in (None, ""):
                counter[0] += 1
                entry.uid = f"toc-{counter[0]}"
        elif isinstance(entry, list):
            _normalize_toc(entry, counter=counter)


__all__ = ["EpubAdapter", "toc_title_map"]
