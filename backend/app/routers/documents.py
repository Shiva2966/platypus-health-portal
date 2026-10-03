"""Patient document endpoints (/api/documents). Files are stored in the DB and only ever served
through these authorised routes."""
from __future__ import annotations

from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, Response, UploadFile
from sqlalchemy import select, or_, func
from sqlalchemy.orm import Session

from app.db import get_db
from sqlalchemy.exc import IntegrityError

from app.models.documents import CATEGORIES, Document, DocumentBlob, utcnow
from app.models.shared import Patient
from app.security import current_patient
from app.services import consent, files
from app.services.audit import log_event
from app.services.notifications import notify

router = APIRouter(prefix="/api/documents", tags=["documents"])

CHUNK = 1024 * 1024


def _own_doc(db: Session, patient: Patient, doc_id: str) -> Document:
    """Fetch a non-deleted doc owned by the patient, else 404 (never reveals other patients' ids)."""
    doc = db.scalar(select(Document).where(Document.id == doc_id, Document.patient_id == patient.id,
                                           Document.deleted_at.is_(None)))
    if doc is None:
        raise HTTPException(404, "Document not found.")
    return doc


def _blob(db: Session, doc_id: str) -> DocumentBlob:
    b = db.get(DocumentBlob, doc_id)
    if b is None:
        raise HTTPException(404, "Document not found.")
    return b


def _check_category(category: str | None) -> str:
    c = (category or "other").strip().lower()
    if c not in CATEGORIES:
        raise HTTPException(422, f"Unknown category. Choose one of: {', '.join(CATEGORIES)}")
    return c


def _check_name(name: str | None) -> str:
    n = files.clean_text(name, 10_000)
    if not n or len(n) > 200:
        raise HTTPException(422, "A name is required (1-200 characters).")
    return n


def _check_desc(desc: str | None) -> str | None:
    if desc is None:
        return None
    d = files.clean_text(desc, 10_000)
    if len(d) > 2000:
        raise HTTPException(422, "The description is too long (maximum 2000 characters).")
    return d or None


def _shares_for(db: Session, patient_id: str, rows) -> dict[str, list[dict]]:
    """document_id -> active shares affecting it (document- or category-scoped)."""
    grants = consent._active_grants(db, patient_id)
    names: dict[str, str] = {}
    out: dict[str, list[dict]] = {}
    for d in rows:
        for g in grants:
            if consent._grant_matches_doc(g, d):
                names.setdefault(g.provider_id, consent.provider_name(db, g.provider_id))
                out.setdefault(d.id, []).append({
                    "grant_id": g.id, "provider_id": g.provider_id, "provider_name": names[g.provider_id],
                    "scope_type": g.scope_type, "expires_at": consent.iso(g.expires_at)})
    return out


def _doc_dict(d, shares: list[dict] | None = None) -> dict:
    return {
        "id": d.id, "name": d.name, "description": d.description, "category": d.category,
        "mime": d.mime_type, "size": d.size_bytes, "sha256": d.sha256, "source": d.source,
        "is_private": bool(d.is_private), "created_at": consent.iso(d.created_at),
        "updated_at": consent.iso(d.updated_at), "previewable": d.mime_type in files.INLINE_SAFE,
        "has_thumbnail": d.mime_type in files.THUMBNAIL_MIMES and files.thumbnails_available(),
        "shared_with": shares or [],
    }


async def _read_limited(upload: UploadFile) -> bytes:
    buf = bytearray()
    while True:
        chunk = await upload.read(CHUNK)
        if not chunk:
            break
        buf += chunk
        if len(buf) > files.MAX_UPLOAD_BYTES:
            raise HTTPException(413, "File is too large (maximum is 15 MB).")
    return bytes(buf)


def _validate(data: bytes, filename: str | None) -> str:
    try:
        return files.validate_upload(data, filename)
    except files.FileValidationError as e:
        raise HTTPException(e.status, str(e))


def _duplicate(db: Session, patient_id: str, sha: str, exclude_id: str | None = None):
    q = select(Document.id, Document.name).where(
        Document.patient_id == patient_id, Document.sha256 == sha, Document.deleted_at.is_(None))
    if exclude_id:
        q = q.where(Document.id != exclude_id)
    return db.execute(q).first()


