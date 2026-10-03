"""Restore a backup into an EMPTY database and verify it against the backup manifest.

    python scripts/restore.py backups/healthportal_20261002T120000Z.sqlite3 --target sqlite:///data/restored.db
    python scripts/restore.py backups/healthportal_...pgdump --target postgresql+psycopg://user:pw@host/dbname
    python scripts/restore.py --latest --target ...        # newest backup in ./backups
    python scripts/restore.py X:\\offsite\\healthportal_...sqlite3.enc --target sqlite:///data/restored.db
                                                           # encrypted off-site copy (needs the key in .env)

Refuses to touch a non-empty target unless --force. Stop the app before restoring over a live database.
Exit code 0 = restored AND verified (row counts, blob sha256, orphans, integrity check).
"""
import argparse
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import data_crypto as dc  # noqa: E402
from app import settings  # noqa: E402,F401  (loads .env: keys, BACKUP_DIR)
from app.db import redacted  # noqa: E402
from app.db_ops import backup as bk  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("backup", nargs="?")
    ap.add_argument("--latest", action="store_true")
    ap.add_argument("--target", required=True, help="target database URL (must be empty)")
    ap.add_argument("--force", action="store_true", help="replace a non-empty target")
    ap.add_argument("--dir", default=os.environ.get("BACKUP_DIR") or str(Path(__file__).resolve().parent.parent / "backups"))
    a = ap.parse_args()
    path = bk.latest_backup(Path(a.dir)) if a.latest else (Path(a.backup) if a.backup else None)
    if a.latest and path is None:  # an off-site folder holds only encrypted copies
        encs = sorted(Path(a.dir).glob("healthportal_*" + dc.FILE_SUFFIX), key=lambda p: p.name, reverse=True)
        path = encs[0] if encs else None
    if not path or not path.exists():
        print("backup file not found", file=sys.stderr)
        return 1
    print(f"restoring {path.name} -> {redacted(a.target)}")
    with tempfile.TemporaryDirectory() as td:
        if path.name.endswith(dc.FILE_SUFFIX):
            plain_name = path.name[: -len(dc.FILE_SUFFIX)]
            try:
                plain = dc.decrypt_file(path, Path(td) / plain_name)
            except (dc.DecryptionError, dc.KeyMaterialError) as e:
                print("DECRYPT FAILED:", e, file=sys.stderr)
                return 1
            man = path.with_name(plain_name + ".manifest.json")
            if man.exists():
                shutil.copyfile(man, Path(td) / man.name)
            print(f"decrypted {path.name} (AES-256-GCM)")
            path = plain
        try:
            res = bk.restore_backup(path, a.target, force=a.force)
        except bk.BackupError as e:
            print("RESTORE FAILED:", e, file=sys.stderr)
            return 1
    print("verification:", "PASSED" if res["ok"] else "FAILED")
    for p in res["problems"]:
        print("  -", p)
    print(f"rows restored: {sum(res['row_counts'].values())} in {len(res['row_counts'])} tables; "
          f"blobs verified: {res['blobs']['ok']}/{res['blobs']['checked']}")
    return 0 if res["ok"] else 2


if __name__ == "__main__":
    sys.exit(main())
