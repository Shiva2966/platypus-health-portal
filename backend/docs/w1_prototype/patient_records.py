"""Generic patient-owned records (history, meds, allergies, results, insurance, contacts, bills, EOBs) + helpers."""
import json
from datetime import date

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.core import RECORD_CATEGORIES\nfrom app.core_schemas import SCHEMAS, dedupe_key, is_active_medication, validate
from app.db import get_db
from app.errors import FieldError
from app.models.core import CorrectionRequest, Record
from app.models.shared import Patient, utcnow
from app.security import current_patient
from app.services.audit import log_event
from app.services.explain import explain
from app.services.notifications import notify

router = APIRouter(prefix="/api")


def get_owned(db: Session, patient: Patient, rid: str, category: str | None = None) -> Record:
    """ALWAYS fetch records through this: enforces that the record belongs to the logged-in patient (404 otherwise)."""
    r = db.get(Record, rid)
    if not r or r.patient_id != patient.id or (category and r.category != category):
        raise HTTPException(404, "Not found.")
    return r


def check_category(category: str) -> dict:
    if category not in RECORD_CATEGORIES:
        raise HTTPException(404, "Unknown record type.")
    return SCHEMAS[category]


def result_flag(d: dict) -> str:
    v, lo, hi = d.get("value"), d.get("ref_low"), d.get("ref_high")
    if v is None or (lo is None and hi is None):
        return "no_range"
    if lo is not None and v < lo:
        return "low"
    if hi is not None and v > hi:
        return "high"
    return "in_range"


def rec_dict(r: Record, db: Session | None = None) -> dict:
    d = r.data
    spec = SCHEMAS.get(r.category, {})
    out = {"id": r.id, "category": r.category, "data": d, "title": d.get(spec.get("title", ""), ""),
           "source": r.source, "confirmed_by": r.confirmed_by, "confirmed_at": r.confirmed_at,
           "verification": r.verification, "verification_note": r.verification_note,
           "created_at": r.created_at, "updated_at": r.updated_at, "editable": r.source != "clinician_confirmed"}
    if r.category == "medication":
        out["active"] = is_active_medication(d)
    if r.category == "result":
        out["flag"] = result_flag(d)
    if r.category == "insurance":
        today = date.today().isoformat()
        out["currently_effective"] = d.get("effective_date", "") <= today and (not d.get("end_date") or d["end_date"] >= today)
    return out


def _find_duplicate(db, patient, category, data, exclude_id=None):
    key = dedupe_key(category, data)
    if key is None:
        return None
    for o in db.scalars(select(Record).where(Record.patient_id == patient.id, Record.category == category)):
        if o.id != exclude_id and dedupe_key(category, o.data) == key:
            return o
    return None


@router.get("/schema")
def schema(_p: Patient = Depends(current_patient)):
    return SCHEMAS


@router.get("/records/{category}")
def list_records(category: str, p: Patient = Depends(current_patient), db: Session = Depends(get_db)):
    check_category(category)
    rows = db.scalars(select(Record).where(Record.patient_id == p.id, Record.category == category).order_by(Record.created_at.desc())).all()
    items = [rec_dict(r) for r in rows]
    out = {"items": items, "label": SCHEMAS[category]["label"]}
    if category == "allergy":
        out["allergy_status"] = p.allergy_status
    return out


@router.post("/records/{category}", status_code=201)
def create_record(category: str, payload: dict, p: Patient = Depends(current_patient), db: Session = Depends(get_db)):
    check_category(category)
    data = validate(category, payload)
    dup = _find_duplicate(db, p, category, data)
    if dup:
        raise HTTPException(409, f"This looks like a duplicate of an entry you already have ({SCHEMAS[category]['label']}). Edit that one instead.")
    r = Record(patient_id=p.id, category=category, data_json=json.dumps(data), source="patient_entered",
               verification="entered" if category == "insurance" else None)
    db.add(r)
    db.flush()
    if category == "emergency_contact" and data.get("is_primary"):
        _clear_other_primary(db, p, r.id)
    if category == "allergy":
        p.allergy_status = "has_allergies"
    log_event(db, actor_type="patient", actor_id=p.id, patient_id=p.id, action="record_created", resource_type=category, resource_id=r.id)
    db.commit()
    return rec_dict(r)


def _clear_other_primary(db, p, keep_id):
    for o in db.scalars(select(Record).where(Record.patient_id == p.id, Record.category == "emergency_contact", Record.id != keep_id)):
        d = o.data
        if d.get("is_primary"):
            d["is_primary"] = False
            o.data_json = json.dumps(d)


