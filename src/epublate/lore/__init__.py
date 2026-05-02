"""Lore Book primitive (PRD §4.3 / F-LB-10).

A *Lore Book* is a portable, attachable artifact whose canonical store
mirrors the per-project SQLite shape: same ``glossary_entry`` /
``glossary_alias`` / ``glossary_revision`` schema, plus
``lore_meta`` and ``lore_source`` rows that describe what the book is
and which ePubs have been ingested into it.

The module is organised as:

* :mod:`epublate.lore.lore` — :class:`LoreBook` lifecycle (create / open
  / close).
* :mod:`epublate.lore.repo` — Lore-Book-scoped CRUD on the shared
  glossary tables, plus the new ``lore_meta`` / ``lore_source`` rows.
* :mod:`epublate.lore.ingest` — wraps the existing
  :func:`epublate.core.extractor.run_book_intake` so a Lore Book DB can
  receive proposals from a *source-language* ePub.
* :mod:`epublate.lore.ingest_target` — new helper-LLM pass that walks
  a *target-language* ePub and seeds the Lore Book with target-only
  proper-noun proposals (PRD F-LB-3).
* :mod:`epublate.lore.library` — XDG-aware library helpers for the
  default ``~/.config/epublate/lore/`` location (PRD §4.3).
"""

from __future__ import annotations

from epublate.lore.import_project import (
    ConflictAction,
    ProjectImportConflict,
    ProjectImportPolicy,
    ProjectImportSummary,
    apply_conflict_resolution,
    import_project_glossary,
)
from epublate.lore.ingest import (
    LoreIngestSummary,
    ingest_source_epub,
)
from epublate.lore.ingest_target import (
    ingest_target_epub,
)
from epublate.lore.library import (
    DEFAULT_LIBRARY_ENV,
    default_library_dir,
    iter_library_lore_books,
)
from epublate.lore.lore import (
    LORE_DB_SUFFIX,
    LORE_DIR_SUFFIX,
    LoreBook,
    open_lore_book,
)
from epublate.lore.repo import (
    LoreMetaRow,
    LoreSourceRow,
    create_lore_meta,
    get_lore_meta,
    insert_lore_source,
    list_lore_sources,
    update_lore_meta,
)

__all__ = [
    "DEFAULT_LIBRARY_ENV",
    "LORE_DB_SUFFIX",
    "LORE_DIR_SUFFIX",
    "ConflictAction",
    "LoreBook",
    "LoreIngestSummary",
    "LoreMetaRow",
    "LoreSourceRow",
    "ProjectImportConflict",
    "ProjectImportPolicy",
    "ProjectImportSummary",
    "apply_conflict_resolution",
    "create_lore_meta",
    "default_library_dir",
    "get_lore_meta",
    "import_project_glossary",
    "ingest_source_epub",
    "ingest_target_epub",
    "insert_lore_source",
    "iter_library_lore_books",
    "list_lore_sources",
    "open_lore_book",
    "update_lore_meta",
]
