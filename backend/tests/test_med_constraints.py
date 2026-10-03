"""Database constraints (real CHECK/UNIQUE/FK) and API validation for the medical tables."""
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

try:
    from test_med_support import MED, mw  # noqa: F401
except ImportError:
    from tests.test_med_support import MED, mw  # noqa: F401

from app.db import engine
from app.models.medical import (MedAllergy, MedCorrection, MedHistory, MedMedication, MedProvider, MedResult,
                                MedVaccination)
from app.models.shared import Patient


def insert(mw, row):
    mw.db.add(row)
    with pytest.raises(IntegrityError):
        mw.db.flush()
    mw.db.rollback()


def test_check_constraints_reject_bad_values(mw):
    pid = mw.A.id
    insert(mw, MedHistory(patient_id=pid, kind="rumor", title="x"))
    insert(mw, MedHistory(patient_id=pid, kind="condition", title="x", status="cured"))
    insert(mw, MedHistory(patient_id=pid, kind="condition", title="x", start_date="19"))
    insert(mw, MedHistory(patient_id=pid, kind="condition", title="x", source="self-declared-doctor"))
    insert(mw, MedHistory(patient_id=pid, kind="condition", title=""))
    insert(mw, MedMedication(patient_id=pid, name="x", status="maybe"))
    insert(mw, MedAllergy(patient_id=pid, substance="x", substance_key="x", severity="deadly"))
    insert(mw, MedVaccination(patient_id=pid, name="x", dose_number=0))
    insert(mw, MedVaccination(patient_id=pid, name="x", status="done"))
    insert(mw, MedProvider(patient_id=pid, name="x", name_key="x", kind="wizard"))
    insert(mw, MedCorrection(patient_id=pid, record_type="history", record_id="r", message="m", status="done"))
    insert(mw, MedCorrection(patient_id=pid, record_type="bank", record_id="r", message="m"))


def test_result_needs_a_value_and_ordered_reference_range(mw):
    pid = mw.A.id
    insert(mw, MedResult(patient_id=pid, test_name="T", test_key="t"))
    insert(mw, MedResult(patient_id=pid, test_name="T", test_key="t", value_num=Decimal("1"),
                         ref_low=Decimal("9"), ref_high=Decimal("2")))
    ok = MedResult(patient_id=pid, test_name="T", test_key="t", value_text="Negative")
    mw.db.add(ok)
    mw.db.flush()
    mw.db.rollback()


def test_unique_allergy_and_provider_per_patient(mw):
    pid = mw.A.id
    mw.db.add(MedAllergy(patient_id=pid, substance="Latex", substance_key="latex"))
    mw.db.add(MedProvider(patient_id=pid, name="Dr Q", name_key="dr q"))
    mw.db.commit()
    insert(mw, MedAllergy(patient_id=pid, substance="LATEX", substance_key="latex"))
    insert(mw, MedProvider(patient_id=pid, name="DR Q", name_key="dr q"))
    mw.db.add(MedAllergy(patient_id=mw.B.id, substance="Latex", substance_key="latex"))  # different patient is fine
    mw.db.commit()


def test_foreign_keys_enforced_and_cascade_on_patient_delete(mw):
    insert(mw, MedHistory(patient_id="00000000-0000-0000-0000-000000000000", kind="condition", title="orphan"))
    p = mw.new_patient("Cascade Test")
    prov = MedProvider(patient_id=p.id, name="Dr C", name_key="dr c")
    mw.db.add(prov)
    mw.db.flush()
    mw.db.add_all([MedHistory(patient_id=p.id, kind="condition", title="c", provider_id=prov.id),
                   MedResult(patient_id=p.id, test_name="T", test_key="t", value_num=Decimal("1"))])
    mw.db.commit()
    mw.db.execute(text("DELETE FROM patients WHERE id = :i"), {"i": p.id})
    mw.db.commit()
    for model in (MedHistory, MedResult, MedProvider):
        assert mw.db.query(model).filter_by(patient_id=p.id).count() == 0


def test_deleting_a_provider_keeps_records_and_nulls_the_link(mw):
    prov = mw.create(mw.A, "providers", {"name": "Dr Link"})
    med = mw.create(mw.A, "medications", {"name": "Zinc", "prescriber_id": prov["id"]})
    assert mw.client(mw.A).delete(f"{MED}/providers/{prov['id']}").status_code == 200
    again = mw.client(mw.A).get(f"{MED}/medications/{med['id']}").json()
    assert again["prescriber_id"] is None and again["name"] == "Zinc"


def test_every_foreign_key_and_searched_column_is_indexed(mw):
    insp = inspect(engine)
    for table in ("med_providers", "med_history", "med_medications", "med_allergies", "med_vaccinations",
                  "med_results", "med_corrections"):
        indexed = {tuple(i["column_names"][:1]) for i in insp.get_indexes(table)}
        indexed |= {tuple(u["column_names"][:1]) for u in insp.get_unique_constraints(table)}
        for fk in insp.get_foreign_keys(table):
            assert tuple(fk["constrained_columns"][:1]) in indexed, (table, fk)
            assert fk["options"].get("ondelete"), (table, fk)
        cols = {c["name"]: c for c in insp.get_columns(table)}
        assert cols["patient_id"]["nullable"] is False


