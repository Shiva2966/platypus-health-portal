"""W6 tables: email OTP codes, trusted devices, pending (unverified) sign-ups, login failures.

Rules followed (see CONTRACT "DATABASE RELIABILITY RULES"): String(36) UUID ids, timezone-aware UTC
timestamps, CHECK constraints for enums, indexes on every searched column. `account_id` is polymorphic
(patients.id OR staff_users.id depending on `account_type`), so it cannot be a real foreign key.

SECURITY: plaintext OTP codes are NEVER stored - only a salted HMAC (`code_hash`).
"""
from datetime import datetime, timezone

from sqlalchemy import CheckConstraint, DateTime, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models.shared import new_id

ACCOUNT_TYPES = ("patient", "staff")
OTP_PURPOSES = ("signup", "login", "reset", "email_change")


def utcnow_tz() -> datetime:
    """Timezone-aware UTC now (use this for every W6 timestamp)."""
    return datetime.now(timezone.utc)


def as_utc(dt: datetime | None) -> datetime | None:
    """SQLite hands back naive datetimes; treat them as UTC so comparisons never raise."""
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


class OtpCode(Base):
    __tablename__ = "otp_codes"
    __table_args__ = (
        CheckConstraint("account_type IN ('patient','staff')", name="ck_otp_account_type"),
        CheckConstraint("purpose IN ('signup','login','reset','email_change')", name="ck_otp_purpose"),
        CheckConstraint("attempts >= 0 AND max_attempts > 0", name="ck_otp_attempts"),
        Index("ix_otp_key_created", "account_type", "email", "purpose", "created_at"),
        Index("ix_otp_ip_created", "ip", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    account_type: Mapped[str] = mapped_column(String(10))
    account_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)  # NULL pre-signup / unknown email
    email: Mapped[str] = mapped_column(String(254), index=True)
    purpose: Mapped[str] = mapped_column(String(20))
    code_hash: Mapped[str] = mapped_column(String(200))  # "salt$hmac" - never the code
    # sha256 of the opaque `otp_challenge_id` handed to the client (login purpose only)
    challenge_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow_tz, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    max_attempts: Mapped[int] = mapped_column(Integer, default=5, nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(300), nullable=True)


class TrustedDevice(Base):
    """'Remember this device for 30 days' - only a hash of the random cookie token is stored."""

    __tablename__ = "trusted_devices"
    __table_args__ = (
        CheckConstraint("account_type IN ('patient','staff')", name="ck_trusted_account_type"),
        Index("ix_trusted_account", "account_type", "account_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    account_type: Mapped[str] = mapped_column(String(10))
    account_id: Mapped[str] = mapped_column(String(36))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    label: Mapped[str | None] = mapped_column(String(120), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow_tz)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class PendingSignup(Base):
    """A sign-up waiting for its email OTP. The real account row is created ONLY after verification,
    so an unverified account can never log in (and never appears in patient search)."""

    __tablename__ = "pending_signups"
    __table_args__ = (
        CheckConstraint("account_type IN ('patient','staff')", name="ck_pending_account_type"),
        UniqueConstraint("account_type", "email", name="uq_pending_signup_email"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    account_type: Mapped[str] = mapped_column(String(10))
    email: Mapped[str] = mapped_column(String(254), index=True)
    name: Mapped[str] = mapped_column(String(200))
    dob: Mapped[str | None] = mapped_column(String(10), nullable=True)
    password_hash: Mapped[str] = mapped_column(String(300))
    extra_json: Mapped[str | None] = mapped_column(Text, nullable=True)  # staff: provider_id
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow_tz)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class LoginFailure(Base):
    """One row per bad password. Keyed by email text (not account id) so unknown emails behave identically."""

    __tablename__ = "login_failures"
    __table_args__ = (
        CheckConstraint("account_type IN ('patient','staff')", name="ck_loginfail_account_type"),
        Index("ix_loginfail_email_ts", "account_type", "email", "ts"),
        Index("ix_loginfail_ip_ts", "ip", "ts"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_type: Mapped[str] = mapped_column(String(10))
    email: Mapped[str] = mapped_column(String(254))
    ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow_tz)
