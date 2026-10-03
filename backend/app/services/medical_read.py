"""Read side of the medical records (W7): serializers, per-category reads, timeline, trends, summary.

PUBLIC INTERFACE FOR OTHER WORKSTREAMS (W2/W3/W9)
    get_category_for_patient(db, patient_id, category) -> list[dict]
        category in MED_CATEGORIES = ("history", "medications", "allergies", "vaccinations", "results").
        NO authorization happens here: the caller MUST already have checked that the requester is the patient or
        a staff member holding an active, unexpired consent grant for that category (W2 consent service).
        Unknown categories raise ValueError.  Every item is a plain JSON-safe dict carrying ``record_type``,
        ``source``/``source_label`` ("Patient-entered" / "Clinician-confirmed") and ``updated_at``.
        For "allergies" the FIRST item is always ``{"record_type": "allergy_status", "status": ...}`` so a reader
        can tell "No known allergies" (explicitly stated) from "Unknown" (never stated) even when the list is empty.

Results are only ever compared with the reporting lab's own reference range and described neutrally as
"outside the lab's reference range".  Nothing here diagnoses or advises.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.medical import (MedAllergy, MedCorrection, MedHistory, MedMedication, MedProvider, MedResult,
                                MedVaccination)
from app.models.shared import Patient, utcnow
from app.services.medical_schemas import sort_date

MED_CATEGORIES = ("history", "medications", "allergies", "vaccinations", "results")
OUTSIDE_RANGE_TEXT = "Outside the lab's reference range"
RESULT_DISCLAIMER = ("Reference ranges come from the lab that ran each test. \"Outside the lab's reference range\" "
                     "only describes where a number sits compared with that range. It is not a diagnosis or medical advice.")
SOURCE_LABELS = {"patient_entered": "Patient-entered", "clinician_confirmed": "Clinician-confirmed"}
ALLERGY_STATUS_LABELS = {
    "has_allergies": "Allergies listed",
    "no_known_allergies": "No known allergies",
    "unknown": "Unknown - allergy information has not been provided",
}
FOLLOWUP_SOON_DAYS = 30


def iso(dt: datetime | None) -> str | None:
    return dt.isoformat(timespec="seconds") + "Z" if dt else None


def _num(v: Decimal | None):
    if v is None:
        return None
    f = float(v)
    return int(f) if f.is_integer() else f


def _base(row, record_type: str) -> dict:
    return {
        "id": row.id,
        "record_type": record_type,
        "source": row.source,
        "source_label": SOURCE_LABELS.get(row.source, row.source),
        "confirmed_by": row.confirmed_by,
        "confirmed_at": iso(row.confirmed_at),
        "notes": row.notes,
        "created_at": iso(row.created_at),
        "updated_at": iso(row.updated_at),
        "can_edit": row.source != "clinician_confirmed",
    }


# --------------------------------------------------------------------------- range flag

def range_status(value, low, high) -> tuple[str, str | None]:
    """('within'|'outside'|'unknown', direction 'below'|'above'|None) against the lab's own range only."""
    if value is None or (low is None and high is None):
        return "unknown", None
    if low is not None and value < low:
        return "outside", "below"
    if high is not None and value > high:
        return "outside", "above"
    return "within", None


def ref_display(low, high, unit, ref_text) -> str | None:
    u = f" {unit}" if unit else ""
    if low is not None and high is not None:
        return f"{_num(low)}\u2013{_num(high)}{u}"
    if high is not None:
        return f"up to {_num(high)}{u}"
    if low is not None:
        return f"{_num(low)}{u} or more"
    return ref_text or None


# --------------------------------------------------------------------------- serializers

def ser_provider(r: MedProvider) -> dict:
    d = _base(r, "provider")
    d.update(name=r.name, kind=r.kind, specialty=r.specialty, organization=r.organization,
             role_in_care=r.role_in_care, phone=r.phone, address=r.address, is_care_team=bool(r.is_care_team),
             last_seen=r.last_seen.isoformat() if r.last_seen else None, linked_provider_id=r.linked_provider_id,
             title=r.name)
    return d


def ser_history(r: MedHistory) -> dict:
    d = _base(r, "history")
    d.update(kind=r.kind, title=r.title, status=r.status, start_date=r.start_date, end_date=r.end_date,
             relation=r.relation, facility_name=r.facility_name, provider_id=r.provider_id)
    return d


