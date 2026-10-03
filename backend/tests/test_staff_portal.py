"""Staff portal: auth, roles, search/DOB, request-access flow, documents, appointments, audit visibility."""
import json
import uuid

import pytest
import w3_support as w

@pytest.fixture(autouse=True)
def _no_otp(monkeypatch):
    w.legacy_auth_env(monkeypatch)


NURSE, FRONT, DOCTOR, ADMIN, LAKESIDE = ("nurse@riverside.demo", "frontdesk@riverside.demo", "doctor@riverside.demo",
                                         "admin@riverside.demo", "doctor@lakeside.demo")


# ---------------------------------------------------------------- auth + roles

def test_staff_login_logout_and_session_separation():
    w.seeded()
    c = w.new_client()
    assert c.get("/api/staff/me").status_code == 401
    bad = c.post("/api/staff/login", json={"email": NURSE, "password": "wrong-password"})
    assert bad.status_code == 401
    ok = c.post("/api/staff/login", json={"email": NURSE, "password": w.DEMO_PASSWORD})
    assert ok.status_code == 200
    body = ok.json()
    assert body["role"] == "nurse" and "password_hash" not in body and body["provider_name"]
    assert "hp_staff" in c.cookies and "hp_patient" not in c.cookies
    assert "httponly" in ok.headers["set-cookie"].lower()
    assert c.get("/api/staff/me").json()["email"] == NURSE
    # a staff cookie is NOT a patient session
    assert c.get("/api/auth/me").status_code == 401
    assert c.post("/api/staff/logout").status_code == 200
    assert c.get("/api/staff/me").status_code == 401


def test_patient_session_is_not_a_staff_session():
    pc, *_ = w.make_patient()
    assert pc.get("/api/staff/me").status_code == 401
    assert pc.get("/api/staff/patients/search?q=Test").status_code == 401
    assert pc.get("/api/staff/admin/audit").status_code == 401


def test_inactive_staff_cannot_login_or_use_session():
    from app.models.staff import StaffUser
    from sqlalchemy import select
    w.seeded()
    admin = w.staff_login(ADMIN)
    uid = admin.post("/api/staff/admin/users", json={"name": "Temp Nurse", "email": f"{uuid.uuid4().hex[:8]}@riverside.demo",
                                                     "password": "Temporary-Pass-1", "role": "nurse"}).json()
    email = uid["email"]
    c = w.new_client()
    assert c.post("/api/staff/login", json={"email": email, "password": "Temporary-Pass-1"}).status_code == 200
    assert admin.patch(f"/api/staff/admin/users/{uid['id']}", json={"active": False}).status_code == 200
    assert c.get("/api/staff/me").status_code == 401  # session killed
    assert c.post("/api/staff/login", json={"email": email, "password": "Temporary-Pass-1"}).status_code == 401


def test_role_enforcement():
    admin, front, nurse = w.staff_login(ADMIN), w.staff_login(FRONT), w.staff_login(NURSE)
    # admin: audit + staff management only, no patient lookups
    assert admin.get("/api/staff/admin/audit").status_code == 200
    assert admin.get("/api/staff/admin/users").status_code == 200
    assert admin.get("/api/staff/patients/search?q=Jo").status_code == 403
    assert admin.get("/api/staff/appointments").status_code == 403
    # everyone else: no audit viewer, no staff management
    for c in (front, nurse):
        assert c.get("/api/staff/admin/audit").status_code == 403
        assert c.get("/api/staff/admin/users").status_code == 403
        assert c.post("/api/staff/admin/users", json={"name": "x", "email": "x@y.z", "password": "Password-123456",
                                                     "role": "admin"}).status_code == 403
    # admin cannot lock themselves out
    me = admin.get("/api/staff/me").json()
    assert admin.patch(f"/api/staff/admin/users/{me['id']}", json={"active": False}).status_code == 409
    assert admin.patch(f"/api/staff/admin/users/{me['id']}", json={"role": "nurse"}).status_code == 409


# ---------------------------------------------------------------- search + DOB + request access

