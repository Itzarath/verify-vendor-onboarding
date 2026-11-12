"""Bank-change confirmations: bound to their baseline, kept through unrelated corrections, approvable by two procurement
reviewers at high risk; unconfirmed old dates go to a reviewer; acceptances recorded before confirmations existed are
confirmed again; suppliers with an open application stay on the sign-in list; a future-dated proof is not called old.

API tests run on the real PostgreSQL test database with the test-only fixture reader. No live model calls. The review box being keyed by the evidence fingerprint is a frontend state fix, covered by the type-checked build.
"""

import copy

from sqlalchemy import update

from app.db import session_scope
from app.models import Request, ReviewResolution, User
from app.pipeline import master, risk
from app.pipeline.checks import run_checks
from app.pipeline.compare import compare
from app.pipeline.evidence import EvalInput, ResolutionRec
from app.pipeline.runner import enqueue_run

from .test_api_flow import BANK_CONFIRMATION, case, new_client, op, run_worker_until_idle, vendor
from .test_auto_approval import as_user, make_buyer
from .test_reviewer_judgements import _check
from .test_clearances_and_visibility import _approve, _submit
from .test_scenarios_logic import SCEN, evidence, first_submission, policy, registry

S1, S2 = SCEN["bank_change_rev1"], SCEN["bank_change_rev2"]  # account A, then account B


def _bank_change(form, baselines, resolutions=(), origin=None, docs=None):
    inp = EvalInput(form=form, docs=evidence(docs or S2["documents"]), policy=policy(), verifications=registry(form),
                    revision_number=4, revision_id=4, baselines=baselines, bank_origin=origin)
    inp.resolutions = list(resolutions)
    return inp, {c.key: c for c in run_checks(inp, compare(inp))}


def _accepted(fp, revision_id=2):
    return ResolutionRec("change:bank", fp, "accepted", "Confirmed", "Finance", revision_id=revision_id,
                         confirmation={"method": "call_back", "method_label": "call-back", "contact_source": "contract",
                                       "contact_source_label": "contact in the signed contract", "contact_person": "A. Rao",
                                       "confirmed": True})


# ---------------------------------------------------------------------------- returning to an account confirmed earlier

def test_going_back_to_an_account_confirmed_earlier_needs_a_new_confirmation():
    # Revision 2 (A → B) is confirmed against the first submission.
    _, c2 = _bank_change(S2["form"], first_submission(S1))
    fp2 = c2["change:bank"].fingerprint
    # Revision 4 returns to B after finance confirmed revision 3 (back to A): the baseline is now revision 3.
    docs_a = {d.slot: d.sha256 for d in evidence(S1["documents"])}
    base = {"bank": (S1["form"], "revision 3, as cleared by finance", docs_a), "identity": (S1["form"], "revision 1")}
    _, c4 = _bank_change(S2["form"], base, [_accepted(fp2)])
    assert c4["change:bank"].status == "needs_review" and c4["change:bank"].fingerprint != fp2
    # Re-evaluating the confirmed revision itself (same baseline) still finds its confirmation.
    _, again = _bank_change(S2["form"], first_submission(S1), [_accepted(fp2)])
    assert again["change:bank"].status == "satisfied"


# ---------------------------------------------------------------------------- unrelated corrections keep the confirmation

def test_an_unrelated_correction_keeps_the_confirmed_bank_change_in_risk_and_the_record():
    _, c2 = _bank_change(S2["form"], first_submission(S1))
    form3 = copy.deepcopy(S2["form"])
    form3.setdefault("contact", {})["phone"] = "9800000001"  # only the phone changes in revision 3
    docs_b = {d.slot: d.sha256 for d in evidence(S2["documents"])}
    base = {"bank": (S2["form"], "revision 2, as cleared by finance", docs_b), "identity": (S1["form"], "revision 1")}
    inp, c3 = _bank_change(form3, base, [_accepted(c2["change:bank"].fingerprint)],
                           origin=(S1["form"], "the first submission (revision 1)"))
    bank = c3["change:bank"]
    assert bank.status == "satisfied" and not bank.blocking
    assert "confirmed by finance" in bank.title.lower() and "by call-back" in bank.reason
    assert any(c["field"] == "bank.account_number" for c in bank.evidence["changes"])
    flags = risk.assess(list(c3.values()), form3)["flags"]
    assert any(f["flag"].startswith("Bank details changed") for f in flags)  # still counts, so the tier holds
    rec = master.build(inp, list(c3.values()), {"score": 0, "tier": "low"}, "VEN-1", "B", "VOB-1")
    assert rec["payment_setup"]["bank_change_confirmation"]["method"] == "call_back"


# ---------------------------------------------------------------------------- two procurement reviewers

