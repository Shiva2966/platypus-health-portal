"""Staff view of shared health records: DOB gate, role matrix, consent, audit/notify, revoke,
clinician confirmation, corrections inbox, bill-reminder delegation."""
import pytest

import w3_support as w

NURSE, DOCTOR, FRONT, ADMIN = ("nurse@riverside.demo", "doctor@riverside.demo", "frontdesk@riverside.demo",
                               "admin@riverside.demo")
OTHER_ORG = "doctor@lakeside.demo"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    w.legacy_auth_env(monkeypatch)


def _patient(seed=True):
    pc, pid, name, dob = w.make_patient()
    if seed:
        from app.services.seed_medical import seed_medical
        s = w.db()
        try:
            seed_medical(s, pid)
        finally:
            s.close()
    return pc, pid, dob


def _share(pc, categories, provider=None):
    r = pc.post("/api/sharing/grants", json={"provider_id": provider or w.riverside_id(), "categories": categories,
                                              "expires_in_days": 7})
    assert r.status_code == 201, r.text
    return [g["id"] for g in r.json()["grants"]]


def _staff(email, pid, dob):
    sc = w.staff_login(email)
    assert sc.post(f"/api/staff/patients/{pid}/confirm", json={"dob": dob}).status_code == 200
    return sc


def _tabs(sc, pid):
    r = sc.get(f"/api/staff/patients/{pid}/records")
    assert r.status_code == 200, r.text
    return {t["category"]: t for t in r.json()["tabs"]}


def test_dob_confirmation_required_and_admin_blocked():
    pc, pid, dob = _patient(seed=False)
    _share(pc, ["allergies"])
    sc = w.staff_login(NURSE)
    assert sc.get(f"/api/staff/patients/{pid}/records").status_code == 403
    assert sc.get(f"/api/staff/patients/{pid}/records/allergies").status_code == 403
    admin = w.staff_login(ADMIN)
    assert admin.get(f"/api/staff/patients/{pid}/records").status_code == 403
    assert admin.get("/api/staff/corrections").status_code == 403
    assert w.new_client().get(f"/api/staff/patients/{pid}/records").status_code == 401


def test_nurse_sees_only_shared_categories_with_audit_and_notification():
    from app.models.shared import AuditLog
    from sqlalchemy import select

    pc, pid, dob = _patient()
    _share(pc, ["allergies"])
    sc = _staff(NURSE, pid, dob)
    tabs = _tabs(sc, pid)
    assert list(tabs) == ["allergies", "medications", "history", "vaccinations", "results", "billing"]
    assert tabs["allergies"]["shared"] and tabs["allergies"]["grant_expires_at"]
    assert not tabs["medications"]["shared"] and tabs["medications"]["role_allowed"]
    assert "Not shared" in tabs["medications"]["message"]
    # the overview itself is not a data read: no record_viewed yet
    assert pc.get("/api/notifications", params={"kind": "record_viewed"}).json()["items"] == []

    r = sc.get(f"/api/staff/patients/{pid}/records/allergies")
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    assert items[0]["record_type"] == "allergy_status"
    assert all("source_label" in i and "updated_at" in i for i in items[1:]) and len(items) > 1
    assert r.json()["can_confirm"] is True

    denied = sc.get(f"/api/staff/patients/{pid}/records/medications")
    assert denied.status_code == 403 and "Not shared" in denied.json()["detail"]
    assert sc.get(f"/api/staff/patients/{pid}/records/bogus").status_code == 404

    notes = pc.get("/api/notifications", params={"kind": "record_viewed"}).json()["items"]
    assert len(notes) == 1 and "allergies" in notes[0]["title"].lower()
    s = w.db()
    try:
        actions = s.scalars(select(AuditLog.action).where(AuditLog.patient_id == pid)).all()
    finally:
        s.close()
    assert "record_viewed" in actions and "record_access_denied" in actions
    hist = pc.get("/api/audit/mine", params={"who": "staff"}).json()
    labels = {i["action"]: i["action_label"] for i in (hist.get("items") or hist.get("events") or [])}
    assert labels.get("record_viewed") == "viewed your health data"


def test_results_keep_reference_range_and_neutral_flag():
    pc, pid, dob = _patient()
    _share(pc, ["results"])
    sc = _staff(DOCTOR, pid, dob)
    items = sc.get(f"/api/staff/patients/{pid}/records/results").json()["items"]
    flagged = [i for i in items if i["outside_range"]]
    assert flagged and all(i["flag_text"] == "Outside the lab's reference range" for i in flagged)
    assert any(i["ref_display"] for i in items)


def test_front_desk_billing_only_and_role_message():
    pc, pid, dob = _patient()
    _share(pc, ["allergies", "billing"])
    fd = _staff(FRONT, pid, dob)
    tabs = _tabs(fd, pid)
    assert tabs["allergies"]["role_allowed"] is False and tabs["allergies"]["shared"] is False
    assert tabs["billing"]["shared"] is True
    r = fd.get(f"/api/staff/patients/{pid}/records/allergies")
    assert r.status_code == 403 and "role" in r.json()["detail"].lower()
    b = fd.get(f"/api/staff/patients/{pid}/records/billing")
    assert b.status_code == 200 and b.json()["items"][0]["record_type"] == "billing"
    assert b.json()["can_confirm"] is False
    assert fd.get(f"/api/staff/patients/{pid}/records/billing").status_code == 200
    assert fd.get("/api/staff/corrections").status_code == 403