def test_search_returns_minimal_identity_and_no_clinical_data_without_grant():
    pc, pid, name, dob = w.make_patient()
    w.upload(pc, "Secret lab result")
    nurse = w.staff_login(NURSE)
    r = nurse.get("/api/staff/patients/search", params={"q": name[:8]})
    assert r.status_code == 200
    hit = next(x for x in r.json()["results"] if x["id"] == pid)
    assert set(hit) == {"id", "name", "dob_masked"}
    assert hit["dob_masked"] == "1980-**-**" and dob not in json.dumps(hit)
    assert "Secret lab result" not in r.text
    # patient page is closed until the DOB is confirmed
    assert nurse.get(f"/api/staff/patients/{pid}").status_code == 403
    assert nurse.post(f"/api/staff/patients/{pid}/confirm", json={"dob": "1999-01-01"}).status_code == 403
    assert nurse.get(f"/api/staff/patients/{pid}").status_code == 403
    ok = nurse.post(f"/api/staff/patients/{pid}/confirm", json={"dob": dob})
    assert ok.status_code == 200 and ok.json()["identity"]["dob"] == dob
    page = nurse.get(f"/api/staff/patients/{pid}").json()
    assert page["documents"] == [] and page["empty_message"] == "No shared records"
    assert page["scope"]["authorized"] is False and page["can_request_access"] is True
    assert "Secret lab result" not in json.dumps(page)


def test_search_dob_filter_and_short_query():
    pc, pid, name, dob = w.make_patient()
    nurse = w.staff_login(NURSE)
    assert nurse.get("/api/staff/patients/search", params={"q": name, "dob": "1970-01-01"}).json()["results"] == []
    assert any(x["id"] == pid for x in nurse.get("/api/staff/patients/search", params={"q": name, "dob": dob}).json()["results"])
    assert nurse.get("/api/staff/patients/search", params={"q": "a"}).status_code == 422


def test_search_matches_words_in_any_order_and_middle_names():
    tag = uuid.uuid4().hex[:6].title()
    pc, pid, name, dob = w.make_patient(name=f"Jordan{tag} Alexander Ellis{tag}")
    nurse = w.staff_login(NURSE)
    for q in (f"Jordan{tag} Ellis{tag}", f"ellis{tag} jordan{tag}", f"ELLIS{tag}, Jordan{tag}",
              f"jordan{tag} alexander"):
        res = nurse.get("/api/staff/patients/search", params={"q": q}).json()["results"]
        assert [x["id"] for x in res] == [pid], q
        assert set(res[0]) == {"id", "name", "dob_masked"}
    assert nurse.get("/api/staff/patients/search", params={"q": f"Jordan{tag} Smith{tag}"}).json()["results"] == []
    assert nurse.get("/api/staff/patients/search", params={"q": "%_%"}).json()["results"] == []


def test_dob_guessing_is_throttled():
    pc, pid, name, dob = w.make_patient()
    nurse = w.staff_login(NURSE)
    codes = [nurse.post(f"/api/staff/patients/{pid}/confirm", json={"dob": f"1990-01-{d:02d}"}).status_code for d in range(1, 9)]
    assert 429 in codes
    assert nurse.post(f"/api/staff/patients/{pid}/confirm", json={"dob": dob}).status_code == 429  # even the right one


