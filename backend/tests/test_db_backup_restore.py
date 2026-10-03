"""Backup -> restore into a fresh DB -> row counts + blob hashes match.  Tamper / safety checks."""
import hashlib
import json
import os
import time
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

from test_db_support import db, db_engine, empty_db, kind, new_document, new_patient, scratch_url  # noqa: F401

from app.db_ops import backup as bk
from app.db_ops import integrity
from app.services.audit import log_event
from app.services.notifications import notify


@pytest.fixture
def populated(db, db_engine):
    p1, p2 = new_patient(db), new_patient(db)
    docs = [new_document(db, p1.id, os.urandom(2 * 1024 * 1024 + 7), name="big.pdf"),
            new_document(db, p1.id, b"tiny", name="t.txt"),
            new_document(db, p2.id, os.urandom(50_000), name="p2.pdf")]
    for p in (p1, p2):
        log_event(db, actor_type="patient", actor_id=p.id, patient_id=p.id, action="signup")
        notify(db, recipient_type="patient", recipient_id=p.id, kind="doc_uploaded", title="hi")
    db.commit()
    return db_engine, docs


def _restore_target(kind, tmp_path):
    url, cleanup = scratch_url(kind, tmp_path)
    if kind == "sqlite":
        return url.replace("db_", "restored_"), cleanup
    return url, cleanup


def test_backup_restore_roundtrip_counts_and_blob_hashes(populated, kind, tmp_path):
    engine, docs = populated
    out = bk.create_backup(engine.url.render_as_string(hide_password=False), tmp_path / "bk", keep=5)
    m = bk.read_manifest(out)
    assert m["engine"] == ("sqlite" if kind == "sqlite" else "postgresql")
    assert m["row_counts"]["documents"] == 3 and m["blobs"]["ok"] == 3 and m["blobs"]["clean"]

    target, cleanup = _restore_target(kind, tmp_path)
    try:
        res = bk.restore_backup(out, target)
        assert res["ok"], res["problems"]
        assert res["row_counts"] == m["row_counts"]
        eng2 = create_engine(target)
        try:
            # every blob byte-identical to the original
            for d in docs:
                with engine.connect() as a, eng2.connect() as b:
                    assert integrity.sha256_of_blob(a, d.id) == integrity.sha256_of_blob(b, d.id)
            # audit triggers survive the restore (append-only still enforced)
            with eng2.connect() as c:
                from app.db_ops.triggers import audit_triggers_present
                assert audit_triggers_present(c)
        finally:
            eng2.dispose()
    finally:
        cleanup()


def test_restore_refuses_non_empty_target_without_force(populated, kind, tmp_path):
    engine, _ = populated
    out = bk.create_backup(engine.url.render_as_string(hide_password=False), tmp_path / "bk")
    target, cleanup = _restore_target(kind, tmp_path)
    try:
        assert bk.restore_backup(out, target)["ok"]
        with pytest.raises(bk.BackupError, match="force|exists|not empty"):
            bk.restore_backup(out, target)
        assert bk.restore_backup(out, target, force=True)["ok"]
    finally:
        cleanup()


def test_tampered_backup_is_rejected(populated, kind, tmp_path):
    engine, _ = populated
    out = bk.create_backup(engine.url.render_as_string(hide_password=False), tmp_path / "bk")
    raw = bytearray(out.read_bytes())
    raw[len(raw) // 2] ^= 0xFF
    out.write_bytes(bytes(raw))
    target, cleanup = _restore_target(kind, tmp_path)
    try:
        with pytest.raises(bk.BackupError, match="checksum"):
            bk.restore_backup(out, target)
    finally:
        cleanup()


def test_verify_detects_restore_that_lost_a_row(populated, kind, tmp_path):
    engine, _ = populated
    out = bk.create_backup(engine.url.render_as_string(hide_password=False), tmp_path / "bk")
    m = bk.read_manifest(out)
    m["row_counts"]["patients"] += 1  # pretend the backup had one more patient than the restore
    target, cleanup = _restore_target(kind, tmp_path)
    try:
        assert bk.restore_backup(out, target)["ok"]
        eng2 = create_engine(target)
        res = bk.verify_against_manifest(eng2, m)
        eng2.dispose()
        assert not res["ok"] and any("row count mismatch in patients" in p for p in res["problems"])
    finally:
        cleanup()


def test_retention_prunes_oldest_and_keeps_newest(populated, tmp_path):
    engine, _ = populated
    url = engine.url.render_as_string(hide_password=False)
    made = []
    for _ in range(4):
        made.append(bk.create_backup(url, tmp_path / "bk", keep=None))
        time.sleep(0.02)
    gone = bk.prune_backups(tmp_path / "bk", "healthportal", keep=2)
    left = sorted(p.name for p in (tmp_path / "bk").iterdir() if not p.name.endswith(".manifest.json"))
    assert len(gone) == 2 and left == sorted(p.name for p in made[-2:])
    assert not any((tmp_path / "bk" / (g.name + ".manifest.json")).exists() for g in gone)


def test_manifest_contains_no_patient_data(populated, tmp_path):
    engine, _ = populated
    out = bk.create_backup(engine.url.render_as_string(hide_password=False), tmp_path / "bk")
    text_ = out.with_name(out.name + ".manifest.json").read_text()
    assert "example.test" not in text_ and "Test Patient" not in text_


def test_backup_cli_end_to_end_sqlite(tmp_path):
    """scripts/backup.py + restore.py on a real SQLite file, as an operator would run them."""
    import subprocess
    import sys

    root = Path(__file__).resolve().parent.parent
    db_file = tmp_path / "cli.db"
    env = {**os.environ, "DATABASE_URL": f"sqlite:///{db_file}"}
    env.pop("HP_DB_PATH", None)
    env.pop("HP_TEST_DATABASE_URL", None)
    code = ("import app.main\nfrom app.db import SessionLocal\nfrom app.models.shared import Patient\n"
            "s=SessionLocal(); s.add(Patient(email='cli@example.test',password_hash='x',legal_name='Cli')); s.commit()")
    subprocess.run([sys.executable, "-c", code], cwd=root, env=env, check=True, capture_output=True)
    r = subprocess.run([sys.executable, "scripts/backup.py", "--dest", str(tmp_path / "b"), "--verify-restore"],
                       cwd=root, env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "PASSED" in r.stdout
    r = subprocess.run([sys.executable, "scripts/restore.py", "--latest", "--dir", str(tmp_path / "b"),
                        "--target", f"sqlite:///{tmp_path / 'restored.db'}"],
                       cwd=root, env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    r = subprocess.run([sys.executable, "scripts/verify_blobs.py", "--url", f"sqlite:///{tmp_path / 'restored.db'}"],
                       cwd=root, env=env, capture_output=True, text=True)
    assert r.returncode == 0 and "CLEAN" in r.stdout, r.stdout + r.stderr
