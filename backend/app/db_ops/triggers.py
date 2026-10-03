"""DB-level guarantees that cannot be bypassed by buggy application code.

* ``audit_log`` is APPEND-ONLY: UPDATE / DELETE (and TRUNCATE on PostgreSQL) raise an error.
* ``document_blobs.data`` uses PostgreSQL ``STORAGE EXTERNAL`` (no TOAST compression; files are
  already compressed, and it enables cheap ranged ``substr()`` reads for streaming).

All installers are idempotent; ``ensure_db_objects`` is called on startup and from migrations.
"""
from __future__ import annotations

import logging

from sqlalchemy import inspect, text
from sqlalchemy.engine import Connection, Engine

log = logging.getLogger("health-portal.db")

AUDIT_MSG = "audit_log is append-only (updates and deletes are not allowed)"

_PG_FUNC = f"""
CREATE OR REPLACE FUNCTION audit_log_immutable() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION '{AUDIT_MSG}' USING ERRCODE = 'integrity_constraint_violation';
END;
$$ LANGUAGE plpgsql
"""
_PG_ROW_TRIGGER = """
CREATE TRIGGER audit_log_no_change BEFORE UPDATE OR DELETE ON audit_log
FOR EACH ROW EXECUTE FUNCTION audit_log_immutable()
"""
_PG_TRUNC_TRIGGER = """
CREATE TRIGGER audit_log_no_truncate BEFORE TRUNCATE ON audit_log
FOR EACH STATEMENT EXECUTE FUNCTION audit_log_immutable()
"""
_SQLITE_TRIGGERS = [
    f"CREATE TRIGGER IF NOT EXISTS audit_log_no_update BEFORE UPDATE ON audit_log "
    f"BEGIN SELECT RAISE(ABORT, '{AUDIT_MSG}'); END",
    f"CREATE TRIGGER IF NOT EXISTS audit_log_no_delete BEFORE DELETE ON audit_log "
    f"BEGIN SELECT RAISE(ABORT, '{AUDIT_MSG}'); END",
]


def install_audit_triggers(conn: Connection) -> bool:
    """Create the append-only triggers if the audit_log table exists. Returns True if present after."""
    if not inspect(conn).has_table("audit_log"):
        return False
    d = conn.dialect.name
    if d == "postgresql":
        existing = {r[0] for r in conn.exec_driver_sql(
            "SELECT tgname FROM pg_trigger WHERE tgrelid = 'audit_log'::regclass AND NOT tgisinternal")}
        if not existing >= {"audit_log_no_change", "audit_log_no_truncate"}:
            conn.exec_driver_sql(_PG_FUNC)
            if "audit_log_no_change" not in existing:
                conn.exec_driver_sql(_PG_ROW_TRIGGER)
            if "audit_log_no_truncate" not in existing:
                conn.exec_driver_sql(_PG_TRUNC_TRIGGER)
    elif d == "sqlite":
        for stmt in _SQLITE_TRIGGERS:
            conn.exec_driver_sql(stmt)
    return True


def audit_triggers_present(conn: Connection) -> bool:
    d = conn.dialect.name
    if not inspect(conn).has_table("audit_log"):
        return False
    if d == "postgresql":
        names = {r[0] for r in conn.exec_driver_sql(
            "SELECT tgname FROM pg_trigger WHERE tgrelid = 'audit_log'::regclass AND NOT tgisinternal")}
        return {"audit_log_no_change", "audit_log_no_truncate"} <= names
    if d == "sqlite":
        names = {r[0] for r in conn.exec_driver_sql(
            "SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='audit_log'")}
        return {"audit_log_no_update", "audit_log_no_delete"} <= names
    return True


def tune_postgres_storage(conn: Connection) -> None:
    if conn.dialect.name != "postgresql":
        return
    if inspect(conn).has_table("document_blobs"):
        conn.exec_driver_sql("ALTER TABLE document_blobs ALTER COLUMN data SET STORAGE EXTERNAL")
    elif inspect(conn).has_table("documents") and "data" in {
            c["name"] for c in inspect(conn).get_columns("documents")}:
        conn.exec_driver_sql("ALTER TABLE documents ALTER COLUMN data SET STORAGE EXTERNAL")


def ensure_db_objects(engine_or_conn) -> None:
    """Idempotently install triggers / storage tuning."""
    if isinstance(engine_or_conn, Engine):
        with engine_or_conn.begin() as conn:
            install_audit_triggers(conn)
            tune_postgres_storage(conn)
    else:
        install_audit_triggers(engine_or_conn)
        tune_postgres_storage(engine_or_conn)
