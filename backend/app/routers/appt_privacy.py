"""Privacy + accessibility settings, connected apps, export-all-my-data, deletion request (W9).  Prefix /api/settings."""
import json

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field, StrictBool, StrictInt
from sqlalchemy.orm import Session

from app.db import get_db
from app.models.shared import Patient
from app.security import current_patient
from app.services import appt_privacy as pv
from app.services.audit import log_event

router = APIRouter(prefix="/api/settings", tags=["privacy"])


class SettingsIn(BaseModel):
    default_share_days: StrictInt | None = None
    default_share_categories: list[str] | None = None
    share_requires_my_approval: StrictBool | None = None
    notify_on_view: StrictBool | None = None
    allow_email: StrictBool | None = None
    allow_sms: StrictBool | None = None
    allow_phone_calls: StrictBool | None = None
    allow_appointment_reminders: StrictBool | None = None
    allow_marketing: StrictBool | None = None
    text_size: str | None = None
    high_contrast: StrictBool | None = None
    reduced_motion: StrictBool | None = None


class AccessibilityIn(BaseModel):
    text_size: str | None = None
    high_contrast: StrictBool | None = None
    reduced_motion: StrictBool | None = None


class ConnectIn(BaseModel):
    app_key: str = Field(default="", max_length=50)


class DeletionIn(BaseModel):
    reason: str | None = Field(default=None, max_length=1000)
    confirm: bool = False


@router.get("/privacy")
def get_privacy(p: Patient = Depends(current_patient), db: Session = Depends(get_db)):
    s = pv.get_settings(db, p.id)
    db.commit()
    return {"settings": pv.settings_dict(s), "deletion_request": pv.deletion_dict(pv.pending_deletion(db, p.id)),
            "retention_explanation": pv.RETENTION_EXPLANATION,
            "note": "Nothing is shared with anyone unless you choose to share it. These are only your defaults."}


@router.put("/privacy")
def put_privacy(body: SettingsIn, p: Patient = Depends(current_patient), db: Session = Depends(get_db)):
    s = pv.update_settings(db, p, body.model_dump(exclude_unset=True))
    db.commit()
    return {"settings": pv.settings_dict(s)}


@router.get("/accessibility")
def get_accessibility(p: Patient = Depends(current_patient), db: Session = Depends(get_db)):
    s = pv.get_settings(db, p.id)
    db.commit()
    d = pv.settings_dict(s)
    return {k: d[k] for k in ("text_size", "high_contrast", "reduced_motion")}


@router.put("/accessibility")
def put_accessibility(body: AccessibilityIn, p: Patient = Depends(current_patient), db: Session = Depends(get_db)):
    s = pv.update_settings(db, p, body.model_dump(exclude_unset=True))
    db.commit()
    d = pv.settings_dict(s)
    return {k: d[k] for k in ("text_size", "high_contrast", "reduced_motion")}


# ---------------- connected apps ----------------

@router.get("/connected-apps")
def connected_apps(p: Patient = Depends(current_patient), db: Session = Depends(get_db)):
    return {"items": pv.list_apps(db, p.id), "available": pv.catalog(db, p.id),
            "note": "Demo only: these are pretend apps. In a real service each one would ask you to approve exactly what it can read."}


@router.post("/connected-apps", status_code=201)
def connect(body: ConnectIn, p: Patient = Depends(current_patient), db: Session = Depends(get_db)):
    try:
        row = pv.connect_app(db, p, body.app_key)
    except ValueError as e:
        db.rollback()
        raise HTTPException(409, str(e))
    db.commit()
    return {"id": row.id, "name": row.name}


@router.delete("/connected-apps/{app_id}")
def disconnect(app_id: str, p: Patient = Depends(current_patient), db: Session = Depends(get_db)):
    try:
        pv.revoke_app(db, p, app_id)
    except LookupError:
        raise HTTPException(404, "Not found.")
    db.commit()
    return {"ok": True}


# ---------------- export ----------------

@router.get("/export.json")
def export_json(p: Patient = Depends(current_patient), db: Session = Depends(get_db)):
    data = pv.export_all(db, p)
    log_event(db, actor_type="patient", actor_id=p.id, patient_id=p.id, action="data_exported", resource_type="export",
              detail={"format": "json"})
    db.commit()
    return Response(json.dumps(data, indent=2, default=str), media_type="application/json",
                    headers={"Content-Disposition": 'attachment; filename="my-health-data.json"'})


@router.get("/export.zip")
def export_zip(p: Patient = Depends(current_patient), db: Session = Depends(get_db)):
    blob, meta = pv.export_zip(db, p)
    log_event(db, actor_type="patient", actor_id=p.id, patient_id=p.id, action="data_exported", resource_type="export",
              detail={"format": "zip", **meta})
    db.commit()
    return Response(blob, media_type="application/zip",
                    headers={"Content-Disposition": 'attachment; filename="my-health-data.zip"'})


# ---------------- deletion workflow ----------------

@router.get("/deletion-request")
def deletion_status(p: Patient = Depends(current_patient), db: Session = Depends(get_db)):
    return {"request": pv.deletion_dict(pv.pending_deletion(db, p.id)), "retention_explanation": pv.RETENTION_EXPLANATION,
            "cooling_off_days": pv.COOLING_OFF_DAYS}


@router.post("/deletion-request", status_code=201)
def request_deletion(body: DeletionIn, p: Patient = Depends(current_patient), db: Session = Depends(get_db)):
    if not body.confirm:
        raise HTTPException(422, "Please confirm that you understand what deletion means.")
    try:
        r = pv.request_deletion(db, p, body.reason)
    except ValueError as e:
        db.rollback()
        raise HTTPException(409, str(e))
    db.commit()
    return {"request": pv.deletion_dict(r), "retention_explanation": pv.RETENTION_EXPLANATION}


@router.delete("/deletion-request")
def cancel_deletion(p: Patient = Depends(current_patient), db: Session = Depends(get_db)):
    try:
        pv.cancel_deletion(db, p)
    except LookupError as e:
        raise HTTPException(404, str(e))
    db.commit()
    return {"ok": True}
