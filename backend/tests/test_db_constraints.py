"""DB-level constraint enforcement: FK / UNIQUE / CHECK / CASCADE, and schema-wide reliability rules."""
import os
import uuid
from datetime import datetime, timedelta

import pytest
from sqlalchemy import DateTime, Float, String, inspect, select, text
from sqlalchemy.exc import IntegrityError

from test_db_support import db, db_engine, empty_db, kind, new_document, new_patient  # noqa: F401

from app.db import Base, import_all_models
from app.db_ops.types import UTCDateTime
from app.models.documents import Document, DocumentBlob, ShareGrant
from app.models.shared import AuditLog, Notification, Patient, Provider, utcnow


def test_foreign_key_rejects_unknown_parent(db):
    db.add(Document(patient_id=str(uuid.uuid4()), name="x.pdf", mime_type="application/pdf", size_bytes=1,
                    sha256="a" * 64))
    with pytest.raises(IntegrityError):
        db.commit()  # FK checked at flush or commit (deferrable) - never silently accepted
    db.rollback()


def test_blob_requires_document(db):
    db.add(DocumentBlob(document_id=str(uuid.uuid4()), data=b"abc"))
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_unique_email_case_insensitive(db):
    new_patient(db, "Same@Example.test")
    db.add(Patient(email="same@example.TEST", password_hash="x", legal_name="Dup"))
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_live_document_unique_per_patient_sha(db):
    p = new_patient(db)
    d1 = new_document(db, p.id, b"same-bytes", name="a.pdf")
    # second live copy of the same bytes for the same patient -> rejected
    with pytest.raises(IntegrityError):
        new_document(db, p.id, b"same-bytes", name="b.pdf")
    db.rollback()
    # after soft-delete the same file can be uploaded again
    d1 = db.get(Document, d1.id)
    d1.deleted_at = datetime(2026, 1, 1)
    db.commit()
    new_document(db, p.id, b"same-bytes", name="c.pdf")
    # ...and a different patient may hold the same file
    other = new_patient(db)
    new_document(db, other.id, b"same-bytes", name="d.pdf")


@pytest.mark.parametrize("bad", [
    dict(size_bytes=-1),
    dict(size_bytes=15 * 1024 * 1024 + 1),
    dict(sha256="short"),
    dict(source="hacker"),
    dict(name=""),
])
def test_document_check_constraints(db, bad):
    p = new_patient(db)
    kw = dict(patient_id=p.id, name="ok.pdf", mime_type="application/pdf", size_bytes=10, sha256="b" * 64)
    kw.update(bad)
    db.add(Document(**kw))
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_notification_and_audit_check_constraints(db):
    db.add(Notification(recipient_type="alien", recipient_id="x", kind="k", title="t"))
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()
    db.add(AuditLog(actor_type="ghost", action="x"))
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_not_null_enforced(db):
    db.add(Patient(email=None, password_hash="x", legal_name="n"))
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_cascade_delete_patient_removes_documents_and_blobs(db):
    p = new_patient(db)
    d = new_document(db, p.id)
    did = d.id
    db.execute(text("DELETE FROM patients WHERE id = :i"), {"i": p.id})
    db.commit()
    assert db.execute(text("SELECT count(*) FROM documents WHERE id = :i"), {"i": did}).scalar() == 0
    assert db.execute(text("SELECT count(*) FROM document_blobs WHERE document_id = :i"), {"i": did}).scalar() == 0


def test_restrict_delete_provider_with_grant_is_cascade_but_explicit(db):
    """Provider deletion cascades to its grants (documented), never leaves dangling grants."""
    p = new_patient(db)
    d = new_document(db, p.id)
    prov = Provider(name="Clinic")
    db.add(prov)
    db.commit()
    db.add(ShareGrant(patient_id=p.id, provider_id=prov.id, scope_type="document", document_id=d.id,
                      expires_at=utcnow() + timedelta(days=1)))
    db.commit()
    db.execute(text("DELETE FROM providers WHERE id = :i"), {"i": prov.id})
    db.commit()
    assert db.execute(text("SELECT count(*) FROM share_grants")).scalar() == 0


# ------------------------------------------------------------------ schema-wide rules (CONTRACT.md)
@pytest.fixture(scope="module")
def metadata():
    import_all_models()
    return Base.metadata


def test_every_fk_has_explicit_ondelete(metadata):
    bad = [f"{t.name}.{','.join(c.name for c in fk.columns)}" for t in metadata.tables.values()
           for fk in t.foreign_key_constraints if not fk.ondelete]
    assert not bad, f"foreign keys without explicit ondelete: {bad}"


def test_every_fk_column_is_indexed(metadata):
    bad = []
    for t in metadata.tables.values():
        for fk in t.foreign_key_constraints:
            first = list(fk.columns)[0]
            covered = (first.primary_key and list(t.primary_key.columns)[0] is first) or any(
                list(i.expressions)[0] is first for i in t.indexes) or any(
                list(getattr(c, "columns", []))[:1] == [first] and type(c).__name__ in
                ("UniqueConstraint", "PrimaryKeyConstraint") for c in t.constraints)
            if not covered:
                bad.append(f"{t.name}.{first.name}")
    assert not bad, f"FK columns without index: {bad}"


def test_no_float_money_and_utc_timestamps(metadata):
    floats = [f"{t.name}.{c.name}" for t in metadata.tables.values() for c in t.columns if isinstance(c.type, Float)]
    assert not floats, f"use Numeric(12,2) instead of float: {floats}"
    naive = [f"{t.name}.{c.name}" for t in metadata.tables.values() for c in t.columns
             if isinstance(c.type, DateTime) and not isinstance(c.type, UTCDateTime)]
    assert not naive, f"timestamps must be UTC-aware types: {naive}"


def test_string_ids_are_uuid_sized(metadata):
    bad = []
    for t in metadata.tables.values():
        for c in t.columns:
            if c.name == "id" and c.primary_key and isinstance(c.type, String) and c.type.length != 36:
                bad.append(f"{t.name}.id length={c.type.length}")
    assert not bad, bad


def test_postgres_uses_timestamptz_and_bytea(db_engine, kind):
    if kind != "postgres":
        pytest.skip("PostgreSQL only")
    with db_engine.connect() as c:
        types = dict(c.execute(text(
            "SELECT column_name || '@' || table_name, data_type FROM information_schema.columns "
            "WHERE table_name IN ('documents','document_blobs','patients')")).all())
    assert types["created_at@documents"] == "timestamp with time zone"
    assert types["data@document_blobs"] == "bytea"


def test_utc_roundtrip_is_naive_utc_and_comparable(db):
    from datetime import timezone

    p = new_patient(db)
    aware = datetime(2026, 5, 1, 12, 0, tzinfo=timezone(timedelta(hours=-5)))  # 17:00 UTC
    p.deletion_requested_at = aware
    db.commit()
    db.expire_all()
    got = db.get(Patient, p.id).deletion_requested_at
    assert got.tzinfo is None and got == datetime(2026, 5, 1, 17, 0)
    # comparisons against naive-UTC python values work server-side and client-side
    assert db.scalar(select(Patient.id).where(Patient.deletion_requested_at < datetime(2026, 6, 1))) == p.id
    assert got < utcnow()
