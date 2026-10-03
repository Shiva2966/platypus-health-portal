"""W7 medical-record tables (history, medications, allergies, vaccinations/preventive care, results, care team,
correction requests).  Portable SQLAlchemy 2.x: UUID string ids, Numeric for measured values, timezone-aware UTC
timestamps, real FOREIGN KEYs with explicit ondelete, CHECK/UNIQUE constraints and an index on every FK and every
searched column.  Synthetic demo data only.

Conventions
* ``source``: ``patient_entered`` | ``clinician_confirmed``.  Patients can only create ``patient_entered``;
  clinician confirmation goes through ``app.services.medical_write.confirm_record`` (staff side, later).
* Partial dates (``start_date`` etc.) are text ``YYYY`` | ``YYYY-MM`` | ``YYYY-MM-DD`` so "I only remember the year"
  is representable.  NULL means *Unknown*.  Columns that are real calendar dates use ``Date``.
* ``*_key`` columns hold the normalised (casefolded, whitespace-collapsed) name used for uniqueness/grouping.
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (Boolean, CheckConstraint, Date, ForeignKey, Index, Numeric, String, Text,
                        UniqueConstraint)
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.db_ops.types import UTCDateTime
from app.models.shared import new_id, utcnow

SOURCES = ("patient_entered", "clinician_confirmed")
HISTORY_KINDS = ("condition", "surgery", "hospitalization", "family_history")
HISTORY_STATUSES = ("active", "resolved", "unknown")
MED_STATUSES = ("active", "past")
ALLERGY_CATEGORIES = ("drug", "food", "environmental", "other", "unknown")
ALLERGY_SEVERITIES = ("mild", "moderate", "severe", "unknown")
ALLERGY_STATUSES = ("active", "inactive")
VACCINATION_KINDS = ("vaccine", "screening", "preventive_care")
VACCINATION_STATUSES = ("completed", "scheduled", "declined", "unknown")
RESULT_CATEGORIES = ("lab", "imaging", "other")
PROVIDER_KINDS = ("clinician", "facility", "pharmacy", "lab", "other")
CORRECTION_STATUSES = ("open", "resolved", "declined", "withdrawn")
CORRECTABLE_TYPES = ("history", "medication", "allergy", "vaccination", "result", "provider")


def norm_key(s: str) -> str:
    """Normalised name for uniqueness / grouping (case, spacing)."""
    return " ".join((s or "").casefold().split())


def _key_of(col: str):
    """Column default/onupdate: derive the normalised key from ``col`` so callers that omit it (seeds, other
    workstreams) still satisfy NOT NULL + UNIQUE."""
    return lambda ctx: norm_key(ctx.get_current_parameters().get(col))


def _q(vals) -> str:
    return ",".join(f"'{v}'" for v in vals)


def _pd(col: str) -> str:
    """CHECK fragment: a partial date is NULL or has the length of YYYY / YYYY-MM / YYYY-MM-DD."""
    return f"{col} IS NULL OR length({col}) IN (4, 7, 10)"


def _fk(target: str, *, nullable: bool = True, ondelete: str = "SET NULL"):
    return mapped_column(ForeignKey(target, ondelete=ondelete), nullable=nullable, index=True)


class _RecordMixin:
    """Columns every patient-facing record carries: owner, source label, confirmation, timestamps."""

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    patient_id: Mapped[str] = mapped_column(ForeignKey("patients.id", ondelete="CASCADE"), index=True)
    source: Mapped[str] = mapped_column(String(30), default="patient_entered", server_default="patient_entered")
    confirmed_by: Mapped[str | None] = mapped_column(String(200), nullable=True)
    confirmed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow, index=True)


class MedProvider(_RecordMixin, Base):
    """Care-team directory entry (clinician, facility, pharmacy, lab). Optionally linked to a hospital in ``providers``."""

    __tablename__ = "med_providers"
    __table_args__ = (
        UniqueConstraint("patient_id", "name_key", name="uq_med_providers_patient_name"),
        CheckConstraint(f"kind IN ({_q(PROVIDER_KINDS)})", name="ck_med_providers_kind"),
        CheckConstraint(f"source IN ({_q(SOURCES)})", name="ck_med_providers_source"),
        CheckConstraint("length(name) >= 1", name="ck_med_providers_name"),
        Index("ix_med_providers_patient_care_team", "patient_id", "is_care_team"),
    )

    name: Mapped[str] = mapped_column(String(200), index=True)
    name_key: Mapped[str] = mapped_column(String(200), default=_key_of("name"), onupdate=_key_of("name"))
    kind: Mapped[str] = mapped_column(String(20), default="clinician")
    specialty: Mapped[str | None] = mapped_column(String(100), nullable=True)
    organization: Mapped[str | None] = mapped_column(String(200), nullable=True)
    role_in_care: Mapped[str | None] = mapped_column(String(100), nullable=True)  # e.g. "Primary care"
    phone: Mapped[str | None] = mapped_column(String(50), nullable=True)
    address: Mapped[str | None] = mapped_column(String(300), nullable=True)
    is_care_team: Mapped[bool] = mapped_column(Boolean, default=True)
    last_seen: Mapped[date | None] = mapped_column(Date, nullable=True)
    linked_provider_id: Mapped[str | None] = _fk("providers.id")


class MedHistory(_RecordMixin, Base):
    """Medical history timeline: conditions, surgeries, hospitalizations, family history."""

    __tablename__ = "med_history"
    __table_args__ = (
        CheckConstraint(f"kind IN ({_q(HISTORY_KINDS)})", name="ck_med_history_kind"),
        CheckConstraint(f"status IN ({_q(HISTORY_STATUSES)})", name="ck_med_history_status"),
        CheckConstraint(f"source IN ({_q(SOURCES)})", name="ck_med_history_source"),
        CheckConstraint("length(title) >= 1", name="ck_med_history_title"),
        CheckConstraint(_pd("start_date"), name="ck_med_history_start_date"),
        CheckConstraint(_pd("end_date"), name="ck_med_history_end_date"),
        Index("ix_med_history_patient_kind", "patient_id", "kind"),
        Index("ix_med_history_patient_start", "patient_id", "start_date"),
    )

    kind: Mapped[str] = mapped_column(String(20))
    title: Mapped[str] = mapped_column(String(200), index=True)
    status: Mapped[str] = mapped_column(String(20), default="unknown")
    start_date: Mapped[str | None] = mapped_column(String(10), nullable=True)
    end_date: Mapped[str | None] = mapped_column(String(10), nullable=True)
    relation: Mapped[str | None] = mapped_column(String(60), nullable=True)  # family_history only
    facility_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    provider_id: Mapped[str | None] = _fk("med_providers.id")


class MedMedication(_RecordMixin, Base):
    """Medications and supplements.  Descriptive only - no interaction checks, no advice."""

    __tablename__ = "med_medications"
    __table_args__ = (
        CheckConstraint(f"status IN ({_q(MED_STATUSES)})", name="ck_med_medications_status"),
        CheckConstraint(f"source IN ({_q(SOURCES)})", name="ck_med_medications_source"),
        CheckConstraint("length(name) >= 1", name="ck_med_medications_name"),
        CheckConstraint(_pd("start_date"), name="ck_med_medications_start_date"),
        CheckConstraint(_pd("end_date"), name="ck_med_medications_end_date"),
        Index("ix_med_medications_patient_status", "patient_id", "status"),
    )

    name: Mapped[str] = mapped_column(String(200), index=True)
    status: Mapped[str] = mapped_column(String(10), default="active")
    is_supplement: Mapped[bool] = mapped_column(Boolean, default=False)
    dose: Mapped[str | None] = mapped_column(String(100), nullable=True)
    frequency: Mapped[str | None] = mapped_column(String(100), nullable=True)
    route: Mapped[str | None] = mapped_column(String(50), nullable=True)
    reason: Mapped[str | None] = mapped_column(String(200), nullable=True)  # what it is taken for, in the patient's words
    start_date: Mapped[str | None] = mapped_column(String(10), nullable=True)
    end_date: Mapped[str | None] = mapped_column(String(10), nullable=True)
    prescriber_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    prescriber_id: Mapped[str | None] = _fk("med_providers.id")


class MedAllergy(_RecordMixin, Base):
    """One allergy/intolerance.  The patient-level "No known allergies" / "Unknown" flag lives in
    ``patients.allergy_status`` (single source of truth, shared with the intake form)."""

    __tablename__ = "med_allergies"
    __table_args__ = (
        UniqueConstraint("patient_id", "substance_key", name="uq_med_allergies_patient_substance"),
        CheckConstraint(f"category IN ({_q(ALLERGY_CATEGORIES)})", name="ck_med_allergies_category"),
        CheckConstraint(f"severity IN ({_q(ALLERGY_SEVERITIES)})", name="ck_med_allergies_severity"),
        CheckConstraint(f"status IN ({_q(ALLERGY_STATUSES)})", name="ck_med_allergies_status"),
        CheckConstraint(f"source IN ({_q(SOURCES)})", name="ck_med_allergies_source"),
        CheckConstraint("length(substance) >= 1", name="ck_med_allergies_substance"),
        CheckConstraint(_pd("onset"), name="ck_med_allergies_onset"),
        Index("ix_med_allergies_patient_status", "patient_id", "status"),
    )

    substance: Mapped[str] = mapped_column(String(200), index=True)
    substance_key: Mapped[str] = mapped_column(String(200), default=_key_of("substance"), onupdate=_key_of("substance"))
    category: Mapped[str] = mapped_column(String(20), default="unknown")
    reaction: Mapped[str | None] = mapped_column(String(300), nullable=True)
    severity: Mapped[str] = mapped_column(String(10), default="unknown")
    status: Mapped[str] = mapped_column(String(10), default="active")
    onset: Mapped[str | None] = mapped_column(String(10), nullable=True)


class MedVaccination(_RecordMixin, Base):
    """Vaccinations and preventive care (screenings, check-ups) with an optional follow-up date."""

    __tablename__ = "med_vaccinations"
    __table_args__ = (
        CheckConstraint(f"kind IN ({_q(VACCINATION_KINDS)})", name="ck_med_vaccinations_kind"),
        CheckConstraint(f"status IN ({_q(VACCINATION_STATUSES)})", name="ck_med_vaccinations_status"),
        CheckConstraint(f"source IN ({_q(SOURCES)})", name="ck_med_vaccinations_source"),
        CheckConstraint("length(name) >= 1", name="ck_med_vaccinations_name"),
        CheckConstraint(_pd("date_given"), name="ck_med_vaccinations_date_given"),
        CheckConstraint("dose_number IS NULL OR (dose_number >= 1 AND dose_number <= 20)",
                        name="ck_med_vaccinations_dose"),
        Index("ix_med_vaccinations_patient_due", "patient_id", "next_due_date"),
    )

    kind: Mapped[str] = mapped_column(String(20), default="vaccine")
    name: Mapped[str] = mapped_column(String(200), index=True)
    status: Mapped[str] = mapped_column(String(15), default="completed")
    date_given: Mapped[str | None] = mapped_column(String(10), nullable=True)
    dose_number: Mapped[int | None] = mapped_column(nullable=True)
    administered_by: Mapped[str | None] = mapped_column(String(200), nullable=True)
    provider_id: Mapped[str | None] = _fk("med_providers.id")
    next_due_date: Mapped[date | None] = mapped_column(Date, nullable=True)  # follow-up / next dose / next screening


class MedResult(_RecordMixin, Base):
    """A test result.  ``value_num`` + ``unit`` + the lab's own reference range make results comparable; the
    app only states whether a value is inside/outside THAT range - it never interprets it."""

    __tablename__ = "med_results"
    __table_args__ = (
        CheckConstraint(f"category IN ({_q(RESULT_CATEGORIES)})", name="ck_med_results_category"),
        CheckConstraint(f"source IN ({_q(SOURCES)})", name="ck_med_results_source"),
        CheckConstraint("length(test_name) >= 1", name="ck_med_results_test_name"),
        CheckConstraint("value_num IS NOT NULL OR value_text IS NOT NULL", name="ck_med_results_has_value"),
        CheckConstraint("ref_low IS NULL OR ref_high IS NULL OR ref_low <= ref_high", name="ck_med_results_ref_order"),
        Index("ix_med_results_patient_series", "patient_id", "test_key", "result_date"),
        Index("ix_med_results_patient_date", "patient_id", "result_date"),
    )

    test_name: Mapped[str] = mapped_column(String(200), index=True)
    test_key: Mapped[str] = mapped_column(String(200), default=_key_of("test_name"), onupdate=_key_of("test_name"))
    category: Mapped[str] = mapped_column(String(10), default="lab")
    result_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    value_num: Mapped[Decimal | None] = mapped_column(Numeric(14, 4), nullable=True)
    value_text: Mapped[str | None] = mapped_column(String(300), nullable=True)  # non-numeric / qualitative values
    unit: Mapped[str | None] = mapped_column(String(40), nullable=True)
    ref_low: Mapped[Decimal | None] = mapped_column(Numeric(14, 4), nullable=True)
    ref_high: Mapped[Decimal | None] = mapped_column(Numeric(14, 4), nullable=True)
    ref_text: Mapped[str | None] = mapped_column(String(200), nullable=True)  # e.g. "Negative"
    source_name: Mapped[str | None] = mapped_column(String(200), nullable=True)  # lab / facility that reported it
    provider_id: Mapped[str | None] = _fk("med_providers.id")
    document_id: Mapped[str | None] = _fk("documents.id")  # original report in the document store (W2)


class MedCorrection(Base):
    """Patient asks for a record to be corrected.  Staff-visible later; the record itself is not changed by this."""

    __tablename__ = "med_corrections"
    __table_args__ = (
        CheckConstraint(f"record_type IN ({_q(CORRECTABLE_TYPES)})", name="ck_med_corrections_record_type"),
        CheckConstraint(f"status IN ({_q(CORRECTION_STATUSES)})", name="ck_med_corrections_status"),
        CheckConstraint("length(message) >= 1", name="ck_med_corrections_message"),
        Index("ix_med_corrections_patient_status", "patient_id", "status"),
        Index("ix_med_corrections_record", "record_type", "record_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    patient_id: Mapped[str] = mapped_column(ForeignKey("patients.id", ondelete="CASCADE"), index=True)
    record_type: Mapped[str] = mapped_column(String(20))
    record_id: Mapped[str] = mapped_column(String(36))  # polymorphic (no FK); verified against the patient on create
    record_label: Mapped[str] = mapped_column(String(200), default="")  # snapshot of the record's title
    message: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(15), default="open")
    resolution_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    resolved_by: Mapped[str | None] = mapped_column(String(200), nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)
