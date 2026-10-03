"""W3 tables: staff accounts, staff sessions, notification preferences, DOB confirmations, reminder log.

Portable SQLAlchemy (SQLite dev / PostgreSQL prod): UUID string ids, UTC timestamps, real constraints.
"""
from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, ForeignKey, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.db_ops.types import UTCDateTime
from app.models.shared import new_id, utcnow

STAFF_ROLES = ("front_desk", "nurse", "physician", "admin")


class StaffUser(Base):
    __tablename__ = "staff_users"
    __table_args__ = (
        CheckConstraint("role IN ('front_desk','nurse','physician','admin')", name="ck_staff_role"),
        Index("ix_staff_users_provider_role", "provider_id", "role"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(200), index=True)
    email: Mapped[str] = mapped_column(String(254), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(300))
    role: Mapped[str] = mapped_column(String(20), index=True)  # front_desk | nurse | physician | admin
    provider_id: Mapped[str | None] = mapped_column(ForeignKey("providers.id", ondelete="SET NULL"),
                                                    nullable=True, index=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    def to_dict(self) -> dict:
        return {"id": self.id, "name": self.name, "email": self.email, "role": self.role,
                "provider_id": self.provider_id, "active": bool(self.active)}


class StaffSession(Base):
    """Server-side staff session (separate table + cookie from patient sessions)."""

    __tablename__ = "staff_sessions"

    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    staff_id: Mapped[str] = mapped_column(ForeignKey("staff_users.id", ondelete="CASCADE"), index=True)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime, index=True)


class NotificationPref(Base):
    """Per-kind on/off for a patient or staff recipient. Missing row = enabled."""

    __tablename__ = "notification_prefs"
    __table_args__ = (
        UniqueConstraint("recipient_type", "recipient_id", "kind", name="uq_notif_pref"),
        CheckConstraint("recipient_type IN ('patient','staff')", name="ck_notif_pref_type"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    recipient_type: Mapped[str] = mapped_column(String(20))
    recipient_id: Mapped[str] = mapped_column(String(36), index=True)  # polymorphic: patients.id or staff_users.id
    kind: Mapped[str] = mapped_column(String(50))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)


class StaffPatientConfirmation(Base):
    """Staff member confirmed a patient's DOB (anti mix-up step). Short-lived (see staff_portal.CONFIRM_TTL)."""

    __tablename__ = "staff_patient_confirmations"
    __table_args__ = (UniqueConstraint("staff_id", "patient_id", name="uq_staff_patient_confirm"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    staff_id: Mapped[str] = mapped_column(ForeignKey("staff_users.id", ondelete="CASCADE"), index=True)
    patient_id: Mapped[str] = mapped_column(ForeignKey("patients.id", ondelete="CASCADE"), index=True)
    confirmed_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class ReminderLog(Base):
    """Dedupe keys so the reminder sweep never sends the same reminder twice."""

    __tablename__ = "reminder_log"

    key: Mapped[str] = mapped_column(String(200), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)
