"""Appointment service (W9).  The ONLY place appointment status rules live.

Used by: W9 patient/caregiver routers, W3's staff inbox, W1's dashboard, seed code.

Conventions
* Functions ``flush`` but never ``commit`` - the CALLER commits, so appointment + history + audit +
  notification are ONE transaction (commit or roll back together).
* Errors: ``ApptNotFound`` (LookupError), ``InvalidTransition`` (ValueError), ``PermissionError`` (staff of
  another organization), ``app.errors.FieldError`` (validation, carries per-field messages).
* No AI triage, no diagnosis: free text is stored and shown exactly as the patient wrote it.

Public API (stable - W3/W1 code against this):
    get(db, appt_id) -> Appointment | None
    list_for_provider(db, provider_id, status=None, limit=200) -> list[Appointment]   (never returns drafts)
    set_status(db, appt_id, status, new_time=None, staff_id=None, note=None) -> Appointment
    create(db, *, patient_id, provider_id, intake, submit=False, ...) -> Appointment
    update(db, appt, *, provider_id=..., intake=..., ...) -> Appointment            (drafts only)
    submit(db, appt, ...) / request_reschedule(db, appt, ...) / confirm_proposed_time(db, appt, ...) / cancel(db, appt, ...)
    get_dashboard_summary(db, patient_id) -> dict
"""
from __future__ import annotations

import json
import re
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.errors import FieldError
from app.models import medical as med
from app.models.appointments import Appointment, AppointmentEvent, CaregiverAccount, DependentProfile
from app.models.shared import Patient, Provider, utcnow
from app.models.staff import StaffUser
from app.services.audit import log_event
from app.services.notif_helpers import notify_pref, notify_provider_staff

NOT_A_DIAGNOSIS = ("This form is not a diagnosis and the app does not triage emergencies. "
                   "If you think this is an emergency, call your local emergency services now.")

STATUSES = ("draft", "requested", "booked", "rescheduled", "cancelled", "declined")
ACTIVE_STATUSES = ("requested", "booked", "rescheduled")
TERMINAL = ("cancelled", "declined")

# from -> allowed targets.  "requested" from booked/rescheduled = patient asked to move it.
TRANSITIONS: dict[str, set[str]] = {
    "draft": {"requested", "cancelled"},
    "requested": {"booked", "rescheduled", "declined", "cancelled"},
    "booked": {"rescheduled", "cancelled", "requested"},
    "rescheduled": {"booked", "rescheduled", "cancelled", "declined", "requested"},
    "cancelled": set(),
    "declined": set(),
}
STAFF_TARGETS = {"booked", "rescheduled", "declined", "cancelled"}

VISIT_TYPES = ("in_person", "video", "phone")
INTAKE_KEYS = ("reason", "symptoms", "onset", "duration", "availability", "visit_type", "accommodations",
               "preferred_slot")
MAX_LEN = {"reason": 1000, "symptoms": 2000, "onset": 200, "duration": 200, "availability": 500,
           "accommodations": 1000, "preferred_slot": 200}
REQUIRED_TO_SUBMIT = ("provider_id", "reason", "availability", "visit_type")

INTAKE_FIELDS = [
    {"name": "reason", "label": "Why do you want to be seen? (in your own words)", "type": "textarea", "required": True,
     "maxlength": MAX_LEN["reason"]},
    {"name": "symptoms", "label": "Symptoms, in your own words", "type": "textarea", "required": False,
     "maxlength": MAX_LEN["symptoms"],
     "help": "Describe what you notice. You do not need medical words. Leave empty if there are none."},
    {"name": "onset", "label": "When did it start?", "type": "text", "required": False, "maxlength": MAX_LEN["onset"],
     "help": "Optional. For example: about 3 days ago, or last March."},
    {"name": "duration", "label": "How long does it last, or how often does it happen?", "type": "text",
     "required": False, "maxlength": MAX_LEN["duration"], "help": "For example: constant, or a few minutes twice a day."},
    {"name": "availability", "label": "When are you available?", "type": "textarea", "required": True,
     "maxlength": MAX_LEN["availability"], "help": "For example: weekday mornings, not Wednesdays."},
    {"name": "visit_type", "label": "Type of visit", "type": "select", "required": True,
     "options": [{"value": "in_person", "label": "In person"}, {"value": "video", "label": "Video visit"},
                 {"value": "phone", "label": "Phone call"}]},
    {"name": "accommodations", "label": "Anything we should prepare for you?", "type": "textarea", "required": False,
     "maxlength": MAX_LEN["accommodations"],
     "help": "For example: wheelchair access, large-print forms, an interpreter, someone coming with you."},
]


