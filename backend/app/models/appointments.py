"""W9 tables: appointment history, caregiver accounts/links, dependents, privacy + accessibility settings,
connected apps, deletion requests.

NOTE ON `Appointment`: the model lives HERE (moved from W1's app/models/core.py, which re-exports it so old
imports keep working).  There is exactly ONE `appointments` table.  Columns:
id, patient_id, provider_id, status (draft|requested|booked|rescheduled|cancelled|declined), intake_json,
contact_json, scheduled_for (str), staff_note, submitted_at, created_at, updated_at.

Portable SQLAlchemy 2.x: UUID string ids, UTC timestamps, real constraints, indexes on every FK/search column.
"""
from __future__ import annotations

import json
from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.db_ops.types import UTCDateTime
from app.models.shared import new_id, utcnow

APPT_STATUSES = ("draft", "requested", "booked", "rescheduled", "cancelled", "declined")
LINK_STATUSES = ("invited", "active", "revoked", "declined")
TEXT_SIZES = ("normal", "large", "xlarge")
DELETION_STATUSES = ("pending", "cancelled", "completed")


def _q(vals) -> str:
    return ",".join(f"'{v}'" for v in vals)


class Appointment(Base):
    """Appointment request + intake. status: draft | requested | booked | rescheduled | cancelled | declined.

    Moved here from app/models/core.py (which re-exports it, so old imports keep working).  Table/columns unchanged.
    """

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

class AppointmentEvent(Base):
    """Status history of an appointment (who changed what, when). Never edited."""

    __tablename__ = "appointment_events"
    __table_args__ = (
        CheckConstraint("actor_type IN ('patient','staff','caregiver','system')", name="ck_appt_event_actor"),
        Index("ix_appt_events_appt_ts", "appointment_id", "ts"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    appointment_id: Mapped[str] = mapped_column(ForeignKey("appointments.id", ondelete="CASCADE"), index=True)
    ts: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    actor_type: Mapped[str] = mapped_column(String(20))
    actor_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    from_status: Mapped[str | None] = mapped_column(String(20), nullable=True)
    to_status: Mapped[str] = mapped_column(String(20))
    note: Mapped[str | None] = mapped_column(Text, nullable=True)


class CaregiverAccount(Base):
    """A separate login for a caregiver (NOT a Patient).  Created when an invitation is accepted."""

    __tablename__ = "caregiver_accounts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    email: Mapped[str] = mapped_column(String(254), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(200))
    password_hash: Mapped[str] = mapped_column(String(300))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    disabled_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)


class CaregiverSession(Base):
    __tablename__ = "caregiver_sessions"

    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    caregiver_id: Mapped[str] = mapped_column(ForeignKey("caregiver_accounts.id", ondelete="CASCADE"), index=True)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime, index=True)


class CaregiverLink(Base):
    """A patient's invitation/grant to ONE caregiver.  Scoped, expiring, revocable.

    scope:  record_categories_json  list of Record categories the caregiver may VIEW (e.g. ["medication","allergy"])
            manage_appointments     may view/request/reschedule/cancel appointments
            manage_bills            may view bills/EOBs
    Access is allowed only while status == 'active', revoked_at is NULL and expires_at is in the future.
    """

    __tablename__ = "caregiver_links"
    __table_args__ = (
        CheckConstraint(f"status IN ({_q(LINK_STATUSES)})", name="ck_caregiver_link_status"),
        Index("ix_caregiver_links_patient_status", "patient_id", "status"),
        Index("ix_caregiver_links_email", "invite_email"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    patient_id: Mapped[str] = mapped_column(ForeignKey("patients.id", ondelete="CASCADE"), index=True)
    caregiver_id: Mapped[str | None] = mapped_column(ForeignKey("caregiver_accounts.id", ondelete="CASCADE"),
                                                     nullable=True, index=True)
    invite_email: Mapped[str] = mapped_column(String(254))
    invite_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    relationship_: Mapped[str | None] = mapped_column("relationship", String(100), nullable=True)
    invite_token_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, unique=True)
    status: Mapped[str] = mapped_column(String(20), default="invited")
    record_categories_json: Mapped[str] = mapped_column(Text, default="[]")
    manage_appointments: Mapped[bool] = mapped_column(Boolean, default=False)
    manage_bills: Mapped[bool] = mapped_column(Boolean, default=False)
    invited_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    invite_expires_at: Mapped[datetime] = mapped_column(UTCDateTime)  # the emailed link stops working
    accepted_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime, index=True)  # the ACCESS ends
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)


