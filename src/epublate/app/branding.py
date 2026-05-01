"""Visual identity for the TUI: logo, icons, palette helpers.

Keeping branding out of individual screens means we can refresh the
look without hunting through every module. Every constant here is a
plain string or simple helper so the TUI stays portable across
terminals — no Nerd Fonts assumed.
"""

from __future__ import annotations

# A compact wordmark drawn with ASCII block characters so it renders
# identically in every terminal. Six lines tall to fit comfortably in
# the Projects-screen hero card without dominating it.
EPUBLATE_LOGO = r"""
 ███████╗██████╗ ██╗   ██╗██████╗ ██╗      █████╗ ████████╗███████╗
 ██╔════╝██╔══██╗██║   ██║██╔══██╗██║     ██╔══██╗╚══██╔══╝██╔════╝
 █████╗  ██████╔╝██║   ██║██████╔╝██║     ███████║   ██║   █████╗
 ██╔══╝  ██╔═══╝ ██║   ██║██╔══██╗██║     ██╔══██║   ██║   ██╔══╝
 ███████╗██║     ╚██████╔╝██████╔╝███████╗██║  ██║   ██║   ███████╗
 ╚══════╝╚═╝      ╚═════╝ ╚═════╝ ╚══════╝╚═╝  ╚═╝   ╚═╝   ╚══════╝
""".strip("\n")

TAGLINE = "Translate ePub story books with an LLM — format and lore intact."

# Decorative glyphs we use to hint at status without leaning on
# emoji (which fall back to monochrome boxes in many terminals).
ICON_ACTIVE = "●"
ICON_MISSING = "✗"
ICON_DONE = "★"
ICON_ARROW = "→"
ICON_BULLET = "•"


def progress_bar(ratio: float, width: int = 12) -> str:
    """Render a compact Unicode progress bar of ``width`` cells.

    ``ratio`` is clamped to ``[0, 1]``. We use full-block + light-shade
    glyphs that sit at every Unicode terminal we care about.
    """

    if ratio < 0:
        ratio = 0.0
    if ratio > 1:
        ratio = 1.0
    filled = round(ratio * width)
    return "█" * filled + "░" * (width - filled)


def progress_color(ratio: float) -> str:
    """Pick a Rich color tag for a progress ratio.

    Used as ``[{progress_color(r)}]██░░░░ 30%[/]``. Stays inside the
    theme's semantic colors so it tracks light/dark/high-contrast.
    """

    if ratio >= 0.99:
        return "$success"
    if ratio >= 0.66:
        return "$primary"
    if ratio >= 0.33:
        return "$warning"
    return "$text-muted"


__all__ = [
    "EPUBLATE_LOGO",
    "ICON_ACTIVE",
    "ICON_ARROW",
    "ICON_BULLET",
    "ICON_DONE",
    "ICON_MISSING",
    "TAGLINE",
    "progress_bar",
    "progress_color",
]
