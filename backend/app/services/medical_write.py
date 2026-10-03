"""Write side of the medical records (W7): validated create/update/delete, allergy status rules, corrections.

All functions flush but DO NOT commit (the route commits, so record + audit + notification share ONE transaction).
Every function takes the owning ``patient_id`` and only ever touches rows with that ``patient_id`` - a foreign id is
indistinguishable from a missing one (404).
"""
from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.errors import FieldError
from app.models.medical import (MedAllergy, MedCorrection, MedHistory, MedMedication, MedProvider, MedResult,
                                MedVaccination)
from app.models.shared import Patient, Provider, utcnow
from app.services.audit import log_event
from app.services.medical_schemas import SCHEMAS, norm_key
from app.services.notifications import notify

MODELS = {
    "history": MedHistory, "medications": MedMedication, "allergies": MedAllergy,
    "vaccinations": MedVaccination, "results": MedResult, "providers": MedProvider,
}
# correction record_type  <->  resource name
TYPE_TO_RESOURCE = {"history": "history", "medication": "medications", "allergy": "allergies",
                    "vaccination": "vaccinations", "result": "results", "provider": "providers"}
RESOURCE_TO_TYPE = {v: k for k, v in TYPE_TO_RESOURCE.items()}

CONFIRMED_MESSAGE = ("A clinician confirmed this entry, so it can't be edited or removed directly. "
                     "Use \"Request a correction\" and the care team will see your request.")


class NotFound(LookupError):
    pass


class Locked(PermissionError):
    """Clinician-confirmed records are changed only through the correction workflow."""


def _title(res: str, row) -> str:
    return {"history": lambda r: r.title, "medications": lambda r: r.name, "allergies": lambda r: r.substance,
            "vaccinations": lambda r: r.name, "results": lambda r: r.test_name,
            "providers": lambda r: r.name}[res](row)


def get_owned(db: Session, res: str, patient_id: str, rec_id: str):
    row = db.get(MODELS[res], rec_id)
    if row is None or row.patient_id != patient_id:
        raise NotFound("Record not found.")
    return row


def _audit(db, patient_id: str, res: str, action: str, rec_id: str) -> None:
    # No values in the audit trail - only what kind of record changed.
    log_event(db, actor_type="patient", actor_id=patient_id, patient_id=patient_id, action=f"med_{res}_{action}",
              resource_type=f"med_{res}", resource_id=rec_id)


def _own_provider(db: Session, patient_id: str, provider_id: str | None, field: str) -> None:
    if provider_id is None:
        return
    p = db.get(MedProvider, provider_id)
    if p is None or p.patient_id != patient_id:
        raise FieldError({field: "Choose a provider from your own care team list."})


def _check_refs(db: Session, patient_id: str, res: str, vals: dict) -> None:
    for f in ("provider_id", "prescriber_id"):
        if f in vals:
            _own_provider(db, patient_id, vals[f], f)
    if vals.get("linked_provider_id") and db.get(Provider, vals["linked_provider_id"]) is None:
        raise FieldError({"linked_provider_id": "That hospital isn't in the directory."})
    if res == "results" and vals.get("document_id"):
        try:
            from app.models.documents import Document
        except ImportError:  # pragma: no cover - documents module absent
            raise FieldError({"document_id": "Documents are not available yet."}) from None
        d = db.get(Document, vals["document_id"])
        if d is None or d.patient_id != patient_id or d.deleted_at is not None:
            raise FieldError({"document_id": "Choose one of your own uploaded documents."})


def _derive(res: str, vals: dict) -> dict:
    if res == "allergies":
        vals["substance_key"] = norm_key(vals["substance"])
    elif res == "results":
        vals["test_key"] = norm_key(vals["test_name"])
        if vals.get("unit"):
            vals["unit"] = vals["unit"].strip()
    elif res == "providers":
        vals["name_key"] = norm_key(vals["name"])
    return vals


_DUP = {"allergies": ("substance", "You already have this allergy listed. Edit that entry instead."),
        "providers": ("name", "This provider is already in your list. Edit that entry instead.")}


def _flush_unique(db: Session, res: str) -> None:
    """Flush inside a savepoint so a unique violation becomes a friendly field error, not a 500."""
    try:
        with db.begin_nested():
            db.flush()
    except IntegrityError as e:
        if res in _DUP:
            field, msg = _DUP[res]
            raise FieldError({field: msg}) from e
        raise


