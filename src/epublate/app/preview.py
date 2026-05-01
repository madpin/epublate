"""Reader-friendly rendering of placeholder-bearing segment text.

The translation pipeline replaces every inline tag with opaque
``[[T0]]…[[/T0]]`` placeholders so the model never sees raw HTML
(format-handling rule §1). That contract is excellent for the LLM —
and brutal for human eyes when surfaced in the Reader screen as-is.

This module reverses the placeholder substitution **for display
only**: it walks the placeholder text against the segment's inline
skeleton and emits Rich markup that renders the structure curators
care about (emphasis, links, images, line breaks). The actual
``source_text`` / ``target_text`` stored in the DB stays untouched —
re-translation, validation, and reassembly continue to operate on the
authoritative placeholder form.

Hard rules:

* **No mutation.** ``render_preview`` is pure: same inputs always
  return the same Rich-markup string.
* **No HTML escaping.** Inputs come from segments where inline tags
  are already replaced by placeholders, so the only literal markup
  characters left are user-authored (e.g. someone wrote ``[note]``).
  We pass them through ``rich.markup.escape`` so they don't accidentally
  collide with the markup we emit.
* **Structural fidelity.** When the placeholder text references a
  skeleton index that doesn't exist (corrupted segment), we fall
  through and render a visible ``⟪?⟫`` marker rather than crash.
"""

from __future__ import annotations

from collections.abc import Sequence

from rich.markup import escape

from epublate.core.segmentation import PLACEHOLDER_RE
from epublate.formats.base import InlineToken

EMPHASIS_TAGS: frozenset[str] = frozenset({"em", "i", "cite", "dfn", "var"})
STRONG_TAGS: frozenset[str] = frozenset({"strong", "b"})
UNDERLINE_TAGS: frozenset[str] = frozenset({"u", "ins"})
STRIKE_TAGS: frozenset[str] = frozenset({"s", "del", "strike"})
CODE_TAGS: frozenset[str] = frozenset({"code", "kbd", "samp", "tt"})
LINK_TAGS: frozenset[str] = frozenset({"a"})
RUBY_TEXT_TAGS: frozenset[str] = frozenset({"rt"})
RUBY_PARENS_TAGS: frozenset[str] = frozenset({"rp"})
SUP_TAGS: frozenset[str] = frozenset({"sup"})
SUB_TAGS: frozenset[str] = frozenset({"sub"})
LINEBREAK_TAGS: frozenset[str] = frozenset({"br"})
IMAGE_TAGS: frozenset[str] = frozenset({"img", "image"})


def _local_name(tag: str) -> str:
    if tag.startswith("{"):
        return tag.split("}", 1)[1]
    return tag


def _open_close(tag: str) -> tuple[str, str]:
    """Return Rich-markup open/close fragments for a recognised inline tag.

    Returns ``("", "")`` for tags we don't decorate (keeps the inner
    text intact without adding noise).
    """

    name = _local_name(tag).lower()
    if name in STRONG_TAGS:
        return "[bold]", "[/bold]"
    if name in EMPHASIS_TAGS:
        return "[italic]", "[/italic]"
    if name in UNDERLINE_TAGS:
        return "[underline]", "[/underline]"
    if name in STRIKE_TAGS:
        return "[strike]", "[/strike]"
    if name in CODE_TAGS:
        return "[reverse]", "[/reverse]"
    if name in LINK_TAGS:
        return "[underline cyan]", "[/underline cyan]"
    if name in RUBY_TEXT_TAGS:
        return "[dim](", "[/dim])"
    if name in RUBY_PARENS_TAGS:
        return "", ""
    if name in SUP_TAGS:
        return "[dim]^", "[/dim]"
    if name in SUB_TAGS:
        return "[dim]_", "[/dim]"
    return "", ""


def _image_marker(token: InlineToken) -> str:
    """Render an ``<img>`` placeholder as a visible ``[image: …]`` label."""

    alt = token.attrs.get("alt") or ""
    src = token.attrs.get("src") or token.attrs.get("href") or ""
    label = alt.strip() or src.split("/")[-1] or "image"
    safe = escape(label)
    return f"[dim italic][image: {safe}][/dim italic]"


def _void_marker(token: InlineToken) -> str:
    """Render a non-image void tag (``<br/>``, ``<hr/>``, …)."""

    name = _local_name(token.tag).lower()
    if name in IMAGE_TAGS:
        return _image_marker(token)
    if name in LINEBREAK_TAGS:
        return "\n"
    if name == "hr":
        return "\n[dim]──────[/dim]\n"
    if name == "wbr":
        return ""
    safe = escape(name)
    return f"[dim]<{safe}/>[/dim]"


def render_preview(text: str, skeleton: Sequence[InlineToken]) -> str:
    """Render placeholder text + skeleton as a Rich-markup string.

    The output is suitable for any ``Static`` (or other Rich-aware)
    Textual widget with ``markup=True``. Plain text is markup-escaped
    so user-authored ``[..]`` chunks don't mis-render.

    Returns an empty string when ``text`` is empty (the caller decides
    how to indicate "no translatable content").
    """

    if not text:
        return ""

    out: list[str] = []
    pos = 0
    open_stack: list[tuple[int, str]] = []
    n = len(text)

    while pos < n:
        m = PLACEHOLDER_RE.match(text, pos)
        if m is None:
            next_match = PLACEHOLDER_RE.search(text, pos)
            end = next_match.start() if next_match else n
            chunk = text[pos:end]
            if chunk:
                out.append(escape(chunk))
            pos = end
            continue

        is_close = m.group(1) == "/"
        idx = int(m.group(2))
        if idx < 0 or idx >= len(skeleton):
            out.append("[dim]⟪?⟫[/dim]")
            pos = m.end()
            continue

        token = skeleton[idx]
        if token.kind == "void":
            out.append(_void_marker(token))
            pos = m.end()
            continue

        open_tag, close_tag = _open_close(token.tag)
        if is_close:
            if open_stack and open_stack[-1][0] == idx:
                open_stack.pop()
            out.append(close_tag)
        else:
            out.append(open_tag)
            open_stack.append((idx, close_tag))
        pos = m.end()

    while open_stack:
        _idx, close_tag = open_stack.pop()
        out.append(close_tag)

    return "".join(out)


def has_translatable_text(text: str) -> bool:
    """True iff ``text`` contains anything beyond placeholders / whitespace.

    Used by the Reader to skip image-only / structurally-empty hosts
    (``<p><img/></p>``, ``<p><br/></p>``, ``<p></p>``) that ended up
    in the segment table because every block-level host produces a
    segment by default.
    """

    if not text:
        return False
    stripped = PLACEHOLDER_RE.sub("", text).strip()
    return bool(stripped)


__all__ = [
    "has_translatable_text",
    "render_preview",
]
