"""Alembic env.

Driven programmatically from :func:`epublate.db.connect`. The migration runner
is configured with the SQLAlchemy URL of the project's SQLite file at
runtime, so the same revisions apply to every per-project database.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from alembic import context

from epublate.db.schema import metadata as target_metadata

if TYPE_CHECKING:
    from sqlalchemy.engine import Connection

config = context.config


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def _do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = config.attributes.get("connection", None)
    if connectable is not None:
        _do_run_migrations(connectable)
        return

    from sqlalchemy import engine_from_config, pool

    engine = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with engine.connect() as connection:
        _do_run_migrations(connection)


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
