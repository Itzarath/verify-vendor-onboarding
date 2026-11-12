"""Hosting conditions: demo access code, production start-up checks, database URL handling and the PDF sandbox start-up
check. No live model calls."""

import pytest
from fastapi.testclient import TestClient

from app.api import access
from app.config import Settings, get_settings
from app.main import check_production_settings

CODE = "evaluator-2026-demo"


@pytest.fixture()
def gated(app_client, monkeypatch):
    monkeypatch.setattr(get_settings(), "demo_access_code", CODE)
    access.reset_failures()
    yield lambda: TestClient(app_client.app)
    access.reset_failures()


def test_without_the_code_the_api_is_closed(gated):
    c = gated()
    for path in ("/api/auth/personas", "/api/auth/me", "/api/config/form", "/api/review/dashboard",
                 "/api/documents/1/file"):
        r = c.get(path)
        assert r.status_code == 401 and r.json()["error"] == "access_required", path
    r = c.post("/api/auth/new-vendor", json={"company_name": "Gate Crasher Pvt Ltd", "buyer": "meridian"})
    assert r.status_code == 401 and r.json()["error"] == "access_required"
    assert c.post("/api/vendor/documents", files={"file": ("x.pdf", b"%PDF-1.4", "application/pdf")}).status_code == 401
    # the platform health check stays open but says nothing beyond "ok"
    assert c.get("/api/health").json() == {"ok": True}
    assert c.get("/api/access").json() == {"required": True, "granted": False}


def test_the_right_code_opens_it_and_a_changed_code_closes_it(gated, monkeypatch):
    c = gated()
    bad = c.post("/api/access", json={"code": "guess"})
    assert bad.status_code == 401 and bad.json()["error"] == "wrong_code"
    ok = c.post("/api/access", json={"code": f"  {CODE} "})
    assert ok.status_code == 200 and access.ACCESS_COOKIE in ok.cookies
    assert c.get("/api/access").json() == {"required": True, "granted": True}
    assert c.get("/api/auth/personas").status_code == 200
    assert c.get("/api/health").json() == {"ok": True}  # details only when asked for
    details = c.get("/api/health?details=1").json()
    assert "reader" in details and "pdf_sandbox" in details
    monkeypatch.setattr(get_settings(), "demo_access_code", "a-new-code-123")
    assert c.get("/api/auth/personas").json()["error"] == "access_required"


def test_repeated_wrong_codes_are_slowed_down(gated):
    c = gated()
    for _ in range(access.MAX_FAILURES):
        assert c.post("/api/access", json={"code": "wrong-code"}).status_code == 401
    r = c.post("/api/access", json={"code": CODE})  # even the right code waits out the window
    assert r.status_code == 429 and r.json()["error"] == "too_many_attempts"


def test_page_shell_is_served_without_the_code(gated):
    r = gated().get("/")
    assert r.status_code == 200  # only the access-code screen can do anything without the code


def test_no_gate_when_no_code_is_configured(app_client):
    assert not get_settings().demo_access_code
    c = TestClient(app_client.app)
    assert c.get("/api/access").json() == {"required": False, "granted": True}
    assert c.get("/api/auth/personas").status_code == 200


def _prod(**kw):
    base = {"app_env": "prod", "session_secret": "s" * 40, "extraction_mode": "live", "pdf_isolation": True,
            "demo_access_code": CODE, "llm_model": "gemini-3.8-flash", "llm_api_key": "test-key-not-real",
            "llm_api_key_2": None, "llm_api_key_3": None, "llm_fallback_model": None, "llm_fallback_base_url": None,
            "llm_fallback_api_key": None, **{f"llm_fallback_{n}_{f}": None for n in (2, 3)
                                              for f in ("base_url", "api_key", "model")}}
    return get_settings().model_copy(update={**base, **kw})


def test_production_start_up_checks():
    check_production_settings(_prod())
    check_production_settings(_prod(demo_access_code=None))  # an open demo, without the shared code, may start
    for kw, needle in (({"demo_access_code": "short"}, "DEMO_ACCESS_CODE"),
                       ({"llm_api_key": None}, "LLM_API_KEY"), ({"llm_model": ""}, "LLM_MODEL"),
                       ({"llm_model": "gemini-3.8-flash,other-model"}, "one main model"),
                       ({"llm_fallback_model": "other-model"}, "backup LLM_FALLBACK needs"),
                       ({"llm_fallback_2_api_key": "k"}, "backup LLM_FALLBACK_2 needs"),
                       ({"llm_fallback_model": "a,b", "llm_fallback_base_url": "https://b.example/v1",
                         "llm_fallback_api_key": "k"}, "names one model")):
        with pytest.raises(RuntimeError, match=needle):
            check_production_settings(_prod(**kw))
    # Complete backups are allowed, in order (used only when every earlier model failed).
    check_production_settings(_prod(llm_fallback_model="backup-model", llm_fallback_base_url="https://b.example/v1",
                                    llm_fallback_api_key="test-key-not-real", llm_fallback_2_model="openrouter/free",
                                    llm_fallback_2_base_url="https://openrouter.ai/api/v1", llm_fallback_2_api_key="k2"))


