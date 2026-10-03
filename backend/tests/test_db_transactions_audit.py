"""Transaction atomicity (record + audit + notification commit/rollback together) and audit append-only."""
import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from test_db_support import db, db_engine, empty_db, kind, new_document, new_patient  # noqa: F401

from app.models.documents import Document
from app.models.shared import AuditLog, Notification, Patient
from app.services.audit import log_event
from app.services.notifications import notify


def _counts(db):
    return (db.scalar(select(func.count()).select_from(Patient)),
            db.scalar(select(func.count()).select_from(AuditLog)),
            db.scalar(select(func.count()).select_from(Notification)))


def test_record_audit_notification_commit_together(db):
    p = new_patient(db, commit=False)
    log_event(db, actor_type="patient", actor_id=p.id, patient_id=p.id, action="signup")
    notify(db, recipient_type="patient", recipient_id=p.id, kind="doc_uploaded", title="Welcome")
    db.commit()
    assert _counts(db) == (1, 1, 1)


def test_failure_in_last_step_rolls_back_everything(db):
    p = new_patient(db, commit=False)
    log_event(db, actor_type="patient", actor_id=p.id, patient_id=p.id, action="signup")
    with pytest.raises((ValueError, IntegrityError)):
        # invalid recipient_type/kind causes a failure -> the WHOLE unit must roll back
        notify(db, recipient_type="bogus", recipient_id=p.id, kind="x", title="t")
        db.commit()
    db.rollback()
    assert _counts(db) == (0, 0, 0)


def test_error_after_flush_leaves_no_partial_document(db):
    p = new_patient(db)
    with pytest.raises(RuntimeError):
        d = new_document(db, p.id, b"x" * 100, commit=False)  # document + blob flushed, not committed
        log_event(db, actor_type="patient", actor_id=p.id, patient_id=p.id, action="doc_upload",
                  resource_type="document", resource_id=d.id)
        raise RuntimeError("boom before commit")
    db.rollback()
    assert db.scalar(select(func.count()).select_from(Document)) == 0
    assert db.scalar(text("SELECT count(*) FROM document_blobs")) == 0
    assert db.scalar(select(func.count()).select_from(AuditLog)) == 0


def test_get_db_dependency_rolls_back_uncommitted_work(db_engine, monkeypatch):
    import app.db as appdb
    from sqlalchemy.orm import sessionmaker

    monkeypatch.setattr(appdb, "SessionLocal", sessionmaker(bind=db_engine, expire_on_commit=False))
    gen = appdb.get_db()
    s = next(gen)
    s.add(Patient(email="gone@example.test", password_hash="x", legal_name="Gone"))
    s.flush()
    with pytest.raises(StopIteration):
        next(gen)  # request ends without commit -> rolled back
    with db_engine.connect() as c:
        assert c.execute(text("SELECT count(*) FROM patients")).scalar() == 0

    gen = appdb.get_db()
    s = next(gen)
    s.add(Patient(email="gone2@example.test", password_hash="x", legal_name="Gone"))
    s.flush()
    with pytest.raises(ValueError):
        gen.throw(ValueError("handler failed"))
    with db_engine.connect() as c:
        assert c.execute(text("SELECT count(*) FROM patients")).scalar() == 0


# ------------------------------------------------------------------ audit_log is append-only
def test_audit_insert_ok_update_delete_blocked(db):
    row = log_event(db, actor_type="staff", actor_id="s1", patient_id="p1", action="view_document")
    db.commit()
    rid = row.id
    for sql in ("UPDATE audit_log SET action = 'tampered' WHERE id = :i", "DELETE FROM audit_log WHERE id = :i"):
        with pytest.raises(DBAPIError) as ei:
            db.execute(text(sql), {"i": rid})
            db.commit()
        db.rollback()
        assert "append-only" in str(ei.value)
    assert db.scalar(text("SELECT action FROM audit_log WHERE id = :i"), {"i": rid}) == "view_document"


def test_audit_orm_update_and_delete_blocked(db):
    row = log_event(db, actor_type="system", actor_id=None, patient_id=None, action="boot")
    db.commit()
    row.action = "edited"
    with pytest.raises(DBAPIError):
        db.commit()
    db.rollback()
    db.delete(db.get(AuditLog, row.id))
    with pytest.raises(DBAPIError):
        db.commit()
    db.rollback()


def test_audit_survives_patient_deletion(db):
    p = new_patient(db)
    log_event(db, actor_type="patient", actor_id=p.id, patient_id=p.id, action="login")
    db.commit()
    db.execute(text("DELETE FROM patients WHERE id = :i"), {"i": p.id})
    db.commit()
    assert db.scalar(text("SELECT count(*) FROM audit_log")) == 1  # no FK: history outlives the account


def test_audit_truncate_blocked_on_postgres(db, kind):
    if kind != "postgres":
        pytest.skip("PostgreSQL only")
    log_event(db, actor_type="system", actor_id=None, patient_id=None, action="boot")
    db.commit()
    with pytest.raises(DBAPIError):
        db.execute(text("TRUNCATE audit_log"))
        db.commit()
    db.rollback()


def test_triggers_reported_present(db_engine):
    from app.db_ops.triggers import audit_triggers_present

    with db_engine.connect() as c:
        assert audit_triggers_present(c)
