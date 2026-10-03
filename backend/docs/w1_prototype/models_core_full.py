"""W1 core tables. Portable SQLAlchemy 2.x (SQLite for dev, PostgreSQL for prod): UUID string ids, Numeric money,
UTC timestamps, real constraints and indexes on every FK and searched column."""
import json
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import CheckConstraint, Date, ForeignKey, Index, Numeric, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.db_ops.types import UTCDateTime
from app.models.shared import new_id, utcnow

RECORD_CATEGORIES = ("emergency_contact", "condition", "surgery", "hospitalization", "family_history",
                     "medication", "allergy", "result", "insurance")
_q = lambda vals: ",".join(f"'{v}'" for v in vals)
Money = lambda: Numeric(12, 2)


class PatientSession(Base):
    __tablename__ = "patient_sessions"

    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    patient_id: Mapped[str] = mapped_column(ForeignKey("patients.id", ondelete="CASCADE"), index=True)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime, index=True)


class Record(Base):
    """Generic patient-owned record. `category` selects the field schema (app/core_schemas.py)."""

    __tablename__ = "records"
    __table_args__ = (
        CheckConstraint(f"category IN ({_q(RECORD_CATEGORIES)})", name="ck_records_category"),
        CheckConstraint("source IN ('patient_entered','clinician_confirmed')", name="ck_records_source"),
        CheckConstraint("verification IS NULL OR verification IN ('entered','verified','failed')", name="ck_records_verification"),
        Index("ix_records_patient_category", "patient_id", "category"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    patient_id: Mapped[str] = mapped_column(ForeignKey("patients.id", ondelete="CASCADE"))
    category: Mapped[str] = mapped_column(String(30))
    data_json: Mapped[str] = mapped_column(Text, default="{}")
    source: Mapped[str] = mapped_column(String(30), default="patient_entered")
    confirmed_by: Mapped[str | None] = mapped_column(String(200), nullable=True)
    confirmed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    verification: Mapped[str | None] = mapped_column(String(20), nullable=True)  # insurance only (mock)
    verification_note: Mapped[str | None] = mapped_column(String(300), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)

    @property
    def data(self) -> dict:
        return json.loads(self.data_json or "{}")


class Eob(Base):
    """Insurer explanation of benefits (patient-entered numbers). Money is Numeric(12,2)."""

    __tablename__ = "eobs"
    __table_args__ = (
        UniqueConstraint("patient_id", "claim_number", name="uq_eob_patient_claim"),
        CheckConstraint("claim_status IN ('processing','approved','partially_denied','denied','appealed')", name="ck_eob_status"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    patient_id: Mapped[str] = mapped_column(ForeignKey("patients.id", ondelete="CASCADE"), index=True)
    insurer: Mapped[str] = mapped_column(String(200))
    claim_number: Mapped[str] = mapped_column(String(100), index=True)
    provider_name: Mapped[str] = mapped_column(String(200), index=True)
    service_date: Mapped[date] = mapped_column(Date, index=True)
    billed_amount: Mapped[Decimal] = mapped_column(Money())
    allowed_amount: Mapped[Decimal | None] = mapped_column(Money(), nullable=True)
    insurer_paid: Mapped[Decimal | None] = mapped_column(Money(), nullable=True)
    deductible: Mapped[Decimal | None] = mapped_column(Money(), nullable=True)
    copay: Mapped[Decimal | None] = mapped_column(Money(), nullable=True)
    coinsurance: Mapped[Decimal | None] = mapped_column(Money(), nullable=True)
    patient_responsibility: Mapped[Decimal | None] = mapped_column(Money(), nullable=True)
    claim_status: Mapped[str] = mapped_column(String(20), default="processing", index=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)


class Bill(Base):
    """Provider bill. Visible ONLY to the patient (never part of share scopes). Money is Numeric(12,2)."""

    __tablename__ = "bills"
    __table_args__ = (
        UniqueConstraint("patient_id", "provider_name", "service_date", "billed_amount", name="uq_bill_dedupe"),
        CheckConstraint("status IN ('unpaid','partially_paid','paid','in_dispute')", name="ck_bill_status"),
        CheckConstraint("billed_amount >= 0 AND amount_due >= 0 AND payments_made >= 0", name="ck_bill_amounts"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    patient_id: Mapped[str] = mapped_column(ForeignKey("patients.id", ondelete="CASCADE"), index=True)
    provider_name: Mapped[str] = mapped_column(String(200), index=True)
    service_date: Mapped[date] = mapped_column(Date, index=True)
    description: Mapped[str | None] = mapped_column(String(300), nullable=True)
    billed_amount: Mapped[Decimal] = mapped_column(Money())
    amount_due: Mapped[Decimal] = mapped_column(Money())
    payments_made: Mapped[Decimal] = mapped_column(Money(), default=Decimal("0.00"))
    due_date: Mapped[date | None] = mapped_column(Date, nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(20), default="unpaid", index=True)
    receipt_note: Mapped[str | None] = mapped_column(String(300), nullable=True)
    claim_id: Mapped[str | None] = mapped_column(ForeignKey("eobs.id", ondelete="SET NULL"), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)


class CorrectionRequest(Base):
    __tablename__ = "correction_requests"
    __table_args__ = (CheckConstraint("status IN ('open','resolved','declined')", name="ck_correction_status"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    patient_id: Mapped[str] = mapped_column(ForeignKey("patients.id", ondelete="CASCADE"), index=True)
    record_id: Mapped[str] = mapped_column(String(36), index=True)
    category: Mapped[str] = mapped_column(String(30))
    message: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="open")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class Appointment(Base):
    """Appointment request + intake. status: draft | requested | booked | rescheduled | cancelled | declined"""

    __tablename__ = "appointments"
    __table_args__ = (
        CheckConstraint("status IN ('draft','requested','booked','rescheduled','cancelled','declined')", name="ck_appt_status"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    patient_id: Mapped[str] = mapped_column(ForeignKey("patients.id", ondelete="CASCADE"), index=True)
    provider_id: Mapped[str | None] = mapped_column(ForeignKey("providers.id", ondelete="SET NULL"), nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(20), default="draft", index=True)
    intake_json: Mapped[str] = mapped_column(Text, default="{}")
    contact_json: Mapped[str] = mapped_column(Text, default="{}")  # profile snapshot the patient agreed to send
    scheduled_for: Mapped[str | None] = mapped_column(String(60), nullable=True)
    staff_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    submitted_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow, index=True)

    @property
    def intake(self) -> dict:
        return json.loads(self.intake_json or "{}")

    @property
    def contact(self) -> dict:
        return json.loads(self.contact_json or "{}")


class CaregiverGrant(Base):
    """Roadmap stub: records the patient's intent. Caregiver sign-in/enforcement is NOT implemented."""

    __tablename__ = "caregiver_grants"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    patient_id: Mapped[str] = mapped_column(ForeignKey("patients.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    relationship_: Mapped[str] = mapped_column("relationship", String(100))
    email: Mapped[str | None] = mapped_column(String(254), nullable=True)
    scope_json: Mapped[str] = mapped_column(Text, default="[]")
    expires_at: Mapped[str | None] = mapped_column(String(10), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
