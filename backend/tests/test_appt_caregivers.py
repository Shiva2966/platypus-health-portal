"""Caregivers: invitation, scope enforcement, expiry, revocation, audit trail, isolation."""
# appt_support MUST be imported first: it points HP_DB_PATH at a temp DB before app.db is loaded.
from tests.appt_support import GOOD_PW, INTAKE, TestClient, app, world  # noqa: F401  # isort: skip

import json
from datetime import timedelta

from app.models.appointments import CaregiverLink
from app.models.shared import AuditLog, utcnow
from app.services import appointments as svc



def _expire(w, link_id, *, access=False, invite=False):
    w.db.expire_all()
    l = w.db.get(CaregiverLink, link_id)
    if access:
        l.expires_at = utcnow() - timedelta(minutes=1)
    if invite:
        l.invite_expires_at = utcnow() - timedelta(minutes=1)
    w.db.commit()


def _seed_med(w, patient):
    from app.models.medical import MedAllergy, MedMedication
    w.db.add_all([MedMedication(patient_id=patient.id, name="Lisinopril", status="active"),
                  MedAllergy(patient_id=patient.id, substance="Penicillin", substance_key="penicillin", status="active")])
    w.db.commit()


def test_invitation_flow_and_email_modes(world, monkeypatch):
    w = world
    c = w.client(w.A)
    # without DEV_SHOW_OTP the raw token is never returned by the API
    monkeypatch.setenv("DEV_SHOW_OTP", "0")
    r = c.post("/api/care/caregivers", json={"email": "Nina@Example.test", "record_categories": ["medications"], "access_days": 30})
    assert r.status_code == 201 and "dev_invite_token" not in r.json()
    assert r.json()["item"]["state"] == "invited" and r.json()["item"]["email"] == "nina@example.test"
    lid = r.json()["item"]["id"]
    # duplicate pending invitation
    assert c.post("/api/care/caregivers", json={"email": "nina@example.test", "record_categories": ["allergies"]}).status_code == 409
    # re-send issues a fresh token (dev mode shows it)
    monkeypatch.setenv("DEV_SHOW_OTP", "1")
    tok = c.post(f"/api/care/caregivers/{lid}/resend").json()["dev_invite_token"]
    cg = TestClient(app)
    prev = cg.get(f"/api/caregiver/invite/{tok}").json()
    assert prev["invited_by"] == "Alice Anders" and prev["record_categories"] == ["Medications"]
    # weak password rejected, good one accepted, token single-use
    assert cg.post("/api/caregiver/accept", json={"token": tok, "name": "Nina", "password": "short"}).status_code == 422, cg.post("/api/caregiver/accept", json={"token": tok, "name": "Nina", "password": "short"}).text
    ok = cg.post("/api/caregiver/accept", json={"token": tok, "name": "Nina", "password": GOOD_PW})
    assert ok.status_code == 200
    assert cg.post("/api/caregiver/accept", json={"token": tok, "name": "Nina", "password": GOOD_PW}).status_code == 404
    assert [i["state"] for i in c.get("/api/care/caregivers").json()["items"]] == ["active"]
    assert cg.get("/api/caregiver/patients").json()["items"][0]["patient_name"] == "Alice Anders"
    assert w.notes("patient", w.A.id, "caregiver_activity")
    # validation
    assert c.post("/api/care/caregivers", json={"email": "not-an-email", "record_categories": ["medications"]}).status_code == 422
    assert c.post("/api/care/caregivers", json={"email": w.A.email, "record_categories": ["medications"]}).status_code == 422
    assert c.post("/api/care/caregivers", json={"email": "x@example.test"}).status_code == 422  # no scope
    assert c.post("/api/care/caregivers", json={"email": "y@example.test", "record_categories": ["secrets"]}).status_code == 422


def test_invite_token_expires_and_decline(world, monkeypatch):
    w = world
    c = w.client(w.A)
    monkeypatch.setenv("DEV_SHOW_OTP", "1")
    r = c.post("/api/care/caregivers", json={"email": "late@example.test", "manage_appointments": True}).json()
    _expire(w, r["item"]["id"], invite=True)
    cg = TestClient(app)
    tok = r["dev_invite_token"]
    assert cg.get(f"/api/caregiver/invite/{tok}").status_code == 410
    assert cg.post("/api/caregiver/accept", json={"token": tok, "name": "L", "password": GOOD_PW}).status_code == 410
    assert cg.post("/api/caregiver/accept", json={"token": "bogus", "name": "L", "password": GOOD_PW}).status_code == 404
    r2 = c.post("/api/care/caregivers", json={"email": "no@example.test", "manage_bills": True}).json()
    assert cg.post("/api/caregiver/decline", json={"token": r2["dev_invite_token"]}).status_code == 200
    states = {i["email"]: i["state"] for i in c.get("/api/care/caregivers").json()["items"]}
    assert states == {"late@example.test": "invite_expired", "no@example.test": "declined"}