class ApptNotFound(LookupError):
    pass


class InvalidTransition(ValueError):
    pass


# ----------------------------------------------------------------------------- helpers

_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _clean(s) -> str:
    return _CTRL.sub("", str(s)).strip()


def validate_intake(raw: dict | None) -> dict:
    """Clean + length-check the intake text.  Required-ness is only enforced at submit time."""
    raw = raw or {}
    out: dict = {}
    errs: dict[str, str] = {}
    for key in INTAKE_KEYS:
        v = raw.get(key)
        if v is None or _clean(v) == "":
            continue
        v = _clean(v)
        if key == "visit_type":
            if v not in VISIT_TYPES:
                errs[key] = "Choose in person, video visit or phone call."
                continue
        elif len(v) > MAX_LEN[key]:
            errs[key] = f"Please keep this under {MAX_LEN[key]} characters."
            continue
        out[key] = v
    if errs:
        raise FieldError(errs)
    return out


def missing_for_submit(provider_id: str | None, intake: dict) -> list[str]:
    # "When did it start?" is labelled optional, so it never blocks sending (even with symptoms).
    return [k for k in REQUIRED_TO_SUBMIT if not (provider_id if k == "provider_id" else intake.get(k))]


_MISS_LABEL = {"provider_id": "who you want to see", "reason": "why you want to be seen", "availability": "when you are available",
               "visit_type": "the type of visit", "onset": "when the symptoms started"}


def intake_progress(a: Appointment) -> dict:
    """How complete a draft is: {percent, missing:[keys], missing_labels:[text]}."""
    miss = missing_for_submit(a.provider_id, a.intake)
    total = len(REQUIRED_TO_SUBMIT)
    done = max(total - len(miss), 0)
    return {"percent": int(round(100 * done / total)) if total else 100, "missing": miss,
            "missing_labels": [_MISS_LABEL.get(m, m) for m in miss]}


def get(db: Session, appt_id: str) -> Appointment | None:
    return db.get(Appointment, appt_id)


def _require(db: Session, appt_id: str) -> Appointment:
    a = db.get(Appointment, appt_id)
    if a is None:
        raise ApptNotFound(appt_id)
    return a


def list_for_provider(db: Session, provider_id: str | None, status: str | None = None, limit: int = 200) -> list[Appointment]:
    """Inbox query for one organization.  Drafts are private to the patient and never returned."""
    if not provider_id:
        return []
    q = select(Appointment).where(Appointment.provider_id == provider_id, Appointment.status != "draft")
    if status:
        q = q.where(Appointment.status == status)
    q = q.order_by(Appointment.submitted_at.desc().nulls_last(), Appointment.created_at.desc()).limit(limit)
    return list(db.scalars(q))


def list_for_patient(db: Session, patient_id: str, statuses: tuple[str, ...] | None = None) -> list[Appointment]:
    q = select(Appointment).where(Appointment.patient_id == patient_id)
    if statuses:
        q = q.where(Appointment.status.in_(statuses))
    return list(db.scalars(q.order_by(Appointment.updated_at.desc())))


def guardian_of(db: Session, patient_id: str) -> str | None:
    """If ``patient_id`` is a dependent profile, the guardian's patient id (they receive its notifications)."""
    return db.scalar(select(DependentProfile.guardian_patient_id).where(
        DependentProfile.dependent_patient_id == patient_id, DependentProfile.ended_at.is_(None)))


