"""Shared helpers/fixtures for W2 tests (documents + sharing). Not a test module itself."""
import io
import os
import tempfile
import uuid
import zipfile
from datetime import timedelta

import pytest

# Must be set before app.db is imported anywhere (first importer wins when running the whole suite).
os.environ.setdefault("LOGIN_OTP_REQUIRED", "0")      # per CONTRACT: keep old register/login behaviour in tests
os.environ.setdefault("SIGNUP_VERIFY_REQUIRED", "0")
os.environ.setdefault("HP_DB_PATH", os.path.join(tempfile.mkdtemp(prefix="hp_w2_"), "test.db"))

from fastapi.testclient import TestClient  # noqa: E402

from app.db import SessionLocal  # noqa: E402
from app.main import app  # noqa: E402
from app.models.core import PatientSession  # noqa: E402
from app.models.shared import Patient, Provider, utcnow  # noqa: E402
from app.models.staff import StaffUser  # noqa: E402
from app.security import hash_password, hash_token, new_token  # noqa: E402
from app.services import consent  # noqa: E402

PDF = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"
PNG = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
       b"\x00\x00\x00\rIDATx\x9cc\xf8\xff\xff?\x00\x05\xfe\x02\xfe\xa7\x9a\xa0\xa0\x00\x00\x00\x00IEND\xaeB`\x82")
JPG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00\xff\xd9"
HEIC = b"\x00\x00\x00\x18ftypheic\x00\x00\x00\x00mif1heic"
WEBP = b"RIFF\x1a\x00\x00\x00WEBPVP8 \x0e\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00"


def make_docx(tag: str = "x") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("word/document.xml", f"<w:document>{tag}</w:document>")
    return buf.getvalue()


def uniq(prefix=b"") -> bytes:
    """Unique valid PDF so duplicate detection doesn't trip across tests."""
    return b"%PDF-1.4\n% " + uuid.uuid4().hex.encode() + b"\n%%EOF\n" + prefix


class World:
    """Two patients, two providers (with staff), plus logged-in clients."""

    def __init__(self):
        self.db = SessionLocal()
        db = self.db
        self.patients = []
        for i in range(2):
            p = Patient(email=f"p{i}-{uuid.uuid4().hex[:8]}@example.test", password_hash=hash_password("x"),
                        legal_name=f"Test Patient {i}")
            db.add(p)
            self.patients.append(p)
        self.provs = [Provider(name=f"General Hospital {uuid.uuid4().hex[:4]}"),
                      Provider(name=f"Eye Clinic {uuid.uuid4().hex[:4]}")]
        db.add_all(self.provs)
        db.flush()
        self.staff = []
        for prov in self.provs:
            s = StaffUser(name="Nurse Test", email=f"s-{uuid.uuid4().hex[:8]}@example.test",
                          password_hash="x", role="nurse", provider_id=prov.id)
            db.add(s)
            self.staff.append(s)
        db.commit()
        self.A, self.B = self.patients
        self.clients = {}

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

    def upload(self, patient, data=None, name="Lab result", filename="a.pdf", category="lab_result", **extra):
        data = data if data is not None else uniq()
        r = self.client(patient).post("/api/documents", data={"name": name, "category": category, **extra},
                                      files={"file": (filename, data, "application/octet-stream")})
        assert r.status_code == 201, r.text
        return r.json()

    def notifications(self, rtype, rid, kind=None):
        from app.models.shared import Notification
        self.db.expire_all()
        q = self.db.query(Notification).filter_by(recipient_type=rtype, recipient_id=rid)
        if kind:
            q = q.filter_by(kind=kind)
        return q.all()

    def audit(self, patient_id, action=None):
        from app.models.shared import AuditLog
        self.db.expire_all()
        q = self.db.query(AuditLog).filter_by(patient_id=patient_id)
        if action:
            q = q.filter_by(action=action)
        return q.all()

    def view(self, staff, patient, doc_id):
        return consent.get_document_for_staff(self.db, staff_id=staff.id, staff_role=staff.role,
                                              patient_id=patient.id, document_id=doc_id)

    def visible(self, staff, patient):
        return consent.list_visible_documents(self.db, staff_id=staff.id, staff_role=staff.role,
                                              patient_id=patient.id)


@pytest.fixture
def world():
    consent.reset_token_attempts()
    w = World()
    yield w
    w.db.close()