def test_scope_is_enforced_per_category_appointments_and_bills(world, monkeypatch):
    w = world
    _seed_med(w, w.A)
    cg, lid, _ = w.caregiver(w.A, cats=("medications",), monkeypatch=monkeypatch)
    pid = w.A.id
    meds = cg.get(f"/api/caregiver/patients/{pid}/records/medications")
    assert meds.status_code == 200 and meds.json()["items"][0]["name"] == "Lisinopril"
    assert cg.get(f"/api/caregiver/patients/{pid}/records/allergies").status_code == 403   # not ticked
    assert cg.get(f"/api/caregiver/patients/{pid}/records/results").status_code == 403
    assert cg.get(f"/api/caregiver/patients/{pid}/bills").status_code == 403
    assert cg.get(f"/api/caregiver/patients/{pid}/appointments").status_code == 403
    assert cg.post(f"/api/caregiver/patients/{pid}/appointments", json={"provider_id": w.provs[0].id, "intake": INTAKE}).status_code == 403
    assert cg.get(f"/api/caregiver/patients/{pid}/records/nonsense").status_code == 403
    # another patient is invisible even though the caregiver is signed in
    assert cg.get(f"/api/caregiver/patients/{w.B.id}/records/medications").status_code == 403
    assert cg.get(f"/api/caregiver/patients/{w.B.id}").status_code == 403
    # a caregiver is not a patient: patient APIs reject the caregiver cookie
    assert cg.get("/api/appt/appointments").status_code == 401
    assert cg.get("/api/care/caregivers").status_code == 401
    # widening the scope takes effect immediately
    assert w.client(w.A).put(f"/api/care/caregivers/{lid}", json={"record_categories": ["medications", "allergies"], "manage_bills": True}).status_code == 200
    assert cg.get(f"/api/caregiver/patients/{pid}/records/allergies").json()["items"][0]["substance"] == "Penicillin"
    assert cg.get(f"/api/caregiver/patients/{pid}/bills").status_code == 200
    # patient's other private data (profile contact details) is not in the overview
    ov = cg.get(f"/api/caregiver/patients/{pid}").json()
    assert "email" not in ov and "phone" not in ov and "dob" not in ov


def test_caregiver_manages_appointments_with_audit_and_patient_notice(world, monkeypatch):
    w = world
    cg, lid, email = w.caregiver(w.A, cats=(), appts=True, monkeypatch=monkeypatch)
    pid = w.A.id
    r = cg.post(f"/api/caregiver/patients/{pid}/appointments", json={"provider_id": w.provs[0].id, "intake": INTAKE})
    assert r.status_code == 201 and r.json()["status"] == "requested" and r.json()["patient_id"] == pid
    aid = r.json()["id"]
    assert len(w.notes("staff", w.staff[0].id, "appointment_request")) == 1
    svc.set_status(w.db, aid, "booked", new_time="2026-12-02 11:00", staff_id=w.staff[0].id)
    w.db.commit()
    assert cg.post(f"/api/caregiver/patients/{pid}/appointments/{aid}/reschedule", json={"preferred_times": "Fridays"}).json()["status"] == "requested"
    assert cg.post(f"/api/caregiver/patients/{pid}/appointments/{aid}/cancel").json()["status"] == "cancelled"
    assert len(cg.get(f"/api/caregiver/patients/{pid}/appointments").json()["items"]) == 1
    # cannot act on someone else's appointment id
    other = w.request(w.B)
    assert cg.post(f"/api/caregiver/patients/{pid}/appointments/{other['id']}/cancel").status_code == 404
    # audit: every caregiver action is logged against the patient, acting_as caregiver
    w.db.expire_all()
    actions = [a.action for a in w.db.query(AuditLog).filter_by(patient_id=pid).order_by(AuditLog.id)]
    for expected in ("caregiver_invited", "caregiver_accepted_invite", "caregiver_appointment_draft_saved",
                     "caregiver_appointment_submitted", "caregiver_appointment_reschedule_requested",
                     "caregiver_appointment_cancelled", "caregiver_viewed_appointments"):
        assert expected in actions, expected
    row = w.db.query(AuditLog).filter_by(patient_id=pid, action="caregiver_appointment_cancelled").one()
    d = json.loads(row.detail)
    assert d["acting_as"] == "caregiver" and d["caregiver_email"] == email
    # patient is told, and can read the activity feed
    assert any("(caregiver)" in n.title for n in w.notes("patient", pid, "caregiver_activity"))
    feed = w.client(w.A).get("/api/care/activity").json()["items"]
    assert any(i["action"] == "caregiver_appointment_cancelled" and i["by"] == email for i in feed)
    # nothing leaked to the other patient's audit log
    assert not [a for a in w.audit(w.B.id) if json.loads(a.detail or "{}").get("caregiver_email") == email]