def _patient_recipient(db: Session, patient_id: str) -> str:
    return guardian_of(db, patient_id) or patient_id


def _who(db: Session, patient_id: str) -> str:
    p = db.get(Patient, patient_id)
    return p.display_name if p else "A patient"


def _notify_patient(db: Session, a: Appointment, title: str, body: str = "") -> None:
    rid = _patient_recipient(db, a.patient_id)
    if rid != a.patient_id:  # dependent: say whose appointment it is
        title = f"{_who(db, a.patient_id)}: {title}"
    _send(db, recipient_type="patient", recipient_id=rid, kind="appointment_update", title=title, body=body,
          link="#/appointments")


def _send(db: Session, **kw) -> None:
    """Notification honoring the recipient's per-kind preferences."""
    notify_pref(db, **kw)


def notify_staff(db: Session, provider_id: str | None, kind: str, title: str, body: str = "", link: str = "#/appointments") -> int:
    """Notify every active clinical staff user of the provider. Returns how many notifications were created."""
    return notify_provider_staff(db, provider_id=provider_id, kind=kind, title=title, body=body, link=link)


def _event(db: Session, a: Appointment, frm: str | None, to: str, actor_type: str, actor_id: str | None, note: str | None = None):
    db.add(AppointmentEvent(appointment_id=a.id, from_status=frm, to_status=to, actor_type=actor_type,
                            actor_id=actor_id, note=(note or None) and note[:1000]))


def _audit(db: Session, a: Appointment, action: str, actor_type: str, actor_id: str | None, detail: dict | None = None):
    # audit_log.actor_type only allows patient|staff|system, so caregiver actions are recorded as 'system'
    # with detail.acting_as='caregiver' (see CONTRACT Requests).
    d = dict(detail or {})
    if actor_type == "caregiver":
        d["acting_as"] = "caregiver"
        d["caregiver_id"] = actor_id
        acct = db.get(CaregiverAccount, actor_id) if actor_id else None
        d["caregiver_email"] = acct.email if acct else None
        action = "caregiver_" + action
        actor_type = "system"
    log_event(db, actor_type=actor_type, actor_id=actor_id, patient_id=a.patient_id, action=action,
              resource_type="appointment", resource_id=a.id, detail=d or None)


def _check_transition(a: Appointment, target: str) -> None:
    if target not in TRANSITIONS.get(a.status, set()):
        raise InvalidTransition(f"An appointment that is {a.status} cannot become {target}.")


def _move(db: Session, a: Appointment, target: str, actor_type: str, actor_id: str | None, note: str | None = None) -> None:
    _check_transition(a, target)
    frm = a.status
    a.status = target
    a.updated_at = utcnow()
    _event(db, a, frm, target, actor_type, actor_id, note)


# ----------------------------------------------------------------------------- prefill

def medical_summary(db: Session, patient_id: str) -> dict:
    """Names only (no clinical advice) of active medications, allergies and conditions, read from W7's tables."""
    out: dict = {"medications": [], "allergies": [], "conditions": [], "allergy_status": None}
    p = db.get(Patient, patient_id)
    if p is not None:
        out["allergy_status"] = getattr(p, "allergy_status", None)
    for r in db.scalars(select(med.MedMedication).where(med.MedMedication.patient_id == patient_id,
                                                        med.MedMedication.status == "active")):
        out["medications"].append(r.name)
    for r in db.scalars(select(med.MedAllergy).where(med.MedAllergy.patient_id == patient_id,
                                                     med.MedAllergy.status == "active")):
        out["allergies"].append(r.substance)
    for r in db.scalars(select(med.MedHistory).where(med.MedHistory.patient_id == patient_id,
                                                     med.MedHistory.kind == "condition",
                                                     med.MedHistory.status != "resolved")):
        out["conditions"].append(r.title)
    return out


