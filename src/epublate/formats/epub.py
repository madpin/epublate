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
    apply_parts_to_host,
    is_trivially_empty,
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


# Tags that may not contain block-level descendants per HTML5 content
# rules (e.g. ``<p>`` is phrasing-only). When the parent of an orphan
# inline run is one of these we must wrap with ``<div>`` rather than
# duplicating the parent tag, otherwise the parser would auto-close the
# outer element and the wrap would silently move the orphan out of the
# parent — defeating the whole point of the hoist.
_PHRASING_ONLY_BLOCK_HOSTS: frozenset[str] = frozenset(
    {"p", "h1", "h2", "h3", "h4", "h5", "h6", "dt"}
)


def _is_orphan_wrapper(elem: etree._Element) -> bool:
    """True iff ``elem`` is a wrapper inserted by :func:`_hoist_orphaned_inline_runs`.

    Marked with ``data-epublate-orphan="1"`` so we can tell our wrappers
    apart from authored ``<div>``s — needed for idempotency and for the
    ``epublate repair`` flow that re-segments existing projects.
    """

    return bool(elem.get("data-epublate-orphan") == "1")


def _is_mixed_content_block(elem: etree._Element) -> bool:
    """True if ``elem`` has both block-host children AND non-trivial inline content.

    "Inline content" here means: direct text on ``elem.text``, an inline
    child element (or entity reference) that carries text, or trailing
    text on a child's ``tail``. Pure-whitespace runs don't count — those
    serialize fine and don't need a translatable wrapper.
    """

    has_block = False
    has_inline = bool((elem.text or "").strip())
    for child in elem.iterchildren():
        if isinstance(child.tag, str):
            child_tag = _local_name(str(child.tag))
            if child_tag in _BLOCK_HOST_TAGS:
                has_block = True
            else:
                # Non-block element. Any text it carries is orphaned.
                if "".join(child.itertext()).strip():
                    has_inline = True
        else:
            # Comment / PI / Entity-reference. Entities (``&nbsp;``,
            # ``&copy;``) carry user-visible content; treat as inline.
            text = child.text or ""
            if text.strip():
                has_inline = True
        if child.tail and child.tail.strip():
            has_inline = True
    return has_block and has_inline


def _wrapper_tag_for(parent: etree._Element) -> str:
    """Choose the synthetic wrapper's tag, namespaced to match ``parent``.

    Always emits a ``<div>`` (or namespaced equivalent) so we never produce
    HTML5-invalid nesting like ``<p><p>…</p></p>``: even when the parent is
    itself a phrasing-only host, the wrapper has to be the most permissive
    block container we have.
    """

    parent_tag = str(parent.tag)
    if "}" in parent_tag:
        ns = parent_tag.split("}", 1)[0][1:]
        return f"{{{ns}}}div"
    return "div"


def _hoist_orphaned_inline_runs(tree: etree._Element) -> int:
    """Wrap orphaned inline siblings inside mixed-content blocks (PRD §4.1).

    Real-world ePubs (especially Calibre conversions) frequently emit
    block elements that mix inline-only siblings with nested block-level
    descendants, e.g.::

        <div class="calibre22">
          <span class="calibre10">prose paragraph...</span>
          <div class="calibre26"><blockquote>quoted text</blockquote></div>
        </div>

    The default segmenter walks ``_BLOCK_HOST_TAGS`` and skips any host
    with block-host descendants — which silently strands the leading
    ``<span>``: it never gets a segment row, never reaches the LLM, and
    surfaces as untranslated source-language text mid-chapter in the
    exported ePub.

    This pass runs before segmentation/reassembly and rewrites every
    such mixed-content host so each contiguous inline run becomes its
    own block-host (a synthetic ``<div>`` carrying the parent's
    ``class`` and a ``data-epublate-orphan="1"`` marker). After the
    pass, no block-host element has mixed content and the standard
    ``_find_translatable_hosts`` walk picks the orphans up cleanly.

    Idempotent: re-running on a tree that already has no mixed-content
    hosts is a no-op. Same-input → same-output, so XPaths produced at
    segment time still resolve at export time after a fresh load of the
    original ePub.

    Returns the number of synthetic wrappers inserted (mainly useful
    for tests / repair tooling).
    """

    candidates: list[etree._Element] = []
    for elem in tree.iter():
        if not isinstance(elem.tag, str):
            continue
        if _local_name(str(elem.tag)) not in _BLOCK_HOST_TAGS:
            continue
        if _is_skipped(elem):
            continue
        if _is_mixed_content_block(elem):
            candidates.append(elem)

    inserted = 0
    for parent in candidates:
        inserted += _split_mixed_content_block(parent)
    return inserted