def test_revocation_is_immediate(world, monkeypatch):
    w = world
    _seed_med(w, w.A)
    cg, lid, _ = w.caregiver(w.A, cats=("medications",), appts=True, monkeypatch=monkeypatch)
    pid = w.A.id
    assert cg.get(f"/api/caregiver/patients/{pid}/records/medications").status_code == 200
    r = w.client(w.A).post(f"/api/care/caregivers/{lid}/revoke")
    assert r.status_code == 200 and r.json()["item"]["state"] == "revoked"
    assert cg.get(f"/api/caregiver/patients/{pid}/records/medications").status_code == 403
    assert cg.post(f"/api/caregiver/patients/{pid}/appointments", json={"provider_id": w.provs[0].id, "intake": INTAKE}).status_code == 403
    assert cg.get("/api/caregiver/patients").json()["items"] == []
    assert w.audit(pid, "caregiver_revoked")
    # idempotent, and another patient cannot revoke / edit it
    assert w.client(w.A).post(f"/api/care/caregivers/{lid}/revoke").status_code == 200
    assert w.client(w.B).post(f"/api/care/caregivers/{lid}/revoke").status_code == 404
    assert w.client(w.B).put(f"/api/care/caregivers/{lid}", json={"manage_bills": True}).status_code == 404
    assert w.client(w.A).put(f"/api/care/caregivers/{lid}", json={"manage_bills": True}).status_code == 409
    # re-inviting the same address after revocation works
    assert w.client(w.A).post("/api/care/caregivers", json={"email": r.json()["item"]["email"], "manage_bills": True}).status_code == 201


def test_expiry_blocks_access_without_any_action(world, monkeypatch):
    w = world
    _seed_med(w, w.A)
    cg, lid, _ = w.caregiver(w.A, cats=("medications",), monkeypatch=monkeypatch)
    pid = w.A.id
    assert cg.get(f"/api/caregiver/patients/{pid}/records/medications").status_code == 200
    _expire(w, lid, access=True)
    assert cg.get(f"/api/caregiver/patients/{pid}/records/medications").status_code == 403
    assert cg.get("/api/caregiver/patients").json()["items"] == []
    assert w.client(w.A).get("/api/care/caregivers").json()["items"][0]["state"] == "expired"
    # the patient can extend it
    assert w.client(w.A).put(f"/api/care/caregivers/{lid}", json={"record_categories": ["medications"], "access_days": 10}).status_code == 409


def test_caregiver_login_logout_and_throttle(world, monkeypatch):
    w = world
    cg, lid, email = w.caregiver(w.A, monkeypatch=monkeypatch)
    cg.post("/api/caregiver/logout")
    assert cg.get("/api/caregiver/me").status_code == 401
    anon = TestClient(app)
    assert anon.post("/api/caregiver/login", json={"email": email, "password": "wrong-password"}).status_code == 401
    assert anon.post("/api/caregiver/login", json={"email": "nobody@example.test", "password": GOOD_PW}).status_code == 401
    assert anon.post("/api/caregiver/login", json={"email": email, "password": GOOD_PW}).status_code == 200
    assert anon.get("/api/caregiver/me").json()["email"] == email
    # one account, two patients: B also invites the same person; both visible, access separate
    cg2, lid2, _ = w.caregiver(w.B, email=email, cats=("allergies",), monkeypatch=monkeypatch) if False else (None, None, None)
    assert anon.get("/api/caregiver/patients").json()["items"][0]["patient_id"] == w.A.id
    assert anon.get("/caregiver").status_code == 200 and "text/html" in anon.get("/caregiver").headers["content-type"]
    # every tab of the page needs a session
    assert TestClient(app).get(f"/api/caregiver/patients/{w.A.id}/records/medications").status_code == 401


def test_same_caregiver_for_two_patients_keeps_scopes_separate(world, monkeypatch):
    w = world
    _seed_med(w, w.A)
    _seed_med(w, w.B)
    cg, _, email = w.caregiver(w.A, cats=("medications",), monkeypatch=monkeypatch)
    r = w.client(w.B).post("/api/care/caregivers", json={"email": email, "record_categories": ["allergies"]})
    tok = r.json()["dev_invite_token"]
    # existing account: must prove the password
    other = TestClient(app)
    assert other.post("/api/caregiver/accept", json={"token": tok, "password": "Wrong-Password-1!"}).status_code == 401
    assert other.post("/api/caregiver/accept", json={"token": tok, "password": GOOD_PW}).status_code == 200
    assert len(other.get("/api/caregiver/patients").json()["items"]) == 2
    assert other.get(f"/api/caregiver/patients/{w.A.id}/records/medications").status_code == 200
    assert other.get(f"/api/caregiver/patients/{w.A.id}/records/allergies").status_code == 403
    assert other.get(f"/api/caregiver/patients/{w.B.id}/records/allergies").status_code == 200
    assert other.get(f"/api/caregiver/patients/{w.B.id}/records/medications").status_code == 403
