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


def _local_name(tag: str) -> str:
    """Strip Clark-notation namespace from a tag name (``{ns}foo`` → ``foo``)."""

    if tag.startswith("{"):
        return tag.split("}", 1)[1]
    return tag


def _is_void(tag: str) -> bool:
    return _local_name(tag).lower() in VOID_LOCAL_NAMES


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
    "placeholderize",
    "split_by_sentences",
]
