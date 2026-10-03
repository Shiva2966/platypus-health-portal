"""Staff login/logout/me. Staff cookie (`hp_staff`) is separate from the patient cookie."""
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models.shared import Provider
from app.models.staff import StaffUser
from app.security import throttle_check, throttle_fail, throttle_reset
from app.services.auth_passwords import burn_password_time, verify_password
from app.services.audit import log_event
from app.services.staff_security import (ROLE_CAPS, create_staff_session, current_staff,
                                         destroy_staff_session)

router = APIRouter(prefix="/api/staff", tags=["staff"])


class LoginIn(BaseModel):
    email: str
    password: str


def staff_payload(db: Session, s: StaffUser) -> dict:
    prov = db.get(Provider, s.provider_id) if s.provider_id else None
    d = s.to_dict()
    d["provider_name"] = prov.name if prov else None
    d["capabilities"] = sorted(ROLE_CAPS.get(s.role, set()))
    return d


@router.post("/login")
def login(body: LoginIn, request: Request, response: Response, db: Session = Depends(get_db)):
    email = body.email.strip().lower()
    key = f"staff-login:{email}"
    throttle_check(key)
    staff = db.scalar(select(StaffUser).where(StaffUser.email == email))
    if staff is None:
        burn_password_time(body.password)
    if staff is None or not staff.active or not verify_password(body.password, staff.password_hash):
        throttle_fail(key)
        if staff is not None:
            log_event(db, actor_type="staff", actor_id=staff.id, patient_id=None, action="staff_login_failed")
            db.commit()
        raise HTTPException(401, "Email or password is incorrect.")
    throttle_reset(key)
    create_staff_session(db, staff, request, response)
    log_event(db, actor_type="staff", actor_id=staff.id, patient_id=None, action="staff_login")
    db.commit()
    return staff_payload(db, staff)


@router.post("/logout")
def logout(request: Request, response: Response, db: Session = Depends(get_db)):
    destroy_staff_session(db, request, response)
    return {"ok": True}


@router.get("/me")
def me(staff: StaffUser = Depends(current_staff), db: Session = Depends(get_db)):
    return staff_payload(db, staff)
