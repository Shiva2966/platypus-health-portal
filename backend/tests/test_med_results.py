"""Results, trends, neutral range flags, timeline, follow-ups, seed data."""
from datetime import timedelta

try:
    from test_med_support import MED, mw  # noqa: F401
except ImportError:
    from tests.test_med_support import MED, mw  # noqa: F401

from app.models.shared import utcnow
from app.services import medical_read
from app.services.seed_medical import seed_medical


def post_result(mw, patient, name, value, date, unit="mg/dL", lo=None, hi=None, **kw):
    return mw.create(patient, "results", {"test_name": name, "value_num": value, "result_date": date, "unit": unit,
                                          "ref_low": lo, "ref_high": hi, **kw})


def d(days_ago):
    return (utcnow().date() - timedelta(days=days_ago)).isoformat()


def test_range_flag_is_neutral_and_uses_only_the_labs_range(mw):
    inside = post_result(mw, mw.A, "LDL", 100, d(5), lo=0, hi=129)
    above = post_result(mw, mw.A, "LDL", 135, d(4), lo=0, hi=129)
    below = post_result(mw, mw.A, "Ferritin", 5, d(3), "ng/mL", lo=20, hi=200)
    edge = post_result(mw, mw.A, "LDL", 129, d(2), lo=0, hi=129)
    no_range = post_result(mw, mw.A, "Other", 7, d(1))
    assert inside["range_status"] == "within" and inside["flag_text"] is None and not inside["outside_range"]
    assert above["outside_range"] and above["flag_text"] == "Outside the lab's reference range"
    assert above["range_direction"] == "above" and below["range_direction"] == "below"
    assert edge["range_status"] == "within"  # boundaries are inside
    assert no_range["range_status"] == "unknown" and no_range["flag_text"] is None
    blob = " ".join(str(v) for r in (inside, above, below, edge, no_range) for v in r.values()).lower()
    for banned in ("abnormal", "diagnos", "disease", "dangerous", "you should", "see a doctor", "critical"):
        assert banned not in blob.replace("not a diagnosis", "")


def test_qualitative_result_has_no_numeric_flag(mw):
    r = mw.create(mw.A, "results", {"test_name": "Urine protein", "value_text": "Negative", "ref_text": "Negative"})
    assert r["value_num"] is None and r["range_status"] == "unknown" and r["ref_display"] == "Negative"


def test_trend_series_are_ordered_and_comparable(mw):
    for i, v in enumerate([5.5, 5.7, 5.9, 6.1]):
        post_result(mw, mw.A, "Hemoglobin A1c", v, d(300 - i * 90), "%", 4.0, 5.9)
    post_result(mw, mw.A, "Hemoglobin A1c", 5.8, d(5000) if False else d(2), "%", 4.0, 5.9)
    t = mw.client(mw.A).get(f"{MED}/results/trends").json()
    assert "not a diagnosis" in t["disclaimer"].lower()
    (s,) = [x for x in t["series"] if x["test_name"] == "Hemoglobin A1c"]
    assert s["comparable"] and s["count"] == 5 and s["unit"] == "%"
    dates = [p["date"] for p in s["points"]]
    assert dates == sorted(dates)
    assert [p["value"] for p in s["points"]][:4] == [5.5, 5.7, 5.9, 6.1]
    assert [p["outside_range"] for p in s["points"]] == [False, False, False, True, False]
    assert s["any_outside"] and s["latest"]["value"] == 5.8


def test_name_matching_is_case_and_space_insensitive_but_units_are_never_mixed(mw):
    post_result(mw, mw.A, "Fasting glucose", 90, d(400), "mg/dL", 70, 99)
    post_result(mw, mw.A, "  fasting   GLUCOSE ", 94, d(300), "MG/DL", 70, 99)
    post_result(mw, mw.A, "Fasting glucose", 5.1, d(200), "mmol/L", 3.9, 5.5)
    t = mw.client(mw.A).get(f"{MED}/results/trends").json()
    series = {s["unit"] or "": s for s in t["series"] if s["test_name"].lower().strip().startswith("fasting")}
    assert len(series) == 2
    mg = next(s for u, s in series.items() if u.lower() == "mg/dl")
    mmol = series["mmol/L"]
    assert mg["count"] == 2 and mg["comparable"] and mg["other_units"] == ["mmol/L"]
    assert "different unit" in mg["unit_note"]
    assert mmol["count"] == 1 and mmol["comparable"] is False


def test_undated_and_non_numeric_results_are_not_plotted(mw):
    mw.create(mw.A, "results", {"test_name": "Potassium", "value_num": 4.1, "unit": "mmol/L"})  # no date
    mw.create(mw.A, "results", {"test_name": "Potassium", "value_text": "Normal", "result_date": d(3)})
    assert mw.client(mw.A).get(f"{MED}/results/trends").json()["series"] == []
    assert len(mw.client(mw.A).get(f"{MED}/results").json()) == 2  # still listed


def test_trend_key_filter(mw):
    post_result(mw, mw.A, "LDL", 100, d(30), lo=0, hi=129)
    post_result(mw, mw.A, "LDL", 110, d(10), lo=0, hi=129)
    post_result(mw, mw.A, "HDL", 50, d(10), lo=40, hi=100)
    c = mw.client(mw.A)
    all_ = c.get(f"{MED}/results/trends").json()["series"]
    assert len(all_) == 2
    one = c.get(f"{MED}/results/trends", params={"key": all_[0]["key"]}).json()["series"]
    assert len(one) == 1 and one[0]["key"] == all_[0]["key"]


