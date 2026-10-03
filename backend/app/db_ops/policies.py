"""Central database policy layer (W5).

Applies the DATABASE RELIABILITY RULES from CONTRACT.md to ``Base.metadata`` in ONE place, so
the rules hold for every model module no matter who wrote it, for BOTH ``create_all`` and Alembic
autogenerate (Alembic's env.py calls ``apply_policies`` too):

1. every ``DateTime`` column becomes ``UTCDateTime`` (timestamptz on PostgreSQL, naive-UTC python values)
2. CHECK constraints for status / enum / non-negative columns        (``CHECKS``)
3. missing FOREIGN KEYS for id columns that were declared as bare strings (``FOREIGN_KEYS``),
   DEFERRABLE INITIALLY DEFERRED so flush order never matters and integrity is checked at COMMIT
4. explicit ``ondelete`` on every FK that lacks one (nullable -> SET NULL, required -> RESTRICT)
5. an index on every FK column, plus the searched-column / composite indexes in ``INDEXES``
6. partial UNIQUE index so one live (non soft-deleted) document per (patient, sha256)

Everything is idempotent and tolerant of tables/columns that do not exist (yet).
Rules for a table that nobody has created are silently skipped.
"""
from __future__ import annotations

import logging

from sqlalchemy import (CheckConstraint, DateTime, ForeignKeyConstraint, Index, MetaData, Table,
                        func, text)

from app.db_ops.types import UTCDateTime

log = logging.getLogger("health-portal.db")

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


def _in(values) -> str:
    return "(" + ", ".join("'" + v + "'" for v in values) + ")"


# (table, constraint_suffix, sql)      sql is portable (no dialect functions beyond length()).
_ROLE = ("front_desk", "nurse", "physician", "admin")
CHECKS: list[tuple[str, str, str]] = [
    ("patients", "verification_status", f"verification_status IN {_in(['unverified', 'pending', 'verified'])}"),
    ("patients", "allergy_status", f"allergy_status IN {_in(['unknown', 'has_allergies', 'no_known_allergies'])}"),
    ("patients", "email_nonempty", "length(email) > 2"),
    ("audit_log", "actor_type", f"actor_type IN {_in(['patient', 'staff', 'system'])}"),
    ("audit_log", "action_nonempty", "length(action) > 0"),
    ("notifications", "recipient_type", f"recipient_type IN {_in(['patient', 'staff'])}"),
    ("notification_prefs", "recipient_type", f"recipient_type IN {_in(['patient', 'staff'])}"),
    ("records", "verification", f"verification IS NULL OR verification IN {_in(['entered', 'verified'])}"),
    ("correction_requests", "status", f"status IN {_in(['open', 'resolved', 'declined'])}"),
    ("appointments", "status",
     f"status IN {_in(['draft', 'requested', 'booked', 'rescheduled', 'cancelled', 'declined'])}"),
    ("staff_users", "role", f"role IN {_in(_ROLE)}"),
    ("documents", "size_bytes", "size_bytes >= 0 AND size_bytes <= 15728640"),
    ("documents", "sha256_len", "length(sha256) = 64"),
    ("documents", "source", f"source IN {_in(['patient_uploaded', 'clinician_provided'])}"),
    ("documents", "uploaded_by_type", f"uploaded_by_type IN {_in(['patient', 'staff', 'system'])}"),
    ("document_blobs", "data_nonempty", "length(data) > 0"),
    ("share_grants", "via", f"via IN {_in(['patient', 'access_request', 'token'])}"),
    ("share_tokens", "grant_days_pos", "grant_days > 0"),
    ("access_requests", "status", f"status IN {_in(['pending', 'approved', 'denied', 'expired'])}"),
    ("access_requests", "duration_pos", "duration_days > 0"),
]

# (table, column, target "table.column", ondelete)
FOREIGN_KEYS: list[tuple[str, str, str, str]] = [
    ("documents", "patient_id", "patients.id", "CASCADE"),
    ("document_blobs", "document_id", "documents.id", "CASCADE"),
    ("share_grants", "patient_id", "patients.id", "CASCADE"),
    ("share_grants", "provider_id", "providers.id", "CASCADE"),
    ("share_grants", "document_id", "documents.id", "CASCADE"),
    ("share_grants", "access_request_id", "access_requests.id", "SET NULL"),
    ("share_grants", "token_id", "share_tokens.id", "SET NULL"),
    ("share_tokens", "patient_id", "patients.id", "CASCADE"),
    ("access_requests", "patient_id", "patients.id", "CASCADE"),
    ("access_requests", "provider_id", "providers.id", "RESTRICT"),
    ("access_requests", "staff_id", "staff_users.id", "RESTRICT"),
    ("staff_users", "provider_id", "providers.id", "SET NULL"),
    ("staff_sessions", "staff_id", "staff_users.id", "CASCADE"),
    ("staff_patient_confirmations", "staff_id", "staff_users.id", "CASCADE"),
    ("staff_patient_confirmations", "patient_id", "patients.id", "CASCADE"),
    ("correction_requests", "record_id", "records.id", "CASCADE"),
]

