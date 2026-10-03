import zipfile, io

import pytest

try:
    from test_documents_support import *  # noqa: F401,F403
    from test_documents_support import world, uniq, PDF, PNG, JPG, HEIC, WEBP, make_docx  # noqa: F401
except ImportError:  # tests used as a package
    from tests.test_documents_support import *  # noqa: F401,F403
    from tests.test_documents_support import world, uniq, PDF, PNG, JPG, HEIC, WEBP, make_docx  # noqa: F401

from app.models.documents import Document
from app.services import files


def post(w, patient, data, filename="x.pdf", **form):
    form.setdefault("name", "My doc")
    return w.client(patient).post("/api/documents", data=form, files={"file": (filename, data, "application/octet-stream")})


def test_requires_login(world):
    from fastapi.testclient import TestClient
    from app.main import app
    c = TestClient(app)
    assert c.get("/api/documents").status_code == 401
    assert c.post("/api/documents", data={"name": "x"}, files={"file": ("a.pdf", PDF)}).status_code == 401


def test_upload_each_allowed_type(world):
    cases = [("a.pdf", uniq()), ("a.jpg", JPG + uniq()[:8]), ("a.png", PNG + b"1"), ("a.heic", HEIC + uniq()),
             ("a.webp", WEBP + uniq()[:6]), ("a.txt", b"hello world " + uniq()), ("a.docx", make_docx(uniq().hex()))]
    expect = ["application/pdf", "image/jpeg", "image/png", "image/heic", "image/webp", "text/plain",
              "application/vnd.openxmlformats-officedocument.wordprocessingml.document"]
    for (fn, data), mime in zip(cases, expect):
        r = post(world, world.A, data, filename=fn, name=f"doc {fn}")
        assert r.status_code == 201, (fn, r.text)
        assert r.json()["mime"] == mime
    assert world.client(world.A).get("/api/documents").json()["count"] == len(cases)


def test_defaults_private_and_not_shared(world):
    d = world.upload(world.A)
    assert d["is_private"] is True and d["shared_with"] == [] and d["source"] == "patient_uploaded"


def test_name_required_and_validated(world):
    c = world.client(world.A)
    r = c.post("/api/documents", files={"file": ("a.pdf", uniq())})
    assert r.status_code == 422
    assert post(world, world.A, uniq(), name="   ").status_code == 422
    assert post(world, world.A, uniq(), name="x" * 201).status_code == 422
    assert post(world, world.A, uniq(), description="d" * 2001).status_code == 422
    assert post(world, world.A, uniq(), category="bogus").status_code == 422


def test_magic_byte_spoof_rejected(world):
    # exe-ish bytes named .pdf
    assert post(world, world.A, b"MZ\x90\x00\x03\x00\x00\x00" + b"\x01" * 50, filename="a.pdf").status_code == 415
    # PNG bytes named .pdf -> mismatch
    assert post(world, world.A, PNG, filename="a.pdf").status_code == 415
    # HTML disguised as txt content is just text (served as text/plain, nosniff) but with .html ext rejected
    assert post(world, world.A, b"<html><script>alert(1)</script></html>", filename="a.html").status_code == 415
    # binary named .txt
    assert post(world, world.A, b"\x00\x01\x02\x03binary", filename="a.txt").status_code == 415
    # fake docx (zip without word/document.xml)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("evil.exe", "x")
    assert post(world, world.A, buf.getvalue(), filename="a.docx").status_code == 415
    # empty
    assert post(world, world.A, b"", filename="a.pdf").status_code == 400


def test_size_limit(world):
    big = b"%PDF-1.4\n" + b"0" * (files.MAX_UPLOAD_BYTES + 10)
    assert post(world, world.A, big).status_code == 413
    ok = b"%PDF-1.4\n" + uniq() + b"0" * (1024 * 1024)
    assert post(world, world.A, ok).status_code == 201


