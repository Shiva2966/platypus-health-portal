"""Notifications for BOTH patients and staff, notification preferences, SSE stream and the patient's
own access history (/api/audit/mine). (Reminder sweeps run in app/scheduler.py.)

Clients: polling /api/notifications/unread-count is the reliable baseline; the SSE stream is an optional
speed-up that clients may drop at any time.

Which session is used? `?as=staff` -> only the staff cookie; `?as=patient` -> only the patient cookie;
no hint -> patient cookie first, then staff cookie. A patient can never read staff notifications
(and vice-versa) because rows are always filtered by (recipient_type, recipient_id) of the session.
"""
import asyncio
import json

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import SessionLocal, get_db
from app.models.shared import AuditLog, Notification, Provider, utcnow
from app.models.staff import StaffUser
from app.security import current_patient
from app.services import notif_helpers as nh
from app.services.staff_security import staff_from_request


router = APIRouter()


# ---------------------------------------------------------------- who is calling?

def resolve_principal(request: Request, db: Session, as_: str | None) -> tuple[str, str]:
    if as_ not in (None, "", "patient", "staff"):
        raise HTTPException(422, "`as` must be 'patient' or 'staff'.")
    if as_ in (None, "", "patient"):
        try:
            return "patient", current_patient(request, db).id
        except HTTPException:
            if as_ == "patient":
                raise
    staff = staff_from_request(request, db)
    if staff:
        return "staff", staff.id
    raise HTTPException(401, "Please sign in.")


def principal(request: Request, as_: str | None = Query(default=None, alias="as"),
              db: Session = Depends(get_db)) -> tuple[str, str]:
    return resolve_principal(request, db, as_)


# ---------------------------------------------------------------- notifications

@router.get("/api/notifications")
def list_notifications(unread: bool = False, kind: str | None = None,
                       limit: int = Query(default=30, ge=1, le=100), offset: int = Query(default=0, ge=0),
                       who: tuple[str, str] = Depends(principal), db: Session = Depends(get_db)):
    rtype, rid = who
    q = nh.visible_query(db, rtype, rid)
    if unread:
        q = q.where(Notification.read_at.is_(None))
    if kind:
        q = q.where(Notification.kind == kind)
    total = db.scalar(select(func.count()).select_from(q.subquery()))
    rows = db.scalars(q.order_by(Notification.id.desc()).limit(limit).offset(offset)).all()
    return {"items": [nh.notif_dict(n) for n in rows], "total": total,
            "unread": nh.unread_count(db, rtype, rid), "kinds": nh.KINDS[rtype],
            "kind_labels": {k: nh.KIND_LABELS.get(k, k) for k in nh.KINDS[rtype]}, "recipient_type": rtype}


@router.get("/api/notifications/unread-count")
def unread(who: tuple[str, str] = Depends(principal), db: Session = Depends(get_db)):
    n = nh.unread_count(db, *who)
    return {"unread": n, "count": n}  # `count` is what W1's dashboard reads


@router.post("/api/notifications/read-all")
def read_all(who: tuple[str, str] = Depends(principal), db: Session = Depends(get_db)):
    n = nh.mark_all_read(db, *who)
    db.commit()
    return {"marked": n, "unread": 0}


class PrefsIn(BaseModel):
    prefs: dict[str, bool]


@router.get("/api/notifications/prefs")
def get_prefs(who: tuple[str, str] = Depends(principal), db: Session = Depends(get_db)):
    return {"prefs": nh.get_prefs(db, *who)}


@router.put("/api/notifications/prefs")
def put_prefs(body: PrefsIn, who: tuple[str, str] = Depends(principal), db: Session = Depends(get_db)):
    try:
        nh.set_prefs(db, *who, body.prefs)
    except ValueError as e:
        raise HTTPException(422, str(e))
    db.commit()
    return {"prefs": nh.get_prefs(db, *who)}


@router.post("/api/notifications/{notif_id}/read")
def mark_read(notif_id: int, who: tuple[str, str] = Depends(principal), db: Session = Depends(get_db)):
    n = db.get(Notification, notif_id)
    if n is None or (n.recipient_type, n.recipient_id) != who:
        raise HTTPException(404, "Notification not found.")  # same answer for other people's rows
    if n.read_at is None:
        n.read_at = utcnow()
        db.commit()
    return {"ok": True, "unread": nh.unread_count(db, *who)}


def _count_for(rtype: str, rid: str) -> int:
    db = SessionLocal()
    try:
        return nh.unread_count(db, rtype, rid)
    finally:
        db.close()