def profile_snapshot(p: Patient) -> dict:
    return {"patient_id": p.id, "legal_name": p.legal_name, "preferred_name": p.preferred_name, "dob": p.dob,
            "phone": p.phone, "email": p.email if "@dependents.invalid" not in (p.email or "") else None,
            "address": p.address, "pronouns": p.pronouns}


def prefill(db: Session, patient_id: str, *, actor_patient_id: str | None = None) -> dict:
    """What the intake form is prefilled with.  For a dependent, contact details of the guardian are added."""
    p = db.get(Patient, patient_id)
    if p is None:
        raise ApptNotFound(patient_id)
    snap = profile_snapshot(p)
    gid = guardian_of(db, patient_id)
    if gid:
        g = db.get(Patient, gid)
        if g:
            snap["guardian_name"] = g.legal_name
            snap["guardian_phone"] = g.phone
            snap["guardian_email"] = g.email
    missing = [k for k in ("phone", "dob") if not snap.get(k) and not (gid and k == "phone")]
    return {"profile": snap, "medical_summary": medical_summary(db, patient_id), "missing_profile": missing,
            "is_dependent": bool(gid)}


# ----------------------------------------------------------------------------- serialization

def _history(db: Session, appt_id: str) -> list[dict]:
    return [{"ts": e.ts.isoformat() + "Z", "actor_type": e.actor_type, "from": e.from_status, "to": e.to_status,
             "note": e.note} for e in db.scalars(select(AppointmentEvent).where(
                 AppointmentEvent.appointment_id == appt_id).order_by(AppointmentEvent.ts, AppointmentEvent.id))]


def to_dict(db: Session, a: Appointment, *, provider: Provider | None = None, history: bool = False) -> dict:
    prov = provider or (db.get(Provider, a.provider_id) if a.provider_id else None)
    prog = intake_progress(a) if a.status == "draft" else {"percent": 100, "missing": [], "missing_labels": []}
    d = {
        "id": a.id, "patient_id": a.patient_id, "patient_name": _who(db, a.patient_id), "status": a.status,
        "provider_name": prov.name if prov else None,
        "provider": {"id": prov.id, "name": prov.name, "specialty": prov.specialty} if prov else None,
        "intake": a.intake, "contact": a.contact, "scheduled_for": a.scheduled_for, "staff_note": a.staff_note,
        "submitted_at": a.submitted_at.isoformat() + "Z" if a.submitted_at else None,
        "created_at": a.created_at.isoformat() + "Z" if a.created_at else None,
        "updated_at": a.updated_at.isoformat() + "Z" if a.updated_at else None,
        "progress": prog, "is_draft": a.status == "draft",
        "can_cancel": a.status in ("draft", "requested", "booked", "rescheduled"),
        "can_reschedule": a.status in ("booked", "rescheduled"),
        "needs_my_confirmation": a.status == "rescheduled",
        "disclaimer": NOT_A_DIAGNOSIS,
    }
    if history:
        d["history"] = _history(db, a.id)
    return d


def review_summary(db: Session, a: Appointment, *, include_medical: bool = False) -> dict:
    """Everything that WILL be sent to the clinic, shown to the patient before they press Send."""
    pf = prefill(db, a.patient_id)
    prov = db.get(Provider, a.provider_id) if a.provider_id else None
    sections = [{"label": f["label"], "key": f["name"], "value": a.intake.get(f["name"], "")} for f in INTAKE_FIELDS]
    return {"appointment_id": a.id, "to": prov.name if prov else None, "sections": sections,
            "contact_shared": {k: v for k, v in pf["profile"].items() if k != "patient_id" and v},
            "medical_summary": pf["medical_summary"] if include_medical else None,
            "medical_summary_available": pf["medical_summary"], "missing": intake_progress(a)["missing_labels"],
            "ready": not intake_progress(a)["missing"], "disclaimer": NOT_A_DIAGNOSIS}


# ----------------------------------------------------------------------------- patient-side operations

