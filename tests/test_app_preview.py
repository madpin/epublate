"""Unit tests for :mod:`epublate.app.preview` (PRD §4.6 / Reader UX)."""

from __future__ import annotations

from epublate.app.preview import has_translatable_text, render_preview
from epublate.formats.base import InlineToken


def test_render_preview_emphasis_becomes_italic() -> None:
    skeleton = [InlineToken(tag="em", kind="pair")]
    out = render_preview("hello [[T0]]world[[/T0]]!", skeleton)
    assert out == "hello [italic]world[/italic]!"


def test_render_preview_strong_becomes_bold() -> None:
    skeleton = [InlineToken(tag="strong", kind="pair")]
    out = render_preview("[[T0]]Title[[/T0]] only", skeleton)
    assert out == "[bold]Title[/bold] only"


def test_render_preview_link_gets_underline_color() -> None:
    skeleton = [
        InlineToken(tag="a", kind="pair", attrs={"href": "#fn1"}),
    ]
    out = render_preview("see [[T0]]footnote[[/T0]]", skeleton)
    assert out == "see [underline cyan]footnote[/underline cyan]"


def test_render_preview_image_marker_uses_alt_text() -> None:
    skeleton = [InlineToken(tag="img", kind="void", attrs={"alt": "A diagram"})]
    out = render_preview("[[T0]]", skeleton)
    assert out == "[dim italic][image: A diagram][/dim italic]"


def test_render_preview_image_marker_falls_back_to_src() -> None:
    skeleton = [
        InlineToken(tag="img", kind="void", attrs={"src": "img/cover.png"}),
    ]
    out = render_preview("[[T0]]", skeleton)
    assert out == "[dim italic][image: cover.png][/dim italic]"


def test_render_preview_br_becomes_newline() -> None:
    skeleton = [InlineToken(tag="br", kind="void")]
    out = render_preview("line one[[T0]]line two", skeleton)
    assert out == "line one\nline two"


def test_render_preview_nested_emphasis_round_trips() -> None:
    skeleton = [
        InlineToken(tag="em", kind="pair"),
        InlineToken(tag="strong", kind="pair"),
    ]
    out = render_preview(
        "[[T0]]italic [[T1]]and bold[[/T1]] still italic[[/T0]] plain", skeleton
    )
    assert out == ("[italic]italic [bold]and bold[/bold] still italic[/italic] plain")


def test_render_preview_escapes_literal_brackets_in_text() -> None:
    out = render_preview("a [literal] [bold]bracket", [])
    assert out == r"a \[literal] \[bold]bracket"


def test_render_preview_handles_unknown_skeleton_index_gracefully() -> None:
    out = render_preview("hi [[T9]]there[[/T9]]", [])
    assert "⟪?⟫" in out
    assert "hi" in out
    assert "there" in out


def test_render_preview_closes_unbalanced_open_pair() -> None:
    skeleton = [InlineToken(tag="em", kind="pair")]
    out = render_preview("[[T0]]oops never closed", skeleton)
    assert out.endswith("[/italic]")


def test_render_preview_uses_namespaced_tag() -> None:
    skeleton = [InlineToken(tag="{http://www.w3.org/1999/xhtml}strong", kind="pair")]
    out = render_preview("[[T0]]Bold[[/T0]]", skeleton)
    assert out == "[bold]Bold[/bold]"


def test_render_preview_empty_text_returns_empty_string() -> None:
    assert render_preview("", []) == ""


def test_has_translatable_text_detects_image_only_paragraph() -> None:
    assert not has_translatable_text("[[T0]]")
    assert not has_translatable_text("[[T0]][[/T0]]")
    assert not has_translatable_text("   [[T0]][[/T0]]\n  ")


def test_has_translatable_text_keeps_real_content() -> None:
    assert has_translatable_text("[[T0]]Three Political Dimensions[[/T0]]")
    assert has_translatable_text("Hello, world.")


def test_has_translatable_text_empty_string() -> None:
    assert not has_translatable_text("")
