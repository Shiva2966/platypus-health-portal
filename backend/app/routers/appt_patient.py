"""Patient-side appointments + intake (W9).  Prefix /api/appt, plus GET /api/appointments for W1's dashboard widget.

NOT a diagnosis, NO triage: free text is stored as written; the UI shows the emergency label on every step.
Every route is owned-by-the-signed-in-patient (or one of their dependents); anything else is a 404.
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models.appointments import Appointment, DependentProfile
from app.models.shared import Patient, Provider
from app.security import current_patient
from app.services import appointments as svc
from app.services import appt_care as care

router = APIRouter(prefix="/api", tags=["appointments"])


def _err(e: Exception) -> HTTPException:
    if isinstance(e, svc.ApptNotFound):
        return HTTPException(404, "Not found.")
    if isinstance(e, svc.InvalidTransition):
        return HTTPException(409, str(e))
    if isinstance(e, care.CareError):
        return HTTPException(e.status, e.message)
    raise e


def _owned(db: Session, p: Patient, aid: str) -> Appointment:
    a = db.get(Appointment, aid)
    if a is not None and a.patient_id != p.id:
        ok = db.scalar(select(func.count()).select_from(DependentProfile).where(
            DependentProfile.guardian_patient_id == p.id, DependentProfile.dependent_patient_id == a.patient_id,
            DependentProfile.ended_at.is_(None)))
        if not ok:
            a = None
    if a is None:
        raise HTTPException(404, "Not found.")
    return a


class ApptIn(BaseModel):
    provider_id: str | None = None
    intake: dict = Field(default_factory=dict)
    submit: bool = False
    include_medical_summary: bool = False
    for_patient_id: str | None = None


class ApptPatch(BaseModel):
    provider_id: str | None = None
    intake: dict | None = None


class SubmitIn(BaseModel):
    include_medical_summary: bool = False


class RescheduleIn(BaseModel):
    preferred_times: str = Field(default="", max_length=500)
    reason: str | None = Field(default=None, max_length=500)


class CancelIn(BaseModel):
    reason: str | None = Field(default=None, max_length=500)


@router.get("/appt/info")
def info():
    return {"disclaimer": svc.NOT_A_DIAGNOSIS, "statuses": list(svc.STATUSES), "visit_types": list(svc.VISIT_TYPES)}


# ---------------- provider / specialty search ----------------

@router.get("/appt/providers")
def search_providers(q: str = "", specialty: str = "", limit: int = 50, _p: Patient = Depends(current_patient),
                     db: Session = Depends(get_db)):
    limit = max(1, min(limit, 100))
    stmt = select(Provider)
    q = q.strip().lower()[:100]
    if q:
        like = "%" + q.replace("%", "").replace("_", "") + "%"
        stmt = stmt.where(or_(func.lower(Provider.name).like(like), func.lower(func.coalesce(Provider.specialty, "")).like(like),
                              func.lower(func.coalesce(Provider.address, "")).like(like)))
    if specialty.strip():
        stmt = stmt.where(func.lower(Provider.specialty) == specialty.strip().lower())
    rows = db.scalars(stmt.order_by(Provider.name).limit(limit)).all()
    return {"items": [r.to_dict() for r in rows],
            "note": "Availability shown is an example for this demo. The clinic confirms the real time."}


@router.get("/appt/specialties")
def specialties(_p: Patient = Depends(current_patient), db: Session = Depends(get_db)):
    rows = db.scalars(select(Provider.specialty).where(Provider.specialty.is_not(None)).distinct().order_by(Provider.specialty)).all()
    return {"items": [r for r in rows if r]}


# ---------------- form ----------------

@router.get("/appt/intake-form")
def intake_form(for_patient_id: str | None = None, p: Patient = Depends(current_patient), db: Session = Depends(get_db)):
    try:
        subject = care.resolve_subject(db, p, for_patient_id)
        pf = svc.prefill(db, subject.id)
    except Exception as e:
        raise _err(e)
    return {"fields": svc.INTAKE_FIELDS, "prefill": pf["profile"], "medical_summary": pf["medical_summary"],
            "missing_profile": pf["missing_profile"], "is_dependent": pf["is_dependent"],
            "for_patient_id": subject.id, "disclaimer": svc.NOT_A_DIAGNOSIS}


# ---------------- appointments ----------------

def _list(db: Session, p: Patient, for_patient_id: str | None, status: str | None) -> list[dict]:
    ids = [p.id] + [d.dependent_patient_id for d in svc.dependents_of(db, p.id)]
    if for_patient_id:
        if for_patient_id not in ids:
            raise HTTPException(404, "Not found.")
        ids = [for_patient_id]
    stmt = select(Appointment).where(Appointment.patient_id.in_(ids))
    if status:
        if status not in svc.STATUSES:
            raise HTTPException(422, "Unknown status.")
        stmt = stmt.where(Appointment.status == status)
    provs = {x.id: x for x in db.scalars(select(Provider))}
    rows = db.scalars(stmt.order_by(Appointment.updated_at.desc()).limit(200)).all()
    out = []
    for a in rows:
        d = svc.to_dict(db, a, provider=provs.get(a.provider_id))
        d["for_name"] = d["patient_name"] if a.patient_id != p.id else None
        out.append(d)
    return out


@router.get("/appt/appointments")
def list_appointments(for_patient_id: str | None = None, status: str | None = None, p: Patient = Depends(current_patient),
                      db: Session = Depends(get_db)):
    return {"items": _list(db, p, for_patient_id, status), "disclaimer": svc.NOT_A_DIAGNOSIS}


@router.get("/appointments")
def list_appointments_for_dashboard(p: Patient = Depends(current_patient), db: Session = Depends(get_db)):
    """W1's dashboard widget calls this."""
    return {"items": _list(db, p, None, None)}


