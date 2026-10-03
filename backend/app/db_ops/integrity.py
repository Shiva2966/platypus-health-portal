"""Integrity tooling: streamed blob reads, sha256 verification, orphan detection, row counts.

Plain SQL on purpose (works on any engine, independent of the ORM models, usable on a restored copy).
Blob reads are CHUNKED with substr(data, offset, n): the whole file is never required in Python memory
and list queries never touch ``document_blobs``.
"""
from __future__ import annotations

import hashlib
from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import inspect, text
from sqlalchemy.engine import Connection, Engine

CHUNK = 1024 * 1024  # 1 MiB


@contextmanager
def _conn(bind):
    """Accept an Engine (open/close a connection) or an existing Connection (use as is)."""
    if isinstance(bind, Connection):
        yield bind
    else:
        with bind.connect() as c:
            yield c
            c.rollback()


def blob_length(conn: Connection, document_id: str) -> int | None:
    row = conn.execute(text("SELECT length(data) FROM document_blobs WHERE document_id = :i"),
                       {"i": document_id}).first()
    return None if row is None else int(row[0] or 0)


def iter_blob_chunks(conn: Connection, document_id: str, chunk_size: int = CHUNK) -> Iterator[bytes]:
    """Yield the PLAINTEXT file bytes in chunks. Encrypted rows (app.data_crypto) are decrypted as a whole
    (AES-GCM authenticates the complete ciphertext); legacy plaintext rows are streamed."""
    from app.data_crypto import MAGIC, decrypt_blob

    head = conn.execute(text("SELECT substr(data, 1, 4) FROM document_blobs WHERE document_id = :i"),
                        {"i": document_id}).first()
    if head is not None and head[0] is not None and bytes(head[0]) == MAGIC:
        stored = conn.execute(text("SELECT data FROM document_blobs WHERE document_id = :i"),
                              {"i": document_id}).scalar()
        view = memoryview(decrypt_blob(bytes(stored)))
        for off in range(0, len(view), chunk_size):
            yield bytes(view[off:off + chunk_size])
        return
    yield from iter_stored_chunks(conn, document_id, chunk_size)


def iter_stored_chunks(conn: Connection, document_id: str, chunk_size: int = CHUNK) -> Iterator[bytes]:
    """Yield the bytes exactly as stored (ciphertext for encrypted rows), in chunks
    (portable: SQLite ``substr`` on BLOB / PostgreSQL ``substr`` on bytea)."""
    total = blob_length(conn, document_id)
    if total is None:
        raise LookupError("blob not found")
    if conn.dialect.name == "sqlite":
        # SQLite's substr() re-reads the whole value on every call (O(n^2)); one read + slicing is faster.
        data = conn.execute(text("SELECT data FROM document_blobs WHERE document_id = :i"),
                            {"i": document_id}).scalar()
        view = memoryview(bytes(data))
        for off in range(0, total, chunk_size):
            yield bytes(view[off:off + chunk_size])
        return
    off = 1  # substr is 1-based (PostgreSQL: only the requested slice leaves the server)
    stmt = text("SELECT substr(data, :o, :n) FROM document_blobs WHERE document_id = :i")
    while off <= total:
        chunk = conn.execute(stmt, {"o": off, "n": chunk_size, "i": document_id}).scalar()
        if not chunk:
            break
        yield bytes(chunk)
        off += chunk_size


def sha256_of_blob(conn: Connection, document_id: str) -> tuple[str, int]:
    h = hashlib.sha256()
    n = 0
    for chunk in iter_blob_chunks(conn, document_id):
        h.update(chunk)
        n += len(chunk)
    return h.hexdigest(), n


