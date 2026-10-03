"""Database setup (SQLAlchemy 2.x) - PostgreSQL in production, SQLite for local dev/tests.

Configuration (environment, or a git-ignored ``.env`` in the project root):

* ``DATABASE_URL``      default ``sqlite:///data/app.db``.  Production: ``postgresql+psycopg://user:pw@host:5432/db``
                        (``postgres://`` and ``postgresql://`` are accepted and rewritten to the psycopg3 driver).
* ``HP_TEST_DATABASE_URL`` / ``HP_DB_PATH``  test isolation overrides.  They WIN over ``DATABASE_URL`` so a
                        test run can never touch the real database.
* ``AUTO_MIGRATE=1``    run ``alembic upgrade head`` at startup (production).  Otherwise ``create_all`` (dev).
* ``DB_POOL_SIZE`` (10) ``DB_MAX_OVERFLOW`` (20) ``DB_POOL_TIMEOUT`` (30) ``DB_POOL_RECYCLE`` (1800)
* ``DB_STATEMENT_TIMEOUT_MS`` (30000)  ``DB_LOCK_TIMEOUT_MS`` (10000)  ``DB_IDLE_TX_TIMEOUT_MS`` (60000)  (PostgreSQL)
* ``SQLITE_BUSY_TIMEOUT_MS`` (30000)

Every model module in app/models/ must subclass ``Base``.  Schema-level rules live in
``app/db_ops/policies.py`` (see docs/DATABASE.md).
"""
from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import MetaData, create_engine, event, text
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.db_ops.policies import NAMING_CONVENTION, apply_policies
from app.db_ops.triggers import ensure_db_objects

log = logging.getLogger("health-portal.db")

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_URL = "sqlite:///data/app.db"
MIGRATION_LOCK_KEY = 727274  # arbitrary constant for pg_advisory_lock

from dotenv import load_dotenv  # python-dotenv is a hard requirement (requirements.txt)

load_dotenv(ROOT / ".env", override=False)  # never overrides real environment variables


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


def normalize_url(url: str) -> str:
    """Rewrite common URL spellings to the drivers we ship; make relative SQLite paths project-relative."""
    if url.startswith("postgres://"):
        url = "postgresql+psycopg://" + url[len("postgres://"):]
    elif url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://"):]
    u = make_url(url)
    if u.get_backend_name() == "sqlite" and u.database and u.database != ":memory:":
        p = Path(u.database)
        if not p.is_absolute():
            p = ROOT / p
        u = u.set(database=str(p))
        url = u.render_as_string(hide_password=False)
    return url


def get_database_url() -> str:
    test_url = os.environ.get("HP_TEST_DATABASE_URL")
    if test_url:
        return normalize_url(test_url)
    if os.environ.get("HP_DB_PATH"):  # test isolation (W1 convention)
        if os.environ.get("DATABASE_URL"):
            log.warning("HP_DB_PATH is set: ignoring DATABASE_URL (test isolation)")
        return normalize_url("sqlite:///" + str(Path(os.environ["HP_DB_PATH"]).resolve()))
    return normalize_url(os.environ.get("DATABASE_URL") or DEFAULT_URL)


def redacted(url: str) -> str:
    return make_url(url).render_as_string(hide_password=True)


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def make_engine(url: str | None = None, **overrides) -> Engine:
    """Create a configured engine.  Used by the app, Alembic, scripts and tests."""
    url = normalize_url(url) if url else get_database_url()
    u = make_url(url)
    backend = u.get_backend_name()
    if backend == "sqlite":
        kw: dict = {"connect_args": {"check_same_thread": False,
                                     "timeout": _env_int("SQLITE_BUSY_TIMEOUT_MS", 30000) / 1000}}
        if u.database in (None, "", ":memory:"):
            kw["poolclass"] = StaticPool
        else:
            Path(u.database).parent.mkdir(parents=True, exist_ok=True)
        kw.update(overrides)
        eng = create_engine(url, **kw)

        @event.listens_for(eng, "connect")
        def _sqlite_pragmas(dbapi_conn, _rec):  # the ONLY place SQLite-specific PRAGMAs live
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA synchronous=NORMAL")
            cur.execute(f"PRAGMA busy_timeout={_env_int('SQLITE_BUSY_TIMEOUT_MS', 30000)}")
            cur.close()

        return eng

    if backend == "postgresql":
        options = (f"-c statement_timeout={_env_int('DB_STATEMENT_TIMEOUT_MS', 30000)} "
                   f"-c lock_timeout={_env_int('DB_LOCK_TIMEOUT_MS', 10000)} "
                   f"-c idle_in_transaction_session_timeout={_env_int('DB_IDLE_TX_TIMEOUT_MS', 60000)}")
        # (no session timezone override: columns are timestamptz and UTCDateTime normalises to UTC)
        kw = {
            "pool_pre_ping": True,
            "pool_size": _env_int("DB_POOL_SIZE", 10),
            "max_overflow": _env_int("DB_MAX_OVERFLOW", 20),
            "pool_timeout": _env_int("DB_POOL_TIMEOUT", 30),
            "pool_recycle": _env_int("DB_POOL_RECYCLE", 1800),
            "connect_args": {"connect_timeout": 10, "options": options, "application_name": "health-portal"},
        }
        kw.update(overrides)
        return create_engine(url, **kw)

    return create_engine(url, **overrides)


