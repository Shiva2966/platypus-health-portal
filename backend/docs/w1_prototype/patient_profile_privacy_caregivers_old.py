# ---------------- profile ----------------

@router.get("/profile")
def get_profile(p: Patient = Depends(current_patient)):
    return {"profile": patient_dict(p), "fields": PROFILE_FIELDS}


class ProfileIn(BaseModel):
    legal_name: str | None = None
    preferred_name: str | None = None
    dob: str | None = None
    address: str | None = None
    phone: str | None = None
    email: str | None = None
    pronouns: str | None = None


@router.put("/profile")
def put_profile(body: ProfileIn, p: Patient = Depends(current_patient), db: Session = Depends(get_db)):
    data, errs = {}, {}
    raw = body.model_dump()
    for f in PROFILE_FIELDS:
        try:
            data[f["name"]] = clean_value(f, raw.get(f["name"]))
        except ValueError as e:
            errs[f["name"]] = str(e)
    if data.get("dob") and data["dob"] > date.today().isoformat():
        errs["dob"] = "Date of birth can't be in the future."
    if data.get("email") and "email" not in errs:
        data["email"] = data["email"].lower()
        other = db.scalar(select(Patient).where(func.lower(Patient.email) == data["email"], Patient.id != p.id))
        if other:
            errs["email"] = "That email is already used by another account."
    if errs:
        raise FieldError(errs)
    identity_changed = (data["legal_name"], data["dob"]) != (p.legal_name, p.dob)
    for k, v in data.items():
        setattr(p, k, v)
    if identity_changed and p.verification_status == "verified":
        p.verification_status = "unverified"
        p.verified_note = "Identity details changed - needs to be checked again."
    log_event(db, actor_type="patient", actor_id=p.id, patient_id=p.id, action="profile_updated", resource_type="profile")
    db.commit()
    return patient_dict(p)


@router.post("/profile/request-verification")
def request_verification(p: Patient = Depends(current_patient), db: Session = Depends(get_db)):
    """Mock: marks identity as 'pending'. A clinic front desk would verify in person (staff side is W3)."""
    if p.verification_status == "verified":
        raise HTTPException(409, "Your identity is already verified.")
    p.verification_status = "pending"
    p.verified_note = "Bring a photo ID to your next visit so staff can verify it."
    db.commit()
    return patient_dict(p)


class AllergyStatusIn(BaseModel):
    status: str


@router.put("/allergy-status")
def set_allergy_status(body: AllergyStatusIn, p: Patient = Depends(current_patient), db: Session = Depends(get_db)):
    if body.status not in ("unknown", "no_known_allergies", "has_allergies"):
        raise FieldError({"status": "Invalid choice."})
    n = db.scalar(select(func.count()).select_from(Record).where(Record.patient_id == p.id, Record.category == "allergy"))
    if body.status == "no_known_allergies" and n:
        raise HTTPException(409, "You have allergies listed. Remove them first if you want to say 'No known allergies'.")
    if body.status == "has_allergies" and not n:
        raise HTTPException(409, "Add at least one allergy first, or choose 'No known allergies' or 'Unknown'.")
    p.allergy_status = body.status
    db.commit()
    return {"allergy_status": p.allergy_status}


# ---------------- privacy ----------------

def _export(p: Patient, db: Session) -> dict:
    recs = db.scalars(select(Record).where(Record.patient_id == p.id).order_by(Record.created_at)).all()
    out = {"exported_at": utcnow().isoformat(), "note": "Synthetic demo data. Passwords and session tokens are never exported.",
           "profile": patient_dict(p), "records": {}}
    for r in recs:
        out["records"].setdefault(r.category, []).append(
            {"id": r.id, **r.data, "source": r.source, "confirmed_by": r.confirmed_by, "verification": r.verification,
             "created_at": r.created_at.isoformat(), "updated_at": r.updated_at.isoformat()})
    out["appointments"] = [{"id": a.id, "status": a.status, "intake": a.intake, "scheduled_for": a.scheduled_for}
                           for a in db.scalars(select(Appointment).where(Appointment.patient_id == p.id))]
    out["correction_requests"] = [{"record_id": c.record_id, "message": c.message, "status": c.status}
                                  for c in db.scalars(select(CorrectionRequest).where(CorrectionRequest.patient_id == p.id))]
    out["audit_log"] = [{"ts": a.ts.isoformat(), "actor_type": a.actor_type, "action": a.action, "resource_type": a.resource_type}
                        for a in db.scalars(select(AuditLog).where(AuditLog.patient_id == p.id).order_by(AuditLog.ts))]
    try:  # W2's documents table (metadata only - files are downloaded from the Documents page)
        from app.models.documents import Document  # type: ignore
        out["documents"] = [{"id": d.id, "name": d.name, "description": d.description, "category": d.category,
                             "mime_type": d.mime_type, "size_bytes": d.size_bytes}
                            for d in db.scalars(select(Document).where(Document.patient_id == p.id, Document.deleted_at.is_(None)))]
    except Exception:
        out["documents"] = "unavailable"
    return out