def verify_blobs(engine: Engine | Connection, *, include_deleted: bool = True, limit: int | None = None) -> dict:
    """Recompute sha256 + size of every stored file and compare with the metadata row.

    Returns {'checked': n, 'ok': n, 'bad_hash': [...], 'bad_size': [...], 'missing_blob': [...],
    'orphan_blobs': [...], 'total_bytes': n, 'digest': 'sha256 over (id:sha) pairs'}.
    """
    from app.data_crypto import MAGIC, DecryptionError

    out = {"checked": 0, "ok": 0, "bad_hash": [], "bad_size": [], "missing_blob": [], "orphan_blobs": [],
           "undecryptable": [], "encrypted": 0, "plaintext": 0,
           "total_bytes": 0, "digest": hashlib.sha256().hexdigest()}
    insp = inspect(engine)
    if not (insp.has_table("documents") and insp.has_table("document_blobs")):
        return out
    q = "SELECT id, sha256, size_bytes, deleted_at FROM documents"
    if not include_deleted:
        q += " WHERE deleted_at IS NULL"
    q += " ORDER BY id"
    if limit:
        q += f" LIMIT {int(limit)}"
    digest = hashlib.sha256()
    with _conn(engine) as conn:
        for doc_id, sha, size, _deleted in conn.execute(text(q)).all():
            out["checked"] += 1
            if blob_length(conn, doc_id) is None:
                out["missing_blob"].append(doc_id)
                continue
            head = conn.execute(text("SELECT substr(data, 1, 4) FROM document_blobs WHERE document_id = :i"),
                                {"i": doc_id}).scalar()
            out["encrypted" if head is not None and bytes(head) == MAGIC else "plaintext"] += 1
            try:
                actual, n = sha256_of_blob(conn, doc_id)
            except DecryptionError:
                out["undecryptable"].append(doc_id)
                continue
            out["total_bytes"] += n
            digest.update(f"{doc_id}:{actual}\n".encode())
            if actual != sha:
                out["bad_hash"].append(doc_id)
            elif n != size:
                out["bad_size"].append(doc_id)
            else:
                out["ok"] += 1
        out["orphan_blobs"] = [r[0] for r in conn.execute(text(
            "SELECT b.document_id FROM document_blobs b LEFT JOIN documents d ON d.id = b.document_id "
            "WHERE d.id IS NULL"))]
    out["digest"] = digest.hexdigest()
    return out


def blobs_clean(report: dict) -> bool:
    return not (report["bad_hash"] or report["bad_size"] or report["missing_blob"] or report["orphan_blobs"]
                or report.get("undecryptable"))


def table_counts(engine: Engine | Connection, exclude=("alembic_version",)) -> dict[str, int]:
    names = sorted(t for t in inspect(engine).get_table_names() if t not in exclude)
    with _conn(engine) as conn:
        return {t: int(conn.execute(text(f'SELECT count(*) FROM "{t}"')).scalar()) for t in names}


def find_orphans(engine: Engine) -> list[dict]:
    """Child rows whose parent row does not exist, for every foreign key actually present in the DB.

    (With FK enforcement on there should be none; this catches data written while enforcement was off,
    partial restores, and manual SQL.)"""
    insp = inspect(engine)
    problems = []
    with engine.connect() as conn:
        for table in sorted(insp.get_table_names()):
            for fk in insp.get_foreign_keys(table):
                cols, ref, rcols = fk["constrained_columns"], fk["referred_table"], fk["referred_columns"]
                if not cols or not ref:
                    continue
                join = " AND ".join(f'c."{a}" = p."{b}"' for a, b in zip(cols, rcols))
                notnull = " AND ".join(f'c."{a}" IS NOT NULL' for a in cols)
                sql = (f'SELECT count(*) FROM "{table}" c LEFT JOIN "{ref}" p ON {join} '
                       f'WHERE {notnull} AND p."{rcols[0]}" IS NULL')
                n = int(conn.execute(text(sql)).scalar())
                if n:
                    problems.append({"table": table, "columns": cols, "references": ref, "orphans": n})
        # polymorphic recipients (no FK possible): notifications -> patients/staff_users
        names = set(insp.get_table_names())
        for tbl in ("notifications", "notification_prefs"):
            if tbl not in names:
                continue
            for rtype, parent in (("patient", "patients"), ("staff", "staff_users")):
                if parent not in names:
                    continue
                n = int(conn.execute(text(
                    f'SELECT count(*) FROM "{tbl}" c LEFT JOIN "{parent}" p ON p.id = c.recipient_id '
                    f"WHERE c.recipient_type = '{rtype}' AND p.id IS NULL")).scalar())
                if n:
                    problems.append({"table": tbl, "columns": ["recipient_id"], "references": parent,
                                     "orphans": n, "note": f"recipient_type={rtype}"})
        conn.rollback()
    return problems
