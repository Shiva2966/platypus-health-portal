"""Every endpoint the patient dashboard (core_dashboard.js) calls exists and returns the fields it reads."""
import w3_support as w


def test_dashboard_endpoints_exist_with_expected_shapes():
    pc, pid, name, dob = w.make_patient()
    gaps = pc.get("/api/profile/gaps")
    assert gaps.status_code == 200 and isinstance(gaps.json()["items"], list)
    assert all({"message", "link"} <= set(g) for g in gaps.json()["items"])

    unread = pc.get("/api/notifications/unread-count").json()
    assert "count" in unread

    assert isinstance(pc.get("/api/access-requests", params={"status": "pending"}).json(), list)
    assert isinstance(pc.get("/api/shares/active").json(), list)
    assert isinstance(pc.get("/api/results/recent", params={"limit": 5}).json(), list)

    appt = pc.get("/api/appt/dashboard-summary").json()
    assert isinstance(appt["upcoming"], list) and isinstance(appt["incomplete_intake"], list)
    assert isinstance(pc.get("/api/appointments").json()["items"], list)

    bills = pc.get("/api/billing/summary").json()
    assert {"total_outstanding", "overdue_count", "next_due"} <= set(bills)


def test_dashboard_endpoints_require_a_patient_session():
    c = w.new_client()
    for url in ("/api/profile/gaps", "/api/results/recent", "/api/appt/dashboard-summary", "/api/billing/summary",
                "/api/shares/active", "/api/access-requests"):
        assert c.get(url).status_code == 401, url