def _dup_error(dup) -> HTTPException:
    return HTTPException(409, detail={"message": f"You already uploaded this exact file as \"{dup[1]}\".",
                                      "duplicate_of": dup[0]})


@router.get("/meta")
def meta(patient: Patient = Depends(current_patient)):
    return {"categories": CATEGORIES, "max_bytes": files.MAX_UPLOAD_BYTES, "accept": files.ACCEPT_ATTR,
            "allowed": ["PDF", "JPG", "PNG", "HEIC/HEIF", "WEBP", "TXT", "DOCX"]}


@router.get("")
def list_documents(q: str | None = None, category: str | None = None, mime: str | None = None,
                   date_from: str | None = None, date_to: str | None = None, shared: bool | None = None,
                   sort: str = "newest", db: Session = Depends(get_db), patient: Patient = Depends(current_patient)):
    stmt = select(*consent.DOC_COLS).where(Document.patient_id == patient.id, Document.deleted_at.is_(None))
    if q:
        term = "%" + files.clean_text(q, 100).lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        stmt = stmt.where(or_(func.lower(Document.name).like(term, escape="\\"),
                              func.lower(func.coalesce(Document.description, "")).like(term, escape="\\")))
    if category:
        stmt = stmt.where(Document.category == _check_category(category))
    if mime:
        m = mime.strip().lower()
        stmt = stmt.where(Document.mime_type.like(m + "/%") if "/" not in m else Document.mime_type == m)
    try:
        if date_from:
            stmt = stmt.where(Document.created_at >= datetime.fromisoformat(date_from[:10]))
        if date_to:
            stmt = stmt.where(Document.created_at < datetime.fromisoformat(date_to[:10]) + timedelta(days=1))
    except ValueError:
        raise HTTPException(422, "Dates must look like YYYY-MM-DD.")
    order = {"oldest": Document.created_at.asc(), "name": func.lower(Document.name).asc(),
             "size": Document.size_bytes.desc()}.get(sort, Document.created_at.desc())
    rows = db.execute(stmt.order_by(order)).all()
    shares = _shares_for(db, patient.id, rows)
    items = [_doc_dict(r, shares.get(r.id)) for r in rows]
    if shared is not None:
        items = [i for i in items if bool(i["shared_with"]) == shared]
    return {"documents": items, "count": len(items)}


@router.post("", status_code=201)
async def upload_document(request: Request, file: UploadFile = File(...), name: str = Form(...),
                          description: str | None = Form(None), category: str = Form("other"),
                          is_private: bool = Form(True), db: Session = Depends(get_db),
                          patient: Patient = Depends(current_patient)):
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > files.MAX_UPLOAD_BYTES + 64 * 1024:
        raise HTTPException(413, "File is too large (maximum is 15 MB).")
    name = _check_name(name)
    desc = _check_desc(description)
    cat = _check_category(category)
    data = await _read_limited(file)
    mime = _validate(data, file.filename)
    sha = files.sha256_hex(data)
    dup = _duplicate(db, patient.id, sha)
    if dup:
        raise _dup_error(dup)
    doc = Document(patient_id=patient.id, name=name, description=desc, category=cat, mime_type=mime,
                   size_bytes=len(data), sha256=sha, source="patient_uploaded",
                   uploaded_by_type="patient", uploaded_by_id=patient.id, is_private=bool(is_private))
    db.add(doc)
    try:
        db.flush()  # unique (patient, sha256) index also protects against concurrent duplicate uploads
    except IntegrityError:
        db.rollback()
        dup = _duplicate(db, patient.id, sha)
        raise _dup_error(dup or ("", "an existing document"))
    db.add(DocumentBlob(document_id=doc.id, data=data))
    log_event(db, actor_type="patient", actor_id=patient.id, patient_id=patient.id, action="document_uploaded",
              resource_type="document", resource_id=doc.id, detail={"name": name, "mime": mime, "size": len(data)})
    notify(db, recipient_type="patient", recipient_id=patient.id, kind="doc_uploaded",
           title="Document uploaded", body=f"\"{name}\" was added to your documents. It is private until you share it.",
           link="#documents")
    db.commit()
    return _doc_dict(doc)


