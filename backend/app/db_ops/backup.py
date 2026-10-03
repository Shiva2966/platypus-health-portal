"""Backup / restore / verification for SQLite and PostgreSQL.

* SQLite     : online backup via ``sqlite3.Connection.backup`` (consistent snapshot while the app runs).
* PostgreSQL : ``pg_dump --format=custom`` taken at an EXPORTED SNAPSHOT, so row counts / blob hashes in the
               manifest describe exactly what is inside the dump.

Every backup gets a ``<file>.manifest.json`` (row counts, blob digest, alembic revision, sha256 of the dump
file).  ``restore_backup`` restores into an EMPTY target and verifies counts + blob hashes against the
manifest.  Manifests contain no patient data, only counts and hashes.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine, make_url

from app.db_ops import integrity


class BackupError(RuntimeError):
    pass


def _now_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")[:-4] + "Z"


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def find_pg_tool(name: str) -> str:
    """Locate pg_dump / pg_restore: $PG_BIN dir, PATH, common install dirs, pgserver's bundled binaries."""
    exe = name + (".exe" if os.name == "nt" else "")
    cands = []
    if os.environ.get("PG_BIN"):
        cands.append(Path(os.environ["PG_BIN"]) / exe)
    which = shutil.which(name)
    if which:
        return which
    try:
        import pgserver

        cands.append(Path(pgserver.__file__).parent / "pginstall" / "bin" / exe)
    except Exception:
        pass
    for base in (r"C:\Program Files\PostgreSQL", "/usr/lib/postgresql"):
        p = Path(base)
        if p.exists():
            cands += sorted(p.glob(f"*/bin/{exe}"), reverse=True)
    for c in cands:
        if c.exists():
            return str(c)
    raise BackupError(f"{name} not found. Install PostgreSQL client tools or set PG_BIN to their directory.")


def _pg_env(url) -> dict:
    env = os.environ.copy()
    if url.password:
        env["PGPASSWORD"] = url.password
    return env


def _pg_args(url) -> list[str]:
    a = []
    if url.host:
        a += ["-h", url.host]
    if url.port:
        a += ["-p", str(url.port)]
    if url.username:
        a += ["-U", url.username]
    return a


def schema_revision(engine: Engine) -> str | None:
    try:
        if inspect(engine).has_table("alembic_version"):
            with engine.connect() as c:
                return c.execute(text("SELECT version_num FROM alembic_version")).scalar()
    except Exception:
        pass
    return None


# ----------------------------------------------------------------------------------- backup

