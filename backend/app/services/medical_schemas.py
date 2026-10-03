"""Request schemas for the medical-record API (W7).  Patients can never set ``source``/``confirmed_*`` (extra fields are
forbidden), every optional field accepts null/"" meaning *Unknown*, and only the few fields that make a record
meaningful are required."""
from __future__ import annotations

import re
from datetime import date, timedelta
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.models.medical import norm_key  # noqa: F401  (re-exported for the services)
from app.models.shared import utcnow

_PARTIAL = re.compile(r"^(\d{4})(?:-(\d{2})(?:-(\d{2}))?)?$")


def parse_partial_date(s: str | None) -> str | None:
    """Validate ``YYYY`` | ``YYYY-MM`` | ``YYYY-MM-DD``; return it unchanged. None/"" -> None (Unknown)."""
    if s is None or s == "":
        return None
    m = _PARTIAL.match(s)
    if not m:
        raise ValueError("Use a year (2019), a month (2019-05) or a full date (2019-05-17), or leave it blank if unknown.")
    y = int(m.group(1))
    if not 1900 <= y <= 2100:
        raise ValueError("Please enter a year between 1900 and 2100.")
    try:
        if m.group(3):
            date(y, int(m.group(2)), int(m.group(3)))
        elif m.group(2):
            date(y, int(m.group(2)), 1)
    except ValueError:
        raise ValueError("That date doesn't exist. Check the month and day.") from None
    return s


def sort_date(s: str | None) -> str | None:
    """Partial date -> sortable ``YYYY-MM-DD`` (missing month/day sort first)."""
    if not s:
        return None
    return s + "-01-01"[len(s) - 4:]


def _pdate(*fields):
    @field_validator(*fields)
    @classmethod
    def _v(cls, x):
        return parse_partial_date(x)
    return _v


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    @model_validator(mode="before")
    @classmethod
    def _blank_to_none(cls, data):
        if isinstance(data, dict):
            return {k: (None if isinstance(v, str) and v.strip() == "" else v) for k, v in data.items()}
        return data


def _order_check(start: str | None, end: str | None) -> None:
    a, b = sort_date(start), sort_date(end)
    if a and b and b < a:
        raise ValueError("end_date|The end date can't be before the start date.")


class ProviderIn(_In):
    name: str = Field(min_length=1, max_length=200)
    kind: Literal["clinician", "facility", "pharmacy", "lab", "other"] = "clinician"
    specialty: str | None = Field(None, max_length=100)
    organization: str | None = Field(None, max_length=200)
    role_in_care: str | None = Field(None, max_length=100)
    phone: str | None = Field(None, max_length=50)
    address: str | None = Field(None, max_length=300)
    is_care_team: bool = True
    last_seen: date | None = None
    linked_provider_id: str | None = Field(None, max_length=36)
    notes: str | None = Field(None, max_length=2000)


class HistoryIn(_In):
    kind: Literal["condition", "surgery", "hospitalization", "family_history"]
    title: str = Field(min_length=1, max_length=200)
    status: Literal["active", "resolved", "unknown"] = "unknown"
    start_date: str | None = None
    end_date: str | None = None
    relation: str | None = Field(None, max_length=60)
    facility_name: str | None = Field(None, max_length=200)
    provider_id: str | None = Field(None, max_length=36)
    notes: str | None = Field(None, max_length=2000)

    _d = _pdate("start_date", "end_date")

    @model_validator(mode="after")
    def _check(self):
        _order_check(self.start_date, self.end_date)
        if self.kind != "family_history":
            self.relation = None
        return self


class MedicationIn(_In):
    name: str = Field(min_length=1, max_length=200)
    status: Literal["active", "past"] = "active"
    is_supplement: bool = False
    dose: str | None = Field(None, max_length=100)
    frequency: str | None = Field(None, max_length=100)
    route: str | None = Field(None, max_length=50)
    reason: str | None = Field(None, max_length=200)
    start_date: str | None = None
    end_date: str | None = None
    prescriber_name: str | None = Field(None, max_length=200)
    prescriber_id: str | None = Field(None, max_length=36)
    notes: str | None = Field(None, max_length=2000)

    _d = _pdate("start_date", "end_date")

    @model_validator(mode="after")
    def _check(self):
        _order_check(self.start_date, self.end_date)
        if self.status == "active" and self.end_date:
            raise ValueError("end_date|An active medication has no end date. Choose \"Past\" if you stopped taking it.")
        return self


class AllergyIn(_In):
    substance: str = Field(min_length=1, max_length=200)
    category: Literal["drug", "food", "environmental", "other", "unknown"] = "unknown"
    reaction: str | None = Field(None, max_length=300)
    severity: Literal["mild", "moderate", "severe", "unknown"] = "unknown"
    status: Literal["active", "inactive"] = "active"
    onset: str | None = None
    notes: str | None = Field(None, max_length=2000)

    _d = _pdate("onset")


class VaccinationIn(_In):
    kind: Literal["vaccine", "screening", "preventive_care"] = "vaccine"
    name: str = Field(min_length=1, max_length=200)
    status: Literal["completed", "scheduled", "declined", "unknown"] = "completed"
    date_given: str | None = None
    dose_number: int | None = Field(None, ge=1, le=20)
    administered_by: str | None = Field(None, max_length=200)
    provider_id: str | None = Field(None, max_length=36)
    next_due_date: date | None = None
    notes: str | None = Field(None, max_length=2000)

    _d = _pdate("date_given")


class ResultIn(_In):
    test_name: str = Field(min_length=1, max_length=200)
    category: Literal["lab", "imaging", "other"] = "lab"
    result_date: date | None = None
    value_num: Decimal | None = Field(None, ge=Decimal("-1000000000"), le=Decimal("1000000000"), decimal_places=4)
    value_text: str | None = Field(None, max_length=300)
    unit: str | None = Field(None, max_length=40)
    ref_low: Decimal | None = Field(None, ge=Decimal("-1000000000"), le=Decimal("1000000000"), decimal_places=4)
    ref_high: Decimal | None = Field(None, ge=Decimal("-1000000000"), le=Decimal("1000000000"), decimal_places=4)
    ref_text: str | None = Field(None, max_length=200)
    source_name: str | None = Field(None, max_length=200)
    provider_id: str | None = Field(None, max_length=36)
    document_id: str | None = Field(None, max_length=36)
    notes: str | None = Field(None, max_length=2000)

    @field_validator("result_date")
    @classmethod
    def _not_future(cls, v):
        if v and v > utcnow().date() + timedelta(days=1):
            raise ValueError("A result date can't be in the future.")
        return v

    @model_validator(mode="after")
    def _check(self):
        if self.value_num is None and not self.value_text:
            raise ValueError("value_num|Enter the result value (a number, or text such as \"Negative\").")
        if self.ref_low is not None and self.ref_high is not None and self.ref_low > self.ref_high:
            raise ValueError("ref_low|The lab's low reference value can't be higher than the high value.")
        return self


class CorrectionIn(_In):
    record_type: Literal["history", "medication", "allergy", "vaccination", "result", "provider"]
    record_id: str = Field(min_length=1, max_length=36)
    message: str = Field(min_length=1, max_length=2000)


class AllergyStatusIn(_In):
    status: Literal["unknown", "no_known_allergies"]


SCHEMAS = {
    "history": HistoryIn, "medications": MedicationIn, "allergies": AllergyIn,
    "vaccinations": VaccinationIn, "results": ResultIn, "providers": ProviderIn,
}