def test_timestamps_are_timezone_aware_type_and_portable_types():
    from sqlalchemy import Boolean, Date, Numeric, String
    from app.db_ops.types import UTCDateTime

    assert isinstance(MedResult.__table__.c.created_at.type, UTCDateTime)
    assert isinstance(MedResult.__table__.c.value_num.type, Numeric)  # never float
    assert isinstance(MedResult.__table__.c.result_date.type, Date)
    assert isinstance(MedVaccination.__table__.c.next_due_date.type, Date)
    assert isinstance(MedProvider.__table__.c.is_care_team.type, Boolean)
    assert MedHistory.__table__.c.id.type.length == 36 and isinstance(MedHistory.__table__.c.id.type, String)


# ----------------------------------------------------------------------------- API validation
def test_api_validation_messages_and_partial_dates(mw):
    c = mw.client(mw.A)
    r = c.post(f"{MED}/history", json={"kind": "condition", "title": "x", "start_date": "2019-13"})
    assert r.status_code == 422 and "start_date" in r.json()["fields"]
    r = c.post(f"{MED}/history", json={"kind": "condition", "title": "x", "start_date": "2020", "end_date": "2019-05"})
    assert r.status_code == 422 and "end_date" in r.json()["fields"]
    ok = c.post(f"{MED}/history", json={"kind": "surgery", "title": "Knee scope", "start_date": "2012"})
    assert ok.status_code == 201 and ok.json()["start_date"] == "2012" and ok.json()["status"] == "unknown"
    r = c.post(f"{MED}/medications", json={"name": "A", "status": "active", "end_date": "2020"})
    assert r.status_code == 422 and "end_date" in r.json()["fields"]
    r = c.post(f"{MED}/results", json={"test_name": "T"})
    assert r.status_code == 422 and "value_num" in r.json()["fields"]
    r = c.post(f"{MED}/results", json={"test_name": "T", "value_num": 5, "ref_low": 9, "ref_high": 2})
    assert r.status_code == 422 and "ref_low" in r.json()["fields"]
    r = c.post(f"{MED}/results", json={"test_name": "T", "value_num": 5, "result_date": "2999-01-01"})
    assert r.status_code == 422 and "result_date" in r.json()["fields"]
    r = c.post(f"{MED}/vaccinations", json={"name": "V", "next_due_date": "soon"})
    assert r.status_code == 422 and "next_due_date" in r.json()["fields"]


def test_blank_optional_fields_mean_unknown(mw):
    c = mw.client(mw.A)
    r = c.post(f"{MED}/vaccinations", json={"name": "Flu", "date_given": "", "next_due_date": "", "dose_number": None,
                                            "administered_by": "  "})
    assert r.status_code == 201
    body = r.json()
    assert body["date_given"] is None and body["next_due_date"] is None and body["administered_by"] is None


def test_patient_cannot_set_source_or_confirmation(mw):
    for extra in ({"source": "clinician_confirmed"}, {"confirmed_by": "Dr X"}, {"patient_id": mw.B.id}, {"id": "abc"}):
        r = mw.client(mw.A).post(f"{MED}/medications", json={"name": "Sneaky", **extra})
        assert r.status_code == 422, extra
    m = mw.create(mw.A, "medications", {"name": "Honest"})
    assert m["source"] == "patient_entered" and m["source_label"] == "Patient-entered" and m["confirmed_by"] is None
    assert m["updated_at"] and m["created_at"]


def test_clinician_confirmed_records_are_locked_but_updatable_by_staff_service(mw):
    from app.services import medical_write as W

    m = mw.create(mw.A, "medications", {"name": "Lisinopril"})
    W.confirm_record(mw.db, patient_id=mw.A.id, res="medications", rec_id=m["id"], confirmed_by="Dr. Demo")
    mw.db.commit()
    c = mw.client(mw.A)
    got = c.get(f"{MED}/medications/{m['id']}").json()
    assert got["source"] == "clinician_confirmed" and got["source_label"] == "Clinician-confirmed"
    assert got["confirmed_by"] == "Dr. Demo" and got["can_edit"] is False
    r = c.put(f"{MED}/medications/{m['id']}", json={"name": "Changed"})
    assert r.status_code == 409 and "correction" in r.json()["detail"].lower()
    assert c.delete(f"{MED}/medications/{m['id']}").status_code == 409
    assert c.get(f"{MED}/medications/{m['id']}").json()["name"] == "Lisinopril"


def test_update_changes_updated_at_and_keeps_owner(mw):
    c = mw.client(mw.A)
    m = mw.create(mw.A, "medications", {"name": "Iron", "is_supplement": True})
    r = c.put(f"{MED}/medications/{m['id']}", json={"name": "Iron", "is_supplement": True, "dose": "25 mg"})
    assert r.status_code == 200 and r.json()["dose"] == "25 mg" and r.json()["updated_at"] >= m["updated_at"]
    row = mw.db.get(MedMedication, m["id"])
    mw.db.refresh(row)
    assert row.patient_id == mw.A.id


def test_audit_entries_written_without_record_values(mw):
    from app.models.shared import AuditLog

    m = mw.create(mw.A, "medications", {"name": "SecretDrugName"})
    mw.db.expire_all()
    rows = mw.db.query(AuditLog).filter_by(patient_id=mw.A.id, resource_id=m["id"]).all()
    assert rows and rows[0].action == "med_medications_created"
    assert all("SecretDrugName" not in (r.detail or "") for r in rows)


def test_required_fields_have_friendly_messages(mw):
    r = mw.client(mw.A).post(f"{MED}/results", json={"test_name": "", "category": "bogus"})
    assert r.status_code == 422
    f = r.json()["fields"]
    assert f["test_name"] == "Please fill this in." and f["category"] == "Please choose one of the options."
