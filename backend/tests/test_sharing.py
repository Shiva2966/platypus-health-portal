from datetime import timedelta

import pytest

try:
    from test_documents_support import world, uniq, PNG  # noqa: F401
except ImportError:
    from tests.test_documents_support import world, uniq, PNG  # noqa: F401

from app.models.documents import AccessRequest, ShareGrant, ShareToken
from app.models.shared import utcnow
from app.services import consent


def share(w, patient, staffprov, doc_ids=None, cats=None, **extra):
    r = w.client(patient).post("/api/sharing/grants", json={
        "provider_id": staffprov.id, "document_ids": doc_ids or [], "categories": cats or [], **extra})
    return r


def test_nothing_shared_by_default(world):
    world.upload(world.A)
    assert world.visible(world.staff[0], world.A) == []
    assert consent.active_grants_for_patient(world.db, world.A.id) == []


def test_staff_without_grant_gets_nothing(world):
    d = world.upload(world.A)
    with pytest.raises(PermissionError):
        world.view(world.staff[0], world.A, d["id"])
    # denied attempt is audited but patient is NOT told "viewed"
    assert world.audit(world.A.id, "document_access_denied")
    assert not world.notifications("patient", world.A.id, "document_viewed")


def test_unlinked_staff_sees_nothing(world):
    d = world.upload(world.A)
    share(world, world.A, world.provs[0], [d["id"]])

    class Ghost:
        id = "00000000-0000-0000-0000-000000000000"
        role = "admin"
    assert world.visible(Ghost, world.A) == []
    with pytest.raises(PermissionError):
        world.view(Ghost, world.A, d["id"])


def test_document_scope_grant(world):
    d1 = world.upload(world.A, name="one")
    d2 = world.upload(world.A, name="two")
    r = share(world, world.A, world.provs[0], [d1["id"]], purpose="Referral", expires_in_days=3)
    assert r.status_code == 201
    vis = world.visible(world.staff[0], world.A)
    assert [m["id"] for m in vis] == [d1["id"]]
    m = vis[0]
    for k in ("id", "name", "description", "category", "mime", "size", "source", "uploaded_at", "grant_expires_at"):
        assert k in m
    meta, data = world.view(world.staff[0], world.A, d1["id"])
    assert meta["name"] == "one" and data.startswith(b"%PDF")
    with pytest.raises(PermissionError):
        world.view(world.staff[0], world.A, d2["id"])
    # other provider's staff gets nothing
    assert world.visible(world.staff[1], world.A) == []
    with pytest.raises(PermissionError):
        world.view(world.staff[1], world.A, d1["id"])


def test_grant_for_one_patient_does_not_cover_another(world):
    dA = world.upload(world.A)
    dB = world.upload(world.B)
    share(world, world.A, world.provs[0], [dA["id"]])
    with pytest.raises(PermissionError):
        world.view(world.staff[0], world.B, dB["id"])
    # patient id mismatch with a doc the grant does cover
    with pytest.raises(PermissionError):
        world.view(world.staff[0], world.B, dA["id"])


def test_category_scope_and_private_flag(world):
    lab_priv = world.upload(world.A, name="lab private", category="lab_result", is_private="true")
    lab_open = world.upload(world.A, name="lab open", category="lab_result", is_private="false")
    other = world.upload(world.A, name="scan", category="imaging")
    pv = world.client(world.A).post("/api/sharing/preview", json={"categories": ["lab_result"]}).json()
    assert [d["name"] for d in pv["documents"]] == ["lab open"] and pv["excluded_private_count"] == 1
    r = share(world, world.A, world.provs[0], cats=["lab_result"])
    assert r.status_code == 201
    ids = {m["id"] for m in world.visible(world.staff[0], world.A)}
    assert ids == {lab_open["id"]}  # private excluded from category grants, other category excluded
    # include_private opt-in
    share(world, world.A, world.provs[0], cats=["lab_result"], include_private=True)
    ids = {m["id"] for m in world.visible(world.staff[0], world.A)}
    assert ids == {lab_open["id"], lab_priv["id"]} and other["id"] not in ids
    # new doc in category is covered automatically
    new = world.upload(world.A, name="lab new", category="lab_result", is_private="false")
    assert new["id"] in {m["id"] for m in world.visible(world.staff[0], world.A)}