class DependentProfile(Base):
    """A person (child, adult in your care) managed by a guardian patient.

    The dependent gets a real, non-loginable `patients` row (so appointments/records can reference it with
    normal foreign keys).  Only the guardian (and caregivers with explicit scope on the GUARDIAN) act for it.
    """

    __tablename__ = "dependent_profiles"
    __table_args__ = (UniqueConstraint("dependent_patient_id", name="uq_dependent_patient"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    guardian_patient_id: Mapped[str] = mapped_column(ForeignKey("patients.id", ondelete="CASCADE"), index=True)
    dependent_patient_id: Mapped[str] = mapped_column(ForeignKey("patients.id", ondelete="CASCADE"))
    relationship_: Mapped[str] = mapped_column("relationship", String(100))
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    ended_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)


class PrivacySettings(Base):
    """One row per patient (created lazily).  Sharing defaults + communication permissions + accessibility."""

    __tablename__ = "privacy_settings"
    __table_args__ = (
        CheckConstraint(f"text_size IN ({_q(TEXT_SIZES)})", name="ck_privacy_text_size"),
        CheckConstraint("default_share_days >= 1 AND default_share_days <= 365", name="ck_privacy_share_days"),
    )

    patient_id: Mapped[str] = mapped_column(ForeignKey("patients.id", ondelete="CASCADE"), primary_key=True)
    # sharing defaults (W2's share UI may pre-fill from these; nothing is ever shared automatically)
    default_share_days: Mapped[int] = mapped_column(Integer, default=30)
    default_share_categories_json: Mapped[str] = mapped_column(Text, default="[]")
    share_requires_my_approval: Mapped[bool] = mapped_column(Boolean, default=True)
    notify_on_view: Mapped[bool] = mapped_column(Boolean, default=True)
    # communication permissions
    allow_email: Mapped[bool] = mapped_column(Boolean, default=True)
    allow_sms: Mapped[bool] = mapped_column(Boolean, default=False)
    allow_phone_calls: Mapped[bool] = mapped_column(Boolean, default=False)
    allow_appointment_reminders: Mapped[bool] = mapped_column(Boolean, default=True)
    allow_marketing: Mapped[bool] = mapped_column(Boolean, default=False)
    # accessibility (no language setting by design)
    text_size: Mapped[str] = mapped_column(String(10), default="normal")
    high_contrast: Mapped[bool] = mapped_column(Boolean, default=False)
    reduced_motion: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)


class ConnectedApp(Base):
    """A third-party app the patient connected (demo: mock catalogue, no real OAuth)."""

    __tablename__ = "connected_apps"
    __table_args__ = (Index("ix_connected_apps_patient", "patient_id", "revoked_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    patient_id: Mapped[str] = mapped_column(ForeignKey("patients.id", ondelete="CASCADE"), index=True)
    app_key: Mapped[str] = mapped_column(String(50))
    name: Mapped[str] = mapped_column(String(100))
    scopes_json: Mapped[str] = mapped_column(Text, default="[]")
    connected_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)


class DeletionRequest(Base):
    """Account-deletion workflow.  Nothing is erased instantly: pending -> (cooling-off) -> completed by an admin."""

    __tablename__ = "deletion_requests"
    __table_args__ = (
        CheckConstraint(f"status IN ({_q(DELETION_STATUSES)})", name="ck_deletion_status"),
        Index("ix_deletion_requests_patient_status", "patient_id", "status"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    patient_id: Mapped[str] = mapped_column(ForeignKey("patients.id", ondelete="CASCADE"), index=True)
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    requested_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    earliest_completion_at: Mapped[datetime] = mapped_column(UTCDateTime)
    resolved_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
