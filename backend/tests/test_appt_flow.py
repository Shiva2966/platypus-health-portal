"""Appointments: API flow, status transitions, isolation, notification side effects, dashboard summary."""
# appt_support MUST be imported first: it points HP_DB_PATH at a temp DB before app.db is loaded.
from tests.appt_support import INTAKE, world  # noqa: F401  # isort: skip

import pytest

from app.errors import FieldError
from app.models.appointments import Appointment, AppointmentEvent
from app.services import appointments as svc



def _appt(w, aid):
    w.db.expire_all()
    return w.db.get(Appointment, aid)


# ---------------------------------------------------------------- submit + notifications

def test_submit_creates_request_and_notifies_staff_and_patient(world):
    w = world
    a = w.request(w.A, symptoms="Dull ache", onset="two weeks ago")
    assert a["status"] == "requested" and a["provider"]["id"] == w.provs[0].id
    assert a["intake"]["symptoms"] == "Dull ache"
    assert "not a diagnosis" in a["disclaimer"].lower() and "emergency" in a["disclaimer"].lower()
    staff = w.staff[0]
    assert len(w.notes("staff", staff.id, "appointment_request")) == 1
    assert len(w.notes("staff", staff.id, "intake_received")) == 1
    assert w.notes("staff", w.staff[1].id) == []  # other organization hears nothing
    assert len(w.notes("patient", w.A.id, "appointment_update")) == 1
    assert w.audit(w.A.id, "appointment_submitted")
    # contact snapshot = profile (agreed to send); no medical summary unless opted in
    assert a["contact"]["legal_name"] == "Alice Anders" and "medical_summary" not in a["contact"]
    assert [h["to"] for h in svc.to_dict(w.db, _appt(w, a["id"]), history=True)["history"]] == ["draft", "requested"]


def test_no_triage_or_diagnosis_fields_in_response(world):
    a = world.request(world.A, symptoms="chest tightness and I feel faint", onset="today")
    a = {k: v for k, v in a.items() if k != "disclaimer"}  # the label itself says "does not triage"
    flat = str(a).lower()
    for word in ("triage", "urgency", "severity", "diagnos"):
        assert word not in flat.replace("not a diagnosis", "")


def test_draft_review_and_incomplete_tracking(world):
    w = world
    c = w.client(w.A)
    r = c.post("/api/appt/appointments", json={"provider_id": w.provs[0].id, "intake": {"reason": "Cough", "symptoms": "dry cough"}})
    assert r.status_code == 201
    d = r.json()
    assert d["status"] == "draft" and d["progress"]["percent"] < 100
    assert set(d["progress"]["missing"]) == {"availability", "visit_type"}
    # nothing reaches staff while it is a draft
    assert w.notes("staff", w.staff[0].id) == []
    assert svc.list_for_provider(w.db, w.provs[0].id) == []
    # dashboard lists it as incomplete
    dash = c.get("/api/appt/dashboard-summary").json()
    assert dash["counts"]["incomplete_intake"] == 1 and dash["incomplete_intake"][0]["id"] == d["id"]
    # submit with gaps -> field errors
    bad = c.post(f"/api/appt/appointments/{d['id']}/submit", json={})
    assert bad.status_code == 422 and {"availability", "visit_type"} <= set(bad.json()["fields"])
    assert "onset" not in bad.json()["fields"]  # "When did it start?" is optional
    # fill in (leaving the optional start blank), review, submit
    c.put(f"/api/appt/appointments/{d['id']}", json={"intake": {"availability": "Mornings", "visit_type": "video"}})
    rev = c.get(f"/api/appt/appointments/{d['id']}/review").json()
    assert rev["ready"] and rev["to"] == w.provs[0].name and rev["contact_shared"]["legal_name"] == "Alice Anders"
    assert rev["medical_summary"] is None and "emergency" in rev["disclaimer"].lower()
    ok = c.post(f"/api/appt/appointments/{d['id']}/submit", json={})
    assert ok.status_code == 200 and ok.json()["status"] == "requested"
    assert c.get("/api/appt/dashboard-summary").json()["counts"]["incomplete_intake"] == 0
    # a sent request can no longer be edited
    assert c.put(f"/api/appt/appointments/{d['id']}", json={"intake": {"reason": "x"}}).status_code == 409


def test_optional_onset_never_blocks_send(world):
    """QA-05: symptoms filled, 'When did it start?' (labelled optional) blank -> ready to send."""
    c = world.client(world.A)
    onset = next(f for f in c.get("/api/appt/intake-form").json()["fields"] if f["name"] == "onset")
    assert onset["required"] is False
    d = c.post("/api/appt/appointments", json={"provider_id": world.provs[0].id, "intake": {
        "reason": "Cough", "symptoms": "dry cough", "availability": "Mornings", "visit_type": "phone"}}).json()
    assert d["progress"]["missing"] == [] and d["progress"]["percent"] == 100
    assert c.get(f"/api/appt/appointments/{d['id']}/review").json()["ready"] is True
    assert c.post(f"/api/appt/appointments/{d['id']}/submit", json={}).status_code == 200


