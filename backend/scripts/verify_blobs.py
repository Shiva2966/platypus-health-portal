"""Integrity check: recompute sha256 + size of every stored document and compare with its metadata row;
report missing blobs, orphan blobs and orphan rows (FK violations).

    python scripts/verify_blobs.py                 # DATABASE_URL
    python scripts/verify_blobs.py --url sqlite:///data/app.db --json

Exit code 0 = everything consistent, 1 = problems found.  Safe to run against a live database (read-only).
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import settings  # noqa: E402,F401  (loads .env: DATABASE_URL, DATA_ENCRYPTION_KEY)
from sqlalchemy import create_engine  # noqa: E402

from app.db import get_database_url, normalize_url, redacted  # noqa: E402
from app.db_ops import integrity  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default=None)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--skip-deleted", action="store_true", help="ignore soft-deleted documents")
    a = ap.parse_args()
    url = normalize_url(a.url) if a.url else get_database_url()
    eng = create_engine(url)
    rep = integrity.verify_blobs(eng, include_deleted=not a.skip_deleted)
    orphans = integrity.find_orphans(eng)
    clean = integrity.blobs_clean(rep) and not orphans
    if a.json:
        print(json.dumps({"blobs": rep, "orphans": orphans, "clean": clean}, indent=2))
    else:
        print(f"database: {redacted(url)}")
        print(f"documents checked: {rep['checked']}  ok: {rep['ok']}  bytes: {rep['total_bytes']:,}")
        print(f"at rest: {rep['encrypted']} encrypted (AES-256-GCM), {rep['plaintext']} plaintext"
              + ("  -> run scripts/encrypt_existing_blobs.py" if rep['plaintext'] else ""))
        for k, label in (("bad_hash", "SHA-256 MISMATCH"), ("bad_size", "SIZE MISMATCH"),
                         ("missing_blob", "MISSING BLOB"), ("orphan_blobs", "ORPHAN BLOB"),
                         ("undecryptable", "CANNOT DECRYPT (wrong/missing DATA_ENCRYPTION_KEY or tampered)")):
            for i in rep[k]:
                print(f"  {label}: {i}")
        for o in orphans:
            print(f"  ORPHAN ROWS: {o}")
        print("RESULT:", "CLEAN" if clean else "PROBLEMS FOUND")
    return 0 if clean else 1


if __name__ == "__main__":
    sys.exit(main())