def test_a_high_risk_bank_change_can_be_approved_by_two_procurement_reviewers(app_client, monkeypatch):
    from app.pipeline import runner

    run_worker_until_idle(50)
    monkeypatch.setattr(runner.risk_mod, "assess", lambda checks, form, today=None: {
        "score": 60, "tier": "high", "flags": [{"weight": 60, "flag": "Test flag", "check": None}]})
    b = make_buyer(auto=False)
    with session_scope() as db:
        second = User(email=f"rv2-{b.slug}@test.example", display_name="Second reviewer", role="reviewer",
                      buyer_id=b.buyer_id)
        db.add(second)
        db.flush()
        second_id = second.id
    v = vendor(app_client, "High Risk Bank Change LLP", buyer=b.slug)
    req = _submit(v, "bank_change_rev1")["request_id"]
    _submit(v, "bank_change_rev2", based_on=1)
    rv, fin, rv2 = (as_user(app_client, i) for i in (b.reviewer_id, b.finance_id, second_id))
    c = case(fin, req)
    x = _check(c, "change:bank")
    assert fin.post(f"/api/review/requests/{req}/resolve", json={
        "operation_id": op(), "expected_version": c["request"]["version"], "check_key": "change:bank",
        "fingerprint": x["evidence"]["fingerprint"], "outcome": "accepted", "note": "Confirmed",
        "confirmation": BANK_CONFIRMATION}).status_code == 200
    run_worker_until_idle()
    assert _approve(fin, req).json()["error"] == "bank_change_same_person"
    assert _approve(rv, req).json().get("awaiting_second_approval") is True
    assert _approve(rv2, req).status_code == 200


def test_each_buyer_has_two_procurement_reviewers(app_client):
    staff = app_client.get("/api/auth/personas").json()["staff"]
    for buyer in ("Meridian", "Kestrel"):
        assert sum(1 for s in staff if s["role"] == "reviewer" and s["buyer"].startswith(buyer)) >= 2


# ---------------------------------------------------------------------------- dates not confirmed from the text

def test_an_old_date_not_confirmed_from_the_text_goes_to_a_reviewer_first():
    s = SCEN["happy_nimbus"]

    def dated(value, provenance):
        docs = evidence(s["documents"])
        for d in docs:
            if d.slot == "bank_proof":
                f = d.reading.fields["document_date"]
                d.reading.fields["document_date"] = f.model_copy(update={"value": value, "normalized": value,
                                                                          "provenance": provenance})
        inp = EvalInput(form=s["form"], docs=docs, policy=policy(), verifications=[])
        return {c.key: c for c in run_checks(inp, compare(inp))}["credibility:bank_proof_date"]
    for prov in ("quote_not_in_text", "visual_unconfirmed"):
        c = dated("02/11/2020", prov)
        assert c.status == "needs_review" and c.owner == "reviewer" and c.task_key == "reviewer:bank_proof_date", prov
    c = dated("02/11/2020", "text_layer_verified")  # a genuinely old, grounded date still goes to the supplier
    assert c.status == "not_satisfied" and c.owner == "vendor"


# ---------------------------------------------------------------------------- acceptances recorded before confirmations existed

def test_an_open_case_with_an_acceptance_from_before_confirmations_is_confirmed_again(app_client):
    run_worker_until_idle(50)
    b = make_buyer(auto=False)
    v = vendor(app_client, "Legacy Acceptance LLP", buyer=b.slug)
    req = _submit(v, "bank_change_rev1")["request_id"]
    _submit(v, "bank_change_rev2", based_on=1)
    fin = as_user(app_client, b.finance_id)
    c = case(fin, req)
    x = _check(c, "change:bank")
    assert fin.post(f"/api/review/requests/{req}/resolve", json={
        "operation_id": op(), "expected_version": c["request"]["version"], "check_key": "change:bank",
        "fingerprint": x["evidence"]["fingerprint"], "outcome": "accepted", "note": "Confirmed",
        "confirmation": BANK_CONFIRMATION}).status_code == 200
    run_worker_until_idle()
    assert case(fin, req)["request"]["state"] == "READY_FOR_DECISION"
    with session_scope() as db:  # what an acceptance recorded before migration 0005 looks like
        db.execute(update(ReviewResolution).where(ReviewResolution.request_id == req).values(confirmation=None))
        r = db.get(Request, req)
        enqueue_run(db, r, r.current_revision_id, "upgrade")
    run_worker_until_idle()
    c = case(fin, req)
    assert c["request"]["state"] == "ACTION_REQUIRED" and _check(c, "change:bank")["status"] == "needs_review"


# ---------------------------------------------------------------------------- suppliers with an open application

def test_a_supplier_with_an_open_application_stays_on_the_sign_in_list(app_client):
    run_worker_until_idle(50)
    b = make_buyer(auto=False)
    old = _submit(vendor(app_client, "Waiting Supplier LLP", buyer=b.slug), "bank_change_rev1")["reference"]
    for i in range(41):  # newer sign-ups that never submit
        vendor(app_client, f"Newer Sign Up {i} Private Limited", buyer=b.slug)
    rows = new_client(app_client).get("/api/auth/personas").json()["vendors"]
    assert any(r["application"] == old for r in rows)


# ---------------------------------------------------------------------------- risk wording for a future-dated proof

class _Chk:
    def __init__(self, key, status, evidence):
        self.key, self.status, self.evidence = key, status, evidence


def test_a_refused_future_dated_bank_proof_is_not_called_old_in_the_risk_reasons():
    import datetime as dt

    today = dt.date(2026, 11, 12)
    form = SCEN["happy_nimbus"]["form"]

    def flag(document_date):
        c = _Chk("credibility:bank_proof_date", "not_satisfied", {"document_date": document_date})
        return [f["flag"] for f in risk.assess([c], form, today=today)["flags"] if f["check"] == "credibility:bank_proof_date"]
    assert flag("2027-01-01") == ["The bank proof is dated in the future"]
    assert flag("2026-01-01") == ["The bank proof is more than 6 months old"]
