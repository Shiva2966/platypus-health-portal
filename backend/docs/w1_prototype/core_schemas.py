"""Field schemas + validation for patient-owned record categories. One source of truth: the UI builds forms
from GET /api/schema, the API validates with `validate()`."""
import re
from datetime import date

from app.errors import FieldError

F = lambda name, label, type="text", required=False, **kw: {"name": name, "label": label, "type": type, "required": required, **kw}

ROUTES = ["oral", "injection", "topical", "inhaled", "eye/ear drops", "other"]
RELATIVES = ["mother", "father", "sister", "brother", "daughter", "son", "grandparent", "aunt/uncle", "other"]

SCHEMAS: dict[str, dict] = {
    "emergency_contact": {
        "label": "Emergency contact", "group": "contacts", "title": "name",
        "fields": [F("name", "Full name", required=True), F("relationship", "Relationship", required=True, placeholder="e.g. sister"),
                   F("phone", "Phone", "tel", True), F("email", "Email", "email"),
                   F("is_primary", "This is my primary emergency contact", "checkbox"),
                   F("can_access_records", "Also authorized to see my records", "checkbox",
                     help="Separate from being an emergency contact. Ticking this only records your wish; to actually give access use Sharing.")],
        "dedupe": ["name", "phone"],
    },
    "condition": {"label": "Condition", "group": "history", "title": "name",
                  "fields": [F("name", "Condition", required=True), F("approx_date", "Approximate date diagnosed", "partial_date", help="A year is fine. Leave blank if unknown."),
                             F("status", "Status", "select", options=["active", "resolved", "unknown"], default="unknown"), F("notes", "Notes", "textarea")]},
    "surgery": {"label": "Surgery or procedure", "group": "history", "title": "name",
                "fields": [F("name", "Surgery or procedure", required=True), F("approx_date", "Approximate date", "partial_date"),
                           F("facility", "Hospital or clinic"), F("notes", "Notes", "textarea")]},
    "hospitalization": {"label": "Hospital stay", "group": "history", "title": "reason",
                        "fields": [F("reason", "Reason for stay", required=True), F("approx_date", "Approximate date", "partial_date"),
                                   F("facility", "Hospital"), F("duration_days", "Length of stay (days)", "number"), F("notes", "Notes", "textarea")]},
    "family_history": {"label": "Family health history", "group": "history", "title": "condition",
                       "fields": [F("relative", "Family member", "select", True, options=RELATIVES), F("condition", "Condition", required=True),
                                  F("age_at_onset", "Age when it started", "number", help="Leave blank if unknown."), F("notes", "Notes", "textarea")]},
    "medication": {"label": "Medication", "group": "medications", "title": "name",
                   "fields": [F("name", "Medication name", required=True), F("strength", "Strength", placeholder="e.g. 10 mg"),
                              F("dose", "Dose", placeholder="e.g. 1 tablet"), F("route", "How taken", "select", options=ROUTES),
                              F("frequency", "How often", placeholder="e.g. once a day"), F("start_date", "Start date", "date"),
                              F("end_date", "End date", "date", help="Leave blank if you still take it."), F("prescriber", "Prescribed by")],
                   "dedupe": ["name", "strength", "start_date"]},
    "allergy": {"label": "Allergy", "group": "allergies", "title": "substance",
                "fields": [F("substance", "Allergic to", required=True), F("reaction", "Reaction", placeholder="e.g. rash, trouble breathing"),
                           F("severity", "Severity", "select", options=["mild", "moderate", "severe", "unknown"], default="unknown"), F("notes", "Notes", "textarea")],
                "dedupe": ["substance"]},
    "result": {"label": "Test result", "group": "results", "title": "test_name",
               "fields": [F("test_name", "Test name", required=True, placeholder="e.g. HbA1c"), F("result_date", "Date of test", "date", True),
                          F("value", "Result value", "number", True), F("unit", "Unit", required=True, placeholder="e.g. %"),
                          F("ref_low", "Lab normal range: low", "number"), F("ref_high", "Lab normal range: high", "number"),
                          F("lab_name", "Lab or clinic"), F("notes", "Notes", "textarea")],
               "dedupe": ["test_name", "result_date", "value"]},
    "insurance": {"label": "Insurance plan", "group": "insurance", "title": "plan_name",
                  "fields": [F("plan_name", "Plan name", required=True), F("insurer", "Insurance company", required=True),
                             F("member_id", "Member ID", required=True), F("group_number", "Group number"),
                             F("rank", "Coverage order", "select", True, options=["primary", "secondary", "other"]),
                             F("effective_date", "Effective from", "date", True), F("end_date", "Ends", "date")],
                  "dedupe": ["insurer", "member_id"]},
    "bill": {"label": "Provider bill", "group": "bills", "title": "provider_name",
             "fields": [F("provider_name", "Billed by (provider)", required=True), F("service_date", "Date of service", "date", True),
                        F("description", "What it was for"), F("billed_amount", "Total billed ($)", "money", True),
                        F("amount_due", "Amount they ask you to pay ($)", "money", True), F("payments_made", "Already paid ($)", "money", default=0),
                        F("due_date", "Due date", "date"), F("status", "Status", "select", options=["unpaid", "partially_paid", "paid", "in_dispute"], default="unpaid"),
                        F("receipt_note", "Receipt / confirmation number", help="Optional. Keep the receipt in Documents.")],
             "dedupe": ["provider_name", "service_date", "billed_amount"]},
    "eob": {"label": "Insurer explanation (EOB)", "group": "bills", "title": "claim_number",
            "fields": [F("insurer", "Insurance company", required=True), F("claim_number", "Claim number", required=True),
                       F("provider_name", "Provider", required=True), F("service_date", "Date of service", "date", True),
                       F("billed_amount", "Amount billed ($)", "money", True), F("allowed_amount", "Allowed amount ($)", "money", help="From your EOB. Leave blank if not shown."),
                       F("insurer_paid", "Insurer paid ($)", "money"), F("deductible", "Applied to deductible ($)", "money"),
                       F("copay", "Copay ($)", "money"), F("coinsurance", "Coinsurance ($)", "money"),
                       F("patient_responsibility", "You owe, per EOB ($)", "money"),
                       F("claim_status", "Claim status", "select", options=["processing", "approved", "partially_denied", "denied", "appealed"], default="processing"),
                       F("notes", "Notes", "textarea")],
            "dedupe": ["claim_number"]},
}

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
PARTIAL_RE = re.compile(r"^\d{4}(-\d{2}(-\d{2})?)?$")


