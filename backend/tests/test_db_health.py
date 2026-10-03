"""/healthz and /readyz."""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from test_db_support import db_engine, empty_db, kind  # noqa: F401

from app.db import get_db
from app.routers import health


def _client(engine):
    app = FastAPI()
    app.include_router(health.router)
    Session = sessionmaker(bind=engine, expire_on_commit=False)

    def _get_db():
        s = Session()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = _get_db
    return TestClient(app)


def test_healthz_never_touches_db(empty_db):
    r = _client(empty_db).get("/healthz")
    assert r.status_code == 200 and r.json() == {"status": "ok"}
    assert r.headers["cache-control"] == "no-store"


def test_readyz_ready_on_dev_schema(db_engine, monkeypatch):
    monkeypatch.delenv("AUTO_MIGRATE", raising=False)
    r = _client(db_engine).get("/readyz")
    body = r.json()
    assert r.status_code == 200, body
    assert body["status"] == "ready"
    assert body["checks"]["database"]["ok"] and body["checks"]["audit_append_only"]["ok"]
    assert body["checks"]["migrations"]["mode"] == "create_all"
    assert "password" not in r.text.lower()


def test_readyz_503_when_auto_migrate_but_unmanaged(db_engine, monkeypatch):
    monkeypatch.setenv("AUTO_MIGRATE", "1")  # prod mode requires alembic to be at head
    r = _client(db_engine).get("/readyz")
    assert r.status_code == 503 and r.json()["status"] == "not_ready"


def test_readyz_ready_after_alembic_upgrade(empty_db, monkeypatch):
    from app.db_ops.migrate import upgrade_to_head

    monkeypatch.setenv("AUTO_MIGRATE", "1")
    upgrade_to_head(empty_db)
    r = _client(empty_db).get("/readyz")
    assert r.status_code == 200, r.json()
    assert r.json()["checks"]["migrations"]["mode"] == "alembic"


def test_readyz_503_when_audit_triggers_missing(db_engine, kind, monkeypatch):
    monkeypatch.delenv("AUTO_MIGRATE", raising=False)
    with db_engine.begin() as c:
        if kind == "sqlite":
            c.exec_driver_sql("DROP TRIGGER audit_log_no_update")
        else:
            c.exec_driver_sql("DROP TRIGGER audit_log_no_change ON audit_log")
    r = _client(db_engine).get("/readyz")
    assert r.status_code == 503
    assert r.json()["checks"]["audit_append_only"]["ok"] is False


def test_readyz_503_when_database_down(monkeypatch, tmp_path):
    from app.db import make_engine

    eng = make_engine("postgresql+psycopg://nobody:x@127.0.0.1:1/none?connect_timeout=1")
    r = _client(eng).get("/readyz")
    assert r.status_code == 503
    assert r.json()["checks"]["database"]["ok"] is False


def test_real_app_exposes_health_routes():
    from app.main import app

    c = TestClient(app)
    assert c.get("/healthz").status_code == 200
    assert c.get("/readyz").status_code in (200, 503)
