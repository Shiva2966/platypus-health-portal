"""Shared tables used by every workstream: Patient, Provider, AuditLog, Notification."""
import json
import uuid
from datetime import datetime, timezone

from sqlalchemy import CheckConstraint, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.db_ops.types import UTCDateTime


def utcnow() -> datetime:
    """Naive UTC datetime (SQLite stores naive; we treat everything as UTC)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def new_id() -> str:
    return str(uuid.uuid4())


class Patient(Base):
    """id is a random UUID4 - never derived from any personal data."""

    __tablename__ = "patients"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    email: Mapped[str] = mapped_column(String(254), unique=True, index=True)  # login email
    password_hash: Mapped[str] = mapped_column(String(300))
    legal_name: Mapped[str] = mapped_column(String(200))
    preferred_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    dob: Mapped[str | None] = mapped_column(String(10), nullable=True)  # YYYY-MM-DD
    phone: Mapped[str | None] = mapped_column(String(50), nullable=True)
    address: Mapped[str | None] = mapped_column(String(500), nullable=True)
    pronouns: Mapped[str | None] = mapped_column(String(50), nullable=True)
    # unverified | pending | verified   (staff/identity checks set this; patient cannot)
    verification_status: Mapped[str] = mapped_column(String(20), default="unverified")
    verified_note: Mapped[str | None] = mapped_column(String(300), nullable=True)
    # unknown | has_allergies | no_known_allergies
    allergy_status: Mapped[str] = mapped_column(String(30), default="unknown")
    deletion_requested_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)

    @property
    def display_name(self) -> str:
        return self.preferred_name or self.legal_name


class Provider(Base):
    """A care organization/provider. Share grants and appointments target a Provider."""

    __tablename__ = "providers"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(200))
    specialty: Mapped[str | None] = mapped_column(String(100), nullable=True)
    address: Mapped[str | None] = mapped_column(String(300), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(50), nullable=True)
    # JSON text. availability: ["Mon 9-12", ...]; cost_estimates: [{"service","low","high"}] (MOCK)
    availability_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    cost_estimates_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "specialty": self.specialty,
            "address": self.address,
            "phone": self.phone,
            "availability": json.loads(self.availability_json or "[]"),
            "cost_estimates": json.loads(self.cost_estimates_json or "[]"),
        }


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)
    actor_type: Mapped[str] = mapped_column(String(20))  # patient | staff | system
    actor_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    patient_id: Mapped[str | None] = mapped_column(String(36), index=True, nullable=True)
    action: Mapped[str] = mapped_column(String(80))
    resource_type: Mapped[str | None] = mapped_column(String(50), nullable=True)
    resource_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON text


class Notification(Base):
    __tablename__ = "notifications"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)
    recipient_type: Mapped[str] = mapped_column(String(20))  # patient | staff
    recipient_id: Mapped[str] = mapped_column(String(36), index=True)
    kind: Mapped[str] = mapped_column(String(50))
    title: Mapped[str] = mapped_column(String(200))
    body: Mapped[str] = mapped_column(Text, default="")
    link: Mapped[str | None] = mapped_column(String(300), nullable=True)
    read_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