def _is_date(s: str) -> bool:
    if not DATE_RE.match(s):
        return False
    try:
        y = date.fromisoformat(s).year
        return 1900 <= y <= 2100
    except ValueError:
        return False


def clean_value(f: dict, raw):
    """Return cleaned value or raise ValueError(message)."""
    t = f["type"]
    if t == "checkbox":
        return bool(raw)
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        if f.get("required"):
            raise ValueError("This field is required.")
        return None
    if t in ("number", "money"):
        try:
            if isinstance(raw, bool):
                raise ValueError
            v = float(raw)
        except (TypeError, ValueError):
            raise ValueError("Enter a number.")
        if v != v or abs(v) > 1e9:
            raise ValueError("Enter a reasonable number.")
        if t == "money" and v < 0:
            raise ValueError("Amount can't be negative.")
        return round(v, 2) if t == "money" else v
    if not isinstance(raw, str):
        raise ValueError("Enter text.")
    s = raw.strip()
    if t == "date":
        if not _is_date(s):
            raise ValueError("Use a date like 2024-03-15.")
        return s
    if t == "partial_date":
        if not PARTIAL_RE.match(s) or not (1900 <= int(s[:4]) <= 2100):
            raise ValueError("Use a year (2019), year-month (2019-05) or full date (2019-05-17).")
        return s
    if t == "select":
        if s not in f["options"]:
            raise ValueError("Choose one of the options.")
        return s
    limit = 2000 if t == "textarea" else 300
    if len(s) > limit:
        raise ValueError(f"Too long (max {limit} characters).")
    if t == "email" and not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", s):
        raise ValueError("Enter a valid email address.")
    return s


def validate(category: str, payload: dict) -> dict:
    spec = SCHEMAS[category]
    out, errors = {}, {}
    for f in spec["fields"]:
        raw = payload.get(f["name"], f.get("default") if f["type"] != "checkbox" else False)
        try:
            out[f["name"]] = clean_value(f, raw)
        except ValueError as e:
            errors[f["name"]] = str(e)
    # cross-field checks
    def both(a, b):
        return a in out and b in out and out[a] is not None and out[b] is not None and a not in errors and b not in errors
    if both("start_date", "end_date") and out["end_date"] < out["start_date"]:
        errors["end_date"] = "End date can't be before the start date."
    if both("effective_date", "end_date") and out["end_date"] < out["effective_date"]:
        errors["end_date"] = "End date can't be before the start date."
    if both("ref_low", "ref_high") and out["ref_low"] > out["ref_high"]:
        errors["ref_high"] = "The high end must be at least the low end."
    if category == "bill" and not errors:
        if out["amount_due"] > out["billed_amount"] + 0.005:
            pass  # allowed but flagged by the explanation; real bills can include late fees
    if errors:
        raise FieldError(errors)
    return out


def norm(v):
    if isinstance(v, str):
        return v.strip().lower()
    if isinstance(v, float):
        return round(v, 2)
    return v


def dedupe_key(category: str, data: dict):
    keys = SCHEMAS[category].get("dedupe")
    return tuple(norm(data.get(k)) for k in keys) if keys else None


def is_active_medication(d: dict) -> bool:
    end = d.get("end_date")
    return not end or end >= date.today().isoformat()
