"""Privacy + accessibility settings, connected apps, export-all-my-data, deletion-request workflow (W9).

Functions flush but never commit (caller commits).  Everything here is about the signed-in patient's OWN data.
"""
from __future__ import annotations

import io
import json
import re
import zipfile
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.errors import FieldError
from app.models import billing as bm
from app.models import core as cm
from app.models import documents as dm
from app.models import medical as mm
from app.models.appointments import (Appointment, AppointmentEvent, CaregiverLink, ConnectedApp, DeletionRequest,
                                     DependentProfile, PrivacySettings, TEXT_SIZES)
from app.models.shared import AuditLog, Notification, Patient, utcnow
from app.services.appt_care import _row, dependent_dict, link_dict
from app.services.audit import log_event

COOLING_OFF_DAYS = 30

RETENTION_EXPLANATION = (
    "What happens when you ask us to delete your account: (1) We record your request and you get a "
    f"{COOLING_OFF_DAYS}-day cooling-off period during which you can cancel it. (2) After that, an administrator "
    "reviews and completes it: your profile, records, documents and sharing settings are erased and all sharing "
    "and caregiver access stops. (3) Some information must be kept for a limited time by law or to protect you - "
    "for example a minimal audit trail of who accessed your records and what was sent to hospitals, and billing "
    "records the clinic must retain. Copies a hospital already received under a share you approved are held by "
    "that hospital under its own rules; deleting your account cannot pull them back. (4) You can download all of "
    "your data first from 'Export my data'. In this demo no data is erased automatically.")

# Mock catalogue (no real OAuth): what a patient can "connect"
APP_CATALOG = {
    "fitsync": {"name": "FitSync (demo)", "scopes": ["Steps and activity", "Heart-rate summaries"]},
    "pharmasave": {"name": "PharmaSave (demo)", "scopes": ["Medication list (read)", "Allergy list (read)"]},
    "carecalendar": {"name": "CareCalendar (demo)", "scopes": ["Appointment dates (read)"]},
}

SETTING_FIELDS = {
    "default_share_days": int, "share_requires_my_approval": bool, "notify_on_view": bool, "allow_email": bool,
    "allow_sms": bool, "allow_phone_calls": bool, "allow_appointment_reminders": bool, "allow_marketing": bool,
    "text_size": str, "high_contrast": bool, "reduced_motion": bool,
}


# ----------------------------------------------------------------------------- settings

def get_settings(db: Session, patient_id: str) -> PrivacySettings:
    s = db.get(PrivacySettings, patient_id)
    if s is None:
        s = PrivacySettings(patient_id=patient_id, default_share_days=30, default_share_categories_json="[]",
                            share_requires_my_approval=True, notify_on_view=True, allow_email=True, allow_sms=False,
                            allow_phone_calls=False, allow_appointment_reminders=True, allow_marketing=False,
                            text_size="normal", high_contrast=False, reduced_motion=False)
        db.add(s)
        db.flush()
    return s


def settings_dict(s: PrivacySettings) -> dict:
    return {"default_share_days": s.default_share_days,
            "default_share_categories": json.loads(s.default_share_categories_json or "[]"),
            "share_requires_my_approval": s.share_requires_my_approval, "notify_on_view": s.notify_on_view,
            "allow_email": s.allow_email, "allow_sms": s.allow_sms, "allow_phone_calls": s.allow_phone_calls,
            "allow_appointment_reminders": s.allow_appointment_reminders, "allow_marketing": s.allow_marketing,
            "text_size": s.text_size, "high_contrast": s.high_contrast, "reduced_motion": s.reduced_motion,
            "updated_at": s.updated_at.isoformat() + "Z" if s.updated_at else None}