def test_seed_has_trends_and_exactly_one_out_of_range_ldl(mw):
    out = seed_medical(mw.db, mw.A.id)
    assert out["skipped"] is False and out["results"] >= 10
    assert seed_medical(mw.db, mw.A.id) == {"skipped": True}  # idempotent
    t = mw.client(mw.A).get(f"{MED}/results/trends").json()
    by = {s["test_name"]: s for s in t["series"] if s["unit"] in ("%", "mg/dL")}
    assert by["Hemoglobin A1c"]["count"] >= 4 and by["LDL cholesterol"]["count"] >= 4
    assert sum(p["outside_range"] for p in by["LDL cholesterol"]["points"]) == 1
    assert by["Hemoglobin A1c"]["any_outside"] is False
    assert mw.fresh(mw.A).allergy_status == "has_allergies"
    data = medical_read.summary(mw.db, mw.A.id)
    assert data["counts"]["active_medications"] >= 2 and data["counts"]["allergies"] == 2
    assert data["allergy"]["status"] == "has_allergies"


def test_timeline_is_newest_first_with_undated_listed_separately(mw):
    c = mw.client(mw.A)
    mw.create(mw.A, "history", {"kind": "condition", "title": "Old", "start_date": "1999"})
    mw.create(mw.A, "history", {"kind": "surgery", "title": "Newer", "start_date": "2015-06-01"})
    mw.create(mw.A, "history", {"kind": "condition", "title": "Mid", "start_date": "2010-03"})
    mw.create(mw.A, "history", {"kind": "family_history", "title": "Heart disease", "relation": "Father"})
    t = c.get(f"{MED}/timeline", params={"types": "history"}).json()
    assert [e["title"] for e in t["events"]] == ["Newer", "Mid", "Old"]
    assert [e["title"] for e in t["undated"]] == ["Heart disease"]
    assert all(e["source_label"] and e["updated_at"] for e in t["events"] + t["undated"])
    only_results = c.get(f"{MED}/timeline", params={"types": "results"}).json()
    assert only_results["events"] == []


def test_vaccination_followups_are_labelled_neutrally(mw):
    seed_medical(mw.db, mw.A.id)
    vs = {v["name"]: v for v in mw.client(mw.A).get(f"{MED}/vaccinations").json()}
    assert vs["Diabetic eye exam"]["followup_state"] == "passed"
    assert vs["Diabetic eye exam"]["followup_label"] == "Follow-up date has passed"
    assert vs["Influenza (seasonal)"]["followup_state"] == "soon"
    assert vs["Colorectal screening"]["followup_state"] == "scheduled"
    s = mw.client(mw.A).get(f"{MED}/summary").json()
    assert {f["name"] for f in s["followups"]} >= {"Diabetic eye exam", "Influenza (seasonal)"}


def test_medications_active_past_and_supplements(mw):
    seed_medical(mw.db, mw.A.id)
    c = mw.client(mw.A)
    active = c.get(f"{MED}/medications", params={"status": "active"}).json()
    past = c.get(f"{MED}/medications", params={"status": "past"}).json()
    assert {m["status"] for m in active} == {"active"} and {m["status"] for m in past} == {"past"}
    assert any(m["is_supplement"] for m in active)
    assert {m["source"] for m in active} == {"patient_entered", "clinician_confirmed"}


def test_result_can_link_own_document(mw):
    import uuid

    from app.models.documents import Document, DocumentBlob

    doc = Document(patient_id=mw.A.id, name="Lab report (synthetic)", category="lab_result", mime_type="application/pdf",
                   size_bytes=5, sha256=uuid.uuid4().hex * 2)
    mw.db.add(doc)
    mw.db.flush()
    mw.db.add(DocumentBlob(document_id=doc.id, data=b"%PDF-"))
    mw.db.commit()
    # another patient can't link it
    r = mw.client(mw.B).post(f"{MED}/results", json={"test_name": "LDL", "value_num": 1, "document_id": doc.id})
    assert r.status_code == 422 and "document_id" in r.json()["fields"]
    r = mw.create(mw.A, "results", {"test_name": "LDL", "value_num": 100, "document_id": doc.id})
    assert r["document_id"] == doc.id
    docs = mw.client(mw.A).get(f"{MED}/linkable-documents").json()
    assert [x["id"] for x in docs] == [doc.id]
    assert mw.client(mw.B).get(f"{MED}/linkable-documents").json() == []


def test_recent_results_for_dashboard_and_ui_modules(mw):
    seed_medical(mw.db, mw.A.id)
    assert mw.anon().get("/api/results/recent").status_code == 401
    r = mw.client(mw.A).get("/api/results/recent", params={"limit": 3}).json()
    assert len(r) == 3 and all(x["test_name"] and "flag" in x for x in r)
    assert [x["result_date"] for x in r] == sorted((x["result_date"] for x in r), reverse=True)
    assert mw.client(mw.B).get("/api/results/recent").json() == []
    ldl = [x for x in mw.client(mw.A).get("/api/results/recent", params={"limit": 50}).json() if x["flag"]]
    assert len(ldl) == 1 and ldl[0]["flag"] == "high" and ldl[0]["flag_text"] == "Outside the lab's reference range"
    scripts = mw.anon().get("/api/ui-modules").json()["scripts"]
    assert any(s.endswith("/med_common.js") for s in scripts) and any(s.endswith("/med_results.js") for s in scripts)
