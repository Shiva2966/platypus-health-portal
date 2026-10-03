"""Allergies: explicit "No known allergies" vs "Unknown" must never be confused."""
try:
    from test_med_support import MED, mw  # noqa: F401
except ImportError:
    from tests.test_med_support import MED, mw  # noqa: F401

from app.services import medical_read


def status(mw, p):
    return mw.client(p).get(f"{MED}/allergy-status").json()


def test_new_patient_is_unknown_not_none(mw):
    s = status(mw, mw.A)
    assert s["status"] == "unknown" and s["unknown"] is True and s["explicit_none"] is False
    assert "Unknown" in s["label"]


def test_explicit_nka_is_stored_and_distinct(mw):
    r = mw.client(mw.A).put(f"{MED}/allergy-status", json={"status": "no_known_allergies"})
    assert r.status_code == 200
    s = status(mw, mw.A)
    assert s["status"] == "no_known_allergies" and s["explicit_none"] is True and s["unknown"] is False
    assert s["label"] == "No known allergies"
    assert mw.fresh(mw.A).allergy_status == "no_known_allergies"  # single source of truth shared with intake
    first = medical_read.get_category_for_patient(mw.db, mw.A.id, "allergies")[0]
    assert first["record_type"] == "allergy_status" and first["explicit_none"] is True
    # the other patient is untouched
    assert status(mw, mw.B)["status"] == "unknown"


def test_adding_an_allergy_switches_to_has_allergies(mw):
    mw.client(mw.A).put(f"{MED}/allergy-status", json={"status": "no_known_allergies"})
    mw.create(mw.A, "allergies", {"substance": "Penicillin", "category": "drug", "severity": "moderate"})
    assert status(mw, mw.A)["status"] == "has_allergies"
    assert mw.fresh(mw.A).allergy_status == "has_allergies"


def test_cannot_state_nka_or_unknown_while_allergies_are_active(mw):
    mw.create(mw.A, "allergies", {"substance": "Peanuts"})
    for st in ("no_known_allergies", "unknown"):
        r = mw.client(mw.A).put(f"{MED}/allergy-status", json={"status": st})
        assert r.status_code == 422 and "status" in r.json()["fields"]
    assert status(mw, mw.A)["status"] == "has_allergies"


def test_inactive_allergy_allows_nka(mw):
    a = mw.create(mw.A, "allergies", {"substance": "Shellfish"})
    r = mw.client(mw.A).put(f"{MED}/allergies/{a['id']}", json={"substance": "Shellfish", "status": "inactive"})
    assert r.status_code == 200
    assert status(mw, mw.A)["status"] == "unknown"  # list emptied -> never silently "none"
    assert mw.client(mw.A).put(f"{MED}/allergy-status", json={"status": "no_known_allergies"}).status_code == 200


def test_removing_last_allergy_reverts_to_unknown_never_nka(mw):
    a = mw.create(mw.A, "allergies", {"substance": "Latex"})
    assert mw.client(mw.A).delete(f"{MED}/allergies/{a['id']}").status_code == 200
    assert status(mw, mw.A)["status"] == "unknown"
    assert mw.fresh(mw.A).allergy_status == "unknown"


def test_bad_status_values_rejected(mw):
    for bad in ("has_allergies", "none", "", None):
        r = mw.client(mw.A).put(f"{MED}/allergy-status", json={"status": bad})
        assert r.status_code in (422,), (bad, r.text)


def test_duplicate_allergy_gives_friendly_error_and_other_patient_may_have_same(mw):
    mw.create(mw.A, "allergies", {"substance": "Penicillin"})
    r = mw.client(mw.A).post(f"{MED}/allergies", json={"substance": "  penicillin "})
    assert r.status_code == 422 and "substance" in r.json()["fields"]
    mw.create(mw.B, "allergies", {"substance": "Penicillin"})  # other patient: fine
    assert len(mw.client(mw.A).get(f"{MED}/allergies").json()) == 1


def test_only_substance_is_required_and_everything_else_may_be_unknown(mw):
    a = mw.create(mw.A, "allergies", {"substance": "Dust"})
    assert a["severity"] == "unknown" and a["category"] == "unknown" and a["reaction"] is None and a["onset"] is None
    assert mw.client(mw.A).post(f"{MED}/allergies", json={}).status_code == 422
