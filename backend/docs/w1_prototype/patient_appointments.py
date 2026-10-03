"""Patient side of appointments + intake, provider list (seeded, mock availability/costs), dashboard."""
import json
from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core_schemas import F, SCHEMAS, clean_value
from app.db import get_db
from app.errors import FieldError
from app.models.core import Appointment, Record
from app.models.shared import Patient, Provider, utcnow
from app.routers.patient_records import rec_dict
from app.security import current_patient
from app.services.audit import log_event
from app.services.notifications import notify
from app.services.routing_hint import route_hint

router = APIRouter(prefix="/api")

INTAKE_FIELDS = [
    F("reason", "Why do you want to be seen? (in your own words)", "textarea", True, maxlength=1000),
    F("symptoms", "Any symptoms?", "textarea", help="Describe what you notice. Skip if none."),
    F("onset", "When did it start?", help="e.g. 'about 3 days ago' or 'last March'"),
    F("duration", "How long does it last / how often?", help="e.g. 'constant' or 'a few minutes, twice a day'"),
    F("availability", "When are you available?", "textarea", True, help="e.g. 'Weekday mornings, not Wednesdays'"),
    F("visit_type", "Type of visit", "select", True, options=[{"value": "in_person", "label": "In person"}, {"value": "video", "label": "Video visit"}, {"value": "phone", "label": "Phone call"}]),
    F("accommodations", "Anything we should prepare for you?", "textarea", help="e.g. wheelchair access, large-print forms, someone coming with you, quiet room."),
]
REQUIRED_TO_SUBMIT = ["provider_id", "reason", "availability", "visit_type"]


def appt_dict(a: Appointment, prov: Provider | None) -> dict:
    intake = a.intake
    missing = [k for k in REQUIRED_TO_SUBMIT if not (a.provider_id if k == "provider_id" else intake.get(k))]
    return {"id": a.id, "status": a.status, "provider": {"id": prov.id, "name": prov.name, "specialty": prov.specialty} if prov else None,
            "intake": intake, "contact": a.contact, "missing": missing if a.status == "draft" else [],
            "scheduled_for": a.scheduled_for, "staff_note": a.staff_note, "submitted_at": a.submitted_at,
            "created_at": a.created_at, "updated_at": a.updated_at,
            "can_cancel": a.status in ("requested", "booked", "rescheduled"), "is_draft": a.status == "draft"}


def _clean_intake(raw: dict) -> dict:
    out, errs = {}, {}
    for f in INTAKE_FIELDS:
        try:
            ff = {**f, "required": False}  # required-ness is enforced only on submit
            out[f["name"]] = clean_value(ff, raw.get(f["name"]))
        except ValueError as e:
            errs[f["name"]] = str(e)
    if errs:
        raise FieldError(errs)
    return out


class ApptIn(BaseModel):
    provider_id: str | None = None
    intake: dict = {}
    submit: bool = False


def _contact_snapshot(p: Patient) -> dict:
    return {"patient_id": p.id, "legal_name": p.legal_name, "preferred_name": p.preferred_name, "dob": p.dob,
            "phone": p.phone, "email": p.email, "pronouns": p.pronouns}


def _notify_staff(db: Session, a: Appointment, p: Patient, prov: Provider):
    """Tell staff of that provider about the request (W3's StaffUser model; skipped if not present yet)."""
    try:
        from app.models.staff import StaffUser  # type: ignore
        for s in db.scalars(select(StaffUser).where(StaffUser.provider_id == prov.id)):
            notify(db, recipient_type="staff", recipient_id=s.id, kind="appointment_request",
                   title=f"New appointment request from {p.display_name}", body=(a.intake.get("reason") or "")[:140], link="#/appointments")
    except Exception:
        pass


