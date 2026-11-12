"""The model time budget and hand-over to a backup, queue position, GSTINs unknown to the simulated registry, e-mail,
PIN code and date formats, pages without a text layer, and old bank letters.
Fake model clients and the fixture reader: no model calls.
"""

import json
from types import SimpleNamespace

import httpx
import openai
import pytest

from app.extraction.provider import ModelChain, ModelTarget, reset_key_state
from app.pipeline.checks import run_checks
from app.pipeline.compare import compare
from app.pipeline.evidence import EvalInput, VerificationRec

from .test_api_flow import run_worker_until_idle, vendor
from .test_auto_approval import make_buyer
from .test_scenarios_logic import SCEN, evaluate, evidence, policy

URL = "https://example.test/v1/"


# ---------------------------------------------------------------------------- a slow main model hands over

class Clock:
    """Fake time: a call 'takes' its scripted seconds, capped at the timeout it was given."""

    def __init__(self):
        self.now = 1_800_000_000.0
        self.calls: list[tuple[str, float | None]] = []

    def clock(self):
        return self.now

    def sleep(self, s):
        self.now += s

    def factory(self, script):
        def make(base_url, key, timeout):
            def create(**kw):
                self.calls.append((key, kw.get("timeout")))
                kind, secs = script[key]
                limit = kw.get("timeout") or timeout
                if kind == "hang" or secs > limit:
                    self.now += limit
                    raise openai.APITimeoutError(request=httpx.Request("POST", URL))
                self.now += secs
                if kind == "503":
                    raise openai.InternalServerError("overloaded", response=httpx.Response(503, request=httpx.Request("POST", URL)), body=None)
                return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps({"by": key})))], usage=None)
            return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        return make


def _chain(c: Clock, script, handover=90.0):
    targets = [ModelTarget("gemini", URL, ("main",), "gemini-3.8-flash"), ModelTarget("nvidia", URL, ("backup",), "nemotron")]
    return ModelChain(targets, timeout_s=240, handover_s=handover, sleep=c.sleep, clock=c.clock,
                      client_factory=c.factory(script), backoff_s=(4.0, 10.0))


@pytest.fixture(autouse=True)
def _fresh_keys():
    reset_key_state()
    yield
    reset_key_state()


def _read(chain):
    return chain.complete_json("sys", [{"type": "text", "text": "x"}], {"type": "object"}, "s")


def test_a_hanging_main_model_gets_its_time_budget_not_the_full_timeout():
    c = Clock()
    t0 = c.now
    r = _read(_chain(c, {"main": ("hang", 0), "backup": ("ok", 20)}))
    assert r.data == {"by": "backup"}
    assert c.calls[0] == ("main", 90.0)  # cut from 240 s to the budget
    assert [k for k, _ in c.calls] == ["main", "backup"]
    assert c.now - t0 == pytest.approx(110)  # was 240 s + 4 s + 240 s + the backup


def test_slow_overload_replies_stop_at_the_budget():
    c = Clock()
    t0 = c.now
    r = _read(_chain(c, {"main": ("503", 40), "backup": ("ok", 20)}))
    assert r.data == {"by": "backup"}
    main = [t for k, t in c.calls if k == "main"]
    assert main == [90.0, pytest.approx(46.0)]  # the second call only gets what is left; no third
    assert c.now - t0 < 90 + 20 + 1


def test_the_last_model_keeps_the_full_timeout_and_no_budget_means_the_old_behaviour():
    c = Clock()
    _read(_chain(c, {"main": ("hang", 0), "backup": ("ok", 200)}))
    assert c.calls[-1] == ("backup", None)  # the backup (last) is not cut short
    c = Clock()
    with pytest.raises(Exception):
        _read(_chain(c, {"main": ("hang", 0), "backup": ("hang", 0)}, handover=None))
    assert c.calls[0] == ("main", None)


# ---------------------------------------------------------------------------- the queue says what it waits for

def test_a_queued_run_says_how_many_runs_are_ahead(app_client):
    run_worker_until_idle(50)
    b = make_buyer(auto=False)
    runs = []
    for name in ("Queue First Private Limited", "Queue Second Private Limited"):
        v = vendor(app_client, name, buyer=b.slug)
        s = v.post("/api/vendor/samples/happy_nimbus/load").json()
        r = v.post("/api/vendor/submit", json={"operation_id": f"op-{name}", "form": s["form"],
                                                "documents": {k: d["document_id"] for k, d in s["documents"].items()}})
        assert r.status_code == 200, r.text
        runs.append((v, r.json()["run_id"]))
    v, second = runs[1]
    run = v.get(f"/api/runs/{second}/events").json()["run"]
    assert run["status"] == "QUEUED" and run["ahead"] >= 1
    run_worker_until_idle(50)
    assert "ahead" not in v.get(f"/api/runs/{second}/events").json()["run"]


