"""W7 correction-request workflow (patient side).  Stored in ``med_corrections``; staff see/resolve them later through
``app.services.medical_write.resolve_correction`` / ``medical_read.list_corrections`` after W2/W3 authorization.

  GET    /api/med/corrections[?status=open]
  POST   /api/med/corrections            {record_type, record_id, message}
  POST   /api/med/corrections/{id}/withdraw
"""
from __future__ import annotations

from fastapi import APIRouter, Body, Depends, HTTPException
from sqlalchemy.orm import Session

from app.db import get_db
from app.models.shared import Patient
from app.security import current_patient
from app.services import medical_read as R
from app.services import medical_write as W

router = APIRouter(prefix="/api/med/corrections", tags=["medical"])


@router.get("")
def list_(status: str | None = None, p: Patient = Depends(current_patient), db: Session = Depends(get_db)):
    return R.list_corrections(db, p.id, status)


@router.post("", status_code=201)
def create(body: dict = Body(...), p: Patient = Depends(current_patient), db: Session = Depends(get_db)):
    try:
        c = W.create_correction(db, p, body)
    except W.NotFound as e:
        raise HTTPException(404, "That record was not found.") from e
    db.commit()
    return R.ser_correction(c)


@router.post("/{correction_id}/withdraw")
def withdraw(correction_id: str, p: Patient = Depends(current_patient), db: Session = Depends(get_db)):
    try:
        c = W.withdraw_correction(db, p, correction_id)
    except W.NotFound as e:
        raise HTTPException(404, "Correction request not found.") from e
    db.commit()
    return R.ser_correction(c)