def test_share_validation(world):
    d = world.upload(world.A)
    c = world.client(world.A)
    assert share(world, world.A, world.provs[0]).status_code == 422
    assert c.post("/api/sharing/grants", json={"provider_id": "nope", "document_ids": [d["id"]]}).status_code == 404
    assert share(world, world.A, world.provs[0], cats=["bogus"]).status_code == 422
    assert share(world, world.A, world.provs[0], [d["id"]], expires_in_days=0).status_code == 422
    assert share(world, world.A, world.provs[0], [d["id"]], expires_in_days=9999).status_code == 422
    assert share(world, world.A, world.provs[0], [d["id"]], expires_at="2001-01-01T00:00:00").status_code == 422


def test_share_notifies_provider_staff_and_audits(world):
    d = world.upload(world.A, name="MRI")
    share(world, world.A, world.provs[0], [d["id"]])
    n = world.notifications("staff", world.staff[0].id, "document_shared")
    assert n and "MRI" in n[0].body
    assert not world.notifications("staff", world.staff[1].id, "document_shared")
    assert world.audit(world.A.id, "share_granted")


def test_expiry_blocks_access(world):
    d = world.upload(world.A)
    share(world, world.A, world.provs[0], [d["id"]])
    assert world.view(world.staff[0], world.A, d["id"])
    g = world.db.query(ShareGrant).filter_by(patient_id=world.A.id).one()
    g.expires_at = utcnow() - timedelta(minutes=1)
    world.db.commit()
    assert world.visible(world.staff[0], world.A) == []
    with pytest.raises(PermissionError):
        world.view(world.staff[0], world.A, d["id"])
    assert consent.active_grants_for_patient(world.db, world.A.id) == []


def test_revocation_blocks_future_access_and_notifies(world):
    d = world.upload(world.A, name="ECG")
    share(world, world.A, world.provs[0], [d["id"]])
    world.view(world.staff[0], world.A, d["id"])
    c = world.client(world.A)
    grants = c.get("/api/sharing/grants").json()["grants"]
    assert len(grants) == 1 and grants[0]["document_name"] == "ECG"
    r = c.delete(f"/api/sharing/grants/{grants[0]['id']}")
    assert r.status_code == 200 and "cannot be taken back" in r.json()["retention_note"]
    with pytest.raises(PermissionError):
        world.view(world.staff[0], world.A, d["id"])
    assert world.visible(world.staff[0], world.A) == []
    assert c.get("/api/sharing/grants").json()["grants"] == []
    assert world.notifications("staff", world.staff[0].id, "access_revoked")
    assert world.audit(world.A.id, "share_revoked")
    # idempotent + other patients can't revoke
    assert c.delete(f"/api/sharing/grants/{grants[0]['id']}").status_code == 200
    assert world.client(world.B).delete(f"/api/sharing/grants/{grants[0]['id']}").status_code == 404


def test_staff_view_audits_and_notifies_patient(world):
    d = world.upload(world.A, name="Discharge summary")
    share(world, world.A, world.provs[0], [d["id"]])
    world.view(world.staff[0], world.A, d["id"])
    ev = world.audit(world.A.id, "document_viewed")
    assert len(ev) == 1 and ev[0].actor_type == "staff" and ev[0].actor_id == world.staff[0].id
    assert ev[0].resource_id == d["id"]
    n = world.notifications("patient", world.A.id, "document_viewed")
    assert n and "viewed Discharge summary" in n[0].title and world.provs[0].name in n[0].title
    hist = world.client(world.A).get("/api/sharing/history").json()["history"]
    assert any(h["action"] == "document_viewed" and h["document"] == "Discharge summary" for h in hist)


def test_deleting_document_revokes_its_grants(world):
    d = world.upload(world.A)
    share(world, world.A, world.provs[0], [d["id"]])
    world.client(world.A).delete(f"/api/documents/{d['id']}")
    assert world.visible(world.staff[0], world.A) == []
    assert world.notifications("staff", world.staff[0].id, "access_revoked")


