"""SQLite persistence layer for epublate (PRD §6.4 / db-and-persistence rule).

Public surface:

* :func:`connect` — open (or create) a project database, apply migrations,
  and enable the WAL pragmas required by the resumability invariant.
* :func:`apply_migrations` — run Alembic ``upgrade head`` against an existing
  connection (used by tests and by :func:`connect`).
* The schema metadata via :mod:`epublate.db.schema`.

Hard rules:

* SQLite is opened in **WAL mode** with ``foreign_keys=ON``.
* Migrations live in :mod:`epublate.db.migrations`. Don't hand-edit applied
  revisions; add new ones.
* Repository helpers go through SQLAlchemy Core with bound parameters —
  never f-string SQL.
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy import event as sa_event
from sqlalchemy.engine import Engine

from epublate.db import schema
from epublate.errors import MigrationError

_MIGRATIONS_PACKAGE = "epublate.db.migrations"


def _enable_sqlite_pragmas(dbapi_connection: object, _record: object) -> None:
    """Enable WAL + foreign keys on every new SQLite connection.

    SQLAlchemy emits a ``connect`` event for each underlying DB-API connection;
    we attach this listener once to the SQLite ``Engine`` so the pragmas apply
    even after the pool recycles connections.
    """

    # ``sqlite3.Connection`` is the runtime type; we duck-type to keep the
    # listener compatible with the abstract ``DBAPIConnection`` SQLAlchemy
    # passes here.
    cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
    try:
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA synchronous=NORMAL")
    finally:
        cursor.close()


def _engine_for(path: Path) -> Engine:
    url = f"sqlite+pysqlite:///{path}"
    engine = create_engine(url, future=True)
    sa_event.listen(engine, "connect", _enable_sqlite_pragmas)
    return engine


def apply_migrations(engine: Engine) -> None:
    """Run Alembic ``upgrade head`` on ``engine``.

    Imported lazily because Alembic pulls in Mako and a logging configuration
    we don't want to load until persistence is actually exercised.
    """

    from alembic import command
    from alembic.config import Config

    cfg = Config()
    cfg.set_main_option("script_location", f"{_MIGRATIONS_PACKAGE}:")
    cfg.set_main_option("sqlalchemy.url", str(engine.url))
    try:
        with engine.begin() as connection:
            cfg.attributes["connection"] = connection
            command.upgrade(cfg, "head")
    except Exception as exc:
        raise MigrationError(f"failed to apply migrations: {exc}") from exc


def connect(path: str | Path) -> Engine:
    """Open a per-project SQLite database, applying migrations on first use.

    The file is created if it does not exist. Parent directories are **not**
    auto-created — the caller (project lifecycle in M1) owns the project
    folder layout.
    """

    project_path = Path(path)
    engine = _engine_for(project_path)
    apply_migrations(engine)
    return engine


__all__ = [
    "Engine",
    "apply_migrations",
    "connect",
    "schema",
]
