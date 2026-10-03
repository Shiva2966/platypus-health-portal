"""Admin organization creation + role promotion (OPS)."""
import uuid

import pytest
import w3_support as w


@pytest.fixture(autouse=True)
def _no_otp(monkeypatch):
    w.legacy_auth_env(monkeypatch)


def test_admin_creates_provider_and_promotes_staff_with_audit():
    w.seeded()
    admin, nurse = w.staff_login("admin@riverside.demo"), w.staff_login("nurse@riverside.demo")
    name = f"Ops Clinic {uuid.uuid4().hex[:6]}"
    assert nurse.post("/api/staff/admin/providers", json={"name": name}).status_code == 403
    r = admin.post("/api/staff/admin/providers", json={"name": name, "specialty": "Clinic"})
    assert r.status_code == 201
    pid = r.json()["id"]
    assert admin.post("/api/staff/admin/providers", json={"name": name.upper()}).status_code == 409
    assert any(p["id"] == pid for p in admin.get("/api/staff/admin/users").json()["providers"])

    u = admin.post("/api/staff/admin/users", json={"name": "New Hire", "email": f"{uuid.uuid4().hex[:8]}@ops.test",
                                                   "password": "Temporary-Pass-1", "role": "front_desk"}).json()
    for role in ("nurse", "physician", "admin"):
        p = admin.patch(f"/api/staff/admin/users/{u['id']}", json={"role": role, "provider_id": pid})
        assert p.status_code == 200 and p.json()["role"] == role and p.json()["provider_id"] == pid
    actions = admin.get("/api/staff/admin/audit", params={"action": "staff_updated", "limit": 200}).json()["items"]
    assert any(a["resource_id"] == u["id"] and a["detail"].get("role") == "admin" for a in actions)
    created = admin.get("/api/staff/admin/audit", params={"action": "provider_created"}).json()["items"]
    assert any(a["resource_id"] == pid for a in created)
