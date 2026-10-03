"""Cost & provider search over a SEEDED MOCK directory (W8).  Synthetic data only.

Everything here is a mock: the providers, distances, appointment openings and prices are made up so the demo works
offline.  Prices are ESTIMATE RANGES with stated uncertainty - never quotes.  Money is ``Decimal``.

search_providers(...) filters: service, specialty, text, distance (miles from a mock home area), plan network,
accessibility needs, date window for openings.  Sort: distance | cost | soonest.
"""
from __future__ import annotations

import hashlib
import math
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from app.models.shared import utcnow
from app.services.eob_explainer import D

LABEL = ("ESTIMATE ONLY - mock data for this demo. Real prices depend on your plan, what is actually done, and "
         "the provider's final bill. Ask the provider and your insurer for a written estimate.")

# Mock "home areas" so distance can be shown without real addresses.  (fictional town of Springfield)
AREAS = {
    "downtown": {"label": "Downtown Springfield (mock)", "lat": 39.7990, "lon": -89.6500},
    "northgate": {"label": "Northgate (mock)", "lat": 39.8700, "lon": -89.6400},
    "riverside": {"label": "Riverside (mock)", "lat": 39.7600, "lon": -89.7200},
    "eastfield": {"label": "Eastfield (mock)", "lat": 39.8000, "lon": -89.5200},
}
DEFAULT_AREA = "downtown"

ACCESSIBILITY = {
    "wheelchair": "Wheelchair accessible",
    "accessible_parking": "Accessible parking",
    "asl_interpreter": "Sign language interpreter available",
    "hearing_loop": "Hearing loop",
    "sensory_friendly": "Sensory-friendly rooms",
    "telehealth": "Video visits",
    "step_free_entry": "Step-free entry",
}

# service key -> (label, typical full price low, high, copay style visit?, needs specialty keyword)
SERVICES: dict[str, dict[str, Any]] = {
    "primary_care_visit": {"label": "Primary care office visit", "low": "120", "high": "260", "visit": True},
    "specialist_visit": {"label": "Specialist office visit", "low": "220", "high": "480", "visit": True},
    "urgent_care_visit": {"label": "Urgent care visit", "low": "180", "high": "420", "visit": True},
    "telehealth_visit": {"label": "Video visit", "low": "70", "high": "160", "visit": True},
    "lab_panel": {"label": "Basic blood test panel", "low": "60", "high": "210", "visit": False},
    "xray": {"label": "X-ray (one area)", "low": "130", "high": "380", "visit": False},
    "mri": {"label": "MRI scan", "low": "900", "high": "2600", "visit": False},
    "physical_therapy_session": {"label": "Physical therapy session", "low": "110", "high": "230", "visit": True},
}

_ALL_A11Y = list(ACCESSIBILITY)