_FRIENDLY = {
    "extra_forbidden": "This field can't be set here.",
    "missing": "Please fill this in.", "string_type": "Please fill this in.",
    "string_too_short": "Please fill this in.", "literal_error": "Please choose one of the options.",
    "date_type": "Enter a date like 2024-05-17.", "date_parsing": "Enter a date like 2024-05-17.",
    "date_from_datetime_parsing": "Enter a date like 2024-05-17.",
    "decimal_type": "Enter a number.", "decimal_parsing": "Enter a number.",
    "decimal_max_places": "Use at most 4 decimal places.",
    "int_type": "Enter a whole number.", "int_parsing": "Enter a whole number.",
    "bool_type": "Choose yes or no.", "bool_parsing": "Choose yes or no.",
}


def validate(res: str, body: dict) -> dict:
    """Parse + validate a request body; pydantic errors become FieldError (per-field messages)."""
    from pydantic import ValidationError

    if not isinstance(body, dict):
        raise FieldError({}, "Please send the record as a JSON object.")
    try:
        return SCHEMAS[res].model_validate(body).model_dump()
    except ValidationError as e:
        fields: dict[str, str] = {}
        for err in e.errors():
            msg = str(err.get("msg", "Invalid value")).replace("Value error, ", "")
            loc = [str(p) for p in err.get("loc", [])]
            msg = _FRIENDLY.get(err.get("type"), msg)
            if loc:
                fields.setdefault(loc[-1], msg)
            elif "|" in msg:
                f, _, m = msg.partition("|")
                fields.setdefault(f, m)
            else:
                fields.setdefault("_", msg)
        first = next(iter(fields.values()), "Please check the highlighted fields.")
        raise FieldError(fields, first if "_" in fields else "Please check the highlighted fields.") from e


def create_record(db: Session, patient: Patient, res: str, body: dict) -> object:
    vals = _derive(res, validate(res, body))
    _check_refs(db, patient.id, res, vals)
    row = MODELS[res](patient_id=patient.id, source="patient_entered", **vals)
    db.add(row)
    _flush_unique(db, res)
    if res == "allergies":
        sync_allergy_status(db, patient)
    _audit(db, patient.id, res, "created", row.id)
    return row


def update_record(db: Session, patient: Patient, res: str, rec_id: str, body: dict) -> object:
    row = get_owned(db, res, patient.id, rec_id)
    if row.source == "clinician_confirmed":
        raise Locked(CONFIRMED_MESSAGE)
    vals = _derive(res, validate(res, body))
    _check_refs(db, patient.id, res, vals)
    for k, v in vals.items():
        setattr(row, k, v)
    row.updated_at = utcnow()
    _flush_unique(db, res)
    if res == "allergies":
        sync_allergy_status(db, patient)
    _audit(db, patient.id, res, "updated", row.id)
    return row


def delete_record(db: Session, patient: Patient, res: str, rec_id: str) -> None:
    row = get_owned(db, res, patient.id, rec_id)
    if row.source == "clinician_confirmed":
        raise Locked(CONFIRMED_MESSAGE)
    db.delete(row)
    db.flush()
    # Open correction requests about a record that no longer exists are closed, not orphaned.
    for c in db.scalars(select(MedCorrection).where(
            MedCorrection.patient_id == patient.id, MedCorrection.record_type == RESOURCE_TO_TYPE[res],
            MedCorrection.record_id == rec_id, MedCorrection.status == "open")).all():
        c.status = "withdrawn"
        c.resolution_note = "The entry was removed by the patient."
        c.resolved_at = utcnow()
    if res == "allergies":
        sync_allergy_status(db, patient)
    _audit(db, patient.id, res, "deleted", rec_id)


# --------------------------------------------------------------------------- allergy status

def active_allergy_count(db: Session, patient_id: str) -> int:
    return db.scalar(select(func.count()).select_from(MedAllergy)
                     .where(MedAllergy.patient_id == patient_id, MedAllergy.status == "active")) or 0


def sync_allergy_status(db: Session, patient: Patient) -> str:
    """Keep ``patients.allergy_status`` consistent with the allergy list.

    Active allergies  -> has_allergies.
    List became empty -> back to *unknown* (we never infer "no known allergies"; the patient must say it).
    """
    n = active_allergy_count(db, patient.id)
    if n and patient.allergy_status != "has_allergies":
        patient.allergy_status = "has_allergies"
    elif not n and patient.allergy_status == "has_allergies":
        patient.allergy_status = "unknown"
    return patient.allergy_status


