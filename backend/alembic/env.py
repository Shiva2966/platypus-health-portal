"""Alembic environment: autogenerate from the app's models (all of app.models.* auto-imported,
schema policies applied) against DATABASE_URL.  Works for SQLite (batch mode) and PostgreSQL."""
import logging
from logging.config import fileConfig

import sqlalchemy as sa
from alembic import context

from app.db import MIGRATION_LOCK_KEY, Base, import_all_models, make_engine
from app.db_ops.types import UTCDateTime

config = context.config
if config.config_file_name is not None and config.attributes.get("engine") is None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)
log = logging.getLogger("alembic.env")

import_all_models()  # registers every table AND applies app/db_ops/policies.py
target_metadata = Base.metadata


def render_item(type_, obj, autogen_context):
    # keep migration files independent of app code
    if type_ == "type" and isinstance(obj, UTCDateTime):
        return "sa.DateTime(timezone=True)"
    return False


def _configure(connection):
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        render_as_batch=connection.dialect.name == "sqlite",
        render_item=render_item,
        include_object=lambda obj, name, type_, reflected, compare_to: not (
            type_ == "table" and name == "alembic_version"),
    )


def run_migrations_offline() -> None:
    from app.db import get_database_url

    context.configure(url=get_database_url(), target_metadata=target_metadata, literal_binds=True,
                      render_as_batch=True, render_item=render_item, dialect_opts={"paramstyle": "named"})
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = config.attributes.get("engine") or make_engine()
    with engine.connect() as connection:
        if connection.dialect.name == "postgresql":
            connection.exec_driver_sql("SET statement_timeout = 0")
            connection.exec_driver_sql("SET lock_timeout = 0")
            # Serialise concurrent migrators (several replicas starting together).
            # NOTE: the app's own startup lock uses the same key; migrate() is only called once it holds
            # it, so use a *second* key here to avoid self-deadlock.
            connection.exec_driver_sql(f"SELECT pg_advisory_lock({MIGRATION_LOCK_KEY + 1})")
        _configure(connection)
        try:
            with context.begin_transaction():
                context.run_migrations()
            connection.commit()
        finally:
            if connection.dialect.name == "postgresql":
                connection.exec_driver_sql(f"SELECT pg_advisory_unlock({MIGRATION_LOCK_KEY + 1})")
                connection.commit()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
