"""Shared helpers for W9 tests (appointments, caregivers, privacy). Not a test module."""
import os
import tempfile
import uuid
import warnings
from datetime import timedelta

import pytest

warnings.filterwarnings("ignore")
os.environ.setdefault("HP_DB_PATH", os.path.join(tempfile.mkdtemp(prefix="hp_w9_"), "test.db"))
os.environ["HP_DISABLE_SWEEP"] = "1"

from fastapi.testclient import TestClient  # noqa: E402

from app.db import SessionLocal  # noqa: E402
from app.main import app  # noqa: E402
from app.models.core import PatientSession  # noqa: E402
from app.models.shared import AuditLog, Notification, Patient, Provider, utcnow  # noqa: E402
from app.models.staff import StaffUser  # noqa: E402
from app.security import hash_password, hash_token, new_token  # noqa: E402

GOOD_PW = "Correct-Horse-Battery-9!"
INTAKE = {"reason": "My knee hurts when I climb stairs", "availability": "Weekday mornings", "visit_type": "in_person"}


class World:
    """Two patients (A guardian-type, B stranger), two providers each with a nurse, logged-in clients."""

    def __init__(self):
        self.db = SessionLocal()
        db = self.db
        self.A = self._patient("Alice Anders", "1980-01-02")
        self.B = self._patient("Bob Brown", "1975-05-06")
        self.provs = [Provider(name=f"Riverside Clinic {uuid.uuid4().hex[:4]}", specialty="Family medicine"),
                      Provider(name=f"Eye Care {uuid.uuid4().hex[:4]}", specialty="Ophthalmology")]
        db.add_all(self.provs)
        db.flush()
        self.staff = []
        for prov in self.provs:
            s = StaffUser(name="Nurse Test", email=f"s-{uuid.uuid4().hex[:8]}@example.test", password_hash="x",
                          role="nurse", provider_id=prov.id)
            db.add(s)
            self.staff.append(s)
        db.commit()
        self._clients = {}

    def _patient(self, name, dob):
        p = Patient(email=f"{name.split()[0].lower()}-{uuid.uuid4().hex[:8]}@example.test",
                    password_hash=hash_password("x"), legal_name=name, dob=dob, phone="555-0100")
        self.db.add(p)
        self.db.flush()
        return p

    def client(self, patient) -> TestClient:
        if patient.id not in self._clients:
            token = new_token()
            self.db.add(PatientSession(token_hash=hash_token(token), patient_id=patient.id,
                                       expires_at=utcnow() + timedelta(hours=1)))
            self.db.commit()
            c = TestClient(app)
            c.cookies.set("hp_patient", token)
            self._clients[patient.id] = c
        return self._clients[patient.id]

    def anon(self) -> TestClient:
        return TestClient(app)

    def notes(self, rtype, rid, kind=None):
        self.db.expire_all()
        q = self.db.query(Notification).filter_by(recipient_type=rtype, recipient_id=rid)
        if kind:
            q = q.filter_by(kind=kind)
        return q.all()

    def audit(self, patient_id, action=None):
        self.db.expire_all()
        q = self.db.query(AuditLog).filter_by(patient_id=patient_id)
        if action:
            q = q.filter_by(action=action)
        return q.all()

    def request(self, patient, provider=None, **intake):
        """Submit an appointment request via the API; returns the JSON."""
        provider = provider or self.provs[0]
        body = {"provider_id": provider.id, "intake": {**INTAKE, **intake}, "submit": True}
        r = self.client(patient).post("/api/appt/appointments", json=body)
        assert r.status_code == 201, r.text
        return r.json()

    def caregiver(self, patient, email=None, *, cats=("medications",), appts=False, bills=False, days=30, monkeypatch=None):
        """Invite + accept; returns (client logged in as caregiver, link_id, email)."""
        email = email or f"cg-{uuid.uuid4().hex[:8]}@example.test"
        monkeypatch.setenv("DEV_SHOW_OTP", "1")
        r = self.client(patient).post("/api/care/caregivers", json={
            "email": email, "name": "Casey Care", "relationship": "daughter", "record_categories": list(cats),
            "manage_appointments": appts, "manage_bills": bills, "access_days": days})
        assert r.status_code == 201, r.text
        token = r.json()["dev_invite_token"]
        cg = TestClient(app)
        a = cg.post("/api/caregiver/accept", json={"token": token, "name": "Casey Care", "password": GOOD_PW})
        assert a.status_code == 200, a.text
        return cg, r.json()["item"]["id"], email


@pytest.fixture
def world():
    w = World()
    yield w
    w.db.close()