def _restore_outer_whitespace(source_text: str, target_text: str) -> str:
    """Restore leading / trailing whitespace the LLM stripped from the target.

    OpenAI-compatible chat endpoints almost universally ``.strip()`` the
    response (or the model itself does), so a host like
    ``"\\n    [[T0]]Title[[/T0]]\\n  "`` typically comes back as
    ``"[[T0]]Título[[/T0]]"``. The lost whitespace is purely cosmetic —
    browsers collapse it — but the source ePub's per-host indentation
    is part of its identity, and putting it back keeps diffs against
    the original small and reviews tractable. Re-introduces the source
    side's leading and trailing whitespace runs only when the target
    isn't already carrying its own (so we don't double up).
    """

    if not source_text or not target_text:
        return target_text
    src_lead_len = len(source_text) - len(source_text.lstrip())
    src_trail_len = len(source_text) - len(source_text.rstrip())
    tgt_lead_len = len(target_text) - len(target_text.lstrip())
    tgt_trail_len = len(target_text) - len(target_text.rstrip())

    out = target_text
    if src_lead_len and not tgt_lead_len:
        out = source_text[:src_lead_len] + out
    if src_trail_len and not tgt_trail_len:
        out = out + source_text[len(source_text) - src_trail_len :]
    return out


def _split_mixed_content_block(parent: etree._Element) -> int:
    """Wrap every orphaned inline run inside ``parent`` into a synthetic block.

    Implementation walks ``parent``'s direct children left-to-right,
    routing each block-host child through unchanged and accumulating the
    inline-only ones into a buffer. When a block-host arrives the buffer
    is flushed into a fresh wrapper element. The wrapper inherits the
    parent's ``class`` so styling stays close to what the browser
    rendered as an anonymous block before the change.

    Whitespace-only buffers don't get a wrapper (they're cosmetic
    indentation in the source XHTML); the whitespace is appended to the
    preceding block's ``tail`` so the file still serializes with the
    same line breaks at the top level.

    Returns how many wrappers this call inserted.
    """

    from lxml import etree

    children = list(parent)
    if not children:
        return 0

    wrapper_qtag = _wrapper_tag_for(parent)
    parent_class = parent.get("class")

    new_top_level: list[etree._Element] = []
    pending_text: str = parent.text or ""
    pending_inlines: list[etree._Element] = []
    inserted = 0

    def _child_textual(child: etree._Element) -> str:
        # Inline elements (string tag) contribute their full text run;
        # comments / PIs only have ``.text``. Either way we just need a
        # quick "is there any non-whitespace content here?" check.
        if isinstance(child.tag, str):
            return "".join(child.itertext())
        return child.text or ""

    def flush_inline_run() -> None:
        nonlocal pending_text, pending_inlines, inserted
        if not pending_text.strip() and not any(
            _child_textual(c).strip() for c in pending_inlines
        ):
            # Nothing meaningful to wrap. Preserve any trivial
            # whitespace by attaching it to the previous block's tail
            # so the file's line-by-line shape stays close to the
            # original XHTML — pure cosmetics, but the byte-level diff
            # against the source stays smaller.
            if pending_text and new_top_level:
                last = new_top_level[-1]
                last.tail = (last.tail or "") + pending_text
            elif pending_text and not new_top_level:
                # leading whitespace before any block: drop it; the
                # parent's ``.text`` was already cleared and we cannot
                # safely re-set it without confusing the wrapper logic.
                pass
            pending_text = ""
            pending_inlines = []
            return
        wrapper = etree.Element(wrapper_qtag, nsmap=parent.nsmap)
        if parent_class:
            wrapper.set("class", parent_class)
        wrapper.set("data-epublate-orphan", "1")
        wrapper.text = pending_text or None
        for child in pending_inlines:
            wrapper.append(child)
        new_top_level.append(wrapper)
        inserted += 1
        pending_text = ""
        pending_inlines = []

    for child in children:
        is_block = (
            isinstance(child.tag, str)
            and _local_name(str(child.tag)) in _BLOCK_HOST_TAGS
        )
        if is_block:
            flush_inline_run()
            tail = child.tail
            child.tail = None
            new_top_level.append(child)
            pending_text = tail or ""
        else:
            pending_inlines.append(child)

    flush_inline_run()

    for child in list(parent):
        parent.remove(child)
    parent.text = None
    for child in new_top_level:
        parent.append(child)

    return inserted


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
        # Lift orphaned inline runs in mixed-content blocks into their
        # own ``<div>`` hosts BEFORE selecting hosts, so the LLM gets a
        # shot at every visible piece of prose. The transform is
        # deterministic and idempotent so the same XPath survives a
        # re-load at export time (where this exact pass runs again on
        # the freshly-loaded original ePub before reassembly).
        _hoist_orphaned_inline_runs(doc.tree)
        segments: list[Segment] = []
        for host in _find_translatable_hosts(doc.tree, target_lang=self.target_lang):
            source_text, skeleton = placeholderize(host)
            # ``<p>&#160;</p>``, ``<p><img/></p>``, ``<p><a>&#160;</a></p>``
            # and friends placeholderize to text that is wholly placeholders
            # plus invisible glue (NBSP, BOM, zero-width spaces). They
            # round-trip fine when left untouched in the DOM, so segmenting
            # them just bloats the queue and confuses the Reader (PRD §4.6).
            if is_trivially_empty(source_text):
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
        # Re-run the orphan hoist so the synthetic wrappers exist on
        # the freshly-loaded original tree before any XPath lookup. The
        # transform is idempotent — if segmentation already created
        # them in this process, this is a no-op.
        _hoist_orphaned_inline_runs(doc.tree)
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
                (
                    _restore_outer_whitespace(seg.source_text, seg.target_text)
                    if seg.target_text is not None
                    else seg.source_text,
                    seg.inline_skeleton,
                )
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