def test_request_access_approval_open_document_revoke_flow():
    pc, pid, name, dob = w.make_patient()
    lab = w.upload(pc, "Blood panel")
    ins = w.upload(pc, "Insurance card", category="insurance")
    nurse, front, lake = w.staff_login(NURSE), w.staff_login(FRONT), w.staff_login(LAKESIDE)

    # nurse requests -> patient is notified
    assert nurse.post(f"/api/staff/patients/{pid}/confirm", json={"dob": dob}).status_code == 200
    bad = nurse.post(f"/api/staff/patients/{pid}/request-access", json={"categories": ["lab_result"], "purpose": ""})
    assert bad.status_code == 422  # purpose required
    req = nurse.post(f"/api/staff/patients/{pid}/request-access",
                     json={"categories": ["lab_result", "insurance"], "purpose": "Pre-op review", "duration_days": 5})
    assert req.status_code == 200 and req.json()["status"] == "pending"
    notes = pc.get("/api/notifications", params={"kind": "access_request"}).json()["items"]
    assert notes and "requesting access" in notes[0]["title"]
    # request tracker is visible to staff: pending
    tr = nurse.get("/api/staff/requests", params={"patient_id": pid}).json()["requests"]
    assert tr[0]["status"] == "pending" and tr[0]["patient_name"] == name

    # patient approves -> staff notified, tracker shows approved
    assert pc.post(f"/api/sharing/requests/{req.json()['id']}/approve", json={}).status_code == 200
    kinds = [n["kind"] for n in nurse.get("/api/notifications", params={"as": "staff"}).json()["items"]]
    assert "access_approved" in kinds
    assert nurse.get("/api/staff/requests", params={"patient_id": pid, "status": "approved"}).json()["requests"]

    page = nurse.get(f"/api/staff/patients/{pid}").json()
    names = {d["name"]: d for d in page["documents"]}
    assert set(names) == {"Blood panel", "Insurance card"}
    assert names["Blood panel"]["source_label"] == "Patient-entered"
    assert page["scope"]["authorized"] and page["scope"]["authorized_until"]
    assert page["empty_message"] is None

    # opening a document: right headers, audited, patient notified
    f = nurse.get(f"/api/staff/patients/{pid}/documents/{lab}/file")
    assert f.status_code == 200 and f.content.startswith(b"%PDF")
    assert f.headers["x-content-type-options"] == "nosniff" and "inline" in f.headers["content-disposition"]
    viewed = pc.get("/api/notifications", params={"kind": "document_viewed"}).json()["items"]
    assert viewed and "Blood panel" in viewed[0]["title"]

    # front desk (same org, DOB confirmed) sees only administrative categories
    assert front.post(f"/api/staff/patients/{pid}/confirm", json={"dob": dob}).status_code == 200
    fp = front.get(f"/api/staff/patients/{pid}").json()
    assert [d["name"] for d in fp["documents"]] == ["Insurance card"]
    assert front.get(f"/api/staff/patients/{pid}/documents/{lab}/file").status_code == 403
    assert front.get(f"/api/staff/patients/{pid}/documents/{ins}/file").status_code == 200
    assert front.post(f"/api/staff/patients/{pid}/request-access",
                      json={"categories": ["lab_result"], "purpose": "x"}).status_code == 403

    # a different organization has no access, even for the same patient
    assert lake.post(f"/api/staff/patients/{pid}/confirm", json={"dob": dob}).status_code == 200
    lp = lake.get(f"/api/staff/patients/{pid}").json()
    assert lp["documents"] == [] and lp["empty_message"] == "No shared records"
    assert lake.get(f"/api/staff/patients/{pid}/documents/{lab}/file").status_code == 403

    # global document search only sees authorized documents
    res = nurse.get("/api/staff/documents/search", params={"q": "blood"}).json()["results"]
    assert any(r["id"] == lab and r["patient_id"] == pid for r in res)
    assert lake.get("/api/staff/documents/search", params={"q": "blood"}).json()["results"] == []

    # revoke -> access gone + staff notified
    grants = pc.get("/api/sharing/grants").json()["grants"]
    for g in grants:
        assert pc.delete(f"/api/sharing/grants/{g['id']}").status_code == 200
    assert nurse.get(f"/api/staff/patients/{pid}/documents/{lab}/file").status_code == 403
    assert nurse.get(f"/api/staff/patients/{pid}").json()["documents"] == []
    kinds = [n["kind"] for n in nurse.get("/api/notifications", params={"as": "staff"}).json()["items"]]
    assert "access_revoked" in kinds


def test_denied_request_notifies_staff_and_tracker():
    pc, pid, name, dob = w.make_patient()
    doctor = w.staff_login(DOCTOR)
    doctor.post(f"/api/staff/patients/{pid}/confirm", json={"dob": dob})
    rid = doctor.post(f"/api/staff/patients/{pid}/request-access",
                      json={"categories": ["imaging"], "purpose": "Consult"}).json()["id"]
    assert pc.post(f"/api/sharing/requests/{rid}/deny").status_code == 200
    kinds = [n["kind"] for n in doctor.get("/api/notifications", params={"as": "staff"}).json()["items"]]
    assert "access_denied" in kinds
    st = doctor.get("/api/staff/requests", params={"patient_id": pid}).json()["requests"][0]["status"]
    assert st == "denied"


def test_redeem_share_token():
    pc, pid, name, dob = w.make_patient()
    doc = w.upload(pc, "Discharge summary", category="visit_summary")
    tok = pc.post("/api/sharing/tokens", json={"document_ids": [doc], "purpose": "ER visit"}).json()["token"]
    nurse = w.staff_login(NURSE)
    assert nurse.post("/api/staff/redeem", json={"token": "x" * 24}).status_code == 400
    r = nurse.post("/api/staff/redeem", json={"token": tok})
    assert r.status_code == 200 and r.json()["patient_id"] == pid
    # redeeming in person counts as identity confirmation
    assert [d["name"] for d in nurse.get(f"/api/staff/patients/{pid}").json()["documents"]] == ["Discharge summary"]
    assert nurse.post("/api/staff/redeem", json={"token": tok}).status_code == 400  # single use


