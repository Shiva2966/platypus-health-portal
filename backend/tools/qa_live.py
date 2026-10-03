"""QA live probe against a running instance (default http://127.0.0.1:8001).
Usage: .venv\\Scripts\\python.exe tools/qa_live.py
Prints PASS/FAIL/INFO lines. Synthetic seed data only. Never prints secrets.
"""
import io
import json
import re
import sys
import uuid

import httpx

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8001"
PW_P = "Demo-Patient-2026!"
PW_S = "Staff-Demo-2026!"
results = []


def rec(ok, name, extra=""):
    results.append((ok, name))
    print(("PASS " if ok else "FAIL ") + name + ((" :: " + str(extra)[:300]) if extra and not ok else ""))


def client():
    return httpx.Client(base_url=BASE, timeout=20, follow_redirects=False)


def patient_login(email, pw=PW_P, c=None):
    c = c or client()
    r = c.post("/api/auth/login", json={"email": email, "password": pw})
    j = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    if r.status_code == 200 and j.get("otp_required"):
        code = j.get("dev_otp")
        r = c.post("/api/auth/verify-login-otp", json={"challenge_id": j.get("otp_challenge_id"), "otp_challenge_id": j.get("otp_challenge_id"), "code": code})
    return c, r


def staff_login(email, pw=PW_S):
    c = client()
    r = c.post("/api/staff/auth/login", json={"email": email, "password": pw})
    j = r.json()
    if r.status_code == 200 and j.get("otp_required"):
        r = c.post("/api/staff/auth/verify-login-otp", json={"challenge_id": j.get("otp_challenge_id"), "otp_challenge_id": j.get("otp_challenge_id"), "code": j.get("dev_otp")})
    return c, r


