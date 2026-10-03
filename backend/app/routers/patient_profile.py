"""Patient profile, identity-verification status, emergency contacts, /api/auth/me."""
from datetime import date

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import get_db
from app.errors import FieldError
from app.models.core import EmergencyContact
from app.models.shared import Patient
from app.security import current_patient
from app.services.audit import log_event
from app.validators import F, clean_value

router = APIRouter(prefix="/api")

PROFILE_FIELDS = [
    F("legal_name", "Legal name", required=True), F("preferred_name", "Preferred name", help="What should we call you?"),
    F("dob", "Date of birth", "date", True), F("address", "Home address", "textarea", maxlength=500),
    F("phone", "Phone", "tel"),
    F("pronouns", "Pronouns", help="Optional, e.g. she/her, he/him, they/them."),
]

VERIFICATION_TEXT = {
    "unverified": "Not verified yet. Staff can verify your identity with a photo ID at a visit.",
    "pending": "Verification requested. Bring a photo ID to your next visit.",
    "verified": "Identity verified by a clinic.",
}


def patient_dict(p: Patient) -> dict:
    return {"id": p.id, "legal_name": p.legal_name, "preferred_name": p.preferred_name, "display_name": p.display_name,
            "dob": p.dob, "email": p.email, "phone": p.phone, "address": p.address, "pronouns": p.pronouns,
            "verification_status": p.verification_status, "verification_text": VERIFICATION_TEXT.get(p.verification_status),
            "verified_note": p.verified_note, "allergy_status": p.allergy_status, "created_at": p.created_at, "updated_at": p.updated_at}


@router.get("/auth/me")
def me(p: Patient = Depends(current_patient)):
    return {"id": p.id, "display_name": p.display_name, "email": p.email, "role": "patient",
            "verification_status": p.verification_status}


@router.get("/profile")
def get_profile(p: Patient = Depends(current_patient)):
    return {"profile": patient_dict(p), "fields": PROFILE_FIELDS}


class ProfileIn(BaseModel):
    legal_name: str | None = None
    preferred_name: str | None = None
    dob: str | None = None
    address: str | None = None
    phone: str | None = None
    email: str | None = None
    pronouns: str | None = None


@router.put("/profile")
def put_profile(body: ProfileIn, p: Patient = Depends(current_patient), db: Session = Depends(get_db)):
    if body.email and body.email.strip().lower() != p.email.lower():
        raise HTTPException(409, "To change your sign-in email, use Account settings > Change email "
                                 "(it is verified with a code sent to the new address).")
    data, errs = {}, {}
    raw = body.model_dump()
    for f in PROFILE_FIELDS:
        try:
            data[f["name"]] = clean_value(f, raw.get(f["name"]))
        except ValueError as e:
            errs[f["name"]] = str(e)
    if data.get("dob") and data["dob"] > date.today().isoformat():
        errs["dob"] = "Date of birth can't be in the future."
    if errs:
        raise FieldError(errs)
    identity_changed = (data["legal_name"], data["dob"]) != (p.legal_name, p.dob)
    for k, v in data.items():
        setattr(p, k, v)
    if identity_changed and p.verification_status == "verified":
        p.verification_status = "unverified"
        p.verified_note = "Name or date of birth changed - identity needs to be checked again."
    log_event(db, actor_type="patient", actor_id=p.id, patient_id=p.id, action="profile_updated", resource_type="profile")
    db.commit()
    return patient_dict(p)


@router.post("/profile/request-verification")
def request_verification(p: Patient = Depends(current_patient), db: Session = Depends(get_db)):
    """Patient asks for identity verification. Staff set 'verified' via app.services.identity.set_verification."""
    if p.verification_status == "verified":
        raise HTTPException(409, "Your identity is already verified.")
    p.verification_status = "pending"
    p.verified_note = "Bring a photo ID to your next visit so staff can verify it."
    log_event(db, actor_type="patient", actor_id=p.id, patient_id=p.id, action="verification_requested", resource_type="profile")
    db.commit()
    return patient_dict(p)


# ---------------- emergency contacts ----------------

CONTACT_FIELDS = [
    F("name", "Full name", required=True), F("relationship", "Relationship", required=True, placeholder="e.g. sister"),
    F("phone", "Phone", "tel", True), F("email", "Email", "email"),
    F("is_primary", "This is my primary emergency contact", "checkbox"),
    F("authorized_to_access_records", "Also authorized to see my health records", "checkbox",
      help="Separate from being an emergency contact. Ticking this only records your wish; to actually give someone access use Sharing or Caregivers."),
    F("notes", "Notes", maxlength=500),
]


def contact_dict(c: EmergencyContact) -> dict:
    return {"id": c.id, "name": c.name, "relationship": c.relationship_, "phone": c.phone, "email": c.email,
            "is_primary": c.is_primary, "authorized_to_access_records": c.authorized_to_access_records, "notes": c.notes,
            "created_at": c.created_at, "updated_at": c.updated_at}


def _clean_contact(payload: dict) -> dict:
    out, errs = {}, {}
    for f in CONTACT_FIELDS:
        raw = payload.get(f["name"]) if f["name"] != "relationship" else payload.get("relationship")
        try:
            out[f["name"]] = clean_value(f, raw)
        except ValueError as e:
            errs[f["name"]] = str(e)
    if errs:
        raise FieldError(errs)
    return out


def _owned_contact(db, p, cid) -> EmergencyContact:
    c = db.get(EmergencyContact, cid)
    if not c or c.patient_id != p.id:  # never reveal other patients' rows
        raise HTTPException(404, "Not found.")
    return c


def _one_primary(db, p, keep_id):
    for o in db.scalars(select(EmergencyContact).where(EmergencyContact.patient_id == p.id, EmergencyContact.id != keep_id,
                                                      EmergencyContact.is_primary.is_(True))):
        o.is_primary = False


