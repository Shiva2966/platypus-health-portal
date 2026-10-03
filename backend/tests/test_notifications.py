"""Notifications for patients AND staff: isolation, read state, preferences, SSE, reminder sweep."""
import json
from datetime import timedelta

import w3_support as w

import pytest

NURSE = "nurse@riverside.demo"


@pytest.fixture(autouse=True)
def _no_otp(monkeypatch):
    w.legacy_auth_env(monkeypatch)


def _bill(s, pid, provider, amount, due, status):
    import uuid
    from datetime import date
    from decimal import Decimal

    from app.models.billing import BillingBill
    b = BillingBill(patient_id=pid, provider_name=provider, service_date=date(2026, 1, 5), billed_amount=Decimal(amount),
                    amount_due=Decimal(amount), due_date=due, status=status, dedupe_key=uuid.uuid4().hex)
    s.add(b)
    s.flush()
    return b


def _push(rtype, rid, kind, title="T", plain=True):
    from app.services.notif_helpers import notify_pref
    from app.services.notifications import notify
    s = w.db()
    try:
        fn = notify if plain else notify_pref
        n = fn(s, recipient_type=rtype, recipient_id=rid, kind=kind, title=title)
        s.commit()
        return n.id if n else None
    finally:
        s.close()


def test_requires_a_session():
    c = w.new_client()
    for path in ("/api/notifications", "/api/notifications/unread-count", "/api/notifications/prefs"):
        assert c.get(path).status_code == 401
    assert c.post("/api/notifications/read-all").status_code == 401
    pc, *_ = w.make_patient()
    assert pc.get("/api/notifications", params={"as": "staff"}).status_code == 401  # patient cookie != staff
    sc = w.staff_login(NURSE)
    assert sc.get("/api/notifications", params={"as": "patient"}).status_code == 401


def test_patient_and_staff_cannot_read_each_others_notifications():
    pc, pid, *_ = w.make_patient()
    sc = w.staff_login(NURSE)
    sid = w.staff_id(NURSE)
    p_note = _push("patient", pid, "bill_due", "PATIENT-ONLY-MARKER")
    s_note = _push("staff", sid, "access_approved", "STAFF-ONLY-MARKER")

    pl = pc.get("/api/notifications").json()
    assert pl["recipient_type"] == "patient" and "STAFF-ONLY-MARKER" not in json.dumps(pl)
    sl = sc.get("/api/notifications", params={"as": "staff"}).json()
    assert sl["recipient_type"] == "staff" and "PATIENT-ONLY-MARKER" not in json.dumps(sl)
    assert any(i["title"] == "STAFF-ONLY-MARKER" for i in sl["items"])

    # cross-reads/marks are indistinguishable from "not found"
    assert pc.post(f"/api/notifications/{s_note}/read").status_code == 404
    assert sc.post(f"/api/notifications/{p_note}/read", params={"as": "staff"}).status_code == 404
    # another patient can't touch it either
    other, *_ = w.make_patient()
    assert other.post(f"/api/notifications/{p_note}/read").status_code == 404
    assert pc.get("/api/notifications", params={"unread": True}).json()["items"][0]["read"] is False


def test_mark_read_unread_count_filters_and_read_all():
    pc, pid, *_ = w.make_patient()
    a = _push("patient", pid, "bill_due", "Bill A")
    _push("patient", pid, "bill_due", "Bill B")
    _push("patient", pid, "info_outdated", "Outdated")
    assert pc.get("/api/notifications/unread-count").json()["unread"] == 3
    assert pc.post(f"/api/notifications/{a}/read").json()["unread"] == 2
    assert pc.post(f"/api/notifications/{a}/read").status_code == 200  # idempotent
    j = pc.get("/api/notifications", params={"unread": True}).json()
    assert j["total"] == 2 and all(not i["read"] for i in j["items"])
    j = pc.get("/api/notifications", params={"kind": "bill_due"}).json()
    assert j["total"] == 2 and {i["title"] for i in j["items"]} == {"Bill A", "Bill B"}
    assert pc.get("/api/notifications", params={"limit": 1}).json()["items"][0]["title"] == "Outdated"  # newest first
    assert pc.post("/api/notifications/read-all").json()["unread"] == 0
    assert pc.get("/api/notifications/unread-count").json()["unread"] == 0
    assert pc.get("/api/notifications", params={"limit": 500}).status_code == 422


def test_read_all_only_affects_own_notifications():
    pc, pid, *_ = w.make_patient()
    other, oid, *_ = w.make_patient()
    _push("patient", pid, "bill_due")
    _push("patient", oid, "bill_due")
    pc.post("/api/notifications/read-all")
    assert other.get("/api/notifications/unread-count").json()["unread"] == 1