def update_settings(db: Session, patient: Patient, changes: dict) -> PrivacySettings:
    s = get_settings(db, patient.id)
    errs: dict[str, str] = {}
    applied: dict = {}
    for k, v in changes.items():
        if v is None:
            continue
        if k == "default_share_categories":
            if not isinstance(v, list) or not all(isinstance(x, str) and len(x) <= 40 for x in v) or len(v) > 30:
                errs[k] = "Choose categories from the list."
                continue
            s.default_share_categories_json = json.dumps(list(dict.fromkeys(v)))
            applied[k] = v
            continue
        typ = SETTING_FIELDS.get(k)
        if typ is None:
            continue  # unknown keys are ignored (there is deliberately no language setting)
        if typ is bool and not isinstance(v, bool):
            errs[k] = "Choose on or off."
        elif typ is int and (not isinstance(v, int) or isinstance(v, bool) or not 1 <= v <= 365):
            errs[k] = "Choose between 1 and 365 days."
        elif k == "text_size" and v not in TEXT_SIZES:
            errs[k] = "Choose normal, large or extra large."
        else:
            setattr(s, k, v)
            applied[k] = v
    if errs:
        raise FieldError(errs)
    db.flush()
    if applied:
        log_event(db, actor_type="patient", actor_id=patient.id, patient_id=patient.id, action="privacy_settings_changed",
                  resource_type="privacy_settings", detail=applied)
    return s


# ----------------------------------------------------------------------------- connected apps

def list_apps(db: Session, patient_id: str) -> list[dict]:
    rows = db.scalars(select(ConnectedApp).where(ConnectedApp.patient_id == patient_id, ConnectedApp.revoked_at.is_(None))
                      .order_by(ConnectedApp.connected_at))
    return [{"id": r.id, "key": r.app_key, "name": r.name, "scopes": json.loads(r.scopes_json),
             "connected_at": r.connected_at.isoformat() + "Z"} for r in rows]


def catalog(db: Session, patient_id: str) -> list[dict]:
    have = {a["key"] for a in list_apps(db, patient_id)}
    return [{"key": k, "name": v["name"], "scopes": v["scopes"]} for k, v in APP_CATALOG.items() if k not in have]


def connect_app(db: Session, patient: Patient, key: str) -> ConnectedApp:
    meta = APP_CATALOG.get(key)
    if meta is None:
        raise FieldError({"app_key": "Choose an app from the list."})
    if any(a["key"] == key for a in list_apps(db, patient.id)):
        raise ValueError("This app is already connected.")
    row = ConnectedApp(patient_id=patient.id, app_key=key, name=meta["name"], scopes_json=json.dumps(meta["scopes"]))
    db.add(row)
    db.flush()
    log_event(db, actor_type="patient", actor_id=patient.id, patient_id=patient.id, action="app_connected",
              resource_type="connected_app", resource_id=row.id, detail={"app": meta["name"], "scopes": meta["scopes"]})
    return row


def revoke_app(db: Session, patient: Patient, app_id: str) -> ConnectedApp:
    row = db.get(ConnectedApp, app_id)
    if row is None or row.patient_id != patient.id:
        raise LookupError("Not found.")
    if row.revoked_at is None:
        row.revoked_at = utcnow()
        db.flush()
        log_event(db, actor_type="patient", actor_id=patient.id, patient_id=patient.id, action="app_disconnected",
                  resource_type="connected_app", resource_id=row.id, detail={"app": row.name})
    return row


# ----------------------------------------------------------------------------- deletion workflow

def pending_deletion(db: Session, patient_id: str) -> DeletionRequest | None:
    return db.scalar(select(DeletionRequest).where(DeletionRequest.patient_id == patient_id,
                                                   DeletionRequest.status == "pending"))


def deletion_dict(r: DeletionRequest | None) -> dict | None:
    if r is None:
        return None
    return {"id": r.id, "status": r.status, "reason": r.reason, "requested_at": r.requested_at.isoformat() + "Z",
            "earliest_completion_at": r.earliest_completion_at.isoformat() + "Z"}


def request_deletion(db: Session, patient: Patient, reason: str | None) -> DeletionRequest:
    if pending_deletion(db, patient.id):
        raise ValueError("You already have a deletion request. You can cancel it below.")
    now = utcnow()
    r = DeletionRequest(patient_id=patient.id, status="pending", reason=(reason or "").strip()[:1000] or None,
                        requested_at=now, earliest_completion_at=now + timedelta(days=COOLING_OFF_DAYS))
    db.add(r)
    patient.deletion_requested_at = now
    db.flush()
    log_event(db, actor_type="patient", actor_id=patient.id, patient_id=patient.id, action="deletion_requested",
              resource_type="deletion_request", resource_id=r.id)
    return r


def cancel_deletion(db: Session, patient: Patient) -> DeletionRequest:
    r = pending_deletion(db, patient.id)
    if r is None:
        raise LookupError("You have no pending deletion request.")
    r.status, r.resolved_at = "cancelled", utcnow()
    patient.deletion_requested_at = None
    db.flush()
    log_event(db, actor_type="patient", actor_id=patient.id, patient_id=patient.id, action="deletion_cancelled",
              resource_type="deletion_request", resource_id=r.id)
    return r


