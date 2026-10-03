"""W8 insurance + billing tables. PRIVATE to the patient by default (never part of document share scopes).

Portable SQLAlchemy 2.x: UUID string ids, Numeric(12,2) money (never float), UTC timestamps, real FK ondelete,
CHECK/UNIQUE constraints and an index on every FK + searched column.

Table names are prefixed so they never collide with W1's legacy `bills` / `eobs` tables:
insurance_plans, billing_claims, billing_bills, billing_payments, billing_disputes.
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (CheckConstraint, Date, ForeignKey, Index, Integer, Numeric, String, Text,
                        UniqueConstraint, text)
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.db_ops.types import UTCDateTime
from app.models.shared import new_id, utcnow

Money = lambda: Numeric(12, 2)  # noqa: E731  - money is NEVER float
_q = lambda vals: ",".join(f"'{v}'" for v in vals)  # noqa: E731

PLAN_RANKS = ("primary", "secondary", "tertiary")
PLAN_TYPES = ("hmo", "ppo", "epo", "pos", "hdhp", "medicare", "medicaid", "dental", "vision", "other")
VERIFICATION = ("entered", "verified", "failed")
RELATIONSHIPS = ("self", "spouse", "parent", "child", "other")
CLAIM_STATUSES = ("submitted", "processing", "paid", "partially_denied", "denied", "appealed")
BILL_STATUSES = ("unpaid", "partially_paid", "paid", "in_dispute", "void")
PAY_METHODS = ("card", "check", "cash", "bank_transfer", "hsa_fsa", "other")
DISPUTE_STATUSES = ("open", "resolved", "withdrawn")
DISPUTE_REASONS = ("amount_wrong", "duplicate_charge", "insurance_not_applied", "service_not_received",
                   "claim_denied", "other")


class InsurancePlan(Base):
    __tablename__ = "insurance_plans"
    __table_args__ = (
        UniqueConstraint("patient_id", "dedupe_key", name="uq_insurance_plan_dedupe"),
        CheckConstraint(f"coverage_rank IN ({_q(PLAN_RANKS)})", name="ck_insurance_plan_rank"),
        CheckConstraint(f"plan_type IN ({_q(PLAN_TYPES)})", name="ck_insurance_plan_type"),
        CheckConstraint(f"verification_status IN ({_q(VERIFICATION)})", name="ck_insurance_plan_verification"),
        CheckConstraint(f"policyholder_relationship IN ({_q(RELATIONSHIPS)})", name="ck_insurance_plan_relationship"),
        CheckConstraint("end_date IS NULL OR end_date >= effective_date", name="ck_insurance_plan_dates"),
        CheckConstraint("deductible_annual IS NULL OR deductible_annual >= 0", name="ck_insurance_plan_deductible"),
        CheckConstraint("out_of_pocket_max IS NULL OR out_of_pocket_max >= 0", name="ck_insurance_plan_oop"),
        CheckConstraint("copay_amount IS NULL OR copay_amount >= 0", name="ck_insurance_plan_copay"),
        CheckConstraint("coinsurance_pct IS NULL OR (coinsurance_pct >= 0 AND coinsurance_pct <= 100)",
                        name="ck_insurance_plan_coinsurance"),
        Index("ix_insurance_plans_patient_rank", "patient_id", "coverage_rank"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    patient_id: Mapped[str] = mapped_column(ForeignKey("patients.id", ondelete="CASCADE"), index=True)
    insurer_name: Mapped[str] = mapped_column(String(200), index=True)
    plan_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    plan_type: Mapped[str] = mapped_column(String(20), default="other")
    member_id: Mapped[str] = mapped_column(String(60))
    group_number: Mapped[str | None] = mapped_column(String(60), nullable=True)
    dedupe_key: Mapped[str] = mapped_column(String(300))  # lower(insurer)|UPPER(member id)
    policyholder_name: Mapped[str] = mapped_column(String(200))
    policyholder_relationship: Mapped[str] = mapped_column(String(20), default="self")
    policyholder_dob: Mapped[date | None] = mapped_column(Date, nullable=True)
    coverage_rank: Mapped[str] = mapped_column(String(20), default="primary")
    effective_date: Mapped[date] = mapped_column(Date, index=True)
    end_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    deductible_annual: Mapped[Decimal | None] = mapped_column(Money(), nullable=True)
    out_of_pocket_max: Mapped[Decimal | None] = mapped_column(Money(), nullable=True)
    copay_amount: Mapped[Decimal | None] = mapped_column(Money(), nullable=True)
    coinsurance_pct: Mapped[Decimal | None] = mapped_column(Numeric(5, 2), nullable=True)  # plan pays 100-x
    insurer_phone: Mapped[str | None] = mapped_column(String(50), nullable=True)
    # entered = patient typed it; verified = mock eligibility check passed; failed = mock check failed
    verification_status: Mapped[str] = mapped_column(String(20), default="entered", index=True)
    verified_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    verification_note: Mapped[str | None] = mapped_column(String(300), nullable=True)
    card_front_document_id: Mapped[str | None] = mapped_column(
        ForeignKey("documents.id", ondelete="SET NULL"), nullable=True, index=True)
    card_back_document_id: Mapped[str | None] = mapped_column(
        ForeignKey("documents.id", ondelete="SET NULL"), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)


class BillingClaim(Base):
    """A claim / explanation of benefits (EOB) as entered from the insurer's paperwork."""

    __tablename__ = "billing_claims"
    __table_args__ = (
        UniqueConstraint("patient_id", "claim_key", name="uq_billing_claim_dedupe"),
        CheckConstraint(f"status IN ({_q(CLAIM_STATUSES)})", name="ck_billing_claim_status"),
        CheckConstraint("billed_amount >= 0", name="ck_billing_claim_billed"),
        CheckConstraint("allowed_amount IS NULL OR allowed_amount >= 0", name="ck_billing_claim_allowed"),
        CheckConstraint("insurer_paid IS NULL OR insurer_paid >= 0", name="ck_billing_claim_paid"),
        CheckConstraint("deductible IS NULL OR deductible >= 0", name="ck_billing_claim_deductible"),
        CheckConstraint("copay IS NULL OR copay >= 0", name="ck_billing_claim_copay"),
        CheckConstraint("coinsurance IS NULL OR coinsurance >= 0", name="ck_billing_claim_coinsurance"),
        CheckConstraint("patient_responsibility IS NULL OR patient_responsibility >= 0", name="ck_billing_claim_resp"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    patient_id: Mapped[str] = mapped_column(ForeignKey("patients.id", ondelete="CASCADE"), index=True)
    plan_id: Mapped[str | None] = mapped_column(ForeignKey("insurance_plans.id", ondelete="SET NULL"),
                                                nullable=True, index=True)
    claim_number: Mapped[str] = mapped_column(String(100), index=True)
    claim_key: Mapped[str] = mapped_column(String(100))  # UPPER(trimmed claim number)
    insurer_name: Mapped[str] = mapped_column(String(200))
    provider_name: Mapped[str] = mapped_column(String(200), index=True)
    service_date: Mapped[date] = mapped_column(Date, index=True)
    service_description: Mapped[str | None] = mapped_column(String(300), nullable=True)
    billed_amount: Mapped[Decimal] = mapped_column(Money())
    allowed_amount: Mapped[Decimal | None] = mapped_column(Money(), nullable=True)
    insurer_paid: Mapped[Decimal | None] = mapped_column(Money(), nullable=True)
    deductible: Mapped[Decimal | None] = mapped_column(Money(), nullable=True)
    copay: Mapped[Decimal | None] = mapped_column(Money(), nullable=True)
    coinsurance: Mapped[Decimal | None] = mapped_column(Money(), nullable=True)
    patient_responsibility: Mapped[Decimal | None] = mapped_column(Money(), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="processing", index=True)
    denial_reason: Mapped[str | None] = mapped_column(String(300), nullable=True)
    eob_document_id: Mapped[str | None] = mapped_column(ForeignKey("documents.id", ondelete="SET NULL"),
                                                        nullable=True, index=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)


class BillingBill(Base):
    __tablename__ = "billing_bills"
    __table_args__ = (
        UniqueConstraint("patient_id", "dedupe_key", name="uq_billing_bill_dedupe"),
        UniqueConstraint("claim_id", name="uq_billing_bill_claim"),  # a claim is matched to at most one bill
        CheckConstraint(f"status IN ({_q(BILL_STATUSES)})", name="ck_billing_bill_status"),
        CheckConstraint("match_status IN ('unmatched','matched')", name="ck_billing_bill_match"),
        CheckConstraint("billed_amount >= 0 AND amount_due >= 0", name="ck_billing_bill_amounts"),
        Index("ix_billing_bills_patient_status", "patient_id", "status"),
        Index("ix_billing_bills_patient_due", "patient_id", "due_date"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    patient_id: Mapped[str] = mapped_column(ForeignKey("patients.id", ondelete="CASCADE"), index=True)
    provider_name: Mapped[str] = mapped_column(String(200), index=True)
    account_number: Mapped[str | None] = mapped_column(String(80), nullable=True)
    dedupe_key: Mapped[str] = mapped_column(String(400))  # provider|service date|billed|account
    service_date: Mapped[date] = mapped_column(Date, index=True)
    statement_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    description: Mapped[str | None] = mapped_column(String(300), nullable=True)
    billed_amount: Mapped[Decimal] = mapped_column(Money())  # full charge shown on the bill
    amount_due: Mapped[Decimal] = mapped_column(Money())  # what the provider asks the patient to pay
    due_date: Mapped[date | None] = mapped_column(Date, nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(20), default="unpaid", index=True)
    plan_id: Mapped[str | None] = mapped_column(ForeignKey("insurance_plans.id", ondelete="SET NULL"),
                                                nullable=True, index=True)
    claim_id: Mapped[str | None] = mapped_column(ForeignKey("billing_claims.id", ondelete="SET NULL"),
                                                 nullable=True)
    match_status: Mapped[str] = mapped_column(String(20), default="unmatched")
    match_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_reminder_key: Mapped[str | None] = mapped_column(String(60), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)


class BillingPayment(Base):
    """A payment toward a bill; doubles as the receipt record."""

    __tablename__ = "billing_payments"
    __table_args__ = (
        UniqueConstraint("bill_id", "confirmation_number", name="uq_billing_payment_confirmation"),
        UniqueConstraint("patient_id", "receipt_number", name="uq_billing_payment_receipt"),
        CheckConstraint("amount > 0", name="ck_billing_payment_amount"),
        CheckConstraint(f"method IN ({_q(PAY_METHODS)})", name="ck_billing_payment_method"),
        Index("ix_billing_payments_bill_paid", "bill_id", "paid_on"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    patient_id: Mapped[str] = mapped_column(ForeignKey("patients.id", ondelete="CASCADE"), index=True)
    bill_id: Mapped[str] = mapped_column(ForeignKey("billing_bills.id", ondelete="CASCADE"), index=True)
    amount: Mapped[Decimal] = mapped_column(Money())
    paid_on: Mapped[date] = mapped_column(Date, index=True)
    method: Mapped[str] = mapped_column(String(20), default="card")
    confirmation_number: Mapped[str | None] = mapped_column(String(80), nullable=True)
    receipt_number: Mapped[str] = mapped_column(String(40))
    note: Mapped[str | None] = mapped_column(String(300), nullable=True)
    receipt_document_id: Mapped[str | None] = mapped_column(ForeignKey("documents.id", ondelete="SET NULL"),
                                                            nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class BillingDispute(Base):
    __tablename__ = "billing_disputes"
    __table_args__ = (
        CheckConstraint(f"status IN ({_q(DISPUTE_STATUSES)})", name="ck_billing_dispute_status"),
        CheckConstraint(f"reason_code IN ({_q(DISPUTE_REASONS)})", name="ck_billing_dispute_reason"),
        # at most ONE open dispute per bill
        Index("uq_billing_dispute_open_per_bill", "bill_id", unique=True,
              sqlite_where=text("status = 'open'"), postgresql_where=text("status = 'open'")),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    patient_id: Mapped[str] = mapped_column(ForeignKey("patients.id", ondelete="CASCADE"), index=True)
    bill_id: Mapped[str] = mapped_column(ForeignKey("billing_bills.id", ondelete="CASCADE"), index=True)
    reason_code: Mapped[str] = mapped_column(String(30))
    message: Mapped[str] = mapped_column(Text)
    disputed_amount: Mapped[Decimal | None] = mapped_column(Money(), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="open", index=True)
    status_before: Mapped[str | None] = mapped_column(String(20), nullable=True)  # bill status to restore
    resolution_note: Mapped[str | None] = mapped_column(String(500), nullable=True)
    opened_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    closed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
