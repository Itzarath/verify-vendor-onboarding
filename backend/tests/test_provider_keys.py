"""Key rotation in ModelChain, with a fake client: no network, no quota used."""

from __future__ import annotations

import json
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import httpx
import openai
import pytest

from app.config import Settings
from app.extraction.provider import LLMError, ModelChain, ModelTarget, next_quota_reset, reset_key_state

URL = "https://example.test/v1/"


def _err(cls, status: int, message: str, body_text: str = ""):
    resp = httpx.Response(status, request=httpx.Request("POST", URL), text=body_text)
    return cls(message, response=resp, body=None)


def quota():
    # As Gemini sends it: the SDK message is just "Error code: 429"; the quota id is only in the raw JSON list body.
    body = json.dumps([{"error": {"code": 429, "status": "RESOURCE_EXHAUSTED", "details": [
        {"violations": [{"quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier", "quotaValue": "20"}]}]}}])
    return _err(openai.RateLimitError, 429, "Error code: 429", body)


def per_minute():
    return _err(openai.RateLimitError, 429, "Quota exceeded per minute. {'retryDelay': '20s'}")


def bad_key():
    return _err(openai.BadRequestError, 400, "API key not valid. Please pass a valid API key.")


def overloaded():
    return _err(openai.InternalServerError, 503, "The model is overloaded")


class World:
    """Fake clock, sleep and per-key scripted replies."""

    def __init__(self, script: dict[str, list]):
        self.now = 1_800_000_000.0
        self.script = script
        self.calls: list[str] = []
        self.slept: list[float] = []

    def clock(self):
        return self.now

    def sleep(self, s):
        self.slept.append(s)
        self.now += s

    def factory(self, base_url, key, timeout):
        world = self

        def create(**kwargs):
            world.calls.append(key)
            queue = world.script.get(key) or ["ok"]
            step = queue.pop(0) if len(queue) > 1 else queue[0]
            if step != "ok":
                raise step()
            msg = SimpleNamespace(content=json.dumps({"ok": True, "key": key}))
            return SimpleNamespace(choices=[SimpleNamespace(message=msg)], usage=None)

        return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))


def chain(world: World, keys=("k1", "k2", "k3")) -> ModelChain:
    t = ModelTarget("gemini", URL, tuple(keys), "gemini-3.8-flash")
    return ModelChain([t], sleep=world.sleep, clock=world.clock, client_factory=world.factory, backoff_s=(1.0,))


def call(c: ModelChain):
    return c.complete_json("sys", [{"type": "text", "text": "x"}], {"type": "object"}, "s")


@pytest.fixture(autouse=True)
def _fresh_keys():
    reset_key_state()
    yield
    reset_key_state()


def test_daily_quota_moves_to_next_key_and_is_remembered():
    w = World({"k1": [quota]})
    c = chain(w)
    r = call(c)
    assert r.data["key"] == "k2"
    assert [a.outcome for a in r.attempts] == ["quota_exhausted", "ok"]
    assert r.attempts[0].target == "gemini:gemini-3.8-flash (key 1)"
    for _ in range(6):
        call(c)
    assert w.calls.count("k1") == 1  # never retried while its quota is used up
    assert w.slept == []
    assert c.key_status()[0]["available"] == 2


def test_key_comes_back_after_quota_reset():
    w = World({"k1": [quota, "ok"]})
    c = chain(w, keys=("k1", "k2"))
    call(c)
    w.now = next_quota_reset(w.now) + 1
    calls_before = w.calls.count("k1")
    for _ in range(2):
        call(c)
    assert w.calls.count("k1") == calls_before + 1


def test_all_keys_out_of_quota_fails_fast_without_waiting():
    w = World({"k1": [quota], "k2": [quota], "k3": [quota]})
    c = chain(w)
    with pytest.raises(LLMError) as e:
        call(c)
    assert e.value.kind == "quota_exhausted"
    assert "all 3 API keys" in e.value.message
    n = len(w.calls)
    with pytest.raises(LLMError) as e2:
        call(c)
    assert e2.value.kind == "quota_exhausted"
    assert len(w.calls) == n  # nothing sent: every key is known to be out
    assert w.slept == []


def test_per_minute_limit_rotates_then_waits_for_the_earliest_key():
    w = World({"k1": [per_minute, "ok"], "k2": [per_minute, "ok"]})
    c = chain(w, keys=("k1", "k2"))
    r = call(c)
    assert r.data["ok"] is True
    assert w.slept == [20.0]  # waited for the provider's suggested delay, once
    assert [a.outcome for a in r.attempts] == ["rate_limited", "rate_limited", "ok"]


def test_rejected_key_is_set_aside_and_others_keep_working():
    w = World({"k2": [bad_key]})
    c = chain(w)
    outs = [call(c).data["key"] for _ in range(6)]
    assert "k2" not in outs
    assert w.calls.count("k2") == 1


def test_every_key_rejected_reports_auth():
    w = World({"k1": [bad_key], "k2": [bad_key]})
    with pytest.raises(LLMError) as e:
        call(chain(w, keys=("k1", "k2")))
    assert e.value.kind == "auth"


def test_overload_retries_with_backoff_across_keys():
    w = World({"k1": [overloaded], "k2": [overloaded]})
    r = call(chain(w))
    assert r.data["key"] == "k3"
    assert w.slept == [1.0, 1.0]


def test_single_key_labels_unchanged():
    w = World({})
    c = chain(w, keys=("only",))
    r = call(c)
    assert r.attempts[0].target == "gemini:gemini-3.8-flash"
    assert c.describe() == "gemini:gemini-3.8-flash"
    assert chain(w).describe() == "gemini:gemini-3.8-flash (3 keys)"


def test_settings_collects_keys_without_blanks_or_duplicates():
    s = Settings(llm_api_key="a", llm_api_key_2=" a ", llm_api_key_3="b")
    assert s.llm_api_keys == ["a", "b"]
    s = Settings(llm_api_key="", llm_api_key_2=None, llm_api_key_3="c")
    assert s.llm_api_keys == ["c"]


def test_quota_reset_is_next_pacific_midnight():
    pt = ZoneInfo("America/Los_Angeles")
    now = datetime(2026, 11, 7, 22, 30, tzinfo=pt).timestamp()
    reset = datetime.fromtimestamp(next_quota_reset(now), pt)
    assert (reset.year, reset.month, reset.day, reset.hour, reset.minute) == (2026, 11, 8, 0, 1)


def timed_out():
    return openai.APITimeoutError(request=httpx.Request("POST", URL))


def test_a_timeout_is_repeated_once_not_three_times():
    # every call times out: one repeat, then give up (each timed-out call usually still counts against the quota)
    w = World({"k1": [timed_out], "k2": [timed_out], "k3": [timed_out]})
    with pytest.raises(LLMError) as e:
        call(chain(w))
    assert e.value.kind == "timeout" and len(w.calls) == 2


def test_a_timeout_then_a_reply_succeeds():
    w = World({"k1": [timed_out, "ok"], "k2": ["ok"], "k3": ["ok"]})
    assert call(chain(w)).data["ok"] and len(w.calls) == 2


def test_hosted_default_timeout_allows_slow_document_reads():
    assert Settings(_env_file=None).llm_timeout_s >= 180  # measured single-document reads reached 121 s