# (table, index_name, [columns or sql-expression strings], unique)
INDEXES: list[tuple[str, str, list[str], bool]] = [
    ("patients", "ix_patients_legal_name", ["legal_name"], False),
    ("patients", "ix_patients_dob", ["dob"], False),
    ("patients", "uq_patients_email_lower", ["lower(email)"], True),
    ("staff_users", "uq_staff_users_email_lower", ["lower(email)"], True),
    ("providers", "ix_providers_name", ["name"], False),
    ("notifications", "ix_notifications_recipient_unread", ["recipient_type", "recipient_id", "read_at"], False),
    ("notifications", "ix_notifications_recipient_ts", ["recipient_type", "recipient_id", "ts"], False),
    ("audit_log", "ix_audit_log_patient_ts", ["patient_id", "ts"], False),
    ("audit_log", "ix_audit_log_actor", ["actor_type", "actor_id"], False),
    ("audit_log", "ix_audit_log_action", ["action"], False),
    ("records", "ix_records_patient_category", ["patient_id", "category"], False),
    ("appointments", "ix_appointments_status", ["status"], False),
    ("appointments", "ix_appointments_provider_status", ["provider_id", "status"], False),
    ("appointments", "ix_appointments_scheduled_for", ["scheduled_for"], False),
    ("documents", "ix_documents_patient_created", ["patient_id", "created_at"], False),
    ("documents", "ix_documents_name", ["name"], False),
    ("share_grants", "ix_share_grants_expires_at", ["expires_at"], False),
    ("share_grants", "ix_share_grants_document", ["document_id", "provider_id"], False),
    ("share_tokens", "ix_share_tokens_expires_at", ["expires_at"], False),
    ("access_requests", "ix_access_requests_patient_status", ["patient_id", "status"], False),
    ("patient_sessions", "ix_patient_sessions_expires_at", ["expires_at"], False),
    ("staff_sessions", "ix_staff_sessions_expires_at", ["expires_at"], False),
]

# Partial UNIQUE: one *live* document per (patient, sha256).  Works on SQLite and PostgreSQL.
PARTIAL_UNIQUE = [
    ("documents", "uq_documents_patient_sha256_live", ["patient_id", "sha256"], "deleted_at IS NULL"),
]


def _tbl(md: MetaData, name: str) -> Table | None:
    return md.tables.get(name)


def _has_constraint(table: Table, name: str) -> bool:
    return any(getattr(c, "name", None) == name for c in table.constraints)


def _has_index(table: Table, name: str) -> bool:
    return any(i.name == name for i in table.indexes)


def _sig(expr) -> str:
    return getattr(expr, "name", None) if hasattr(expr, "table") else str(expr).replace(" ", "")


def _has_equivalent_index(table: Table, exprs, unique: bool) -> bool:
    want = tuple(_sig(e) for e in exprs)
    for idx in table.indexes:
        if tuple(_sig(e) for e in idx.expressions) == want and (idx.unique or not unique):
            return True
    return False


def _col_covered_by_index(table: Table, col_name: str) -> bool:
    col = table.c[col_name]
    if col.primary_key and list(table.primary_key.columns)[0] is col:
        return True
    for idx in table.indexes:
        cols = list(idx.expressions)
        if cols and getattr(cols[0], "name", None) == col_name and getattr(cols[0], "table", None) is table:
            return True
    for c in table.constraints:
        cols = list(getattr(c, "columns", []))
        if cols and cols[0] is col and c.__class__.__name__ in ("UniqueConstraint", "PrimaryKeyConstraint"):
            return True
    return False


def _expr(table: Table, item: str):
    if item.endswith(")") and "(" in item:  # e.g. lower(email)
        fn, arg = item[:-1].split("(", 1)
        if fn == "lower" and arg in table.c:
            return func.lower(table.c[arg])
        raise ValueError(item)
    return table.c[item]


def apply_policies(md: MetaData) -> None:
    # 1. UTC timestamps
    for table in md.tables.values():
        for col in table.columns:
            if isinstance(col.type, DateTime) and not isinstance(col.type, UTCDateTime):
                col.type = UTCDateTime()

    # 2. checks
    for tname, suffix, sql in CHECKS:
        t = _tbl(md, tname)
        if t is None:
            continue
        cname = f"ck_{tname}_{suffix}"
        if _has_constraint(t, cname):
            continue
        try:
            t.append_constraint(CheckConstraint(sql, name=cname))
        except Exception:  # pragma: no cover
            log.exception("could not add check %s", cname)

    # 3. foreign keys for bare id columns
    for tname, col, target, ondelete in FOREIGN_KEYS:
        t = _tbl(md, tname)
        ref_table = target.split(".")[0]
        if t is None or col not in t.c or ref_table not in md.tables:
            continue
        if any(col in [c.name for c in fk.columns] for fk in t.foreign_key_constraints):
            continue
        t.append_constraint(ForeignKeyConstraint(
            [col], [target], ondelete=ondelete, deferrable=True, initially="DEFERRED"))

    # 4. explicit ondelete everywhere
    for t in md.tables.values():
        for fkc in t.foreign_key_constraints:
            if fkc.ondelete is None:
                od = "SET NULL" if all(c.nullable for c in fkc.columns) else "RESTRICT"
                fkc.ondelete = od
                for fk in fkc.elements:
                    fk.ondelete = od

    # 5. indexes: all FK columns + searched columns
    for t in md.tables.values():
        for fkc in list(t.foreign_key_constraints):
            first = list(fkc.columns)[0]
            if not _col_covered_by_index(t, first.name):
                name = f"ix_{t.name}_{first.name}"
                if not _has_index(t, name):
                    Index(name, first)
    for tname, iname, cols, unique in INDEXES:
        t = _tbl(md, tname)
        if t is None or _has_index(t, iname):
            continue
        try:
            Index(iname, *[_expr(t, c) for c in cols], unique=unique)
            # Index(...) with Column objects auto-attaches to the table; with func expressions we
            # must attach explicitly:
        except Exception:
            continue
    # expression indexes built above reference columns, so they attach automatically.

    # 6. partial unique indexes
    for tname, iname, cols, where in PARTIAL_UNIQUE:
        t = _tbl(md, tname)
        if t is None or _has_index(t, iname) or any(c not in t.c for c in cols):
            continue
        Index(iname, *[t.c[c] for c in cols], unique=True,
              sqlite_where=text(where), postgresql_where=text(where))
