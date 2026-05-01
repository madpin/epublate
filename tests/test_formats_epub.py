"""ePub adapter round-trip tests (PRD §10 / M1 acceptance).

The format-handling rule states that for any unmodified segment,
``adapter.segment`` followed by ``adapter.reassemble`` plus a save/reload
must reproduce the original segment byte-equivalently. We test both:

* the programmatic ``tiny_epub_factory`` (covers exotic tag combinations);
* the committed CC0 ``tests/fixtures/sample.epub`` (covers ebooklib's real
  emit/parse pipeline).
"""

from __future__ import annotations

import zipfile
from collections.abc import Callable
from pathlib import Path

import pytest
from lxml import etree

from epublate.formats.base import Segment
from epublate.formats.epub import DC_NS, EpubAdapter


def _segments_for(adapter: EpubAdapter, path: Path) -> list[Segment]:
    book = adapter.load(path)
    out: list[Segment] = []
    for doc in adapter.iter_chapters(book):
        if doc.tree is None:
            continue
        out.extend(adapter.segment(doc, chapter_id=f"ch-{doc.spine_idx}"))
    return out


def _structural_segments_for(path: Path) -> list[Segment]:
    """Re-segment a saved ePub without the target-language filter.

    The round-trip tests want to compare segment *structure* before
    and after a save. After save the chapter ``<html lang>`` is set
    to the target language (we genuinely advertise the file as such),
    so re-segmenting with ``target_lang=`` set would correctly skip
    every block as already-translated and the comparison would only
    ever pass by accident. Drop the filter on the after-side so the
    test compares what it claims to compare.
    """

    return _segments_for(EpubAdapter(target_lang=None), path)


def _segment_signature(seg: Segment) -> tuple[str, tuple[tuple[str, str], ...]]:
    """A comparable signature: source_text + skeleton (tag, kind) pairs.

    Attribute dicts can drift across ebooklib's namespace handling (it adds
    ``xmlns:epub`` etc. on save) without changing translatable content; this
    signature is stable across those transforms.
    """

    skeleton = tuple((t.tag, t.kind) for t in seg.inline_skeleton)
    return seg.source_text, skeleton


def test_round_trip_on_programmatic_epub(
    tiny_epub_factory: Callable[..., Path], tmp_path: Path
) -> None:
    src = tiny_epub_factory(
        chapters=[
            (
                "One",
                "<h1>One</h1>"
                "<p>Hello, <em>brave</em> world.</p>"
                "<p>A second paragraph with <strong>strong words</strong>.</p>",
            ),
            (
                "Two",
                "<h1>Two</h1>"
                '<p>Anchors: <a href="#x">link</a>; '
                "voids: line<br/>break.</p>",
            ),
        ]
    )
    adapter = EpubAdapter(target_lang="pt")
    book = adapter.load(src)
    book.extras["target_lang"] = "pt"
    before = []
    for doc in adapter.iter_chapters(book):
        if doc.tree is None:
            continue
        segs = adapter.segment(doc, chapter_id=f"ch-{doc.spine_idx}")
        before.extend(segs)
        adapter.reassemble(doc, segs)
    out = tmp_path / "out.epub"
    adapter.save(book, out)

    after = _structural_segments_for(out)
    assert [_segment_signature(s) for s in after] == [
        _segment_signature(s) for s in before
    ]


def test_save_updates_dc_language_and_provenance(
    tiny_epub_factory: Callable[..., Path], tmp_path: Path
) -> None:
    src = tiny_epub_factory()
    adapter = EpubAdapter(target_lang="pt")
    book = adapter.load(src)
    book.extras["target_lang"] = "pt"
    for doc in adapter.iter_chapters(book):
        if doc.tree is None:
            continue
        segs = adapter.segment(doc, chapter_id=f"ch-{doc.spine_idx}")
        adapter.reassemble(doc, segs)
    out = tmp_path / "out.epub"
    adapter.save(book, out)

    reloaded = adapter.load(out)
    languages = reloaded.container.get_metadata("DC", "language")
    assert [v for v, _ in languages] == ["pt"]
    contributors = reloaded.container.get_metadata("DC", "contributor")
    assert any(v.startswith("epublate ") for v, _ in contributors), (
        f"missing epublate provenance, got {contributors!r}"
    )


