"""Prescriptions view (patient-only): combines prescription documents with
medications that have a prescriber recorded.  No new database tables.

  GET /api/med/prescriptions
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models.shared import Patient
from app.security import current_patient
from app.services import medical_read as R

router = APIRouter(tags=["medical"])


@router.get("/api/med/prescriptions")
def list_prescriptions(p: Patient = Depends(current_patient), db: Session = Depends(get_db)):
    """Combined view: medications that have a prescriber name recorded plus
    uploaded prescription documents.  Read-only aggregation of existing data."""
    medications = [m for m in R.list_medications(db, p.id) if m.get("prescriber_name")]

    documents: list[dict] = []
    try:
        from app.models.documents import Document
        rows = db.execute(
            select(Document.id, Document.name, Document.description, Document.created_at)
            .where(Document.patient_id == p.id, Document.category == "prescription",
                   Document.deleted_at.is_(None))
            .order_by(Document.created_at.desc())
            .limit(100)
        ).all()
        documents = [{"id": r.id, "name": r.name, "description": r.description,
                      "created_at": R.iso(r.created_at)} for r in rows]
    except ImportError:
        pass

    return {"medications": medications, "documents": documents}