# ---------------------------------------------------------------- appointments / intake

def test_appointment_inbox_decisions_and_fhir_export():
    pc, pid, name, dob = w.make_patient()
    aid = w.submit_appointment(pid, w.riverside_id())
    front, lake = w.staff_login(FRONT), w.staff_login(LAKESIDE)

    inbox = front.get("/api/staff/appointments", params={"status": "requested"}).json()["appointments"]
    assert any(a["id"] == aid for a in inbox)
    assert "intake_items" not in inbox[0]  # list is light; the clinical-ish intake text is on the detail view
    assert aid not in [a["id"] for a in lake.get("/api/staff/appointments").json()["appointments"]]
    assert lake.get(f"/api/staff/appointments/{aid}").status_code == 404
    assert lake.get(f"/api/staff/appointments/{aid}/export.json").status_code == 404

    d = front.get(f"/api/staff/appointments/{aid}").json()
    labels = {i["label"]: i["value"] for i in d["intake_items"]}
    assert labels["Reason"] == "Persistent cough" and labels["Visit type"] == "in_person"

    ex = front.get(f"/api/staff/appointments/{aid}/export.json")
    assert ex.status_code == 200 and "attachment" in ex.headers["content-disposition"]
    b = ex.json()
    assert b["resourceType"] == "Bundle"
    res = {e["resource"]["resourceType"]: e["resource"] for e in b["entry"]}
    assert set(res) == {"Patient", "Appointment", "QuestionnaireResponse"}
    assert res["Patient"]["id"] == pid and res["Patient"]["name"][0]["text"] == name
    assert res["Patient"]["birthDate"] == dob
    assert res["Appointment"]["status"] == "proposed"
    qr = res["QuestionnaireResponse"]
    assert qr["status"] == "completed" and qr["subject"]["reference"] == f"Patient/{pid}"
    item = next(i for i in qr["item"] if i["linkId"] == "reason")
    assert item["answer"][0]["valueString"] == "Persistent cough"

    # accept requires a time; then patient is notified
    assert front.post(f"/api/staff/appointments/{aid}/decision", json={"action": "accept"}).status_code == 422
    r = front.post(f"/api/staff/appointments/{aid}/decision",
                   json={"action": "accept", "scheduled_for": "2030-01-15T09:30", "note": "Bring your ID"})
    assert r.status_code == 200 and r.json()["status"] == "booked"
    n = pc.get("/api/notifications", params={"kind": "appointment_update"}).json()["items"]
    assert any("confirmed" in x["title"] for x in n)
    assert front.get(f"/api/staff/appointments/{aid}/export.json").json()["entry"][1]["resource"]["status"] == "booked"
    r = front.post(f"/api/staff/appointments/{aid}/decision", json={"action": "reschedule", "scheduled_for": "2030-01-16T10:00"})
    assert r.json()["status"] == "rescheduled"
    r = front.post(f"/api/staff/appointments/{aid}/decision", json={"action": "decline", "note": "Fully booked"})
    assert r.json()["status"] == "declined"
    assert front.post(f"/api/staff/appointments/{aid}/decision", json={"action": "decline"}).status_code == 409


def test_staff_notified_of_new_appointment_request():
    pc, pid, name, dob = w.make_patient()
    sid = w.staff_id(NURSE)
    nurse = w.staff_login(NURSE)
    w.submit_appointment(pid, w.riverside_id())
    items = nurse.get("/api/notifications", params={"as": "staff", "kind": "appointment_request"}).json()["items"]
    assert any(name in i["title"] or "appointment" in i["title"].lower() for i in items)
    assert sid


# ---------------------------------------------------------------- audit visibility

