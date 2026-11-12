"""Sessions: a tab showing one account can't act under another account's cookie.

The decision form being keyed by revision and assessment, and the correction form locking while it sends, are frontend
state fixes covered by the type-checked build.
"""

from .test_api_flow import op


def test_a_tab_showing_supplier_a_cannot_submit_as_supplier_b(app_client):
    c = app_client.__class__(app_client.app)  # one browser: both tabs share this cookie jar
    a = c.post("/api/auth/new-vendor", json={"company_name": "Alpha Cross Tab Services", "buyer": "meridian"}).json()
    draft = c.post("/api/vendor/samples/happy_nimbus/load").json()  # supplier A's tab fills in the form
    body = {"operation_id": op(), "form": draft["form"],
            "documents": {k: v["document_id"] for k, v in draft["documents"].items()}}
    # A second tab signs in as a supplier of another buyer, which replaces the shared cookie.
    b = c.post("/api/auth/new-vendor", json={"company_name": "Bravo Cross Tab Services", "buyer": "kestrel"}).json()
    assert a["id"] != b["id"]

    # The first tab still shows A and says so: refused, and nothing is filed under B.
    c.headers["X-Signed-In-As"] = str(a["id"])
    r = c.post("/api/vendor/submit", json=body)
    assert r.status_code == 409 and r.json()["error"] == "signed_in_elsewhere"
    assert c.get("/api/vendor/overview").status_code == 409  # reads too, so the tab learns at its next refresh

    # The second tab (showing B) carries on as B, and a request without the header (e.g. an opened PDF) still works.
    c.headers["X-Signed-In-As"] = str(b["id"])
    assert c.get("/api/vendor/overview").json()["requests"] == []
    del c.headers["X-Signed-In-As"]
    assert c.get("/api/auth/me").json()["id"] == b["id"]
    # A has nothing filed either: the refused submission went nowhere.
    assert c.post("/api/auth/login", json={"user_id": a["id"]}).status_code == 200
    assert c.get("/api/vendor/overview").json()["requests"] == []
