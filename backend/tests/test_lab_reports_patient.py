from test_documents_support import world, uniq


def test_patient_lab_report_list_and_original_links(world):
    original = uniq(b"patient lab report regression")
    doc = world.upload(world.A, data=original, name="Patient report")
    owner = world.client(world.A)
    response = owner.get("/api/documents?category=lab_result&sort=newest")
    assert response.status_code == 200
    assert any(d["id"] == doc["id"] for d in response.json()["documents"])
    for suffix in ("content", "download"):
        url = f"/api/documents/{doc['id']}/{suffix}"
        opened = owner.get(url)
        assert opened.status_code == 200
        assert opened.content == original
        assert world.client(world.B).get(url).status_code == 404
