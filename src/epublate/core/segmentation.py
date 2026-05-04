"""Reversible XHTML ↔ placeholder text conversion (PRD §4.1, §6.5).

This module is the single source of truth for the format-handling invariant:
inline tags become opaque ``[[T0]]…[[/T0]]`` placeholders before any LLM call
and are restored byte-equivalently on reassembly. It is intentionally
format-agnostic so the future PDF adapter can reuse it.

The public surface:

* :func:`count_tokens` — coarse heuristic used during segmentation. M2 swaps
  this for ``tiktoken`` once the OpenAI-compatible provider lands.
* :func:`placeholderize` — extract a block-level lxml element's inner content
  into a placeholder-bearing string plus a skeleton.
* :func:`apply_parts_to_host` — splice one or more ``(source_text, skeleton)``
  parts back into a block-level lxml element, replacing its current children.
* :func:`split_by_sentences` — split a long ``source_text`` into chunks under
  a token budget without ever cutting through a placeholder pair.

Hard rules enforced here:

1. ``apply_parts_to_host(host, [placeholderize(host)])`` is identity on the
   host's serialized form. Property-tested with ``hypothesis``.
2. Every placeholder issued in the source must have a matching closing
   placeholder for ``pair`` tokens; ``void`` tokens carry no closer. A
   mismatch raises :class:`~epublate.errors.FormatError`.
3. Sentence splits never land inside a placeholder pair.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import TYPE_CHECKING

from epublate.errors import FormatError
from epublate.formats.base import InlineToken

if TYPE_CHECKING:  # lxml is heavy; keep static-only at module load time
    from lxml import etree

PLACEHOLDER_RE = re.compile(r"\[\[(/?)T(\d+)\]\]")

# Zero-width / formatting code points that ``str.isspace()`` treats as
# *non*-whitespace but that carry no translatable content. Stripping them
# alongside Unicode whitespace lets us detect "this segment is just an
# invisible glue char" before paying for an LLM call (PRD F-LLM-7).
# Common offenders in real-world ePubs: stray ZWSP/ZWNJ from copy-paste,
# BOM left over from Windows-1252 → UTF-8 round-trips, WORD JOINER as a
# soft separator. Keep the set conservative — anything *visible* (combining
# marks, non-ASCII letters) must stay translatable.
_INVISIBLE_FORMATTING_RE = re.compile(r"[\u200b\u200c\u200d\u2060\ufeff]")

# HTML void elements: they never have child content or a closing tag in
# well-formed XHTML. Anything else is treated as a "pair" token.
VOID_LOCAL_NAMES: frozenset[str] = frozenset(
    {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }
)


def count_tokens(text: str) -> int:
    """Cheap, deterministic token estimate (≈ chars / 4).

    Real tokenization arrives in M2 with the OpenAI-compatible provider. The
    M1 segmenter only needs a monotonic, side-effect-free estimate so the
    sentence splitter stays deterministic across platforms.
    """

    return max(1, len(text) // 4)


def is_trivially_empty(source_text: str) -> bool:
    """True if ``source_text`` carries no content worth sending to the LLM.

    A segment is "trivially empty" when, after stripping every inline-tag
    placeholder and every Unicode whitespace + zero-width formatting
    code point, nothing remains. Examples that match:

    * ``""`` — a wholly empty string.
    * ``" \u00a0\t"`` — a paragraph with only spacing (the classic
      ``<p>&#160;</p>`` separator from Calibre-converted ePubs).
    * ``"[[T0]]\u00a0[[/T0]]"`` — a link wrapping just a non-breaking
      space (e.g. an empty TOC anchor).
    * ``"\ufeff\u200b"`` — leftover BOM / zero-width space.

    Examples that do *not* match (these still get translated):

    * ``"1"`` — a chapter number is short but real text.
    * ``"—"`` — a dash separator is real punctuation.
    * ``"[[T0]]Title[[/T0]]"`` — a link with text inside.

    The ePub segmenter uses this to keep noise out of the segment table at
    intake time; the translation pipeline uses it as a defense-in-depth
    check so projects segmented before this filter existed still avoid
    spurious LLM round-trips on ``&nbsp;``-only paragraphs.
    """

    if not source_text:
        return True
    stripped = PLACEHOLDER_RE.sub("", source_text)
    stripped = _INVISIBLE_FORMATTING_RE.sub("", stripped)
    return not stripped.strip()


def _local_name(tag: str) -> str:
    """Strip Clark-notation namespace from a tag name (``{ns}foo`` → ``foo``)."""

    if tag.startswith("{"):
        return tag.split("}", 1)[1]
    return tag


def _is_void(tag: str) -> bool:
    return _local_name(tag).lower() in VOID_LOCAL_NAMES


# Marker prefix we put on the ``InlineToken.tag`` of a "entity" token so
# the rebuilder can tell entity tokens apart from regular tag names.
# The format is ``"&name;"`` (the entity reference exactly as it would
# appear in the source XHTML) so a quick visual scan of a stored
# segment row makes the round-trip intent obvious. Real XML element
# names cannot start with ``&`` so this prefix is unambiguous.
_ENTITY_TAG_PREFIX: str = "&"


# Named XHTML entities the segmenter expands inline to their literal
# Unicode characters instead of emitting a placeholder + entity token.
#
# Why expand at all: an entity placeholder forces the LLM to preserve
# ``[[T0]]`` exactly once in the target, but several of these entities
# encode typography the *target* language renders differently:
#
# * French elision apostrophes (``j&rsquo;ai``, ``d&rsquo;avoir``,
#   ``qu&rsquo;a``) become ``[[T0]]`` placeholders that the
#   translator prompt then asks the model to drop entirely when going
#   to Portuguese / Spanish / German (cf. ``_SOURCE_LANG_NOTES['fr']``
#   in :mod:`epublate.llm.prompts.translator`). The model honors the
#   typography rule, drops ``[[T0]]``, and the structural validator
#   hard-fails ``"entity placeholder [[T0]] missing or duplicated"``.
# * The French narrow non-breaking space before ``:`` / ``!`` / ``?``
#   (``&nbsp;`` in older converters, U+202F in modern ones) becomes
#   a ``[[Tn]]`` the LLM legitimately drops when the target language
#   does not insert a space before the colon.
# * Curly quotes, em/en dashes, and ellipses are pure punctuation:
#   forcing the model to "preserve" a placeholder for them in a
#   re-translation often loses the placeholder and trips the validator.
#
# Expanding these to their Unicode chars puts them in the same plane
# as the rest of the prose: the LLM sees ``j'ai`` (or ``J'ai``) and
# translates to ``Tenho``, the typography sanitiser cleans up any
# stranded leading apostrophes (``core/typography.py``), and there is
# no placeholder to enforce. Reassembly emits the literal U+2019
# character instead of ``&rsquo;`` for unmodified segments — visually
# identical in any reader, and the format-handling round-trip
# property still holds at the *character* level (it only weakens the
# byte-identical-with-named-entity invariant for these specific
# characters).
#
# The whitelist is intentionally narrow:
#
# * Only entities whose target Unicode code point is **text content**
#   (apostrophes, quotes, dashes, ellipsis, whitespace, plus the XML
#   ``&amp;``). Symbols like ``&copy;`` / ``&trade;`` / ``&deg;`` /
#   ``&reg;`` / ``&para;`` / ``&sect;`` / ``&shy;`` keep the entity
#   placeholder treatment because (a) the LLM does not drop them as
#   part of typography normalization and (b) they're rare enough in
#   prose that round-trip identity is the more useful contract.
# * ``&lt;`` and ``&gt;`` are kept as entities so a literal ``<`` or
#   ``>`` never reaches the LLM as bare text — the model would read
#   them as malformed tag markup and could mis-translate around them.
#
# Adding an entity to this set is a one-key change. Keep the
# Unicode chars literal in the source (escaped via ``\u`` for the
# truly invisible ones) so a quick scan of the dict is enough to
# audit what's in scope.
_TEXT_ENTITY_EXPANSIONS: dict[str, str] = {
    # Apostrophes and quotes.
    "apos": "'",
    "quot": '"',
    "lsquo": "\u2018",
    "rsquo": "\u2019",
    "ldquo": "\u201c",
    "rdquo": "\u201d",
    "sbquo": "\u201a",
    "bdquo": "\u201e",
    "laquo": "\u00ab",
    "raquo": "\u00bb",
    "prime": "\u2032",
    "Prime": "\u2033",
    # Dashes and ellipsis.
    "ndash": "\u2013",
    "mdash": "\u2014",
    "horbar": "\u2015",
    "hellip": "\u2026",
    # Whitespace variants. Expanding ``&nbsp;`` is the load-bearing
    # case for French source ePubs that put a narrow space before
    # colons / semi-colons / question marks; the LLM will legitimately
    # drop the space when translating to a language that doesn't use
    # the convention, and the validator must not fail on that.
    "nbsp": "\u00a0",
    "ensp": "\u2002",
    "emsp": "\u2003",
    "thinsp": "\u2009",
    "hairsp": "\u200a",
    "numsp": "\u2007",
    "puncsp": "\u2008",
    # Standard XML predefined. ``&lt;`` / ``&gt;`` are intentionally
    # excluded — see the docstring above.
    "amp": "&",
}


def _is_real_element(child: object) -> bool:
    """True iff ``child`` is a parsed Element node we can splice as an inline tag.

    lxml exposes Comment / Processing-Instruction / Entity-Reference
    nodes through the same iteration protocol as Element nodes, but
    their ``.tag`` is a *Cython function* (``etree.Comment`` etc.),
    not a string. Stringifying that cython object used to slip in here
    via ``str(child.tag)``, producing absurd tag names like
    ``"<cyfunction Entity at 0x108cefad0>"`` that crashed the
    reassembler at export time. We now route those nodes through
    dedicated helpers (or skip them) instead of letting them inherit
    the regular element path.
    """

    from lxml import etree

    if not etree.iselement(child):
        return False
    return isinstance(child.tag, str)  # type: ignore[attr-defined]


def _entity_name_from_node(child: etree._Element) -> str:
    """Extract the entity name from an lxml entity-reference node.

    lxml stores the literal reference (e.g. ``"&nbsp;"``) on
    ``child.text``. We strip the surrounding ``&`` / ``;`` to get the
    bare name; a guard pass keeps us safe against unexpected text
    shapes (we'd rather emit an empty entity ref than crash export).
    """

    raw = child.text or ""
    if raw.startswith("&") and raw.endswith(";"):
        raw = raw[1:-1]
    return raw


def placeholderize(host: etree._Element) -> tuple[str, list[InlineToken]]:
    """Convert a block-level element's inner content to placeholder text.

    The returned ``source_text`` is what the LLM eventually sees; the skeleton
    is the ordered list of inline tokens those placeholders refer to. The
    host element itself is unchanged.
    """

    parts: list[str] = []
    skeleton: list[InlineToken] = []

    if host.text:
        parts.append(host.text)
    for child in host.iterchildren():
        _emit_child(child, parts, skeleton)

    return "".join(parts), skeleton


def _emit_child(
    child: etree._Element,
    parts: list[str],
    skeleton: list[InlineToken],
) -> None:
    if not _is_real_element(child):
        _emit_non_element_child(child, parts, skeleton)
        return
    tag = str(child.tag)
    attrs = {str(k): str(v) for k, v in child.attrib.items()}
    my_idx = len(skeleton)
    if _is_void(tag):
        skeleton.append(InlineToken(tag=tag, kind="void", attrs=attrs))
        parts.append(f"[[T{my_idx}]]")
    else:
        skeleton.append(InlineToken(tag=tag, kind="pair", attrs=attrs))
        parts.append(f"[[T{my_idx}]]")
        if child.text:
            parts.append(child.text)
        for grandchild in child.iterchildren():
            _emit_child(grandchild, parts, skeleton)
        parts.append(f"[[/T{my_idx}]]")
    if child.tail:
        parts.append(child.tail)


def _emit_non_element_child(
    child: etree._Element,
    parts: list[str],
    skeleton: list[InlineToken],
) -> None:
    """Handle entity-reference / comment / PI children inside a host.

    Entity references (`&nbsp;`, `&copy;`, ...) are preserved as
    void-style placeholders so the exporter can rebuild the original
    reference verbatim instead of emitting a literal Unicode char.
    Comments and processing instructions are dropped here — they don't
    affect rendered text and including them would force the LLM to
    reason about non-content nodes.
    """

    from lxml import etree

    tag = child.tag
    if tag is etree.Entity:
        name = _entity_name_from_node(child)
        # Typographic entities (apostrophes, dashes, ellipsis, NBSP,
        # …) get expanded to their literal Unicode characters here so
        # the LLM never sees a ``[[Tn]]`` placeholder it would
        # legitimately drop as part of target-language typography
        # normalization — see :data:`_TEXT_ENTITY_EXPANSIONS`.
        expansion = _TEXT_ENTITY_EXPANSIONS.get(name)
        if expansion is not None:
            parts.append(expansion)
        else:
            my_idx = len(skeleton)
            skeleton.append(
                InlineToken(
                    tag=f"{_ENTITY_TAG_PREFIX}{name};",
                    kind="entity",
                    attrs={"name": name},
                )
            )
            parts.append(f"[[T{my_idx}]]")
    if child.tail:
        parts.append(child.tail)


def apply_parts_to_host(
    host: etree._Element,
    parts: Sequence[tuple[str, Sequence[InlineToken]]],
) -> None:
    """Replace ``host``'s contents with one or more text+skeleton parts.

    Each ``(text, skeleton)`` is independent: its placeholders are numbered
    starting from ``0`` and resolve only against its own skeleton. This makes
    it safe to splice translations that were chunked by
    :func:`split_by_sentences` and renumbered.
    """

    from lxml import etree

    for child in list(host):
        host.remove(child)
    host.text = None

    # Track where the next text chunk should land. Initially it goes to the
    # host's ``.text``. After a child is appended at depth 0, it goes to that
    # child's ``.tail``. Inside an open pair, it goes to the open child's
    # ``.text`` (until a child is appended, then its ``.tail``).
    stack: list[etree._Element] = [host]
    last_text_target: tuple[etree._Element, str] = (host, "text")

    def _append_text(s: str) -> None:
        elem, attr = last_text_target
        existing = getattr(elem, attr) or ""
        setattr(elem, attr, existing + s)

    for source_text, skeleton in parts:
        pos = 0
        for m in PLACEHOLDER_RE.finditer(source_text):
            text_before = source_text[pos : m.start()]
            if text_before:
                _append_text(text_before)
            is_close = m.group(1) == "/"
            tok_idx = int(m.group(2))
            if tok_idx < 0 or tok_idx >= len(skeleton):
                raise FormatError(
                    f"placeholder index {tok_idx} has no matching skeleton entry"
                )
            token = skeleton[tok_idx]
            if is_close:
                if len(stack) <= 1:
                    raise FormatError(
                        f"closing placeholder [[/T{tok_idx}]] without an open pair"
                    )
                closed = stack.pop()
                last_text_target = (closed, "tail")
            elif token.kind == "entity":
                parent = stack[-1]
                name = token.attrs.get("name") or _strip_entity_brackets(token.tag)
                new_elem = _make_entity_node(name)
                parent.append(new_elem)
                last_text_target = (new_elem, "tail")
            elif _looks_like_legacy_cyfunction_tag(token.tag):
                # Older builds (pre-entity-fix) stored
                # ``"<cyfunction Entity at 0x...>"`` as a token tag when
                # they hit an entity reference. Rebuilding that as an
                # element fails with ``ValueError("Invalid tag name")``
                # at export time and breaks the curator's "Save ePub"
                # flow. Skip the placeholder and keep the surrounding
                # text intact so the rest of the segment still
                # round-trips; re-segmenting the chapter regenerates a
                # clean skeleton with proper entity tokens.
                pass
            else:
                parent = stack[-1]
                new_elem = etree.SubElement(parent, token.tag, dict(token.attrs))
                if token.kind == "pair":
                    stack.append(new_elem)
                    last_text_target = (new_elem, "text")
                else:
                    last_text_target = (new_elem, "tail")
            pos = m.end()
        text_after = source_text[pos:]
        if text_after:
            _append_text(text_after)

    if len(stack) > 1:
        unclosed = ", ".join(str(s.tag) for s in stack[1:])
        raise FormatError(f"placeholder text left open pairs: {unclosed}")


def _strip_entity_brackets(tag: str) -> str:
    """Pull the entity name out of an ``"&name;"``-style token tag."""

    if tag.startswith("&") and tag.endswith(";"):
        return tag[1:-1]
    return tag


def _make_entity_node(name: str) -> etree._Element:
    """Build a fresh ``etree.Entity(name)`` node, robust to malformed input.

    lxml rejects empty entity names with a ``ValueError``; the caller
    already asks for that path on legacy data, so falling back to a
    no-op text-bearing placeholder span keeps the export honest
    rather than crashing it. Real entity references always carry a
    name, so this is purely a defensive fallback.
    """

    from lxml import etree

    try:
        return etree.Entity(name)
    except ValueError:
        return etree.Element("span")


def _looks_like_legacy_cyfunction_tag(tag: str) -> bool:
    """True iff ``tag`` looks like ``str(etree.Entity)`` from old segmenter runs."""

    return tag.startswith("<cyfunction ")


def expand_text_entity_placeholders(
    *,
    source_text: str,
    target_text: str | None,
    skeleton: Sequence[InlineToken],
) -> tuple[str, str | None, list[InlineToken], bool]:
    """Migrate a stored segment to the new text-entity expansion behavior.

    Pure function (no I/O, no DB) used by the
    ``Project.expand_typographic_entities`` migration to bring projects
    segmented before the entity-expansion fix into line with the new
    segmenter without losing translations or curator edits.

    Behavior:

    * Each entity token in ``skeleton`` whose name is in
      :data:`_TEXT_ENTITY_EXPANSIONS` is removed from the skeleton.
      Its ``[[Tk]]`` placeholder in ``source_text`` is replaced with
      the literal Unicode character; the same substitution is applied
      to ``target_text`` (where present) so any translation that
      preserved the placeholder reads cleanly afterwards.
    * Surviving placeholders are renumbered so indices are
      contiguous from 0 again. Closing placeholders ``[[/Tk]]`` are
      renumbered alongside their openers.
    * Placeholders pointing at indices that don't exist in the
      passed-in skeleton (malformed rows, hallucinated indices in a
      target_text) are left intact so the validator can flag them
      downstream.

    Returns ``(new_source, new_target, new_skeleton, changed)``.
    ``changed`` is ``False`` when the skeleton has no expandable
    entity tokens — caller should skip the DB write to keep the
    audit trail quiet.
    """

    expansion_for_idx: dict[int, str] = {}
    new_skeleton: list[InlineToken] = []
    old_to_new_idx: dict[int, int] = {}
    for old_idx, tok in enumerate(skeleton):
        if tok.kind == "entity":
            name = (tok.attrs.get("name") or "").strip().lower()
            if not name:
                name = _strip_entity_brackets(tok.tag).lower()
            expansion = _TEXT_ENTITY_EXPANSIONS.get(name)
            if expansion is not None:
                expansion_for_idx[old_idx] = expansion
                continue
        old_to_new_idx[old_idx] = len(new_skeleton)
        new_skeleton.append(tok)

    if not expansion_for_idx:
        return source_text, target_text, list(skeleton), False

    def _rewrite(text: str) -> str:
        def _sub(m: re.Match[str]) -> str:
            slash = m.group(1)
            old_idx = int(m.group(2))
            if slash:
                # Closing tag — only valid for ``pair`` tokens, never
                # for an entity, so a closer always belongs to a
                # surviving (renumbered) skeleton entry.
                new_idx = old_to_new_idx.get(old_idx)
                if new_idx is None:
                    return m.group(0)
                return f"[[/T{new_idx}]]"
            if old_idx in expansion_for_idx:
                return expansion_for_idx[old_idx]
            new_idx = old_to_new_idx.get(old_idx)
            if new_idx is None:
                return m.group(0)
            return f"[[T{new_idx}]]"

        return PLACEHOLDER_RE.sub(_sub, text)

    new_source = _rewrite(source_text)
    new_target = _rewrite(target_text) if target_text is not None else None
    return new_source, new_target, new_skeleton, True


def split_by_sentences(
    source_text: str,
    skeleton: Sequence[InlineToken],
    *,
    max_tokens: int,
) -> list[tuple[str, list[InlineToken]]]:
    """Split a long source_text into chunks under ``max_tokens``.

    Splits are placed at sentence boundaries (``.``/``!``/``?`` followed by
    whitespace) but only at points where placeholder pair depth is zero —
    i.e. never inside a wrapping inline tag. Returns chunks with
    placeholders **renumbered** starting from ``0`` and a sliced skeleton
    matching each chunk's local indices.

    If the text already fits, returns a single chunk identical to the input.
    If no safe split point exists, returns the input unchanged: the validator
    layer (M3) decides whether to still send it to the LLM or flag it.
    """

    if count_tokens(source_text) <= max_tokens:
        return [(source_text, list(skeleton))]

    safe_points = _safe_split_points(source_text, skeleton)
    if not safe_points:
        return [(source_text, list(skeleton))]

    pieces: list[str] = []
    prev = 0
    for sp in safe_points:
        if sp > prev:
            pieces.append(source_text[prev:sp])
            prev = sp
    if prev < len(source_text):
        pieces.append(source_text[prev:])

    chunks: list[str] = []
    current = ""
    for piece in pieces:
        candidate = current + piece
        if current and count_tokens(candidate) > max_tokens:
            chunks.append(current)
            current = piece
        else:
            current = candidate
    if current:
        chunks.append(current)

    return [_renumber(chunk, skeleton) for chunk in chunks]


def _safe_split_points(source_text: str, skeleton: Sequence[InlineToken]) -> list[int]:
    """Return positions in ``source_text`` where it is safe to cut.

    A position is safe if it sits between sentences (after ``[.!?]+\\s+``) and
    no inline pair is currently open.
    """

    safe: list[int] = []
    depth = 0
    pos = 0
    n = len(source_text)
    while pos < n:
        m = PLACEHOLDER_RE.match(source_text, pos)
        if m:
            is_close = m.group(1) == "/"
            tok_idx = int(m.group(2))
            kind = skeleton[tok_idx].kind if 0 <= tok_idx < len(skeleton) else "void"
            if is_close:
                depth -= 1
            elif kind == "pair":
                depth += 1
            pos = m.end()
            continue
        if depth == 0 and source_text[pos] in ".!?":
            j = pos + 1
            while j < n and source_text[j] in ".!?":
                j += 1
            if j < n and source_text[j].isspace():
                k = j
                while k < n and source_text[k].isspace():
                    k += 1
                safe.append(k)
                pos = k
                continue
        pos += 1
    return safe


def _renumber(
    text: str, skeleton: Sequence[InlineToken]
) -> tuple[str, list[InlineToken]]:
    seen: dict[int, int] = {}
    used_indices: list[int] = []
    for m in PLACEHOLDER_RE.finditer(text):
        old = int(m.group(2))
        if old not in seen:
            seen[old] = len(used_indices)
            used_indices.append(old)

    def _sub(m: re.Match[str]) -> str:
        slash = m.group(1)
        old = int(m.group(2))
        return f"[[{slash}T{seen[old]}]]"

    new_text = PLACEHOLDER_RE.sub(_sub, text)
    new_skeleton = [skeleton[i] for i in used_indices]
    return new_text, new_skeleton


__all__ = [
    "PLACEHOLDER_RE",
    "VOID_LOCAL_NAMES",
    "apply_parts_to_host",
    "count_tokens",
    "expand_text_entity_placeholders",
    "is_trivially_empty",
    "placeholderize",
    "split_by_sentences",
]
