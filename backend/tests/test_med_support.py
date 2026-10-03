"""Shared fixtures for W7 tests (medical records). Not a test module itself."""
import os
import tempfile
import uuid
from datetime import timedelta

import pytest

# Must be set before app.db is imported anywhere (first importer wins when running the whole suite).
os.environ.setdefault("HP_DB_PATH", os.path.join(tempfile.mkdtemp(prefix="hp_w7_"), "test.db"))

from fastapi.testclient import TestClient  # noqa: E402

from app.db import SessionLocal  # noqa: E402
from app.main import app  # noqa: E402
from app.models.core import PatientSession  # noqa: E402
from app.models.shared import Patient, utcnow  # noqa: E402
from app.security import hash_password, hash_token, new_token  # noqa: E402

MED = "/api/med"


class MedWorld:
    """Two patients with logged-in clients."""

    def __init__(self):
        self.db = SessionLocal()
        self.A, self.B = [self.new_patient(f"Med Test {i}") for i in range(2)]
        self.clients = {}

    def new_patient(self, name):
        p = Patient(email=f"m-{uuid.uuid4().hex[:10]}@example.test", password_hash=hash_password("x"), legal_name=name)
        self.db.add(p)
        self.db.commit()
        return p

    def client(self, patient) -> TestClient:
        if patient.id not in self.clients:
            token = new_token()
            self.db.add(PatientSession(token_hash=hash_token(token), patient_id=patient.id,
                                       expires_at=utcnow() + timedelta(hours=1)))
            self.db.commit()
            c = TestClient(app)
            c.cookies.set("hp_patient", token)
            self.clients[patient.id] = c
        return self.clients[patient.id]

    def anon(self) -> TestClient:
        return TestClient(app)

    def create(self, patient, res, body, expect=201):
        r = self.client(patient).post(f"{MED}/{res}", json=body)
        assert r.status_code == expect, r.text
        return r.json()

    def fresh(self, patient):
        """Re-read a patient row (other sessions changed it)."""
        self.db.expire_all()
        return self.db.get(Patient, patient.id)


@pytest.fixture
def mw():
    w = MedWorld()
    yield w
    w.db.close()
