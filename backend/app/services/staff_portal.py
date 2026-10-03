"""Logic behind the staff portal routers: patient search, DOB confirmation, role-based document
filtering, consent adapter (lazy, degrades to a clear "unavailable" state), appointment intake view
and loosely FHIR-shaped export."""
import json
import re
from datetime import timedelta

from fastapi import HTTPException
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models.shared import Patient, utcnow
from app.models.staff import StaffPatientConfirmation, StaffUser
from app.services.staff_security import FRONT_DESK_CATEGORIES

CONFIRM_TTL = timedelta(minutes=30)
UNAVAILABLE = "The document service is unavailable right now. Please try again in a moment."


# ---------------------------------------------------------------- consent adapter

def consent_service():
    """Lazy import of W2's consent engine; 503 with a friendly message if it is missing/broken."""
    try:
        from app.services import consent
        return consent
    except Exception:
        raise HTTPException(503, UNAVAILABLE)


# ---------------------------------------------------------------- patient search / identity

def mask_dob(dob: str | None) -> str:
    return f"{dob[:4]}-**-**" if dob and len(dob) >= 4 else "unknown"


def search_patients(db: Session, *, query: str, dob: str | None = None, limit: int = 10) -> list[dict]:
    """Minimal identity only: internal id, name, masked DOB. NO clinical data."""
    q = (query or "").strip()
    if len(q) < 2:
        return []
    # Every word must appear in the legal or preferred name, in any order ("Ellis Jordan" finds
    # "Jordan Alexander Ellis"). Case-insensitive; LIKE wildcards in user input are dropped.
    tokens = [t for t in re.split(r"[\s,.;]+", q.replace("%", "").replace("_", "")) if t][:6]
    if not tokens:
        return []
    stmt = select(Patient)
    for t in tokens:
        like = f"%{t}%"
        stmt = stmt.where(or_(Patient.legal_name.ilike(like), Patient.preferred_name.ilike(like)))
    if dob:
        stmt = stmt.where(Patient.dob == dob.strip())
    rows = db.execute(stmt.order_by(Patient.legal_name).limit(limit)).scalars().all()
    return [{"id": p.id, "name": p.legal_name, "dob_masked": mask_dob(p.dob)} for p in rows]


def identity_block(p: Patient) -> dict:
    return {"id": p.id, "legal_name": p.legal_name, "preferred_name": p.preferred_name, "dob": p.dob,
            "phone": p.phone, "address": p.address, "pronouns": p.pronouns,
            "verification_status": p.verification_status}


def confirm_dob(db: Session, staff: StaffUser, patient: Patient, dob: str) -> bool:
    ok = bool(patient.dob) and (dob or "").strip() == patient.dob
    if ok:
        mark_confirmed(db, staff.id, patient.id)
    return ok


def mark_confirmed(db: Session, staff_id: str, patient_id: str) -> None:
    row = db.scalar(select(StaffPatientConfirmation).where(
        StaffPatientConfirmation.staff_id == staff_id, StaffPatientConfirmation.patient_id == patient_id))
    if row:
        row.confirmed_at = utcnow()
    else:
        db.add(StaffPatientConfirmation(staff_id=staff_id, patient_id=patient_id))
    db.flush()


def is_confirmed(db: Session, staff_id: str, patient_id: str) -> bool:
    row = db.scalar(select(StaffPatientConfirmation).where(
        StaffPatientConfirmation.staff_id == staff_id, StaffPatientConfirmation.patient_id == patient_id))
    return bool(row and utcnow() - row.confirmed_at <= CONFIRM_TTL)


def require_confirmed_patient(db: Session, staff: StaffUser, patient_id: str) -> Patient:
    patient = db.get(Patient, patient_id)
    if patient is None:
        raise HTTPException(404, "Patient not found.")
    if not is_confirmed(db, staff.id, patient_id):
        raise HTTPException(403, "Confirm the patient's date of birth first.")
    return patient


# ---------------------------------------------------------------- documents (role aware)

def source_label(source: str | None) -> str:
    return "Clinician-confirmed" if source in ("clinician_provided", "clinician_confirmed") else "Patient-entered"


def role_category_ok(role: str, category: str | None) -> bool:
    if role in ("nurse", "physician"):
        return True
    if role == "front_desk":
        return (category or "").lower() in FRONT_DESK_CATEGORIES
    return False


def doc_view(meta: dict) -> dict:
    return {"id": meta["id"], "name": meta["name"], "description": meta.get("description"),
            "category": meta.get("category"), "mime": meta.get("mime") or meta.get("mime_type"),
            "size": meta.get("size") or meta.get("size_bytes"), "uploaded_at": meta.get("uploaded_at"),
            "source": meta.get("source"), "source_label": source_label(meta.get("source")),
            "grant_expires_at": meta.get("grant_expires_at")}


def visible_documents(db: Session, staff: StaffUser, patient_id: str) -> list[dict]:
    consent = consent_service()
    metas = consent.list_visible_documents(db, staff_id=staff.id, staff_role=staff.role, patient_id=patient_id)
    return [doc_view(m) for m in metas if role_category_ok(staff.role, m.get("category"))]