def set_allergy_status(db: Session, patient: Patient, status: str) -> str:
    if status not in ("unknown", "no_known_allergies"):
        raise FieldError({"status": "Choose \"No known allergies\" or \"Unknown\"."})
    if status == "no_known_allergies" and active_allergy_count(db, patient.id):
        raise FieldError({"status": "You have allergies listed. Mark them inactive or remove them before saying "
                                    "\"No known allergies\"."})
    if status == "unknown" and active_allergy_count(db, patient.id):
        raise FieldError({"status": "You have allergies listed, so the status is \"Allergies listed\"."})
    patient.allergy_status = status
    patient.updated_at = utcnow()
    log_event(db, actor_type="patient", actor_id=patient.id, patient_id=patient.id, action="med_allergy_status_set",
              resource_type="patient", resource_id=patient.id, detail={"status": status})
    return status


# --------------------------------------------------------------------------- clinician confirmation (staff side, later)

def confirm_record(db: Session, *, patient_id: str, res: str, rec_id: str, confirmed_by: str,
                   actor_type: str = "staff", actor_id: str | None = None) -> object:
    """Mark a record clinician-confirmed.  For W3's staff portal; the CALLER must have authenticated the staff user.
    Does not commit."""
    row = get_owned(db, res, patient_id, rec_id)
    row.source = "clinician_confirmed"
    row.confirmed_by = (confirmed_by or "")[:200] or None
    row.confirmed_at = utcnow()
    row.updated_at = utcnow()
    log_event(db, actor_type=actor_type, actor_id=actor_id, patient_id=patient_id, action=f"med_{res}_confirmed",
              resource_type=f"med_{res}", resource_id=rec_id)
    return row


# --------------------------------------------------------------------------- corrections

def create_correction(db: Session, patient: Patient, body: dict) -> MedCorrection:
    from app.services.medical_schemas import CorrectionIn
    from pydantic import ValidationError

    try:
        data = CorrectionIn.model_validate(body).model_dump()
    except ValidationError as e:
        fields = {str(err["loc"][-1]): str(err["msg"]) for err in e.errors() if err.get("loc")}
        raise FieldError(fields) from e
    res = TYPE_TO_RESOURCE[data["record_type"]]
    row = get_owned(db, res, patient.id, data["record_id"])  # NotFound for someone else's / missing id
    dup = db.scalar(select(MedCorrection).where(
        MedCorrection.patient_id == patient.id, MedCorrection.record_type == data["record_type"],
        MedCorrection.record_id == row.id, MedCorrection.status == "open"))
    if dup is not None:
        raise FieldError({"message": "You already have an open correction request for this entry."})
    c = MedCorrection(patient_id=patient.id, record_type=data["record_type"], record_id=row.id,
                      record_label=_title(res, row)[:200], message=data["message"])
    db.add(c)
    db.flush()
    log_event(db, actor_type="patient", actor_id=patient.id, patient_id=patient.id, action="med_correction_requested",
              resource_type="med_correction", resource_id=c.id, detail={"record_type": c.record_type})
    return c


def withdraw_correction(db: Session, patient: Patient, correction_id: str) -> MedCorrection:
    c = db.get(MedCorrection, correction_id)
    if c is None or c.patient_id != patient.id:
        raise NotFound("Correction request not found.")
    if c.status != "open":
        raise FieldError({"status": f"This request is already {c.status}."})
    c.status = "withdrawn"
    c.resolved_at = utcnow()
    c.updated_at = utcnow()
    log_event(db, actor_type="patient", actor_id=patient.id, patient_id=patient.id, action="med_correction_withdrawn",
              resource_type="med_correction", resource_id=c.id)
    return c


def resolve_correction(db: Session, *, correction_id: str, status: str, resolved_by: str, note: str | None = None,
                       actor_id: str | None = None) -> MedCorrection:
    """Staff side (W3, later): mark a correction resolved/declined; the patient is notified.  Does not commit.
    The CALLER must have authenticated the staff user and checked consent for the patient."""
    if status not in ("resolved", "declined"):
        raise ValueError("status must be 'resolved' or 'declined'")
    c = db.get(MedCorrection, correction_id)
    if c is None:
        raise NotFound("Correction request not found.")
    if c.status != "open":
        raise ValueError(f"This request is already {c.status}.")
    c.status = status
    c.resolved_by = (resolved_by or "")[:200] or None
    c.resolution_note = (note or "")[:2000] or None
    c.resolved_at = utcnow()
    c.updated_at = utcnow()
    log_event(db, actor_type="staff", actor_id=actor_id, patient_id=c.patient_id, action=f"med_correction_{status}",
              resource_type="med_correction", resource_id=c.id)
    notify(db, recipient_type="patient", recipient_id=c.patient_id, kind="correction_update",
           title=f"Your correction request was {status}",
           body=f"About \"{c.record_label}\".", link="#/med_corrections")
    return c