def ser_medication(r: MedMedication) -> dict:
    d = _base(r, "medication")
    d.update(name=r.name, title=r.name, status=r.status, is_supplement=bool(r.is_supplement), dose=r.dose,
             frequency=r.frequency, route=r.route, reason=r.reason, start_date=r.start_date, end_date=r.end_date,
             prescriber_name=r.prescriber_name, prescriber_id=r.prescriber_id)
    return d


def ser_allergy(r: MedAllergy) -> dict:
    d = _base(r, "allergy")
    d.update(substance=r.substance, title=r.substance, category=r.category, reaction=r.reaction,
             severity=r.severity, status=r.status, onset=r.onset)
    return d


def followup_state(due: date | None, status: str, today: date | None = None) -> str | None:
    """Neutral scheduling label only: 'passed' | 'soon' | 'scheduled' | None."""
    if due is None or status == "declined":
        return None
    today = today or utcnow().date()
    if due < today:
        return "passed"
    if due <= today + timedelta(days=FOLLOWUP_SOON_DAYS):
        return "soon"
    return "scheduled"


FOLLOWUP_LABELS = {"passed": "Follow-up date has passed", "soon": "Follow-up coming up", "scheduled": "Follow-up scheduled"}


def ser_vaccination(r: MedVaccination) -> dict:
    d = _base(r, "vaccination")
    st = followup_state(r.next_due_date, r.status)
    d.update(kind=r.kind, name=r.name, title=r.name, status=r.status, date_given=r.date_given,
             dose_number=r.dose_number, administered_by=r.administered_by, provider_id=r.provider_id,
             next_due_date=r.next_due_date.isoformat() if r.next_due_date else None,
             followup_state=st, followup_label=FOLLOWUP_LABELS.get(st))
    return d


def ser_result(r: MedResult) -> dict:
    d = _base(r, "result")
    status, direction = range_status(r.value_num, r.ref_low, r.ref_high)
    d.update(test_name=r.test_name, title=r.test_name, category=r.category,
             result_date=r.result_date.isoformat() if r.result_date else None,
             value_num=_num(r.value_num), value_text=r.value_text, unit=r.unit,
             ref_low=_num(r.ref_low), ref_high=_num(r.ref_high), ref_text=r.ref_text,
             ref_display=ref_display(r.ref_low, r.ref_high, r.unit, r.ref_text),
             range_status=status, range_direction=direction,
             outside_range=status == "outside",
             flag_text=OUTSIDE_RANGE_TEXT if status == "outside" else None,
             source_name=r.source_name, provider_id=r.provider_id, document_id=r.document_id,
             series_key=series_key(r.test_key, r.unit))
    return d


def series_key(test_key: str, unit: str | None) -> str:
    return f"{test_key}|{(unit or '').casefold().strip()}"


# --------------------------------------------------------------------------- list queries

def _patient_status(db: Session, patient_id: str) -> str:
    st = db.scalar(select(Patient.allergy_status).where(Patient.id == patient_id))
    return st or "unknown"


def allergy_status_info(db: Session, patient_id: str) -> dict:
    st = _patient_status(db, patient_id)
    active = db.scalar(select(func.count()).select_from(MedAllergy)
                       .where(MedAllergy.patient_id == patient_id, MedAllergy.status == "active")) or 0
    if active and st != "has_allergies":
        st = "has_allergies"  # derived: listed allergies always win over a stale flag
    return {"record_type": "allergy_status", "status": st, "label": ALLERGY_STATUS_LABELS.get(st, st),
            "explicit_none": st == "no_known_allergies", "unknown": st == "unknown", "active_count": active}


def list_history(db, patient_id):
    rows = db.scalars(select(MedHistory).where(MedHistory.patient_id == patient_id)).all()
    rows = sorted(rows, key=lambda r: (sort_date(r.start_date) or "0000", r.created_at), reverse=True)
    return [ser_history(r) for r in rows]


def list_medications(db, patient_id):
    rows = db.scalars(select(MedMedication).where(MedMedication.patient_id == patient_id)
                      .order_by(MedMedication.status, func.lower(MedMedication.name))).all()
    return [ser_medication(r) for r in rows]


def list_allergies(db, patient_id):
    rows = db.scalars(select(MedAllergy).where(MedAllergy.patient_id == patient_id)
                      .order_by(MedAllergy.status, func.lower(MedAllergy.substance))).all()
    sev = {"severe": 0, "moderate": 1, "mild": 2, "unknown": 3}
    return [ser_allergy(r) for r in sorted(rows, key=lambda r: (r.status != "active", sev.get(r.severity, 3), r.substance.casefold()))]