def grant_scope(db: Session, staff: StaffUser, patient_id: str) -> dict:
    """What this staff member's organization is currently authorized to see for the patient."""
    from app.models.documents import ShareGrant

    now = utcnow()
    grants = db.scalars(select(ShareGrant).where(
        ShareGrant.patient_id == patient_id, ShareGrant.provider_id == staff.provider_id,
        ShareGrant.revoked_at.is_(None), ShareGrant.expires_at > now)).all()
    items = [{"scope_type": g.scope_type, "category": g.category, "document_id": g.document_id,
              "purpose": g.purpose, "expires_at": g.expires_at.isoformat(timespec="seconds") + "Z"}
             for g in grants]
    latest = max((g.expires_at for g in grants), default=None)
    return {"grants": items, "authorized": bool(items),
            "authorized_until": latest.isoformat(timespec="seconds") + "Z" if latest else None}


# ---------------------------------------------------------------- appointment intake

def _label(key: str) -> str:
    return str(key).replace("_", " ").strip().capitalize()


def flatten_intake(intake, prefix: str = "") -> list[dict]:
    """Standardised, order-preserving list of {key, label, value} (value is always a string)."""
    out: list[dict] = []
    if isinstance(intake, dict):
        for k, v in intake.items():
            key = f"{prefix}{k}"
            if isinstance(v, (dict, list)):
                out.extend(flatten_intake(v, key + "."))
            elif v not in (None, ""):
                out.append({"key": key, "label": _label(k), "value": str(v)})
    elif isinstance(intake, list):
        simple = all(not isinstance(x, (dict, list)) for x in intake)
        if simple:
            if intake:
                out.append({"key": prefix.rstrip("."), "label": _label(prefix.rstrip(".").split(".")[-1]),
                            "value": ", ".join(str(x) for x in intake)})
        else:
            for i, x in enumerate(intake, 1):
                out.extend(flatten_intake(x, f"{prefix}{i}."))
    return out


FHIR_APPT_STATUS = {"requested": "proposed", "booked": "booked", "rescheduled": "proposed",
                    "cancelled": "cancelled", "declined": "cancelled", "draft": "proposed"}


def appointment_to_dict(db: Session, a, patient: Patient | None, *, include_intake: bool = True) -> dict:
    from app.models.shared import Provider

    prov = db.get(Provider, a.provider_id) if a.provider_id else None
    d = {"id": a.id, "status": a.status, "scheduled_for": a.scheduled_for, "staff_note": a.staff_note,
         "submitted_at": a.submitted_at.isoformat() + "Z" if a.submitted_at else None,
         "updated_at": a.updated_at.isoformat() + "Z" if a.updated_at else None,
         "patient": {"id": patient.id, "name": patient.legal_name, "dob_masked": mask_dob(patient.dob)} if patient else None,
         "provider": {"id": prov.id, "name": prov.name} if prov else None}
    if include_intake:
        d["intake_items"] = flatten_intake(a.intake)
        d["contact"] = flatten_intake(a.contact)
    return d


def fhir_export(a, patient: Patient, provider_name: str | None) -> dict:
    """Loosely FHIR R4-shaped bundle (Patient, Appointment, QuestionnaireResponse). Demo only."""
    pat = {"resourceType": "Patient", "id": patient.id,
           "name": [{"text": patient.legal_name}], "birthDate": patient.dob}
    contact = a.contact or {}
    telecom = []
    if contact.get("phone") or patient.phone:
        telecom.append({"system": "phone", "value": contact.get("phone") or patient.phone})
    if contact.get("email"):
        telecom.append({"system": "email", "value": contact["email"]})
    if telecom:
        pat["telecom"] = telecom
    addr = contact.get("address") or patient.address
    if addr:
        pat["address"] = [{"text": str(addr)}]
    appt = {"resourceType": "Appointment", "id": a.id,
            "status": FHIR_APPT_STATUS.get(a.status, "proposed"),
            "participant": [{"actor": {"reference": f"Patient/{patient.id}", "display": patient.legal_name},
                             "status": "accepted" if a.status == "booked" else "tentative"}]}
    if provider_name:
        appt["participant"].append({"actor": {"display": provider_name}, "status": "accepted"})
    if a.scheduled_for:
        appt["start"] = a.scheduled_for
    if a.staff_note:
        appt["comment"] = a.staff_note
    items = []
    for it in flatten_intake(a.intake):
        items.append({"linkId": it["key"], "text": it["label"], "answer": [{"valueString": it["value"]}]})
    qr = {"resourceType": "QuestionnaireResponse", "id": f"intake-{a.id}", "status": "completed",
          "subject": {"reference": f"Patient/{patient.id}"},
          "authored": a.submitted_at.isoformat() + "Z" if a.submitted_at else None, "item": items}
    return {"resourceType": "Bundle", "type": "collection",
            "meta": {"tag": [{"display": "Loosely FHIR-shaped demo export; synthetic data; not conformant"}]},
            "entry": [{"fullUrl": f"urn:uuid:{r['id']}", "resource": r} for r in (pat, appt, qr)]}


def loads(s: str | None, default=None):
    try:
        return json.loads(s) if s else default
    except ValueError:
        return default
