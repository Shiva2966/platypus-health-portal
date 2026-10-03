"""Identity-verification status helper for staff code (W3). The patient cannot set 'verified' themselves."""
from app.models.shared import Patient
from app.services.audit import log_event
from app.services.notifications import notify


def set_verification(db, *, patient_id: str, status: str, staff_id: str | None, note: str | None = None) -> Patient:
    """status: 'verified' | 'unverified' | 'pending'. Logs an audit event and tells the patient. Caller commits."""
    if status not in ("verified", "unverified", "pending"):
        raise ValueError("invalid verification status")
    p = db.get(Patient, patient_id)
    if p is None:
        raise LookupError("patient not found")
    p.verification_status = status
    p.verified_note = note or ("Identity checked in person by clinic staff." if status == "verified" else None)
    log_event(db, actor_type="staff", actor_id=staff_id, patient_id=p.id, action=f"identity_{status}", resource_type="profile")
    notify(db, recipient_type="patient", recipient_id=p.id, kind="info_outdated" if status != "verified" else "appointment_update",
           title="Your identity was verified" if status == "verified" else "Identity verification status changed",
           body=p.verified_note or "", link="#/profile")
    return p
