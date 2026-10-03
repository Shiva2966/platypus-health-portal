"""Alembic: migrations apply on an empty DB, match the models exactly (no drift), and can be reverted."""
import os

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy import inspect, text

from test_db_support import db_engine, empty_db, kind  # noqa: F401

from app.db import Base, import_all_models
from app.db_ops import migrate
from app.db_ops.triggers import audit_triggers_present


def test_upgrade_head_creates_everything(empty_db):
    migrate.upgrade_to_head(empty_db)
    st = migrate.migration_state(empty_db)
    assert st["up_to_date"] and st["managed"]
    tables = set(inspect(empty_db).get_table_names())
    import_all_models()
    assert set(Base.metadata.tables) <= tables
    with empty_db.connect() as c:
        assert audit_triggers_present(c)


def test_upgrade_is_idempotent(empty_db):
    migrate.upgrade_to_head(empty_db)
    migrate.upgrade_to_head(empty_db)
    assert migrate.migration_state(empty_db)["up_to_date"]


@pytest.mark.skipif(os.environ.get("SKIP_MIGRATION_DRIFT") == "1", reason="explicitly skipped")
def test_models_and_migrations_have_not_drifted(empty_db):
    """Fails when someone changes a model without a migration.  Fix:  scripts\\migrate.ps1 new "<what>"
    (against an EMPTY database at the previous head)."""
    migrate.upgrade_to_head(empty_db)
    import_all_models()
    with empty_db.connect() as conn:
        mc = MigrationContext.configure(conn, opts={"compare_type": True,
                                                    "render_as_batch": conn.dialect.name == "sqlite"})
        diffs = compare_metadata(mc, Base.metadata)
    assert not diffs, f"models differ from migrations: {diffs[:8]}"


def test_downgrade_to_base_and_back(empty_db):
    migrate.upgrade_to_head(empty_db)
    command.downgrade(migrate.alembic_config(empty_db), "base")
    left = set(inspect(empty_db).get_table_names()) - {"alembic_version"}
    assert not left, f"downgrade left tables behind: {left}"
    migrate.upgrade_to_head(empty_db)
    assert migrate.migration_state(empty_db)["up_to_date"]


def test_upgrade_refuses_unmanaged_existing_schema(db_engine):
    with pytest.raises(RuntimeError, match="alembic stamp head"):
        migrate.upgrade_to_head(db_engine)  # create_all'd dev DB must be stamped deliberately


def test_stamp_then_state_is_current(db_engine):
    migrate.stamp_head(db_engine)
    assert migrate.migration_state(db_engine)["up_to_date"]


def test_init_db_with_auto_migrate(empty_db, monkeypatch):
    from app.db import init_db

    monkeypatch.setenv("AUTO_MIGRATE", "1")
    init_db(empty_db)
    assert migrate.migration_state(empty_db)["up_to_date"]
    monkeypatch.delenv("AUTO_MIGRATE")
    init_db(empty_db)  # create_all on an alembic-managed DB is a harmless no-op
    with empty_db.connect() as c:
        assert c.execute(text("SELECT count(*) FROM alembic_version")).scalar() == 1
