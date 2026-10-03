"""Blob storage: chunked streaming, sha256 integrity verification, orphan detection, list queries stay light."""
import hashlib
import os

import pytest
from sqlalchemy import text

from test_db_support import db, db_engine, empty_db, kind, new_document, new_patient  # noqa: F401

from app.db_ops import integrity
from app.models.documents import Document


def test_document_model_has_no_bytes_column():
    # metadata queries (list/search) can never load file bytes
    assert "data" not in Document.__table__.c
    assert "document_blobs" in Document.metadata.tables


def test_chunked_read_equals_original(db, db_engine):
    p = new_patient(db)
    data = os.urandom(3 * 1024 * 1024 + 123)  # forces multiple 1 MiB chunks + a ragged tail
    d = new_document(db, p.id, data)
    with db_engine.connect() as c:
        chunks = list(integrity.iter_blob_chunks(c, d.id, chunk_size=1024 * 1024))
        assert len(chunks) == 4
        assert b"".join(chunks) == data
        assert integrity.sha256_of_blob(c, d.id) == (hashlib.sha256(data).hexdigest(), len(data))


def test_verify_blobs_clean_then_detects_corruption(db, db_engine):
    p = new_patient(db)
    d1 = new_document(db, p.id, b"alpha" * 1000, name="a.pdf")
    d2 = new_document(db, p.id, b"beta" * 1000, name="b.pdf")
    rep = integrity.verify_blobs(db_engine)
    assert rep["checked"] == 2 and rep["ok"] == 2 and integrity.blobs_clean(rep)

    # flip one byte in the stored blob (simulates bit-rot / bad manual SQL)
    bad = bytearray(b"alpha" * 1000)
    bad[10] ^= 0xFF
    db.execute(text("UPDATE document_blobs SET data = :d WHERE document_id = :i"), {"d": bytes(bad), "i": d1.id})
    db.commit()
    rep = integrity.verify_blobs(db_engine)
    assert rep["bad_hash"] == [d1.id] and rep["ok"] == 1 and not integrity.blobs_clean(rep)

    # size metadata mismatch
    db.execute(text("UPDATE documents SET size_bytes = 5 WHERE id = :i"), {"i": d2.id})
    db.commit()
    assert integrity.verify_blobs(db_engine)["bad_size"] == [d2.id]


def test_missing_and_orphan_blobs_detected(db, db_engine, kind):
    p = new_patient(db)
    d = new_document(db, p.id, b"x" * 500)
    db.execute(text("DELETE FROM document_blobs WHERE document_id = :i"), {"i": d.id})
    db.commit()
    rep = integrity.verify_blobs(db_engine)
    assert rep["missing_blob"] == [d.id]

    # orphan blob: FK enforcement must be bypassed to even create one (it normally cannot exist)
    raw = db_engine.raw_connection()
    try:
        cur = raw.cursor()
        if kind == "sqlite":
            raw.commit()
            cur.execute("PRAGMA foreign_keys=OFF")
            cur.execute("INSERT INTO document_blobs(document_id, data) VALUES ('ghost', x'0102')")
            raw.commit()
            cur.execute("PRAGMA foreign_keys=ON")
        else:
            cur.execute("SET session_replication_role = replica")  # skips FK triggers (superuser)
            cur.execute("INSERT INTO document_blobs(document_id, data) VALUES ('ghost', '\\x0102')")
            cur.execute("SET session_replication_role = DEFAULT")
            raw.commit()
    except Exception as e:  # pragma: no cover
        raw.rollback()
        pytest.skip(f"cannot bypass FK on this server: {e}")
    finally:
        raw.close()
    rep = integrity.verify_blobs(db_engine)
    assert rep["orphan_blobs"] == ["ghost"]
    orphans = integrity.find_orphans(db_engine)
    assert any(o["table"] == "document_blobs" and o["references"] == "documents" for o in orphans)


def test_find_orphans_polymorphic_notifications(db, db_engine):
    from app.models.shared import Notification

    p = new_patient(db)
    db.add(Notification(recipient_type="patient", recipient_id=p.id, kind="k", title="ok"))
    db.add(Notification(recipient_type="patient", recipient_id="nobody", kind="k", title="dangling"))
    db.commit()
    found = integrity.find_orphans(db_engine)
    assert [o for o in found if o["table"] == "notifications" and o["orphans"] == 1]


def test_large_15mb_roundtrip_and_hash(db, db_engine):
    p = new_patient(db)
    data = os.urandom(15 * 1024 * 1024)
    d = new_document(db, p.id, data, name="big.pdf")
    rep = integrity.verify_blobs(db_engine)
    assert rep["ok"] == 1 and rep["total_bytes"] == len(data)
    got = b"".join(integrity.iter_blob_chunks(db_engine.connect(), d.id))
    assert hashlib.sha256(got).hexdigest() == hashlib.sha256(data).hexdigest()
