"""HARD agent: invite-code throttling, minimal public /readyz, per-IP rate limits behind the tunnel,
AES-256-GCM encryption at rest for document blobs and backup files."""
import hashlib
import os
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from sqlalchemy import text

import otp_support as s
from otp_support import outbox, strict  # noqa: F401  (fixtures)
from test_db_support import db, db_engine, empty_db, kind, new_document, new_patient  # noqa: F401

from app import data_crypto as dc
from app import hardening
from app.db_ops import integrity
from app.main import app

KEY = dc.generate_key()
P = "/api/staff/auth"


@pytest.fixture
def limiter():
    hardening.RATE_LIMITER.reset()
    yield hardening.RATE_LIMITER
    hardening.RATE_LIMITER.reset()


# ------------------------------------------------------------------ 1. staff invite code
def test_invite_code_normalized_and_wrong_guesses_throttled(outbox, strict, limiter):
    strict.setenv("STAFF_INVITE_CODE", "Hosp-Invite 7Q")
    strict.setenv("INVITE_MAX_FAILURES", "3")
    body = {"password": "Orange-Falcon-Lantern-42", "name": "New Staff"}
    c = s.client()
    assert c.post(f"{P}/signup", json={**body, "email": s.email(), "invite_code": " hosp invite-7q "}).status_code == 200
    codes = [c.post(f"{P}/signup", json={**body, "email": s.email(), "invite_code": f"nope{i}"}).status_code
             for i in range(3)]
    assert codes == [403, 403, 403]
    r = c.post(f"{P}/signup", json={**body, "email": s.email(), "invite_code": "HOSPINVITE7Q"})
    assert r.status_code == 429 and int(r.headers["Retry-After"]) > 0  # locked out even with the right code
    # another client IP (relayed by the local tunnel) is not affected
    other = TestClient(app, client=("127.0.0.1", 40000))
    r = other.post(f"{P}/signup", json={**body, "email": s.email(), "invite_code": "hospinvite7q"},
                   headers={"CF-Connecting-IP": "198.51.100.77"})
    assert r.status_code == 200


# ------------------------------------------------------------------ 8. session idle timeout
class _Clock:
    def __init__(self):
        self.t = 1_000_000.0

    def monotonic(self):
        return self.t


def test_idle_timeout_ends_session_and_polling_does_not_count(monkeypatch):
    from app import session_idle

    clock = _Clock()
    monkeypatch.setattr(session_idle, "time", clock)
    monkeypatch.delenv("SESSION_IDLE_MINUTES_PATIENT", raising=False)
    c = s.client()
    r = c.post("/api/auth/register", json={"email": s.email(), "password": s.NEW_PW, "name": "Idle Tester"})
    assert r.status_code in (200, 201), r.text
    assert c.get("/api/auth/me").status_code == 200
    clock.t += 25 * 60
    assert c.get("/api/auth/me").status_code == 200  # activity resets the window
    clock.t += 31 * 60
    r = c.get("/api/auth/me")
    assert r.status_code == 401 and "without activity" in r.json()["detail"]
    assert c.get("/api/auth/me").status_code == 401  # the server-side session is gone

    c2 = s.client()
    c2.post("/api/auth/register", json={"email": s.email(), "password": s.NEW_PW, "name": "Idle Tester"})
    assert c2.get("/api/auth/me").status_code == 200
    for _ in range(4):  # background badge polling for 40 min does not keep it alive
        clock.t += 10 * 60
        c2.get("/api/notifications/unread-count")
    assert c2.get("/api/auth/me").status_code == 401


# ------------------------------------------------------------------ 2. /readyz
@pytest.fixture
def prod(strict, limiter):
    strict.setenv("APP_ENV", "production")
    strict.setenv("SECRET_KEY", "test-secret-key-for-hard-tests-only")
    strict.setenv("READYZ_TOKEN", "readyz-test-token-123")
    for k in ("AUTH_RATE_LIMIT_PER_MINUTE", "API_RATE_LIMIT_PER_MINUTE"):
        strict.setenv(k, "0")
    return strict


def test_readyz_public_is_minimal(prod):
    r = TestClient(app).get("/readyz")  # production without SMTP -> not ready
    assert r.status_code == 503 and r.json() == {"status": "unavailable"}
    tunnel = TestClient(app, client=("127.0.0.1", 40001))  # relayed by cloudflared = public
    r = tunnel.get("/readyz", headers={"CF-Connecting-IP": "203.0.113.9", "X-Forwarded-For": "203.0.113.9"})
    assert r.json() == {"status": "unavailable"}
    assert TestClient(app).get("/readyz", headers={"X-Readyz-Token": "wrong"}).json() == {"status": "unavailable"}


def test_readyz_details_with_token_or_direct_loopback(prod):
    r = TestClient(app).get("/readyz", headers={"X-Readyz-Token": "readyz-test-token-123"})
    assert r.status_code == 503 and "checks" in r.json() and r.json()["checks"]["mailer"]["ok"] is False
    r = TestClient(app).get("/readyz", headers={"Authorization": "Bearer readyz-test-token-123"})
    assert "checks" in r.json()
    r = TestClient(app, client=("127.0.0.1", 40002)).get("/readyz")
    assert "checks" in r.json()


