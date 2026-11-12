"""Verification at the source, and risk.

Document authenticity (digital signatures), bank details at the source (IFSC directory + sandbox penny drop), the GST
compliance profile, risk scoring with four-eyes approval, and the ERP vendor master record. No network: live lookups
are switched off in tests (conftest) and the live IFSC path is exercised with a fake HTTP response.
"""

import datetime as dt
import json
from dataclasses import replace

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import pkcs7
from cryptography.x509.oid import NameOID
from sqlalchemy import select

from app.db import session_scope
from app.models import Assessment, AuditEvent, Request
from app.pdf.signature import read_signatures
from app.pipeline import master, risk
from app.pipeline.checks import run_checks
from app.pipeline.compare import compare
from app.pipeline.evidence import EvalInput, VerificationRec
from app.verification import bank

from .test_api_flow import case, op, run_worker_until_idle, vendor
from .test_auto_approval import as_user, make_buyer
from .test_clearances_and_visibility import _submit
from .test_scenarios_logic import SCEN, evidence, policy, registry


# ---------------------------------------------------------------------------- a signed PDF, made here

def _signed_pdf(org: str = "GOODS AND SERVICES TAX NETWORK", cn: str = "DS GOODS AND SERVICES TAX NETWORK 1") -> bytes:
    """A tiny PDF signed like a portal certificate (detached CMS over the ByteRange), with a throwaway test key."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn), x509.NameAttribute(NameOID.ORGANIZATION_NAME, org)])
    issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Test Sub CA for Document Signer")])
    now = dt.datetime.now(dt.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(issuer).public_key(key.public_key())
            .serial_number(4242).not_valid_before(now - dt.timedelta(days=1)).not_valid_after(now + dt.timedelta(days=365))
            .sign(key, hashes.SHA256()))
    placeholder = b"0" * 8192
    head = (b"%PDF-1.4\n1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj\n2 0 obj << /Type /Pages /Kids [3 0 R] /Count 1 >> "
            b"endobj\n3 0 obj << /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] >> endobj\n4 0 obj << /Type /Sig "
            b"/Filter /Adobe.PPKLite /SubFilter /adbe.pkcs7.detached /ByteRange [0 XXXXXXXXXX YYYYYYYYYY ZZZZZZZZZZ] "
            b"/Contents <")
    tail = b"> >> endobj\ntrailer << /Root 1 0 R >>\n%%EOF\n"
    data = head + placeholder + tail
    b_ = len(head) - 1  # up to (not including) '<'
    c_ = len(head) + len(placeholder) + 1  # just after '>'
    d_ = len(data) - c_
    data = data.replace(b"0 XXXXXXXXXX YYYYYYYYYY ZZZZZZZZZZ", f"0 {b_:<10} {c_:<10} {d_:<10}".encode())
    signed = data[:b_] + data[c_:]
    sig = (pkcs7.PKCS7SignatureBuilder().set_data(signed).add_signer(cert, key, hashes.SHA256())
           .sign(serialization.Encoding.DER, [pkcs7.PKCS7Options.DetachedSignature, pkcs7.PKCS7Options.Binary]))
    hexsig = sig.hex().encode()
    assert len(hexsig) <= len(placeholder)
    return data[:b_ + 1] + hexsig + placeholder[len(hexsig):] + data[c_ - 1:]


def test_a_signature_verifies_and_any_edit_breaks_it():
    pdf = _signed_pdf()
    r = read_signatures(pdf)
    assert [s["status"] for s in r["signatures"]] == ["valid"] and not r["changed_after_signing"]
    assert r["signatures"][0]["signer"]["o"] == "GOODS AND SERVICES TAX NETWORK"
    i = pdf.index(b"MediaBox [0 0 200") + 15  # change "200" to "201" inside the signed bytes
    edited = pdf[:i] + b"1" + pdf[i + 1:]
    assert [s["status"] for s in read_signatures(edited)["signatures"]] == ["broken"]
    assert read_signatures(pdf + b"5 0 obj << /Annot 1 >> endobj\n")["changed_after_signing"] is True
    assert read_signatures(b"%PDF-1.4\n%%EOF\n") == {"signatures": [], "changed_after_signing": False, "reader": 3}


def _gst_doc_with(sigs: dict):
    s = SCEN["happy_nimbus"]
    docs = [replace(d, signatures=sigs) if d.slot == "gst_certificate" else d for d in evidence(s["documents"])]
    inp = EvalInput(form=s["form"], docs=docs, policy=policy(), verifications=registry(s["form"]))
    return {c.key: c for c in run_checks(inp, compare(inp))}["authenticity:gst_certificate"]


def test_certificate_authenticity_outcomes():
    ok = read_signatures(_signed_pdf())
    c = _gst_doc_with(ok)
    assert c.status == "satisfied" and "unchanged since signing" in c.reason and "CCA India root" in c.reason
    broken = {**ok, "signatures": [{**ok["signatures"][0], "status": "broken"}]}
    c = _gst_doc_with(broken)
    assert c.status == "needs_review" and c.owner == "reviewer" and c.task_key == "reviewer:authenticity"
    assert c.severity == "high" and c.vendor_text is None  # the vendor is not told why (no tip-off)
    c = _gst_doc_with({**ok, "changed_after_signing": True})
    assert c.status == "needs_review" and "added after" in c.reason
    c = _gst_doc_with(read_signatures(_signed_pdf(org="SOME PRINT SHOP", cn="Some Print Shop")))
    assert c.status == "needs_review" and "not the authority" in c.reason
    c = _gst_doc_with({"signatures": [], "changed_after_signing": False})
    assert c.status == "info" and not c.blocking and "no digital signature" in c.reason


def test_a_signed_upload_is_checked_at_intake(app_client):
    v = vendor(app_client, "Signature Intake Private Limited")
    r = v.post("/api/vendor/documents", files={"file": ("gst.pdf", _signed_pdf(), "application/pdf")})
    if r.status_code == 400:
        pytest.skip("the hand-made PDF has no renderable pages for intake: " + r.text)
    assert r.status_code == 200, r.text
    from app.models import Document
    with session_scope() as db:
        doc = db.get(Document, r.json()["document_id"])
        assert doc.inspection["signatures"]["signatures"][0]["status"] == "valid"


# ---------------------------------------------------------------------------- bank details at the source

def _bank_checks(form: dict, ifsc_answer=None, account_answer=None) -> dict:
    s = SCEN["happy_nimbus"]
    b = form["bank"]
    acct, ifsc = b["account_number"].replace(" ", ""), b["ifsc"].upper()
    recs = list(registry(form))
    a = ifsc_answer or bank.lookup_ifsc(ifsc)
    recs.append(VerificationRec(90, "IFSC", ifsc, a.method, a.result, a.observed, a.source, "system"))
    a = account_answer or bank.penny_drop(acct, ifsc)
    recs.append(VerificationRec(91, "BANK_ACCOUNT", f"{acct}|{ifsc}", a.method, a.result, a.observed, a.source, "system"))
    inp = EvalInput(form=form, docs=evidence(s["documents"]), policy=policy(), verifications=recs)
    return {c.key: c for c in run_checks(inp, compare(inp))}


def _form(**bank_fields) -> dict:
    f = json.loads(json.dumps(SCEN["happy_nimbus"]["form"]))
    f["bank"].update(bank_fields)
    return f


def test_the_bank_confirms_the_sample_account_holder():
    checks = _bank_checks(_form())
    assert checks["verification:bank_account"].status == "satisfied"
    assert checks["verification:bank_account"].evidence["name_match_result"] == "MATCH"
    assert checks["verification:bank_branch"].status == "satisfied"
    assert "SIMULATED" in checks["verification:bank_branch"].reason


def test_an_account_the_bank_says_belongs_to_someone_else_goes_back_to_the_vendor():
    checks = _bank_checks(_form(account_number="000412300099999"))
    c = checks["verification:bank_account"]
    assert c.status == "not_satisfied" and c.task_key == "vendor:bank_details" and c.severity == "high"
    assert c.evidence["name_at_bank"] == "RAVI KUMAR SHARMA" and c.evidence["name_match_result"] == "NO_MATCH"
    assert "RAVI" not in c.vendor_text  # the bank's name for the account is not shown to the applicant
    assert checks["verification:bank_account"].evidence["account"].startswith("••")


def test_an_account_that_does_not_exist_and_a_real_unknown_account():
    c = _bank_checks(_form(account_number="000412300000000"))["verification:bank_account"]
    assert c.status == "not_satisfied" and "does not exist" in c.vendor_text
    c = _bank_checks(_form(account_number="111122223333"))["verification:bank_account"]
    assert c.status == "info" and not c.blocking and "SANDBOX" in c.reason


def test_a_partly_matching_name_goes_to_finance():
    ans = bank.Answer("valid", {"status": "VALID", "name_at_bank": "NIMBUS LEDGER SOLUTIONS"}, "penny_drop_sandbox", "x")
    form = _form(account_holder="Nimbus Ledger Software Solutions Private Limited")
    c = _bank_checks(form, account_answer=ans)["verification:bank_account"]
    assert c.status == "needs_review" and c.owner == "finance" and c.task_key == "finance:bank_account"


def test_ifsc_directory_live_lookup_and_mismatches(monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "live_lookups", True)
    calls = []

    def fake_get(url, **kw):
        calls.append(url)
        if url.endswith("HDFC0000143"):
            return httpx.Response(200, json={"BANK": "HDFC Bank", "BRANCH": "Crawford Market", "CITY": "MUMBAI",
                                             "STATE": "MAHARASHTRA", "IFSC": "HDFC0000143"})
        if url.endswith("HDFC0009999"):
            return httpx.Response(404, json="Not Found")
        raise httpx.ConnectError("offline")
    monkeypatch.setattr(httpx, "get", fake_get)
    a = bank.lookup_ifsc("HDFC0000143")
    assert a.result == "found" and a.observed["BANK"] == "HDFC Bank" and a.method == "ifsc_directory"
    assert bank.lookup_ifsc("HDFC0009999").result == "not_found"
    assert bank.lookup_ifsc("ICIC0001234").result == "unavailable"
    assert bank.lookup_ifsc("DECB0000412").method == "demo_bank_directory"  # fictional banks never go to the network
    assert len(calls) == 3
    c = _bank_checks(_form(ifsc="HDFC0000143", bank_name="ICICI Bank"), ifsc_answer=a)["verification:bank_branch"]
    assert c.status == "not_satisfied" and "belongs to HDFC Bank" in c.vendor_text
    c = _bank_checks(_form(ifsc="HDFC0000143", bank_name="HDFC Bank Ltd"), ifsc_answer=a)["verification:bank_branch"]
    assert c.status == "satisfied" and "live" in c.reason
    c = _bank_checks(_form(), ifsc_answer=bank.Answer("not_found", None, "ifsc_directory", "x"))["verification:bank_branch"]
    assert c.status == "not_satisfied" and "not a bank branch" in c.vendor_text
    c = _bank_checks(_form(), ifsc_answer=bank.Answer("unavailable", None, "ifsc_directory", "x"))["verification:bank_branch"]
    assert c.status == "info" and not c.blocking


def test_bank_lookups_are_recorded_once_per_request(app_client):
    run_worker_until_idle(50)
    b = make_buyer(auto=False)
    req = _submit(vendor(app_client, "Bank Lookup Once Private Limited", buyer=b.slug), "happy_nimbus")["request_id"]
    from app.models import Verification
    with session_scope() as db:
        rows = [(v.identifier_type, v.method, v.result) for v in db.scalars(
            select(Verification).where(Verification.request_id == req, Verification.identifier_type != "GSTIN"))]
    assert rows == [("IFSC", "demo_bank_directory", "found"), ("BANK_ACCOUNT", "penny_drop_sandbox", "valid")]
    rv = as_user(app_client, b.reviewer_id)
    c = case(rv, req)
    r = rv.post(f"/api/review/requests/{req}/retry", json={"operation_id": op(), "expected_version": c["request"]["version"]})
    run_worker_until_idle(50)
    with session_scope() as db:
        n = len(db.scalars(select(Verification).where(Verification.request_id == req, Verification.identifier_type != "GSTIN")).all())
    assert n == 2  # conclusive answers are not looked up again


# ---------------------------------------------------------------------------- GST compliance profile

def _gst_checks(key: str, verifications=None) -> dict:
    s = SCEN[key]
    inp = EvalInput(form=s["form"], docs=evidence(s["documents"]), policy=policy(),
                    verifications=verifications if verifications is not None else registry(s["form"]))
    return {c.key: c for c in run_checks(inp, compare(inp))}


def test_unfiled_gst_returns_go_to_finance_and_complete_filing_passes():
    c = _gst_checks("director_pan")["verification:gst_returns"]
    assert c.status == "needs_review" and c.owner == "finance" and c.task_key == "finance:gst_returns"
    assert "2026-04" in c.reason and "Rule 37A" in c.reason
    ok = _gst_checks("happy_nimbus")
    assert ok["verification:gst_returns"].status == "satisfied"
    assert ok["verification:gst_taxpayer_type"].status == "satisfied"


def test_a_composition_taxpayer_is_noted_not_blocked():
    form = SCEN["happy_nimbus"]["form"]
    g = form["tax"]["gstin"]
    obs = {"legal_name": form["business"]["legal_name"].upper(), "status": "Active", "taxpayer_type": "Composition",
           "gstr3b_missed": 0, "gstr1_missed": 0}
    rec = VerificationRec(1, "GSTIN", g, "manual_registry", "found", obs, "GST portal", "reviewer")
    checks = _gst_checks("happy_nimbus", [rec])
    assert checks["verification:gst_taxpayer_type"].status == "info"
    assert "no input tax credit" in checks["verification:gst_taxpayer_type"].reason
    # GSTR-1/3B don't apply to a Composition taxpayer, so zero missed is a note, not a pass.
    assert checks["verification:gst_returns"].status == "info"
    assert "Filing not assessed" in checks["verification:gst_returns"].reason


def test_a_reviewer_records_the_taxpayer_type_and_filing_gaps(app_client):
    run_worker_until_idle(50)
    b = make_buyer(auto=False)
    req = _submit(vendor(app_client, "Manual GST Profile Private Limited", buyer=b.slug), "happy_nimbus")["request_id"]
    rv = as_user(app_client, b.reviewer_id)
    c = case(rv, req)
    f = SCEN["happy_nimbus"]["form"]
    body = {"operation_id": op(), "expected_version": c["request"]["version"], "identifier": f["tax"]["gstin"],
            "result": "found", "legal_name": f["business"]["legal_name"], "status": "Active",
            "taxpayer_type": "regular", "gstr3b_missed": 2, "gstr1_missed": 0}
    assert rv.post(f"/api/review/requests/{req}/verification", json={**body, "gstr3b_missed": 13,
                                                                      "operation_id": op()}).status_code == 400
    # One count alone would hide known gaps or claim "all filed": both or neither.
    for k in ("gstr3b_missed", "gstr1_missed"):
        r = rv.post(f"/api/review/requests/{req}/verification", json={**body, k: None, "operation_id": op()})
        assert r.status_code == 400 and r.json()["error"] == "incomplete_filing_counts", r.text
    r = rv.post(f"/api/review/requests/{req}/verification", json=body)
    assert r.status_code == 200, r.text
    run_worker_until_idle(50)
    c = case(rv, req)
    checks = {x["key"]: x for x in c["assessment"]["checks"]}
    assert checks["verification:gst_returns"]["status"] == "needs_review"
    assert checks["verification:gst_taxpayer_type"]["status"] == "satisfied"


# ---------------------------------------------------------------------------- risk

def test_risk_flags_and_tiers():
    s = SCEN["happy_nimbus"]
    checks = list(_gst_checks("happy_nimbus").values())
    r = risk.assess(checks, s["form"], today=dt.date(2026, 11, 11))
    assert r == {"score": 0, "tier": "low", "flags": []}
    young = json.loads(json.dumps(s["form"]))
    young["business"]["incorporation_date"] = "01/08/2026"
    r = risk.assess(checks, young, today=dt.date(2026, 11, 11))
    assert r["score"] == 20 and r["tier"] == "low" and "less than a year" in r["flags"][0]["flag"]
    r = risk.assess(list(_gst_checks("director_pan").values()), SCEN["director_pan"]["form"], today=dt.date(2026, 11, 11))
    assert any("GSTR-3B" in f["flag"] for f in r["flags"])
    bad = list(_bank_checks(_form(account_number="000412300099999")).values())
    r = risk.assess(bad, s["form"], today=dt.date(2026, 11, 11))
    assert r["tier"] == "high" and r["flags"][0]["weight"] == 50  # either strong fraud signal alone is high risk
    assert risk.tier(49) == "medium" and risk.tier(50) == "high"


def _make_ready_high_risk(app_client):
    run_worker_until_idle(50)
    b = make_buyer(auto=False)
    req = _submit(vendor(app_client, "Four Eyes Private Limited", buyer=b.slug), "happy_nimbus")["request_id"]
    rv = as_user(app_client, b.reviewer_id)
    from .test_safeguards import clear_reviewer_items
    c = clear_reviewer_items(rv, req)
    assert c["request"]["state"] == "READY_FOR_DECISION", c["tasks"]
    with session_scope() as db:  # make this assessment high risk, as if the findings had said so
        a = db.get(Assessment, db.get(Request, req).current_assessment_id)
        a.summary = {**a.summary, "risk": {"score": 60, "tier": "high", "flags": [{"weight": 60, "flag": "test", "check": None}]}}
    return b, req, rv


def test_a_high_risk_supplier_needs_two_different_approvers(app_client):
    b, req, rv = _make_ready_high_risk(app_client)
    c = case(rv, req)
    r = rv.post(f"/api/review/requests/{req}/approve", json={"operation_id": op(), "expected_version": c["request"]["version"],
                                                            "assessment_id": c["assessment"]["id"], "note": "Looks fine"})
    assert r.status_code == 200 and r.json()["awaiting_second_approval"] is True
    c = case(rv, req)
    assert c["request"]["state"] == "READY_FOR_DECISION" and c["second_approval"]["first_by"] == "Test reviewer"
    r = rv.post(f"/api/review/requests/{req}/approve", json={"operation_id": op(), "expected_version": c["request"]["version"],
                                                            "assessment_id": c["assessment"]["id"]})
    assert r.status_code == 409 and r.json()["error"] == "second_approver_required"
    fin = as_user(app_client, b.finance_id)
    c = case(fin, req)
    r = fin.post(f"/api/review/requests/{req}/approve", json={"operation_id": op(), "expected_version": c["request"]["version"],
                                                             "assessment_id": c["assessment"]["id"]})
    assert r.status_code == 200, r.text
    c = case(fin, req)
    assert c["request"]["state"] == "APPROVED"
    assert "Second approval" in c["decisions"][-1]["reason"] and "Test reviewer" in c["decisions"][-1]["reason"]
    with session_scope() as db:
        assert db.scalar(select(AuditEvent).where(AuditEvent.request_id == req,
                                                  AuditEvent.event_type == "APPROVAL_FIRST_SIGNOFF")) is not None


def test_a_low_risk_supplier_needs_one_approver(app_client):
    run_worker_until_idle(50)
    b = make_buyer(auto=False)
    req = _submit(vendor(app_client, "One Approver Private Limited", buyer=b.slug), "happy_nimbus")["request_id"]
    rv = as_user(app_client, b.reviewer_id)
    from .test_safeguards import clear_reviewer_items
    c = clear_reviewer_items(rv, req)
    assert c["assessment"]["summary"]["risk"]["tier"] in ("low", "medium")
    r = rv.post(f"/api/review/requests/{req}/approve", json={"operation_id": op(), "expected_version": c["request"]["version"],
                                                            "assessment_id": c["assessment"]["id"]})
    assert r.status_code == 200 and "awaiting_second_approval" not in r.json()


def test_automatic_approval_stops_above_the_policy_risk(app_client, monkeypatch):
    from app.pipeline import runner

    run_worker_until_idle(50)
    b = make_buyer(auto=True)
    monkeypatch.setattr(runner.risk_mod, "assess", lambda checks, form, today=None: {
        "score": 30, "tier": "medium", "flags": [{"weight": 30, "flag": "Test flag", "check": None}]})
    req = _submit(vendor(app_client, "Medium Risk Auto Private Limited", buyer=b.slug), "happy_nimbus")["request_id"]
    rv = as_user(app_client, b.reviewer_id)
    c = case(rv, req)
    assert c["request"]["state"] != "APPROVED"
    titles = [e["title"] for e in rv.get(f"/api/runs/{c['runs'][0]['id']}/events").json()["events"]]
    assert any("risk is medium" in t for t in titles), titles


# ---------------------------------------------------------------------------- vendor master record

def test_vendor_master_record_carries_payment_and_tax_rules():
    s = SCEN["happy_nimbus"]
    checks = list(_gst_checks("happy_nimbus").values())
    inp = EvalInput(form=s["form"], docs=evidence(s["documents"]), policy=policy(), verifications=registry(s["form"]))
    rec = master.build(inp, checks, {"score": 0, "tier": "low"}, "VEN-1", "Buyer", "VOB-1")
    assert rec["msme"]["category"] == "Small" and rec["msme"]["max_days_with_written_agreement"] == 45
    assert "s.43B(h)" in rec["msme"]["payment_rule"]
    assert rec["tds"]["law"] == "Income-tax Act, 2025, s.393(1)" and rec["tds"]["table_entry"] == "Sl. 6(iii)"
    assert rec["gst"]["taxpayer_type"] == "Regular" and rec["gst"]["state_code"] == "29"
    csv_text = master.to_csv(rec)
    assert csv_text.splitlines()[0].startswith("record_type,vendor_code") and "msme.category" in csv_text.splitlines()[0]


def test_vendor_master_export_endpoint(app_client):
    run_worker_until_idle(50)
    b = make_buyer(auto=False)
    req = _submit(vendor(app_client, "Master Export Private Limited", buyer=b.slug), "happy_nimbus")["request_id"]
    rv = as_user(app_client, b.reviewer_id)
    r = rv.get(f"/api/review/requests/{req}/vendor-master?format=csv")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    assert "attachment" in r.headers["content-disposition"]
    r = rv.get(f"/api/review/requests/{req}/vendor-master")
    assert r.json()["status"].startswith("draft") and r.json()["bank"]["verified_at_bank"]["result"] == "MATCH"
    other = make_buyer(auto=False)
    assert as_user(app_client, other.reviewer_id).get(f"/api/review/requests/{req}/vendor-master").status_code == 404
