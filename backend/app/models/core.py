"""W1 core tables: sessions, emergency contacts. Appointment is TRANSITIONAL (W9 owns it; W3 imports it from here until W9 re-exports).  Portable SQLAlchemy 2.x (SQLite for dev, PostgreSQL for prod): UUID string ids, Numeric money,
UTC timestamps, real constraints and indexes on every FK and searched column."""
import json
from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.db_ops.types import UTCDateTime
from app.models.shared import new_id, utcnow

class PatientSession(Base):
    __tablename__ = "patient_sessions"

    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    patient_id: Mapped[str] = mapped_column(ForeignKey("patients.id", ondelete="CASCADE"), index=True)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime, index=True)


from app.models.appointments import Appointment  # noqa: E402,F401  (W9 owns it; re-exported for old imports)


class EmergencyContact(Base):
    """Emergency contact. `authorized_to_access_records` is a SEPARATE flag: being called in an emergency is not
    the same as being allowed to see records. It records the patient's wish only; real access needs a share grant
    (W2) or a caregiver account (W9)."""

    __tablename__ = "emergency_contacts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    patient_id: Mapped[str] = mapped_column(ForeignKey("patients.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    relationship_: Mapped[str] = mapped_column("relationship", String(100))
    phone: Mapped[str] = mapped_column(String(50))
    email: Mapped[str | None] = mapped_column(String(254), nullable=True)
    is_primary: Mapped[bool] = mapped_column(Boolean, default=False)
    authorized_to_access_records: Mapped[bool] = mapped_column(Boolean, default=False)
    notes: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)