def list_vaccinations(db, patient_id):
    rows = db.scalars(select(MedVaccination).where(MedVaccination.patient_id == patient_id)).all()
    rows = sorted(rows, key=lambda r: (sort_date(r.date_given) or (r.next_due_date.isoformat() if r.next_due_date else "0000")),
                  reverse=True)
    return [ser_vaccination(r) for r in rows]


def list_results(db, patient_id, test_key: str | None = None):
    q = select(MedResult).where(MedResult.patient_id == patient_id)
    if test_key:
        q = q.where(MedResult.test_key == test_key)
    rows = db.scalars(q).all()
    rows = sorted(rows, key=lambda r: (r.result_date.isoformat() if r.result_date else "0000", r.created_at), reverse=True)
    return [ser_result(r) for r in rows]


def list_providers(db, patient_id):
    rows = db.scalars(select(MedProvider).where(MedProvider.patient_id == patient_id)).all()
    return [ser_provider(r) for r in sorted(rows, key=lambda r: (not r.is_care_team, r.name.casefold()))]


LISTERS = {
    "history": list_history, "medications": list_medications, "allergies": list_allergies,
    "vaccinations": list_vaccinations, "results": list_results, "providers": list_providers,
}


def get_category_for_patient(db: Session, patient_id: str, category: str) -> list[dict]:
    """Records of one consent category for a patient.  CALLER MUST HAVE AUTHORIZED (see module docstring)."""
    category = (category or "").strip().lower()
    if category not in MED_CATEGORIES:
        raise ValueError(f"Unknown medical category: {category[:40]}")
    items = LISTERS[category](db, patient_id)
    if category == "allergies":
        return [allergy_status_info(db, patient_id)] + items
    return items


# --------------------------------------------------------------------------- trends

def result_trends(db: Session, patient_id: str, key: str | None = None) -> dict:
    """Comparable numeric results grouped by test name AND unit (different units are never plotted together)."""
    q = select(MedResult).where(MedResult.patient_id == patient_id, MedResult.value_num.is_not(None),
                                MedResult.result_date.is_not(None))
    rows = db.scalars(q).all()
    groups: dict[str, list[MedResult]] = {}
    for r in rows:
        groups.setdefault(series_key(r.test_key, r.unit), []).append(r)
    units_by_test: dict[str, set] = {}
    for k, rs in groups.items():
        units_by_test.setdefault(rs[0].test_key, set()).add(rs[0].unit or "")
    series = []
    for k, rs in groups.items():
        if key and k != key:
            continue
        rs.sort(key=lambda r: (r.result_date, r.created_at))
        pts = []
        for r in rs:
            st, direction = range_status(r.value_num, r.ref_low, r.ref_high)
            pts.append({"id": r.id, "date": r.result_date.isoformat(), "value": _num(r.value_num),
                        "ref_low": _num(r.ref_low), "ref_high": _num(r.ref_high), "range_status": st,
                        "outside_range": st == "outside", "range_direction": direction,
                        "source_name": r.source_name, "document_id": r.document_id, "source": r.source})
        latest = rs[-1]
        other_units = sorted(u for u in units_by_test.get(rs[0].test_key, set()) if u != (rs[0].unit or ""))
        series.append({
            "key": k, "test_name": latest.test_name, "unit": latest.unit, "points": pts, "count": len(pts),
            "comparable": len(pts) >= 2,
            "latest": pts[-1],
            "any_outside": any(p["outside_range"] for p in pts),
            "other_units": other_units,
            "unit_note": ("Other results for this test use a different unit (%s), so they are not plotted here."
                          % ", ".join(u or "no unit" for u in other_units)) if other_units else None,
        })
    series.sort(key=lambda s: (-s["count"], s["test_name"].casefold()))
    return {"series": series, "disclaimer": RESULT_DISCLAIMER}


# --------------------------------------------------------------------------- timeline

TIMELINE_TYPES = ("history", "medications", "vaccinations", "results")