@router.get("/appt/dashboard-summary")
def dashboard_summary(p: Patient = Depends(current_patient), db: Session = Depends(get_db)):
    return svc.get_dashboard_summary(db, p.id)


@router.post("/appt/appointments", status_code=201)
def create_appointment(body: ApptIn, p: Patient = Depends(current_patient), db: Session = Depends(get_db)):
    try:
        subject = care.resolve_subject(db, p, body.for_patient_id)
        a = svc.create(db, patient_id=subject.id, provider_id=body.provider_id, intake=body.intake, submit=body.submit,
                       include_medical=body.include_medical_summary, actor_type="patient", actor_id=p.id)
        db.commit()
    except (svc.ApptNotFound, svc.InvalidTransition, care.CareError) as e:
        db.rollback()
        raise _err(e)
    return svc.to_dict(db, a, history=True)


@router.get("/appt/appointments/{aid}")
def get_appointment(aid: str, p: Patient = Depends(current_patient), db: Session = Depends(get_db)):
    return svc.to_dict(db, _owned(db, p, aid), history=True)


@router.put("/appt/appointments/{aid}")
def update_appointment(aid: str, body: ApptPatch, p: Patient = Depends(current_patient), db: Session = Depends(get_db)):
    a = _owned(db, p, aid)
    try:
        kw = {"intake": body.intake}
        if "provider_id" in body.model_fields_set:
            kw["provider_id"] = body.provider_id
        svc.update(db, a, actor_type="patient", actor_id=p.id, **kw)
        db.commit()
    except (svc.InvalidTransition, svc.ApptNotFound) as e:
        db.rollback()
        raise _err(e)
    return svc.to_dict(db, a, history=True)


@router.get("/appt/appointments/{aid}/review")
def review(aid: str, include_medical: bool = False, p: Patient = Depends(current_patient), db: Session = Depends(get_db)):
    """Review-before-submit: exactly what will be sent to the clinic."""
    return svc.review_summary(db, _owned(db, p, aid), include_medical=include_medical)


@router.post("/appt/appointments/{aid}/submit")
def submit(aid: str, body: SubmitIn, p: Patient = Depends(current_patient), db: Session = Depends(get_db)):
    a = _owned(db, p, aid)
    try:
        svc.submit_request(db, a, include_medical=body.include_medical_summary, actor_type="patient", actor_id=p.id)
        db.commit()
    except svc.InvalidTransition as e:
        db.rollback()
        raise _err(e)
    return svc.to_dict(db, a, history=True)


@router.post("/appt/appointments/{aid}/reschedule")
def reschedule(aid: str, body: RescheduleIn, p: Patient = Depends(current_patient), db: Session = Depends(get_db)):
    a = _owned(db, p, aid)
    try:
        svc.request_reschedule(db, a, preferred_times=body.preferred_times, reason=body.reason, actor_type="patient", actor_id=p.id)
        db.commit()
    except svc.InvalidTransition as e:
        db.rollback()
        raise _err(e)
    return svc.to_dict(db, a, history=True)


@router.post("/appt/appointments/{aid}/confirm")
def confirm(aid: str, p: Patient = Depends(current_patient), db: Session = Depends(get_db)):
    a = _owned(db, p, aid)
    try:
        svc.confirm_proposed_time(db, a, actor_type="patient", actor_id=p.id)
        db.commit()
    except svc.InvalidTransition as e:
        db.rollback()
        raise _err(e)
    return svc.to_dict(db, a, history=True)


@router.post("/appt/appointments/{aid}/cancel")
def cancel(aid: str, body: CancelIn | None = None, p: Patient = Depends(current_patient), db: Session = Depends(get_db)):
    a = _owned(db, p, aid)
    try:
        svc.cancel(db, a, reason=body.reason if body else None, actor_type="patient", actor_id=p.id)
        db.commit()
    except svc.InvalidTransition as e:
        db.rollback()
        raise _err(e)
    return svc.to_dict(db, a, history=True)


@router.delete("/appt/appointments/{aid}")
def delete_draft(aid: str, p: Patient = Depends(current_patient), db: Session = Depends(get_db)):
    a = _owned(db, p, aid)
    if a.status != "draft":
        raise HTTPException(409, "Only drafts can be deleted. Cancel a sent request instead.")
    db.delete(a)
    db.commit()
    return {"ok": True}