@router.get("/emergency-contacts")
def list_contacts(p: Patient = Depends(current_patient), db: Session = Depends(get_db)):
    rows = db.scalars(select(EmergencyContact).where(EmergencyContact.patient_id == p.id)
                      .order_by(EmergencyContact.is_primary.desc(), EmergencyContact.created_at)).all()
    return {"items": [contact_dict(c) for c in rows], "fields": CONTACT_FIELDS}


@router.post("/emergency-contacts", status_code=201)
def add_contact(payload: dict, p: Patient = Depends(current_patient), db: Session = Depends(get_db)):
    d = _clean_contact(payload)
    dup = db.scalar(select(EmergencyContact.id).where(EmergencyContact.patient_id == p.id,
                                                      func.lower(EmergencyContact.name) == d["name"].lower(),
                                                      EmergencyContact.phone == d["phone"]))
    if dup:
        raise HTTPException(409, "You already have a contact with this name and phone number.")
    first = not db.scalar(select(EmergencyContact.id).where(EmergencyContact.patient_id == p.id))
    c = EmergencyContact(patient_id=p.id, name=d["name"], relationship_=d["relationship"], phone=d["phone"], email=d["email"],
                         is_primary=bool(d["is_primary"]) or first, authorized_to_access_records=bool(d["authorized_to_access_records"]),
                         notes=d["notes"])
    db.add(c)
    db.flush()
    if c.is_primary:
        _one_primary(db, p, c.id)
    log_event(db, actor_type="patient", actor_id=p.id, patient_id=p.id, action="emergency_contact_added", resource_type="emergency_contact", resource_id=c.id)
    db.commit()
    return contact_dict(c)


@router.put("/emergency-contacts/{cid}")
@router.patch("/emergency-contacts/{cid}")
def update_contact(cid: str, payload: dict, p: Patient = Depends(current_patient), db: Session = Depends(get_db)):
    c = _owned_contact(db, p, cid)
    # QA fix: keys missing from the payload keep their current value (a partial update such as {"is_primary": true}
    # used to 422, and a payload without the checkbox keys silently cleared "primary"/"authorized").
    merged = {**contact_dict(c), **{k: v for k, v in payload.items() if k in {f["name"] for f in CONTACT_FIELDS}}}
    d = _clean_contact(merged)
    c.name, c.relationship_, c.phone, c.email, c.notes = d["name"], d["relationship"], d["phone"], d["email"], d["notes"]
    c.is_primary, c.authorized_to_access_records = bool(d["is_primary"]), bool(d["authorized_to_access_records"])
    if c.is_primary:
        _one_primary(db, p, c.id)
    log_event(db, actor_type="patient", actor_id=p.id, patient_id=p.id, action="emergency_contact_updated", resource_type="emergency_contact", resource_id=c.id)
    db.commit()
    return contact_dict(c)


@router.delete("/emergency-contacts/{cid}")
def delete_contact(cid: str, p: Patient = Depends(current_patient), db: Session = Depends(get_db)):
    c = _owned_contact(db, p, cid)
    was_primary = bool(c.is_primary)
    db.delete(c)
    db.flush()
    if was_primary:  # QA fix: don't leave the patient with contacts but no primary one
        nxt = db.scalars(select(EmergencyContact).where(EmergencyContact.patient_id == p.id)
                         .order_by(EmergencyContact.created_at)).first()
        if nxt:
            nxt.is_primary = True
    log_event(db, actor_type="patient", actor_id=p.id, patient_id=p.id, action="emergency_contact_deleted", resource_type="emergency_contact", resource_id=cid)
    db.commit()
    return {"ok": True}


@router.post("/emergency-contacts/{cid}/primary")
def make_primary_contact(cid: str, p: Patient = Depends(current_patient), db: Session = Depends(get_db)):
    """One-click 'make primary' (the previous primary contact is demoted)."""
    c = _owned_contact(db, p, cid)
    c.is_primary = True
    _one_primary(db, p, c.id)
    log_event(db, actor_type="patient", actor_id=p.id, patient_id=p.id, action="emergency_contact_updated", resource_type="emergency_contact", resource_id=c.id)
    db.commit()
    return contact_dict(c)


# ---------------- missing / outdated profile info (used by the dashboard) ----------------

@router.get("/profile/gaps")
def profile_gaps(p: Patient = Depends(current_patient), db: Session = Depends(get_db)):
    from datetime import timedelta

    from app.models.shared import utcnow

    gaps = []
    if not p.phone:
        gaps.append({"key": "phone", "message": "Add a phone number so clinics can reach you.", "link": "#/profile"})
    if not p.address:
        gaps.append({"key": "address", "message": "Add your home address.", "link": "#/profile"})
    if p.verification_status != "verified":
        gaps.append({"key": "identity", "message": "Your identity is not verified yet. A clinic can verify it with a photo ID.", "link": "#/profile"})
    contacts = db.scalars(select(EmergencyContact).where(EmergencyContact.patient_id == p.id)).all()
    if not contacts:
        gaps.append({"key": "contact", "message": "Add an emergency contact (optional but helpful).", "link": "#/contacts"})
    elif not any(c.is_primary for c in contacts):
        gaps.append({"key": "primary_contact", "message": "Pick one emergency contact as primary.", "link": "#/contacts"})
    if p.allergy_status == "unknown":
        gaps.append({"key": "allergies", "message": "Allergies: add what you know, or say 'No known allergies'.", "link": "#/med_allergies"})
    if p.updated_at < utcnow() - timedelta(days=365):
        gaps.append({"key": "outdated", "message": "Your profile hasn't been updated in over a year. Please review it.", "link": "#/profile"})
    return {"items": gaps}
