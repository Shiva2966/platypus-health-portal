"""Staff-facing patient search, patient page, documents, access requests, share-token redemption, dashboard."""
import re
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models.shared import Patient, utcnow
from app.models.staff import StaffUser
from app.security import throttle_check, throttle_fail, throttle_reset
from app.services import staff_portal as sp
from app.services.audit import log_event
from app.services.notif_helpers import unread_count
from app.services.staff_security import FRONT_DESK_CATEGORIES, require_role

router = APIRouter(prefix="/api/staff", tags=["staff-patients"])
CLINICAL_STAFF = ("front_desk", "nurse", "physician")
Clinical = Depends(require_role(*CLINICAL_STAFF))


class DobIn(BaseModel):
    dob: str = Field(min_length=8, max_length=10)


class AccessRequestIn(BaseModel):
    categories: list[str] = []
    document_ids: list[str] = []
    purpose: str = Field(default="", max_length=300)
    duration_days: int = Field(default=7, ge=1, le=365)


class RedeemIn(BaseModel):
    token: str = Field(min_length=8, max_length=300)


@router.get("/patients/search")
def search(q: str = Query(min_length=2, max_length=100), dob: str | None = Query(default=None, max_length=10),
           staff: StaffUser = Clinical, db: Session = Depends(get_db)):
    results = sp.search_patients(db, query=q, dob=dob)
    log_event(db, actor_type="staff", actor_id=staff.id, patient_id=None, action="patient_search",
              detail={"query_length": len(q.strip()), "dob_filter": bool(dob), "results": len(results)})
    db.commit()
    return {"results": results,
            "note": "Only name and birth year are shown until you confirm the date of birth."}


@router.get("/patients/{patient_id}/minimal")
def patient_minimal(patient_id: str, staff: StaffUser = Clinical, db: Session = Depends(get_db)):
    """Name + birth year only (for deep links such as notifications); the DOB step is still required."""
    p = db.get(Patient, patient_id)
    if p is None:
        raise HTTPException(404, "Patient not found.")
    return {"id": p.id, "name": p.legal_name, "dob_masked": sp.mask_dob(p.dob),
            "confirmed": sp.is_confirmed(db, staff.id, patient_id)}


@router.post("/patients/{patient_id}/confirm")
def confirm(patient_id: str, body: DobIn, staff: StaffUser = Clinical, db: Session = Depends(get_db)):
    patient = db.get(Patient, patient_id)
    if patient is None:
        raise HTTPException(404, "Patient not found.")
    key = f"dob:{staff.id}:{patient_id}"
    throttle_check(key)
    if not sp.confirm_dob(db, staff, patient, body.dob):
        throttle_fail(key)
        log_event(db, actor_type="staff", actor_id=staff.id, patient_id=patient_id, action="dob_check_failed",
                  resource_type="patient", resource_id=patient_id)
        db.commit()
        raise HTTPException(403, "That date of birth does not match. Check you picked the right person.")
    throttle_reset(key)
    log_event(db, actor_type="staff", actor_id=staff.id, patient_id=patient_id, action="dob_confirmed",
              resource_type="patient", resource_id=patient_id)
    db.commit()
    return {"confirmed": True, "identity": sp.identity_block(patient)}


