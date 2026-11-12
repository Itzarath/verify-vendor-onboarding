"""Edge cases in verification and risk: the bank's holder name kept from the supplier, two-approver rules, renewed bank
letters, unknown taxpayer types, stale duplicate facts, RSA-PSS and unreadable signatures, bank-name matching, filing
counts, future-dated letters and downloads from an older screen.

API tests run on the real PostgreSQL test database with the test-only fixture reader. No live model calls.
"""

import datetime as dt
import json
import re

import httpx
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.serialization import pkcs7
from cryptography.x509.oid import NameOID
from sqlalchemy import select

from app.db import session_scope
from app.models import AuditEvent
from app.pdf.signature import read_signatures
from app.pipeline import master, risk
from app.pipeline.evidence import EvalInput, VerificationRec
from app.verification import bank

from .test_api_flow import case, op, run_worker_until_idle, vendor
from .test_auto_approval import as_user, make_buyer
from .test_verification_and_risk import _gst_checks, _gst_doc_with, _make_ready_high_risk, _signed_pdf
from .test_clearances_and_visibility import _submit
from .test_scenarios_logic import SCEN, evidence, policy


def test_the_supplier_run_log_never_shows_the_banks_holder_name(app_client):
    run_worker_until_idle(50)
    b = make_buyer(auto=False)
    v = vendor(app_client, "Run Log Privacy Private Limited", buyer=b.slug)
    s = v.post("/api/vendor/samples/happy_nimbus/load").json()
    form = s["form"]
    form["bank"]["account_number"] = "000412300099999"
    r = v.post("/api/vendor/submit", json={"operation_id": op(), "form": form,
                                           "documents": {k: d["document_id"] for k, d in s["documents"].items()}})
    assert r.status_code == 200, r.text
    run_worker_until_idle()
    run_id = r.json()["run_id"]
    seen = json.dumps(v.get(f"/api/runs/{run_id}/events").json()["events"])
    assert "RAVI" not in seen.upper() and "Risk:" not in seen and "someone else" not in seen
    assert "Bank account checked at the bank (SANDBOX penny drop)" in seen
    staff_seen = json.dumps(as_user(app_client, b.reviewer_id).get(f"/api/runs/{run_id}/events").json()["events"])
    assert "RAVI KUMAR SHARMA" in staff_seen and "Risk: high" in staff_seen  # reviewers still see everything


def test_two_required_approvers_always_stop_automatic_approval(app_client, monkeypatch):
    from app.models import Buyer, Policy
    from app.pipeline import runner

    run_worker_until_idle(50)
    b = make_buyer(auto=True)
    with session_scope() as db:  # overlapping thresholds: automatic up to medium, two people from medium
        pol = db.get(Policy, db.get(Buyer, b.buyer_id).active_policy_id)
        pol.document = {**pol.document, "approval": {**pol.document["approval"], "auto_approve_max_risk": "medium",
                                                     "second_approver_from_risk": "medium"}}
    monkeypatch.setattr(runner.risk_mod, "assess", lambda checks, form, today=None: {
        "score": 30, "tier": "medium", "flags": [{"weight": 30, "flag": "Test flag", "check": None}]})
    req = _submit(vendor(app_client, "Overlap Policy Private Limited", buyer=b.slug), "happy_nimbus")["request_id"]
    rv = as_user(app_client, b.reviewer_id)
    c = case(rv, req)
    assert c["request"]["state"] == "READY_FOR_DECISION"
    titles = [e["title"] for e in rv.get(f"/api/runs/{c['runs'][0]['id']}/events").json()["events"]]
    assert any("requires two people" in t for t in titles), titles


class _C:
    def __init__(self, key, status, evidence):
        self.key, self.status, self.evidence = key, status, evidence


def test_a_renewed_letter_for_the_same_account_is_not_a_bank_change():
    form, today = SCEN["happy_nimbus"]["form"], dt.date(2026, 11, 11)
    assert risk.assess([_C("change:bank", "info", {})], form, today=today)["score"] == 0
    changed = [_C("change:bank", "needs_review", {"changes": [{"field": "bank.account_number"}]})]
    assert risk.assess(changed, form, today=today)["flags"][0]["flag"].startswith("Bank details changed")
    owner_only = [_C("change:bank", "needs_review", {"changes": [{"field": "tax.pan"}]})]
    assert risk.assess(owner_only, form, today=today)["score"] == 0


