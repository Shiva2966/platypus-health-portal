"""Programmatic Alembic helpers (used by app startup, /readyz, scripts and tests)."""
from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import inspect
from sqlalchemy.engine import Engine

ROOT = Path(__file__).resolve().parent.parent.parent


def alembic_config(engine: Engine) -> Config:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "alembic"))
    cfg.attributes["engine"] = engine
    return cfg


def head_revisions() -> list[str]:
    return list(ScriptDirectory(str(ROOT / "alembic")).get_heads())


def current_revisions(engine: Engine) -> list[str]:
    with engine.connect() as conn:
        return list(MigrationContext.configure(conn).get_current_heads())


def migration_state(engine: Engine) -> dict:
    """{'head': [...], 'current': [...], 'up_to_date': bool, 'managed': bool}.

    ``managed`` is False when the schema was created with ``create_all`` (no alembic_version table)."""
    head = head_revisions()
    with engine.connect() as conn:
        managed = inspect(conn).has_table("alembic_version")
        current = list(MigrationContext.configure(conn).get_current_heads()) if managed else []
    return {"head": head, "current": current, "managed": managed,
            "up_to_date": managed and sorted(current) == sorted(head)}


def upgrade_to_head(engine: Engine) -> None:
    state = migration_state(engine)
    if not state["managed"]:
        with engine.connect() as conn:
            existing = [t for t in inspect(conn).get_table_names() if t != "alembic_version"]
        if existing:
            raise RuntimeError(
                "Database has tables but no alembic_version (created by create_all?). "
                "Verify it matches the models, then run:  alembic stamp head   "
                "(or restore a backup into an empty database).")
    command.upgrade(alembic_config(engine), "head")


def stamp_head(engine: Engine) -> None:
    command.stamp(alembic_config(engine), "head")