@router.get("/patients/{patient_id}")
def patient_page(patient_id: str, staff: StaffUser = Clinical, db: Session = Depends(get_db)):
    patient = sp.require_confirmed_patient(db, staff, patient_id)
    errors: dict[str, str] = {}
    out = {"identity": sp.identity_block(patient), "role": staff.role}
    try:
        out["scope"] = sp.grant_scope(db, staff, patient_id)
    except Exception:
        db.rollback()
        out["scope"] = {"grants": [], "authorized": False, "authorized_until": None}
        errors["scope"] = sp.UNAVAILABLE
    try:
        out["documents"] = sp.visible_documents(db, staff, patient_id)
    except HTTPException as e:
        out["documents"] = []
        errors["documents"] = str(e.detail)
    except Exception:
        db.rollback()
        out["documents"] = []
        errors["documents"] = "Documents could not be loaded. Please retry."
    try:
        out["requests"] = sp.consent_service().requests_for_staff(db, staff_id=staff.id, patient_id=patient_id)
    except HTTPException as e:
        out["requests"] = []
        errors["requests"] = str(e.detail)
    except Exception:
        db.rollback()
        out["requests"] = []
        errors["requests"] = "Access requests could not be loaded. Please retry."
    try:
        from app.models.appointments import Appointment
        appts = db.scalars(select(Appointment).where(
            Appointment.patient_id == patient_id, Appointment.provider_id == staff.provider_id,
            Appointment.status != "draft").order_by(Appointment.created_at.desc()).limit(20)).all()
        out["appointments"] = [sp.appointment_to_dict(db, a, patient, include_intake=False) for a in appts]
    except Exception:
        db.rollback()
        out["appointments"] = []
        errors["appointments"] = "Appointments could not be loaded. Please retry."
    out["errors"] = errors
    out["can_request_access"] = True
    out["has_records"] = bool(out["documents"])
    out["empty_message"] = None if out["documents"] else "No shared records"
    out["allowed_categories"] = None if staff.role != "front_desk" else sorted(FRONT_DESK_CATEGORIES)
    log_event(db, actor_type="staff", actor_id=staff.id, patient_id=patient_id, action="patient_record_opened",
              resource_type="patient", resource_id=patient_id,
              detail={"role": staff.role, "provider_id": staff.provider_id, "documents_visible": len(out["documents"])})
    db.commit()
    return out


_SAFE_INLINE = re.compile(r"^(application/pdf|image/(png|jpeg|webp|heic|heif|gif))$")


@router.get("/patients/{patient_id}/documents/{document_id}/file")
def open_document(patient_id: str, document_id: str, download: bool = False, staff: StaffUser = Clinical,
                  db: Session = Depends(get_db)):
    sp.require_confirmed_patient(db, staff, patient_id)
    consent = sp.consent_service()
    # role gate BEFORE bytes are fetched (a denied role attempt is audited, patient is not notified)
    allowed = {d["id"] for d in sp.visible_documents(db, staff, patient_id)}
    if document_id not in allowed:
        log_event(db, actor_type="staff", actor_id=staff.id, patient_id=patient_id,
                  action="document_access_denied", resource_type="document", resource_id=document_id,
                  detail={"role": staff.role, "reason": "not authorized for role or no active grant"})
        db.commit()
        raise HTTPException(403, "You are not authorized to open this document.")
    try:
        meta, data = consent.get_document_for_staff(db, staff_id=staff.id, staff_role=staff.role,
                                                    patient_id=patient_id, document_id=document_id)
    except PermissionError:
        raise HTTPException(403, "You are not authorized to open this document.")
    mime = meta.get("mime") or meta.get("mime_type") or "application/octet-stream"
    try:  # W2's delivery headers (safe Content-Disposition, nosniff, PDF-friendly CSP)
        from app.services import files
        headers = files.delivery_headers(meta.get("name") or "document", mime, download=download)
        media = headers.pop("Content-Type", mime)
    except Exception:
        inline = bool(_SAFE_INLINE.match(mime)) and not download
        fname = re.sub(r"[^\w.\- ]", "_", meta.get("name") or "document")[:120] or "document"
        headers = {"Content-Disposition": f"{'inline' if inline else 'attachment'}; "
                                          f"filename=\"{fname.encode('ascii', 'replace').decode()}\"; "
                                          f"filename*=UTF-8''{quote(fname)}",
                   "X-Content-Type-Options": "nosniff", "Cache-Control": "no-store",
                   "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; sandbox"}
        media = mime if inline else "application/octet-stream"
    return Response(content=data, media_type=media, headers=headers)


@router.post("/patients/{patient_id}/request-access")
def request_access(patient_id: str, body: AccessRequestIn, staff: StaffUser = Clinical,
                   db: Session = Depends(get_db)):
    sp.require_confirmed_patient(db, staff, patient_id)
    cats = [c.strip().lower() for c in body.categories if c.strip()]
    if staff.role == "front_desk":
        bad = [c for c in cats if c not in FRONT_DESK_CATEGORIES]
        if bad or body.document_ids:
            raise HTTPException(403, "Front desk can only request insurance, billing and ID categories.")
    if not body.purpose.strip():
        raise HTTPException(422, "Please state the purpose of the request.")
    consent = sp.consent_service()
    try:
        req = consent.create_access_request(db, staff_id=staff.id, patient_id=patient_id, categories=cats,
                                            document_ids=body.document_ids, purpose=body.purpose,
                                            duration_days=body.duration_days)
    except ValueError as e:
        raise HTTPException(422, str(e))
    except LookupError as e:
        raise HTTPException(404, str(e))
    except PermissionError as e:
        raise HTTPException(403, str(e))
    return consent.request_to_dict(db, req)


