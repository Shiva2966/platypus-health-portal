"""QA-06: photo thumbnails (Pillow) for JPG/PNG; HEIC and PDF have none and the card falls back to an icon."""
import io
import uuid

import pytest

try:
    from test_documents_support import world, HEIC  # noqa: F401
except ImportError:
    from tests.test_documents_support import world, HEIC  # noqa: F401

from app.services import files

PIL = pytest.importorskip("PIL.Image")


def _image(fmt: str) -> bytes:
    im = PIL.new("RGB", (640, 480), (int(uuid.uuid4().int % 255), 120, 200))
    buf = io.BytesIO()
    im.save(buf, format=fmt)
    return buf.getvalue()


@pytest.mark.parametrize("fmt,filename", [("JPEG", "photo.jpg"), ("PNG", "scan.png")])
def test_thumbnail_for_photos(world, fmt, filename):
    d = world.upload(world.A, data=_image(fmt), name="Photo", filename=filename, category="other")
    assert d["has_thumbnail"] is True
    r = world.client(world.A).get(f"/api/documents/{d['id']}/thumbnail")
    assert r.status_code == 200 and r.headers["content-type"].startswith("image/png")
    with PIL.open(io.BytesIO(r.content)) as im:
        assert max(im.size) <= 256
    # other patients can't fetch it
    assert world.client(world.B).get(f"/api/documents/{d['id']}/thumbnail").status_code == 404


def test_heic_has_no_thumbnail_but_does_not_error(world):
    d = world.upload(world.A, data=HEIC + uuid.uuid4().bytes, name="iPhone photo", filename="p.heic", category="other")
    assert d["has_thumbnail"] is False
    assert world.client(world.A).get(f"/api/documents/{d['id']}/thumbnail").status_code == 404


def test_has_thumbnail_false_without_pillow(world, monkeypatch):
    monkeypatch.setattr(files, "thumbnails_available", lambda: False)
    d = world.upload(world.A, data=_image("PNG"), name="Scan", filename="s.png", category="other")
    assert d["has_thumbnail"] is False


def test_document_card_falls_back_to_file_icon():
    from pathlib import Path

    js = (Path(__file__).resolve().parent.parent / "app/static/js/docs_documents.js").read_text(encoding="utf-8")
    assert "function fileIcon" in js and "replaceWith(fileIcon(" in js
