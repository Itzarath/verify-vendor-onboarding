"""Honest tax and payment wording in the vendor master record, Composition filing, duplicate PAN/GSTIN within a buyer,
and bank changes accepted only with an independent confirmation, then approved by someone else.

API tests run on the real PostgreSQL test database with the test-only fixture reader. No live model calls.
"""

from app.pipeline import master
from app.pipeline.evidence import EvalInput, VerificationRec

from .test_api_flow import BANK_CONFIRMATION, case, op, run_worker_until_idle, vendor
from .test_auto_approval import as_user, make_buyer
from .test_verification_and_risk import _gst_checks
from .test_reviewer_judgements import _check
from .test_clearances_and_visibility import _approve, _submit
from .test_scenarios_logic import SCEN, evidence, policy, registry


class _C:
    def __init__(self, key, status, evidence):
        self.key, self.status, self.evidence, self.reason = key, status, evidence, ""


# ---------------------------------------------------------------------------- the vendor master's tax wording

def test_the_vendor_master_does_not_overstate_credit_terms_or_payment_readiness():
    s = SCEN["happy_nimbus"]
    inp = EvalInput(form=s["form"], docs=evidence(s["documents"]), policy=policy(), verifications=registry(s["form"]))
    rec = master.build(inp, list(_gst_checks("happy_nimbus").values()), {"score": 0, "tier": "low"}, "VEN-1", "B", "VOB-1")
    assert rec["gst"]["input_tax_credit"].startswith("Depends on each invoice") and "not assessed" in rec["gst"]["input_tax_credit"]
    m = rec["msme"]
    assert m["max_days_with_written_agreement"] == 45 and m["max_days_without_agreement"] == 15
    assert m["agreed_payment_terms"].startswith("not recorded") and "payment_terms_max_days" not in rec
    # Payments stay on hold until the bank confirms the holder through a live provider; a sandbox match never releases them.
    assert rec["payment_setup"]["status"] == "hold"
    sandbox = _C("verification:bank_account", "satisfied", {"name_match_result": "MATCH", "method": "penny_drop_sandbox"})
    rec = master.build(inp, [sandbox], {"score": 0, "tier": "low"}, "VEN-1", "B", "VOB-1")
    assert rec["payment_setup"]["status"] == "hold" and "SANDBOX" in rec["payment_setup"]["hold_reasons"]
    live = _C("verification:bank_account", "satisfied", {"name_match_result": "MATCH", "method": "provider_live"})
    assert master.build(inp, [live], {"score": 0, "tier": "low"}, "VEN-1", "B", "VOB-1")["payment_setup"]["status"] == "ready"


# ---------------------------------------------------------------------------- Composition filing

def test_no_missed_monthly_returns_proves_nothing_for_a_composition_supplier():
    form = SCEN["happy_nimbus"]["form"]
    g = form["tax"]["gstin"]
    found = {"legal_name": form["business"]["legal_name"].upper(), "status": "Active", "gstr3b_missed": 0, "gstr1_missed": 0}

    def returns(tt):
        recs = [VerificationRec(1, "GSTIN", g, "manual_registry", "found", {**found, "taxpayer_type": tt}, "x", "reviewer")]
        return _gst_checks("happy_nimbus", verifications=recs)["verification:gst_returns"]
    c = returns("Composition")
    assert c.status == "info" and "Filing not assessed" in c.reason and "CMP-08" in c.reason
    assert "filed for every month" not in c.reason
    assert returns("Regular").status == "satisfied"


# ---------------------------------------------------------------------------- duplicate PAN / GSTIN

def _other_account(v, key="happy_nimbus", account="000412300011111"):
    """The same legal identity with a different bank account (so only the identity is shared)."""
    s = v.post(f"/api/vendor/samples/{key}/load").json()
    s["form"]["bank"]["account_number"] = account
    r = v.post("/api/vendor/submit", json={"operation_id": op(), "form": s["form"],
                                           "documents": {k: d["document_id"] for k, d in s["documents"].items()}})
    assert r.status_code == 200, r.text
    run_worker_until_idle()
    return r.json()["request_id"]