@router.get("/requests")
def my_requests(patient_id: str | None = None, status: str | None = None, staff: StaffUser = Clinical,
                db: Session = Depends(get_db)):
    consent = sp.consent_service()
    rows = consent.requests_for_staff(db, staff_id=staff.id, patient_id=patient_id)
    if status:
        rows = [r for r in rows if r["status"] == status]
    names = {p.id: p.legal_name for p in db.scalars(select(Patient).where(
        Patient.id.in_({r["patient_id"] for r in rows} or {""}))).all()}
    for r in rows:
        r["patient_name"] = names.get(r["patient_id"])
    return {"requests": rows}


@router.post("/redeem")
def redeem(body: RedeemIn, staff: StaffUser = Clinical, db: Session = Depends(get_db)):
    consent = sp.consent_service()
    try:
        summary = consent.redeem_share_token(db, token=body.token, staff_id=staff.id)
    except consent.TooManyAttempts as e:
        raise HTTPException(429, str(e))
    except consent.InvalidToken as e:
        raise HTTPException(400, str(e))
    except PermissionError as e:
        raise HTTPException(403, str(e))
    sp.mark_confirmed(db, staff.id, summary["patient_id"])  # the patient handed over the code in person
    db.commit()
    return summary


@router.get("/documents/search")
def search_documents(q: str = Query(min_length=2, max_length=100), staff: StaffUser = Clinical,
                     db: Session = Depends(get_db)):
    """Global search by document name/description among documents this staff member is authorized for."""
    from app.models.documents import ShareGrant

    needle = q.strip().lower()
    pids = db.scalars(select(ShareGrant.patient_id).where(
        ShareGrant.provider_id == staff.provider_id, ShareGrant.revoked_at.is_(None),
        ShareGrant.expires_at > utcnow()).distinct()).all()
    results = []
    for pid in pids:
        patient = db.get(Patient, pid)
        for d in sp.visible_documents(db, staff, pid):
            if needle in (d["name"] or "").lower() or needle in (d.get("description") or "").lower():
                results.append({**d, "patient_id": pid, "patient_name": patient.legal_name if patient else None})
    return {"results": results[:50]}


@router.get("/dashboard")
def dashboard(staff: StaffUser = Clinical, db: Session = Depends(get_db)):
    errors: dict[str, str] = {}
    out: dict = {"unread_notifications": unread_count(db, "staff", staff.id)}
    try:
        from app.models.appointments import Appointment
        out["new_appointment_requests"] = db.scalar(select(func.count()).select_from(Appointment).where(
            Appointment.provider_id == staff.provider_id, Appointment.status == "requested")) or 0
    except Exception:
        db.rollback()
        out["new_appointment_requests"] = None
        errors["appointments"] = "Appointment data unavailable."
    try:
        consent = sp.consent_service()
        reqs = consent.requests_for_staff(db, staff_id=staff.id)
        counts = {"pending": 0, "approved": 0, "denied": 0, "expired": 0}
        for r in reqs:
            counts[r["status"]] = counts.get(r["status"], 0) + 1
        out["access_requests"] = counts
        names = {p.id: p.legal_name for p in db.scalars(select(Patient).where(
            Patient.id.in_({r["patient_id"] for r in reqs[:8]} or {""}))).all()}
        out["recent_requests"] = [{**r, "patient_name": names.get(r["patient_id"])} for r in reqs[:8]]
    except HTTPException as e:
        out["access_requests"] = None
        out["recent_requests"] = []
        errors["access_requests"] = str(e.detail)
    except Exception:
        db.rollback()
        out["access_requests"] = None
        out["recent_requests"] = []
        errors["access_requests"] = "Access request data unavailable."
    try:
        from app.models.documents import ShareGrant
        rows = db.execute(select(func.count(), func.count(ShareGrant.patient_id.distinct())).where(
            ShareGrant.provider_id == staff.provider_id, ShareGrant.revoked_at.is_(None),
            ShareGrant.expires_at > utcnow())).one()
        out["shares_granted"] = {"grants": rows[0], "patients": rows[1]}
    except Exception:
        db.rollback()
        out["shares_granted"] = None
        errors["shares"] = "Sharing data unavailable."
    out["errors"] = errors
    return out