# ----------------------------------------------------------------------------- export

def _dump(db: Session, models: tuple, patient_id: str) -> dict:
    """{ModelName: [row dicts]} for the given models (every one must have a patient_id column)."""
    return {m.__name__: [_row(r) for r in db.scalars(select(m).where(m.patient_id == patient_id))] for m in models}


def export_all(db: Session, patient: Patient) -> dict:
    pid = patient.id
    deps = [d for d in db.scalars(select(DependentProfile).where(DependentProfile.guardian_patient_id == pid))]
    appts = []
    for a in db.scalars(select(Appointment).where(Appointment.patient_id == pid).order_by(Appointment.created_at)):
        d = _row(a)
        d["intake"], d["contact"] = a.intake, a.contact
        d.pop("intake_json", None), d.pop("contact_json", None)
        d["history"] = [_row(e) for e in db.scalars(select(AppointmentEvent).where(AppointmentEvent.appointment_id == a.id)
                                                    .order_by(AppointmentEvent.id))]
        appts.append(d)
    docs = [_row(d) for d in db.scalars(
        select(dm.Document).where(dm.Document.patient_id == pid, dm.Document.deleted_at.is_(None)))]
    profile = _row(patient)
    profile.pop("verified_note", None)
    return {
        "exported_at": utcnow().isoformat() + "Z",
        "note": "Synthetic demo data. Passwords, sign-in sessions and share tokens are never exported.",
        "profile": profile,
        "emergency_contacts": _dump(db, (cm.EmergencyContact,), pid)["EmergencyContact"],
        "medical": _dump(db, (mm.MedProvider, mm.MedHistory, mm.MedMedication, mm.MedAllergy, mm.MedVaccination,
                              mm.MedResult, mm.MedCorrection), pid),
        "insurance_and_billing": _dump(db, (bm.InsurancePlan, bm.BillingClaim, bm.BillingBill, bm.BillingPayment,
                                            bm.BillingDispute), pid),
        "appointments": appts,
        "documents": docs,
        "sharing": _dump(db, (dm.ShareGrant, dm.AccessRequest), pid),
        "caregivers": [link_dict(l) for l in db.scalars(select(CaregiverLink).where(CaregiverLink.patient_id == pid))],
        "dependents": [dependent_dict(db, d) for d in deps],
        "privacy_settings": settings_dict(get_settings(db, pid)),
        "connected_apps": [_row(a) for a in db.scalars(select(ConnectedApp).where(ConnectedApp.patient_id == pid))],
        "deletion_requests": [_row(r) for r in db.scalars(select(DeletionRequest).where(DeletionRequest.patient_id == pid))],
        "notifications": [_row(n) for n in db.scalars(select(Notification).where(
            Notification.recipient_type == "patient", Notification.recipient_id == pid).order_by(Notification.ts))],
        "activity_log": [_row(a) for a in db.scalars(select(AuditLog).where(AuditLog.patient_id == pid).order_by(AuditLog.ts))],
    }


def _safe_name(name: str) -> str:
    name = re.sub(r"[^\w.\- ]+", "_", name or "file")
    name = re.sub(r"\.{2,}", ".", name).strip(" .") or "file"
    return name[:120]


def export_zip(db: Session, patient: Patient) -> tuple[bytes, dict]:
    """ZIP: data.json + documents/<id8>-<name> (file bytes, when W2's tables exist) + README.txt."""
    data = export_all(db, patient)
    buf = io.BytesIO()
    included = 0
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("README.txt", "Your data export (synthetic demo data).\n"
                   "data.json - everything in your account as structured data.\n"
                   "documents/ - the files you stored in the app (if any).\n")
        for d in data["documents"]:
            blob = db.get(dm.DocumentBlob, d["id"])
            if blob is None:
                d["included_in_zip"] = False
                continue
            path = f"documents/{d['id'][:8]}-{_safe_name(d.get('name'))}"
            z.writestr(path, blob.data)
            d["zip_path"], d["included_in_zip"] = path, True
            included += 1
        z.writestr("data.json", json.dumps(data, indent=2, default=str))
    return buf.getvalue(), {"documents_included": included}