DATABASE_URL = get_database_url()
IS_SQLITE = make_url(DATABASE_URL).get_backend_name() == "sqlite"
# Back-compat for older code/tests: filesystem path of the SQLite file (None for other engines).
DB_PATH: Path | None = Path(make_url(DATABASE_URL).database) if IS_SQLITE and make_url(DATABASE_URL).database else None

engine = make_engine(DATABASE_URL)
SessionLocal = sessionmaker(bind=engine, autoflush=True, expire_on_commit=False)

# Whatever creates tables (create_all, tests, scripts), the schema rules are in force first.
event.listen(Base.metadata, "before_create", lambda target, connection, **kw: apply_policies(target))


def get_db():
    """FastAPI dependency: ONE session per request.

    Convention (unchanged): routes call ``db.commit()`` themselves, so a request either commits all of
    its writes (record + audit + notification) or none.  On any exception the transaction is rolled back;
    anything left uncommitted when the request ends is rolled back too (and a PHI-free warning is logged,
    because it usually means a forgotten commit).
    """
    db = SessionLocal()
    try:
        yield db
    except Exception:
        db.rollback()
        raise
    finally:
        try:
            if db.new or db.dirty or db.deleted:
                log.warning("request ended with uncommitted ORM changes; rolled back (missing db.commit()?)")
            if db.in_transaction():
                db.rollback()
        finally:
            db.close()


@contextmanager
def session_scope():
    """Transactional scope for scripts / background jobs: commit on success, rollback on error."""
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def check_connection(eng: Engine | None = None) -> None:
    with (eng or engine).connect() as c:
        c.execute(text("SELECT 1"))


# Every model module, listed explicitly. A new file in app/models/ must be added here; the startup
# self-check (app/startup_check.py) refuses to start if a module exists that is not listed.
MODEL_MODULES = ("appointments", "auth", "billing", "core", "documents", "medical", "shared", "staff")


def import_all_models():
    """Import every model module (MODEL_MODULES) and apply the schema policies to Base.metadata.
    An import error propagates: the app must not start with part of its schema missing."""
    import importlib

    for name in MODEL_MODULES:
        importlib.import_module(f"app.models.{name}")
    apply_policies(Base.metadata)


def is_db_locked(exc: BaseException) -> bool:
    """SQLite 'database is locked' (busy_timeout ran out). Safe to retry: the transaction was rolled back."""
    from sqlalchemy.exc import OperationalError

    return isinstance(exc, OperationalError) and "locked" in str(getattr(exc, "orig", exc)).lower()


def auto_migrate_enabled() -> bool:
    return os.environ.get("AUTO_MIGRATE", "0").strip().lower() in ("1", "true", "yes", "on")


@contextmanager
def _startup_lock(eng: Engine):
    """Serialise schema work across several uvicorn workers / replicas (PostgreSQL only)."""
    if eng.dialect.name != "postgresql":
        yield
        return
    with eng.connect() as c:
        c.exec_driver_sql("SET statement_timeout = 0")
        c.exec_driver_sql("SET lock_timeout = 0")
        c.exec_driver_sql(f"SELECT pg_advisory_lock({MIGRATION_LOCK_KEY})")
        try:
            yield
        finally:
            c.exec_driver_sql(f"SELECT pg_advisory_unlock({MIGRATION_LOCK_KEY})")
            c.commit()


def init_db(eng: Engine | None = None):
    """AUTO_MIGRATE=1 -> alembic upgrade head;  else create_all (dev).  Then install DB-level triggers."""
    eng = eng or engine
    import_all_models()
    with _startup_lock(eng):
        if auto_migrate_enabled():
            from app.db_ops.migrate import upgrade_to_head

            upgrade_to_head(eng)
        else:
            Base.metadata.create_all(eng)
        ensure_db_objects(eng)