@router.put("/records/{category}/{rid}")
def update_record(category: str, rid: str, payload: dict, p: Patient = Depends(current_patient), db: Session = Depends(get_db)):
    check_category(category)
    r = get_owned(db, p, rid, category)
    if r.source == "clinician_confirmed":
        raise HTTPException(403, "A clinician has confirmed this record, so you can't change it directly. Use 'Request correction' instead.")
    data = validate(category, payload)
    if _find_duplicate(db, p, category, data, exclude_id=r.id):
        raise HTTPException(409, "This would duplicate another entry you already have.")
    r.data_json = json.dumps(data)
    r.updated_at = utcnow()
    if category == "insurance":  # coverage details changed -> previous verification no longer applies
        r.verification, r.verification_note = "entered", None
    if category == "emergency_contact" and data.get("is_primary"):
        _clear_other_primary(db, p, r.id)
    log_event(db, actor_type="patient", actor_id=p.id, patient_id=p.id, action="record_updated", resource_type=category, resource_id=r.id)
    db.commit()
    return rec_dict(r)


@router.delete("/records/{category}/{rid}")
def delete_record(category: str, rid: str, p: Patient = Depends(current_patient), db: Session = Depends(get_db)):
    check_category(category)
    r = get_owned(db, p, rid, category)
    if r.source == "clinician_confirmed":
        raise HTTPException(403, "A clinician has confirmed this record, so you can't delete it directly. Use 'Request correction' instead.")
    db.delete(r)
    db.flush()
    if category == "allergy" and p.allergy_status == "has_allergies":
        left = db.scalars(select(Record.id).where(Record.patient_id == p.id, Record.category == "allergy")).first()
        if not left:
            p.allergy_status = "unknown"
    log_event(db, actor_type="patient", actor_id=p.id, patient_id=p.id, action="record_deleted", resource_type=category, resource_id=rid)
    db.commit()
    return {"ok": True}


# ---------- corrections ----------

class CorrectionIn(BaseModel):
    message: str = ""


@router.post("/records/{category}/{rid}/correction", status_code=201)
def request_correction(category: str, rid: str, body: CorrectionIn, p: Patient = Depends(current_patient), db: Session = Depends(get_db)):
    check_category(category)
    r = get_owned(db, p, rid, category)
    msg = body.message.strip()
    if not msg:
        raise FieldError({"message": "Tell us what looks wrong."})
    if len(msg) > 2000:
        raise FieldError({"message": "Too long (max 2000 characters)."})
    c = CorrectionRequest(patient_id=p.id, record_id=r.id, category=category, message=msg)
    db.add(c)
    log_event(db, actor_type="patient", actor_id=p.id, patient_id=p.id, action="correction_requested", resource_type=category, resource_id=r.id)
    db.commit()
    return {"id": c.id, "status": c.status, "note": "Request saved. In this demo it waits in a queue; no clinic workflow reads it yet."}


@router.get("/corrections")
def list_corrections(p: Patient = Depends(current_patient), db: Session = Depends(get_db)):
    rows = db.scalars(select(CorrectionRequest).where(CorrectionRequest.patient_id == p.id).order_by(CorrectionRequest.created_at.desc())).all()
    return {"items": [{"id": c.id, "record_id": c.record_id, "category": c.category, "message": c.message,
                       "status": c.status, "created_at": c.created_at} for c in rows]}


# ---------- insurance verification (MOCK) ----------

@router.post("/records/insurance/{rid}/verify")
def verify_insurance(rid: str, p: Patient = Depends(current_patient), db: Session = Depends(get_db)):
    r = get_owned(db, p, rid, "insurance")
    d = r.data
    today = date.today().isoformat()
    if d.get("member_id", "").upper().startswith("BAD"):
        r.verification, r.verification_note = "failed", "Mock check: the insurer did not recognize this member ID."
    elif d.get("end_date") and d["end_date"] < today:
        r.verification, r.verification_note = "failed", "Mock check: this coverage has ended."
    else:
        r.verification, r.verification_note = "verified", f"Mock check passed on {today}. No real insurer was contacted."
    r.updated_at = utcnow()
    log_event(db, actor_type="patient", actor_id=p.id, patient_id=p.id, action="insurance_verification_run", resource_type="insurance", resource_id=r.id)
    db.commit()
    return rec_dict(r)


# ---------- results trends ----------

@router.get("/results/trends")
def result_trends(p: Patient = Depends(current_patient), db: Session = Depends(get_db)):
    rows = db.scalars(select(Record).where(Record.patient_id == p.id, Record.category == "result")).all()
    groups: dict[tuple, list[Record]] = {}
    for r in rows:
        d = r.data
        groups.setdefault(((d.get("test_name") or "").strip().lower(), (d.get("unit") or "").strip().lower()), []).append(r)
    out = []
    for (_n, _u), rs in groups.items():
        rs.sort(key=lambda r: r.data.get("result_date", ""))
        last = rs[-1].data
        out.append({"test_name": last["test_name"], "unit": last["unit"], "ref_low": last.get("ref_low"), "ref_high": last.get("ref_high"),
                    "latest_flag": result_flag(last), "count": len(rs),
                    "points": [{"id": r.id, "date": r.data["result_date"], "value": r.data["value"], "flag": result_flag(r.data), "source": r.source} for r in rs]})
    out.sort(key=lambda g: (-g["count"], g["test_name"].lower()))
    return {"items": out}