def test_duplicate_detection_per_patient(world):
    data = uniq()
    assert post(world, world.A, data).status_code == 201
    r = post(world, world.A, data, name="again")
    assert r.status_code == 409
    assert "already uploaded" in r.json()["detail"]["message"]
    # a different patient may upload the same bytes
    assert post(world, world.B, data).status_code == 201


def test_deleted_doc_allows_reupload(world):
    data = uniq()
    d = post(world, world.A, data).json()
    assert world.client(world.A).delete(f"/api/documents/{d['id']}").status_code == 200
    assert post(world, world.A, data).status_code == 201


def test_blob_stored_in_db_and_sha(world):
    data = uniq()
    d = post(world, world.A, data).json()
    from app.models.documents import DocumentBlob
    row = world.db.get(Document, d["id"])
    assert not hasattr(row, "data")  # metadata table holds no bytes
    assert bytes(world.db.get(DocumentBlob, d["id"]).data) == data
    assert row.sha256 == files.sha256_hex(data) and row.size_bytes == len(data)


def test_db_unique_index_blocks_active_duplicates(world):
    """The DB itself (not just the router check) refuses duplicate active uploads."""
    from sqlalchemy.exc import IntegrityError
    data = uniq()
    d = post(world, world.A, data).json()
    src = world.db.get(Document, d["id"])
    dup = Document(patient_id=world.A.id, name="dupe", category="other", mime_type=src.mime_type,
                   size_bytes=src.size_bytes, sha256=src.sha256)
    world.db.add(dup)
    with pytest.raises(IntegrityError):
        world.db.flush()
    world.db.rollback()


def test_serving_headers_and_content(world):
    data = uniq()
    d = world.upload(world.A, data, name='Evil"; name\r\n.pdf')
    c = world.client(world.A)
    r = c.get(f"/api/documents/{d['id']}/content")
    assert r.status_code == 200 and r.content == data
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["content-type"] == "application/pdf"
    cd = r.headers["content-disposition"]
    assert cd.startswith("inline") and "\r" not in cd and "\n" not in cd
    r = c.get(f"/api/documents/{d['id']}/download")
    assert r.headers["content-disposition"].startswith("attachment")
    # text is served as text/plain, never html
    t = world.upload(world.A, b"<script>alert(1)</script> " + uniq(), name="note", filename="n.txt")
    r = c.get(f"/api/documents/{t['id']}/content")
    assert r.headers["content-type"].startswith("text/plain")
    # HEIC/DOCX are never inline
    h = world.upload(world.A, HEIC + uniq(), name="heic", filename="a.heic")
    assert c.get(f"/api/documents/{h['id']}/content").headers["content-disposition"].startswith("attachment")


def test_patient_isolation(world):
    d = world.upload(world.A)
    b = world.client(world.B)
    for path in ("", "/content", "/download", "/thumbnail"):
        assert b.get(f"/api/documents/{d['id']}{path}").status_code == 404
    assert b.patch(f"/api/documents/{d['id']}", json={"name": "hax"}).status_code == 404
    assert b.delete(f"/api/documents/{d['id']}").status_code == 404
    assert b.put(f"/api/documents/{d['id']}/file", files={"file": ("a.pdf", uniq())}).status_code == 404
    assert b.get("/api/documents").json()["count"] == 0
    # existence of a random id looks identical
    assert b.get("/api/documents/00000000-0000-0000-0000-000000000000").status_code == 404
    # sharing a foreign doc is refused
    p = b.post("/api/sharing/grants", json={"provider_id": world.provs[0].id, "document_ids": [d["id"]]})
    assert p.status_code == 404
    assert world.client(world.A).get(f"/api/documents/{d['id']}").status_code == 200


