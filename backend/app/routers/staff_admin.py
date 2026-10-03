"""Admin-only: staff audit log viewer (with filters) and staff account management."""
import json
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models.shared import AuditLog, Patient, Provider
from app.models.staff import STAFF_ROLES, StaffSession, StaffUser
from app.services.auth_passwords import hash_password
from app.services.audit import log_event
from app.services.staff_security import require_role

router = APIRouter(prefix="/api/staff/admin", tags=["staff-admin"])
Admin = Depends(require_role("admin"))


def _parse_day(s: str | None, end: bool = False) -> datetime | None:
    if not s:
        return None
    try:
        d = datetime.fromisoformat(s)
    except ValueError:
        raise HTTPException(422, "Dates must look like 2026-10-02.")
    return d + timedelta(days=1) if end and len(s) <= 10 else d


@router.get("/audit")
def audit_log(actor_type: str | None = None, actor_id: str | None = None, patient_id: str | None = None,
              action: str | None = None, since: str | None = None, until: str | None = None,
              q: str | None = Query(default=None, max_length=100),
              limit: int = Query(default=50, ge=1, le=200), offset: int = Query(default=0, ge=0),
              admin: StaffUser = Admin, db: Session = Depends(get_db)):
    stmt = select(AuditLog)
    if actor_type:
        stmt = stmt.where(AuditLog.actor_type == actor_type)
    if actor_id:
        stmt = stmt.where(AuditLog.actor_id == actor_id)
    if patient_id:
        stmt = stmt.where(AuditLog.patient_id == patient_id)
    if action:
        stmt = stmt.where(AuditLog.action == action)
    if (d := _parse_day(since)):
        stmt = stmt.where(AuditLog.ts >= d)
    if (d := _parse_day(until, end=True)):
        stmt = stmt.where(AuditLog.ts < d)
    if q:
        like = f"%{q.strip()}%"
        stmt = stmt.where(or_(AuditLog.action.ilike(like), AuditLog.detail.ilike(like),
                              AuditLog.resource_type.ilike(like)))
    total = db.scalar(select(func.count()).select_from(stmt.subquery()))
    rows = db.scalars(stmt.order_by(AuditLog.id.desc()).limit(limit).offset(offset)).all()
    staff_names = {s.id: f"{s.name} ({s.role})" for s in db.scalars(select(StaffUser)).all()}
    pids = {r.patient_id for r in rows if r.patient_id}
    patient_names = {p.id: p.legal_name for p in db.scalars(select(Patient).where(Patient.id.in_(pids or {""}))).all()}
    items = []
    for r in rows:
        try:
            detail = json.loads(r.detail) if r.detail else None
        except ValueError:
            detail = r.detail
        items.append({"id": r.id, "ts": r.ts.isoformat() + "Z", "actor_type": r.actor_type, "actor_id": r.actor_id,
                      "actor_name": staff_names.get(r.actor_id) if r.actor_type == "staff" else
                      (patient_names.get(r.actor_id) if r.actor_type == "patient" else "System"),
                      "patient_id": r.patient_id, "patient_name": patient_names.get(r.patient_id),
                      "action": r.action, "resource_type": r.resource_type, "resource_id": r.resource_id,
                      "detail": detail})
    return {"total": total, "limit": limit, "offset": offset, "items": items}


@router.get("/audit/actions")
def audit_actions(admin: StaffUser = Admin, db: Session = Depends(get_db)):
    return {"actions": sorted(db.scalars(select(AuditLog.action).distinct()).all())}


# ---------------------------------------------------------------- staff management

class StaffCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=10, max_length=200)
    role: str
    provider_id: str | None = None


class StaffUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=200)
    role: str | None = None
    provider_id: str | None = None
    active: bool | None = None
    password: str | None = Field(default=None, min_length=10, max_length=200)


class ProviderCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    specialty: str | None = Field(default=None, max_length=100)
    address: str | None = Field(default=None, max_length=300)
    phone: str | None = Field(default=None, max_length=50)


def _check_role(role: str) -> None:
    if role not in STAFF_ROLES:
        raise HTTPException(422, f"Role must be one of: {', '.join(STAFF_ROLES)}.")