def _snapshot_for_submit(db: Session, a: Appointment, include_medical: bool) -> dict:
    pf = prefill(db, a.patient_id)
    snap = dict(pf["profile"])
    if include_medical:
        snap["medical_summary"] = pf["medical_summary"]
    return snap


def create(db: Session, *, patient_id: str, provider_id: str | None, intake: dict | None = None, submit: bool = False,
           include_medical: bool = False, actor_type: str = "patient", actor_id: str | None = None) -> Appointment:
    if provider_id is not None and db.get(Provider, provider_id) is None:
        raise FieldError({"provider_id": "Choose a provider from the list."})
    if db.get(Patient, patient_id) is None:
        raise ApptNotFound(patient_id)
    a = Appointment(patient_id=patient_id, provider_id=provider_id, status="draft", intake_json=json.dumps(validate_intake(intake)))
    db.add(a)
    db.flush()
    _event(db, a, None, "draft", actor_type, actor_id or patient_id)
    _audit(db, a, "appointment_draft_saved", actor_type, actor_id or patient_id)
    if submit:
        submit_request(db, a, include_medical=include_medical, actor_type=actor_type, actor_id=actor_id or patient_id)
    return a


def update(db: Session, a: Appointment, *, provider_id: str | None = ..., intake: dict | None = None,
           actor_type: str = "patient", actor_id: str | None = None) -> Appointment:
    if a.status != "draft":
        raise InvalidTransition("This request was already sent. Cancel it or ask to reschedule instead.")
    if provider_id is not ...:
        if provider_id is not None and db.get(Provider, provider_id) is None:
            raise FieldError({"provider_id": "Choose a provider from the list."})
        a.provider_id = provider_id
    if intake is not None:
        merged = {**a.intake, **validate_intake(intake)}
        for k in INTAKE_KEYS:  # explicit empty string clears a field
            if k in intake and _clean(intake.get(k) or "") == "":
                merged.pop(k, None)
        a.intake_json = json.dumps(merged)
    a.updated_at = utcnow()
    db.flush()
    _audit(db, a, "appointment_draft_saved", actor_type, actor_id or a.patient_id)
    return a


def submit_request(db: Session, a: Appointment, *, include_medical: bool = False, actor_type: str = "patient",
                   actor_id: str | None = None) -> Appointment:
    if a.status != "draft":
        raise InvalidTransition("This request was already sent.")
    miss = missing_for_submit(a.provider_id, a.intake)
    if miss:
        raise FieldError({m: "This is needed to send your request." for m in miss},
                         "A few details are needed before you can send this request.")
    a.contact_json = json.dumps(_snapshot_for_submit(db, a, include_medical))
    a.submitted_at = utcnow()
    _move(db, a, "requested", actor_type, actor_id or a.patient_id)
    prov = db.get(Provider, a.provider_id)
    who = _who(db, a.patient_id)
    notify_staff(db, a.provider_id, "appointment_request", f"New appointment request from {who}",
                 (a.intake.get("reason") or "")[:140], link=f"#/appointments/{a.id}")
    notify_staff(db, a.provider_id, "intake_received", f"Intake form received from {who}",
                 "Open the appointment to read the intake answers.", link=f"#/appointments/{a.id}")
    _notify_patient(db, a, f"Request sent to {prov.name}", "We will tell you when the clinic responds.")
    _audit(db, a, "appointment_submitted", actor_type, actor_id or a.patient_id, {"provider": prov.name})
    db.flush()
    return a