def test_list_search_filter(world):
    c = world.client(world.A)
    world.upload(world.A, name="Blood panel", category="lab_result", description="Fasting cholesterol")
    world.upload(world.A, JPG + uniq()[:5], name="Knee X-ray", category="imaging", filename="k.jpg")
    world.upload(world.A, name="Insurance card", category="insurance", description="100% coverage_plan")
    assert c.get("/api/documents?q=blood").json()["count"] == 1
    assert c.get("/api/documents?q=cholesterol").json()["count"] == 1  # description search
    assert c.get("/api/documents?category=imaging").json()["count"] == 1
    assert c.get("/api/documents?mime=image").json()["count"] == 1
    assert c.get("/api/documents?mime=application/pdf").json()["count"] == 2
    assert c.get("/api/documents?q=%25").json()["count"] == 1  # wildcard escaped: only the literal %
    assert c.get("/api/documents?date_from=2000-01-01&date_to=2999-01-01").json()["count"] == 3
    assert c.get("/api/documents?date_to=2000-01-01").json()["count"] == 0
    assert c.get("/api/documents?date_from=notadate").status_code == 422
    assert c.get("/api/documents?category=zzz").status_code == 422
    assert c.get("/api/documents?sort=name").json()["documents"][0]["name"] == "Blood panel"
    assert "data" not in c.get("/api/documents").json()["documents"][0]


def test_edit_name_description_category_private(world):
    d = world.upload(world.A)
    c = world.client(world.A)
    r = c.patch(f"/api/documents/{d['id']}", json={"name": " New name ", "description": "desc",
                                                   "category": "billing", "is_private": False})
    assert r.status_code == 200
    j = r.json()
    assert (j["name"], j["description"], j["category"], j["is_private"]) == ("New name", "desc", "billing", False)
    assert c.patch(f"/api/documents/{d['id']}", json={"name": ""}).status_code == 422
    assert c.patch(f"/api/documents/{d['id']}", json={"category": "nope"}).status_code == 422


def test_replace_file_versionless(world):
    d = world.upload(world.A, name="Keep me", description="keep")
    c = world.client(world.A)
    new = PNG + b"replace"
    r = c.put(f"/api/documents/{d['id']}/file", files={"file": ("n.png", new)})
    assert r.status_code == 200 and r.json()["mime"] == "image/png" and r.json()["name"] == "Keep me"
    assert c.get(f"/api/documents/{d['id']}/content").content == new
    # replacing with another existing doc's bytes is a duplicate
    other = uniq()
    world.upload(world.A, other, name="other")
    assert c.put(f"/api/documents/{d['id']}/file", files={"file": ("n.pdf", other)}).status_code == 409
    assert c.put(f"/api/documents/{d['id']}/file", files={"file": ("n.pdf", b"MZ....")}).status_code == 415


def test_soft_delete(world):
    d = world.upload(world.A)
    c = world.client(world.A)
    assert c.delete(f"/api/documents/{d['id']}").status_code == 200
    assert c.get(f"/api/documents/{d['id']}").status_code == 404
    assert c.get(f"/api/documents/{d['id']}/content").status_code == 404
    assert c.get("/api/documents").json()["count"] == 0
    from app.models.documents import DocumentBlob
    row = world.db.get(Document, d["id"])
    world.db.refresh(row)
    assert row.deleted_at is not None and world.db.get(DocumentBlob, d["id"]) is not None  # soft: rows retained


def test_upload_side_effects(world):
    d = world.upload(world.A, name="Chest CT")
    assert any("Chest CT" in n.body for n in world.notifications("patient", world.A.id, "doc_uploaded"))
    assert any(a.resource_id == d["id"] for a in world.audit(world.A.id, "document_uploaded"))


def test_meta_endpoint(world):
    j = world.client(world.A).get("/api/documents/meta").json()
    assert j["max_bytes"] == 15 * 1024 * 1024 and "other" in j["categories"]


def test_files_unit_helpers():
    assert files.sniff_mime(b"%PDF-1.7") == "application/pdf"
    assert files.sniff_mime(b"GIF89a\x00\x01\x00\x00") is None  # GIF not allowed, has NULs
    name = files.safe_download_name("../../etc/passwd", "application/pdf")
    assert "/" not in name and "\\" not in name and name.endswith(".pdf")
    assert "\n" not in files.content_disposition("a\nb.pdf", False)