def test_documents_list_shows_shares_and_unshare(world):
    d = world.upload(world.A)
    share(world, world.A, world.provs[0], [d["id"]])
    c = world.client(world.A)
    listed = c.get("/api/documents").json()["documents"][0]
    assert listed["shared_with"][0]["provider_name"] == world.provs[0].name
    assert c.get("/api/documents?shared=true").json()["count"] == 1
    assert c.get("/api/documents?shared=false").json()["count"] == 0
    c.delete(f"/api/sharing/grants/{listed['shared_with'][0]['grant_id']}")
    assert c.get("/api/documents").json()["documents"][0]["shared_with"] == []


# ------------------------------------------------------------------ tokens

def make_token(w, patient, **body):
    r = w.client(patient).post("/api/sharing/tokens", json=body)
    assert r.status_code == 201, r.text
    return r.json()


def test_token_hashed_at_rest_and_opaque(world):
    d = world.upload(world.A, name="Passport scan")
    t = make_token(world, world.A, document_ids=[d["id"]])
    raw = t["token"]
    assert len(raw) >= 22  # 128+ bits base64url
    row = world.db.get(ShareToken, t["id"])
    assert row.token_hash != raw and raw not in row.token_hash and len(row.token_hash) == 64
    # nothing in the DB row contains the raw token or any patient PII
    blob = " ".join(str(getattr(row, c.name)) for c in row.__table__.columns)
    assert raw not in blob and world.A.legal_name not in raw and world.A.id not in raw
    assert t["qr_svg"].lstrip().startswith("<?xml") or "<svg" in t["qr_svg"]
    listing = world.client(world.A).get("/api/sharing/tokens").json()["tokens"]
    assert listing and "token" not in listing[0] and raw not in str(listing)


def test_token_redeem_creates_scoped_grant(world):
    d1 = world.upload(world.A, name="one")
    world.upload(world.A, name="two")
    t = make_token(world, world.A, document_ids=[d1["id"]], grant_days=2, purpose="Walk-in")
    s = consent.redeem_share_token(world.db, token=t["token"], staff_id=world.staff[0].id)
    assert s["patient_id"] == world.A.id and s["provider_id"] == world.provs[0].id
    assert [m["id"] for m in world.visible(world.staff[0], world.A)] == [d1["id"]]
    assert world.notifications("patient", world.A.id, "share_redeemed")
    assert world.audit(world.A.id, "share_token_redeemed")
    assert world.visible(world.staff[1], world.A) == []


def test_token_single_use(world):
    d = world.upload(world.A)
    t = make_token(world, world.A, document_ids=[d["id"]])
    consent.redeem_share_token(world.db, token=t["token"], staff_id=world.staff[0].id)
    with pytest.raises(consent.InvalidToken):
        consent.redeem_share_token(world.db, token=t["token"], staff_id=world.staff[1].id)


def test_token_multi_use_limit(world):
    d = world.upload(world.A)
    t = make_token(world, world.A, document_ids=[d["id"]], max_uses=2)
    consent.redeem_share_token(world.db, token=t["token"], staff_id=world.staff[0].id)
    consent.redeem_share_token(world.db, token=t["token"], staff_id=world.staff[1].id)
    with pytest.raises(consent.InvalidToken):
        consent.redeem_share_token(world.db, token=t["token"], staff_id=world.staff[0].id)
    row = world.db.get(ShareToken, t["id"])
    world.db.refresh(row)
    assert row.use_count == 2


def test_token_expiry_and_revoke(world):
    d = world.upload(world.A)
    t = make_token(world, world.A, document_ids=[d["id"]])
    row = world.db.get(ShareToken, t["id"])
    row.expires_at = utcnow() - timedelta(seconds=1)
    world.db.commit()
    with pytest.raises(consent.InvalidToken):
        consent.redeem_share_token(world.db, token=t["token"], staff_id=world.staff[0].id)
    t2 = make_token(world, world.A, document_ids=[d["id"]])
    assert world.client(world.B).delete(f"/api/sharing/tokens/{t2['id']}").status_code == 404
    assert world.client(world.A).delete(f"/api/sharing/tokens/{t2['id']}").status_code == 200
    with pytest.raises(consent.InvalidToken):
        consent.redeem_share_token(world.db, token=t2["token"], staff_id=world.staff[0].id)


