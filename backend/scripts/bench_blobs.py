"""Quick benchmark: store / list / read / verify 15 MB documents (the max upload size).

    python scripts/bench_blobs.py --url sqlite:///data/bench.db
    python scripts/bench_blobs.py --url postgresql+psycopg://...        (a THROW-AWAY database!)

Creates its own schema + rows in the target database; use a scratch database.
"""
import argparse
import hashlib
import os
import statistics
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select, text  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from app.db import Base, import_all_models, make_engine  # noqa: E402
from app.db_ops import integrity  # noqa: E402
from app.db_ops.triggers import ensure_db_objects  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True)
    ap.add_argument("--mb", type=int, default=15)
    ap.add_argument("-n", type=int, default=8, help="number of documents")
    ap.add_argument("--threads", type=int, default=4, help="concurrent uploaders for the parallel test")
    a = ap.parse_args()

    from app.models.documents import Document, DocumentBlob
    from app.models.shared import Patient

    eng = make_engine(a.url)
    import_all_models()
    Base.metadata.create_all(eng)
    ensure_db_objects(eng)
    S = sessionmaker(bind=eng, expire_on_commit=False)
    size = a.mb * 1024 * 1024
    payloads = [os.urandom(size) for _ in range(a.n)]
    with S() as s:
        pids = []
        for _ in range(a.threads):
            p = Patient(email=f"bench{uuid.uuid4().hex[:8]}@example.test", password_hash="x", legal_name="Bench")
            s.add(p)
            pids.append(p)
        s.commit()
        pids = [p.id for p in pids]

    def store(i: int):
        data = payloads[i]
        with S() as s:
            d = Document(patient_id=pids[i % len(pids)], name=f"bench{i}.pdf", mime_type="application/pdf",
                         size_bytes=len(data), sha256=hashlib.sha256(data).hexdigest())
            s.add(d)
            s.flush()
            s.add(DocumentBlob(document_id=d.id, data=data))
            s.commit()
            return d.id

    t = []
    ids = []
    for i in range(a.n // 2):  # sequential uploads
        t0 = time.perf_counter()
        ids.append(store(i))
        t.append(time.perf_counter() - t0)
    print(f"upload {a.mb} MB (sequential, n={len(t)}): median {statistics.median(t)*1000:.0f} ms, "
          f"{a.mb/statistics.median(t):.0f} MB/s")

    t0 = time.perf_counter()
    with ThreadPoolExecutor(a.threads) as ex:
        ids += list(ex.map(store, range(a.n // 2, a.n)))
    dt = time.perf_counter() - t0
    print(f"upload {a.mb} MB x{a.n - a.n // 2} ({a.threads} concurrent): {dt*1000:.0f} ms total, "
          f"{(a.n - a.n // 2) * a.mb / dt:.0f} MB/s aggregate")

    with S() as s:
        t0 = time.perf_counter()
        for _ in range(50):
            rows = s.execute(select(Document.id, Document.name, Document.size_bytes, Document.sha256)).all()
        lt = (time.perf_counter() - t0) / 50
        print(f"list {len(rows)} documents (metadata only): {lt*1000:.2f} ms/query  "
              f"(table holds {len(rows)*a.mb} MB of blobs - none are read)")

        t0 = time.perf_counter()
        blob = s.execute(text("SELECT data FROM document_blobs WHERE document_id = :i"), {"i": ids[0]}).scalar()
        print(f"read whole blob in one query: {(time.perf_counter()-t0)*1000:.0f} ms ({len(blob)/1e6:.1f} MB)")

    with eng.connect() as c:
        t0 = time.perf_counter()
        n = sum(len(ch) for ch in integrity.iter_blob_chunks(c, ids[1], 1024 * 1024))
        print(f"stream blob in 1 MiB substr() chunks: {(time.perf_counter()-t0)*1000:.0f} ms ({n/1e6:.1f} MB)")
        c.rollback()

    t0 = time.perf_counter()
    rep = integrity.verify_blobs(eng)
    print(f"verify_blobs (sha256 of {rep['checked']} files, {rep['total_bytes']/1e6:.0f} MB): "
          f"{(time.perf_counter()-t0)*1000:.0f} ms  clean={integrity.blobs_clean(rep)}")
    eng.dispose()
    return 0


if __name__ == "__main__":
    sys.exit(main())