@router.get("/{doc_id}")
def get_document(doc_id: str, db: Session = Depends(get_db), patient: Patient = Depends(current_patient)):
    d = _own_doc(db, patient, doc_id)
    return _doc_dict(d, _shares_for(db, patient.id, [d]).get(d.id))


@router.patch("/{doc_id}")
def edit_document(doc_id: str, body: dict, db: Session = Depends(get_db), patient: Patient = Depends(current_patient)):
    d = _own_doc(db, patient, doc_id)
    changed = {}
    if "name" in body:
        d.name = _check_name(body["name"])
        changed["name"] = d.name
    if "description" in body:
        d.description = _check_desc(body["description"])
        changed["description"] = True
    if "category" in body:
        d.category = _check_category(body["category"])
        changed["category"] = d.category
    if "is_private" in body:
        d.is_private = bool(body["is_private"])
        changed["is_private"] = d.is_private
    if changed:
        d.updated_at = utcnow()
        log_event(db, actor_type="patient", actor_id=patient.id, patient_id=patient.id, action="document_edited",
                  resource_type="document", resource_id=d.id, detail=changed)
        db.commit()
    return _doc_dict(d, _shares_for(db, patient.id, [d]).get(d.id))


@router.put("/{doc_id}/file")
async def replace_file(doc_id: str, request: Request, file: UploadFile = File(...),
                       db: Session = Depends(get_db), patient: Patient = Depends(current_patient)):
    """Version-less replace: swaps the bytes, keeps name/description/category/grants."""
    d = _own_doc(db, patient, doc_id)
    data = await _read_limited(file)
    mime = _validate(data, file.filename)
    sha = files.sha256_hex(data)
    dup = _duplicate(db, patient.id, sha, exclude_id=d.id)
    if dup:
        raise _dup_error(dup)
    blob = _blob(db, d.id)
    blob.data = data
    d.mime_type, d.size_bytes, d.sha256, d.updated_at = mime, len(data), sha, utcnow()
    log_event(db, actor_type="patient", actor_id=patient.id, patient_id=patient.id, action="document_replaced",
              resource_type="document", resource_id=d.id, detail={"mime": mime, "size": len(data)})
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, detail={"message": "You already uploaded this exact file."})
    return _doc_dict(d, _shares_for(db, patient.id, [d]).get(d.id))


@router.delete("/{doc_id}")
def delete_document(doc_id: str, db: Session = Depends(get_db), patient: Patient = Depends(current_patient)):
    d = _own_doc(db, patient, doc_id)
    consent.revoke_document_grants(db, patient_id=patient.id, document_id=d.id)
    d = _own_doc(db, patient, doc_id)
    d.deleted_at = utcnow()
    log_event(db, actor_type="patient", actor_id=patient.id, patient_id=patient.id, action="document_deleted",
              resource_type="document", resource_id=d.id, detail={"name": d.name})
    db.commit()
    return {"ok": True, "note": "The document was removed. Providers that already viewed it may keep what they saw."}


def _serve(doc_id: str, db: Session, patient: Patient, *, download: bool) -> Response:
    d = _own_doc(db, patient, doc_id)
    blob = _blob(db, d.id)
    log_event(db, actor_type="patient", actor_id=patient.id, patient_id=patient.id,
              action="document_downloaded" if download else "document_previewed",
              resource_type="document", resource_id=d.id)
    db.commit()
    return Response(content=bytes(blob.data), headers=files.delivery_headers(d.name, d.mime_type, download=download))


@router.get("/{doc_id}/content")
def view_content(doc_id: str, db: Session = Depends(get_db), patient: Patient = Depends(current_patient)):
    return _serve(doc_id, db, patient, download=False)


@router.get("/{doc_id}/download")
def download_content(doc_id: str, db: Session = Depends(get_db), patient: Patient = Depends(current_patient)):
    return _serve(doc_id, db, patient, download=True)


@router.get("/{doc_id}/thumbnail")
def thumbnail(doc_id: str, db: Session = Depends(get_db), patient: Patient = Depends(current_patient)):
    d = _own_doc(db, patient, doc_id)
    png = files.make_thumbnail(bytes(_blob(db, d.id).data), d.mime_type)
    if png is None:
        raise HTTPException(404, "No thumbnail available.")
    h = files.delivery_headers(d.name, "image/png", download=False)
    h["Content-Disposition"] = "inline"
    return Response(content=png, headers=h)