def test_token_bruteforce_limited(world):
    consent.reset_token_attempts()
    for _ in range(consent.TOKEN_FAIL_LIMIT):
        with pytest.raises(consent.InvalidToken):
            consent.redeem_share_token(world.db, token="x" * 30, staff_id=world.staff[0].id)
    d = world.upload(world.A)
    t = make_token(world, world.A, document_ids=[d["id"]])
    with pytest.raises(consent.TooManyAttempts):  # even the right token is refused while locked out
        consent.redeem_share_token(world.db, token=t["token"], staff_id=world.staff[0].id)
    # another staff member is unaffected
    consent.redeem_share_token(world.db, token=t["token"], staff_id=world.staff[1].id)


def test_token_validation(world):
    c = world.client(world.A)
    assert c.post("/api/sharing/tokens", json={}).status_code == 422
    assert c.post("/api/sharing/tokens", json={"categories": ["nope"]}).status_code == 422
    other = world.upload(world.B)
    assert c.post("/api/sharing/tokens", json={"document_ids": [other["id"]]}).status_code == 404
    assert c.post("/api/sharing/tokens", json={"categories": ["lab_result"], "max_uses": 999}).status_code == 422


def test_token_category_scope(world):
    pub = world.upload(world.A, name="pub", category="insurance", is_private="false")
    world.upload(world.A, name="priv", category="insurance")
    t = make_token(world, world.A, categories=["insurance"])
    consent.redeem_share_token(world.db, token=t["token"], staff_id=world.staff[0].id)
    assert [m["id"] for m in world.visible(world.staff[0], world.A)] == [pub["id"]]


# ------------------------------------------------------------------ access requests

def test_access_request_approve_flow(world):
    d = world.upload(world.A, name="Lab", category="lab_result")
    world.upload(world.A, name="Scan", category="imaging")
    st = world.staff[0]
    req = consent.create_access_request(world.db, staff_id=st.id, patient_id=world.A.id,
                                        categories=["lab_result"], document_ids=[], purpose="Pre-op",
                                        duration_days=5)
    assert req.status == "pending"
    n = world.notifications("patient", world.A.id, "access_request")
    assert n and world.provs[0].name in n[0].title
    assert world.visible(st, world.A) == []
    c = world.client(world.A)
    pending = c.get("/api/sharing/requests?status=pending").json()["requests"]
    assert len(pending) == 1 and pending[0]["purpose"] == "Pre-op"
    pv = c.get(f"/api/sharing/requests/{req.id}/preview").json()
    assert [x["name"] for x in pv["documents"]] == ["Lab"]
    r = c.post(f"/api/sharing/requests/{req.id}/approve", json={})
    assert r.status_code == 200 and r.json()["status"] == "approved"
    assert [m["id"] for m in world.visible(st, world.A)] == [d["id"]]
    assert world.notifications("staff", st.id, "access_approved")
    assert world.audit(world.A.id, "access_approved")
    # a decided request cannot be decided again
    assert c.post(f"/api/sharing/requests/{req.id}/deny").status_code == 422
    # grant expiry ~5 days
    g = consent.active_grants_for_patient(world.db, world.A.id)[0]
    assert g["via"] == "access_request" and g["category"] == "lab_result"


def test_access_request_deny_flow(world):
    world.upload(world.A)
    st = world.staff[0]
    req = consent.create_access_request(world.db, staff_id=st.id, patient_id=world.A.id,
                                        categories=["lab_result"], document_ids=[], purpose="x", duration_days=3)
    r = world.client(world.A).post(f"/api/sharing/requests/{req.id}/deny")
    assert r.status_code == 200 and r.json()["status"] == "denied"
    assert world.visible(st, world.A) == []
    assert world.notifications("staff", st.id, "access_denied")
    assert world.client(world.A).post(f"/api/sharing/requests/{req.id}/approve", json={}).status_code == 422