def test_unknown_taxpayer_type_is_not_confirmed_credit_and_a_blank_type_keeps_the_earlier_one():
    s = SCEN["happy_nimbus"]
    form = s["form"]
    g = form["tax"]["gstin"]
    found = {"legal_name": form["business"]["legal_name"].upper(), "status": "Active"}
    recs = [VerificationRec(1, "GSTIN", g, "simulated_registry", "found",
                            {**found, "taxpayer_type": "Composition", "gstr3b_missed": 0, "gstr1_missed": 0}, "x", "system"),
            VerificationRec(2, "GSTIN", g, "simulated_registry", "found",
                            {**found, "gstr3b_missed": 1, "gstr1_missed": 0}, "x", "system")]
    checks = _gst_checks("happy_nimbus", verifications=recs)
    assert checks["verification:gst_taxpayer_type"].status == "info"  # still Composition after the correction
    inp = EvalInput(form=form, docs=evidence(s["documents"]), policy=policy(), verifications=recs)
    rec = master.build(inp, list(checks.values()), {"score": 0, "tier": "low"}, "VEN-1", "Buyer", "VOB-1")
    assert rec["gst"]["taxpayer_type"] == "Composition" and rec["gst"]["input_tax_credit"].startswith("Not available")
    # No type anywhere (no registry type, no certificate reading): credit is "not confirmed", never "available".
    plain = [VerificationRec(1, "GSTIN", g, "simulated_registry", "found", found, "x", "system")]
    docs = [d for d in evidence(s["documents"]) if d.slot != "gst_certificate"]
    inp = EvalInput(form=form, docs=docs, policy=policy(), verifications=plain)
    rec = master.build(inp, list(_gst_checks("happy_nimbus", verifications=plain).values()),
                       {"score": 0, "tier": "low"}, "VEN-1", "Buyer", "VOB-1")
    assert rec["gst"]["taxpayer_type"] is None and rec["gst"]["input_tax_credit"].startswith("Not confirmed")


def test_a_first_signoff_on_stale_duplicate_facts_is_refused(app_client):
    b, req, rv = _make_ready_high_risk(app_client)
    c = case(rv, req)
    _submit(vendor(app_client, "Same Account Later Private Limited", buyer=b.slug), "happy_nimbus")  # same account
    r = rv.post(f"/api/review/requests/{req}/approve", json={"operation_id": op(), "expected_version": c["request"]["version"],
                                                            "assessment_id": c["assessment"]["id"]})
    assert r.status_code == 409 and r.json()["error"] == "re_evaluating", r.text
    with session_scope() as db:
        assert db.scalar(select(AuditEvent).where(AuditEvent.request_id == req,
                                                  AuditEvent.event_type == "APPROVAL_FIRST_SIGNOFF")) is None