def test_backups_are_tried_in_order_whatever_made_the_earlier_one_fail():
    from types import SimpleNamespace

    from app.extraction.provider import LLMError, ModelChain, ModelTarget

    calls = []
    failures = {"main": LLMError("unavailable", "503"), "nvidia": LLMError("auth", "bad key"),
                "router": LLMError("rejected_request", "400 too large")}

    class Client:
        def __init__(self, model):
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

        @staticmethod
        def create(**kw):
            calls.append(kw["model"])
            if kw["model"] in failures:
                raise failures[kw["model"]]
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='{"ok": 1}'))], usage=None)

    targets = [ModelTarget(n, f"https://{n}.example/v1", ("k",), n) for n in ("main", "nvidia", "router", "last")]
    chain = ModelChain(targets, sleep=lambda s: None, client_factory=lambda url, key, t: Client(url))
    out = chain.complete_json("sys", [{"type": "text", "text": "x"}], {"type": "object"}, "t")
    assert out.data == {"ok": 1} and out.target.model == "last"
    assert calls.index("nvidia") > calls.index("main") and calls.index("router") > calls.index("nvidia")
    assert calls[-1] == "last"


def test_a_backup_model_does_not_change_the_reading_cache_key():
    from app.extraction.provider import ModelChain, ModelTarget
    from app.extraction.reader import config_key

    main = ModelTarget("gemini", "https://g.example/v1", ("k",), "gemini-3.8-flash")
    backup = ModelTarget("backup", "https://b.example/v1", ("k2",), "backup-model")
    assert config_key(ModelChain([main], reasoning_effort="low")) == config_key(ModelChain([main, backup], reasoning_effort="low"))


def test_the_reasoning_setting_goes_only_to_the_main_model():
    from app.extraction.provider import ModelChain, ModelTarget

    sent = []

    class Client:
        class chat:  # noqa: N801
            class completions:  # noqa: N801
                @staticmethod
                def create(**kw):
                    sent.append(kw)
                    if kw["model"] == "main":
                        raise __import__("app.extraction.provider", fromlist=["LLMError"]).LLMError("unavailable", "503")
                    from types import SimpleNamespace
                    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='{"ok": 1}'))], usage=None)

    chain = ModelChain([ModelTarget("main", "https://g.example/v1", ("k",), "main"),
                        ModelTarget("backup", "https://b.example/v1", ("k2",), "backup")],
                       reasoning_effort="low", sleep=lambda s: None, client_factory=lambda *a: Client())
    assert chain.complete_json("sys", [{"type": "text", "text": "x"}], {"type": "object"}, "t").data == {"ok": 1}
    assert all("reasoning_effort" in kw for kw in sent if kw["model"] == "main")
    assert all("reasoning_effort" not in kw for kw in sent if kw["model"] == "backup")


@pytest.mark.parametrize("url", ["postgres://u:p@ep-x.ap-southeast-1.aws.neon.tech/db?sslmode=require",
                                 "postgresql://u:p@ep-x.ap-southeast-1.aws.neon.tech/db?sslmode=require"])
def test_hosted_database_urls_use_psycopg(url):
    s = Settings(database_url=url, _env_file=None)
    assert s.database_url == "postgresql+psycopg://u:p@ep-x.ap-southeast-1.aws.neon.tech/db?sslmode=require"


def test_pdf_sandbox_self_test_through_the_real_child(monkeypatch):
    from app.pdf import sandbox

    monkeypatch.setattr(get_settings(), "pdf_isolation", True)
    sandbox.SELF_TEST.clear()
    result = sandbox.self_test()
    assert result["status"] == "ok", result
    assert result["max_children"] == get_settings().pdf_max_children
    assert "memory_cap_enforced" in result  # None off Linux; True expected on the hosted Linux server