def test_other_patient_cannot_decide_request(world):
    world.upload(world.A)
    req = consent.create_access_request(world.db, staff_id=world.staff[0].id, patient_id=world.A.id,
                                        categories=["lab_result"], document_ids=[], purpose="x", duration_days=3)
    b = world.client(world.B)
    assert b.post(f"/api/sharing/requests/{req.id}/approve", json={}).status_code == 404
    assert b.post(f"/api/sharing/requests/{req.id}/deny").status_code == 404
    assert b.get(f"/api/sharing/requests/{req.id}/preview").status_code == 404
    assert b.get("/api/sharing/requests").json()["requests"] == []


def test_approval_can_narrow_scope_and_duration(world):
    world.upload(world.A, name="Lab", category="lab_result")
    img = world.upload(world.A, name="Scan", category="imaging")
    req = consent.create_access_request(world.db, staff_id=world.staff[0].id, patient_id=world.A.id,
                                        categories=["lab_result", "imaging"], document_ids=[], purpose="x",
                                        duration_days=30)
    r = world.client(world.A).post(f"/api/sharing/requests/{req.id}/approve",
                                   json={"categories": ["imaging"], "duration_days": 1})
    assert r.status_code == 200
    assert [m["id"] for m in world.visible(world.staff[0], world.A)] == [img["id"]]
    g = world.db.query(ShareGrant).filter_by(patient_id=world.A.id).one()
    assert g.expires_at < utcnow() + timedelta(days=1, minutes=5)
    # cannot widen beyond what was requested
    req2 = consent.create_access_request(world.db, staff_id=world.staff[1].id, patient_id=world.A.id,
                                         categories=["imaging"], document_ids=[], purpose="x", duration_days=1)
    r = world.client(world.A).post(f"/api/sharing/requests/{req2.id}/approve", json={"categories": ["lab_result"]})
    assert r.status_code == 422
    assert world.visible(world.staff[1], world.A) == []


def test_access_request_validation_and_dedupe(world):
    st = world.staff[0]
    with pytest.raises(ValueError):
        consent.create_access_request(world.db, staff_id=st.id, patient_id=world.A.id, categories=[],
                                      document_ids=[], purpose="x", duration_days=1)
    with pytest.raises(ValueError):
        consent.create_access_request(world.db, staff_id=st.id, patient_id=world.A.id, categories=["nope"],
                                      document_ids=[], purpose="x", duration_days=1)
    with pytest.raises(LookupError):
        consent.create_access_request(world.db, staff_id=st.id, patient_id="nobody", categories=["other"],
                                      document_ids=[], purpose="x", duration_days=1)
    a = consent.create_access_request(world.db, staff_id=st.id, patient_id=world.A.id, categories=["other"],
                                      document_ids=[], purpose="x", duration_days=1)
    b = consent.create_access_request(world.db, staff_id=st.id, patient_id=world.A.id, categories=["other"],
                                      document_ids=[], purpose="x", duration_days=1)
    assert a.id == b.id
    # duration is clamped
    c = consent.create_access_request(world.db, staff_id=st.id, patient_id=world.A.id, categories=["billing"],
                                      document_ids=[], purpose="x", duration_days=99999)
    assert c.duration_days <= consent.MAX_GRANT_DAYS


def test_access_request_with_document_ids_ignores_foreign_ids(world):
    mine = world.upload(world.A, name="mine")
    theirs = world.upload(world.B, name="theirs")
    req = consent.create_access_request(world.db, staff_id=world.staff[0].id, patient_id=world.A.id,
                                        categories=[], document_ids=[mine["id"], theirs["id"]], purpose="x",
                                        duration_days=2)
    assert consent._loads(req.document_ids_json) == [mine["id"]]


def test_request_expires_via_sweep(world):
    world.upload(world.A)
    req = consent.create_access_request(world.db, staff_id=world.staff[0].id, patient_id=world.A.id,
                                        categories=["lab_result"], document_ids=[], purpose="x", duration_days=1)
    req.expires_at = utcnow() - timedelta(minutes=1)
    world.db.commit()
    consent.run_expiry_sweep(world.db)
    world.db.refresh(req)
    assert req.status == "expired"
    r = world.client(world.A).post(f"/api/sharing/requests/{req.id}/approve", json={})
    assert r.status_code == 422


