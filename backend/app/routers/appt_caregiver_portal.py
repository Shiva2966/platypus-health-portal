"""Caregiver-facing API + page (W9).  A caregiver has their OWN account/cookie (hp_caregiver), never a patient session.

Every data route calls care.active_link(...) which re-checks revocation, expiry and scope on EVERY request, and
writes an audit entry.  Write actions also notify the patient.
"""
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models.appointments import Appointment, CaregiverAccount, CaregiverLink
from app.models.shared import Patient, Provider
from app.security import throttle_check, throttle_fail, throttle_reset
from app.services import appointments as svc
from app.services import appt_care as care

router = APIRouter(tags=["caregiver-portal"])
PAGE = Path(__file__).resolve().parent.parent / "static" / "caregiver" / "index.html"


def _http(e: Exception) -> HTTPException:
    if isinstance(e, care.CareError):
        return HTTPException(e.status, e.message)
    if isinstance(e, svc.ApptNotFound):
        return HTTPException(404, "Not found.")
    if isinstance(e, svc.InvalidTransition):
        return HTTPException(409, str(e))
    raise e


def current_caregiver(request: Request, db: Session = Depends(get_db)) -> CaregiverAccount:
    acct = care.session_account(db, request.cookies.get(care.CAREGIVER_COOKIE))
    if acct is None:
        raise HTTPException(401, "Please sign in as a caregiver.")
    return acct


@router.get("/caregiver", include_in_schema=False)
def caregiver_page():
    return FileResponse(PAGE, headers={"Cache-Control": "no-cache"})


class AcceptIn(BaseModel):
    token: str = Field(default="", max_length=200)
    name: str | None = Field(default=None, max_length=200)
    password: str = Field(default="", max_length=200)


class TokenIn(BaseModel):
    token: str = Field(default="", max_length=200)


class LoginIn(BaseModel):
    email: str = Field(default="", max_length=254)
    password: str = Field(default="", max_length=200)


class ApptIn(BaseModel):
    provider_id: str | None = None
    intake: dict = Field(default_factory=dict)
    submit: bool = True


class RescheduleIn(BaseModel):
    preferred_times: str = Field(default="", max_length=500)
    reason: str | None = Field(default=None, max_length=500)


def _set_cookie(request: Request, response: Response, token: str) -> None:
    import os
    secure = request.url.scheme == "https" or os.environ.get("HP_SECURE_COOKIES") == "1"
    response.set_cookie(care.CAREGIVER_COOKIE, token, max_age=care.SESSION_HOURS * 3600, httponly=True, samesite="lax",
                        secure=secure, path="/")


# ---------------- invitation + sign-in ----------------

@router.get("/api/caregiver/invite/{token}")
def invite_preview(token: str, db: Session = Depends(get_db)):
    try:
        return care.invite_preview(db, token)
    except care.CareError as e:
        raise _http(e)


@router.post("/api/caregiver/accept")
def accept(body: AcceptIn, request: Request, response: Response, db: Session = Depends(get_db)):
    throttle_check("cg-accept:" + (request.client.host if request.client else "?"), limit=10)
    try:
        acct, link = care.accept_invite(db, token=body.token, name=body.name, password=body.password)
        session = care.create_session(db, acct)
        db.commit()
    except care.CareError as e:
        db.rollback()
        throttle_fail("cg-accept:" + (request.client.host if request.client else "?"))
        raise _http(e)
    _set_cookie(request, response, session)
    return {"ok": True, "name": acct.name, "patient_id": link.patient_id}


@router.post("/api/caregiver/decline")
def decline(body: TokenIn, db: Session = Depends(get_db)):
    try:
        care.decline_invite(db, body.token)
        db.commit()
    except care.CareError as e:
        db.rollback()
        raise _http(e)
    return {"ok": True}


@router.post("/api/caregiver/login")
def login(body: LoginIn, request: Request, response: Response, db: Session = Depends(get_db)):
    key = "cg-login:" + (body.email or "").strip().lower()
    throttle_check(key)
    try:
        acct = care.login(db, body.email, body.password)
        session = care.create_session(db, acct)
        db.commit()
    except care.CareError as e:
        db.rollback()
        throttle_fail(key)
        raise _http(e)
    throttle_reset(key)
    _set_cookie(request, response, session)
    return {"ok": True, "name": acct.name}


@router.post("/api/caregiver/logout")
def logout(request: Request, response: Response, db: Session = Depends(get_db)):
    care.end_session(db, request.cookies.get(care.CAREGIVER_COOKIE))
    db.commit()
    response.delete_cookie(care.CAREGIVER_COOKIE, path="/")
    return {"ok": True}


@router.get("/api/caregiver/me")
def me(cg: CaregiverAccount = Depends(current_caregiver)):
    return {"id": cg.id, "name": cg.name, "email": cg.email}


# ---------------- patients I can help ----------------

def _link_view(db: Session, link: CaregiverLink) -> dict:
    p = db.get(Patient, link.patient_id)
    d = care.link_dict(link)
    return {"patient_id": link.patient_id, "patient_name": p.display_name if p else "", "relationship": d["relationship"],
            "record_categories": [{"key": c, "label": care.RECORD_CATEGORIES[c][1]} for c in d["record_categories"]],
            "manage_appointments": d["manage_appointments"], "manage_bills": d["manage_bills"], "access_until": d["expires_at"]}


@router.get("/api/caregiver/patients")
def my_patients(cg: CaregiverAccount = Depends(current_caregiver), db: Session = Depends(get_db)):
    return {"items": [_link_view(db, l) for l in care.active_links_for(db, cg.id)]}