@router.get("/api/notifications/stream")
async def stream(request: Request, as_: str | None = Query(default=None, alias="as"), once: bool = False):
    """Server-Sent Events: pushes `unread` events when the count changes. Ends after ~60s so clients
    reconnect (EventSource does this automatically); the UI also polls every 15s as a fallback."""
    def who():
        db = SessionLocal()
        try:
            return resolve_principal(request, db, as_)
        finally:
            db.close()

    rtype, rid = await run_in_threadpool(who)

    async def gen():
        last = None
        for _ in range(1 if once else 12):
            count = await run_in_threadpool(_count_for, rtype, rid)
            if count != last:
                last = count
                yield f"event: unread\ndata: {json.dumps({'unread': count})}\n\n"
            else:
                yield ": keep-alive\n\n"
            if once or await request.is_disconnected():
                return
            await asyncio.sleep(5)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


# ---------------------------------------------------------------- patient access history

ACTION_LABELS = {
    "document_viewed": "viewed a document", "document_access_denied": "tried to open a document without permission",
    "access_requested": "requested access to your records", "access_approved": "access request approved",
    "access_denied": "access request declined", "share_granted": "sharing started",
    "share_revoked": "sharing revoked", "share_token_created": "share code created",
    "share_token_redeemed": "used a share code", "share_token_revoked": "share code revoked",
    "share_token_rejected": "tried an invalid share code", "patient_record_opened": "opened your patient page",
    "appointment_viewed": "viewed your appointment request", "appointment_accept": "confirmed your appointment",
    "appointment_reschedule": "proposed a new appointment time", "appointment_decline": "declined your appointment request",
    "intake_exported": "exported your intake form",
    "record_viewed": "viewed your health data", "record_access_denied": "tried to open health data without permission",
    "billing_viewed": "viewed your billing", "billing_access_denied": "tried to open billing without permission",
    "med_corrections_viewed": "viewed your correction requests",
    "med_correction_resolved": "resolved your correction request",
    "med_correction_declined": "declined your correction request",
    **{f"med_{r}_confirmed": f"confirmed an entry in your {lbl}" for r, lbl in (
        ("history", "medical history"), ("medications", "medications"), ("allergies", "allergies"),
        ("vaccinations", "vaccinations"), ("results", "test results"))},
}
HIDDEN_FROM_PATIENT = {"dob_confirmed", "dob_check_failed"}
ROLE_LABELS = {"front_desk": "Front desk", "nurse": "Nurse", "physician": "Physician", "admin": "Administrator"}


def _safe_detail(d) -> dict:
    if not isinstance(d, dict):
        return {}
    keep = ("provider", "document", "scope", "categories", "purpose", "expires_at", "duration_days",
            "scheduled_for", "status", "documents", "role")
    return {k: d[k] for k in keep if k in d}


@router.get("/api/audit/mine")
def my_access_history(who: str = Query(default="all", pattern="^(all|staff|me)$"),
                      limit: int = Query(default=50, ge=1, le=200), offset: int = Query(default=0, ge=0),
                      patient=Depends(current_patient), db: Session = Depends(get_db)):
    """Who accessed what and when, for the signed-in patient only."""
    q = select(AuditLog).where(AuditLog.patient_id == patient.id, AuditLog.action.not_in(HIDDEN_FROM_PATIENT))
    if who == "staff":
        q = q.where(AuditLog.actor_type != "patient")
    elif who == "me":
        q = q.where(AuditLog.actor_type == "patient")
    total = db.scalar(select(func.count()).select_from(q.subquery()))
    rows = db.scalars(q.order_by(AuditLog.id.desc()).limit(limit).offset(offset)).all()
    staff_ids = {r.actor_id for r in rows if r.actor_type == "staff" and r.actor_id}
    staff = {s.id: s for s in db.scalars(select(StaffUser).where(StaffUser.id.in_(staff_ids or {""}))).all()}
    provs = {p.id: p.name for p in db.scalars(select(Provider).where(
        Provider.id.in_({s.provider_id for s in staff.values() if s.provider_id} or {""}))).all()}
    items = []
    for r in rows:
        try:
            detail = json.loads(r.detail) if r.detail else {}
        except ValueError:
            detail = {}
        if r.actor_type == "patient":
            actor = "You"
        elif r.actor_type == "staff":
            s = staff.get(r.actor_id)
            actor = (f"{s.name} ({ROLE_LABELS.get(s.role, s.role)}), {provs.get(s.provider_id, 'a provider')}"
                     if s else (detail.get("provider") or "Hospital staff"))
        else:
            actor = "System"
        d = _safe_detail(detail)
        items.append({"id": r.id, "ts": r.ts.isoformat() + "Z", "actor_type": r.actor_type, "actor": actor,
                      "action": r.action, "action_label": ACTION_LABELS.get(r.action, r.action.replace("_", " ")),
                      "resource_type": r.resource_type, "document": d.get("document") or d.get("scope"),
                      "detail": d})
    return {"items": items, "total": total, "limit": limit, "offset": offset}
