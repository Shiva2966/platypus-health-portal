"""Appointment/intake inbox for staff: list, view standardized intake, accept/reschedule/decline, JSON export.

Status rules + patient notification + history live in W9's app/services/appointments.py (set_status)."""
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models.appointments import Appointment
from app.models.shared import Patient, Provider
from app.models.staff import StaffUser
from app.services import appointments as appt_service
from app.services import staff_portal as sp
from app.services.audit import log_event
from app.services.staff_security import require_role

router = APIRouter(prefix="/api/staff/appointments", tags=["staff-appointments"])
Staff = Depends(require_role("front_desk", "nurse", "physician"))
INBOX_STATUSES = ("requested", "booked", "rescheduled", "declined", "cancelled")
DECISION_STATUS = {"accept": "booked", "reschedule": "rescheduled", "decline": "declined"}


class DecisionIn(BaseModel):
    action: Literal["accept", "reschedule", "decline"]
    scheduled_for: str | None = Field(default=None, max_length=40)
    note: str | None = Field(default=None, max_length=1000)


def _own_appointment(db: Session, staff: StaffUser, appt_id: str) -> Appointment:
    a = db.get(Appointment, appt_id)
    # Same 404 whether it does not exist or belongs to another organization.
    if a is None or not staff.provider_id or a.provider_id != staff.provider_id or a.status == "draft":
        raise HTTPException(404, "Appointment not found.")
    return a


@router.get("")
def inbox(status: str | None = Query(default=None), staff: StaffUser = Staff, db: Session = Depends(get_db)):
    if status and status not in INBOX_STATUSES:
        raise HTTPException(422, "Unknown status filter.")
    rows = appt_service.list_for_provider(db, staff.provider_id, status=status)
    patients = {p.id: p for p in db.scalars(select(Patient).where(
        Patient.id.in_({a.patient_id for a in rows} or {""}))).all()}
    return {"appointments": [sp.appointment_to_dict(db, a, patients.get(a.patient_id), include_intake=False)
                             for a in rows]}


@router.get("/{appt_id}")
def detail(appt_id: str, staff: StaffUser = Staff, db: Session = Depends(get_db)):
    a = _own_appointment(db, staff, appt_id)
    patient = db.get(Patient, a.patient_id)
    log_event(db, actor_type="staff", actor_id=staff.id, patient_id=a.patient_id, action="appointment_viewed",
              resource_type="appointment", resource_id=a.id, detail={"role": staff.role})
    db.commit()
    return sp.appointment_to_dict(db, a, patient)


@router.post("/{appt_id}/decision")
def decide(appt_id: str, body: DecisionIn, staff: StaffUser = Staff, db: Session = Depends(get_db)):
    a = _own_appointment(db, staff, appt_id)
    try:
        appt_service.set_status(db, a.id, DECISION_STATUS[body.action],
                                new_time=(body.scheduled_for or "").strip() or None,
                                staff_id=staff.id, note=(body.note or "").strip() or None)
    except appt_service.InvalidTransition as e:
        db.rollback()
        raise HTTPException(409, str(e))
    except (PermissionError, LookupError):
        db.rollback()
        raise HTTPException(404, "Appointment not found.")
    db.commit()
    db.refresh(a)
    return sp.appointment_to_dict(db, a, db.get(Patient, a.patient_id))


@router.get("/{appt_id}/export.json")
def export(appt_id: str, staff: StaffUser = Staff, db: Session = Depends(get_db)):
    a = _own_appointment(db, staff, appt_id)
    patient = db.get(Patient, a.patient_id)
    prov = db.get(Provider, a.provider_id)
    log_event(db, actor_type="staff", actor_id=staff.id, patient_id=a.patient_id, action="intake_exported",
              resource_type="appointment", resource_id=a.id, detail={"format": "fhir-like-json"})
    db.commit()
    return JSONResponse(sp.fhir_export(a, patient, prov.name if prov else None),
                        headers={"Content-Disposition": f'attachment; filename="intake-{a.id}.json"'},
                        media_type="application/fhir+json")
