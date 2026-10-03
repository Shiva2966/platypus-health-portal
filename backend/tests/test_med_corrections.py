"""Correction-request workflow (stored; staff resolve later via the service)."""
import pytest

try:
    from test_med_support import MED, mw  # noqa: F401
except ImportError:
    from tests.test_med_support import MED, mw  # noqa: F401

from app.models.shared import AuditLog, Notification
from app.services import medical_read, medical_write as W


def ask(mw, patient, rtype, rid, msg="The dose is wrong."):
    return mw.client(patient).post(f"{MED}/corrections", json={"record_type": rtype, "record_id": rid, "message": msg})


def test_create_list_and_snapshot_label(mw):
    m = mw.create(mw.A, "medications", {"name": "Metformin"})
    r = ask(mw, mw.A, "medication", m["id"])
    assert r.status_code == 201
    c = r.json()
    assert c["status"] == "open" and c["record_label"] == "Metformin" and c["record_id"] == m["id"]
    lst = mw.client(mw.A).get(f"{MED}/corrections").json()
    assert [x["id"] for x in lst] == [c["id"]]
    assert mw.client(mw.A).get(f"{MED}/corrections", params={"status": "resolved"}).json() == []
    assert mw.client(mw.B).get(f"{MED}/corrections").json() == []
    # the record itself is NOT changed by a correction request
    assert mw.client(mw.A).get(f"{MED}/medications/{m['id']}").json()["name"] == "Metformin"
    mw.db.expire_all()
    assert mw.db.query(AuditLog).filter_by(patient_id=mw.A.id, action="med_correction_requested").count() == 1


def test_validation_and_duplicates(mw):
    m = mw.create(mw.A, "allergies", {"substance": "Latex"})
    assert ask(mw, mw.A, "allergy", m["id"], "").status_code == 422
    assert ask(mw, mw.A, "allergy", m["id"], "x" * 2001).status_code == 422
    assert ask(mw, mw.A, "bank_account", m["id"]).status_code == 422
    assert ask(mw, mw.A, "allergy", "no-such-id").status_code == 404
    assert ask(mw, mw.A, "medication", m["id"]).status_code == 404  # right id, wrong type
    assert ask(mw, mw.A, "allergy", m["id"]).status_code == 201
    dup = ask(mw, mw.A, "allergy", m["id"])
    assert dup.status_code == 422 and "message" in dup.json()["fields"]


def test_withdraw_only_own_and_only_open(mw):
    m = mw.create(mw.A, "history", {"kind": "condition", "title": "Hay fever"})
    c = ask(mw, mw.A, "history", m["id"]).json()
    assert mw.client(mw.B).post(f"{MED}/corrections/{c['id']}/withdraw").status_code == 404
    r = mw.client(mw.A).post(f"{MED}/corrections/{c['id']}/withdraw")
    assert r.status_code == 200 and r.json()["status"] == "withdrawn"
    assert mw.client(mw.A).post(f"{MED}/corrections/{c['id']}/withdraw").status_code == 422
    # after withdrawing, a new request is allowed
    assert ask(mw, mw.A, "history", m["id"], "Another note").status_code == 201


def test_confirmed_record_can_be_corrected_by_request(mw):
    m = mw.create(mw.A, "medications", {"name": "Atorvastatin"})
    W.confirm_record(mw.db, patient_id=mw.A.id, res="medications", rec_id=m["id"], confirmed_by="Dr. Demo")
    mw.db.commit()
    assert mw.client(mw.A).put(f"{MED}/medications/{m['id']}", json={"name": "X"}).status_code == 409
    assert ask(mw, mw.A, "medication", m["id"], "I stopped taking this.").status_code == 201


def test_staff_service_resolves_and_notifies_patient(mw):
    m = mw.create(mw.A, "medications", {"name": "Ibuprofen"})
    c = ask(mw, mw.A, "medication", m["id"]).json()
    # staff side reads open corrections (after W2/W3 authorization) ...
    opened = medical_read.list_corrections(mw.db, mw.A.id, "open")
    assert [x["id"] for x in opened] == [c["id"]]
    # ... and resolves one
    W.resolve_correction(mw.db, correction_id=c["id"], status="resolved", resolved_by="Nurse Demo", note="Updated.")
    mw.db.commit()
    got = mw.client(mw.A).get(f"{MED}/corrections").json()[0]
    assert got["status"] == "resolved" and got["resolved_by"] == "Nurse Demo" and got["resolution_note"] == "Updated."
    assert mw.db.query(Notification).filter_by(recipient_type="patient", recipient_id=mw.A.id,
                                               kind="correction_update").count() == 1
    with pytest.raises(ValueError):
        W.resolve_correction(mw.db, correction_id=c["id"], status="resolved", resolved_by="x")
    with pytest.raises(ValueError):
        W.resolve_correction(mw.db, correction_id=c["id"], status="deleted", resolved_by="x")
    assert mw.client(mw.A).post(f"{MED}/corrections/{c['id']}/withdraw").status_code == 422


def test_deleting_a_record_closes_its_open_correction(mw):
    m = mw.create(mw.A, "vaccinations", {"name": "Flu"})
    c = ask(mw, mw.A, "vaccination", m["id"]).json()
    assert mw.client(mw.A).delete(f"{MED}/vaccinations/{m['id']}").status_code == 200
    got = [x for x in mw.client(mw.A).get(f"{MED}/corrections").json() if x["id"] == c["id"]][0]
    assert got["status"] == "withdrawn" and "removed" in got["resolution_note"]
    assert got["record_label"] == "Flu"  # label snapshot survives
