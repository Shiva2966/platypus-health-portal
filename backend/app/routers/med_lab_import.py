"""POST /api/med/results/parse-document : read an already-uploaded PDF (the patient's own document) and return a
PREVIEW of lab rows. Nothing is saved here; the page saves confirmed rows through the normal results API."""
from __future__ import annotations

from fastapi import APIRouter, Body, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models.documents import Document, DocumentBlob
from app.models.shared import Patient
from app.security import current_patient
from app.services import lab_pdf
from app.services.audit import log_event

router = APIRouter(prefix="/api/med", tags=["medical"])


@router.post("/results/parse-document")
def parse_document(body: dict = Body(...), p: Patient = Depends(current_patient), db: Session = Depends(get_db)):
    doc_id = str(body.get("document_id") or "")
    doc = db.scalar(select(Document).where(Document.id == doc_id, Document.patient_id == p.id,
                                           Document.deleted_at.is_(None)))
    if doc is None:
        raise HTTPException(404, "Document not found.")
    blob = db.get(DocumentBlob, doc.id)
    data = bytes(blob.data) if blob is not None else b""
    if not data.startswith(b"%PDF"):
        raise HTTPException(400, "Only PDF reports can be read automatically.")
    text = lab_pdf.extract_text(data)
    rows = lab_pdf.parse_rows(text) if text.strip() else []
    log_event(db, actor_type="patient", actor_id=p.id, patient_id=p.id, action="lab_pdf_parsed",
              resource_type="document", resource_id=doc.id, detail={"rows_found": len(rows)})
    db.commit()
    warning = None
    if not text.strip():
        warning = "This PDF looks like a scan or photo, so the values can't be read automatically. Your PDF is saved; you can add values by hand."
    elif not rows:
        warning = "We couldn't find lab values in this PDF. Your PDF is saved; you can add values by hand."
    return {"document_id": doc.id, "document_name": doc.name, "rows": rows, "warning": warning}