def test_validation(world):
    c = world.client(world.A)
    r = c.post("/api/appt/appointments", json={"provider_id": "nope", "intake": {}})
    assert r.status_code == 422 and "provider_id" in r.json()["fields"]
    r = c.post("/api/appt/appointments", json={"provider_id": world.provs[0].id, "intake": {"visit_type": "teleport"}})
    assert r.status_code == 422 and "visit_type" in r.json()["fields"]
    r = c.post("/api/appt/appointments", json={"provider_id": world.provs[0].id, "intake": {"reason": "x" * 1001}})
    assert r.status_code == 422


# ---------------------------------------------------------------- staff decisions via the service

def test_status_transitions_and_patient_notifications(world):
    w = world
    a = w.request(w.A)
    aid, nurse = a["id"], w.staff[0]
    # booking needs a time
    with pytest.raises(FieldError):
        svc.set_status(w.db, aid, "booked", staff_id=nurse.id)
    w.db.rollback()
    svc.set_status(w.db, aid, "booked", new_time="2026-11-03 10:00", staff_id=nurse.id)
    w.db.commit()
    got = _appt(w, aid)
    assert got.status == "booked" and got.scheduled_for == "2026-11-03 10:00"
    titles = [n.title for n in w.notes("patient", w.A.id, "appointment_update")]
    assert any("confirmed" in t for t in titles)
    assert w.audit(w.A.id, "appointment_booked")[0].actor_type == "staff"
    # staff proposes a new time -> patient confirms
    svc.set_status(w.db, aid, "rescheduled", new_time="2026-11-10 14:00", staff_id=nurse.id, note="Doctor is away")
    w.db.commit()
    assert _appt(w, aid).staff_note == "Doctor is away"
    c = w.client(w.A)
    assert c.post(f"/api/appt/appointments/{aid}/confirm").json()["status"] == "booked"
    assert _appt(w, aid).scheduled_for == "2026-11-10 14:00"
    assert len(w.notes("staff", nurse.id)) == 3  # request, intake, "accepted the new time"
    # decline is final
    b = w.request(w.A)
    svc.set_status(w.db, b["id"], "declined", staff_id=nurse.id)
    w.db.commit()
    with pytest.raises(svc.InvalidTransition):
        svc.set_status(w.db, b["id"], "booked", new_time="x", staff_id=nurse.id)
    w.db.rollback()
    assert c.post(f"/api/appt/appointments/{b['id']}/cancel").status_code == 409
    with pytest.raises(svc.ApptNotFound):
        svc.set_status(w.db, "no-such-id", "booked", new_time="x")


def test_staff_of_other_organization_cannot_change_status(world):
    w = world
    a = w.request(w.A)
    with pytest.raises(PermissionError):
        svc.set_status(w.db, a["id"], "booked", new_time="2026-11-03 10:00", staff_id=w.staff[1].id)
    w.db.rollback()
    assert _appt(w, a["id"]).status == "requested"
    # staff may not push an appointment back to draft/requested
    with pytest.raises(svc.InvalidTransition):
        svc.set_status(w.db, a["id"], "requested", staff_id=w.staff[0].id)
    w.db.rollback()
    assert svc.list_for_provider(w.db, w.provs[1].id) == []
    assert [x.id for x in svc.list_for_provider(w.db, w.provs[0].id, status="requested")] == [a["id"]]
    assert svc.list_for_provider(w.db, w.provs[0].id, status="booked") == []


def test_patient_reschedule_and_cancel(world):
    w = world
    c = w.client(w.A)
    a = w.request(w.A)
    svc.set_status(w.db, a["id"], "booked", new_time="2026-11-03 10:00", staff_id=w.staff[0].id)
    w.db.commit()
    before = len(w.notes("staff", w.staff[0].id, "appointment_request"))
    r = c.post(f"/api/appt/appointments/{a['id']}/reschedule", json={"preferred_times": "Thursdays after 2pm", "reason": "Work"})
    assert r.status_code == 200 and r.json()["status"] == "requested"
    assert r.json()["intake"]["reschedule_from"] == "2026-11-03 10:00"
    assert len(w.notes("staff", w.staff[0].id, "appointment_request")) == before + 1
    assert c.post(f"/api/appt/appointments/{a['id']}/reschedule", json={"preferred_times": ""}).status_code == 422
    r = c.post(f"/api/appt/appointments/{a['id']}/cancel", json={"reason": "Feeling better"})
    assert r.json()["status"] == "cancelled" and r.json()["can_cancel"] is False
    assert any("cancelled" in n.title for n in w.notes("staff", w.staff[0].id, "appointment_request"))
    assert w.audit(w.A.id, "appointment_cancelled")
    hist = [e.to_status for e in w.db.query(AppointmentEvent).filter_by(appointment_id=a["id"]).order_by(AppointmentEvent.id)]
    assert hist == ["draft", "requested", "booked", "requested", "cancelled"]