def create_backup(url: str, dest_dir: str | Path, *, label: str = "healthportal", keep: int | None = 14,
                  max_age_days: int | None = None, hash_blobs: bool = True) -> Path:
    from app.db import normalize_url

    url = normalize_url(url)
    u = make_url(url)
    dest = Path(dest_dir)
    dest.mkdir(parents=True, exist_ok=True)
    stamp = _now_stamp()
    backend = u.get_backend_name()

    if backend == "sqlite":
        out = dest / f"{label}_{stamp}.sqlite3"
        tmp = out.with_suffix(".sqlite3.partial")
        if not Path(u.database).exists():
            raise BackupError(f"SQLite database not found: {u.database}")
        src = sqlite3.connect(u.database, timeout=60)
        try:
            dst = sqlite3.connect(tmp)
            try:
                src.backup(dst)  # online, consistent
                dst.execute("PRAGMA journal_mode=DELETE")  # backup = one self-contained file
            finally:
                dst.close()
        finally:
            src.close()
        os.replace(tmp, out)
        eng = create_engine(f"sqlite:///{out}")
        try:
            manifest = _manifest(eng, backend, hash_blobs)
            chk = sqlite3.connect(out)
            try:
                res = chk.execute("PRAGMA integrity_check").fetchone()[0]
            finally:
                chk.close()
            if res != "ok":
                raise BackupError(f"sqlite integrity_check on backup failed: {res}")
        finally:
            eng.dispose()
    elif backend == "postgresql":
        out = dest / f"{label}_{stamp}.pgdump"
        tmp = out.with_suffix(".pgdump.partial")
        eng = create_engine(url)
        try:
            with eng.connect().execution_options(isolation_level="REPEATABLE READ") as conn:
                snap = conn.execute(text("SELECT pg_export_snapshot()")).scalar()
                cmd = [find_pg_tool("pg_dump"), *_pg_args(u), "--format=custom", "--no-owner",
                       "--no-privileges", f"--snapshot={snap}", "--file", str(tmp), u.database]
                r = subprocess.run(cmd, env=_pg_env(u), capture_output=True, text=True)
                if r.returncode != 0:
                    tmp.unlink(missing_ok=True)
                    raise BackupError("pg_dump failed: " + r.stderr.strip()[-500:])
                manifest = _manifest(eng, backend, hash_blobs, conn=conn)  # same snapshot as the dump
                conn.rollback()
            os.replace(tmp, out)
        finally:
            eng.dispose()
    else:
        raise BackupError(f"unsupported database backend: {backend}")

    manifest.update({"file": out.name, "file_sha256": _sha256_file(out), "file_bytes": out.stat().st_size,
                     "created_at_utc": datetime.now(timezone.utc).isoformat()})
    out.with_name(out.name + ".manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True))
    if keep or max_age_days:
        prune_backups(dest, label, keep=keep, max_age_days=max_age_days)
    return out


def _manifest(engine: Engine, backend: str, hash_blobs: bool, conn=None) -> dict:
    bind = conn if conn is not None else engine
    rep = integrity.verify_blobs(bind) if hash_blobs else None
    m = {"engine": backend, "alembic_revision": schema_revision(engine),
         "row_counts": integrity.table_counts(bind), "format": 1}
    if rep is not None:
        m["blobs"] = {"checked": rep["checked"], "ok": rep["ok"], "total_bytes": rep["total_bytes"],
                      "digest": rep["digest"], "clean": integrity.blobs_clean(rep)}
    return m


def prune_backups(dest: Path, label: str, keep: int | None = 14, max_age_days: int | None = None) -> list[Path]:
    """Delete old backups (and their manifests). Keeps the newest ``keep``; also deletes > max_age_days old,
    but NEVER the newest backup."""
    files = sorted([p for p in dest.glob(f"{label}_*") if p.suffix in (".sqlite3", ".pgdump")
                    and not p.name.endswith(".manifest.json")], key=lambda p: p.name, reverse=True)
    doomed: list[Path] = []
    if keep:
        doomed += files[keep:]
    if max_age_days:
        cutoff = datetime.now(timezone.utc).timestamp() - max_age_days * 86400
        doomed += [p for p in files[1:] if p.stat().st_mtime < cutoff and p not in doomed]
    for p in doomed:
        p.unlink(missing_ok=True)
        p.with_name(p.name + ".manifest.json").unlink(missing_ok=True)
    return doomed


def latest_backup(dest: Path, label: str = "healthportal") -> Path | None:
    files = sorted([p for p in Path(dest).glob(f"{label}_*") if p.suffix in (".sqlite3", ".pgdump")],
                   key=lambda p: p.name, reverse=True)
    return files[0] if files else None


def read_manifest(backup: Path) -> dict:
    mp = backup.with_name(backup.name + ".manifest.json")
    if not mp.exists():
        raise BackupError(f"manifest missing for {backup.name}")
    return json.loads(mp.read_text())


# ----------------------------------------------------------------------------------- restore

def verify_backup_file(backup: Path) -> dict:
    """Check the dump file against the sha256 in its manifest (detects bit-rot / truncation)."""
    m = read_manifest(backup)
    actual = _sha256_file(backup)
    if actual != m["file_sha256"]:
        raise BackupError(f"backup file checksum mismatch for {backup.name}")
    return m


def restore_backup(backup: str | Path, target_url: str, *, force: bool = False) -> dict:
    """Restore into an EMPTY database (or replace with force=True), then verify against the manifest.

    Returns {'ok': bool, 'problems': [...], 'row_counts': {...}, 'blobs': {...}}.
    """
    from app.db import normalize_url

    backup = Path(backup)
    manifest = verify_backup_file(backup)
    target_url = normalize_url(target_url)
    u = make_url(target_url)
    backend = u.get_backend_name()
    if backend != manifest["engine"]:
        raise BackupError(f"backup is a {manifest['engine']} backup; target is {backend} "
                          "(cross-engine restore is not supported)")

    if backend == "sqlite":
        target = Path(u.database)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() and target.stat().st_size > 0:
            if not force:
                raise BackupError(f"target {target} exists; refusing to overwrite (use --force)")
            shutil.move(str(target), str(target) + f".replaced-{_now_stamp()}")
        for ext in ("-wal", "-shm"):
            Path(str(target) + ext).unlink(missing_ok=True)
        shutil.copyfile(backup, target)
    else:
        eng0 = create_engine(target_url)
        try:
            existing = [t for t in inspect(eng0).get_table_names()]
        finally:
            eng0.dispose()
        cmd = [find_pg_tool("pg_restore"), *_pg_args(u), "--no-owner", "--no-privileges",
               "--exit-on-error", "-d", u.database]
        if existing:
            if not force:
                raise BackupError("target database is not empty; refusing to restore (use --force)")
            cmd += ["--clean", "--if-exists"]
        r = subprocess.run(cmd + [str(backup)], env=_pg_env(u), capture_output=True, text=True)
        if r.returncode != 0:
            raise BackupError("pg_restore failed: " + r.stderr.strip()[-800:])

    eng = create_engine(target_url)
    try:
        return verify_against_manifest(eng, manifest)
    finally:
        eng.dispose()


def verify_against_manifest(engine: Engine, manifest: dict) -> dict:
    problems: list[str] = []
    counts = integrity.table_counts(engine)
    for t, n in manifest["row_counts"].items():
        if counts.get(t) != n:
            problems.append(f"row count mismatch in {t}: backup={n} restored={counts.get(t)}")
    for t in counts:
        if t not in manifest["row_counts"]:
            problems.append(f"unexpected extra table {t}")
    rep = integrity.verify_blobs(engine)
    mb = manifest.get("blobs")
    if mb:
        if rep["digest"] != mb["digest"]:
            problems.append("blob digest mismatch")
        if rep["checked"] != mb["checked"]:
            problems.append(f"blob count mismatch: {mb['checked']} vs {rep['checked']}")
    if not integrity.blobs_clean(rep):
        problems.append("blob integrity check failed: "
                        f"{len(rep['bad_hash'])} bad hash, {len(rep['missing_blob'])} missing, "
                        f"{len(rep['orphan_blobs'])} orphans")
    orphans = integrity.find_orphans(engine)
    if orphans:
        problems.append(f"orphan rows: {orphans}")
    if engine.dialect.name == "sqlite":
        with engine.connect() as c:
            res = c.execute(text("PRAGMA integrity_check")).scalar()
        if res != "ok":
            problems.append(f"sqlite integrity_check: {res}")
    rev = schema_revision(engine)
    if manifest.get("alembic_revision") != rev:
        problems.append(f"alembic revision differs: {manifest.get('alembic_revision')} vs {rev}")
    return {"ok": not problems, "problems": problems, "row_counts": counts, "blobs": rep}
