"""Staff view of a patient's billing (W8): ONLY with an active share grant of category `billing`.

Billing is private to the patient by default. Without a grant the answer is 403 "No shared billing records".
Every attempt is audited; successful views notify the patient. The patient's private notes are never included.
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.db import get_db
from app.models.staff import StaffUser
from app.services import billing_read
from app.services.staff_portal import require_confirmed_patient
from app.services.staff_security import require_role

router = APIRouter(prefix="/api/staff", tags=["billing-staff"])


@router.get("/patients/{patient_id}/billing")
def patient_billing(patient_id: str, staff: StaffUser = Depends(require_role("front_desk", "nurse", "physician")),
                    db: Session = Depends(get_db)):
    require_confirmed_patient(db, staff, patient_id)  # 403 until the staff member confirmed the patient's DOB
    try:
        return billing_read.get_billing_for_staff(db, staff_id=staff.id, staff_role=staff.role, patient_id=patient_id)
    except PermissionError as e:
        raise HTTPException(403, str(e))
