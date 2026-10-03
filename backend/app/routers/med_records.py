"""W7 medical records API (patient-only).  Every route depends on ``current_patient`` and filters by that patient's id;
another patient's record id is indistinguishable from a missing one (404).  Writes commit record + audit together.

  GET    /api/med/meta                          enumerations + disclaimers for the UI
  GET    /api/med/summary                       allergy banner, counts, follow-ups, results outside lab range
  GET    /api/med/timeline?types=history,...    chronological events (+ undated list)
  GET    /api/med/results/trends[?key=...]      comparable numeric series (same test + same unit)
  GET    /api/med/allergy-status                {status: unknown|no_known_allergies|has_allergies}
  PUT    /api/med/allergy-status                {status: unknown|no_known_allergies}
  GET    /api/med/directory                     hospitals in the shared directory (optional link target for care team)
  GET    /api/med/linkable-documents            my uploaded documents (to link an original report to a result)
  GET|POST            /api/med/{res}
  GET|PUT|DELETE      /api/med/{res}/{id}
        res = history | medications | allergies | vaccinations | results | providers
"""
from __future__ import annotations

from fastapi import APIRouter, Body, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models.medical import (ALLERGY_CATEGORIES, ALLERGY_SEVERITIES, HISTORY_KINDS, HISTORY_STATUSES,
                                PROVIDER_KINDS, RESULT_CATEGORIES, VACCINATION_KINDS, VACCINATION_STATUSES)
from app.models.shared import Patient, Provider
from app.security import current_patient
from app.services import medical_read as R
from app.services import medical_write as W
from app.services.medical_schemas import norm_key

router = APIRouter(prefix="/api/med", tags=["medical"])

SER = {"history": R.ser_history, "medications": R.ser_medication, "allergies": R.ser_allergy,
       "vaccinations": R.ser_vaccination, "results": R.ser_result, "providers": R.ser_provider}


def _guard(fn):
    """Run a service call; map its exceptions to HTTP errors (FieldError is handled globally -> 422)."""
    try:
        return fn()
    except W.NotFound as e:
        raise HTTPException(404, "That record was not found.") from e
    except W.Locked as e:
        raise HTTPException(409, str(e)) from e


# ----------------------------------------------------------------------------- read helpers
@router.get("/meta")
def meta(p: Patient = Depends(current_patient)):
    return {
        "history_kinds": HISTORY_KINDS, "history_statuses": HISTORY_STATUSES,
        "allergy_categories": ALLERGY_CATEGORIES, "allergy_severities": ALLERGY_SEVERITIES,
        "vaccination_kinds": VACCINATION_KINDS, "vaccination_statuses": VACCINATION_STATUSES,
        "result_categories": RESULT_CATEGORIES, "provider_kinds": PROVIDER_KINDS,
        "disclaimer": R.RESULT_DISCLAIMER, "outside_range_text": R.OUTSIDE_RANGE_TEXT,
        "consent_categories": R.MED_CATEGORIES,
    }


@router.get("/summary")
def summary(p: Patient = Depends(current_patient), db: Session = Depends(get_db)):
    return R.summary(db, p.id)


@router.get("/timeline")
def timeline(types: str | None = None, p: Patient = Depends(current_patient), db: Session = Depends(get_db)):
    return R.timeline(db, p.id, [t.strip() for t in types.split(",")] if types else None)


@router.get("/results/trends")
def trends(key: str | None = None, p: Patient = Depends(current_patient), db: Session = Depends(get_db)):
    return R.result_trends(db, p.id, key)


@router.get("/allergy-status")
def get_allergy_status(p: Patient = Depends(current_patient), db: Session = Depends(get_db)):
    return R.allergy_status_info(db, p.id)


@router.put("/allergy-status")
def put_allergy_status(body: dict = Body(...), p: Patient = Depends(current_patient), db: Session = Depends(get_db)):
    status = body.get("status") if isinstance(body, dict) else None
    W.set_allergy_status(db, p, status)
    db.commit()
    return R.allergy_status_info(db, p.id)


@router.get("/directory")
def directory(p: Patient = Depends(current_patient), db: Session = Depends(get_db)):
    rows = db.scalars(select(Provider).order_by(Provider.name)).all()
    return [{"id": r.id, "name": r.name, "specialty": r.specialty, "phone": r.phone, "address": r.address} for r in rows]


@router.get("/linkable-documents")
def linkable_documents(p: Patient = Depends(current_patient), db: Session = Depends(get_db)):
    try:
        from app.models.documents import Document
    except ImportError:  # pragma: no cover
        return []
    rows = db.execute(select(Document.id, Document.name, Document.category, Document.created_at)
                      .where(Document.patient_id == p.id, Document.deleted_at.is_(None))
                      .order_by(Document.created_at.desc()).limit(200)).all()
    return [{"id": r.id, "name": r.name, "category": r.category, "created_at": R.iso(r.created_at)} for r in rows]


# ----------------------------------------------------------------------------- generic CRUD
def _register(res: str) -> None:
    def list_(status: str | None = None, test: str | None = None, p: Patient = Depends(current_patient),
              db: Session = Depends(get_db)):
        if res == "results":
            return R.list_results(db, p.id, norm_key(test) if test else None)
        items = R.LISTERS[res](db, p.id)
        if status and res in ("medications", "allergies", "history", "vaccinations"):
            items = [i for i in items if i.get("status") == status]
        return items

    def create(body: dict = Body(...), p: Patient = Depends(current_patient), db: Session = Depends(get_db)):
        row = _guard(lambda: W.create_record(db, p, res, body))
        db.commit()
        return SER[res](row)

    def read(rec_id: str, p: Patient = Depends(current_patient), db: Session = Depends(get_db)):
        return SER[res](_guard(lambda: W.get_owned(db, res, p.id, rec_id)))

    def update(rec_id: str, body: dict = Body(...), p: Patient = Depends(current_patient),
               db: Session = Depends(get_db)):
        row = _guard(lambda: W.update_record(db, p, res, rec_id, body))
        db.commit()
        return SER[res](row)

    def delete(rec_id: str, p: Patient = Depends(current_patient), db: Session = Depends(get_db)):
        _guard(lambda: W.delete_record(db, p, res, rec_id))
        db.commit()
        return {"ok": True}

    base = f"/{res}"
    router.add_api_route(base, list_, methods=["GET"], name=f"med_{res}_list")
    router.add_api_route(base, create, methods=["POST"], status_code=201, name=f"med_{res}_create")
    router.add_api_route(base + "/{rec_id}", read, methods=["GET"], name=f"med_{res}_read")
    router.add_api_route(base + "/{rec_id}", update, methods=["PUT"], name=f"med_{res}_update")
    router.add_api_route(base + "/{rec_id}", delete, methods=["DELETE"], name=f"med_{res}_delete")


for _res in W.MODELS:
    _register(_res)
