"""Shared fixtures/helpers for W8 tests (insurance + billing). Not a test module itself."""
import os
import tempfile
import uuid
from datetime import timedelta

import pytest

# Must be set before app.db is imported anywhere (first importer wins when running the whole suite).
os.environ.setdefault("HP_DB_PATH", os.path.join(tempfile.mkdtemp(prefix="hp_w8_"), "test.db"))

from fastapi.testclient import TestClient  # noqa: E402

from app.db import SessionLocal  # noqa: E402
from app.main import app  # noqa: E402
from app.models.core import PatientSession  # noqa: E402
from app.models.documents import ShareGrant  # noqa: E402
from app.models.shared import Patient, Provider, utcnow  # noqa: E402
from app.models.staff import StaffSession, StaffUser  # noqa: E402
from app.security import hash_password, hash_token, new_token  # noqa: E402


def plan_payload(**kw):
    base = dict(insurer_name="Evergreen Health Plan", plan_name="Choice PPO", plan_type="ppo",
                member_id=f"EV-{uuid.uuid4().hex[:8].upper()}", group_number="GRP-1", policyholder_name="Pat Test",
                policyholder_relationship="self", coverage_rank="primary", effective_date="2026-01-01",
                deductible_annual="1500.00", copay_amount="30.00", coinsurance_pct="20")
    base.update(kw)
    return base


def bill_payload(**kw):
    base = dict(provider_name=f"Test Clinic {uuid.uuid4().hex[:4]}", service_date="2026-08-01", billed_amount="500.00",
                amount_due="100.00", due_date="2026-12-01")
    base.update(kw)
    return base


def claim_payload(**kw):
    base = dict(claim_number=f"CLM-{uuid.uuid4().hex[:8].upper()}", insurer_name="Evergreen Health Plan",
                provider_name="Test Clinic", service_date="2026-08-01", billed_amount="500.00",
                allowed_amount="300.00", insurer_paid="240.00", deductible="0.00", copay="30.00",
                coinsurance="30.00", patient_responsibility="60.00", status="paid")
    base.update(kw)
    return base


class World:
    """Two patients (A, B), one provider+staff with a session, one other provider+staff."""

    def __init__(self):
        self.db = SessionLocal()
        db = self.db
        self.A, self.B = [Patient(email=f"w8-{i}-{uuid.uuid4().hex[:8]}@example.test", password_hash=hash_password("x"),
                                  legal_name=f"Billing Patient {i}") for i in range(2)]
        db.add_all([self.A, self.B])
        self.prov = Provider(name=f"W8 Hospital {uuid.uuid4().hex[:4]}")
        self.other_prov = Provider(name=f"W8 Other {uuid.uuid4().hex[:4]}")
        db.add_all([self.prov, self.other_prov])
        db.flush()
        self.staff = StaffUser(name="Nora Nurse", email=f"s-{uuid.uuid4().hex[:8]}@example.test", password_hash="x",
                               role="nurse", provider_id=self.prov.id)
        self.other_staff = StaffUser(name="Olive Other", email=f"o-{uuid.uuid4().hex[:8]}@example.test",
                                     password_hash="x", role="nurse", provider_id=self.other_prov.id)
        db.add_all([self.staff, self.other_staff])
        db.commit()
        self._clients = {}

    def client(self, patient) -> TestClient:
        if patient.id not in self._clients:
            tok = new_token()
            self.db.add(PatientSession(token_hash=hash_token(tok), patient_id=patient.id,
                                       expires_at=utcnow() + timedelta(hours=1)))
            self.db.commit()
            c = TestClient(app)
            c.cookies.set("hp_patient", tok)
            self._clients[patient.id] = c
        return self._clients[patient.id]

    def staff_client(self, staff) -> TestClient:
        key = "staff-" + staff.id
        if key not in self._clients:
            tok = new_token()
            self.db.add(StaffSession(token_hash=hash_token(tok), staff_id=staff.id,
                                     expires_at=utcnow() + timedelta(hours=1)))
            self.db.commit()
            c = TestClient(app)
            c.cookies.set("hp_staff", tok)
            self._clients[key] = c
        return self._clients[key]

    def confirm(self, patient, *staff):
        """Staff 'confirmed the patient's date of birth' (W3 anti mix-up step) - needed before any patient view."""
        from app.models.staff import StaffPatientConfirmation
        for s in staff or (self.staff, self.other_staff):
            self.db.query(StaffPatientConfirmation).filter_by(staff_id=s.id, patient_id=patient.id).delete()
            self.db.add(StaffPatientConfirmation(staff_id=s.id, patient_id=patient.id))
        self.db.commit()

    def grant_billing(self, patient, provider, days=7):
        g = ShareGrant(patient_id=patient.id, provider_id=provider.id, scope_type="category", category="billing",
                       purpose="test", expires_at=utcnow() + timedelta(days=days))
        self.db.add(g)
        self.db.commit()
        return g

    def notifications(self, patient, kind=None):
        from app.models.shared import Notification
        self.db.expire_all()
        q = self.db.query(Notification).filter_by(recipient_type="patient", recipient_id=patient.id)
        return q.filter_by(kind=kind).all() if kind else q.all()

    def audit(self, patient, action=None):
        from app.models.shared import AuditLog
        self.db.expire_all()
        q = self.db.query(AuditLog).filter_by(patient_id=patient.id)
        return q.filter_by(action=action).all() if action else q.all()


@pytest.fixture
def world():
    w = World()
    yield w
    w.db.close()
