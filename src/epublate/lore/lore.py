"""Lore Book lifecycle service (PRD §4.3 / F-LB-10).

A Lore Book lives on disk as a directory:

```
<name>.epublate-lore/
  lore.epublate-lore   # SQLite WAL DB (canonical store)
  sources/             # copies of ingested ePubs (re-runs)
  events.jsonl         # mirror of lore_event (Phase 4 — currently unused)
```

The DB shape mirrors a project DB so the same migrations and the same
glossary repo helpers work. The ``project`` row inside has
``kind='lore'`` and is the FK target of every glossary entry; the
extra ``lore_meta`` row pins Lore-Book-only fields (description,
default proposal kind).

Use :meth:`LoreBook.create` to bootstrap and :meth:`LoreBook.open` to
re-attach. Both return a fully initialized handle whose ``engine``
field is the SQLite engine the repo helpers expect; close it with
:meth:`close` (or use :func:`open_lore_book` as a context manager).
"""

from __future__ import annotations

import logging
import shutil
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.engine import Engine

from epublate.db import connect, repo, schema
from epublate.errors import ConfigurationError
from epublate.lore import repo as lore_repo

LORE_DIR_SUFFIX = ".epublate-lore"
LORE_DB_SUFFIX = ".epublate-lore"
LORE_SOURCES_DIRNAME = "sources"

_logger = logging.getLogger(__name__)


@dataclass(slots=True)
class LoreBook:
    """In-memory handle to an opened Lore Book on disk.

    Mirrors the shape of :class:`epublate.core.project.Project` — same
    ``engine`` field, same ``project_id`` semantics — so the existing
    glossary repo / IO helpers work against a Lore Book DB without
    knowing the difference. The ``description`` and ``default_proposal_kind``
    fields are Lore-Book-only and pulled from the ``lore_meta`` row.
    """

    lore_dir: Path
    project_id: str
    name: str
    source_lang: str
    target_lang: str
    db_path: Path
    engine: Engine
    description: str | None = None
    default_proposal_kind: str = schema.LoreSourceKind.TARGET

    @classmethod
    def create(
        cls,
        *,
        out_dir: Path,
        name: str,
        source_lang: str,
        target_lang: str,
        description: str | None = None,
        default_proposal_kind: str = schema.LoreSourceKind.TARGET,
    ) -> LoreBook:
        """Initialize a fresh Lore Book directory.

        Layout under ``out_dir``:

            <out_dir>/
              lore.epublate-lore       # SQLite DB (WAL)
              sources/                 # populated on first ingest

        Refuses to write into a non-empty directory so a clumsy
        ``--out`` doesn't clobber existing artifacts.
        """

        out_dir = Path(out_dir).resolve()
        if out_dir.exists() and any(out_dir.iterdir()):
            raise ConfigurationError(
                f"refusing to create lore book in non-empty directory: {out_dir}"
            )
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / LORE_SOURCES_DIRNAME).mkdir(exist_ok=True)

        db_path = out_dir / f"lore{LORE_DB_SUFFIX}"
        engine = connect(db_path)
        try:
            project_row = repo.create_project(
                engine,
                name=name,
                source_lang=source_lang,
                target_lang=target_lang,
                source_path="",
                kind=schema.ProjectKind.LORE,
            )
            lore_repo.create_lore_meta(
                engine,
                project_id=project_row.id,
                description=description,
                default_proposal_kind=default_proposal_kind,
            )
            repo.append_event(
                engine,
                project_id=project_row.id,
                kind="lore.created",
                payload={
                    "name": name,
                    "source_lang": source_lang,
                    "target_lang": target_lang,
                    "default_proposal_kind": default_proposal_kind,
                },
            )
        except Exception:
            engine.dispose()
            raise

        return cls(
            lore_dir=out_dir,
            project_id=project_row.id,
            name=name,
            source_lang=source_lang,
            target_lang=target_lang,
            db_path=db_path,
            engine=engine,
            description=description,
            default_proposal_kind=default_proposal_kind,
        )

    @classmethod
    def open(cls, lore_dir: Path) -> LoreBook:
        """Re-open an existing Lore Book directory."""

        lore_dir = Path(lore_dir).resolve()
        if not lore_dir.is_dir():
            raise ConfigurationError(f"not a lore book directory: {lore_dir}")
        db_path = _resolve_db_path(lore_dir)
        engine = connect(db_path)
        try:
            projects = repo.list_projects(engine, kind=schema.ProjectKind.LORE)
            if not projects:
                raise ConfigurationError(f"no lore book row found in {db_path}")
            if len(projects) > 1:
                raise ConfigurationError(
                    f"multiple lore book rows in {db_path}; expected exactly one"
                )
            project_row = projects[0]
            meta = lore_repo.get_lore_meta(engine, project_id=project_row.id)
        except Exception:
            engine.dispose()
            raise

        return cls(
            lore_dir=lore_dir,
            project_id=project_row.id,
            name=project_row.name,
            source_lang=project_row.source_lang,
            target_lang=project_row.target_lang,
            db_path=db_path,
            engine=engine,
            description=meta.description if meta else None,
            default_proposal_kind=(
                meta.default_proposal_kind if meta else schema.LoreSourceKind.TARGET
            ),
        )

    def close(self) -> None:
        self.engine.dispose()

    @property
    def sources_dir(self) -> Path:
        return self.lore_dir / LORE_SOURCES_DIRNAME

    def stash_source(self, epub_path: Path) -> Path:
        """Copy ``epub_path`` into the Lore Book's ``sources/`` dir.

        Returns the destination path. Re-running an ingest against the
        same source ePub is allowed; the destination filename gets a
        ``.<ts>`` suffix to keep history. Used by ingest helpers so a
        Lore Book carries everything needed to re-run intake later.
        """

        epub_path = Path(epub_path).resolve()
        if not epub_path.is_file():
            raise ConfigurationError(f"source ePub not found: {epub_path}")
        self.sources_dir.mkdir(exist_ok=True)
        dest = self.sources_dir / epub_path.name
        if dest.exists():
            ts = uuid.uuid4().hex[:8]
            dest = dest.with_name(f"{dest.stem}.{ts}{dest.suffix}")
        shutil.copyfile(epub_path, dest)
        return dest


@contextmanager
def open_lore_book(lore_dir: Path) -> Iterator[LoreBook]:
    """Context manager around :meth:`LoreBook.open`."""

    book = LoreBook.open(lore_dir)
    try:
        yield book
    finally:
        book.close()


def _resolve_db_path(lore_dir: Path) -> Path:
    candidates = sorted(lore_dir.glob(f"*{LORE_DB_SUFFIX}"))
    if not candidates:
        raise ConfigurationError(f"no *{LORE_DB_SUFFIX} file found in {lore_dir}")
    if len(candidates) > 1:
        raise ConfigurationError(
            f"multiple *{LORE_DB_SUFFIX} files in {lore_dir}; expected one"
        )
    return candidates[0]


__all__ = [
    "LORE_DB_SUFFIX",
    "LORE_DIR_SUFFIX",
    "LORE_SOURCES_DIRNAME",
    "LoreBook",
    "open_lore_book",
]