def test_allergy_unknown_vs_no_known():
    pc, pid, dob = _patient(seed=False)
    _share(pc, ["allergies"])
    sc = _staff(NURSE, pid, dob)
    st = sc.get(f"/api/staff/patients/{pid}/records/allergies").json()["items"][0]
    assert st["status"] == "unknown" and st["explicit_none"] is False
    assert pc.put("/api/med/allergy-status", json={"status": "no_known_allergies"}).status_code == 200
    st = sc.get(f"/api/staff/patients/{pid}/records/allergies").json()["items"][0]
    assert st["status"] == "no_known_allergies" and st["explicit_none"] is True


def test_revoke_removes_access_and_other_org_never_sees():
    pc, pid, dob = _patient()
    gids = _share(pc, ["allergies"])
    sc = _staff(NURSE, pid, dob)
    assert sc.get(f"/api/staff/patients/{pid}/records/allergies").status_code == 200
    for g in gids:
        assert pc.delete(f"/api/sharing/grants/{g}").status_code == 200
    assert _tabs(sc, pid)["allergies"]["shared"] is False
    assert sc.get(f"/api/staff/patients/{pid}/records/allergies").status_code == 403
    lake = _staff(OTHER_ORG, pid, dob)
    _share(pc, ["allergies"])  # shared with Riverside again, never with Lakeside
    assert lake.get(f"/api/staff/patients/{pid}/records/allergies").status_code == 403


def test_confirm_record_rules():
    pc, pid, dob = _patient()
    _share(pc, ["allergies", "billing"])
    sc = _staff(NURSE, pid, dob)
    items = sc.get(f"/api/staff/patients/{pid}/records/allergies").json()["items"]
    target = next(i for i in items[1:] if i["source"] == "patient_entered")
    r = sc.post(f"/api/staff/patients/{pid}/records/allergies/{target['id']}/confirm")
    assert r.status_code == 200, r.text
    assert r.json()["source"] == "clinician_confirmed" and "Nurse" in r.json()["confirmed_by"]
    # patient can no longer edit it directly (W7 rule)
    assert pc.delete(f"/api/med/allergies/{target['id']}").status_code == 409
    # not shared category / billing / wrong role / wrong patient
    meds = pc.get("/api/med/medications").json()
    mid = (meds.get("items") if isinstance(meds, dict) else meds)[0]["id"]
    assert sc.post(f"/api/staff/patients/{pid}/records/medications/{mid}/confirm").status_code == 403
    assert sc.post(f"/api/staff/patients/{pid}/records/billing/x/confirm").status_code == 400
    fd = _staff(FRONT, pid, dob)
    assert fd.post(f"/api/staff/patients/{pid}/records/allergies/{target['id']}/confirm").status_code == 403
    assert sc.post(f"/api/staff/patients/{pid}/records/allergies/not-a-real-id/confirm").status_code == 404


def test_corrections_inbox_and_resolution():
    pc, pid, dob = _patient()
    _share(pc, ["allergies"])
    allergies = pc.get("/api/med/allergies").json()
    aid = (allergies.get("items") if isinstance(allergies, dict) else allergies)[0]["id"]
    r = pc.post("/api/med/corrections", json={"record_type": "allergy", "record_id": aid,
                                              "message": "Reaction was hives, not rash."})
    assert r.status_code in (200, 201), r.text
    cid = r.json()["id"]
    meds = pc.get("/api/med/medications").json()
    mid = (meds.get("items") if isinstance(meds, dict) else meds)[0]["id"]
    pc.post("/api/med/corrections", json={"record_type": "medication", "record_id": mid, "message": "Dose changed."})

    doc = w.staff_login(DOCTOR)
    inbox = doc.get("/api/staff/corrections").json()["items"]
    mine = [i for i in inbox if i["patient_id"] == pid]
    assert [i["category"] for i in mine] == ["allergies"]  # medications not shared -> hidden
    assert "message" not in mine[0] and "record_label" not in mine[0]
    assert all(i["patient_id"] != pid for i in w.staff_login(OTHER_ORG).get("/api/staff/corrections").json()["items"])

    # patient page needs DOB confirmation
    assert doc.get(f"/api/staff/patients/{pid}/corrections").status_code == 403
    assert doc.post(f"/api/staff/corrections/{cid}/resolve", json={"status": "resolved"}).status_code == 403
    doc.post(f"/api/staff/patients/{pid}/confirm", json={"dob": dob})
    full = doc.get(f"/api/staff/patients/{pid}/corrections").json()["items"]
    assert [c["id"] for c in full] == [cid] and full[0]["message"].startswith("Reaction")
    assert _tabs(doc, pid)  # overview still works
    assert doc.get(f"/api/staff/patients/{pid}/records").json()["open_corrections"] == 1

    assert doc.post(f"/api/staff/corrections/{cid}/resolve", json={"status": "declined"}).status_code == 422
    ok = doc.post(f"/api/staff/corrections/{cid}/resolve", json={"status": "resolved", "note": "Updated."})
    assert ok.status_code == 200 and ok.json()["status"] == "resolved"
    assert doc.post(f"/api/staff/corrections/{cid}/resolve", json={"status": "resolved"}).status_code == 409
    notes = pc.get("/api/notifications", params={"kind": "correction_update"}).json()["items"]
    assert len(notes) == 1 and "resolved" in notes[0]["title"]


def test_notification_labels_include_record_viewed():
    pc, *_ = w.make_patient()
    j = pc.get("/api/notifications").json()
    assert "record_viewed" in j["kinds"] and j["kind_labels"]["record_viewed"] == "My health data was viewed"


def test_reminder_sweep_delegates_bill_reminders_to_billing(monkeypatch):
    from app.services import billing_reminders, reminders

    calls = []
    monkeypatch.setattr(billing_reminders, "run_bill_reminder_sweep",
                        lambda db, **kw: calls.append(kw) or 7)
    s = w.db()
    try:
        out = reminders.run_reminder_sweep(s)
    finally:
        s.close()
    assert out["bill_reminders"] == 7 and len(calls) == 1