def request_reschedule(db: Session, a: Appointment, *, preferred_times: str, reason: str | None = None,
                       actor_type: str = "patient", actor_id: str | None = None) -> Appointment:
    """Patient asks to move a booked/proposed appointment.  It goes back to 'requested' for the clinic."""
    preferred_times = _clean(preferred_times or "")
    if not preferred_times:
        raise FieldError({"preferred_times": "Tell us when would suit you better."})
    if len(preferred_times) > 500:
        raise FieldError({"preferred_times": "Please keep this under 500 characters."})
    intake = a.intake
    intake["availability"] = preferred_times
    intake["reschedule_from"] = a.scheduled_for
    if reason:
        intake["reschedule_reason"] = _clean(reason)[:500]
    _move(db, a, "requested", actor_type, actor_id or a.patient_id, f"Reschedule requested: {preferred_times}")
    a.intake_json = json.dumps(intake)
    a.submitted_at = utcnow()
    prov = db.get(Provider, a.provider_id) if a.provider_id else None
    notify_staff(db, a.provider_id, "appointment_request", f"{_who(db, a.patient_id)} asked to reschedule",
                 preferred_times[:140], link=f"#/appointments/{a.id}")
    _notify_patient(db, a, "Reschedule request sent", f"{prov.name + ' will' if prov else 'The clinic will'} suggest a new time.")
    _audit(db, a, "appointment_reschedule_requested", actor_type, actor_id or a.patient_id)
    db.flush()
    return a


def confirm_proposed_time(db: Session, a: Appointment, *, actor_type: str = "patient", actor_id: str | None = None) -> Appointment:
    if a.status != "rescheduled" or not a.scheduled_for:
        raise InvalidTransition("There is no proposed time to confirm.")
    _move(db, a, "booked", actor_type, actor_id or a.patient_id, "Patient accepted the proposed time")
    notify_staff(db, a.provider_id, "appointment_request", f"{_who(db, a.patient_id)} accepted the new time",
                 a.scheduled_for or "", link=f"#/appointments/{a.id}")
    _audit(db, a, "appointment_time_confirmed", actor_type, actor_id or a.patient_id, {"scheduled_for": a.scheduled_for})
    db.flush()
    return a


def cancel(db: Session, a: Appointment, *, reason: str | None = None, actor_type: str = "patient",
           actor_id: str | None = None) -> Appointment:
    was = a.status
    _move(db, a, "cancelled", actor_type, actor_id or a.patient_id, reason)
    if was != "draft":
        notify_staff(db, a.provider_id, "appointment_request", f"{_who(db, a.patient_id)} cancelled an appointment",
                     (reason or "")[:140], link=f"#/appointments/{a.id}")
        _notify_patient(db, a, "Appointment cancelled", "You cancelled this appointment.")
    _audit(db, a, "appointment_cancelled", actor_type, actor_id or a.patient_id)
    db.flush()
    return a


# ----------------------------------------------------------------------------- staff-side operation (W3)

def _check_time(t: str | None) -> str | None:
    t = _clean(t or "") or None
    if t and len(t) > 60:
        raise FieldError({"scheduled_for": "Please keep the date and time under 60 characters."})
    return t


def set_status(db: Session, appt_id: str, status: str, new_time: str | None = None, staff_id: str | None = None,
               note: str | None = None) -> Appointment:
    """Clinic decision: ``booked`` (accept), ``rescheduled`` (propose ``new_time``), ``declined`` or ``cancelled``.

    * ``new_time`` is required for ``rescheduled`` and for ``booked`` unless a time is already set.
    * ``staff_id`` (a StaffUser id) is checked: must be active and belong to the appointment's provider,
      otherwise ``PermissionError``.  Staff may not set draft/requested.
    * Writes a history event, an audit entry (actor staff) and an ``appointment_update`` notification to the
      patient (guardian for dependents), all in the caller's transaction.
    """
    a = _require(db, appt_id)
    if status not in STATUSES:
        raise InvalidTransition(f"Unknown status '{status}'.")
    actor_type, actor_id = "system", None
    if staff_id:
        s = db.get(StaffUser, staff_id)
        if s is None or not s.active or not a.provider_id or s.provider_id != a.provider_id or a.status == "draft":
            raise PermissionError("This appointment belongs to another organization.")
        if status not in STAFF_TARGETS:
            raise InvalidTransition("Staff can book, reschedule, decline or cancel an appointment.")
        actor_type, actor_id = "staff", staff_id
    when = _check_time(new_time)
    if status == "rescheduled" and not when:
        raise FieldError({"scheduled_for": "Please enter the proposed date and time."})
    if status == "booked" and not (when or a.scheduled_for):
        raise FieldError({"scheduled_for": "Please enter the appointment date and time."})
    _move(db, a, status, actor_type, actor_id, note)
    if when and status in ("booked", "rescheduled"):
        a.scheduled_for = when
    if note:
        a.staff_note = _clean(note)[:1000]
    prov = db.get(Provider, a.provider_id) if a.provider_id else None
    pname = prov.name if prov else "The clinic"
    title = {"booked": f"{pname} confirmed your appointment", "rescheduled": f"{pname} proposed a new time",
             "declined": f"{pname} could not take this appointment request", "cancelled": f"{pname} cancelled your appointment",
             "requested": "Appointment updated"}.get(status, "Appointment updated")
    body = (f"Time: {a.scheduled_for}. " if a.scheduled_for and status in ("booked", "rescheduled") else "") + (a.staff_note or "")
    _notify_patient(db, a, title, body.strip())
    _audit(db, a, f"appointment_{status}", actor_type, actor_id,
           {"status": status, "scheduled_for": a.scheduled_for, "provider": pname})
    db.flush()
    return a


