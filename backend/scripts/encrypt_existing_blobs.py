"""Encrypt legacy plaintext document files in place (AES-256-GCM, app/data_crypto.py).

    .venv\\Scripts\\python.exe scripts\\backup.py --verify-restore        # FIRST: take a verified backup
    .venv\\Scripts\\python.exe scripts\\encrypt_existing_blobs.py --dry-run
    .venv\\Scripts\\python.exe scripts\\encrypt_existing_blobs.py
    .venv\\Scripts\\python.exe scripts\\encrypt_existing_blobs.py --rotate   # also re-encrypt rows made with an OLD key

Needs DATA_ENCRYPTION_KEY (in .env). Every row is written in its own transaction, read back and checked
(decrypted bytes == original bytes) before the next one. Safe to re-run; already-encrypted rows are skipped.
Refuses to run unless a backup younger than 24 h exists in BACKUP_DIR/./backups (override: --no-backup-check).
Prints document ids and counts only.
"""
import argparse
import hashlib
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import settings  # noqa: E402,F401  (loads .env)
from sqlalchemy import create_engine, inspect, text  # noqa: E402

from app import data_crypto as dc  # noqa: E402
from app.db import get_database_url, normalize_url, redacted  # noqa: E402
from app.db_ops import backup as bk  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default=None)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--rotate", action="store_true", help="re-encrypt rows whose key is not the current key")
    ap.add_argument("--no-backup-check", action="store_true")
    a = ap.parse_args()

    key, keys = dc.data_keys()
    if key is None:
        print("DATA_ENCRYPTION_KEY is not set (.env). Nothing done.", file=sys.stderr)
        return 2
    cur_id = dc.key_id(key)
    url = normalize_url(a.url) if a.url else get_database_url()
    print(f"database: {redacted(url)}")

    if not a.dry_run and not a.no_backup_check:
        dest = Path(os.environ.get("BACKUP_DIR") or ROOT / "backups")
        last = bk.latest_backup(dest)
        if last is None or time.time() - last.stat().st_mtime > 24 * 3600:
            print(f"No backup younger than 24 h in {dest}. Run scripts\\backup.py --verify-restore first "
                  "(or pass --no-backup-check).", file=sys.stderr)
            return 3
        print(f"latest backup: {last.name}")

    eng = create_engine(url)
    if not inspect(eng).has_table("document_blobs"):
        print("no document_blobs table; nothing to do")
        return 0
    stats = {"rows": 0, "already": 0, "encrypted": 0, "rotated": 0, "sha_mismatch": 0, "failed": 0}
    with eng.connect() as c:
        rows = c.execute(text("SELECT b.document_id, d.sha256 FROM document_blobs b "
                              "LEFT JOIN documents d ON d.id = b.document_id ORDER BY b.document_id")).all()
    for doc_id, sha in rows:
        stats["rows"] += 1
        with eng.begin() as c:
            stored = bytes(c.execute(text("SELECT data FROM document_blobs WHERE document_id = :i"),
                                     {"i": doc_id}).scalar())
            rotate = False
            if dc.is_encrypted(stored):
                if not (a.rotate and stored[5:9] != cur_id):
                    stats["already"] += 1
                    continue
                rotate = True
            try:
                plain = dc.decrypt_blob(stored, keys)
            except dc.DecryptionError as e:
                print(f"  FAILED {doc_id}: {e}")
                stats["failed"] += 1
                continue
            if sha and hashlib.sha256(plain).hexdigest() != sha:
                stats["sha_mismatch"] += 1
                print(f"  note: {doc_id} plaintext sha256 differs from metadata (pre-existing; bytes kept as-is)")
            if a.dry_run:
                stats["rotated" if rotate else "encrypted"] += 1
                continue
            enc = dc.encrypt_blob(plain, key)
            c.execute(text("UPDATE document_blobs SET data = :d WHERE document_id = :i"), {"d": enc, "i": doc_id})
            back = bytes(c.execute(text("SELECT data FROM document_blobs WHERE document_id = :i"),
                                   {"i": doc_id}).scalar())
            if dc.decrypt_blob(back, keys) != plain:
                raise SystemExit(f"read-back check failed for {doc_id}; transaction rolled back")
            stats["rotated" if rotate else "encrypted"] += 1
    eng.dispose()
    print(("DRY RUN - would change: " if a.dry_run else "done: ") + ", ".join(f"{k}={v}" for k, v in stats.items()))
    return 1 if stats["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
