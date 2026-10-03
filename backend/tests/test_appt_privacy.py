"""Privacy settings, accessibility prefs, connected apps, export (JSON + ZIP), deletion workflow."""
# appt_support MUST be imported first: it points HP_DB_PATH at a temp DB before app.db is loaded.
from tests.appt_support import INTAKE, world  # noqa: F401  # isort: skip

import io
import json
import zipfile

from app.models.shared import Patient
from app.services import appointments as svc


def test_settings_defaults_update_and_validation(world):
    w = world
    c = w.client(w.A)
    s = c.get("/api/settings/privacy").json()
    assert s["settings"]["default_share_days"] == 30 and s["settings"]["share_requires_my_approval"] is True
    assert s["settings"]["allow_marketing"] is False and s["deletion_request"] is None
    assert "retain" in s["retention_explanation"].lower() or "kept" in s["retention_explanation"].lower()
    r = c.put("/api/settings/privacy", json={"default_share_days": 7, "allow_sms": True, "default_share_categories": ["lab_result"]})
    assert r.status_code == 200 and r.json()["settings"]["default_share_days"] == 7 and r.json()["settings"]["allow_sms"] is True
    assert r.json()["settings"]["default_share_categories"] == ["lab_result"]
    assert w.audit(w.A.id, "privacy_settings_changed")
    for bad in ({"default_share_days": 0}, {"default_share_days": 9999}, {"text_size": "huge"}, {"allow_email": "yes"}):
        assert c.put("/api/settings/privacy", json=bad).status_code == 422, bad
    # settings are per patient
    assert w.client(w.B).get("/api/settings/privacy").json()["settings"]["default_share_days"] == 30
    # there is deliberately no language setting
    assert "language" not in json.dumps(s).lower().replace("language-neutral", "")
    assert c.put("/api/settings/privacy", json={"language": "fr"}).status_code == 200
    assert "language" not in c.get("/api/settings/privacy").json()["settings"]


def test_accessibility_prefs(world):
    c = world.client(world.A)
    assert c.get("/api/settings/accessibility").json() == {"text_size": "normal", "high_contrast": False, "reduced_motion": False}
    r = c.put("/api/settings/accessibility", json={"text_size": "xlarge", "high_contrast": True, "reduced_motion": True})
    assert r.json() == {"text_size": "xlarge", "high_contrast": True, "reduced_motion": True}
    assert c.get("/api/settings/accessibility").json()["text_size"] == "xlarge"
    assert c.put("/api/settings/accessibility", json={"text_size": "tiny"}).status_code == 422
    assert world.client(world.B).get("/api/settings/accessibility").json()["high_contrast"] is False
    assert world.anon().get("/api/settings/accessibility").status_code == 401


def test_connected_apps(world):
    w = world
    c = w.client(w.A)
    assert c.get("/api/settings/connected-apps").json()["items"] == []
    r = c.post("/api/settings/connected-apps", json={"app_key": "fitsync"})
    assert r.status_code == 201
    assert c.post("/api/settings/connected-apps", json={"app_key": "fitsync"}).status_code == 409
    assert c.post("/api/settings/connected-apps", json={"app_key": "evil"}).status_code == 422
    d = c.get("/api/settings/connected-apps").json()
    assert [a["key"] for a in d["items"]] == ["fitsync"] and "fitsync" not in [a["key"] for a in d["available"]]
    assert w.client(w.B).delete(f"/api/settings/connected-apps/{r.json()['id']}").status_code == 404
    assert c.delete(f"/api/settings/connected-apps/{r.json()['id']}").status_code == 200
    assert c.get("/api/settings/connected-apps").json()["items"] == []
    assert w.audit(w.A.id, "app_disconnected")


def _add_document(w, patient, name="Lab report.pdf", data=b"%PDF-1.4 hello export"):
    import hashlib
    from app.models.documents import Document, DocumentBlob
    d = Document(patient_id=patient.id, name=name, category="lab_result", mime_type="application/pdf", size_bytes=len(data),
                 sha256=hashlib.sha256(data + name.encode()).hexdigest())
    w.db.add(d)
    w.db.flush()
    w.db.add(DocumentBlob(document_id=d.id, data=data))
    w.db.commit()
    return d