def test_a_second_vendor_with_the_same_pan_goes_to_a_reviewer_within_the_buyer_only(app_client):
    run_worker_until_idle(50)
    b = make_buyer(auto=False)
    rv = as_user(app_client, b.reviewer_id)
    first = _submit(vendor(app_client, "Identity First Private Limited", buyer=b.slug), "happy_nimbus")["request_id"]
    assert _check(case(rv, first), "credibility:duplicate_identity")["status"] == "satisfied"
    second = _other_account(vendor(app_client, "Identity Second Private Limited", buyer=b.slug))
    c = _check(case(rv, second), "credibility:duplicate_identity")
    assert c["status"] == "needs_review" and c["owner"] == "reviewer" and "same PAN and GSTIN" in c["reason"]
    assert "CGST s.25" in c["reason"]
    assert _check(case(rv, second), "credibility:duplicate_bank")["status"] == "satisfied"  # only the identity is shared
    # Another buyer's records are never compared.
    other = make_buyer(auto=False)
    elsewhere = _submit(vendor(app_client, "Identity Elsewhere Private Limited", buyer=other.slug), "happy_nimbus")["request_id"]
    assert _check(case(as_user(app_client, other.reviewer_id), elsewhere), "credibility:duplicate_identity")["status"] == "satisfied"
    # The first case's assessment saw no duplicate; approving it now is refused and it is checked again.
    r = _approve(rv, first)
    assert r.status_code == 409 and r.json()["error"] == "re_evaluating", r.text
    run_worker_until_idle()
    assert _check(case(rv, first), "credibility:duplicate_identity")["status"] == "needs_review"


def test_a_duplicate_identity_decision_on_an_outdated_list_is_refused(app_client):
    run_worker_until_idle(50)
    b = make_buyer(auto=False)
    rv = as_user(app_client, b.reviewer_id)
    _submit(vendor(app_client, "Identity List A Private Limited", buyer=b.slug), "happy_nimbus")
    req = _other_account(vendor(app_client, "Identity List B Private Limited", buyer=b.slug))
    c = case(rv, req)
    seen = _check(c, "credibility:duplicate_identity")
    _other_account(vendor(app_client, "Identity List C Private Limited", buyer=b.slug), account="000412300022222")
    r = rv.post(f"/api/review/requests/{req}/resolve", json={
        "operation_id": op(), "expected_version": c["request"]["version"], "check_key": "credibility:duplicate_identity",
        "fingerprint": seen["evidence"]["fingerprint"], "outcome": "accepted", "note": "Group company, checked"})
    assert r.status_code == 409 and r.json()["error"] == "re_evaluating", r.text
    run_worker_until_idle()
    assert len(_check(case(rv, req), "credibility:duplicate_identity")["evidence"]["other_vendor_ids"]) == 2


# ---------------------------------------------------------------------------- bank-change confirmation

def _bank_change_case(app_client):
    run_worker_until_idle(50)
    b = make_buyer(auto=False)
    v = vendor(app_client, "Confirmed Bank Change LLP", buyer=b.slug)
    req = _submit(v, "bank_change_rev1")["request_id"]
    _submit(v, "bank_change_rev2", based_on=1)
    return b, req, as_user(app_client, b.reviewer_id), as_user(app_client, b.finance_id)


def _accept_bank_change(fin, req, confirmation):
    c = case(fin, req)
    x = _check(c, "change:bank")
    return fin.post(f"/api/review/requests/{req}/resolve", json={
        "operation_id": op(), "expected_version": c["request"]["version"], "check_key": "change:bank",
        "fingerprint": x["evidence"]["fingerprint"], "outcome": "accepted", "note": "New account confirmed",
        "confirmation": confirmation})


def test_a_bank_change_is_accepted_only_with_an_independent_confirmation_and_approved_by_someone_else(app_client):
    _, req, rv, fin = _bank_change_case(app_client)
    assert _check(case(rv, req), "change:bank")["status"] == "needs_review"
    for bad in (None, {**BANK_CONFIRMATION, "contact_source": "this_application"}, {**BANK_CONFIRMATION, "confirmed": False},
                {**BANK_CONFIRMATION, "contact_person": ""}):
        r = _accept_bank_change(fin, req, bad)
        assert r.status_code == 400 and r.json()["error"] == "confirmation_required", (bad, r.text)
    assert _accept_bank_change(fin, req, BANK_CONFIRMATION).status_code == 200
    run_worker_until_idle()
    c = case(rv, req)
    assert c["request"]["state"] == "READY_FOR_DECISION"
    bank = _check(c, "change:bank")
    assert "confirmed by call-back with A. Rao" in bank["reason"]
    assert bank["evidence"]["resolution"]["confirmation"]["contact_source"] == "contract"
    assert c["vendor_master"]["payment_setup"]["bank_change_confirmation"]["method"] == "call_back"
    assert c["resolutions"][-1]["confirmation"]["method_label"] == "call-back"
    # Whoever confirmed the change doesn't also approve the vendor.
    r = _approve(fin, req)
    assert r.status_code == 409 and r.json()["error"] == "bank_change_same_person", r.text
    assert _approve(rv, req).status_code == 200