def test_preferences_are_respected():
    pc, pid, *_ = w.make_patient()
    prefs = pc.get("/api/notifications/prefs").json()["prefs"]
    kinds = {p["kind"] for p in prefs}
    assert kinds == {"access_request", "document_viewed", "share_expiring", "share_revoked", "appointment_update",
                     "bill_due", "info_outdated", "doc_uploaded", "record_viewed", "share_redeemed",
                     "correction_update", "caregiver_activity"} and all(p["enabled"] for p in prefs)
    assert pc.put("/api/notifications/prefs", json={"prefs": {"bogus": False}}).status_code == 422
    # staff kinds are not valid for a patient
    assert pc.put("/api/notifications/prefs", json={"prefs": {"access_approved": False}}).status_code == 422

    r = pc.put("/api/notifications/prefs", json={"prefs": {"bill_due": False}})
    assert {p["kind"]: p["enabled"] for p in r.json()["prefs"]}["bill_due"] is False
    # helper skips creation; plain notify() rows are hidden from list + counts
    assert _push("patient", pid, "bill_due", "skipped", plain=False) is None
    _push("patient", pid, "bill_due", "hidden-by-pref")
    _push("patient", pid, "info_outdated", "shown")
    j = pc.get("/api/notifications").json()
    assert [i["title"] for i in j["items"]] == ["shown"] and j["unread"] == 1
    assert pc.get("/api/notifications/unread-count").json()["unread"] == 1
    # switching it back on brings the (still stored) rows back
    pc.put("/api/notifications/prefs", json={"prefs": {"bill_due": True}})
    assert pc.get("/api/notifications/unread-count").json()["unread"] == 2

    sc = w.staff_login(NURSE)
    sp = sc.get("/api/notifications/prefs", params={"as": "staff"}).json()["prefs"]
    assert {p["kind"] for p in sp} == {"document_shared", "access_approved", "access_denied", "access_revoked",
                                       "appointment_request", "intake_received"}
    assert sc.put("/api/notifications/prefs", params={"as": "staff"}, json={"prefs": {"bill_due": False}}).status_code == 422


def test_real_flow_respects_preference_for_appointment_updates():
    """The staff decision path uses notify_pref, so a patient who opted out gets no appointment_update."""
    pc, pid, *_ = w.make_patient()
    pc.put("/api/notifications/prefs", json={"prefs": {"appointment_update": False}})
    aid = w.submit_appointment(pid, w.riverside_id(), {"reason": "Checkup", "availability": "Any", "visit_type": "phone"})
    front = w.staff_login("frontdesk@riverside.demo")
    r = front.post(f"/api/staff/appointments/{aid}/decision", json={"action": "accept", "scheduled_for": "2030-02-01T10:00"})
    assert r.status_code == 200, r.text
    # the notification may exist in the DB (created by W9's service) but is hidden + not counted
    assert pc.get("/api/notifications", params={"kind": "appointment_update"}).json()["items"] == []
    assert pc.get("/api/notifications/unread-count").json()["unread"] == 0
    # (whether the row is stored or never created is up to W9's service; the user never sees it)


def test_sse_stream_pushes_unread_count():
    pc, pid, *_ = w.make_patient()
    _push("patient", pid, "bill_due")
    with pc.stream("GET", "/api/notifications/stream", params={"once": True}) as r:
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
        body = "".join(r.iter_text())
    assert "event: unread" in body and '"unread": 1' in body
    assert w.new_client().get("/api/notifications/stream").status_code == 401


def test_reminder_sweep_is_idempotent_and_sends_appointment_and_bill_reminders():
    from app.models.appointments import Appointment
    from app.models.shared import Notification, utcnow
    from app.services.reminders import run_reminder_sweep
    from sqlalchemy import select

    pc, pid, *_ = w.make_patient()
    s = w.db()
    try:
        soon = (utcnow() + timedelta(hours=5)).isoformat(timespec="minutes")
        s.add(Appointment(patient_id=pid, provider_id=w.riverside_id(), status="booked", scheduled_for=soon,
                          submitted_at=utcnow()))
        s.add(Appointment(patient_id=pid, provider_id=w.riverside_id(), status="booked",
                          scheduled_for=(utcnow() + timedelta(days=9)).isoformat(timespec="minutes")))
        due = (utcnow() + timedelta(days=2)).date()
        _bill(s, pid, "Riverside General Hospital", 80, due, "unpaid")
        _bill(s, pid, "Paid Clinic", 50, due, "paid")
        s.commit()
        first = run_reminder_sweep(s)
        second = run_reminder_sweep(s)
        # the sweep is global (other tests' patients may also have reminders), so check totals loosely and this patient strictly below
        assert first["appointment_reminders"] >= 1 and first["bill_reminders"] >= 1
        assert second["appointment_reminders"] == 0 and second["bill_reminders"] == 0
        rows = s.scalars(select(Notification).where(Notification.recipient_id == pid)).all()
        kinds = sorted(n.kind for n in rows)
        assert kinds.count("bill_due") == 1 and sum(1 for n in rows if n.title == "Appointment reminder") == 1
        assert "$80.00" in next(n for n in rows if n.kind == "bill_due").body
    finally:
        s.close()


def test_reminders_respect_preferences():
    from app.models.shared import utcnow
    from app.services.reminders import run_reminder_sweep

    pc, pid, *_ = w.make_patient()
    pc.put("/api/notifications/prefs", json={"prefs": {"bill_due": False}})
    s = w.db()
    try:
        _bill(s, pid, "X", 10, (utcnow() - timedelta(days=1)).date(), "unpaid")
        s.commit()
        assert run_reminder_sweep(s)["bill_reminders"] == 0
    finally:
        s.close()
    assert pc.get("/api/notifications").json()["items"] == []


def test_sweep_started_by_app_lifespan_when_enabled(monkeypatch):
    """The app lifespan starts the background scheduler (reminders + consent expiry)."""
    import app.scheduler as sched_module

    started = []

    def fake_start():
        started.append(True)

    monkeypatch.setattr(sched_module, "start", fake_start)
    monkeypatch.delenv("HP_DISABLE_SCHEDULER", raising=False)
    with w.TestClient(w.app):
        pass
    assert started == [True]