def _save(db, p, a: Appointment | None, body: ApptIn) -> Appointment:
    prov = None
    if body.provider_id:
        prov = db.get(Provider, body.provider_id)
        if not prov:
            raise FieldError({"provider_id": "Choose a provider from the list."})
    intake = _clean_intake(body.intake)
    if body.submit:
        errs = {k: "This is needed to send your request." for k in REQUIRED_TO_SUBMIT[1:] if not intake.get(k)}
        if not prov:
            errs["provider_id"] = "Choose who you want to see."
        if errs:
            raise FieldError(errs, "A few details are needed before you can send this request.")
    if a is None:
        a = Appointment(patient_id=p.id)
        db.add(a)
    a.provider_id = prov.id if prov else None
    a.intake_json = json.dumps(intake)
    if body.submit:
        a.status, a.submitted_at, a.contact_json = "requested", utcnow(), json.dumps(_contact_snapshot(p))
    else:
        a.status = "draft"
    db.flush()
    if body.submit:
        _notify_staff(db, a, p, prov)
        notify(db, recipient_type="patient", recipient_id=p.id, kind="appointment_update", title=f"Request sent to {prov.name}",
               body="We'll tell you when the clinic responds.", link="#/appointments")
    log_event(db, actor_type="patient", actor_id=p.id, patient_id=p.id, action="appointment_submitted" if body.submit else "appointment_draft_saved",
              resource_type="appointment", resource_id=a.id)
    db.commit()
    return a


def _owned(db, p, aid) -> Appointment:
    a = db.get(Appointment, aid)
    if not a or a.patient_id != p.id:
        raise HTTPException(404, "Not found.")
    return a


@router.get("/providers")
def list_providers(q: str = "", _p: Patient = Depends(current_patient), db: Session = Depends(get_db)):
    rows = db.scalars(select(Provider).order_by(Provider.name)).all()
    q = q.strip().lower()
    items = [r.to_dict() for r in rows if not q or q in (r.name or "").lower() or q in (r.specialty or "").lower()]
    return {"items": items, "estimate_note": "Availability and prices below are MOCK examples for the demo. Real costs depend on your plan, "
                                            "and the final price can be higher or lower than any estimate."}


@router.get("/appointments/form")
def appointment_form(p: Patient = Depends(current_patient), db: Session = Depends(get_db)):
    provs = [{"id": r.id, "name": r.name, "specialty": r.specialty} for r in db.scalars(select(Provider).order_by(Provider.name))]
    return {"fields": INTAKE_FIELDS, "providers": provs, "prefill": _contact_snapshot(p),
            "missing_profile": [k for k in ("phone", "dob") if not getattr(p, k)]}


class HintIn(BaseModel):
    reason: str = ""
    symptoms: str = ""


@router.post("/appointments/route-hint")
def hint(body: HintIn, _p: Patient = Depends(current_patient)):
    return route_hint(body.reason[:1000], body.symptoms[:2000])


@router.get("/appointments")
def list_appointments(p: Patient = Depends(current_patient), db: Session = Depends(get_db)):
    rows = db.scalars(select(Appointment).where(Appointment.patient_id == p.id).order_by(Appointment.updated_at.desc())).all()
    provs = {x.id: x for x in db.scalars(select(Provider))}
    return {"items": [appt_dict(a, provs.get(a.provider_id)) for a in rows]}


@router.post("/appointments", status_code=201)
def create_appointment(body: ApptIn, p: Patient = Depends(current_patient), db: Session = Depends(get_db)):
    a = _save(db, p, None, body)
    return appt_dict(a, db.get(Provider, a.provider_id) if a.provider_id else None)


@router.put("/appointments/{aid}")
def update_appointment(aid: str, body: ApptIn, p: Patient = Depends(current_patient), db: Session = Depends(get_db)):
    a = _owned(db, p, aid)
    if a.status != "draft":
        raise HTTPException(409, "This request was already sent. Cancel it and make a new one if details changed.")
    a = _save(db, p, a, body)
    return appt_dict(a, db.get(Provider, a.provider_id) if a.provider_id else None)


@router.post("/appointments/{aid}/cancel")
def cancel_appointment(aid: str, p: Patient = Depends(current_patient), db: Session = Depends(get_db)):
    a = _owned(db, p, aid)
    if a.status not in ("requested", "booked", "rescheduled"):
        raise HTTPException(409, "Only sent requests can be cancelled.")
    a.status = "cancelled"
    prov = db.get(Provider, a.provider_id) if a.provider_id else None
    log_event(db, actor_type="patient", actor_id=p.id, patient_id=p.id, action="appointment_cancelled", resource_type="appointment", resource_id=a.id)
    try:
        from app.models.staff import StaffUser  # type: ignore
        for s in db.scalars(select(StaffUser).where(StaffUser.provider_id == a.provider_id)):
            notify(db, recipient_type="staff", recipient_id=s.id, kind="appointment_request", title=f"{p.display_name} cancelled an appointment request", link="#/appointments")
    except Exception:
        pass
    db.commit()
    return appt_dict(a, prov)


