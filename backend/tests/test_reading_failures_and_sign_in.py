"""Failed reads are not reported as missing documents, outdated duplicate-account decisions, the sign-in list, the
registry outage switch, rejected suppliers, and the optional backup reading model.
Real PostgreSQL test database, fixture reader, no model calls.
"""

from app.pipeline.compare import compare
from app.pipeline.evidence import DocEvidence, EvalInput

from .test_api_flow import case, op, run_worker_until_idle, staff, vendor
from .test_auto_approval import as_user, make_buyer
from .test_reviewer_judgements import _check
from .test_clearances_and_visibility import _submit
from .test_scenarios_logic import SCEN, evidence, policy, registry

DUP = "credibility:duplicate_bank"


def _drain():
    run_worker_until_idle(50)


# ---------------------------------------------------------------------------- outdated duplicate-account decisions

def test_a_document_that_failed_to_read_is_not_reported_as_missing():
    s = SCEN["happy_nimbus"]
    docs = [d if d.slot != "bank_proof" else DocEvidence(d.slot, d.document_id, d.filename, d.sha256, d.layout,
                                                           d.page_count, "failed", None, {"message": "503"})
            for d in evidence(s["documents"])]
    cmp = compare(EvalInput(form=s["form"], docs=docs, policy=policy(), verifications=registry(s["form"])))
    acct = cmp.fields["account_number"]
    assert acct.status == "not_read" and "could not be read" in acct.detail and "nimbus_bank_letter.pdf" in acct.detail
    # Control: no bank proof uploaded at all is still "no document".
    cmp = compare(EvalInput(form=s["form"], docs=[d for d in docs if d.slot != "bank_proof"], policy=policy(),
                            verifications=registry(s["form"])))
    assert cmp.fields["account_number"].status == "no_evidence"


# ---------------------------------------------------------------------------- a decision on a list that changed

def test_a_duplicate_account_decision_on_an_outdated_list_is_refused_and_rechecked(app_client):
    _drain()
    b = make_buyer(auto=False)
    rv = as_user(app_client, b.reviewer_id)
    _submit(vendor(app_client, "Shared Account A Private Limited", buyer=b.slug), "happy_nimbus")
    req_b = _submit(vendor(app_client, "Shared Account B Private Limited", buyer=b.slug), "happy_nimbus")["request_id"]
    c = case(rv, req_b)
    seen = _check(c, DUP)  # the page as loaded: one other record
    assert seen["status"] == "needs_review" and len(seen["evidence"]["other_vendor_ids"]) == 1
    # Another supplier submits the same account while the reviewer is deciding (only that request is processed).
    vc = vendor(app_client, "Shared Account C Private Limited", buyer=b.slug)
    r = vc.post("/api/vendor/submit", json={"operation_id": op(), **_sample_body(vc)})
    assert r.status_code == 200, r.text
    resp = rv.post(f"/api/review/requests/{req_b}/resolve", json={
        "operation_id": op(), "expected_version": c["request"]["version"], "check_key": DUP,
        "fingerprint": seen["evidence"]["fingerprint"], "outcome": "not_accepted",
        "note": "Explain why another company uses this account"})
    assert resp.status_code == 409 and resp.json()["error"] == "re_evaluating", resp.text
    _drain()
    now = _check(case(rv, req_b), DUP)
    assert now["status"] == "needs_review" and len(now["evidence"]["other_vendor_ids"]) == 2
    c = case(rv, req_b)
    ok = rv.post(f"/api/review/requests/{req_b}/resolve", json={
        "operation_id": op(), "expected_version": c["request"]["version"], "check_key": DUP,
        "fingerprint": now["evidence"]["fingerprint"], "outcome": "accepted", "note": "Group companies, checked"})
    assert ok.status_code == 200, ok.text
    _drain()


def _sample_body(v):
    s = v.post("/api/vendor/samples/happy_nimbus/load").json()
    return {"form": s["form"], "documents": {k: d["document_id"] for k, d in s["documents"].items()}}


# ---------------------------------------------------------------------------- sign-in list, demo switch, wording

def test_the_sign_in_list_tells_suppliers_apart(app_client):
    b = make_buyer(auto=False)
    v = vendor(app_client, "Typed At Sign Up Name", buyer=b.slug)
    req = _submit(v, "happy_nimbus")
    rows = [x for x in app_client.get("/api/auth/personas").json()["vendors"] if x["application"] == req["reference"]]
    assert len(rows) == 1
    row = rows[0]
    assert row["reference"].startswith("VEN-") and row["signed_up_as"] == "Typed At Sign Up Name"
    assert row["state"] in ("READY_FOR_DECISION", "ACTION_REQUIRED")


def test_the_registry_outage_switch_is_offered_only_where_lookups_are_simulated(app_client):
    assert staff(app_client, buyer="Meridian").get("/api/review/dashboard").json()["registry_simulated"] is True
    assert staff(app_client, buyer="Kestrel").get("/api/review/dashboard").json()["registry_simulated"] is False


def test_a_rejected_supplier_is_not_promised_an_action_that_does_not_exist(app_client):
    b = make_buyer(auto=False)
    v = vendor(app_client, "Rejected Wording Private Limited", buyer=b.slug)
    req = _submit(v, "happy_nimbus")["request_id"]
    rv = as_user(app_client, b.reviewer_id)
    c = case(rv, req)
    assert rv.post(f"/api/review/requests/{req}/reject", json={
        "operation_id": op(), "expected_version": c["request"]["version"], "reason": "Test: closing this one",
        "vendor_explanation": "Closed for a wording test."}).status_code == 200
    s = v.post("/api/vendor/samples/happy_nimbus/load").json()
    r = v.post("/api/vendor/submit", json={"operation_id": op(), "form": s["form"],
                                           "documents": {k: d["document_id"] for k, d in s["documents"].items()}})
    assert r.status_code == 409 and "authorise a new application" not in r.text and "procurement team" in r.text
