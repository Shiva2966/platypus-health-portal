"""Embedded PostgreSQL 16 for local development / testing WITHOUT Docker (uses the `pgserver` pip package).

    python scripts/pg_dev.py start          # start (or reuse) the server, print DATABASE_URL
    python scripts/pg_dev.py url            # print URL of a running server
    python scripts/pg_dev.py newdb [name]   # create a fresh empty database, print its URL
    python scripts/pg_dev.py stop

Data lives in .pgdata/ (git-ignored).  This is a DEV/TEST convenience only - production uses the
postgres:16 container in docker-compose.yml or a managed PostgreSQL.
"""
import os
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PGDATA = Path(os.environ.get("HP_PGDATA", ROOT / ".pgdata"))


def _server(stop_on_exit: bool = False):
    import pgserver

    PGDATA.mkdir(parents=True, exist_ok=True)
    return pgserver.get_server(PGDATA, cleanup_mode="stop" if stop_on_exit else None)


def to_psycopg(uri: str, dbname: str | None = None) -> str:
    u = uri.replace("postgresql://", "postgresql+psycopg://", 1)
    if dbname:
        u = u.rsplit("/", 1)[0] + "/" + dbname
    return u


def main(argv: list[str]) -> int:
    cmd = argv[1] if len(argv) > 1 else "start"
    if cmd in ("start", "url"):
        print(to_psycopg(_server().get_uri()))
    elif cmd == "newdb":
        srv = _server()
        name = argv[2] if len(argv) > 2 else "hp_" + uuid.uuid4().hex[:10]
        srv.psql(f'DROP DATABASE IF EXISTS "{name}"')
        srv.psql(f'CREATE DATABASE "{name}"')
        print(to_psycopg(srv.get_uri(), name))
    elif cmd == "stop":
        _server().cleanup()
        print("stopped")
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