@router.delete("/appointments/{aid}")
def delete_draft(aid: str, p: Patient = Depends(current_patient), db: Session = Depends(get_db)):
    a = _owned(db, p, aid)
    if a.status != "draft":
        raise HTTPException(409, "Only drafts can be deleted. Cancel a sent request instead.")
    db.delete(a)
    db.commit()
    return {"ok": True}


# ---------------- dashboard ----------------

def profile_gaps(p: Patient, db: Session) -> list[dict]:
    gaps = []
    if not p.phone:
        gaps.append({"key": "phone", "message": "Add a phone number so clinics can reach you.", "link": "#/profile"})
    if not p.address:
        gaps.append({"key": "address", "message": "Add your home address.", "link": "#/profile"})
    if p.verification_status != "verified":
        gaps.append({"key": "identity", "message": "Your identity is not verified yet. Staff can verify it at a visit.", "link": "#/profile"})
    recs = db.scalars(select(Record).where(Record.patient_id == p.id)).all()
    cats = {r.category for r in recs}
    if "emergency_contact" not in cats:
        gaps.append({"key": "contact", "message": "Add an emergency contact.", "link": "#/contacts"})
    elif not any(r.data.get("is_primary") for r in recs if r.category == "emergency_contact"):
        gaps.append({"key": "primary_contact", "message": "Mark one emergency contact as primary.", "link": "#/contacts"})
    if p.allergy_status == "unknown":
        gaps.append({"key": "allergies", "message": "Allergies: say what you know - or choose 'No known allergies'.", "link": "#/allergies"})
    if "insurance" not in cats:
        gaps.append({"key": "insurance", "message": "Add your insurance plan (optional).", "link": "#/insurance"})
    else:
        today = date.today().isoformat()
        if not any(r.category == "insurance" and (not r.data.get("end_date") or r.data["end_date"] >= today) for r in recs):
            gaps.append({"key": "insurance_expired", "message": "All your insurance plans have ended. Update your coverage.", "link": "#/insurance"})
    if p.updated_at < utcnow() - timedelta(days=365):
        gaps.append({"key": "outdated", "message": "Your profile hasn't been updated in over a year. Please review it.", "link": "#/profile"})
    return gaps


@router.get("/dashboard")
def dashboard(p: Patient = Depends(current_patient), db: Session = Depends(get_db)):
    provs = {x.id: x for x in db.scalars(select(Provider))}
    appts = db.scalars(select(Appointment).where(Appointment.patient_id == p.id).order_by(Appointment.updated_at.desc())).all()
    upcoming = [appt_dict(a, provs.get(a.provider_id)) for a in appts if a.status in ("requested", "booked", "rescheduled")]
    drafts = [appt_dict(a, provs.get(a.provider_id)) for a in appts if a.status == "draft"]
    recs = db.scalars(select(Record).where(Record.patient_id == p.id).order_by(Record.created_at.desc())).all()
    results = sorted([r for r in recs if r.category == "result"], key=lambda r: r.data.get("result_date", ""), reverse=True)[:5]
    recent = [rec_dict(r) for r in recs if r.category not in ("bill", "eob")][:6]
    bills = []
    eobs = {r.id: r for r in recs if r.category == "eob"}
    for r in recs:
        if r.category != "bill":
            continue
        d = rec_dict(r)
        if d["balance"] > 0 and r.data.get("status") != "paid":
            e = eobs.get(r.data.get("claim_id"))
            bills.append({"id": r.id, "provider_name": r.data.get("provider_name"), "balance": d["balance"], "due_date": r.data.get("due_date"),
                          "overdue": d["overdue"], "status": r.data.get("status"), "claim_status": e.data.get("claim_status") if e else None,
                          "has_claim": bool(e)})
    bills.sort(key=lambda b: b["due_date"] or "9999")
    return {"upcoming_appointments": upcoming, "incomplete_intake": drafts,
            "recent_results": [rec_dict(r) for r in results], "recent_records": recent,
            "bills": bills, "profile_gaps": profile_gaps(p, db),
            "verification_status": p.verification_status, "display_name": p.display_name}