def _signed_pss_pdf() -> bytes:
    """_signed_pdf's layout, re-signed over the same bytes with RSA-PSS (PAdES)."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "DS GOODS AND SERVICES TAX NETWORK 1"),
                      x509.NameAttribute(NameOID.ORGANIZATION_NAME, "GOODS AND SERVICES TAX NETWORK")])
    now = dt.datetime.now(dt.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(77).not_valid_before(now - dt.timedelta(days=1)).not_valid_after(now + dt.timedelta(days=30))
            .sign(key, hashes.SHA256()))
    pdf = _signed_pdf()
    a, b_, c_, d_ = map(int, re.search(rb"/ByteRange\s*\[\s*(\d+)\s+(\d+)\s+(\d+)\s+(\d+)", pdf).groups())
    signed = pdf[a:a + b_] + pdf[c_:c_ + d_]
    pss = padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH)
    sig = (pkcs7.PKCS7SignatureBuilder().set_data(signed).add_signer(cert, key, hashes.SHA256(), rsa_padding=pss)
           .sign(serialization.Encoding.DER, [pkcs7.PKCS7Options.DetachedSignature, pkcs7.PKCS7Options.Binary]))
    hexsig = sig.hex().encode()
    room = c_ - b_ - 2
    return pdf[:b_ + 1] + hexsig + b"0" * (room - len(hexsig)) + pdf[c_ - 1:]


def test_pss_signatures_verify_and_an_unreadable_one_is_not_added_content():
    r = read_signatures(_signed_pss_pdf())
    assert [s["status"] for s in r["signatures"]] == ["valid"] and not r["changed_after_signing"]
    pdf = _signed_pdf()
    i = pdf.index(b"/Contents <") + len(b"/Contents <")
    garbled = pdf[:i] + b"zz" + pdf[i + 2:]  # the signature itself is unreadable; nothing was appended
    r = read_signatures(garbled)
    assert [s["status"] for s in r["signatures"]] == ["unreadable"] and r["changed_after_signing"] is False
    assert _gst_doc_with(r).status == "info"


def test_common_bank_names_and_distinct_banks():
    assert bank.same_bank("SBI", "STATE BANK OF INDIA", "SBIN")
    assert bank.same_bank("PNB", "Punjab National Bank", "PUNB") and bank.same_bank("BOB", "Bank of Baroda", "BARB")
    assert bank.same_bank("BOI", "Bank of India", "BKID") and bank.same_bank("HDFC Bank Ltd", "HDFC BANK", "HDFC")
    assert not bank.same_bank("Bank of India", "STATE BANK OF INDIA", "SBIN")
    assert not bank.same_bank("Bank of India", "Union Bank of India", "UBIN")
    assert not bank.same_bank("Indian Bank", "Indian Overseas Bank", "IOBA")
    assert bank.same_bank("Saraswat Bank", "Saraswat Co-operative Bank Ltd", "SRCB")  # not listed: leading words
    assert not bank.same_bank("Axis Bank", "HDFC Bank", "ZZZZ")


def test_a_reply_without_a_bank_record_is_unavailable(monkeypatch):
    from app.config import get_settings
    monkeypatch.setattr(get_settings(), "live_lookups", True)
    replies = iter([{}, {"Error": "x"}, [], None, "text", {"BANK": 12, "BRANCH": "X"},
                    {"BANK": "HDFC Bank", "BRANCH": ["x"]}])
    monkeypatch.setattr(httpx, "get", lambda url, **kw: httpx.Response(200, json=next(replies)))
    for _ in range(6):
        assert bank.lookup_ifsc("HDFC0000143").result == "unavailable"
    a = bank.lookup_ifsc("HDFC0000143")
    assert a.result == "found" and "BRANCH" not in a.observed  # a malformed field is dropped, not passed on


def test_only_the_payment_flags_may_be_true_or_false(monkeypatch):
    from app.config import get_settings
    monkeypatch.setattr(get_settings(), "live_lookups", True)
    reply = {"BANK": "HDFC Bank", "BRANCH": True, "CITY": True, "STATE": "KARNATAKA", "NEFT": True, "UPI": False}
    monkeypatch.setattr(httpx, "get", lambda url, **kw: httpx.Response(200, json=reply))
    a = bank.lookup_ifsc("HDFC0000143")
    assert a.result == "found" and a.observed == {"BANK": "HDFC Bank", "STATE": "KARNATAKA", "NEFT": True, "UPI": False}
    assert bank.branch_place(a.observed) == "Karnataka"


def test_a_stored_lookup_with_one_count_keeps_the_gap_and_never_means_all_filed():
    form = SCEN["happy_nimbus"]["form"]
    g = form["tax"]["gstin"]
    found = {"legal_name": form["business"]["legal_name"].upper(), "status": "Active"}

    def returns(**counts):
        recs = [VerificationRec(1, "GSTIN", g, "manual_registry", "found", {**found, **counts}, "x", "reviewer")]
        return _gst_checks("happy_nimbus", verifications=recs)["verification:gst_returns"]
    c = returns(gstr1_missed=2)
    assert c.status == "needs_review" and c.owner == "finance"
    assert "GSTR-1 not filed for 2" in c.reason and "GSTR-3B filing not recorded" in c.reason
    c = returns(gstr3b_missed=0)
    assert c.status == "needs_review" and "GSTR-1 filing not recorded" in c.reason
    assert returns(gstr3b_missed=0, gstr1_missed=0).status == "satisfied"


def _with_unreadable_later_signature(pdf: bytes) -> bytes:
    """An incremental update after a valid signature, carrying a signature that can not be read."""
    head = pdf + b"5 0 obj << /Type /Sig /ByteRange [0 XXXXXXXXXX YYYYYYYYYY ZZZZZZZZZZ] /Contents "
    tail = b" >> endobj\n%%EOF\n"
    b_, c_ = len(head), len(head) + 6
    return (head + b"<zz00>" + tail).replace(b"XXXXXXXXXX YYYYYYYYYY ZZZZZZZZZZ", f"{b_:<10} {c_:<10} {len(tail):<10}".encode())


def test_a_later_unreadable_signature_cannot_inherit_the_earlier_ones_authenticity():
    r = read_signatures(_with_unreadable_later_signature(_signed_pdf()))
    assert [s["status"] for s in r["signatures"]] == ["valid", "unreadable"] and r["changed_after_signing"] is True
    c = _gst_doc_with(r)
    assert c.status == "needs_review" and c.task_key == "reviewer:authenticity" and "added after" in c.reason


# ---------------------------------------------------------------------------- filing counts, future-dated letters and outdated downloads

def test_a_later_lookup_with_blank_counts_keeps_the_missed_returns():
    form = SCEN["happy_nimbus"]["form"]
    g = form["tax"]["gstin"]
    found = {"legal_name": form["business"]["legal_name"].upper(), "status": "Active"}

    def lookup(i, **extra):
        return VerificationRec(i, "GSTIN", g, "manual_registry", "found", {**found, **extra}, "x", "reviewer")
    gap, blank = lookup(1, gstr3b_missed=0, gstr1_missed=2), lookup(2, taxpayer_type="Regular")
    c = _gst_checks("happy_nimbus", verifications=[gap, blank])["verification:gst_returns"]
    assert c.status == "needs_review" and c.owner == "finance" and "GSTR-1 not filed for 2" in c.reason
    assert "filing counts from an earlier lookup" in c.reason
    # The same evidence as before the blank lookup, so a decision finance already took on it still applies.
    assert c.fingerprint == _gst_checks("happy_nimbus", verifications=[gap])["verification:gst_returns"].fingerprint
    # A lookup that records new counts does replace them.
    newer = [gap, blank, lookup(3, gstr3b_missed=0, gstr1_missed=0)]
    assert _gst_checks("happy_nimbus", verifications=newer)["verification:gst_returns"].status == "satisfied"


def test_a_bank_letter_dated_in_the_future_goes_to_a_reviewer():
    from .test_model_budget_and_form_checks import _bank_dated

    ahead = (dt.date.today() + dt.timedelta(days=90)).strftime("%d/%m/%Y")
    c = _bank_dated(ahead)["credibility:bank_proof_date"]
    assert c.status == "needs_review" and c.owner == "reviewer" and c.task_key == "reviewer:bank_proof_date"
    assert "after today" in c.reason
    tomorrow = (dt.date.today() + dt.timedelta(days=1)).strftime("%d/%m/%Y")
    assert _bank_dated(tomorrow)["credibility:bank_proof_date"].status == "satisfied"  # one day of time-zone slack


def test_a_download_from_an_older_screen_is_refused_and_the_file_names_its_snapshot(app_client):
    _, req, rv = _make_ready_high_risk(app_client)
    c = case(rv, req)
    v = c["request"]["version"]
    r = rv.get(f"/api/review/requests/{req}/vendor-master?version={v}&format=json")
    assert r.status_code == 200, r.text
    assert r.json()["snapshot"] == {"case_version": v, "revision": c["request"]["revision"],
                                    "assessment_id": c["assessment"]["id"]}
    r = rv.get(f"/api/review/requests/{req}/vendor-master?version={v - 1}&format=csv")
    assert r.status_code == 409 and r.json()["error"] == "stale_view", r.text
