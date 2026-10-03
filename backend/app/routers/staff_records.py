"""Staff view of patients' shared health data (W7 records + W8 billing via W2's consent service),
clinician confirmation and the corrections inbox.

Every read goes through `consent.get_records_for_staff` (role matrix + active grant; audited; the
patient is notified). Page loads only use `list_visible_record_categories` (no data, no audit).
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models.shared import Patient, Provider
from app.models.staff import StaffUser
from app.services import staff_portal as sp
from app.services.audit import log_event
from app.services.staff_security import require_role

router = APIRouter(prefix="/api/staff", tags=["staff-records"])
Clinical = Depends(require_role("front_desk", "nurse", "physician"))
Clinician = Depends(require_role("nurse", "physician"))

CLINICAL = ("history", "medications", "allergies", "vaccinations", "results")
ALL_CATEGORIES = CLINICAL + ("billing",)
TAB_LABELS = {"allergies": "Allergies", "medications": "Medications", "history": "Conditions & history",
              "vaccinations": "Vaccinations", "results": "Results", "billing": "Billing"}
TAB_ORDER = ("allergies", "medications", "history", "vaccinations", "results", "billing")
NOT_SHARED = "Not shared with you. The patient has not shared this with your organization, or the sharing has ended."
ROLE_BARRED = "Your role can't view this kind of record."
# correction.record_type -> consent category ("provider" corrections have no consent category)
CORRECTION_CATEGORY = {"history": "history", "medication": "medications", "allergy": "allergies",
                       "vaccination": "vaccinations", "result": "results"}


class ResolveIn(BaseModel):
    status: str = Field(pattern="^(resolved|declined)$")
    note: str = Field(default="", max_length=2000)


def _consent():
    return sp.consent_service()


def _signature(db: Session, staff: StaffUser) -> str:
    prov = db.get(Provider, staff.provider_id) if staff.provider_id else None
    role = {"nurse": "Nurse", "physician": "Physician"}.get(staff.role, staff.role)
    return f"{staff.name} ({role}){', ' + prov.name if prov else ''}"


def _check_category(category: str) -> str:
    c = (category or "").strip().lower()
    if c not in ALL_CATEGORIES:
        raise HTTPException(404, "Unknown record category.")
    return c


@router.get("/patients/{patient_id}/records")
def records_overview(patient_id: str, staff: StaffUser = Clinical, db: Session = Depends(get_db)):
    """Which tabs this staff member may open right now (no record data, not audited)."""
    sp.require_confirmed_patient(db, staff, patient_id)
    consent = _consent()
    visible = {v["category"]: v for v in consent.list_visible_record_categories(
        db, staff_id=staff.id, staff_role=staff.role, patient_id=patient_id)}
    tabs = []
    for c in TAB_ORDER:
        role_ok = consent.staff_role_allows(staff.role, c)
        v = visible.get(c)
        tabs.append({"category": c, "label": TAB_LABELS[c], "role_allowed": role_ok, "shared": bool(v),
                     "grant_expires_at": v["grant_expires_at"] if v else None,
                     "message": None if v else (NOT_SHARED if role_ok else ROLE_BARRED)})
    open_corr = None
    if staff.role in ("nurse", "physician"):
        open_corr = len(_visible_corrections(db, staff, patient_id, "open", visible_only=set(visible)))
    return {"tabs": tabs, "role": staff.role, "can_confirm": staff.role in ("nurse", "physician"),
            "open_corrections": open_corr,
            "notice": "Opening a tab is recorded in the patient's access history and the patient is notified."}


@router.get("/patients/{patient_id}/records/{category}")
def records_category(patient_id: str, category: str, staff: StaffUser = Clinical, db: Session = Depends(get_db)):
    sp.require_confirmed_patient(db, staff, patient_id)
    category = _check_category(category)
    consent = _consent()
    try:
        items = consent.get_records_for_staff(db, staff_id=staff.id, staff_role=staff.role,
                                              patient_id=patient_id, category=category)
    except PermissionError:
        raise HTTPException(403, NOT_SHARED if consent.staff_role_allows(staff.role, category) else ROLE_BARRED)
    exp = next((v["grant_expires_at"] for v in consent.list_visible_record_categories(
        db, staff_id=staff.id, staff_role=staff.role, patient_id=patient_id) if v["category"] == category), None)
    return {"category": category, "label": TAB_LABELS[category], "items": items, "grant_expires_at": exp,
            "can_confirm": staff.role in ("nurse", "physician") and category in CLINICAL}


@router.post("/patients/{patient_id}/records/{category}/{record_id}/confirm")
def confirm(patient_id: str, category: str, record_id: str, staff: StaffUser = Clinician,
            db: Session = Depends(get_db)):
    sp.require_confirmed_patient(db, staff, patient_id)
    category = _check_category(category)
    if category not in CLINICAL:
        raise HTTPException(400, "Only health records can be clinician-confirmed.")
    if not _consent().staff_can_read_category(db, staff_id=staff.id, patient_id=patient_id, category=category):
        raise HTTPException(403, NOT_SHARED)
    from app.services import medical_read, medical_write
    try:
        row = medical_write.confirm_record(db, patient_id=patient_id, res=category, rec_id=record_id,
                                           confirmed_by=_signature(db, staff), actor_type="staff", actor_id=staff.id)
    except medical_write.NotFound:
        raise HTTPException(404, "Record not found.")
    db.commit()
    ser = {"history": medical_read.ser_history, "medications": medical_read.ser_medication,
           "allergies": medical_read.ser_allergy, "vaccinations": medical_read.ser_vaccination,
           "results": medical_read.ser_result}[category]
    return ser(row)


# ---------------------------------------------------------------- corrections

def _visible_corrections(db: Session, staff: StaffUser, patient_id: str, status: str | None,
                         visible_only: set | None = None) -> list[dict]:
    from app.services import medical_read
    consent = _consent()
    cache: dict[str, bool] = {}

    def ok(cat: str) -> bool:
        if visible_only is not None:
            return cat in visible_only and consent.staff_role_allows(staff.role, cat)
        if cat not in cache:
            cache[cat] = consent.staff_can_read_category(db, staff_id=staff.id, patient_id=patient_id, category=cat)
        return cache[cat]

    out = []
    for c in medical_read.list_corrections(db, patient_id, status):
        cat = CORRECTION_CATEGORY.get(c["record_type"])
        if cat and ok(cat):
            out.append({**c, "category": cat, "category_label": TAB_LABELS[cat]})
    return out


@router.get("/patients/{patient_id}/corrections")
def patient_corrections(patient_id: str, status: str | None = "open", staff: StaffUser = Clinician,
                        db: Session = Depends(get_db)):
    sp.require_confirmed_patient(db, staff, patient_id)
    status = status if status in ("open", "resolved", "declined", "withdrawn") else None
    items = _visible_corrections(db, staff, patient_id, status)
    log_event(db, actor_type="staff", actor_id=staff.id, patient_id=patient_id, action="med_corrections_viewed",
              resource_type="med_correction", detail={"role": staff.role, "count": len(items)})
    db.commit()
    return {"items": items}


@router.get("/corrections")
def corrections_inbox(staff: StaffUser = Clinician, db: Session = Depends(get_db)):
    """Open correction requests from patients who currently share the matching category with this
    organization. Minimal fields only (no record names / messages - those open on the patient page)."""
    from app.models.documents import ShareGrant
    from app.models.medical import MedCorrection
    from app.models.shared import utcnow

    pids = set(db.scalars(select(ShareGrant.patient_id).where(
        ShareGrant.provider_id == staff.provider_id, ShareGrant.revoked_at.is_(None),
        ShareGrant.expires_at > utcnow(), ShareGrant.scope_type == "category",
        ShareGrant.category.in_(CLINICAL))).all())
    if not pids:
        return {"items": [], "total": 0}
    rows = db.scalars(select(MedCorrection).where(MedCorrection.patient_id.in_(pids), MedCorrection.status == "open")
                      .order_by(MedCorrection.created_at.desc()).limit(500)).all()
    consent = _consent()
    cache: dict[tuple, bool] = {}
    names = {p.id: p.legal_name for p in db.scalars(select(Patient).where(Patient.id.in_(pids))).all()}
    items = []
    for c in rows:
        cat = CORRECTION_CATEGORY.get(c.record_type)
        if not cat:
            continue
        key = (c.patient_id, cat)
        if key not in cache:
            cache[key] = consent.staff_can_read_category(db, staff_id=staff.id, patient_id=c.patient_id, category=cat)
        if cache[key]:
            items.append({"id": c.id, "patient_id": c.patient_id, "patient_name": names.get(c.patient_id),
                          "category": cat, "category_label": TAB_LABELS[cat],
                          "created_at": c.created_at.isoformat(timespec="seconds") + "Z" if c.created_at else None})
    return {"items": items, "total": len(items)}


@router.post("/corrections/{correction_id}/resolve")
def resolve(correction_id: str, body: ResolveIn, staff: StaffUser = Clinician, db: Session = Depends(get_db)):
    from app.models.medical import MedCorrection
    from app.services import medical_write

    c = db.get(MedCorrection, correction_id)
    if c is None:
        raise HTTPException(404, "Correction request not found.")
    sp.require_confirmed_patient(db, staff, c.patient_id)
    cat = CORRECTION_CATEGORY.get(c.record_type)
    if not cat or not _consent().staff_can_read_category(db, staff_id=staff.id, patient_id=c.patient_id, category=cat):
        raise HTTPException(403, NOT_SHARED)
    if body.status == "declined" and not body.note.strip():
        raise HTTPException(422, "Please tell the patient why the correction was declined.")
    try:
        medical_write.resolve_correction(db, correction_id=c.id, status=body.status, resolved_by=_signature(db, staff),
                                         note=body.note.strip() or None, actor_id=staff.id)
    except ValueError as e:
        raise HTTPException(409, str(e))
    db.commit()
    from app.services import medical_read
    return medical_read.ser_correction(c)
