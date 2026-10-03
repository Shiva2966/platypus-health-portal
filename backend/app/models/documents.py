"""Document storage + sharing/consent tables (W2).

Files live IN THE DATABASE: `documents` holds metadata only; the bytes live in `document_blobs`
(one row per document) so list/search queries never load file data.

Timestamps are naive UTC like app/models/shared.py (SQLite returns naive values; we treat all as UTC).
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    Boolean, CheckConstraint, DateTime, ForeignKey, Index, Integer, LargeBinary, String, Text, text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.data_crypto import EncryptedBlob
from app.db import Base


def utcnow() -> datetime:
    """Naive UTC timestamp."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def new_id() -> str:
    return str(uuid.uuid4())


CATEGORIES = [
    "lab_result", "imaging", "prescription", "visit_summary", "insurance",
    "billing", "identification", "vaccination", "referral", "other",
]  # DOCUMENT categories (what a patient files an upload under)

# Structured RECORD categories (W7 medical data + W8 billing data). A category grant with one of these
# names exposes that patient data to the grantee's staff; `billing` ALSO covers documents filed as billing.
CLINICAL_RECORD_CATEGORIES = ["history", "medications", "allergies", "vaccinations", "results"]
RECORD_CATEGORIES = CLINICAL_RECORD_CATEGORIES + ["billing"]
# The ONE list of categories a grant / share token / access request may name.
SHARE_CATEGORIES = CATEGORIES + [c for c in RECORD_CATEGORIES if c not in CATEGORIES]


class Document(Base):
    __tablename__ = "documents"
    __table_args__ = (
        CheckConstraint("size_bytes >= 0", name="ck_documents_size"),
        CheckConstraint("source IN ('patient_uploaded','clinician_provided')", name="ck_documents_source"),
        CheckConstraint("length(name) >= 1", name="ck_documents_name_nonempty"),
        # duplicate detection per patient among NON-deleted documents
        Index("uq_documents_patient_sha_active", "patient_id", "sha256", unique=True,
              sqlite_where=text("deleted_at IS NULL"), postgresql_where=text("deleted_at IS NULL")),
        Index("ix_documents_patient_created", "patient_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    patient_id: Mapped[str] = mapped_column(ForeignKey("patients.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(200), index=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    category: Mapped[str] = mapped_column(String(40), default="other", index=True)
    mime_type: Mapped[str] = mapped_column(String(120), index=True)
    size_bytes: Mapped[int] = mapped_column(Integer)
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    source: Mapped[str] = mapped_column(String(30), default="patient_uploaded")
    uploaded_by_type: Mapped[str] = mapped_column(String(20), default="patient")
    uploaded_by_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    is_private: Mapped[bool] = mapped_column(Boolean, default=True)


class DocumentBlob(Base):
    """The actual file bytes (BLOB) for a document. Kept apart so metadata queries stay light."""
    __tablename__ = "document_blobs"

    document_id: Mapped[str] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"), primary_key=True)
    data: Mapped[bytes] = mapped_column(EncryptedBlob)  # AES-256-GCM at rest when DATA_ENCRYPTION_KEY is set


class ShareGrant(Base):
    """A patient's consent for one provider (organization) to see a document or a category."""
    __tablename__ = "share_grants"
    __table_args__ = (
        CheckConstraint("scope_type IN ('document','category')", name="ck_share_grants_scope"),
        CheckConstraint("(scope_type = 'document' AND document_id IS NOT NULL) OR "
                        "(scope_type = 'category' AND category IS NOT NULL)", name="ck_share_grants_target"),
        Index("ix_share_grants_lookup", "patient_id", "provider_id"),
        Index("ix_share_grants_expires", "expires_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    patient_id: Mapped[str] = mapped_column(ForeignKey("patients.id", ondelete="CASCADE"), index=True)
    provider_id: Mapped[str] = mapped_column(ForeignKey("providers.id", ondelete="CASCADE"), index=True)
    scope_type: Mapped[str] = mapped_column(String(10))  # 'document' | 'category'
    document_id: Mapped[str | None] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"),
                                                    nullable=True, index=True)
    category: Mapped[str | None] = mapped_column(String(40), nullable=True)
    include_private: Mapped[bool] = mapped_column(Boolean, default=False)
    purpose: Mapped[str | None] = mapped_column(String(300), nullable=True)
    via: Mapped[str] = mapped_column(String(20), default="patient")  # patient|access_request|token
    access_request_id: Mapped[str | None] = mapped_column(ForeignKey("access_requests.id", ondelete="SET NULL"),
                                                          nullable=True, index=True)
    token_id: Mapped[str | None] = mapped_column(ForeignKey("share_tokens.id", ondelete="SET NULL"),
                                                 nullable=True, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    expiry_notified_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class ShareToken(Base):
    """Opaque one-time/multi-use token (QR payload). Only a SHA-256 hash is stored."""
    __tablename__ = "share_tokens"
    __table_args__ = (
        CheckConstraint("max_uses >= 1 AND use_count >= 0", name="ck_share_tokens_uses"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    patient_id: Mapped[str] = mapped_column(ForeignKey("patients.id", ondelete="CASCADE"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    categories_json: Mapped[str] = mapped_column(Text, default="[]")
    document_ids_json: Mapped[str] = mapped_column(Text, default="[]")
    include_private: Mapped[bool] = mapped_column(Boolean, default=False)
    purpose: Mapped[str | None] = mapped_column(String(300), nullable=True)
    grant_days: Mapped[int] = mapped_column(Integer, default=3)
    max_uses: Mapped[int] = mapped_column(Integer, default=1)
    use_count: Mapped[int] = mapped_column(Integer, default=0)
    expires_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class AccessRequest(Base):
    """Staff-initiated request; the patient approves (optionally narrowing) or denies."""
    __tablename__ = "access_requests"
    __table_args__ = (
        CheckConstraint("status IN ('pending','approved','denied','expired')", name="ck_access_requests_status"),
        CheckConstraint("duration_days >= 1", name="ck_access_requests_duration"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    staff_id: Mapped[str | None] = mapped_column(ForeignKey("staff_users.id", ondelete="SET NULL"),
                                                 nullable=True, index=True)
    provider_id: Mapped[str] = mapped_column(ForeignKey("providers.id", ondelete="CASCADE"), index=True)
    patient_id: Mapped[str] = mapped_column(ForeignKey("patients.id", ondelete="CASCADE"), index=True)
    categories_json: Mapped[str] = mapped_column(Text, default="[]")
    document_ids_json: Mapped[str] = mapped_column(Text, default="[]")
    purpose: Mapped[str | None] = mapped_column(String(300), nullable=True)
    duration_days: Mapped[int] = mapped_column(Integer, default=7)
    status: Mapped[str] = mapped_column(String(12), default="pending", index=True)  # pending|approved|denied|expired
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)  # request lifetime


class ShareTokenAttempt(Base):
    """One FAILED share-code redemption (DB-backed lockout so it survives restarts / multiple workers)."""
    __tablename__ = "share_token_attempts"
    __table_args__ = (Index("ix_share_token_attempts_key_ts", "key", "ts"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    key: Mapped[str] = mapped_column(String(80))  # e.g. "staff:<staff id>"
    ts: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
