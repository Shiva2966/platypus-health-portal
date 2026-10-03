"""Shared helpers for W3 tests (staff portal + notifications). Not a test module."""
import os
import tempfile
import uuid
import warnings

warnings.filterwarnings("ignore")
os.environ.setdefault("HP_DB_PATH", os.path.join(tempfile.mkdtemp(prefix="hp_w3_"), "test.db"))
os.environ["HP_DISABLE_SWEEP"] = "1"


def legacy_auth_env(monkeypatch):
    """W6 e-mail OTP is bypassed for W3 tests only (immediate session on login). Use from an autouse fixture."""
    monkeypatch.setenv("LOGIN_OTP_REQUIRED", "0")
    monkeypatch.setenv("SIGNUP_VERIFY_REQUIRED", "0")

from fastapi.testclient import TestClient  # noqa: E402

from app.db import SessionLocal  # noqa: E402
from app.main import app  # noqa: E402
from app.services.staff_seed import DEMO_PASSWORD, seed_staff  # noqa: E402

PDF = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"
STRONG = "Correct-Horse-Battery-9!"


def new_client() -> TestClient:
    return TestClient(app)


def db():
    return SessionLocal()


def seeded() -> dict:
    s = db()
    try:
        return seed_staff(s, quiet=True)
    finally:
        s.close()


def make_patient(name: str | None = None, dob: str = "1980-04-12"):
    """Register a synthetic patient. Returns (client, patient_id, name, dob)."""
    # Created directly in the DB with a real server-side session: independent of how W1's
    # registration flow evolves (e-mail verification codes etc.).
    from datetime import timedelta

    from app.models.core import PatientSession
    from app.models.shared import Patient, utcnow
    from app.security import PATIENT_COOKIE, hash_password, hash_token, new_token

    c = new_client()
    name = name or f"Testy {uuid.uuid4().hex[:8].title()}"
    s = db()
    try:
        p = Patient(email=f"{uuid.uuid4().hex}@example.test", password_hash=hash_password(STRONG),
                    legal_name=name, dob=dob)
        s.add(p)
        s.flush()
        token = new_token()
        s.add(PatientSession(token_hash=hash_token(token), patient_id=p.id, expires_at=utcnow() + timedelta(hours=2)))
        s.commit()
        pid = p.id
    finally:
        s.close()
    c.cookies.set(PATIENT_COOKIE, token)
    return c, pid, name, dob


def staff_login(email: str = "nurse@riverside.demo") -> TestClient:
    seeded()
    c = new_client()
    r = c.post("/api/staff/login", json={"email": email, "password": DEMO_PASSWORD})
    assert r.status_code == 200, r.text
    return c


def staff_id(email: str) -> str:
    from app.models.staff import StaffUser
    from sqlalchemy import select
    s = db()
    try:
        return s.scalar(select(StaffUser.id).where(StaffUser.email == email))
    finally:
        s.close()


def upload(patient_client: TestClient, name: str, category: str = "lab_result", private: bool = False,
           body: bytes | None = None) -> str:
    data = body or (PDF + uuid.uuid4().hex.encode())
    r = patient_client.post("/api/documents", data={"name": name, "category": category,
                                                    "is_private": "true" if private else "false"},
                            files={"file": (f"{uuid.uuid4().hex[:6]}.pdf", data, "application/pdf")})
    assert r.status_code == 201, r.text
    return r.json()["id"]


INTAKE = {"reason": "Persistent cough", "symptoms": "Dry cough, mild fever", "onset": "3 days ago",
          "availability": "Weekday mornings", "visit_type": "in_person", "accommodations": "Large print"}


def submit_appointment(patient_id: str, provider_id: str, intake: dict | None = None) -> str:
    """Patient sends an appointment request (through W9's service, same code path as the patient API)."""
    from app.services import appointments as svc
    s = db()
    try:
        a = svc.create(s, patient_id=patient_id, provider_id=provider_id, intake=intake or INTAKE, submit=True)
        s.commit()
        assert a.status == "requested"
        return a.id
    finally:
        s.close()


def riverside_id() -> str:
    return seeded()["providers"]["Riverside General Hospital"]


def grant_via_request(staff_client: TestClient, patient_client: TestClient, patient_id: str, dob: str,
                      categories: list[str], days: int = 7):
    """Staff confirms DOB + requests access; patient approves. Returns request id."""
    assert staff_client.post(f"/api/staff/patients/{patient_id}/confirm", json={"dob": dob}).status_code == 200
    r = staff_client.post(f"/api/staff/patients/{patient_id}/request-access",
                          json={"categories": categories, "purpose": "Pre-visit review", "duration_days": days})
    assert r.status_code == 200, r.text
    rid = r.json()["id"]
    a = patient_client.post(f"/api/sharing/requests/{rid}/approve", json={})
    assert a.status_code == 200, a.text
    return rid
