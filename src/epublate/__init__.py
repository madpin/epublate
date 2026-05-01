"""epublate — translate ePub story books with an LLM, preserving format and lore.

The authoritative product spec lives in ``docs/PRD.md``. This package follows
the layout in PRD §6.2 and the invariants documented in ``AGENTS.md``.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("epublate")
except PackageNotFoundError:  # editable install before metadata is generated
    __version__ = "0.0.0+local"

__all__ = ["__version__"]
