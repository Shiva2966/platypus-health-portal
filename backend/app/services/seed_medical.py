"""Synthetic medical data for demos/tests (W7).  Entirely made up - not real people, not real results.

    seed_medical(db, patient_id) -> dict   (idempotent: does nothing if the patient already has medical records)
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import select

from app.models.medical import (MedAllergy, MedHistory, MedMedication, MedProvider, MedResult, MedVaccination)
from app.models.shared import Patient, utcnow
from app.services.medical_schemas import norm_key

CONFIRMED = dict(source="clinician_confirmed", confirmed_by="Dr. Alex Rivera (demo)")


def _confirmed():
    return {**CONFIRMED, "confirmed_at": utcnow()}


def seed_medical(db, patient_id: str) -> dict:
    patient = db.get(Patient, patient_id)
    if patient is None:
        raise LookupError("Patient not found.")
    if db.scalar(select(MedHistory.id).where(MedHistory.patient_id == patient_id).limit(1)) or \
            db.scalar(select(MedResult.id).where(MedResult.patient_id == patient_id).limit(1)):
        return {"skipped": True}
    today = utcnow().date()

    def ago(days: int) -> date:
        return today - timedelta(days=days)

    def add(row):
        db.add(row)
        return row

    # --- care team ---------------------------------------------------------------------------------
    def prov(name, **kw):
        return add(MedProvider(patient_id=patient_id, name=name, name_key=norm_key(name), **kw))

    pcp = prov("Dr. Alex Rivera", kind="clinician", specialty="Family medicine", role_in_care="Primary care",
               organization="Lakeside Family Clinic", phone="555-0100", last_seen=ago(75), **_confirmed())
    prov("Dr. Priya Nair", kind="clinician", specialty="Eye care", role_in_care="Eye exams",
         organization="Clearview Eye Center", phone="555-0111", last_seen=ago(400))
    hosp = prov("Lakeside General Hospital", kind="facility", address="100 Example Ave, Springfield (demo)",
                phone="555-0150", is_care_team=False)
    prov("Northside Lab", kind="lab", phone="555-0175", is_care_team=False)
    prov("Greenway Pharmacy", kind="pharmacy", phone="555-0190", is_care_team=False)
    db.flush()

    # --- history -----------------------------------------------------------------------------------
    hist = [
        MedHistory(kind="condition", title="Type 2 diabetes (as recorded by my clinic)", status="active",
                   start_date="2019-04", provider_id=pcp.id, **_confirmed()),
        MedHistory(kind="condition", title="High cholesterol (as recorded by my clinic)", status="active",
                   start_date="2020", provider_id=pcp.id, **_confirmed()),
        MedHistory(kind="condition", title="Seasonal hay fever", status="active", start_date="2005"),
        MedHistory(kind="surgery", title="Appendix removal", status="resolved", start_date="2008-06-12",
                   facility_name="Lakeside General Hospital", **_confirmed()),
        MedHistory(kind="hospitalization", title="Overnight stay for observation", status="resolved",
                   start_date="2021-03", end_date="2021-03", facility_name="Lakeside General Hospital",
                   provider_id=hosp.id),
        MedHistory(kind="family_history", title="Heart disease", relation="Father", status="unknown"),
        MedHistory(kind="family_history", title="Type 2 diabetes", relation="Mother", status="unknown"),
    ]
    # --- medications -------------------------------------------------------------------------------
    meds = [
        MedMedication(name="Metformin", dose="500 mg", frequency="Twice daily", route="By mouth", status="active",
                      reason="Prescribed by my clinic", start_date="2019-05", prescriber_name="Dr. Alex Rivera",
                      prescriber_id=pcp.id, **_confirmed()),
        MedMedication(name="Atorvastatin", dose="20 mg", frequency="Once daily", route="By mouth", status="active",
                      start_date="2020-02", prescriber_name="Dr. Alex Rivera", prescriber_id=pcp.id, **_confirmed()),
        MedMedication(name="Vitamin D3", dose="1000 IU", frequency="Once daily", status="active", is_supplement=True,
                      start_date="2022"),
        MedMedication(name="Fish oil", dose="1000 mg", frequency="Once daily", status="active", is_supplement=True),
        MedMedication(name="Amoxicillin", dose="500 mg", frequency="Three times daily", status="past",
                      start_date="2023-02-03", end_date="2023-02-13", reason="Short course"),
    ]
    # --- allergies ---------------------------------------------------------------------------------
    allergies = [
        MedAllergy(substance="Penicillin", substance_key=norm_key("Penicillin"), category="drug", reaction="Rash",
                   severity="moderate", status="active", onset="1999", **_confirmed()),
        MedAllergy(substance="Peanuts", substance_key=norm_key("Peanuts"), category="food", reaction="Hives",
                   severity="severe", status="active"),
    ]
    patient.allergy_status = "has_allergies"

    # --- vaccinations / preventive care --------------------------------------------------------------
    vaccs = [
        MedVaccination(kind="vaccine", name="Influenza (seasonal)", status="completed",
                       date_given=(today - timedelta(days=345)).isoformat(), administered_by="Greenway Pharmacy",
                       next_due_date=today + timedelta(days=20)),
        MedVaccination(kind="vaccine", name="Tetanus, diphtheria, pertussis (Tdap)", status="completed",
                       date_given="2020-09", provider_id=pcp.id, next_due_date=date(2030, 9, 1), **_confirmed()),
        MedVaccination(kind="vaccine", name="COVID-19 (updated)", status="completed", dose_number=4,
                       date_given="2024-10-15", administered_by="Greenway Pharmacy"),
        MedVaccination(kind="vaccine", name="Hepatitis B", status="completed", dose_number=3, date_given="1998"),
        MedVaccination(kind="screening", name="Diabetic eye exam", status="completed",
                       date_given=ago(400).isoformat(), administered_by="Clearview Eye Center",
                       next_due_date=ago(35)),  # follow-up date has passed -> shown neutrally
        MedVaccination(kind="screening", name="Colorectal screening", status="scheduled",
                       next_due_date=today + timedelta(days=90), provider_id=pcp.id),
        MedVaccination(kind="preventive_care", name="Annual wellness visit", status="completed",
                       date_given=ago(75).isoformat(), provider_id=pcp.id, next_due_date=today + timedelta(days=290),
                       **_confirmed()),
    ]
    for r in hist + meds + allergies + vaccs:
        r.patient_id = patient_id
    db.add_all(hist + meds + allergies + vaccs)

    # --- results (with trends) -----------------------------------------------------------------------
    from app.models import documents as _docs  # noqa: F401  (FK target must be imported)

    doc_id = None
    try:
        doc_id = db.scalar(select(_docs.Document.id).where(
            _docs.Document.patient_id == patient_id, _docs.Document.deleted_at.is_(None),
            _docs.Document.category == "lab_result").limit(1))
    except Exception:  # pragma: no cover - documents table unavailable
        db.rollback()
        raise

    def res(name, d, value, unit, lo, hi, last=False, **kw):
        extra = dict(document_id=doc_id) if last and doc_id else {}
        return MedResult(test_name=name, test_key=norm_key(name), category="lab", result_date=d,
                         value_num=Decimal(str(value)) if value is not None else None, unit=unit,
                         ref_low=Decimal(str(lo)) if lo is not None else None,
                         ref_high=Decimal(str(hi)) if hi is not None else None,
                         source_name="Northside Lab", **extra, **kw)

    results = []
    a1c = [5.5, 5.6, 5.8, 5.7, 5.8]
    ldl = [118, 124, 121, 135, 126]  # one value above the lab's range
    for i, (a, l) in enumerate(zip(a1c, ldl)):
        d = ago(int((4 - i) * 91 + 20))
        results.append(res("Hemoglobin A1c", d, a, "%", 4.0, 5.9, last=(i == 4), **(_confirmed() if i == 4 else {})))
        results.append(res("LDL cholesterol", d, l, "mg/dL", 0, 129, last=(i == 4)))
    for i, g in enumerate([88, 94, 91, 97]):
        results.append(res("Fasting glucose", ago(int((3 - i) * 120 + 30)), g, "mg/dL", 70, 99))
    results.append(res("Fasting glucose", ago(900), 5.2, "mmol/L", 3.9, 5.5))  # different unit: kept separate
    results.append(res("Creatinine", ago(111), 0.9, "mg/dL", 0.6, 1.3))
    results.append(MedResult(test_name="Urine protein (dipstick)", test_key=norm_key("Urine protein (dipstick)"),
                             category="lab", result_date=ago(111), value_text="Negative", ref_text="Negative",
                             source_name="Lakeside Family Clinic", provider_id=pcp.id))
    for r in results:
        r.patient_id = patient_id
    db.add_all(results)
    db.commit()
    return {"skipped": False, "providers": 5, "history": len(hist), "medications": len(meds),
            "allergies": len(allergies), "vaccinations": len(vaccs), "results": len(results)}