@router.get("/api/caregiver/patients/{pid}")
def overview(pid: str, cg: CaregiverAccount = Depends(current_caregiver), db: Session = Depends(get_db)):
    try:
        link = care.active_link(db, cg, pid)
    except care.CareError as e:
        raise _http(e)
    care.audit(db, link=link, caregiver=cg, action="caregiver_viewed_overview", resource_type="patient", resource_id=pid)
    db.commit()
    return _link_view(db, link)


@router.get("/api/caregiver/patients/{pid}/records/{category}")
def records(pid: str, category: str, cg: CaregiverAccount = Depends(current_caregiver), db: Session = Depends(get_db)):
    try:
        link = care.active_link(db, cg, pid, f"records:{category}")
        items = care.records_for(db, link, category)
    except care.CareError as e:
        raise _http(e)
    care.audit(db, link=link, caregiver=cg, action="caregiver_viewed_records", resource_type=category,
               detail={"category": category, "count": len(items)})
    db.commit()
    return {"category": category, "label": care.RECORD_CATEGORIES[category][1], "items": items}


@router.get("/api/caregiver/patients/{pid}/bills")
def bills(pid: str, cg: CaregiverAccount = Depends(current_caregiver), db: Session = Depends(get_db)):
    try:
        link = care.active_link(db, cg, pid, "bills")
    except care.CareError as e:
        raise _http(e)
    data = care.bills_for(db, link)
    care.audit(db, link=link, caregiver=cg, action="caregiver_viewed_bills", resource_type="bills",
               detail={"bills": len(data.get("bills", [])), "claims": len(data.get("claims", []))})
    db.commit()
    return data


# ---------------- appointments on behalf of the patient ----------------

def _appt(db: Session, link: CaregiverLink, aid: str) -> Appointment:
    a = db.get(Appointment, aid)
    if a is None or a.patient_id != link.patient_id:
        raise HTTPException(404, "Not found.")
    return a


@router.get("/api/caregiver/patients/{pid}/providers")
def providers(pid: str, q: str = "", cg: CaregiverAccount = Depends(current_caregiver), db: Session = Depends(get_db)):
    try:
        care.active_link(db, cg, pid, "appointments")
    except care.CareError as e:
        raise _http(e)
    stmt = select(Provider)
    q = q.strip().lower()[:100]
    if q:
        like = "%" + q.replace("%", "").replace("_", "") + "%"
        stmt = stmt.where(or_(func.lower(Provider.name).like(like), func.lower(func.coalesce(Provider.specialty, "")).like(like)))
    return {"items": [{"id": r.id, "name": r.name, "specialty": r.specialty} for r in db.scalars(stmt.order_by(Provider.name).limit(50))]}


@router.get("/api/caregiver/patients/{pid}/appointments")
def list_appts(pid: str, cg: CaregiverAccount = Depends(current_caregiver), db: Session = Depends(get_db)):
    try:
        link = care.active_link(db, cg, pid, "appointments")
    except care.CareError as e:
        raise _http(e)
    rows = svc.list_for_patient(db, pid)
    items = [svc.to_dict(db, a) for a in rows]
    care.audit(db, link=link, caregiver=cg, action="caregiver_viewed_appointments", resource_type="appointment",
               detail={"count": len(items)})
    db.commit()
    return {"items": items, "disclaimer": svc.NOT_A_DIAGNOSIS}


@router.post("/api/caregiver/patients/{pid}/appointments", status_code=201)
def create_appt(pid: str, body: ApptIn, cg: CaregiverAccount = Depends(current_caregiver), db: Session = Depends(get_db)):
    try:
        link = care.active_link(db, cg, pid, "appointments")
        a = svc.create(db, patient_id=pid, provider_id=body.provider_id, intake=body.intake, submit=body.submit,
                       actor_type="caregiver", actor_id=cg.id)
        care.tell_patient(db, link, cg, "requested an appointment" if body.submit else "started an appointment request")
        db.commit()
    except (care.CareError, svc.ApptNotFound, svc.InvalidTransition) as e:
        db.rollback()
        raise _http(e)
    return svc.to_dict(db, a, history=True)


def _act(pid: str, aid: str, cg: CaregiverAccount, db: Session, fn, title: str):
    try:
        link = care.active_link(db, cg, pid, "appointments")
        a = _appt(db, link, aid)
        fn(a)
        care.tell_patient(db, link, cg, title)
        db.commit()
    except (care.CareError, svc.ApptNotFound, svc.InvalidTransition) as e:
        db.rollback()
        raise _http(e)
    return svc.to_dict(db, a, history=True)


@router.post("/api/caregiver/patients/{pid}/appointments/{aid}/cancel")
def cancel_appt(pid: str, aid: str, cg: CaregiverAccount = Depends(current_caregiver), db: Session = Depends(get_db)):
    return _act(pid, aid, cg, db, lambda a: svc.cancel(db, a, actor_type="caregiver", actor_id=cg.id), "cancelled an appointment")


@router.post("/api/caregiver/patients/{pid}/appointments/{aid}/reschedule")
def reschedule_appt(pid: str, aid: str, body: RescheduleIn, cg: CaregiverAccount = Depends(current_caregiver),
                    db: Session = Depends(get_db)):
    return _act(pid, aid, cg, db, lambda a: svc.request_reschedule(db, a, preferred_times=body.preferred_times, reason=body.reason,
                                                                    actor_type="caregiver", actor_id=cg.id),
                "asked to reschedule an appointment")


@router.post("/api/caregiver/patients/{pid}/appointments/{aid}/confirm")
def confirm_appt(pid: str, aid: str, cg: CaregiverAccount = Depends(current_caregiver), db: Session = Depends(get_db)):
    return _act(pid, aid, cg, db, lambda a: svc.confirm_proposed_time(db, a, actor_type="caregiver", actor_id=cg.id),
                "accepted a new appointment time")