def test_audit_visibility_rules():
    pc, pid, name, dob = w.make_patient()
    other, opid, oname, odob = w.make_patient()
    doc = w.upload(pc, "Visible only to me")
    nurse, admin = w.staff_login(NURSE), w.staff_login(ADMIN)
    w.grant_via_request(nurse, pc, pid, dob, ["lab_result"])
    assert nurse.get(f"/api/staff/patients/{pid}").status_code == 200
    assert nurse.get(f"/api/staff/patients/{pid}/documents/{doc}/file").status_code == 200

    mine = pc.get("/api/audit/mine").json()
    actions = {i["action"] for i in mine["items"]}
    assert {"document_viewed", "access_requested", "access_approved", "patient_record_opened"} <= actions
    assert not actions & {"dob_confirmed", "dob_check_failed"}
    viewed = next(i for i in mine["items"] if i["action"] == "document_viewed")
    assert "Nora Nurse" in viewed["actor"] and "Riverside" in viewed["actor"] and viewed["document"] == "Visible only to me"
    assert "password" not in json.dumps(mine).lower()
    only_staff = pc.get("/api/audit/mine", params={"who": "staff"}).json()["items"]
    assert only_staff and all(i["actor_type"] != "patient" for i in only_staff)

    # another patient sees none of it
    assert "Visible only to me" not in json.dumps(other.get("/api/audit/mine").json())
    # staff cannot use the patient endpoint
    assert nurse.get("/api/audit/mine").status_code == 401

    # admin viewer: filters work and expose staff + patient names
    r = admin.get("/api/staff/admin/audit", params={"patient_id": pid, "action": "document_viewed"}).json()
    assert r["total"] >= 1 and all(i["action"] == "document_viewed" for i in r["items"])
    assert r["items"][0]["patient_name"] == name and "Nora Nurse" in r["items"][0]["actor_name"]
    assert admin.get("/api/staff/admin/audit", params={"actor_type": "staff", "q": "lab_result"}).json()["total"] >= 1
    assert admin.get("/api/staff/admin/audit", params={"since": "2999-01-01"}).json()["total"] == 0
    assert admin.get("/api/staff/admin/audit", params={"since": "not-a-date"}).status_code == 422
    assert "document_viewed" in admin.get("/api/staff/admin/audit/actions").json()["actions"]
    # the patient's own history excludes other patients' events
    assert all(i["actor"] != "Nora Nurse (Nurse), Riverside General Hospital"
               for i in other.get("/api/audit/mine").json()["items"])


def test_dashboard_and_partial_data_state():
    pc, pid, name, dob = w.make_patient()
    nurse = w.staff_login(NURSE)
    w.grant_via_request(nurse, pc, pid, dob, ["imaging"])
    dsh = nurse.get("/api/staff/dashboard").json()
    assert dsh["access_requests"]["approved"] >= 1 and dsh["shares_granted"]["patients"] >= 1
    assert dsh["unread_notifications"] >= 1 and dsh["errors"] == {}
    assert isinstance(dsh["new_appointment_requests"], int)
    assert w.staff_login(ADMIN).get("/api/staff/dashboard").status_code == 403


def test_patient_page_degrades_when_document_service_down(monkeypatch):
    from app.services import staff_portal
    pc, pid, name, dob = w.make_patient()
    nurse = w.staff_login(NURSE)
    nurse.post(f"/api/staff/patients/{pid}/confirm", json={"dob": dob})

    def boom():
        from fastapi import HTTPException
        raise HTTPException(503, staff_portal.UNAVAILABLE)
    monkeypatch.setattr(staff_portal, "consent_service", boom)
    r = nurse.get(f"/api/staff/patients/{pid}")
    assert r.status_code == 200
    j = r.json()
    assert j["identity"]["legal_name"] and j["documents"] == [] and "documents" in j["errors"]
    assert nurse.post(f"/api/staff/patients/{pid}/request-access",
                      json={"categories": ["imaging"], "purpose": "x"}).status_code == 503


def test_staff_spa_and_patient_modules_are_served():
    import re
    c = w.new_client()
    r = c.get("/staff/")
    assert r.status_code == 200 and "Staff Portal" in r.text and "DEMO" in r.text
    srcs = re.findall(r'(?:src|href)="(/staff/[^"]+)"', r.text)
    assert len(srcs) >= 5
    for s in srcs:
        assert c.get(s).status_code == 200, s
    scripts = c.get("/api/ui-modules").json()["scripts"]
    assert "/static/js/notif_inbox.js" in scripts and "/static/js/notif_audit.js" in scripts
    for s in scripts:
        if "notif_" in s:
            assert c.get(s).status_code == 200


def test_staff_seed_is_idempotent():
    a = w.seeded()
    b = w.seeded()
    assert a["providers"] == b["providers"] and b["staff"] == []