def test_expiry_sweep_notifies_once(world):
    d = world.upload(world.A, name="Soon")
    share(world, world.A, world.provs[0], [d["id"]])
    far = world.upload(world.A, name="Far")
    share(world, world.A, world.provs[0], [far["id"]], expires_in_days=30)
    g = world.db.query(ShareGrant).filter_by(patient_id=world.A.id, document_id=d["id"]).one()
    g.expires_at = utcnow() + timedelta(hours=5)
    world.db.commit()
    res = consent.run_expiry_sweep(world.db)
    assert res["expiry_warnings"] >= 1
    n = world.notifications("patient", world.A.id, "share_expiring")
    assert len(n) == 1 and "Soon" in n[0].body
    consent.run_expiry_sweep(world.db)
    assert len(world.notifications("patient", world.A.id, "share_expiring")) == 1  # idempotent


def test_sharing_routes_require_login(world):
    from fastapi.testclient import TestClient
    from app.main import app
    c = TestClient(app)
    for m, p in [("get", "/api/sharing/overview"), ("get", "/api/sharing/grants"), ("get", "/api/sharing/requests"),
                 ("get", "/api/sharing/tokens"), ("get", "/api/sharing/history"), ("get", "/api/sharing/providers"),
                 ("post", "/api/sharing/grants"), ("post", "/api/sharing/tokens"), ("post", "/api/sharing/preview"),
                 ("post", "/api/sharing/sweep"), ("delete", "/api/sharing/grants/x")]:
        assert getattr(c, m)(p).status_code in (401, 403), p


def test_overview_and_providers(world):
    j = world.client(world.A).get("/api/sharing/overview").json()
    assert set(j) >= {"grants", "pending_requests", "tokens", "retention_note"}
    names = [p["name"] for p in world.client(world.A).get("/api/sharing/providers").json()["providers"]]
    assert world.provs[0].name in names


def test_dashboard_compat_endpoints(world):
    world.upload(world.A)
    st = world.staff[0]
    consent.create_access_request(world.db, staff_id=st.id, patient_id=world.A.id, categories=["lab_result"],
                                  document_ids=[], purpose="x", duration_days=2)
    c = world.client(world.A)
    pend = c.get("/api/access-requests?status=pending").json()
    assert isinstance(pend, list) and len(pend) == 1
    assert c.get("/api/shares/active").json() == []
    assert c.post(f"/api/access-requests/{pend[0]['id']}/approve", json={}).status_code == 200
    act = c.get("/api/shares/active").json()
    assert isinstance(act, list) and len(act) == 1 and act[0]["category"] == "lab_result"
    assert c.get("/api/access-requests?status=pending").json() == []
    assert world.client(world.B).get("/api/shares/active").json() == []


def test_e2e_contract_flow(world):
    """Mirror of the CONTRACT done-criteria flow (documents part)."""
    pdf = world.upload(world.A, name="Blood test.pdf", category="lab_result")
    photo = world.upload(world.A, PNG + b"x", name="Rash photo", category="imaging", filename="p.png")
    st = world.staff[0]
    assert world.visible(st, world.A) == []
    consent.create_access_request(world.db, staff_id=st.id, patient_id=world.A.id,
                                  categories=["lab_result", "imaging"], document_ids=[], purpose="Visit",
                                  duration_days=7)
    req = world.client(world.A).get("/api/sharing/requests?status=pending").json()["requests"][0]
    world.client(world.A).post(f"/api/sharing/requests/{req['id']}/approve", json={})
    assert {m["id"] for m in world.visible(st, world.A)} == {pdf["id"], photo["id"]}
    meta, data = world.view(st, world.A, pdf["id"])
    assert data.startswith(b"%PDF")
    assert world.audit(world.A.id, "document_viewed")
    for g in world.client(world.A).get("/api/sharing/grants").json()["grants"]:
        world.client(world.A).delete(f"/api/sharing/grants/{g['id']}")
    assert world.visible(st, world.A) == []
    with pytest.raises(PermissionError):
        world.view(st, world.A, pdf["id"])
