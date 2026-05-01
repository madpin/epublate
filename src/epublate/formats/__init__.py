"""Format adapters. ePub lands in M1 (PRD §10); PDF is a post-v1 non-goal."""

from __future__ import annotations

from epublate.formats.base import (
    Book,
    ChapterDoc,
    FormatAdapter,
    InlineKind,
    InlineToken,
    Segment,
)

# ``EpubAdapter`` lives in ``epublate.formats.epub``; importing it here would
# create a cycle (``epub.py`` depends on ``core.segmentation`` which in turn
# imports from this package). Callers should import it explicitly.

__all__ = [
    "Book",
    "ChapterDoc",
    "FormatAdapter",
    "InlineKind",
    "InlineToken",
    "Segment",
]
