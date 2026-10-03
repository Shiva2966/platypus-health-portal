"""Patient side of caregivers + dependents (W9).  Prefix /api/care.  The caregiver's own login/portal is
appt_caregiver_portal.py."""
import json

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models.shared import AuditLog, Patient
from app.security import current_patient
from app.services import appt_care as care

router = APIRouter(prefix="/api/care", tags=["caregivers"])


def _http(e: care.CareError) -> HTTPException:
    return HTTPException(e.status, e.message)


class InviteIn(BaseModel):
    email: str = Field(default="", max_length=254)
    name: str | None = Field(default=None, max_length=200)
    relationship: str | None = Field(default=None, max_length=100)
    record_categories: list[str] = Field(default_factory=list, max_length=20)
    manage_appointments: bool = False
    manage_bills: bool = False
    access_days: int = 90


class ScopeIn(BaseModel):
    record_categories: list[str] = Field(default_factory=list, max_length=20)
    manage_appointments: bool = False
    manage_bills: bool = False
    access_days: int | None = None


class DependentIn(BaseModel):
    name: str = Field(default="", max_length=200)
    dob: str | None = Field(default=None, max_length=10)
    relationship: str = Field(default="", max_length=100)
    notes: str | None = Field(default=None, max_length=1000)


class DependentPatch(BaseModel):
    name: str | None = Field(default=None, max_length=200)
    dob: str | None = Field(default=None, max_length=10)
    relationship: str | None = Field(default=None, max_length=100)
    notes: str | None = Field(default=None, max_length=1000)


def _invite_result(link, token, mode):
    out = {"item": care.link_dict(link), "email_mode": mode}
    if care.dev_token_visible():  # same rule as W6's OTP: only when SMTP is not configured AND DEV_SHOW_OTP=1
        out["dev_invite_token"] = token
    return out


# ---------------- caregivers ----------------

@router.get("/caregivers")
def list_caregivers(p: Patient = Depends(current_patient), db: Session = Depends(get_db)):
    return {"items": [care.link_dict(l) for l in care.list_links(db, p.id)], "categories": care.categories_list(),
            "note": "A caregiver gets their own sign-in. They only see what you tick below, only until the end date, "
                    "and you can remove access at any time. Everything they do is recorded in your activity log."}


@router.post("/caregivers", status_code=201)
def invite(body: InviteIn, p: Patient = Depends(current_patient), db: Session = Depends(get_db)):
    try:
        link, token, mode = care.invite_caregiver(db, p, email=body.email, name=body.name, relationship=body.relationship,
                                                  record_categories=body.record_categories,
                                                  manage_appointments=body.manage_appointments,
                                                  manage_bills=body.manage_bills, access_days=body.access_days)
        db.commit()
    except care.CareError as e:
        db.rollback()
        raise _http(e)
    return _invite_result(link, token, mode)


@router.put("/caregivers/{lid}")
def change_scope(lid: str, body: ScopeIn, p: Patient = Depends(current_patient), db: Session = Depends(get_db)):
    try:
        link = care.own_link(db, p, lid)
        care.update_scope(db, p, link, record_categories=body.record_categories, manage_appointments=body.manage_appointments,
                          manage_bills=body.manage_bills, access_days=body.access_days)
        db.commit()
    except care.CareError as e:
        db.rollback()
        raise _http(e)
    return {"item": care.link_dict(link)}


@router.post("/caregivers/{lid}/resend")
def resend(lid: str, p: Patient = Depends(current_patient), db: Session = Depends(get_db)):
    try:
        link = care.own_link(db, p, lid)
        token, mode = care.resend_invite(db, p, link)
        db.commit()
    except care.CareError as e:
        db.rollback()
        raise _http(e)
    return _invite_result(link, token, mode)


@router.post("/caregivers/{lid}/revoke")
def revoke(lid: str, p: Patient = Depends(current_patient), db: Session = Depends(get_db)):
    try:
        link = care.own_link(db, p, lid)
        care.revoke(db, p, link)
        db.commit()
    except care.CareError as e:
        db.rollback()
        raise _http(e)
    return {"item": care.link_dict(link)}


@router.get("/activity")
def caregiver_activity(limit: int = 100, p: Patient = Depends(current_patient), db: Session = Depends(get_db)):
    """What caregivers (and you, about caregivers) did - newest first."""
    limit = max(1, min(limit, 300))
    rows = db.scalars(select(AuditLog).where(AuditLog.patient_id == p.id, AuditLog.action.like("caregiver\\_%", escape="\\"))
                      .order_by(AuditLog.ts.desc(), AuditLog.id.desc()).limit(limit)).all()
    items = []
    for r in rows:
        try:
            d = json.loads(r.detail or "{}")
        except ValueError:
            d = {}
        items.append({"ts": r.ts.isoformat() + "Z", "action": r.action, "by": d.get("caregiver_email") or "you",
                      "resource_type": r.resource_type, "detail": {k: v for k, v in d.items() if k not in ("caregiver_id",)}})
    return {"items": items}


# ---------------- dependents ----------------

@router.get("/dependents")
def list_dependents(p: Patient = Depends(current_patient), db: Session = Depends(get_db)):
    return {"items": [care.dependent_dict(db, d) for d in care.list_dependents(db, p.id)],
            "note": "A dependent profile lets you book and manage appointments for someone in your care, such as a child. "
                    "Notifications about them come to you."}


@router.post("/dependents", status_code=201)
def add_dependent(body: DependentIn, p: Patient = Depends(current_patient), db: Session = Depends(get_db)):
    try:
        prof = care.add_dependent(db, p, name=body.name, dob=body.dob, relationship=body.relationship, notes=body.notes)
        db.commit()
    except care.CareError as e:
        db.rollback()
        raise _http(e)
    return care.dependent_dict(db, prof)


@router.put("/dependents/{did}")
def update_dependent(did: str, body: DependentPatch, p: Patient = Depends(current_patient), db: Session = Depends(get_db)):
    try:
        prof = care.own_dependent(db, p, did)
        care.update_dependent(db, p, prof, name=body.name, dob=body.dob, relationship=body.relationship, notes=body.notes)
        db.commit()
    except care.CareError as e:
        db.rollback()
        raise _http(e)
    return care.dependent_dict(db, prof)


@router.delete("/dependents/{did}")
def remove_dependent(did: str, p: Patient = Depends(current_patient), db: Session = Depends(get_db)):
    try:
        prof = care.own_dependent(db, p, did)
        care.end_dependent(db, p, prof)
        db.commit()
    except care.CareError as e:
        db.rollback()
        raise _http(e)
    return {"ok": True}
