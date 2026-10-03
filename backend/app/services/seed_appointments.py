"""Synthetic appointments / privacy demo data (W9).  ``seed_appointments(db, patient_id)`` is idempotent and commits."""
from __future__ import annotations

import json
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.appointments import Appointment, ConnectedApp
from app.models.shared import Patient, Provider, utcnow
from app.services import appointments as svc
from app.services.appt_privacy import get_settings


def seed_appointments(db: Session, patient_id: str) -> dict:
    """Creates: one booked visit, one pending request, one request waiting for the patient to confirm a new
    time, one incomplete intake draft, default privacy settings and one connected demo app.
    Uses existing Provider rows (does nothing for appointments if there are none)."""
    patient = db.get(Patient, patient_id)
    if patient is None:
        return {"appointments": 0, "skipped": "patient not found"}
    get_settings(db, patient_id)
    out = {"appointments": 0}
    if not db.scalar(select(func.count()).select_from(ConnectedApp).where(ConnectedApp.patient_id == patient_id)):
        db.add(ConnectedApp(patient_id=patient_id, app_key="carecalendar", name="CareCalendar (demo)",
                            scopes_json=json.dumps(["Appointment dates (read)"])))
    if db.scalar(select(func.count()).select_from(Appointment).where(Appointment.patient_id == patient_id)):
        db.commit()
        out["skipped"] = "already has appointments"
        return out
    provs = list(db.scalars(select(Provider).order_by(Provider.name)))
    if not provs:
        db.commit()
        out["skipped"] = "no providers"
        return out
    p = lambda i: provs[i % len(provs)]  # noqa: E731
    now = utcnow()

    def make(prov, status, intake, when=None, days_ago=0, note=None):
        a = Appointment(patient_id=patient_id, provider_id=prov.id, status="draft", intake_json=json.dumps(intake))
        db.add(a)
        db.flush()
        if status != "draft":
            a.contact_json = json.dumps(svc.profile_snapshot(patient))
            a.submitted_at = now - timedelta(days=days_ago)
            a.status, a.scheduled_for, a.staff_note = status, when, note
        a.created_at = now - timedelta(days=days_ago + 1)
        db.add(svc.AppointmentEvent(appointment_id=a.id, to_status=status, actor_type="system", note="demo seed"))
        out["appointments"] += 1
        return a

    day = lambda n: (now + timedelta(days=n)).strftime("%Y-%m-%d 10:30")  # noqa: E731
    make(p(0), "booked", {"reason": "Yearly check-up", "availability": "Weekday mornings", "visit_type": "in_person"},
         when=day(12), days_ago=6)
    make(p(1), "requested", {"reason": "My knee aches when I climb stairs", "symptoms": "A dull ache after walking",
                             "onset": "About two weeks ago", "duration": "A few hours after activity",
                             "availability": "Afternoons, not Fridays", "visit_type": "video",
                             "accommodations": "Large-print forms please"}, days_ago=2)
    make(p(2), "rescheduled", {"reason": "Follow-up on blood test", "availability": "Any weekday", "visit_type": "phone"},
         when=day(20), days_ago=4, note="The doctor is away that week; does this new time work?")
    make(p(0), "draft", {"reason": "Skin rash on my arm"}, days_ago=1)
    db.commit()
    return out