@router.get("/privacy/export")
def export_my_data(p: Patient = Depends(current_patient), db: Session = Depends(get_db)):
    log_event(db, actor_type="patient", actor_id=p.id, patient_id=p.id, action="data_exported")
    db.commit()
    return Response(json.dumps(_export(p, db), indent=2, default=str), media_type="application/json",
                    headers={"Content-Disposition": 'attachment; filename="my-health-data.json"'})


@router.post("/privacy/deletion-request")
def deletion_request(p: Patient = Depends(current_patient), db: Session = Depends(get_db)):
    """Records the request only. Nothing is deleted automatically in this demo."""
    p.deletion_requested_at = utcnow()
    log_event(db, actor_type="patient", actor_id=p.id, patient_id=p.id, action="deletion_requested")
    db.commit()
    return {"deletion_requested_at": p.deletion_requested_at,
            "note": "Request recorded. In this demo no data is deleted automatically; a real service would review and confirm within a set time."}


@router.delete("/privacy/deletion-request")
def cancel_deletion_request(p: Patient = Depends(current_patient), db: Session = Depends(get_db)):
    p.deletion_requested_at = None
    db.commit()
    return {"deletion_requested_at": None}


# ---------------- caregivers (roadmap stub: records intent only) ----------------

class CaregiverIn(BaseModel):
    name: str = ""
    relationship: str = ""
    email: str | None = None
    scope: list[str] = []
    expires_at: str | None = None


CAREGIVER_SCOPES = ["appointments", "bills", "medications", "allergies", "results", "history", "insurance"]


def _cg(c: CaregiverGrant) -> dict:
    return {"id": c.id, "name": c.name, "relationship": c.relationship_, "email": c.email,
            "scope": json.loads(c.scope_json), "expires_at": c.expires_at, "revoked_at": c.revoked_at,
            "active": c.revoked_at is None and (not c.expires_at or c.expires_at >= date.today().isoformat())}


@router.get("/caregivers")
def list_caregivers(p: Patient = Depends(current_patient), db: Session = Depends(get_db)):
    rows = db.scalars(select(CaregiverGrant).where(CaregiverGrant.patient_id == p.id).order_by(CaregiverGrant.created_at.desc())).all()
    return {"items": [_cg(c) for c in rows], "scopes": CAREGIVER_SCOPES,
            "note": "Roadmap preview: this saves your wishes. Caregiver sign-in is not built yet, so nobody can actually use these grants."}


@router.post("/caregivers", status_code=201)
def add_caregiver(body: CaregiverIn, p: Patient = Depends(current_patient), db: Session = Depends(get_db)):
    errs = {}
    try:
        name = clean_value(F("name", "", "text", True), body.name)
        rel = clean_value(F("relationship", "", "text", True), body.relationship)
        email = clean_value(F("email", "", "email"), body.email)
        exp = clean_value(F("expires_at", "", "date", True), body.expires_at)
    except ValueError as e:
        raise FieldError({"name": str(e)})
    if exp < date.today().isoformat() or exp > (date.today() + timedelta(days=365)).isoformat():
        errs["expires_at"] = "Choose an expiry within the next year."
    scope = [s for s in body.scope if s in CAREGIVER_SCOPES]
    if not scope:
        errs["scope"] = "Choose at least one thing they can see."
    if errs:
        raise FieldError(errs)
    c = CaregiverGrant(patient_id=p.id, name=name, relationship_=rel, email=email, scope_json=json.dumps(scope), expires_at=exp)
    db.add(c)
    log_event(db, actor_type="patient", actor_id=p.id, patient_id=p.id, action="caregiver_grant_created", resource_type="caregiver")
    db.commit()
    return _cg(c)


@router.post("/caregivers/{cid}/revoke")
def revoke_caregiver(cid: str, p: Patient = Depends(current_patient), db: Session = Depends(get_db)):
    c = db.get(CaregiverGrant, cid)
    if not c or c.patient_id != p.id:
        raise HTTPException(404, "Not found.")
    c.revoked_at = c.revoked_at or utcnow()
    log_event(db, actor_type="patient", actor_id=p.id, patient_id=p.id, action="caregiver_grant_revoked", resource_type="caregiver", resource_id=c.id)
    db.commit()
    return _cg(c)