def main():
    # ---------------- config / health
    c = client()
    r = c.get("/api/auth/config")
    print("auth config", r.status_code, r.text[:300])
    # ---------------- patient login
    a, r = patient_login("jordan.ellis@example.test")
    rec(r.status_code == 200, "patient A login (+otp)", r.text)
    b, r = patient_login("casey.morgan@example.test")
    rec(r.status_code == 200, "patient B login (+otp)", r.text)
    me_a = a.get("/api/auth/me").json()
    me_b = b.get("/api/auth/me").json()
    print("me A", json.dumps(me_a)[:300])
    rec(bool(me_a), "me A")
    pid_a = me_a.get("id") or (me_a.get("patient") or {}).get("id")
    pid_b = me_b.get("id") or (me_b.get("patient") or {}).get("id")

    # ---------------- GET sweeps for 500s
    gets = ["/api/profile", "/api/emergency-contacts", "/api/profile/gaps", "/api/med/summary", "/api/med/timeline",
            "/api/med/history", "/api/med/medications", "/api/med/allergies", "/api/med/vaccinations", "/api/med/results",
            "/api/med/results/trends", "/api/med/providers", "/api/med/directory", "/api/med/meta", "/api/med/allergy-status",
            "/api/med/corrections", "/api/med/linkable-documents", "/api/results/recent?limit=5",
            "/api/billing/summary", "/api/billing/plans", "/api/billing/bills", "/api/billing/claims", "/api/billing/disputes",
            "/api/billing/privacy", "/api/billing/export", "/api/billing/providers/meta", "/api/billing/providers/search",
            "/api/appt/info", "/api/appt/providers", "/api/appt/specialties", "/api/appt/intake-form", "/api/appt/appointments",
            "/api/appointments", "/api/appt/dashboard-summary", "/api/care/caregivers", "/api/care/dependents", "/api/care/activity",
            "/api/settings/privacy", "/api/settings/accessibility", "/api/settings/connected-apps", "/api/settings/export.json",
            "/api/settings/export.zip", "/api/settings/deletion-request",
            "/api/documents", "/api/documents/meta", "/api/sharing/overview", "/api/sharing/providers", "/api/sharing/grants",
            "/api/sharing/requests", "/api/sharing/tokens", "/api/sharing/history", "/api/shares/active",
            "/api/access-requests?status=pending", "/api/notifications", "/api/notifications/unread-count", "/api/notifications/prefs",
            "/api/audit/mine", "/api/auth/sessions", "/api/auth/trusted-devices", "/api/ui-modules"]
    for g in gets:
        r = a.get(g)
        rec(r.status_code < 500 and r.status_code != 404, f"A GET {g} -> {r.status_code}", r.text)

    # ---------------- unauth
    anon = client()
    for g in ["/api/profile", "/api/documents", "/api/med/results", "/api/billing/bills", "/api/notifications", "/api/audit/mine", "/api/sharing/grants", "/api/settings/export.json", "/api/emergency-contacts"]:
        r = anon.get(g)
        rec(r.status_code == 401, f"anon GET {g} -> {r.status_code}", r.text)
    for g in ["/api/staff/me", "/api/staff/patients/search?q=Jordan", "/api/staff/dashboard", "/api/staff/admin/audit", "/api/staff/appointments", "/api/staff/requests"]:
        r = anon.get(g)
        rec(r.status_code == 401, f"anon GET {g} -> {r.status_code}", r.text)

    # ---------------- patient session vs staff APIs
    for g in ["/api/staff/me", "/api/staff/patients/search?q=Casey", "/api/staff/dashboard", "/api/staff/admin/audit", "/api/staff/appointments", "/api/staff/requests", "/api/staff/admin/users", f"/api/staff/patients/{pid_b}", f"/api/staff/patients/{pid_a}/billing"]:
        r = a.get(g)
        rec(r.status_code in (401, 403), f"patient session GET {g} -> {r.status_code}", r.text)

    # ---------------- staff login
    roles = {}
    for em in ["frontdesk@riverside.demo", "nurse@riverside.demo", "doctor@riverside.demo", "admin@riverside.demo", "doctor@lakeside.demo"]:
        s, r = staff_login(em)
        rec(r.status_code == 200, f"staff login {em}", r.text)
        roles[em] = s
    fd, nu, dr, ad, lk = (roles[k] for k in roles)
    # staff session vs patient APIs
    for g in ["/api/profile", "/api/documents", "/api/med/results", "/api/billing/bills", "/api/audit/mine", "/api/sharing/grants", "/api/emergency-contacts", "/api/settings/export.json", "/api/appt/appointments"]:
        r = fd.get(g)
        rec(r.status_code in (401, 403), f"staff session GET {g} -> {r.status_code}", r.text[:200])
    # staff GET sweep
    for s_name, s in [("fd", fd), ("nurse", nu), ("doc", dr), ("admin", ad)]:
        for g in ["/api/staff/me", "/api/staff/dashboard", "/api/staff/appointments", "/api/staff/requests", "/api/notifications", "/api/notifications/unread-count", "/api/staff/documents/search"]:
            r = s.get(g)
            rec(r.status_code < 500, f"{s_name} GET {g} -> {r.status_code}", r.text)
    r = fd.get("/api/staff/admin/audit")
    rec(r.status_code in (401, 403), f"front_desk blocked from admin audit -> {r.status_code}")
    r = nu.get("/api/staff/admin/users")
    rec(r.status_code in (401, 403), f"nurse blocked from admin users -> {r.status_code}")
    r = ad.get("/api/staff/admin/audit")
    rec(r.status_code == 200, f"admin audit -> {r.status_code}", r.text)

    # ---------------- staff search, no data w/o grant
    r = dr.get("/api/staff/patients/search", params={"q": "Jordan"})
    print("search", r.status_code, r.text[:400])
    found = r.json() if r.status_code == 200 else {}
    items = found.get("results", found.get("items", [])) if isinstance(found, dict) else found
    rec(r.status_code == 200 and len(items) >= 1, "staff search by name finds Jordan", r.text)
    blob = r.text
    rec("example.test" not in blob and "password" not in blob.lower(), "search result has no email/password", blob)
    if items:
        sid = items[0].get("id") or items[0].get("patient_id")
        rg = dr.get(f"/api/staff/patients/{sid}")
        print("detail no grant", rg.status_code, rg.text[:500])
        rec(rg.status_code in (200, 403), f"staff patient detail no grant -> {rg.status_code}")
        if rg.status_code == 200:
            t = rg.text.lower()
            rec(not any(k in t for k in ["metformin", "lisinopril", "\"results\":[{"]), "no clinical data without grant", rg.text[:400])

    # ---------------- cross patient
    # B's documents/ids unknown to A: create doc as B, A tries to fetch
    pdf = b"%PDF-1.4\n1 0 obj<</Type/Catalog>>endobj\ntrailer<</Root 1 0 R>>\n%%EOF\n"
    nm = "QA-" + uuid.uuid4().hex[:6]
    r = b.post("/api/documents", data={"name": nm, "description": "qa"}, files={"file": ("x.pdf", pdf, "application/pdf")})
    rec(r.status_code in (200, 201), "B uploads pdf", r.text)
    if r.status_code in (200, 201):
        did = r.json().get("id") or r.json().get("item", {}).get("id") or r.json().get("document", {}).get("id")
        for g in [f"/api/documents/{did}", f"/api/documents/{did}/content", f"/api/documents/{did}/download"]:
            ra = a.get(g)
            rec(ra.status_code == 404, f"A GET B doc {g} -> {ra.status_code}")
        rec(a.delete(f"/api/documents/{did}").status_code == 404, "A cannot delete B doc")
        rec(a.patch(f"/api/documents/{did}", json={"name": "hacked"}).status_code == 404, "A cannot edit B doc")
        rec(did and not re.search(r"\d{3}-\d{2}-\d{4}", did), "doc id is opaque")
        # staff w/out grant
        s_r = dr.get(f"/api/staff/patients/{pid_b}/documents/{did}/file")
        rec(s_r.status_code in (403, 404), f"staff w/o grant doc file -> {s_r.status_code}")
        b.delete(f"/api/documents/{did}")

    # upload spoofing
    r = a.post("/api/documents", data={"name": "evil"}, files={"file": ("evil.pdf", b"<html><script>alert(1)</script></html>", "application/pdf")})
    rec(r.status_code in (400, 415, 422), f"spoofed pdf rejected -> {r.status_code}", r.text)
    r = a.post("/api/documents", data={"name": "evil"}, files={"file": ("evil.exe", b"MZ\x90\x00" + b"\x00" * 100, "application/octet-stream")})
    rec(r.status_code in (400, 415, 422), f"exe rejected -> {r.status_code}", r.text)
    r = a.post("/api/documents", data={"name": ""}, files={"file": ("x.pdf", pdf, "application/pdf")})
    rec(r.status_code in (400, 422), f"empty name rejected -> {r.status_code}", r.text)

    # medical cross access
    ra = a.get("/api/med/results").json()
    ids_a = [i["id"] for i in (ra.get("items", ra) if isinstance(ra, dict) else ra)][:2]
    for i in ids_a:
        rb = b.get(f"/api/med/results/{i}")
        rec(rb.status_code == 404, f"B GET A result -> {rb.status_code}")
        rb = b.put(f"/api/med/results/{i}", json={"name": "x"})
        rec(rb.status_code in (404, 422), f"B PUT A result -> {rb.status_code}")
        rb = b.delete(f"/api/med/results/{i}")
        rec(rb.status_code == 404, f"B DELETE A result -> {rb.status_code}")
    bills = a.get("/api/billing/bills").json()
    for it in (bills.get("items", bills) if isinstance(bills, dict) else bills)[:2]:
        rb = b.get(f"/api/billing/bills/{it['id']}")
        rec(rb.status_code == 404, f"B GET A bill -> {rb.status_code}")
        rb = b.get(f"/api/billing/bills/{it['id']}/explain")
        rec(rb.status_code == 404, f"B GET A bill explain -> {rb.status_code}")
    ns = a.get("/api/notifications").json()
    nitems = ns.get("items", ns) if isinstance(ns, dict) else ns
    for it in nitems[:2]:
        rb = b.post(f"/api/notifications/{it['id']}/read")
        rec(rb.status_code in (403, 404), f"B mark A notification read -> {rb.status_code}")
    ec = a.get("/api/emergency-contacts").json()
    for it in (ec.get("items", ec) if isinstance(ec, dict) else ec)[:2]:
        rb = b.put(f"/api/emergency-contacts/{it['id']}", json={"name": "x", "phone": "5551234567", "relationship": "x"})
        rec(rb.status_code in (404,), f"B PUT A contact -> {rb.status_code}")
        rb = b.delete(f"/api/emergency-contacts/{it['id']}")
        rec(rb.status_code == 404, f"B DELETE A contact -> {rb.status_code}")

    # CSRF / origin
    r = a.post("/api/notifications/read-all", headers={"Origin": "http://evil.example"})
    rec(r.status_code in (400, 403), f"foreign origin rejected -> {r.status_code}")

    # security headers
    r = a.get("/")
    for h in ["x-content-type-options", "content-security-policy"]:
        rec(h in r.headers, f"header {h} on /")
    print("\nSummary:", sum(1 for ok, _ in results if ok), "pass /", sum(1 for ok, _ in results if not ok), "fail")


if __name__ == "__main__":
    main()


