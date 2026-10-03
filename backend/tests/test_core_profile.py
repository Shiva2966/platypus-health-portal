"""W1 tests: profile, emergency contacts (authorization + emergency-vs-authorized distinction), gaps, security headers, shell."""
import os
import tempfile
import uuid
from datetime import timedelta

import pytest

os.environ.setdefault("HP_DB_PATH", os.path.join(tempfile.mkdtemp(prefix="hp_w1_"), "test.db"))

from fastapi.testclient import TestClient  # noqa: E402

from app.db import SessionLocal  # noqa: E402
from app.main import app  # noqa: E402
from app.models.core import PatientSession  # noqa: E402
from app.models.shared import Patient, utcnow  # noqa: E402
from app.security import hash_password, hash_token, new_token  # noqa: E402


def make_patient(db, name="Core Test"):
    p = Patient(email=f"c-{uuid.uuid4().hex[:10]}@example.test", password_hash=hash_password("x"), legal_name=name, dob="1990-01-01")
    db.add(p)
    db.commit()
    return p


def client_for(db, p) -> TestClient:
    token = new_token()
    db.add(PatientSession(token_hash=hash_token(token), patient_id=p.id, expires_at=utcnow() + timedelta(hours=1)))
    db.commit()
    c = TestClient(app)
    c.cookies.set("hp_patient", token)
    return c


@pytest.fixture
def world():
    db = SessionLocal()
    a, b = make_patient(db, "Alice A"), make_patient(db, "Bob B")
    yield db, a, b, client_for(db, a), client_for(db, b)
    db.close()


CONTACT = {"name": "Riley", "relationship": "sister", "phone": "555-0100"}


def test_routers_loaded_without_failures():
    r = TestClient(app).get("/api/health").json()
    assert "patient_profile" in r["routers"]
    assert r["failed_routers"] == {}


def test_requires_login():
    c = TestClient(app)
    for method, url in [("get", "/api/profile"), ("get", "/api/emergency-contacts"), ("get", "/api/auth/me"), ("get", "/api/profile/gaps")]:
        assert getattr(c, method)(url).status_code == 401


def test_patient_id_is_random_uuid_without_pii(world):
    _, a, _, ca, _ = world
    pid = ca.get("/api/profile").json()["profile"]["id"]
    assert pid == a.id and uuid.UUID(pid).version == 4
    assert "alice" not in pid.lower() and "1990" not in pid


def test_profile_update_and_validation(world):
    _, _, _, ca, _ = world
    ok = ca.put("/api/profile", json={"legal_name": "Alice Anne A", "dob": "1990-01-01", "phone": "555-0111"})
    assert ok.status_code == 200 and ok.json()["legal_name"] == "Alice Anne A"
    bad = ca.put("/api/profile", json={"legal_name": "", "dob": "2999-01-01"})
    assert bad.status_code == 422
    f = bad.json()["fields"]
    assert {"legal_name", "dob"} <= set(f)
    # optional fields stay optional
    assert ca.put("/api/profile", json={"legal_name": "A", "dob": "1990-01-01" }).status_code == 200


def test_email_change_must_use_verified_flow(world):
    _, a, b, ca, _ = world
    r = ca.put("/api/profile", json={"legal_name": "A", "dob": "1990-01-01", "email": "someone.else@example.test"})
    assert r.status_code == 409 and "Change email" in r.json()["detail"]
    # sending the unchanged email is fine
    assert ca.put("/api/profile", json={"legal_name": "A", "dob": "1990-01-01", "email": a.email}).status_code == 200

def test_cannot_set_verification_status_via_profile(world):
    _, _, _, ca, _ = world
    ca.put("/api/profile", json={"legal_name": "A", "dob": "1990-01-01", "verification_status": "verified"})
    assert ca.get("/api/profile").json()["profile"]["verification_status"] == "unverified"
    assert ca.post("/api/profile/request-verification").json()["verification_status"] == "pending"


def test_identity_change_resets_verified(world):
    db, a, _, ca, _ = world
    from app.services.identity import set_verification
    set_verification(db, patient_id=a.id, status="verified", staff_id=None)
    db.commit()
    assert ca.get("/api/profile").json()["profile"]["verification_status"] == "verified"
    ca.put("/api/profile", json={"legal_name": "Alice Changed", "dob": "1990-01-01"})
    assert ca.get("/api/profile").json()["profile"]["verification_status"] == "unverified"


def test_contacts_crud_primary_and_distinction(world):
    _, _, _, ca, _ = world
    first = ca.post("/api/emergency-contacts", json=CONTACT).json()
    assert first["is_primary"] is True  # first contact becomes primary
    assert first["authorized_to_access_records"] is False  # emergency contact != authorized
    second = ca.post("/api/emergency-contacts", json={**CONTACT, "name": "Sam", "is_primary": True, "authorized_to_access_records": True}).json()
    items = {i["id"]: i for i in ca.get("/api/emergency-contacts").json()["items"]}
    assert items[second["id"]]["is_primary"] and not items[first["id"]]["is_primary"]
    assert items[second["id"]]["authorized_to_access_records"] and not items[first["id"]]["authorized_to_access_records"]
    assert ca.post("/api/emergency-contacts", json=CONTACT).status_code == 409  # duplicate
    assert ca.post("/api/emergency-contacts", json={"name": "x"}).status_code == 422


def test_patient_cannot_touch_other_patients_contacts(world):
    _, _, _, ca, cb = world
    mine = ca.post("/api/emergency-contacts", json=CONTACT).json()
    assert cb.get("/api/emergency-contacts").json()["items"] == []
    assert cb.put(f"/api/emergency-contacts/{mine['id']}", json={**CONTACT, "name": "Hacked"}).status_code == 404
    assert cb.delete(f"/api/emergency-contacts/{mine['id']}").status_code == 404
    assert ca.get("/api/emergency-contacts").json()["items"][0]["name"] == "Riley"


def test_gaps_and_dashboard_inputs(world):
    _, _, _, ca, _ = world
    keys = {g["key"] for g in ca.get("/api/profile/gaps").json()["items"]}
    assert {"phone", "contact", "identity", "allergies"} <= keys
    ca.post("/api/emergency-contacts", json=CONTACT)
    keys = {g["key"] for g in ca.get("/api/profile/gaps").json()["items"]}
    assert "contact" not in keys


def test_security_headers_and_cross_origin_block(world):
    _, _, _, ca, _ = world
    r = ca.get("/api/profile")
    assert r.headers["cache-control"] == "no-store" and r.headers["x-content-type-options"] == "nosniff"
    assert "script-src 'self'" in r.headers["content-security-policy"]
    evil = ca.put("/api/profile", json={"legal_name": "A", "dob": "1990-01-01"}, headers={"Origin": "https://evil.example"})
    assert evil.status_code == 403


def test_pwa_files_and_module_loader():
    c = TestClient(app)
    assert c.get("/").status_code == 200 and "DEMO ONLY" in c.get("/").text
    assert c.get("/manifest.webmanifest").json()["display"] == "standalone"
    assert c.get("/sw.js").status_code == 200
    assert c.get("/static/icons/icon-192.png").headers["content-type"] == "image/png"
    scripts = c.get("/api/ui-modules").json()["scripts"]
    assert "/static/js/core_dashboard.js" in scripts and "/static/js/core_profile.js" in scripts
