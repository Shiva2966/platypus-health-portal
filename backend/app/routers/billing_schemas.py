"""Request models for the W8 billing routers (no `router` here, so it is not auto-mounted)."""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Annotated, Optional

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

from app.models.billing import (BILL_STATUSES, CLAIM_STATUSES, DISPUTE_REASONS, PAY_METHODS, PLAN_RANKS, PLAN_TYPES,
                                RELATIONSHIPS)

Money = Annotated[Decimal, Field(ge=0, le=Decimal("9999999.99"), max_digits=12, decimal_places=2)]
PositiveMoney = Annotated[Decimal, Field(gt=0, le=Decimal("9999999.99"), max_digits=12, decimal_places=2)]
Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
Short = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
Text300 = Annotated[str, StringConstraints(strip_whitespace=True, max_length=300)]
Text2000 = Annotated[str, StringConstraints(strip_whitespace=True, max_length=2000)]


def _choice(v: str, allowed: tuple, label: str) -> str:
    if v not in allowed:
        raise ValueError(f"{label} must be one of: {', '.join(allowed)}")
    return v


class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid")

    @field_validator("*", mode="before")
    @classmethod
    def _blank_to_none(cls, v):
        return None if isinstance(v, str) and v.strip() == "" else v


class PlanIn(_Base):
    insurer_name: Name
    plan_name: Optional[Name] = None
    plan_type: str = "other"
    member_id: Annotated[str, StringConstraints(strip_whitespace=True, min_length=2, max_length=60)]
    group_number: Optional[Annotated[str, StringConstraints(strip_whitespace=True, max_length=60)]] = None
    policyholder_name: Name
    policyholder_relationship: str = "self"
    policyholder_dob: Optional[date] = None
    coverage_rank: str = "primary"
    effective_date: date
    end_date: Optional[date] = None
    deductible_annual: Optional[Money] = None
    out_of_pocket_max: Optional[Money] = None
    copay_amount: Optional[Money] = None
    coinsurance_pct: Optional[Annotated[Decimal, Field(ge=0, le=100, max_digits=5, decimal_places=2)]] = None
    insurer_phone: Optional[Annotated[str, StringConstraints(strip_whitespace=True, max_length=50)]] = None

    @field_validator("plan_type")
    @classmethod
    def _t(cls, v):
        return _choice(v or "other", PLAN_TYPES, "Plan type")

    @field_validator("coverage_rank")
    @classmethod
    def _r(cls, v):
        return _choice(v or "primary", PLAN_RANKS, "Coverage order")

    @field_validator("policyholder_relationship")
    @classmethod
    def _rel(cls, v):
        return _choice(v or "self", RELATIONSHIPS, "Relationship")


class PlanUpdate(PlanIn):
    """PUT = partial update: every field optional."""
    insurer_name: Optional[Name] = None  # type: ignore[assignment]
    member_id: Optional[Annotated[str, StringConstraints(strip_whitespace=True, min_length=2, max_length=60)]] = None  # type: ignore[assignment]
    policyholder_name: Optional[Name] = None  # type: ignore[assignment]
    effective_date: Optional[date] = None  # type: ignore[assignment]
    plan_type: Optional[str] = None  # type: ignore[assignment]
    coverage_rank: Optional[str] = None  # type: ignore[assignment]
    policyholder_relationship: Optional[str] = None  # type: ignore[assignment]


class CardIn(_Base):
    side: str
    document_id: Optional[str] = None


class ClaimIn(_Base):
    claim_number: Short
    insurer_name: Name
    provider_name: Name
    service_date: date
    service_description: Optional[Text300] = None
    plan_id: Optional[str] = None
    billed_amount: Money
    allowed_amount: Optional[Money] = None
    insurer_paid: Optional[Money] = None
    deductible: Optional[Money] = None
    copay: Optional[Money] = None
    coinsurance: Optional[Money] = None
    patient_responsibility: Optional[Money] = None
    status: str = "processing"
    denial_reason: Optional[Text300] = None
    eob_document_id: Optional[str] = None
    notes: Optional[Text2000] = None

    @field_validator("status")
    @classmethod
    def _s(cls, v):
        return _choice(v or "processing", CLAIM_STATUSES, "Status")


class ClaimUpdate(ClaimIn):
    claim_number: Optional[Short] = None  # type: ignore[assignment]
    insurer_name: Optional[Name] = None  # type: ignore[assignment]
    provider_name: Optional[Name] = None  # type: ignore[assignment]
    service_date: Optional[date] = None  # type: ignore[assignment]
    billed_amount: Optional[Money] = None  # type: ignore[assignment]
    status: Optional[str] = None  # type: ignore[assignment]


class BillIn(_Base):
    provider_name: Name
    account_number: Optional[Annotated[str, StringConstraints(strip_whitespace=True, max_length=80)]] = None
    service_date: date
    statement_date: Optional[date] = None
    description: Optional[Text300] = None
    billed_amount: Money
    amount_due: Money
    due_date: Optional[date] = None
    plan_id: Optional[str] = None
    notes: Optional[Text2000] = None


class BillUpdate(BillIn):
    provider_name: Optional[Name] = None  # type: ignore[assignment]
    service_date: Optional[date] = None  # type: ignore[assignment]
    billed_amount: Optional[Money] = None  # type: ignore[assignment]
    amount_due: Optional[Money] = None  # type: ignore[assignment]
    status: Optional[str] = None

    @field_validator("status")
    @classmethod
    def _s(cls, v):
        if v is not None and v not in ("void", "unpaid"):
            raise ValueError("You can only mark a bill void or restore it to unpaid. Payments and disputes set the other statuses.")
        return v


class PaymentIn(_Base):
    amount: PositiveMoney
    paid_on: date
    method: str = "card"
    confirmation_number: Optional[Annotated[str, StringConstraints(strip_whitespace=True, max_length=80)]] = None
    note: Optional[Text300] = None
    receipt_document_id: Optional[str] = None
    separate_payment: bool = False

    @field_validator("method")
    @classmethod
    def _m(cls, v):
        return _choice(v or "card", PAY_METHODS, "Payment method")


class MatchIn(_Base):
    claim_id: str


class DisputeIn(_Base):
    reason_code: str
    message: Annotated[str, StringConstraints(strip_whitespace=True, min_length=5, max_length=2000)]
    disputed_amount: Optional[Money] = None

    @field_validator("reason_code")
    @classmethod
    def _r(cls, v):
        return _choice(v, DISPUTE_REASONS, "Reason")


class DisputeClose(_Base):
    outcome: str = "resolved"
    note: Optional[Annotated[str, StringConstraints(strip_whitespace=True, max_length=500)]] = None
