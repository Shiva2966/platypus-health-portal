"""PRE-RELEASE ONLY: regenerate alembic revision 0001 from the current models.

While the schema is still changing during development and NO real database has been migrated yet,
it is simpler to keep a single initial revision than to pile up dozens of tiny ones.  Once any
persistent database exists (production/staging), STOP using this and add new revisions instead:

    .\\scripts\\migrate.ps1 new "add foo"

Usage:  python scripts/regen_initial.py
"""
import glob
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    versions = ROOT / "alembic" / "versions"
    for f in glob.glob(str(versions / "*_0001_*.py")):
        os.remove(f)
    # later revisions point at 0001; park them while generating the new base revision
    parked = []
    park = Path(tempfile.mkdtemp())
    for f in versions.glob("*.py"):
        shutil.move(str(f), park / f.name)
        parked.append(f.name)
    tmp = Path(tempfile.mkdtemp()) / "regen.db"
    env = {**os.environ, "HP_TEST_DATABASE_URL": f"sqlite:///{tmp}"}
    env.pop("AUTO_MIGRATE", None)
    try:
        r = subprocess.run([sys.executable, "-m", "alembic", "revision", "--autogenerate", "-m", "initial schema",
                            "--rev-id", "0001"], cwd=ROOT, env=env)
    finally:
        for name in parked:
            shutil.move(str(park / name), versions / name)
    for f in glob.glob(str(versions / "*_0001_*.py")):
        print("generated", Path(f).name)
    return r.returncode


if __name__ == "__main__":
    sys.exit(main())