# id, name, specialty, lat, lon, networks (mock insurer names), price_factor, services, access flags, weekly openings
MOCK_PROVIDERS: list[dict[str, Any]] = [
    {"id": "mock-riverside-general", "name": "Riverside General Hospital", "kind": "Hospital", "specialty": "Hospital",
     "address": "100 Demo Way, Springfield (synthetic)", "lat": 39.7650, "lon": -89.7100,
     "networks": ["Evergreen Health Plan", "Pinecrest Supplemental"], "price_factor": "1.30",
     "services": ["urgent_care_visit", "xray", "mri", "lab_panel", "specialist_visit"],
     "access": ["wheelchair", "accessible_parking", "asl_interpreter", "hearing_loop", "step_free_entry"],
     "weekly": {"Mon": ["08:30", "13:00"], "Tue": ["09:00", "14:30"], "Wed": ["08:30"], "Thu": ["10:00", "15:00"], "Fri": ["08:30", "11:00"]}},
    {"id": "mock-lakeside-family", "name": "Lakeside Family Clinic", "kind": "Clinic", "specialty": "Family medicine",
     "address": "22 Sample Rd, Springfield (synthetic)", "lat": 39.8100, "lon": -89.6200,
     "networks": ["Evergreen Health Plan", "Harborview Mutual"], "price_factor": "0.90",
     "services": ["primary_care_visit", "lab_panel", "telehealth_visit"],
     "access": ["wheelchair", "accessible_parking", "telehealth", "step_free_entry"],
     "weekly": {"Mon": ["09:00", "10:30"], "Wed": ["13:00", "15:30"], "Fri": ["09:00"]}},
    {"id": "mock-northgate-imaging", "name": "Northgate Imaging Center", "kind": "Imaging", "specialty": "Radiology",
     "address": "8 Scan Ave, Springfield (synthetic)", "lat": 39.8750, "lon": -89.6350,
     "networks": ["Harborview Mutual"], "price_factor": "0.75",
     "services": ["mri", "xray"],
     "access": ["wheelchair", "accessible_parking", "step_free_entry", "sensory_friendly"],
     "weekly": {"Tue": ["07:30", "12:00"], "Thu": ["07:30", "12:00", "16:00"], "Sat": ["09:00"]}},
    {"id": "mock-summit-pt", "name": "Summit Physical Therapy", "kind": "Clinic", "specialty": "Physical therapy",
     "address": "45 Mock St, Springfield (synthetic)", "lat": 39.7950, "lon": -89.5600,
     "networks": ["Evergreen Health Plan"], "price_factor": "1.00",
     "services": ["physical_therapy_session"],
     "access": ["wheelchair", "accessible_parking", "step_free_entry"],
     "weekly": {"Mon": ["08:00", "16:00"], "Tue": ["08:00"], "Thu": ["08:00", "16:00"]}},
    {"id": "mock-eastfield-urgent", "name": "Eastfield Urgent Care", "kind": "Urgent care", "specialty": "Urgent care",
     "address": "310 Fake Blvd, Springfield (synthetic)", "lat": 39.8020, "lon": -89.5150,
     "networks": ["Harborview Mutual", "Pinecrest Supplemental"], "price_factor": "1.10",
     "services": ["urgent_care_visit", "xray", "lab_panel"],
     "access": ["wheelchair", "accessible_parking", "step_free_entry", "hearing_loop"],
     "weekly": {"Mon": ["12:00", "18:00"], "Tue": ["12:00", "18:00"], "Wed": ["12:00", "18:00"], "Sat": ["10:00", "14:00"], "Sun": ["10:00"]}},
    {"id": "mock-greenway-pediatrics", "name": "Greenway Primary Care", "kind": "Clinic", "specialty": "Family medicine",
     "address": "9 Placeholder Ln, Springfield (synthetic)", "lat": 39.8400, "lon": -89.6900,
     "networks": ["Evergreen Health Plan", "Harborview Mutual"], "price_factor": "0.95",
     "services": ["primary_care_visit", "telehealth_visit", "lab_panel"],
     "access": ["wheelchair", "sensory_friendly", "telehealth", "asl_interpreter", "step_free_entry"],
     "weekly": {"Tue": ["09:00", "11:00"], "Thu": ["09:00", "13:30"], "Fri": ["13:00"]}},
    {"id": "mock-cardio-partners", "name": "Springfield Heart Specialists", "kind": "Specialist", "specialty": "Cardiology",
     "address": "77 Example Ct, Springfield (synthetic)", "lat": 39.7800, "lon": -89.6700,
     "networks": ["Evergreen Health Plan"], "price_factor": "1.25",
     "services": ["specialist_visit", "lab_panel"],
     "access": ["wheelchair", "accessible_parking", "step_free_entry", "hearing_loop"],
     "weekly": {"Wed": ["08:00", "10:00", "14:00"], "Fri": ["08:00", "10:00"]}},
    {"id": "mock-clearview-derm", "name": "Clearview Dermatology", "kind": "Specialist", "specialty": "Dermatology",
     "address": "15 Sample Plaza, Springfield (synthetic)", "lat": 39.8300, "lon": -89.5800,
     "networks": ["Pinecrest Supplemental", "Harborview Mutual"], "price_factor": "1.05",
     "services": ["specialist_visit", "telehealth_visit"],
     "access": ["wheelchair", "telehealth", "step_free_entry"],
     "weekly": {"Mon": ["09:30"], "Wed": ["09:30", "15:00"], "Thu": ["11:00"]}},
    {"id": "mock-telecare-now", "name": "TeleCare Now (video only)", "kind": "Telehealth", "specialty": "Family medicine",
     "address": "Online (synthetic)", "lat": None, "lon": None,
     "networks": ["Evergreen Health Plan", "Harborview Mutual", "Pinecrest Supplemental"], "price_factor": "0.65",
     "services": ["telehealth_visit"],
     "access": ["telehealth", "asl_interpreter", "sensory_friendly", "step_free_entry"],
     "weekly": {d: ["08:00", "12:00", "17:30"] for d in ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")}},
    {"id": "mock-westside-lab", "name": "Westside Community Lab", "kind": "Lab", "specialty": "Laboratory",
     "address": "3 Test Tube Way, Springfield (synthetic)", "lat": 39.7700, "lon": -89.7400,
     "networks": [], "price_factor": "0.55",
     "services": ["lab_panel"],
     "access": ["wheelchair", "accessible_parking", "step_free_entry"],
     "weekly": {"Mon": ["07:00", "08:00", "09:00"], "Tue": ["07:00", "08:00"], "Wed": ["07:00", "08:00", "09:00"], "Fri": ["07:00"]}},
]
_BY_ID = {p["id"]: p for p in MOCK_PROVIDERS}
_DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
Q2 = Decimal("0.01")


def directory_meta() -> dict:
    return {
        "label": LABEL, "mock": True,
        "areas": [{"key": k, "label": v["label"]} for k, v in AREAS.items()], "default_area": DEFAULT_AREA,
        "services": [{"key": k, "label": v["label"]} for k, v in SERVICES.items()],
        "accessibility": [{"key": k, "label": v} for k, v in ACCESSIBILITY.items()],
        "specialties": sorted({p["specialty"] for p in MOCK_PROVIDERS}),
        "sorts": ["distance", "cost", "soonest"],
    }


def miles_between(lat1, lon1, lat2, lon2) -> float | None:
    if None in (lat1, lon1, lat2, lon2):
        return None
    r = 3958.8
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return round(2 * r * math.asin(math.sqrt(a)), 1)


def _booked(provider_id: str, day: date, hhmm: str) -> bool:
    """Deterministic 'already taken' flag so the mock looks realistic but never changes between runs."""
    h = hashlib.sha256(f"{provider_id}|{day.isoformat()}|{hhmm}".encode()).digest()[0]
    return h % 3 == 0


def availability(provider: dict, start: date, end: date, limit: int = 6) -> list[dict]:
    out: list[dict] = []
    d = start
    while d <= end and len(out) < limit:
        for hhmm in provider["weekly"].get(_DAYS[d.weekday()], []):
            if not _booked(provider["id"], d, hhmm):
                out.append({"start": f"{d.isoformat()}T{hhmm}", "label": f"{_DAYS[d.weekday()]} {d.strftime('%b')} {d.day}, {hhmm}",
                            "mock": True})
                if len(out) >= limit:
                    break
        d += timedelta(days=1)
    return out


def _q(d: Decimal) -> Decimal:
    return d.quantize(Q2, rounding=ROUND_HALF_UP)


def estimate_cost(provider: dict, service: str, plan: Any = None, *, deductible_applied: Decimal | None = None) -> dict:
    """Estimate ranges (Decimal) with explicit assumptions + uncertainty.  ``plan``: object/dict with insurer_name,
    deductible_annual, copay_amount, coinsurance_pct (all optional)."""
    svc = SERVICES[service]
    factor = Decimal(provider["price_factor"])
    full_lo, full_hi = _q(Decimal(svc["low"]) * factor), _q(Decimal(svc["high"]) * factor)
    get = (lambda k: plan.get(k) if isinstance(plan, dict) else getattr(plan, k, None)) if plan is not None else (lambda k: None)

    assumptions = [f"Typical full price range for '{svc['label']}' is a made-up demo range.",
                   "Your final bill depends on what is actually done."]
    network = "unknown"
    if plan is not None:
        insurer = (get("insurer_name") or "").strip().lower()
        network = "in_network" if insurer and insurer in [n.lower() for n in provider["networks"]] else "out_of_network"
    result: dict[str, Any] = {"service": service, "service_label": svc["label"], "network": network, "mock": True,
                              "label": LABEL}
    if plan is None:
        result.update(basis="self_pay", total_low=str(full_lo), total_high=str(full_hi),
                      you_pay_low=str(full_lo), you_pay_high=str(full_hi), uncertainty_pct=40, confidence="low")
        assumptions.append("No insurance plan selected, so this is the full price range you might be asked to pay.")
    elif network == "in_network":
        allowed_lo, allowed_hi = _q(full_lo * Decimal("0.55")), _q(full_hi * Decimal("0.75"))
        copay = D(get("copay_amount")) or Decimal("30.00")
        coins = Decimal(str(get("coinsurance_pct") if get("coinsurance_pct") is not None else 20)) / 100
        ded = D(get("deductible_annual")) or Decimal("0.00")
        remaining = max(ded - (deductible_applied or Decimal("0.00")), Decimal("0.00"))

        def share(allowed: Decimal, rem: Decimal) -> Decimal:
            if svc["visit"] and rem == 0:
                return min(allowed, copay)
            ded_part = min(allowed, rem)
            return _q(ded_part + (allowed - ded_part) * coins)

        you_lo, you_hi = share(allowed_lo, Decimal("0.00")), share(allowed_hi, remaining)
        if you_lo > you_hi:
            you_lo, you_hi = you_hi, you_lo
        result.update(basis="in_network_plan", total_low=str(allowed_lo), total_high=str(allowed_hi),
                      you_pay_low=str(_q(you_lo)), you_pay_high=str(_q(you_hi)), uncertainty_pct=25,
                      confidence="medium", allowed_amount_note="Insurer-negotiated price (the 'allowed amount'), estimated.",
                      deductible_remaining_assumed=str(_q(remaining)))
        assumptions += [f"Low end assumes your deductible is already met; high end assumes ${remaining:,.2f} of it is left.",
                        f"Uses a copay of ${copay:,.2f} for visits and {coins * 100:.0f}% coinsurance otherwise "
                        "(from your plan, or demo defaults if blank).",
                        "In-network price is assumed to be about 55%-75% of the full price."]
    else:
        result.update(basis="out_of_network_plan", total_low=str(full_lo), total_high=str(_q(full_hi * Decimal("1.25"))),
                      you_pay_low=str(_q(full_lo * Decimal("0.5"))), you_pay_high=str(_q(full_hi * Decimal("1.25"))),
                      uncertainty_pct=50, confidence="low")
        assumptions += ["This provider is not in your plan's network in our mock list. Out-of-network costs can be much higher "
                        "and you may be billed the difference.",
                        "Ask your insurer to confirm the network before booking."]
    result["assumptions"] = assumptions
    return result


def _distance(p: dict, lat: float, lon: float) -> float | None:
    return miles_between(lat, lon, p["lat"], p["lon"])


def resolve_origin(area: str | None, lat: float | None, lon: float | None) -> tuple[float, float, str]:
    if lat is not None and lon is not None:
        return lat, lon, "Your location (as typed)"
    a = AREAS.get(area or DEFAULT_AREA) or AREAS[DEFAULT_AREA]
    return a["lat"], a["lon"], a["label"]


def search_providers(*, service: str | None = None, specialty: str | None = None, q: str | None = None,
                     area: str | None = None, lat: float | None = None, lon: float | None = None,
                     max_distance_miles: float | None = None, accessibility: list[str] | None = None,
                     plan: Any = None, in_network_only: bool = False, date_from: date | None = None,
                     date_to: date | None = None, sort: str = "distance", deductible_applied: Decimal | None = None,
                     today: date | None = None) -> dict:
    today = today or utcnow().date()
    start = date_from or today
    end = date_to or (start + timedelta(days=14))
    if end < start:
        start, end = end, start
    end = min(end, start + timedelta(days=60))
    olat, olon, olabel = resolve_origin(area, lat, lon)
    needs = [a for a in (accessibility or []) if a in ACCESSIBILITY]
    if service and service not in SERVICES:
        raise ValueError("Unknown service.")
    results = []
    for p in MOCK_PROVIDERS:
        if service and service not in p["services"]:
            continue
        if specialty and specialty.lower() not in p["specialty"].lower():
            continue
        if q and q.lower() not in (p["name"] + " " + p["specialty"] + " " + p["kind"]).lower():
            continue
        if any(n not in p["access"] for n in needs):
            continue
        dist = _distance(p, olat, olon)
        if max_distance_miles is not None and dist is not None and dist > max_distance_miles:
            continue
        if max_distance_miles is not None and dist is None and not (service in (None, "telehealth_visit") or "telehealth" in p["access"]):
            continue
        slots = availability(p, start, end)
        if not slots:
            continue
        est = estimate_cost(p, service, plan, deductible_applied=deductible_applied) if service else None
        if in_network_only and plan is not None and service and est and est["network"] != "in_network":
            continue
        net = None
        if plan is not None:
            ins = (plan.get("insurer_name") if isinstance(plan, dict) else getattr(plan, "insurer_name", "")) or ""
            net = "in_network" if ins.strip().lower() in [n.lower() for n in p["networks"]] else "out_of_network"
        results.append({
            "id": p["id"], "name": p["name"], "kind": p["kind"], "specialty": p["specialty"], "address": p["address"],
            "distance_miles": dist, "network": net,
            "accessibility": [{"key": a, "label": ACCESSIBILITY[a]} for a in p["access"]],
            "services": [{"key": s, "label": SERVICES[s]["label"]} for s in p["services"]],
            "next_openings": slots, "availability_is_mock": True, "estimate": est,
        })
    key = {"distance": lambda r: (r["distance_miles"] is None, r["distance_miles"] or 0, r["name"]),
           "cost": lambda r: (r["estimate"] is None, Decimal(r["estimate"]["you_pay_low"]) if r["estimate"] else Decimal(0), r["name"]),
           "soonest": lambda r: (r["next_openings"][0]["start"], r["name"])}.get(sort)
    if key is None:
        raise ValueError("Unknown sort.")
    results.sort(key=key)
    return {"label": LABEL, "mock": True, "origin": olabel, "window": {"from": start.isoformat(), "to": end.isoformat()},
            "count": len(results), "results": results,
            "uncertainty_note": "Prices are ranges, not quotes. The 'uncertainty' percent is how far real costs could reasonably differ."}


def get_provider(provider_id: str, *, service: str | None = None, plan: Any = None, area: str | None = None,
                 date_from: date | None = None, date_to: date | None = None, deductible_applied: Decimal | None = None) -> dict | None:
    p = _BY_ID.get(provider_id)
    if p is None:
        return None
    start = date_from or utcnow().date()
    end = min(date_to or start + timedelta(days=21), start + timedelta(days=60))
    olat, olon, olabel = resolve_origin(area, None, None)
    return {
        "id": p["id"], "name": p["name"], "kind": p["kind"], "specialty": p["specialty"], "address": p["address"],
        "distance_miles": _distance(p, olat, olon), "origin": olabel, "networks": p["networks"],
        "accessibility": [{"key": a, "label": ACCESSIBILITY[a]} for a in p["access"]],
        "openings": availability(p, start, end, limit=15), "availability_is_mock": True,
        "estimates": [estimate_cost(p, s, plan, deductible_applied=deductible_applied) for s in p["services"]
                      if not service or s == service],
        "label": LABEL, "mock": True,
    }