# ---------------------------------------------------------------- isolation

def test_patients_cannot_see_or_touch_each_others_appointments(world):
    w = world
    a = w.request(w.A)
    cb = w.client(w.B)
    for method, url, kw in [("get", f"/api/appt/appointments/{a['id']}", {}), ("put", f"/api/appt/appointments/{a['id']}", {"json": {"intake": {"reason": "hacked"}}}),
                            ("post", f"/api/appt/appointments/{a['id']}/cancel", {}), ("post", f"/api/appt/appointments/{a['id']}/submit", {"json": {}}),
                            ("get", f"/api/appt/appointments/{a['id']}/review", {}), ("post", f"/api/appt/appointments/{a['id']}/confirm", {}),
                            ("delete", f"/api/appt/appointments/{a['id']}", {})]:
        assert getattr(cb, method)(url, **kw).status_code == 404, url
    assert cb.get("/api/appt/appointments").json()["items"] == []
    assert cb.get("/api/appointments").json()["items"] == []
    assert cb.get("/api/appt/appointments", params={"for_patient_id": w.A.id}).status_code == 404
    assert cb.get("/api/appt/intake-form", params={"for_patient_id": w.A.id}).status_code == 404
    assert cb.post("/api/appt/appointments", json={"provider_id": w.provs[0].id, "intake": INTAKE, "for_patient_id": w.A.id}).status_code == 404
    assert _appt(w, a["id"]).status == "requested"
    # not signed in
    assert w.anon().get("/api/appt/appointments").status_code == 401


# ---------------------------------------------------------------- dependents

def test_dependent_profile_appointments_notify_guardian_only(world):
    w = world
    c = w.client(w.A)
    d = c.post("/api/care/dependents", json={"name": "Dana Anders", "dob": "2015-03-04", "relationship": "daughter"})
    assert d.status_code == 201
    dep_pid = d.json()["patient_id"]
    form = c.get("/api/appt/intake-form", params={"for_patient_id": dep_pid}).json()
    assert form["is_dependent"] and form["prefill"]["legal_name"] == "Dana Anders" and form["prefill"]["guardian_name"] == "Alice Anders"
    r = c.post("/api/appt/appointments", json={"provider_id": w.provs[0].id, "intake": INTAKE, "submit": True, "for_patient_id": dep_pid})
    assert r.status_code == 201 and r.json()["patient_id"] == dep_pid
    aid = r.json()["id"]
    svc.set_status(w.db, aid, "booked", new_time="2026-12-01 09:00", staff_id=w.staff[0].id)
    w.db.commit()
    mine = [n.title for n in w.notes("patient", w.A.id, "appointment_update")]
    assert any(t.startswith("Dana Anders:") and "confirmed" in t for t in mine)
    assert w.notes("patient", dep_pid) == []
    # shows on guardian's list and dashboard (flagged), but a stranger can't reach it
    assert any(i["id"] == aid for i in c.get("/api/appt/appointments").json()["items"])
    dash = c.get("/api/appt/dashboard-summary").json()
    assert dash["next_appointment"]["for_name"] == "Dana Anders"
    assert w.client(w.B).get(f"/api/appt/appointments/{aid}").status_code == 404
    assert c.post(f"/api/appt/appointments/{aid}/cancel").json()["status"] == "cancelled"
    # removing the dependent ends guardian access
    did = d.json()["id"]
    assert c.delete(f"/api/care/dependents/{did}").status_code == 200
    assert c.get(f"/api/appt/appointments/{aid}").status_code == 404
    # dependents cannot sign in
    from app.models.shared import Patient
    assert w.db.get(Patient, dep_pid).password_hash.startswith("!")


# ---------------------------------------------------------------- providers + seed

def test_provider_search_and_specialties(world):
    w = world
    c = w.client(w.A)
    names = [p["name"] for p in c.get("/api/appt/providers", params={"q": "ophthal"}).json()["items"]]
    assert w.provs[1].name in names and w.provs[0].name not in names
    names = [p["name"] for p in c.get("/api/appt/providers", params={"specialty": "Family medicine"}).json()["items"]]
    assert w.provs[0].name in names
    assert "Ophthalmology" in c.get("/api/appt/specialties").json()["items"]
    assert c.get("/api/appt/providers", params={"q": "%"}).status_code == 200


def test_seed_appointments_is_idempotent(world):
    from app.services.seed_appointments import seed_appointments
    w = world
    out = seed_appointments(w.db, w.A.id)
    assert out["appointments"] == 4
    assert seed_appointments(w.db, w.A.id).get("skipped")
    dash = w.client(w.A).get("/api/appt/dashboard-summary").json()
    assert dash["counts"]["incomplete_intake"] == 1 and dash["counts"]["needs_confirmation"] == 1
    assert dash["next_appointment"] is not None