def test_save_is_atomic(
    tiny_epub_factory: Callable[..., Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    src = tiny_epub_factory()
    adapter = EpubAdapter(target_lang="pt")
    book = adapter.load(src)
    book.extras["target_lang"] = "pt"
    for doc in adapter.iter_chapters(book):
        if doc.tree is None:
            continue
        segs = adapter.segment(doc, chapter_id=f"ch-{doc.spine_idx}")
        adapter.reassemble(doc, segs)

    out = tmp_path / "out.epub"

    from ebooklib import epub as eb

    def _boom(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("simulated write failure")

    monkeypatch.setattr(eb, "write_epub", _boom)
    with pytest.raises(RuntimeError):
        adapter.save(book, out)

    # No half-written ePub at the destination, no leftover .tmp file.
    assert not out.exists()
    assert not out.with_suffix(out.suffix + ".tmp").exists()


def test_round_trip_on_sample_fixture(sample_epub_path: Path, tmp_path: Path) -> None:
    adapter_in = EpubAdapter(target_lang="pt")
    book = adapter_in.load(sample_epub_path)
    book.extras["target_lang"] = "pt"
    before: list[Segment] = []
    for doc in adapter_in.iter_chapters(book):
        if doc.tree is None:
            continue
        segs = adapter_in.segment(doc, chapter_id=f"ch-{doc.spine_idx}")
        before.extend(segs)
        adapter_in.reassemble(doc, segs)
    out = tmp_path / "sample-out.epub"
    adapter_in.save(book, out)

    after = _structural_segments_for(out)
    assert [_segment_signature(s) for s in after] == [
        _segment_signature(s) for s in before
    ]
    # The fixture has at least one inline pair (em / strong / a).
    assert any(s.inline_skeleton for s in before)


def test_skip_target_lang_segments(
    tiny_epub_factory: Callable[..., Path],
) -> None:
    src = tiny_epub_factory(
        chapters=[
            (
                "Mixed",
                '<h1>Mixed</h1><p xml:lang="pt">Olá, mundo.</p><p>Hello again.</p>',
            )
        ],
        language="en",
    )
    adapter = EpubAdapter(target_lang="pt")
    segs = _segments_for(adapter, src)
    sources = [s.source_text for s in segs]
    assert "Hello again." in sources
    assert all("Olá" not in s for s in sources)


def test_dc_namespace_constant_matches_ebooklib() -> None:
    # If ebooklib ever changes the DC namespace constant, the adapter's
    # ``_replace_dc_metadata`` would silently no-op. Lock it down.
    assert DC_NS == "http://purl.org/dc/elements/1.1/"


def test_segment_skips_image_only_paragraphs(
    tiny_epub_factory: Callable[..., Path],
) -> None:
    """A ``<p><img/></p>`` block placeholderizes to ``[[T0]]`` only and
    has nothing to translate; the adapter must skip it so the curator's
    queue isn't bloated by structurally-empty hosts (PRD §4.6)."""

    src = tiny_epub_factory(
        chapters=[
            (
                "Mixed",
                "<h1>Mixed</h1>"
                '<p><img src="cover.png" alt="Cover"/></p>'
                "<p>Real text here.</p>",
            )
        ]
    )
    adapter = EpubAdapter(target_lang="pt")
    segs = _segments_for(adapter, src)
    sources = [s.source_text for s in segs]
    # The image-only paragraph must not produce a segment.
    assert all("[[T0]]" not in s or s.strip() != "[[T0]]" for s in sources)
    assert any("Real text here." in s for s in sources)


def test_segment_skips_empty_inline_pair_only_paragraphs(
    tiny_epub_factory: Callable[..., Path],
) -> None:
    """``<p><em></em></p>`` placeholderizes to ``[[T0]][[/T0]]``: still
    no human-readable text, still must be skipped (M2 reader UX fix)."""

    src = tiny_epub_factory(
        chapters=[
            (
                "Mixed",
                "<h1>Mixed</h1><p><em></em></p><p>Hello.</p>",
            )
        ]
    )
    adapter = EpubAdapter(target_lang="pt")
    segs = _segments_for(adapter, src)
    assert all(s.source_text != "[[T0]][[/T0]]" for s in segs)


def test_segment_treats_leaf_div_and_section_as_paragraph_hosts(
    tiny_epub_factory: Callable[..., Path],
) -> None:
    """Calibre-converted ePubs wrap paragraph content in ``<div>`` rather
    than ``<p>``; HTML5-native ePubs frequently nest ``<section>``s.
    Both must produce one segment per leaf paragraph host so chapter
    bodies aren't silently dropped by the Reader (PRD §4.6 / F-IO-4).
    """

    src = tiny_epub_factory(
        chapters=[
            (
                "Calibre-style",
                "<h1>Calibre-style</h1>"
                '<div class="calibre19">&#160;</div>'
                '<div class="calibre22"><span class="calibre10">'
                "First paragraph wrapped in a div."
                "</span></div>"
                '<div class="calibre22"><span class="calibre10">'
                "Second paragraph also in a div."
                "</span></div>",
            ),
            (
                "Sectioned",
                "<section>"
                "<h2>Inner</h2>"
                "<section>"
                "<p>Para inside nested section.</p>"
                "</section>"
                "</section>",
            ),
        ]
    )
    adapter = EpubAdapter(target_lang="pt")
    segs = _segments_for(adapter, src)
    sources = [s.source_text for s in segs]
    assert any("First paragraph wrapped in a div." in s for s in sources), sources
    assert any("Second paragraph also in a div." in s for s in sources), sources
    assert any("Para inside nested section." in s for s in sources), sources
    # The wrapper section must not also be emitted as its own segment;
    # only the innermost block host should be.
    assert all("Para inside nested section.\nInner" not in s for s in sources)


def test_iter_chapters_backfills_title_from_first_heading(
    tiny_epub_factory: Callable[..., Path],
) -> None:
    """When ebooklib doesn't surface a spine title, the adapter must
    derive one from the chapter's first heading so the Reader status
    bar displays a meaningful chapter name."""

    src = tiny_epub_factory(
        chapters=[
            (
                "",  # empty OPF title — common in real-world ePubs
                "<h1>The Beginning</h1><p>Once upon a time.</p>",
            )
        ]
    )
    adapter = EpubAdapter(target_lang="pt")
    book = adapter.load(src)
    docs = list(adapter.iter_chapters(book))
    body_docs = [d for d in docs if "nav" not in d.href]
    assert any(d.title == "The Beginning" for d in body_docs)


def test_iter_chapters_extracts_calibre_styled_heading(
    tiny_epub_factory: Callable[..., Path],
) -> None:
    """Calibre converts headings to ``<div><span class='bold'>…</span></div>``;
    the adapter falls back to that pattern when no ``h1``/``h2``/``h3``
    is present so the Reader sidebar shows the real chapter name
    instead of an empty title."""

    body = (
        '<p class="calibre1"><a class="calibre2"></a></p>'
        '<div class="calibre14">'
        '<span class="calibre6"><span class="bold">Introduction</span></span>'
        "</div>"
        '<div class="calibre19">&#160;</div>'
        '<div class="calibre3"><span class="calibre6">'
        "Real prose body text here."
        "</span></div>"
    )
    src = tiny_epub_factory(chapters=[("", body)])
    adapter = EpubAdapter(target_lang="pt")
    book = adapter.load(src)
    docs = list(adapter.iter_chapters(book))
    body_docs = [d for d in docs if "nav" not in d.href]
    assert any(d.title == "Introduction" for d in body_docs), [
        d.title for d in body_docs
    ]


def test_iter_chapters_calibre_fallback_joins_chapter_number_and_title(
    tiny_epub_factory: Callable[..., Path],
) -> None:
    """Calibre routinely splits ``chapter number`` and ``chapter title``
    into two consecutive bold-styled divs (e.g. ``"1"`` then
    ``"The Rules of Politics"``). Returning just ``"1"`` would give the
    Reader sidebar a useless label, so the fallback joins them with an
    em-dash when the first match is short enough to be a numeral."""

    body = (
        '<p class="calibre1"><a class="calibre2"></a></p>'
        '<div class="calibre14">'
        '<span class="calibre27"><span class="bold">1</span></span>'
        "</div>"
        '<div class="calibre19">&#160;</div>'
        '<div class="calibre14">'
        '<span class="calibre6"><span class="bold">The Rules of Politics</span></span>'
        "</div>"
        '<div class="calibre19">&#160;</div>'
        '<div class="calibre21"><span class="calibre10">Body prose here.</span></div>'
    )
    src = tiny_epub_factory(chapters=[("", body)])
    adapter = EpubAdapter(target_lang="pt")
    book = adapter.load(src)
    docs = list(adapter.iter_chapters(book))
    body_docs = [d for d in docs if "nav" not in d.href]
    assert any(d.title == "1 \u2014 The Rules of Politics" for d in body_docs), [
        d.title for d in body_docs
    ]


def test_iter_chapters_calibre_fallback_skips_inline_bold_runs(
    tiny_epub_factory: Callable[..., Path],
) -> None:
    """The Calibre heading fallback must not latch onto ``<span class='bold'>``
    used purely for emphasis inside body prose — the heuristic only
    fires for top-of-document elements."""

    # No h1/h2/h3, no head <title> match, AND the bold span is buried
    # several elements deep inside body text. The fallback should
    # decline to guess a title rather than return "important".
    body = (
        "<p>Plain opener with no heading.</p>"
        "<p>Another paragraph still without a heading.</p>"
        "<p>Yet another opener, still none.</p>"
        "<p>And another opener for good measure.</p>"
        "<p>Some more body content without a heading.</p>"
        "<p>One last opener before the bold appears.</p>"
        "<p>Even more padding before the bold marker.</p>"
        "<p>And finally some context before the bold.</p>"
        '<p>This is <span class="bold">important</span> stuff.</p>'
    )
    src = tiny_epub_factory(chapters=[("", body)])
    adapter = EpubAdapter(target_lang="pt")
    book = adapter.load(src)
    docs = list(adapter.iter_chapters(book))
    body_docs = [d for d in docs if "nav" not in d.href]
    assert all(d.title != "important" for d in body_docs), [d.title for d in body_docs]


def test_toc_title_map_walks_nested_navpoints(
    tiny_epub_factory: Callable[..., Path],
) -> None:
    """``toc_title_map`` returns ``{href -> title}`` from the EPUB TOC,
    flattening nested navpoints and stripping anchors so the keys
    line up with what ``iter_chapters`` yields."""

    from epublate.formats.epub import toc_title_map

    src = tiny_epub_factory(
        chapters=[
            ("Front Matter", "<h1>Front</h1><p>Stuff.</p>"),
            ("Chapter One", "<h1>Chapter One</h1><p>Stuff.</p>"),
        ]
    )
    adapter = EpubAdapter(target_lang="pt")
    book = adapter.load(src)
    titles = toc_title_map(book)
    assert any("Front" in v for v in titles.values()), titles
    assert any("Chapter One" in v for v in titles.values()), titles


_HANDCRAFTED_CONTAINER = (
    b'<?xml version="1.0" encoding="UTF-8"?>'
    b'<container version="1.0" '
    b'xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
    b"<rootfiles>"
    b'<rootfile full-path="OEBPS/content.opf" '
    b'media-type="application/oebps-package+xml"/>'
    b"</rootfiles></container>"
)

_HANDCRAFTED_OPF = (
    b'<?xml version="1.0" encoding="utf-8"?>'
    b'<package xmlns="http://www.idpf.org/2007/opf" '
    b'unique-identifier="bookid" version="2.0">'
    b'<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
    b'<dc:identifier id="bookid">urn:epublate:test:cover</dc:identifier>'
    b"<dc:title>Cover Test</dc:title>"
    b"<dc:language>en</dc:language>"
    b"</metadata>"
    b"<manifest>"
    b'<item id="css" href="Styles/stylesheet.css" media-type="text/css"/>'
    b'<item id="ch1" href="Text/cover.xhtml" '
    b'media-type="application/xhtml+xml"/>'
    b'<item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>'
    b"</manifest>"
    b'<spine toc="ncx"><itemref idref="ch1"/></spine>'
    b"</package>"
)

_HANDCRAFTED_NCX = (
    b'<?xml version="1.0" encoding="UTF-8"?>'
    b'<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">'
    b"<head>"
    b'<meta name="dtb:uid" content="urn:epublate:test:cover"/>'
    b'<meta name="dtb:depth" content="1"/>'
    b'<meta name="dtb:totalPageCount" content="0"/>'
    b'<meta name="dtb:maxPageNumber" content="0"/>'
    b"</head>"
    b"<docTitle><text>Cover Test</text></docTitle>"
    b"<navMap>"
    b'<navPoint id="np-1" playOrder="1">'
    b"<navLabel><text>Cover</text></navLabel>"
    b'<content src="Text/cover.xhtml"/>'
    b"</navPoint>"
    b"</navMap></ncx>"
)

_HANDCRAFTED_CHAPTER = (
    b'<?xml version="1.0" encoding="utf-8"?>'
    b'<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.1//EN" '
    b'"http://www.w3.org/TR/xhtml11/DTD/xhtml11.dtd">'
    b'<html xmlns="http://www.w3.org/1999/xhtml">'
    b"<head>"
    b"<title>Cover Page</title>"
    b'<link href="../Styles/stylesheet.css" rel="stylesheet" '
    b'type="text/css"/>'
    b'<style type="text/css">'
    b"@page { margin-bottom: 5pt; margin-top: 5pt; }"
    b"</style>"
    b"</head>"
    b'<body class="calibre">'
    b'<div class="calibre3">'
    b'<img alt="cover" class="calibre4" src="../Images/cover.png"/>'
    b"</div>"
    b"<p>Some translatable prose so the chapter has at least one segment.</p>"
    b"</body></html>"
)

_HANDCRAFTED_CSS = b".calibre3 { text-align: center; }\n.calibre4 { width: auto; }\n"

# 1x1 transparent PNG so the manifest has a real binary asset to round-trip.
_PNG_BYTES = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000d49444154789c6300010000000500010d0a2db40000000049454e44ae42"
    "6082"
)


def _write_handcrafted_epub(out: Path) -> Path:
    """Build an ePub by hand so the chapter ``<head>`` is fully under
    our control. ebooklib's writer rebuilds heads from
    ``item.title``/``metas``/``links`` and would silently drop the
    ``<link>`` / ``<style>`` we want to round-trip — we'd be testing
    the writer, not the adapter."""

    out.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("mimetype", "application/epub+zip", zipfile.ZIP_STORED)
        zf.writestr("META-INF/container.xml", _HANDCRAFTED_CONTAINER)
        zf.writestr("OEBPS/content.opf", _HANDCRAFTED_OPF)
        zf.writestr("OEBPS/toc.ncx", _HANDCRAFTED_NCX)
        zf.writestr("OEBPS/Text/cover.xhtml", _HANDCRAFTED_CHAPTER)
        zf.writestr("OEBPS/Styles/stylesheet.css", _HANDCRAFTED_CSS)
        zf.writestr("OEBPS/Images/cover.png", _PNG_BYTES)
    return out


def _read_chapter_xml(epub_path: Path, chapter_name: str) -> bytes:
    """Read the raw bytes of a chapter file out of the ePub zip.

    ebooklib relocates manifest items into its ``EPUB/`` folder on
    save, so we search by basename rather than expecting the original
    path to survive.
    """

    with zipfile.ZipFile(epub_path) as zf:
        for info in zf.infolist():
            if info.filename.endswith("/" + chapter_name):
                return zf.read(info.filename)
    raise FileNotFoundError(f"{chapter_name} not in {epub_path}")


def test_save_preserves_original_chapter_head(tmp_path: Path) -> None:
    """Format-preservation invariant (PRD §4.1 / F-IO-1).

    Real-world ePubs (Calibre conversions, Sigil exports, etc.) put a
    stylesheet ``<link>`` and an inline ``<style>`` in each chapter's
    ``<head>``. Calibre cover pages center/size the cover image via
    those CSS rules — drop them and the cover renders unstyled and
    looks cropped on every reader. Round-trip those head children
    through a save and reload; if any are missing the writer is
    re-emitting via ebooklib's chapter template (which builds heads
    from ``item.title``/``metas``/``links`` and discards the rest)
    and the regression has returned.
    """

    src = _write_handcrafted_epub(tmp_path / "in.epub")
    adapter = EpubAdapter(target_lang="pt")
    book = adapter.load(src)
    book.extras["target_lang"] = "pt"
    for doc in adapter.iter_chapters(book):
        if doc.tree is None:
            continue
        segs = adapter.segment(doc, chapter_id=f"ch-{doc.spine_idx}")
        adapter.reassemble(doc, segs)

    out = tmp_path / "out.epub"
    adapter.save(book, out)

    chapter_xml = _read_chapter_xml(out, "cover.xhtml")
    parser = etree.XMLParser(resolve_entities=False, no_network=True)
    root = etree.fromstring(chapter_xml, parser)

    xhtml_ns = "http://www.w3.org/1999/xhtml"
    head = root.find(f"{{{xhtml_ns}}}head")
    assert head is not None, "chapter must keep a <head>"

    links = head.findall(f"{{{xhtml_ns}}}link")
    assert any(
        link.get("href", "").endswith("stylesheet.css")
        and link.get("rel") == "stylesheet"
        for link in links
    ), (
        "stylesheet <link> was stripped from the chapter head — the "
        "cover image will lose its centering/sizing rules and render "
        "cropped"
    )

    styles = head.findall(f"{{{xhtml_ns}}}style")
    style_text = "".join(s.text or "" for s in styles)
    assert "@page" in style_text, "inline <style> @page rule was stripped"

    titles = head.findall(f"{{{xhtml_ns}}}title")
    assert any((t.text or "").strip() == "Cover Page" for t in titles), (
        "original <title> was lost on save"
    )

    # The image must still be in the body (the actual translatable
    # cover-page content), and the body's translatable prose must
    # have its source text preserved (untranslated => fallback per
    # F-IO-7).
    body = root.find(f"{{{xhtml_ns}}}body")
    assert body is not None
    body_text = etree.tostring(body, encoding="unicode")
    assert 'src="../Images/cover.png"' in body_text
    assert "translatable prose" in body_text


def test_save_updates_html_lang_on_chapter_root(tmp_path: Path) -> None:
    """``<html lang>`` and ``xml:lang`` must reflect the target
    language after export. We bypass ebooklib's chapter template (so
    its ``item.lang`` no longer reaches the file) — the adapter has
    to set both attributes on the parsed tree itself."""

    src = _write_handcrafted_epub(tmp_path / "in.epub")
    adapter = EpubAdapter(target_lang="pt")
    book = adapter.load(src)
    book.extras["target_lang"] = "pt"
    for doc in adapter.iter_chapters(book):
        if doc.tree is None:
            continue
        adapter.segment(doc, chapter_id=f"ch-{doc.spine_idx}")

    out = tmp_path / "out.epub"
    adapter.save(book, out)

    chapter_xml = _read_chapter_xml(out, "cover.xhtml")
    parser = etree.XMLParser(resolve_entities=False, no_network=True)
    root = etree.fromstring(chapter_xml, parser)
    xml_ns = "http://www.w3.org/XML/1998/namespace"
    assert root.get("lang") == "pt"
    assert root.get(f"{{{xml_ns}}}lang") == "pt"
