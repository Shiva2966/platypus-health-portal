"""Shared fixtures for the database-reliability tests (W5).

Every test gets its OWN scratch database, on SQLite always and on PostgreSQL when a server is available
(set HP_TEST_DATABASE_URL to any PostgreSQL URL, e.g. the one printed by `python scripts/pg_dev.py newdb`).
Scratch databases are created next to that server and dropped afterwards.
"""
import hashlib
import os
import tempfile
import uuid

os.environ.setdefault("HP_DB_PATH", os.path.join(tempfile.mkdtemp(prefix="hp_w5_"), "test.db"))

import pytest  # noqa: E402
from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from app.db import Base, import_all_models, make_engine  # noqa: E402
from app.db_ops.triggers import ensure_db_objects  # noqa: E402

PG_URL = os.environ.get("HP_TEST_DATABASE_URL") or os.environ.get("PG_TEST_ADMIN_URL")
ENGINES = ["sqlite"] + (["postgres"] if PG_URL else [])


def _pg_admin():
    u = make_url(PG_URL if "+psycopg" in PG_URL else PG_URL.replace("postgresql://", "postgresql+psycopg://"))
    return create_engine(u.set(database="postgres"), isolation_level="AUTOCOMMIT"), u


def scratch_url(kind: str, tmp_path):
    """Return (url, cleanup_fn) for a brand-new empty database."""
    if kind == "sqlite":
        return f"sqlite:///{tmp_path / ('db_' + uuid.uuid4().hex[:8] + '.db')}", lambda: None
    admin, u = _pg_admin()
    name = "hp_t_" + uuid.uuid4().hex[:12]
    with admin.connect() as c:
        c.execute(text(f'CREATE DATABASE "{name}"'))

    def cleanup():
        with admin.connect() as c:
            c.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()

    return u.set(database=name).render_as_string(hide_password=False), cleanup


@pytest.fixture(params=ENGINES)
def kind(request):
    return request.param


@pytest.fixture
def empty_db(kind, tmp_path):
    url, cleanup = scratch_url(kind, tmp_path)
    eng = make_engine(url)
    yield eng
    eng.dispose()
    cleanup()


@pytest.fixture
def db_engine(empty_db):
    """Engine with the full schema (create_all + audit triggers) - same objects the app creates in dev."""
    import_all_models()
    Base.metadata.create_all(empty_db)
    ensure_db_objects(empty_db)
    return empty_db


@pytest.fixture
def db(db_engine):
    s = sessionmaker(bind=db_engine, expire_on_commit=False)()
    yield s
    s.rollback()
    s.close()


# ------------------------------------------------------------------ builders
def new_patient(db, email=None, commit=True):
    from app.models.shared import Patient

    p = Patient(email=email or f"p{uuid.uuid4().hex[:8]}@example.test", password_hash="x", legal_name="Test Patient")
    db.add(p)
    if commit:
        db.commit()
    else:
        db.flush()
    return p


def new_document(db, patient_id, data: bytes | None = None, name="scan.pdf", commit=True):
    from app.models.documents import Document, DocumentBlob

    data = data if data is not None else os.urandom(2048)
    d = Document(patient_id=patient_id, name=name, mime_type="application/pdf", size_bytes=len(data),
                 sha256=hashlib.sha256(data).hexdigest())
    db.add(d)
    db.flush()
    db.add(DocumentBlob(document_id=d.id, data=data))
    if commit:
        db.commit()
    return d