def test_export_json_contents_and_isolation(world, monkeypatch):
    w = world
    c = w.client(w.A)
    a = w.request(w.A, symptoms="ache", onset="monday")
    _add_document(w, w.A)
    _add_document(w, w.B, name="Bobs secret.pdf")
    c.post("/api/care/dependents", json={"name": "Dana Anders", "relationship": "daughter", "dob": "2015-03-04"})
    w.caregiver(w.A, monkeypatch=monkeypatch)
    c.post("/api/settings/connected-apps", json={"app_key": "carecalendar"})
    r = c.get("/api/settings/export.json")
    assert r.status_code == 200 and "attachment" in r.headers["content-disposition"]
    data = r.json()
    assert data["profile"]["legal_name"] == "Alice Anders" and data["profile"]["id"] == w.A.id
    assert [x["id"] for x in data["appointments"]] == [a["id"]]
    assert data["appointments"][0]["intake"]["symptoms"] == "ache" and len(data["appointments"][0]["history"]) == 2
    assert [d["name"] for d in data["documents"]] == ["Lab report.pdf"]
    assert len(data["caregivers"]) == 1 and len(data["dependents"]) == 1 and len(data["connected_apps"]) == 1
    assert data["privacy_settings"]["default_share_days"] == 30
    assert any(n["kind"] == "appointment_update" for n in data["notifications"])
    assert any(l["action"] == "appointment_submitted" for l in data["activity_log"])
    for key in ("medical", "insurance_and_billing", "sharing", "emergency_contacts"):
        assert key in data
    blob = r.text.lower()
    assert "bobs secret" not in blob and w.B.id not in r.text and "bob brown" not in blob
    for secret in ("password_hash", "scrypt$", "$argon2", "token_hash", "invite_token"):
        assert secret not in blob
    assert w.audit(w.A.id, "data_exported")
    assert w.anon().get("/api/settings/export.json").status_code == 401


def test_export_zip_includes_documents(world):
    w = world
    c = w.client(w.A)
    _add_document(w, w.A, name="Scan one.pdf", data=b"%PDF-1.4 AAA")
    _add_document(w, w.A, name="../../evil name.pdf", data=b"%PDF-1.4 BBB")
    _add_document(w, w.B, name="Not mine.pdf", data=b"%PDF-1.4 CCC")
    r = c.get("/api/settings/export.zip")
    assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
    z = zipfile.ZipFile(io.BytesIO(r.content))
    names = z.namelist()
    assert "data.json" in names and "README.txt" in names
    docs = [n for n in names if n.startswith("documents/")]
    assert len(docs) == 2 and all(".." not in n and n.count("/") == 1 for n in docs)
    contents = sorted(z.read(n) for n in docs)
    assert contents == [b"%PDF-1.4 AAA", b"%PDF-1.4 BBB"]
    data = json.loads(z.read("data.json"))
    assert all(d["included_in_zip"] for d in data["documents"]) and {d["zip_path"] for d in data["documents"]} == set(docs)
    assert b"CCC" not in r.content and w.audit(w.A.id, "data_exported")


def test_deletion_request_workflow(world):
    w = world
    c = w.client(w.A)
    assert c.post("/api/settings/deletion-request", json={}).status_code == 422  # must confirm
    r = c.post("/api/settings/deletion-request", json={"confirm": True, "reason": "Moving away"})
    assert r.status_code == 201
    req = r.json()["request"]
    assert req["status"] == "pending" and req["earliest_completion_at"] > req["requested_at"]
    assert "retention" in r.json()["retention_explanation"].lower() or "kept" in r.json()["retention_explanation"].lower()
    assert c.post("/api/settings/deletion-request", json={"confirm": True}).status_code == 409
    assert c.get("/api/settings/privacy").json()["deletion_request"]["status"] == "pending"
    w.db.expire_all()
    assert w.db.get(Patient, w.A.id).deletion_requested_at is not None
    # nothing is erased: data and account still work
    assert c.get("/api/appt/appointments").status_code == 200
    # other patient has no request; cannot cancel A's
    assert w.client(w.B).get("/api/settings/deletion-request").json()["request"] is None
    assert w.client(w.B).delete("/api/settings/deletion-request").status_code == 404
    assert c.delete("/api/settings/deletion-request").status_code == 200
    w.db.expire_all()
    assert w.db.get(Patient, w.A.id).deletion_requested_at is None
    assert c.get("/api/settings/deletion-request").json()["request"] is None
    assert [a.action for a in w.audit(w.A.id) if a.action.startswith("deletion_")] == ["deletion_requested", "deletion_cancelled"]
    # can ask again after cancelling
    assert c.post("/api/settings/deletion-request", json={"confirm": True}).status_code == 201


def test_get_dashboard_summary_function_contract(world):
    w = world
    s = svc.get_dashboard_summary(w.db, w.A.id)
    assert set(s) >= {"upcoming", "next_appointment", "needs_confirmation", "incomplete_intake", "counts"}
    assert s["counts"] == {"upcoming": 0, "incomplete_intake": 0, "awaiting_clinic": 0, "needs_confirmation": 0}