def _bomb(side: int) -> bytes:
    """A few-KB PDF whose one image claims side x side RGB pixels (double-deflated zeros)."""
    import zlib

    nl = b"\n"
    co = zlib.compressobj(9)
    row = bytes(side * 3 * 50)
    inner = b"".join(co.compress(row) for _ in range(side // 50)) + co.flush()
    outer = zlib.compress(inner, 9)
    content = b"BT /F1 14 Tf 72 740 Td (SYNTHETIC TEST DOCUMENT) Tj ET q 400 0 0 400 100 200 cm /Im1 Do Q"
    image = (b"<< /Type /XObject /Subtype /Image /Width %d /Height %d /ColorSpace /DeviceRGB /BitsPerComponent 8 "
             b"/Filter [/FlateDecode /FlateDecode] /Length %d >>" % (side, side, len(outer)))
    objs = [b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >>"
            b" /XObject << /Im1 6 0 R >> >> >>",
            b"<< /Length %d >>" % len(content) + nl + b"stream" + nl + content + nl + b"endstream",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
            image + nl + b"stream" + nl + outer + nl + b"endstream"]
    out, offs = bytearray(b"%PDF-1.7" + nl), []
    for i, o in enumerate(objs, 1):
        offs.append(len(out))
        out += b"%d 0 obj" % i + nl + o + nl + b"endobj" + nl
    x = len(out)
    out += b"xref" + nl + b"0 %d" % (len(objs) + 1) + nl + b"0000000000 65535 f " + nl
    out += b"".join(b"%010d 00000 n " % o + nl for o in offs)
    out += b"trailer" + nl + b"<< /Size %d /Root 1 0 R >>" % (len(objs) + 1) + nl + b"startxref" + nl
    out += b"%d" % x + nl + b"%%EOF" + nl
    return bytes(out)


@pytest.mark.parametrize("isolated", [False, True])
def test_decompression_bomb_is_refused_before_decoding(isolated, monkeypatch):
    from app.pdf.inspect import IntakeError, inspect_pdf

    monkeypatch.setattr(get_settings(), "pdf_isolation", isolated)
    data = _bomb(8000)  # 64 million pixels declared, a few KB on disk
    assert len(data) < 20_000
    with pytest.raises(IntakeError) as e:
        inspect_pdf(data, 10 * 2**20, 10)
    assert e.value.code == "too_complex" and "too large" in e.value.message


def test_a_damaged_pdf_through_the_real_child_is_a_clear_refusal(monkeypatch):
    # MuPDF prints its own error messages to stdout; they must never be mistaken for the child's result
    from app.extraction.service import FIXTURE_DIR
    from app.pdf.inspect import IntakeError, inspect_pdf

    monkeypatch.setattr(get_settings(), "pdf_isolation", True)
    with pytest.raises(IntakeError) as e:
        inspect_pdf((FIXTURE_DIR / "documents" / "broken_truncated.pdf").read_bytes(), 10 * 2**20, 10)
    assert e.value.code in ("corrupt_pdf", "too_complex")


def test_an_oversized_page_is_not_rendered_huge():
    import time

    import pymupdf

    from app.pdf.inspect import page_texts

    doc = pymupdf.open()
    doc.new_page(width=14400, height=14400).insert_text((72, 200), "SYNTHETIC TEST DOCUMENT on a 200-inch page",
                                                       fontsize=200)
    t = time.monotonic()
    page_texts(doc.tobytes())  # must finish quickly with modest memory (the check renders at most 3000 px a side)
    assert time.monotonic() - t < 10


def test_public_health_check_does_not_touch_the_database(gated, app_client, monkeypatch):
    # Render's health check runs every few seconds; a database query each time would keep Neon from ever pausing.
    # That holds with the shared code and without it (an open demo).
    from sqlalchemy import event

    from app.db import get_engine

    seen = []
    listener = lambda *a, **k: seen.append(1)  # noqa: E731
    event.listen(get_engine(), "before_cursor_execute", listener)
    try:
        assert gated().get("/api/health").json() == {"ok": True}
        monkeypatch.setattr(get_settings(), "demo_access_code", None)
        assert TestClient(app_client.app).get("/api/health").json() == {"ok": True}
    finally:
        event.remove(get_engine(), "before_cursor_execute", listener)
    assert seen == []
    assert "reader" in TestClient(app_client.app).get("/api/health?details=1").json()  # open demo: details on request


def test_idle_worker_does_not_poll_the_database_every_second():
    from app.config import Settings

    assert Settings(_env_file=None).worker_poll_s >= 300  # new work wakes the worker immediately instead


def test_wrong_guesses_from_others_do_not_lock_out_an_evaluator_with_the_code(gated):
    # The limit is per client, so strangers' wrong guesses can't block a fresh evaluator
    for i in range(200):
        access.record_failure(f"203.0.113.{i}")
    r = gated().post("/api/access", json={"code": CODE})
    assert r.status_code == 200
    for i in range(access.MAX_TRACKED_CLIENTS + 10):  # the bookkeeping stays bounded
        access.record_failure(f"client-{i}")
    access.too_many_failures("x")
    assert len(access._failures) <= access.MAX_TRACKED_CLIENTS + 1


def test_an_open_case_page_checks_only_the_version_and_responses_are_compressed(app_client):
    # An open case page asks every few seconds whether the case changed. It gets only the version, not the whole case,
    # and large responses are compressed: the free hosting plan has a small monthly data allowance.
    from .test_api_flow import run_worker_until_idle, staff, submit_sample, vendor

    v = vendor(app_client, "Version Watch Services Private Limited")
    r, _ = submit_sample(v, "trade_name_unsupported")
    req = r.json()["request_id"]
    run_worker_until_idle()
    rv = staff(app_client)
    full = rv.get(f"/api/review/requests/{req}", headers={"Accept-Encoding": "gzip"})
    assert full.headers.get("content-encoding") == "gzip"
    assert rv.get(f"/api/review/requests/{req}/version").json() == {"version": full.json()["request"]["version"]}
    assert staff(app_client, buyer="Kestrel").get(f"/api/review/requests/{req}/version").status_code == 404
    assert v.get(f"/api/review/requests/{req}/version").status_code == 403