def timeline(db: Session, patient_id: str, types=None) -> dict:
    """Chronological events (newest first); items whose date is unknown are listed separately, never hidden."""
    types = [t for t in (types or TIMELINE_TYPES) if t in TIMELINE_TYPES] or list(TIMELINE_TYPES)
    events: list[dict] = []

    def add(rec: dict, etype: str, date_str: str | None, title: str, subtitle: str | None, kind: str):
        events.append({"type": etype, "kind": kind, "id": rec["id"], "record_type": rec["record_type"],
                       "date": date_str, "sort": sort_date(date_str) if date_str and len(date_str) in (4, 7, 10) else None,
                       "title": title, "subtitle": subtitle, "source": rec["source"], "source_label": rec["source_label"],
                       "updated_at": rec["updated_at"], "flag_text": rec.get("flag_text")})

    if "history" in types:
        for r in list_history(db, patient_id):
            label = {"condition": "Condition", "surgery": "Surgery", "hospitalization": "Hospital stay",
                     "family_history": "Family history"}[r["kind"]]
            sub = label + (f" - {r['relation']}" if r.get("relation") else "")
            if r["status"] != "unknown":
                sub += f" ({r['status']})"
            add(r, "history", r["start_date"], r["title"], sub, r["kind"])
    if "medications" in types:
        for r in list_medications(db, patient_id):
            sub = ("Supplement" if r["is_supplement"] else "Medication") + f" - {r['status']}"
            add(r, "medications", r["start_date"], f"Started {r['name']}", sub, "medication")
    if "vaccinations" in types:
        for r in list_vaccinations(db, patient_id):
            if r["status"] == "completed" or r["date_given"]:
                add(r, "vaccinations", r["date_given"], r["name"], r["kind"].replace("_", " ").capitalize(), r["kind"])
    if "results" in types:
        for r in list_results(db, patient_id):
            val = r["value_text"] if r["value_num"] is None else f"{r['value_num']} {r['unit'] or ''}".strip()
            add(r, "results", r["result_date"], r["test_name"], val, "result")
    dated = sorted((e for e in events if e["sort"]), key=lambda e: e["sort"], reverse=True)
    undated = [e for e in events if not e["sort"]]
    for e in dated + undated:
        e.pop("sort", None)
    return {"events": dated, "undated": undated, "types": types}


# --------------------------------------------------------------------------- summary (dashboard / allergy banner)

def summary(db: Session, patient_id: str) -> dict:
    allergies = list_allergies(db, patient_id)
    active_allergies = [a for a in allergies if a["status"] == "active"]
    meds = list_medications(db, patient_id)
    vaccs = list_vaccinations(db, patient_id)
    trends = result_trends(db, patient_id)
    results = list_results(db, patient_id)
    latest_by_series: dict[str, dict] = {}
    for r in results:  # newest first
        latest_by_series.setdefault(r["series_key"], r)
    outside_latest = [r for r in latest_by_series.values() if r["outside_range"]]
    followups = [v for v in vaccs if v["followup_state"] in ("passed", "soon")]
    followups.sort(key=lambda v: v["next_due_date"])
    open_corrections = db.scalar(select(func.count()).select_from(MedCorrection)
                                 .where(MedCorrection.patient_id == patient_id, MedCorrection.status == "open")) or 0
    return {
        "allergy": {**allergy_status_info(db, patient_id), "items": active_allergies},
        "counts": {
            "history": db.scalar(select(func.count()).select_from(MedHistory).where(MedHistory.patient_id == patient_id)) or 0,
            "active_medications": sum(1 for m in meds if m["status"] == "active"),
            "past_medications": sum(1 for m in meds if m["status"] == "past"),
            "allergies": len(active_allergies),
            "vaccinations": len(vaccs),
            "results": len(results),
            "providers": db.scalar(select(func.count()).select_from(MedProvider).where(MedProvider.patient_id == patient_id)) or 0,
            "trend_series": sum(1 for s in trends["series"] if s["comparable"]),
        },
        "results_outside_range": outside_latest[:5],
        "followups": followups[:5],
        "open_corrections": open_corrections,
        "disclaimer": RESULT_DISCLAIMER,
    }


# --------------------------------------------------------------------------- corrections

def ser_correction(c: MedCorrection) -> dict:
    return {"id": c.id, "patient_id": c.patient_id, "record_type": c.record_type, "record_id": c.record_id,
            "record_label": c.record_label, "message": c.message, "status": c.status,
            "resolution_note": c.resolution_note, "resolved_by": c.resolved_by, "resolved_at": iso(c.resolved_at),
            "created_at": iso(c.created_at), "updated_at": iso(c.updated_at)}


def list_corrections(db: Session, patient_id: str, status: str | None = None) -> list[dict]:
    q = select(MedCorrection).where(MedCorrection.patient_id == patient_id)
    if status:
        q = q.where(MedCorrection.status == status)
    return [ser_correction(c) for c in db.scalars(q.order_by(MedCorrection.created_at.desc())).all()]
