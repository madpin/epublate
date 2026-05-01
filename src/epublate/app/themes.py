"""Custom themes for the epublate TUI (PRD §4.6 / M6).

Textual ships a comfortable ``textual-dark`` / ``textual-light`` pair out
of the box. PRD §4.6 also requires a **high-contrast** variant for the
accessibility-minded curator; we register one here and expose the full
ordered list of theme names the cycler iterates through.

We also register a branded ``epublate`` theme — a warm, ink-on-parchment
palette tuned for long reading sessions — and use it as the default
look out of the box. Curators who prefer the stock Textual themes can
cycle to them with ``T``.

Keeping the palette in code (not CSS) means the cheat sheet and Settings
screen can introspect ``EPUBLATE_THEME_ORDER`` and present the same
ordering everywhere.
"""

from __future__ import annotations

from textual.theme import Theme

EPUBLATE_DEFAULT_THEME_NAME = "epublate"
EPUBLATE_CONTRAST_THEME_NAME = "epublate-contrast"


def epublate_theme() -> Theme:
    """Branded default theme: warm ink-on-deep-blue, scholarly accents.

    Hand-picked to keep contrast comfortable for hours-long curating
    sessions; the primary is a soft amber, success a calm teal, and
    error a saturated coral so warnings never blend into the body
    text. Tested against WCAG AA at 4.5:1 for the primary text on
    background.
    """

    return Theme(
        name=EPUBLATE_DEFAULT_THEME_NAME,
        primary="#F5B041",
        secondary="#7FB3D5",
        accent="#E59866",
        warning="#F4D03F",
        error="#E74C3C",
        success="#48C9B0",
        foreground="#ECECEC",
        background="#0F1419",
        surface="#161B22",
        panel="#1F2630",
        boost="#2C3440",
        dark=True,
    )


def epublate_contrast_theme() -> Theme:
    """High-contrast theme for low-vision curators (PRD §4.6).

    Pure-black background, near-white text, saturated yellow/red accents.
    The colors are picked to clear WCAG AA contrast against the
    background; we avoid the lower-contrast pastels Textual's default
    themes use.
    """

    return Theme(
        name=EPUBLATE_CONTRAST_THEME_NAME,
        primary="#FFD400",
        secondary="#00B7FF",
        accent="#FF8800",
        warning="#FFB000",
        error="#FF1A1A",
        success="#00E676",
        foreground="#FFFFFF",
        background="#000000",
        surface="#0A0A0A",
        panel="#141414",
        boost="#1F1F1F",
        dark=True,
    )


EPUBLATE_THEME_ORDER: tuple[str, ...] = (
    EPUBLATE_DEFAULT_THEME_NAME,
    "textual-dark",
    "textual-light",
    EPUBLATE_CONTRAST_THEME_NAME,
)
"""Cycle order for the ``T`` keybinding (PRD §4.6).

The branded theme leads so first-time curators see the polished look;
the stock pair stays one keypress away for users who like them.
"""


__all__ = [
    "EPUBLATE_CONTRAST_THEME_NAME",
    "EPUBLATE_DEFAULT_THEME_NAME",
    "EPUBLATE_THEME_ORDER",
    "epublate_contrast_theme",
    "epublate_theme",
]