# ---------------------------------------------------------------------------- unknown to the simulated registry

def _registry_check(form, verifications, pol="buyer_a.json"):
    s = SCEN["happy_nimbus"]
    inp = EvalInput(form=form, docs=evidence(s["documents"]), policy=policy(pol), verifications=verifications)
    return next(c for c in run_checks(inp, compare(inp)) if c.key == "verification:gst_registry_check")


def test_a_gstin_the_simulated_registry_does_not_know_goes_to_a_manual_lookup_not_to_the_vendor():
    form = SCEN["happy_nimbus"]["form"]
    g = form["tax"]["gstin"]
    sim = VerificationRec(1, "GSTIN", g, "simulated_registry", "not_found", None, "sim", "system")
    c = _registry_check(form, [sim])
    assert c.status == "unable" and c.owner == "reviewer" and c.task_key == "reviewer:registry", c
    assert "not a finding about the vendor" in c.reason and c.vendor_text is None
    # A reviewer's real-portal lookup that finds nothing still goes back to the vendor.
    manual = VerificationRec(2, "GSTIN", g, "manual_registry", "not_found", None, "GST portal", "user")
    c = _registry_check(form, [sim, manual])
    assert c.owner == "vendor" and c.task_key == "vendor:gstin"


# ---------------------------------------------------------------------------- malformed email, PIN code, date

def _with(form, **values):
    f = json.loads(json.dumps(form))
    f["contact"]["email"] = values.get("email", f["contact"]["email"])
    f["business"]["registered_address"]["pincode"] = values.get("pin", f["business"]["registered_address"]["pincode"])
    f["business"]["incorporation_date"] = values.get("date", f["business"].get("incorporation_date"))
    return f


def test_malformed_email_pin_and_date_are_sent_back_to_the_vendor():
    s = SCEN["happy_nimbus"]
    form = _with(s["form"], email="not-an-email", pin="0000", date="31/02/2026")
    inp = EvalInput(form=form, docs=evidence(s["documents"]), policy=policy(), verifications=[])
    c = next(x for x in run_checks(inp, compare(inp)) if x.key == "format:form_values")
    assert c.status == "not_satisfied" and c.owner == "vendor" and c.task_key == "vendor:form_values"
    for bit in ("Contact email", "not-an-email", "PIN code", "0000", "Date of incorporation", "31/02/2026"):
        assert bit in c.vendor_text, (bit, c.vendor_text)


@pytest.mark.parametrize("key", sorted(SCEN))
def test_every_prepared_sample_has_well_formed_contact_details(key):
    _, _, checks, _ = evaluate(key)
    c = next(x for x in checks if x.key == "format:form_values")
    assert c.status == "satisfied", c.reason


# ---------------------------------------------------------------------------- fresh-documents QA: the "blank" label

def test_a_page_without_a_text_layer_is_not_called_blank():
    from dataclasses import replace

    from app.pdf.inspect import layout_label

    assert layout_label("blank") == "no text layer" and layout_label("digital") == "digital"
    s = SCEN["happy_nimbus"]
    docs = [replace(d, layout="blank") if d.slot == "gst_certificate" else d for d in evidence(s["documents"])]
    inp = EvalInput(form=s["form"], docs=docs, policy=policy(), verifications=[])
    c = next(x for x in run_checks(inp, compare(inp)) if x.key == "document:gst_evidence")
    assert "no text layer" in c.reason and "blank" not in c.reason, c.reason


# ---------------------------------------------------------------------------- an old bank letter

def _bank_dated(value):
    s = SCEN["happy_nimbus"]
    docs = evidence(s["documents"])
    for d in docs:
        if d.slot == "bank_proof":
            f = d.reading.fields["document_date"]
            d.reading.fields["document_date"] = f.model_copy(update={"value": value, "normalized": value,
                                                                      "status": "found" if value else "not_found"})
    inp = EvalInput(form=s["form"], docs=docs, policy=policy(), verifications=[])
    return {c.key: c for c in run_checks(inp, compare(inp))}


def test_a_bank_letter_older_than_six_months_is_sent_back_to_the_vendor():
    c = _bank_dated("01/01/2025")["credibility:bank_proof_date"]
    assert c.status == "not_satisfied" and c.owner == "vendor" and c.task_key == "vendor:bank_proof_date"
    assert "01/01/2025" in c.vendor_text and "last 6 months" in c.vendor_text
    assert _bank_dated("02/11/2026")["credibility:bank_proof_date"].status == "satisfied"  # the samples' letters
    assert _bank_dated("2 November 2026")["credibility:bank_proof_date"].status == "satisfied"
    assert "credibility:bank_proof_date" not in _bank_dated(None)  # a cheque carries no date: nothing to judge
