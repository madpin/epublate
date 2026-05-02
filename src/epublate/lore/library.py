"""Lore Book library discovery (PRD §4.3 / F-LB-10).

The "library" is the default folder where a curator drops Lore Books
for casual sharing across projects. We keep it XDG-friendly:

* ``$EPUBLATE_LORE_LIBRARY`` overrides the location entirely.
* Otherwise we follow ``$XDG_CONFIG_HOME`` (or ``~/.config``) +
  ``epublate/lore``.

Helpers here are filesystem-only — they don't open the Lore Book DBs,
so they're cheap enough to call from the projects screen on every
refresh. Callers that need the actual rows go through
:meth:`epublate.lore.LoreBook.open`.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from epublate.lore.lore import LORE_DB_SUFFIX

DEFAULT_LIBRARY_ENV = "EPUBLATE_LORE_LIBRARY"
"""Env var that overrides the default library path."""


@dataclass(slots=True, frozen=True)
class LoreBookHandle:
    """Cheap pointer to a Lore Book on disk.

    The ``db_path`` is the SQLite file inside the Lore Book directory;
    ``lore_dir`` is the parent (the directory the curator named).
    Useful for the LoreBooksScreen list before the curator picks one
    to open.
    """

    lore_dir: Path
    db_path: Path

    @property
    def name(self) -> str:
        return self.lore_dir.name


def default_library_dir() -> Path:
    """Resolve the default library directory.

    Honors ``$EPUBLATE_LORE_LIBRARY`` first, then ``$XDG_CONFIG_HOME``,
    then ``~/.config``. We don't *create* the directory here — that
    happens lazily on the first ``LoreBook.create`` call.
    """

    env = os.environ.get(DEFAULT_LIBRARY_ENV)
    if env:
        return Path(env).expanduser().resolve()
    xdg_config = os.environ.get("XDG_CONFIG_HOME")
    if xdg_config:
        return Path(xdg_config).expanduser().resolve() / "epublate" / "lore"
    return Path.home() / ".config" / "epublate" / "lore"


def iter_library_lore_books(
    library_dir: Path | None = None,
) -> Iterator[LoreBookHandle]:
    """Yield every Lore Book found under ``library_dir``.

    A directory qualifies if it contains exactly one ``*.epublate-lore``
    file at the top level (the canonical Lore Book layout). We keep
    the discovery logic loose on purpose so a curator can reorganize
    sub-folders without breaking the listing.
    """

    base = library_dir or default_library_dir()
    if not base.is_dir():
        return
    for child in sorted(base.iterdir()):
        if not child.is_dir():
            continue
        candidates = sorted(child.glob(f"*{LORE_DB_SUFFIX}"))
        if len(candidates) != 1:
            continue
        yield LoreBookHandle(lore_dir=child, db_path=candidates[0])


__all__ = [
    "DEFAULT_LIBRARY_ENV",
    "LoreBookHandle",
    "default_library_dir",
    "iter_library_lore_books",
]