def _check_provider(db: Session, provider_id: str | None) -> None:
    if provider_id and db.get(Provider, provider_id) is None:
        raise HTTPException(422, "Unknown organization.")


@router.get("/users")
def list_users(admin: StaffUser = Admin, db: Session = Depends(get_db)):
    provs = {p.id: p.name for p in db.scalars(select(Provider)).all()}
    users = db.scalars(select(StaffUser).order_by(StaffUser.name)).all()
    return {"users": [{**u.to_dict(), "provider_name": provs.get(u.provider_id)} for u in users],
            "roles": list(STAFF_ROLES), "providers": [{"id": k, "name": v} for k, v in provs.items()]}


@router.post("/providers", status_code=201)
def create_provider(body: ProviderCreate, admin: StaffUser = Admin, db: Session = Depends(get_db)):
    name = body.name.strip()
    if not name:
        raise HTTPException(422, "Enter the organization name.")
    if db.scalar(select(Provider).where(func.lower(Provider.name) == name.lower())):
        raise HTTPException(409, "An organization with that name already exists.")
    p = Provider(name=name, specialty=(body.specialty or "").strip() or None, address=(body.address or "").strip() or None,
                 phone=(body.phone or "").strip() or None, availability_json="[]", cost_estimates_json="[]")
    db.add(p)
    db.flush()
    if admin.provider_id is None:  # first organization on a fresh install: the admin joins it
        admin.provider_id = p.id
    log_event(db, actor_type="staff", actor_id=admin.id, patient_id=None, action="provider_created",
              resource_type="provider", resource_id=p.id, detail={"name": p.name})
    db.commit()
    return {"id": p.id, "name": p.name}


@router.post("/users", status_code=201)
def create_user(body: StaffCreate, admin: StaffUser = Admin, db: Session = Depends(get_db)):
    _check_role(body.role)
    _check_provider(db, body.provider_id)
    email = body.email.strip().lower()
    if db.scalar(select(StaffUser).where(StaffUser.email == email)):
        raise HTTPException(409, "A staff account with that email already exists.")
    u = StaffUser(name=body.name.strip(), email=email, password_hash=hash_password(body.password),
                  role=body.role, provider_id=body.provider_id or admin.provider_id, active=True)
    db.add(u)
    db.flush()
    log_event(db, actor_type="staff", actor_id=admin.id, patient_id=None, action="staff_created",
              resource_type="staff_user", resource_id=u.id, detail={"role": u.role, "email": u.email})
    db.commit()
    return u.to_dict()


@router.patch("/users/{user_id}")
def update_user(user_id: str, body: StaffUpdate, admin: StaffUser = Admin, db: Session = Depends(get_db)):
    u = db.get(StaffUser, user_id)
    if u is None:
        raise HTTPException(404, "Staff account not found.")
    changes = {}
    if body.name is not None:
        u.name = body.name.strip() or u.name
        changes["name"] = u.name
    if body.role is not None:
        _check_role(body.role)
        if u.id == admin.id and body.role != "admin":
            raise HTTPException(409, "You cannot remove your own admin role.")
        if u.role != body.role:
            changes["role"] = body.role
        u.role = body.role
    if body.provider_id is not None:
        _check_provider(db, body.provider_id)
        u.provider_id = body.provider_id or None
        changes["provider_id"] = u.provider_id
    if body.active is not None:
        if u.id == admin.id and not body.active:
            raise HTTPException(409, "You cannot deactivate your own account.")
        u.active = body.active
        changes["active"] = u.active
    if body.password:
        u.password_hash = hash_password(body.password)
        changes["password"] = "reset"
    if changes.get("active") is False or "role" in changes or "password" in changes:
        for s in db.scalars(select(StaffSession).where(StaffSession.staff_id == u.id)).all():
            db.delete(s)  # force re-login so the change takes effect immediately
    log_event(db, actor_type="staff", actor_id=admin.id, patient_id=None, action="staff_updated",
              resource_type="staff_user", resource_id=u.id, detail=changes)
    db.commit()
    return u.to_dict()
