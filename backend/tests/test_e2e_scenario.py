"""End-to-end scenario from CONTRACT.md (W1 integration):
patient uploads a PDF + a photo -> hospital searches the patient by name -> sees NO shared records -> requests access ->
patient is notified and approves -> hospital sees and opens the files -> patient sees an audit entry + notification ->
patient revokes -> hospital loses access.
"""
import os
import struct
import tempfile
import uuid
import zlib
from datetime import timedelta

import pytest

os.environ.setdefault("HP_DB_PATH", os.path.join(tempfile.mkdtemp(prefix="hp_e2e_"), "test.db"))

from fastapi.testclient import TestClient  # noqa: E402

from app.db import SessionLocal  # noqa: E402
from app.main import app  # noqa: E402
from app.models.core import PatientSession  # noqa: E402
from app.models.shared import Patient, utcnow  # noqa: E402
from app.security import hash_password, hash_token, new_token  # noqa: E402
from app.services.staff_seed import DEMO_PASSWORD, seed_staff  # noqa: E402

PDF = b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog >>\nendobj\ntrailer\n<< /Root 1 0 R >>\n%%EOF\n"


def tiny_png() -> bytes:
    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    raw = b"\x00" + bytes([200, 30, 30]) * 4
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 4, 1, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


@pytest.fixture(scope="module")
def world():
    db = SessionLocal()
    seed_staff(db, quiet=True)
    name = f"Zelda Quillfeather{uuid.uuid4().hex[:4]}"
    p = Patient(email=f"e2e-{uuid.uuid4().hex[:8]}@example.test", password_hash=hash_password("x"), legal_name=name, dob="1985-05-05")
    db.add(p)
    db.commit()
    token = new_token()
    db.add(PatientSession(token_hash=hash_token(token), patient_id=p.id, expires_at=utcnow() + timedelta(hours=2)))
    db.commit()
    patient = TestClient(app)
    patient.cookies.set("hp_patient", token)
    staff = TestClient(app)
    r = staff.post("/api/staff/login", json={"email": "doctor@riverside.demo", "password": DEMO_PASSWORD})
    assert r.status_code == 200, r.text
    other = TestClient(app)  # a physician at a DIFFERENT organization
    assert other.post("/api/staff/login", json={"email": "doctor@lakeside.demo", "password": DEMO_PASSWORD}).status_code == 200
    yield dict(db=db, p=p, name=name, patient=patient, staff=staff, other=other)
    db.close()


def test_full_scenario(world):
    patient, staff, other, p, name = world["patient"], world["staff"], world["other"], world["p"], world["name"]

    # 1. patient uploads a PDF and a photo
    r1 = patient.post("/api/documents", files={"file": ("lab-report.pdf", PDF, "application/pdf")}, data={"name": "Lab report", "category": "lab_result"})
    assert r1.status_code == 201, r1.text
    r2 = patient.post("/api/documents", files={"file": ("rash.png", tiny_png(), "image/png")}, data={"name": "Photo of rash", "category": "other"})
    assert r2.status_code == 201, r2.text
    pdf_id, png_id = r1.json()["id"], r2.json()["id"]

    # 2. hospital searches by name: found, but identity-only
    res = staff.get("/api/staff/patients/search", params={"q": "Quillfeather"}).json()["results"]
    hit = next(x for x in res if x["id"] == p.id)
    assert "dob" not in hit and "email" not in hit and "phone" not in hit
    assert staff.get(f"/api/staff/patients/{p.id}").status_code in (403, 409, 428)  # DOB must be confirmed first
    assert staff.post(f"/api/staff/patients/{p.id}/confirm", json={"dob": "1999-01-01"}).status_code == 403
    assert staff.post(f"/api/staff/patients/{p.id}/confirm", json={"dob": p.dob}).status_code == 200

    # 3. no shared records yet
    page = staff.get(f"/api/staff/patients/{p.id}").json()
    assert page["documents"] == [] and page["scope"]["authorized"] is False
    assert staff.get(f"/api/staff/patients/{p.id}/documents/{pdf_id}/file").status_code in (403, 404)

    # 4. request access -> patient notified
    rq = staff.post(f"/api/staff/patients/{p.id}/request-access",
                    json={"categories": [], "document_ids": [pdf_id, png_id], "purpose": "Second opinion on lab results", "duration_days": 7})
    assert rq.status_code in (200, 201), rq.text
    notes = patient.get("/api/notifications").json()
    items = notes["items"] if isinstance(notes, dict) else notes
    assert any(n["kind"] == "access_request" for n in items)

    # 5. patient approves
    pending = patient.get("/api/sharing/requests").json()
    plist = pending["items"] if isinstance(pending, dict) and "items" in pending else pending.get("requests", pending) if isinstance(pending, dict) else pending
    req_id = plist[0]["id"]
    ap = patient.post(f"/api/sharing/requests/{req_id}/approve", json={})
    assert ap.status_code == 200, ap.text
    assert patient.get("/api/shares/active").json(), "dashboard 'active shares' should now list the grant"

    # 6. hospital sees and opens the files; another organization cannot
    page = staff.get(f"/api/staff/patients/{p.id}").json()
    assert {d["id"] for d in page["documents"]} == {pdf_id, png_id}
    f = staff.get(f"/api/staff/patients/{p.id}/documents/{pdf_id}/file")
    assert f.status_code == 200 and f.content == PDF
    assert other.post(f"/api/staff/patients/{p.id}/confirm", json={"dob": p.dob}).status_code == 200
    assert other.get(f"/api/staff/patients/{p.id}/documents/{pdf_id}/file").status_code in (403, 404)

    # 7. patient sees an audit entry + a notification about the view
    audit = patient.get("/api/audit/mine").json()
    aitems = audit["items"] if isinstance(audit, dict) else audit
    assert any("document" in (a.get("action") or "") and a.get("actor_type") == "staff" for a in aitems), aitems[:3]
    notes = patient.get("/api/notifications").json()
    items = notes["items"] if isinstance(notes, dict) else notes
    assert any(n["kind"] == "document_viewed" for n in items)

    # 8. patient revokes -> hospital loses access
    grants = patient.get("/api/sharing/grants").json()
    glist = grants["items"] if isinstance(grants, dict) and "items" in grants else grants.get("grants", grants) if isinstance(grants, dict) else grants
    assert glist
    for g in glist:
        assert patient.delete(f"/api/sharing/grants/{g['id']}").status_code in (200, 204)
    assert staff.get(f"/api/staff/patients/{p.id}/documents/{pdf_id}/file").status_code in (403, 404)
    assert staff.get(f"/api/staff/patients/{p.id}").json()["documents"] == []
    assert patient.get("/api/shares/active").json() == []
