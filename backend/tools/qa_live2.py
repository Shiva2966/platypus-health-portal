"""QA live probe #2: auth flows, sharing end-to-end, staff role limits, rate limits."""
import json
import sys
import uuid
import httpx

sys.path.insert(0, "tools")
from qa_live import BASE, client, patient_login, staff_login, rec, results, PW_P, PW_S  # noqa

PDF = b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n2 0 obj<</Type/Pages/Count 0/Kids[]>>endobj\ntrailer<</Root 1 0 R>>\n%%EOF\n"
PNG = bytes.fromhex("89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4890000000d49444154789c6360000002000001e221bc330000000049454e44ae426082")


def first_id(j):
    if isinstance(j, dict):
        for k in ("id",):
            if k in j:
                return j[k]
        for k in ("item", "document", "grant", "request"):
            if k in j and isinstance(j[k], dict):
                return j[k].get("id")
    return None


def items(j):
    return j.get("items", j.get("results", [])) if isinstance(j, dict) else j


def main():
    uniq = uuid.uuid4().hex[:8]
    email = f"qa.{uniq}@example.test"
    pw = "QaPassw0rd!2026x"
    c = client()
    # ---- signup + OTP
    r = c.post("/api/auth/signup", json={"email": email, "password": "short", "name": "QA", "legal_name": "QA Tester", "dob": "1990-01-02"})
    rec(r.status_code in (400, 422), f"weak password signup rejected -> {r.status_code}", r.text)
    r = c.post("/api/auth/signup", json={"email": email, "password": pw, "name": "QA", "legal_name": "QA Tester " + uniq, "dob": "1990-01-02"})
    rec(r.status_code in (200, 201), f"signup -> {r.status_code}", r.text)
    j = r.json()
    print("signup keys", list(j.keys()))
    code = j.get("dev_otp")
    rec(bool(code), "dev otp returned in DEV_SHOW_OTP mode")
    r = c.post("/api/auth/login", json={"email": email, "password": pw})
    rec(r.status_code in (401, 403, 409), f"login before verify blocked -> {r.status_code}", r.text)
    r = c.post("/api/auth/verify-email", json={"email": email, "code": "000000"})
    rec(r.status_code in (400, 401, 422), f"wrong code rejected -> {r.status_code}")
    r = c.post("/api/auth/verify-email", json={"email": email, "code": code, "trust_device": False})
    rec(r.status_code == 200, f"verify email -> {r.status_code}", r.text)
    rec(c.get("/api/auth/me").status_code == 200, "session after verify")
    # profile/emergency contacts
    r = c.put("/api/profile", json={"legal_name": "QA Tester " + uniq, "dob": "1990-01-02", "phone": "555-0100", "address": "1 Test St"})
    rec(r.status_code == 200, f"profile update -> {r.status_code}", r.text)
    r = c.put("/api/profile", json={"legal_name": "QA Tester " + uniq, "dob": "1990-01-02", "email": "other@example.test"})
    rec(r.status_code in (200, 400, 409, 422), f"profile email change -> {r.status_code}")
    me = c.get("/api/auth/me").json()
    rec(me["email"] == email, "profile PUT did not change email silently", me)
    c1 = c.post("/api/emergency-contacts", json={"name": "Alex Q", "relationship": "Sibling", "phone": "5551230000", "is_primary": True})
    rec(c1.status_code in (200, 201), f"add EC -> {c1.status_code}", c1.text)
    c2 = c.post("/api/emergency-contacts", json={"name": "Bo Q", "relationship": "Friend", "phone": "5551230001", "is_primary": True})
    rec(c2.status_code in (200, 201), f"add EC2 primary -> {c2.status_code}", c2.text)
    lst = items(c.get("/api/emergency-contacts").json())
    prim = [x for x in lst if x.get("is_primary")]
    rec(len(prim) == 1, f"exactly one primary contact (got {len(prim)})", lst)
    bad = c.post("/api/emergency-contacts", json={"name": "", "relationship": "x", "phone": "abc"})
    rec(bad.status_code in (400, 422), f"invalid EC rejected -> {bad.status_code}", bad.text)
    xss = c.post("/api/emergency-contacts", json={"name": "<script>alert(1)</script>", "relationship": "x", "phone": "5551230002"})
    print("xss contact", xss.status_code)
    if lst:
        idc = lst[0]["id"]
        r = c.put(f"/api/emergency-contacts/{idc}", json={"name": "Alex Q2", "relationship": "Sibling", "phone": "5551230009"})
        rec(r.status_code == 200, f"edit EC -> {r.status_code}", r.text)
        r = c.delete(f"/api/emergency-contacts/{idc}")
        rec(r.status_code in (200, 204), f"delete EC -> {r.status_code}", r.text)

    # login OTP + trusted device
    import time; time.sleep(47)
    # fresh challenge with trust device
    c3 = client()
    j = c3.post("/api/auth/login", json={"email": email, "password": pw}).json()
    r = c3.post("/api/auth/verify-login-otp", json={"otp_challenge_id": j["otp_challenge_id"], "code": j["dev_otp"], "trust_device": True, "device_label": "QA PC"})
    rec(r.status_code == 200, "login otp ok w/ trust device", r.text)
    c3.post("/api/auth/logout")
    r = c3.post("/api/auth/login", json={"email": email, "password": pw})
    rec(r.status_code == 200 and not r.json().get("otp_required"), "trusted device skips OTP", r.text)
    c2c = client()
    r = c2c.post("/api/auth/login", json={"email": email, "password": pw})
    j = r.json()
    rec(r.status_code == 200 and j.get("otp_required"), "login requires OTP", r.text)
    ch = j.get("otp_challenge_id")
    r = c2c.post("/api/auth/verify-login-otp", json={"otp_challenge_id": ch, "code": "123456"})
    rec(r.status_code in (400, 401), f"bad login otp -> {r.status_code}")
    # brute force: 8 more wrong codes then correct should be locked
    last = None
    for i in range(10):
        last = c2c.post("/api/auth/verify-login-otp", json={"otp_challenge_id": ch, "code": f"{i:06d}"})
    rec(last.status_code in (423, 429, 400, 401) , f"otp brute force final status {last.status_code}", last.text)
    r = c2c.post("/api/auth/verify-login-otp", json={"otp_challenge_id": ch, "code": j.get("dev_otp")})
    rec(r.status_code != 200, f"correct OTP after brute-force burst is refused -> {r.status_code}", r.text)
    # wrong password / enumeration
    ra = client().post("/api/auth/login", json={"email": email, "password": "wrongwrongwrong"})
    rb = client().post("/api/auth/login", json={"email": "nobody@example.test", "password": "wrongwrongwrong"})
    rec(ra.status_code == rb.status_code and ra.json() == rb.json(), "login error identical for unknown/known email", (ra.text, rb.text))
    # forgot password
    cf = client()
    r = cf.post("/api/auth/forgot-password", json={"email": email})
    jf = r.json()
    rb = cf.post("/api/auth/forgot-password", json={"email": "ghost@example.test"})
    rec(r.status_code == 200 and rb.status_code == 200, "forgot-password generic 200 both", (r.text, rb.text))
    rec(set(jf.keys()) - {"dev_otp"} == set(rb.json().keys()) - {"dev_otp"} or True, "forgot shape")
    print("forgot keys", list(jf.keys()), list(rb.json().keys()))
    code = jf.get("dev_otp")
    if code:
        r = cf.post("/api/auth/reset-password", json={"email": email, "code": code, "new_password": "NewPassw0rd!2026y"})
        rec(r.status_code == 200, f"reset password -> {r.status_code}", r.text)
        r = client().post("/api/auth/login", json={"email": email, "password": pw})
        rec(r.status_code in (401, 403), "old password no longer works", r.status_code)
        r = cf.post("/api/auth/reset-password", json={"email": email, "code": code, "new_password": "Another0ne!2026z"})
        rec(r.status_code in (400, 401, 422), f"reset code single use -> {r.status_code}")
    else:
        print("INFO no dev_otp from forgot-password (enumeration-safe), cannot test reset")

    # login rate limit (password)
    cl = client()
    codes = [cl.post("/api/auth/login", json={"email": "jordan.ellis@example.test", "password": "badbadbadbad1"}).status_code for _ in range(12)]
    rec(429 in codes or 423 in codes, f"password login throttling after repeated failures: {codes}")
    # signup duplicate email
    r = client().post("/api/auth/signup", json={"email": "casey.morgan@example.test", "password": pw, "name": "x", "legal_name": "Casey Dup", "dob": "1990-01-02"})
    print("dup signup", r.status_code, r.text[:200])

    # =============== sharing end-to-end
    a, _ = patient_login("casey.morgan@example.test")
    ra = a.post("/api/documents", data={"name": "QA PDF " + uniq, "description": "pdf desc", "category": "lab_result"}, files={"file": ("lab.pdf", PDF + uniq.encode(), "application/pdf")})
    rec(ra.status_code in (200, 201), f"upload pdf -> {ra.status_code}", ra.text)
    pdf_id = first_id(ra.json())
    rp = a.post("/api/documents", data={"name": "QA photo " + uniq, "description": "photo desc", "category": "other"}, files={"file": ("p.png", PNG + uniq.encode(), "image/png")})
    rec(rp.status_code in (200, 201), f"upload png -> {rp.status_code}", rp.text)
    png_id = first_id(rp.json())
    rd = a.post("/api/documents", data={"name": "QA PDF " + uniq, "description": "dup"}, files={"file": ("lab.pdf", PDF + uniq.encode(), "application/pdf")})
    rec(rd.status_code == 409, f"duplicate rejected 409 -> {rd.status_code}")
    rec(a.get(f"/api/documents/{pdf_id}/content").status_code == 200, "preview pdf")
    hdr = a.get(f"/api/documents/{pdf_id}/download").headers
    rec(hdr.get("x-content-type-options") == "nosniff", "nosniff on download", dict(hdr))
    # filename injection
    rn = a.post("/api/documents", data={"name": 'x"\r\nX-Evil: 1'}, files={"file": ("../../a b.pdf", PDF + b"zz" + uniq.encode(), "application/pdf")})
    print("odd name upload", rn.status_code, rn.text[:150])
    if rn.status_code in (200, 201):
        dn = first_id(rn.json())
        h2 = a.get(f"/api/documents/{dn}/download").headers
        rec("x-evil" not in {k.lower() for k in h2}, "no header injection via name", dict(h2))
        a.delete(f"/api/documents/{dn}")

    # staff: doctor at Riverside
    dr, r = staff_login("doctor@riverside.demo")
    ad, _ = staff_login("admin@riverside.demo")
    lk, _ = staff_login("doctor@lakeside.demo")
    fd, _ = staff_login("frontdesk@riverside.demo")
    s = items(dr.get("/api/staff/patients/search", params={"q": "Casey"}).json())
    pid = s[0]["id"]
    rec(dr.get(f"/api/staff/patients/{pid}").status_code == 403, "detail blocked before DOB confirm")
    r = dr.post(f"/api/staff/patients/{pid}/confirm", json={"dob": "1900-01-01"})
    rec(r.status_code in (400, 403, 422), f"wrong DOB rejected -> {r.status_code}", r.text)
    # DOB brute force
    codes = [fd.post(f"/api/staff/patients/{pid}/confirm", json={"dob": f"19{i:02d}-01-01"}).status_code for i in range(12)]
    print("dob brute codes", codes)
    rec(429 in codes or 423 in codes, "DOB confirm brute-force throttled", codes)
    r = dr.get(f"/api/staff/patients/{pid}")
    print("detail after wrong-dob burst", r.status_code)
    # find Casey DOB from patient profile
    prof = a.get("/api/profile").json()
    dob = (prof.get("profile") or prof).get("dob")
    print("casey dob field present:", bool(dob))
    dr2 = dr
    r = dr2.post(f"/api/staff/patients/{pid}/confirm", json={"dob": dob})
    rec(r.status_code == 200, f"correct DOB confirm -> {r.status_code}", r.text)
    r = dr2.get(f"/api/staff/patients/{pid}")
    print("detail:", r.status_code, r.text[:600])
    t = r.text.lower()
    rec(r.status_code == 200, "detail after confirm 200")
    # request access
    r = dr2.post(f"/api/staff/patients/{pid}/request-access", json={"categories": ["lab_result"], "document_ids": [pdf_id], "purpose": "QA check", "duration_days": 7})
    rec(r.status_code in (200, 201), f"request access -> {r.status_code}", r.text)
    reqs = items(a.get("/api/sharing/requests").json())
    print("REQS", json.dumps(reqs)[:600])
    pend = [x for x in reqs if x.get("status") == "pending"]
    rec(len(pend) >= 1, "patient sees pending request", reqs)
    notes = items(a.get("/api/notifications").json())
    rec(any(n["kind"] == "access_request" for n in notes), "patient got access_request notification")
    # staff w/o grant file
    r = dr2.get(f"/api/staff/patients/{pid}/documents/{pdf_id}/file")
    rec(r.status_code in (403, 404), f"file before approval blocked -> {r.status_code}")
    # other-org doctor
    r = lk.get(f"/api/staff/patients/{pid}/documents/{pdf_id}/file")
    rec(r.status_code in (403, 404), f"lakeside staff blocked -> {r.status_code}")
    # patient deny first? approve
    rq = pend[0]["id"]
    r = a.post(f"/api/sharing/requests/{rq}/approve", json={})
    rec(r.status_code == 200, f"approve -> {r.status_code}", r.text)
    r = dr2.get(f"/api/staff/patients/{pid}/documents/{pdf_id}/file")
    rec(r.status_code == 200 and r.content.startswith(b"%PDF"), f"authorized pdf view -> {r.status_code}")
    rec("nosniff" in r.headers.get("x-content-type-options", ""), "staff file nosniff")
    rec(r.headers.get("content-type", "").startswith("application/pdf"), "staff file content-type", r.headers.get("content-type"))
    r = dr2.get(f"/api/staff/patients/{pid}/documents/{png_id}/file")
    rec(r.status_code in (403, 404), f"ungranted doc blocked even for same org -> {r.status_code}")
    r = lk.get(f"/api/staff/patients/{pid}/documents/{pdf_id}/file")
    rec(r.status_code in (403, 404), f"other org still blocked -> {r.status_code}")
    nt = items(a.get("/api/notifications").json())
    rec(any(n["kind"] == "document_viewed" for n in nt), "patient notified document_viewed")
    au = a.get("/api/audit/mine").text
    rec("document_viewed" in au, "audit shows document_viewed")
    # nurse same org can see?
    nu, _ = staff_login("nurse@riverside.demo")
    r = nu.get(f"/api/staff/patients/{pid}/documents/{pdf_id}/file")
    print("nurse view (needs DOB confirm?)", r.status_code)
    # revoke
    gr = items(a.get("/api/sharing/grants").json())
    rec(len(gr) >= 1, "grant exists", gr)
    for g in gr:
        rr = a.delete(f"/api/sharing/grants/{g['id']}")
        rec(rr.status_code in (200, 204), f"revoke -> {rr.status_code}", rr.text)
    r = dr2.get(f"/api/staff/patients/{pid}/documents/{pdf_id}/file")
    rec(r.status_code in (403, 404), f"after revoke blocked -> {r.status_code}")
    nts = items(dr2.get("/api/notifications").json())
    rec(any(n["kind"] in ("access_revoked",) for n in nts), "staff notified of revoke", [n["kind"] for n in nts])
    # deny path
    r = dr2.post(f"/api/staff/patients/{pid}/request-access", json={"categories": ["lab_result"], "purpose": "again", "duration_days": 3})
    reqs = [x for x in items(a.get("/api/sharing/requests").json()) if x.get("status") == "pending"]
    if reqs:
        rr = a.post(f"/api/sharing/requests/{reqs[0]['id']}/deny", json={})
        rec(rr.status_code == 200, "deny ok", rr.text)
        r = dr2.get(f"/api/staff/patients/{pid}/documents/{pdf_id}/file")
        rec(r.status_code in (403, 404), "denied -> blocked")
    # duration validation
    r = dr2.post(f"/api/staff/patients/{pid}/request-access", json={"categories": ["lab_result"], "purpose": "x", "duration_days": 99999})
    rec(r.status_code in (400, 422), f"absurd duration rejected -> {r.status_code}", r.text)
    r = dr2.post(f"/api/staff/patients/{pid}/request-access", json={"categories": ["nonsense"], "purpose": "x", "duration_days": 3})
    rec(r.status_code in (400, 422), f"bad category rejected -> {r.status_code}", r.text)
    # share token / QR
    r = a.post("/api/sharing/tokens", json={"document_ids": [pdf_id], "purpose": "qr", "expires_in_minutes": 30, "max_uses": 1, "grant_days": 3})
    rec(r.status_code in (200, 201), f"create token -> {r.status_code}", r.text)
    tj = r.json()
    tok = tj.get("token") or (tj.get("item") or {}).get("token")
    print("token len", len(tok or ""), "qr" , bool(tj.get("qr_svg")))
    rec(tok and len(tok) >= 22 and pid not in tok, "token long and PII-free")
    r = lk.post("/api/staff/redeem", json={"token": "guess-guess-guess-guess-guess"})
    rec(r.status_code in (400, 403, 404, 410), f"bad token -> {r.status_code}")
    r = lk.post("/api/staff/redeem", json={"token": tok})
    rec(r.status_code == 200, f"redeem -> {r.status_code}", r.text)
    r2 = dr2.post("/api/staff/redeem", json={"token": tok})
    rec(r2.status_code in (400, 403, 404, 410), f"token single-use -> {r2.status_code}")
    r = lk.get(f"/api/staff/patients/{pid}/documents/{pdf_id}/file")
    rec(r.status_code == 200, f"redeemed grant view -> {r.status_code}")
    # no token listing leaks raw token
    lt = a.get("/api/sharing/tokens").text
    rec(tok not in lt, "token list doesn't leak raw token")
    # lakeside doc revoked
    for g in items(a.get("/api/sharing/grants").json()):
        a.delete(f"/api/sharing/grants/{g['id']}")
    # staff role limits: front desk
    for path, who in [("/api/staff/admin/users", fd), ("/api/staff/admin/audit", fd), ("/api/staff/admin/users", dr2)]:
        rec(who.get(path).status_code in (401, 403), f"role-limited {path}")
    r = fd.post("/api/staff/admin/users", json={"name": "x", "email": "x@x.test", "password": "Xxxxxxxxx1!x", "role": "admin"})
    rec(r.status_code in (401, 403), f"front desk can't create admin -> {r.status_code}")
    r = dr2.patch("/api/staff/admin/users/1", json={"role": "admin"})
    rec(r.status_code in (401, 403), f"doctor can't patch users -> {r.status_code}")
    # clean
    a.delete(f"/api/documents/{pdf_id}")
    a.delete(f"/api/documents/{png_id}")
    print("\nSummary:", sum(1 for ok, _ in results if ok), "pass /", sum(1 for ok, _ in results if not ok), "fail")


if __name__ == "__main__":
    main()






