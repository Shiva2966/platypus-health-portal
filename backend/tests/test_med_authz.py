"""Authorization + isolation: patient-only routes, no cross-patient access, category reads scoped to one patient."""
import pytest

try:
    from test_med_support import MED, mw  # noqa: F401
except ImportError:
    from tests.test_med_support import MED, mw  # noqa: F401

from app.services import medical_read
from app.services.seed_medical import seed_medical

BODIES = {
    "history": {"kind": "condition", "title": "Hay fever"},
    "medications": {"name": "Vitamin C", "is_supplement": True},
    "allergies": {"substance": "Latex"},
    "vaccinations": {"name": "Flu shot"},
    "results": {"test_name": "Hemoglobin A1c", "value_num": 5.6, "unit": "%"},
    "providers": {"name": "Dr. Test"},
}
RES_TYPE = {"history": "history", "medications": "medication", "allergies": "allergy",
            "vaccinations": "vaccination", "results": "result", "providers": "provider"}


@pytest.mark.parametrize("path", [
    "/meta", "/summary", "/timeline", "/results/trends", "/allergy-status", "/directory", "/linkable-documents",
    "/history", "/medications", "/allergies", "/vaccinations", "/results", "/providers", "/corrections",
])
def test_every_get_route_requires_sign_in(mw, path):
    assert mw.anon().get(MED + path).status_code == 401


@pytest.mark.parametrize("res", list(BODIES))
def test_writes_require_sign_in(mw, res):
    c = mw.anon()
    assert c.post(f"{MED}/{res}", json=BODIES[res]).status_code == 401
    assert c.put(f"{MED}/{res}/x", json=BODIES[res]).status_code == 401
    assert c.delete(f"{MED}/{res}/x").status_code == 401
    assert c.put(f"{MED}/allergy-status", json={"status": "unknown"}).status_code == 401
    assert c.post(f"{MED}/corrections", json={"record_type": "history", "record_id": "x", "message": "m"}).status_code == 401


@pytest.mark.parametrize("res", list(BODIES))
def test_other_patient_cannot_read_change_or_delete(mw, res):
    rec = mw.create(mw.A, res, BODIES[res])
    b = mw.client(mw.B)
    assert b.get(f"{MED}/{res}/{rec['id']}").status_code == 404
    assert b.put(f"{MED}/{res}/{rec['id']}", json=BODIES[res]).status_code == 404
    assert b.delete(f"{MED}/{res}/{rec['id']}").status_code == 404
    assert b.get(f"{MED}/{res}").json() == []
    # owner still has it, untouched
    assert mw.client(mw.A).get(f"{MED}/{res}/{rec['id']}").status_code == 200
    # B cannot file a correction about A's record either (same 404 as a missing id)
    r = b.post(f"{MED}/corrections", json={"record_type": RES_TYPE[res], "record_id": rec["id"], "message": "x"})
    assert r.status_code == 404
    assert mw.client(mw.A).get(f"{MED}/corrections").json() == []


def test_cannot_link_another_patients_provider_or_document(mw):
    prov = mw.create(mw.A, "providers", {"name": "Dr. A Only"})
    r = mw.client(mw.B).post(f"{MED}/medications", json={"name": "X", "prescriber_id": prov["id"]})
    assert r.status_code == 422 and "prescriber_id" in r.json()["fields"]
    r = mw.client(mw.B).post(f"{MED}/results", json={"test_name": "T", "value_num": 1, "document_id": "nope"})
    assert r.status_code == 422 and "document_id" in r.json()["fields"]


def test_summary_timeline_trends_are_per_patient(mw):
    seed_medical(mw.db, mw.A.id)
    a, b = mw.client(mw.A), mw.client(mw.B)
    assert a.get(f"{MED}/summary").json()["counts"]["results"] > 0
    assert b.get(f"{MED}/summary").json()["counts"]["results"] == 0
    assert b.get(f"{MED}/timeline").json()["events"] == []
    assert b.get(f"{MED}/results/trends").json()["series"] == []
    assert b.get(f"{MED}/allergy-status").json()["status"] == "unknown"


def test_get_category_for_patient_is_scoped_and_validated(mw):
    seed_medical(mw.db, mw.A.id)
    for cat in medical_read.MED_CATEGORIES:
        assert medical_read.get_category_for_patient(mw.db, mw.A.id, cat)
    meds = medical_read.get_category_for_patient(mw.db, mw.A.id, "medications")
    assert all(m["record_type"] == "medication" and m["source_label"] and m["updated_at"] for m in meds)
    # B has nothing, and the allergy category still tells "Unknown" apart from "none"
    for cat in ("history", "medications", "vaccinations", "results"):
        assert medical_read.get_category_for_patient(mw.db, mw.B.id, cat) == []
    al = medical_read.get_category_for_patient(mw.db, mw.B.id, "allergies")
    assert al == [al[0]] and al[0]["status"] == "unknown"
    for bad in ("billing", "documents", "", "history; DROP TABLE x"):
        with pytest.raises(ValueError):
            medical_read.get_category_for_patient(mw.db, mw.A.id, bad)


def test_api_never_returns_other_patients_ids_in_lists(mw):
    seed_medical(mw.db, mw.A.id)
    seed_medical(mw.db, mw.B.id)
    for res in BODIES:
        ids_a = {x["id"] for x in mw.client(mw.A).get(f"{MED}/{res}").json()}
        ids_b = {x["id"] for x in mw.client(mw.B).get(f"{MED}/{res}").json()}
        assert ids_a and ids_b and not (ids_a & ids_b)
