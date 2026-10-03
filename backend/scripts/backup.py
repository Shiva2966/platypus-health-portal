"""Back up the database (SQLite online backup or pg_dump), write a manifest, prune old backups.

    python scripts/backup.py                       # uses DATABASE_URL, writes to ./backups
    python scripts/backup.py --dest D:\\hp-backups --keep 30
    python scripts/backup.py --verify-restore      # also restores into a scratch DB and compares (recommended nightly)
    python scripts/backup.py --offsite "%USERPROFILE%\\OneDrive\\HealthPortalBackups"   # or BACKUP_OFFSITE_DIR in .env

Off-site copies are ALWAYS encrypted (AES-256-GCM, BACKUP_ENCRYPTION_KEY or DATA_ENCRYPTION_KEY) as
<name>.enc + the manifest (counts/hashes only, no patient data); the copy is decrypted once and checked against
the manifest's sha256 before old off-site copies are pruned (BACKUP_OFFSITE_KEEP, default = --keep).
Restore an .enc file with scripts/restore.py (it decrypts first). Without the key the .enc files are useless.

Exit code 0 = backup written (and verified if requested).
"""
import argparse
import hashlib
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import settings  # noqa: E402,F401  (loads .env: DATABASE_URL, BACKUP_*, keys)
from app.db import get_database_url, redacted  # noqa: E402
from app.db_ops import backup as bk  # noqa: E402


def copy_offsite(out: Path, offsite: Path, keep: int) -> Path:
    """Encrypt `out` into `offsite`, verify the encrypted copy decrypts to the manifest's sha256, prune."""
    from app import data_crypto as dc

    offsite.mkdir(parents=True, exist_ok=True)
    m = bk.read_manifest(out)
    enc = dc.encrypt_file(out, offsite / (out.name + dc.FILE_SUFFIX))
    shutil.copyfile(out.with_name(out.name + ".manifest.json"), offsite / (out.name + ".manifest.json"))
    with tempfile.TemporaryDirectory() as td:
        plain = dc.decrypt_file(enc, Path(td) / out.name)
        h = hashlib.sha256()
        with open(plain, "rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(chunk)
    if h.hexdigest() != m["file_sha256"]:
        enc.unlink(missing_ok=True)
        raise bk.BackupError("off-site encrypted copy failed its read-back check")
    encs = sorted(offsite.glob(f"healthportal_*{dc.FILE_SUFFIX}"), key=lambda p: p.name, reverse=True)
    for old in encs[max(1, keep):]:
        old.unlink(missing_ok=True)
        offsite.joinpath(old.name[: -len(dc.FILE_SUFFIX)] + ".manifest.json").unlink(missing_ok=True)
    return enc


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default=None, help="database URL (default: DATABASE_URL)")
    ap.add_argument("--dest", default=None, help="backup directory (default: BACKUP_DIR or ./backups)")
    ap.add_argument("--keep", type=int, default=None, help="keep newest N backups (default: BACKUP_KEEP or 14)")
    ap.add_argument("--max-age-days", type=int, default=None)
    ap.add_argument("--no-blob-hash", action="store_true", help="skip sha256 of every file (faster, weaker)")
    ap.add_argument("--verify-restore", action="store_true",
                    help="restore into a throw-away database and compare counts + blob hashes")
    ap.add_argument("--offsite", default=None, help="encrypted off-site copy dir (default: BACKUP_OFFSITE_DIR)")
    a = ap.parse_args()

    import os
    url = a.url or get_database_url()
    dest = Path(a.dest or os.environ.get("BACKUP_DIR") or Path(__file__).resolve().parent.parent / "backups")
    keep = a.keep if a.keep is not None else int(os.environ.get("BACKUP_KEEP", "14"))
    print(f"backing up {redacted(url)} -> {dest}")
    try:
        out = bk.create_backup(url, dest, keep=keep, max_age_days=a.max_age_days, hash_blobs=not a.no_blob_hash)
    except bk.BackupError as e:
        print("BACKUP FAILED:", e, file=sys.stderr)
        return 1
    m = bk.read_manifest(out)
    print(f"ok: {out.name}  {m['file_bytes']:,} bytes  rev={m['alembic_revision']}  "
          f"tables={len(m['row_counts'])} rows={sum(m['row_counts'].values())}")

    if a.verify_restore:
        from sqlalchemy.engine import make_url
        backend = make_url(url).get_backend_name()
        if backend == "sqlite":
            scratch = str(Path(tempfile.mkdtemp()) / "restore_check.db")
            res = bk.restore_backup(out, "sqlite:///" + scratch)
        else:
            res = None
            admin = make_url(url)
            from sqlalchemy import create_engine, text
            name = "hp_restore_check"
            eng = create_engine(admin.set(database="postgres"), isolation_level="AUTOCOMMIT")
            with eng.connect() as c:
                c.execute(text(f'DROP DATABASE IF EXISTS "{name}"'))
                c.execute(text(f'CREATE DATABASE "{name}"'))
            try:
                res = bk.restore_backup(out, admin.set(database=name).render_as_string(hide_password=False))
            finally:
                with eng.connect() as c:
                    c.execute(text(f'DROP DATABASE IF EXISTS "{name}"'))
                eng.dispose()
        print("restore verification:", "PASSED" if res["ok"] else "FAILED")
        for p in res["problems"]:
            print("  -", p)
        if not res["ok"]:
            return 2

    offsite = a.offsite or os.environ.get("BACKUP_OFFSITE_DIR", "").strip()
    if offsite:
        okeep = int(os.environ.get("BACKUP_OFFSITE_KEEP", "") or keep or 14)
        try:
            enc = copy_offsite(out, Path(os.path.expandvars(offsite)), okeep)
        except Exception as e:  # noqa: BLE001 - report, the local backup is still good
            print(f"OFF-SITE COPY FAILED: {type(e).__name__}: {e}", file=sys.stderr)
            return 4
        print(f"off-site (encrypted): {enc}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