# ----------------------------------------------------------------------------- dashboard (W1)

def dependents_of(db: Session, guardian_id: str) -> list[DependentProfile]:
    return list(db.scalars(select(DependentProfile).where(DependentProfile.guardian_patient_id == guardian_id,
                                                          DependentProfile.ended_at.is_(None))))


def get_dashboard_summary(db: Session, patient_id: str) -> dict:
    """For W1's dashboard.  Includes appointments of the patient's dependents (flagged with ``for_name``).

    {"upcoming": [...], "next_appointment": {...}|None, "needs_confirmation": [...],
     "incomplete_intake": [{id, provider, percent, missing_labels, for_name, link}], "counts": {...}}
    """
    ids = [patient_id] + [d.dependent_patient_id for d in dependents_of(db, patient_id)]
    provs = {p.id: p for p in db.scalars(select(Provider))}
    rows = list(db.scalars(select(Appointment).where(Appointment.patient_id.in_(ids)).order_by(Appointment.updated_at.desc())))
    upcoming, incomplete, confirm = [], [], []
    for a in rows:
        d = to_dict(db, a, provider=provs.get(a.provider_id))
        d["for_name"] = d["patient_name"] if a.patient_id != patient_id else None
        if a.status in ACTIVE_STATUSES:
            upcoming.append(d)
            if a.status == "rescheduled":
                confirm.append(d)
        elif a.status == "draft":
            incomplete.append({"id": a.id, "provider": d["provider"], "percent": d["progress"]["percent"],
                               "missing_labels": d["progress"]["missing_labels"], "for_name": d["for_name"],
                               "link": f"#/appointments/{a.id}"})
    upcoming.sort(key=lambda x: (x["status"] != "booked", x["scheduled_for"] or "9999"))
    booked = [u for u in upcoming if u["status"] == "booked" and u["scheduled_for"]]
    return {"upcoming": upcoming, "next_appointment": (sorted(booked, key=lambda x: x["scheduled_for"])[0] if booked else None),
            "needs_confirmation": confirm, "incomplete_intake": incomplete,
            "counts": {"upcoming": len(upcoming), "incomplete_intake": len(incomplete),
                       "awaiting_clinic": sum(1 for u in upcoming if u["status"] == "requested"),
                       "needs_confirmation": len(confirm)}}


def purge_old_drafts(db: Session, days: int = 90) -> int:
    """Housekeeping: drafts nobody touched for ``days`` days (drafts are private, never seen by staff)."""
    cutoff = utcnow() - timedelta(days=days)
    n = 0
    for a in db.scalars(select(Appointment).where(Appointment.status == "draft", Appointment.updated_at < cutoff)):
        db.delete(a)
        n += 1
    return n