def test_readyz_public_ok_body(prod, monkeypatch):
    from app.routers import health

    monkeypatch.setattr(health, "_readyz", lambda response, db: {"status": "ready", "checks": {"x": {"ok": True}}})
    r = TestClient(app).get("/readyz")
    assert r.status_code == 200 and r.json() == {"status": "ok"}


# ------------------------------------------------------------------ 3. rate limits + real client IP
def _probe() -> FastAPI:
    probe = FastAPI()

    @probe.get("/api/ping")
    def ping(request: Request):
        return {"ip": request.client.host, "scheme": request.url.scheme}

    @probe.post("/api/auth/verify-login-otp")
    def otp_verify():
        return {"ok": True}

    @probe.post("/api/auth/login")
    def login():
        return {"ok": True}

    @probe.post("/api/documents")
    def upload():
        return {"ok": True}

    @probe.get("/static/x")
    def static():
        return {"ok": True}

    probe.add_middleware(hardening.HardeningMiddleware)
    probe.add_middleware(hardening.ClientIPMiddleware)
    return probe


def test_real_client_ip_only_from_trusted_proxy(monkeypatch, limiter):
    monkeypatch.delenv("TRUST_PROXY", raising=False)
    monkeypatch.delenv("TRUSTED_PROXY_IPS", raising=False)
    tunnel = TestClient(_probe(), client=("127.0.0.1", 1))
    r = tunnel.get("/api/ping", headers={"CF-Connecting-IP": "198.51.100.5", "X-Forwarded-For": "1.2.3.4, 198.51.100.5",
                                         "X-Forwarded-Proto": "https"})
    assert r.json() == {"ip": "198.51.100.5", "scheme": "https"}
    # no CF header: rightmost X-Forwarded-For entry (left ones are client-supplied and spoofable)
    assert tunnel.get("/api/ping", headers={"X-Forwarded-For": "6.6.6.6, 198.51.100.8"}).json()["ip"] == "198.51.100.8"
    # an untrusted peer cannot spoof its address
    direct = TestClient(_probe(), client=("192.0.2.50", 1))
    assert direct.get("/api/ping", headers={"CF-Connecting-IP": "10.0.0.1", "X-Forwarded-For": "10.0.0.1"}).json()["ip"] == "192.0.2.50"


def test_global_and_strict_limits_return_429_with_retry_after(monkeypatch, limiter):
    for k, v in {"API_RATE_LIMIT_PER_MINUTE": "8", "AUTH_RATE_LIMIT_PER_MINUTE": "5",
                 "OTP_RATE_LIMIT_PER_MINUTE": "2", "UPLOAD_RATE_LIMIT_PER_MINUTE": "3"}.items():
        monkeypatch.setenv(k, v)
    c = TestClient(_probe(), client=("127.0.0.1", 1))
    alice, bob = {"CF-Connecting-IP": "198.51.100.1"}, {"CF-Connecting-IP": "198.51.100.2"}

    assert [c.post("/api/auth/verify-login-otp", headers=alice).status_code for _ in range(3)] == [200, 200, 429]
    r = c.post("/api/auth/verify-login-otp", headers=alice)
    assert r.status_code == 429 and int(r.headers["Retry-After"]) >= 1 and "code" in r.json()["detail"]
    assert c.post("/api/auth/verify-login-otp", headers=bob).status_code == 200  # per real client IP

    assert [c.post("/api/documents", headers=bob).status_code for _ in range(4)] == [200, 200, 200, 429]

    limiter.reset()
    codes = [c.get("/api/ping", headers=alice).status_code for _ in range(9)]
    assert codes[:8] == [200] * 8 and codes[8] == 429
    assert c.get("/static/x", headers=alice).status_code == 200  # only /api is limited
    assert c.get("/api/ping", headers=bob).status_code == 200


def test_production_defaults_enable_api_limits(monkeypatch):
    from app import settings

    for k in ("API_RATE_LIMIT_PER_MINUTE", "OTP_RATE_LIMIT_PER_MINUTE", "UPLOAD_RATE_LIMIT_PER_MINUTE"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.delenv("AUTH_RATE_LIMIT_PER_MINUTE", raising=False)
    assert settings.api_rate_limit_per_minute() > settings.auth_rate_limit_per_minute() > settings.otp_rate_limit_per_minute() > 0
    assert settings.upload_rate_limit_per_minute() > 0
    monkeypatch.setenv("APP_ENV", "development")
    assert settings.api_rate_limit_per_minute() == 0


# ------------------------------------------------------------------ 4. encryption at rest
@pytest.fixture
def key_on(monkeypatch):
    monkeypatch.setenv("DATA_ENCRYPTION_KEY", KEY)
    monkeypatch.delenv("DATA_ENCRYPTION_KEYS_OLD", raising=False)
    monkeypatch.delenv("BACKUP_ENCRYPTION_KEY", raising=False)
    return monkeypatch


def _stored(db, doc_id) -> bytes:
    return bytes(db.execute(text("SELECT data FROM document_blobs WHERE document_id = :i"), {"i": doc_id}).scalar())


def test_blob_encrypted_on_write_and_decrypted_on_read(db, key_on):
    from app.models.documents import DocumentBlob

    plain = b"%PDF-1.7 lab report for a synthetic patient " * 50
    d = new_document(db, new_patient(db).id, data=plain)
    raw = _stored(db, d.id)
    assert raw[:4] == dc.MAGIC and b"synthetic patient" not in raw
    db.expire_all()
    assert bytes(db.get(DocumentBlob, d.id).data) == plain
    assert b"".join(integrity.iter_blob_chunks(db.connection(), d.id, 100)) == plain
    rep = integrity.verify_blobs(db.connection())
    assert integrity.blobs_clean(rep) and rep["encrypted"] >= 1


def test_legacy_plaintext_rows_still_read(db, key_on):
    from app.models.documents import DocumentBlob

    plain = os.urandom(3000)
    key_on.delenv("DATA_ENCRYPTION_KEY")
    d = new_document(db, new_patient(db).id, data=plain)
    assert _stored(db, d.id) == plain  # written without a key = plaintext
    key_on.setenv("DATA_ENCRYPTION_KEY", KEY)
    db.expire_all()
    assert bytes(db.get(DocumentBlob, d.id).data) == plain
    assert integrity.blobs_clean(integrity.verify_blobs(db.connection()))


def test_tampering_and_wrong_key_are_detected(db, key_on):
    d = new_document(db, new_patient(db).id, data=os.urandom(1000))
    raw = bytearray(_stored(db, d.id))
    raw[-5] ^= 0x01
    db.execute(text("UPDATE document_blobs SET data = :d WHERE document_id = :i"), {"d": bytes(raw), "i": d.id})
    db.commit()
    rep = integrity.verify_blobs(db.connection())
    assert d.id in rep["undecryptable"] and not integrity.blobs_clean(rep)
    with pytest.raises(dc.DecryptionError):
        dc.decrypt_blob(bytes(raw))
    good = dc.encrypt_blob(b"hello")
    key_on.setenv("DATA_ENCRYPTION_KEY", dc.generate_key())
    with pytest.raises(dc.DecryptionError):
        dc.decrypt_blob(good)
    key_on.setenv("DATA_ENCRYPTION_KEYS_OLD", KEY)  # rotation: old key still decrypts
    assert dc.decrypt_blob(good) == b"hello"


def test_encrypt_existing_blobs_script(db_engine, key_on, monkeypatch, capsys):
    from sqlalchemy.orm import sessionmaker

    key_on.delenv("DATA_ENCRYPTION_KEY")
    sess = sessionmaker(bind=db_engine)()
    plains = [os.urandom(500), b"plain text note"]
    ids = [new_document(sess, new_patient(sess).id, data=p).id for p in plains]
    sess.close()
    key_on.setenv("DATA_ENCRYPTION_KEY", KEY)
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
    import encrypt_existing_blobs as script

    url = db_engine.url.render_as_string(hide_password=False)
    monkeypatch.setattr(sys, "argv", ["x", "--url", url, "--no-backup-check"])
    assert script.main() == 0
    assert "encrypted=2" in capsys.readouterr().out
    with db_engine.connect() as c:
        for i, p in zip(ids, plains):
            raw = bytes(c.execute(text("SELECT data FROM document_blobs WHERE document_id = :i"), {"i": i}).scalar())
            assert raw[:4] == dc.MAGIC and dc.decrypt_blob(raw) == p
        sha = c.execute(text("SELECT sha256 FROM documents WHERE id = :i"), {"i": ids[0]}).scalar()
    assert sha == hashlib.sha256(plains[0]).hexdigest()  # integrity hash is of the plaintext
    assert integrity.blobs_clean(integrity.verify_blobs(db_engine))
    assert script.main() == 0 and "already=2" in capsys.readouterr().out  # idempotent


def test_backup_file_encryption_roundtrip_and_truncation(tmp_path, key_on):
    src = tmp_path / "db.sqlite3"
    src.write_bytes(os.urandom(dc.FILE_CHUNK * 2 + 123))
    enc = dc.encrypt_file(src, tmp_path / "db.sqlite3.enc")
    assert dc.is_encrypted_file(enc) and src.read_bytes()[:64] not in enc.read_bytes()
    out = dc.decrypt_file(enc, tmp_path / "out.sqlite3")
    assert out.read_bytes() == src.read_bytes()
    cut = tmp_path / "cut.enc"
    cut.write_bytes(enc.read_bytes()[:-(dc.FILE_CHUNK // 2)])
    with pytest.raises(dc.DecryptionError):
        dc.decrypt_file(cut, tmp_path / "cut.out")
    assert not (tmp_path / "cut.out").exists()
