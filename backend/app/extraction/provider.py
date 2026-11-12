"""Provider-neutral model access over the OpenAI-compatible Chat Completions API.

Works with Gemini's OpenAI-compatible endpoint, OpenRouter, or any other
compatible server. The rest of the app only sees `ModelChain.complete_json`.

A target (endpoint + model) can carry several API keys. Calls rotate through them, and a
key that hits its daily quota, its per-minute limit or an auth error is set aside for a
while, so later calls go straight to a key that still works. Keys never appear in logs,
attempts or errors; they are named by slot ("key 2").
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import openai
from openai import OpenAI


class LLMError(Exception):
    def __init__(self, kind: str, message: str):
        super().__init__(message)
        # rate_limited | quota_exhausted | unavailable | timeout | bad_response | auth | rejected_request | not_configured
        self.kind = kind
        self.message = message
        self.retry_after_s: float | None = None

    @property
    def retry_elsewhere(self) -> bool:
        return self.kind in {"rate_limited", "quota_exhausted", "unavailable", "timeout", "bad_response"}


@dataclass
class ModelTarget:
    provider: str  # short label, e.g. "gemini" or "openrouter"
    base_url: str
    api_keys: tuple[str, ...]
    model: str

    @property
    def label(self) -> str:
        return f"{self.provider}:{self.model}"

    def slot_label(self, slot: int) -> str:
        return self.label if len(self.api_keys) == 1 else f"{self.label} (key {slot + 1})"


@dataclass
class Attempt:
    target: str
    outcome: str  # ok | rate_limited | unavailable | timeout | bad_response | ...
    latency_ms: int
    detail: str = ""


@dataclass
class CompletionResult:
    data: dict
    target: ModelTarget
    latency_ms: int
    usage: dict
    attempts: list[Attempt] = field(default_factory=list)


def provider_label(base_url: str) -> str:
    host = urlparse(base_url).hostname or "custom"
    if "googleapis" in host:
        return "gemini"
    if "openrouter" in host:
        return "openrouter"
    if "nvidia" in host:
        return "nvidia"
    return host


_RETRY_DELAY = re.compile(r"retry[ _-]?delay\W{0,4}(\d+(?:\.\d+)?)s", re.IGNORECASE)


def _error_text(exc: Exception) -> str:
    """The SDK's message plus the raw body: Gemini returns a JSON list, which the SDK leaves out of str(exc)."""
    parts = [str(exc)]
    try:
        resp = getattr(exc, "response", None)
        if resp is not None:
            parts.append(resp.text[:4000])
    except Exception:  # noqa: BLE001 - body not readable (streamed / closed)
        pass
    return "\n".join(parts)


def _classify(exc: Exception) -> LLMError:
    if isinstance(exc, LLMError):
        return exc
    if isinstance(exc, openai.RateLimitError) or (isinstance(exc, openai.APIStatusError) and getattr(exc, "status_code", 0) == 429):
        text = _error_text(exc)
        if "PerDay" in text or "per day" in text.lower():
            return LLMError("quota_exhausted", "the model's daily request quota is used up (free tier: resets at midnight "
                                               "Pacific time); retry later or enable paid usage")
        err = LLMError("rate_limited", "provider rate limit reached")
        m = _RETRY_DELAY.search(text)
        if m:
            err.retry_after_s = float(m.group(1))
        return err
    if isinstance(exc, openai.APITimeoutError):
        return LLMError("timeout", "provider did not respond in time")
    if isinstance(exc, openai.APIConnectionError):
        return LLMError("unavailable", "could not connect to provider")
    if isinstance(exc, (openai.AuthenticationError, openai.PermissionDeniedError)):
        return LLMError("auth", "provider rejected the API key")
    if isinstance(exc, openai.InternalServerError):
        return LLMError("unavailable", f"provider error {getattr(exc, 'status_code', '')}".strip())
    if isinstance(exc, openai.APIStatusError):
        status = getattr(exc, "status_code", 0)
        if status >= 500:
            return LLMError("unavailable", f"provider error {status}")
        if status == 400 and "api key" in _error_text(exc).lower():
            return LLMError("auth", "provider rejected the API key")
        return LLMError("rejected_request", f"provider rejected the request ({status}): {str(exc)[:200]}")
    return LLMError("unavailable", f"{type(exc).__name__}: {str(exc)[:200]}")


def _parse_json(text: str | None) -> dict:
    if not text:
        raise LLMError("bad_response", "empty response")
    t = text.strip()
    if t.startswith("```"):
        t = t.strip("`")
        t = t[t.find("{"):] if "{" in t else t
    start, end = t.find("{"), t.rfind("}")
    if start < 0 or end < 0:
        raise LLMError("bad_response", "response was not JSON")
    try:
        return json.loads(t[start : end + 1])
    except json.JSONDecodeError as exc:
        raise LLMError("bad_response", f"invalid JSON: {exc.msg}") from exc


# ---------------------------------------------------------------------------- key pool
# Process-wide, so every reader instance and worker thread shares what it learned about a key.

_POOL_LOCK = threading.Lock()
_KEY_HOLD: dict[str, tuple[float, str]] = {}  # key fingerprint -> (usable again at, epoch s; reason)
_NEXT_SLOT: dict[str, int] = {}  # target fingerprint -> slot to try first next time


def _fp(base_url: str, key: str) -> str:
    return hashlib.sha256(f"{base_url}|{key}".encode()).hexdigest()[:16]


def _target_fp(t: ModelTarget) -> str:
    return hashlib.sha256("|".join([t.base_url, t.model, *t.api_keys]).encode()).hexdigest()[:16]


def next_quota_reset(now: float | None = None) -> float:
    """Gemini's daily quotas reset at midnight Pacific time."""
    now = time.time() if now is None else now
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo("America/Los_Angeles")
    except Exception:  # noqa: BLE001 - no tz database: assume PST
        tz = timezone(timedelta(hours=-8))
    local = datetime.fromtimestamp(now, tz)
    midnight = (local + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight.timestamp() + 60  # a minute of slack


def reset_key_state() -> None:
    """Forget everything learned about keys (tests, or after enabling billing)."""
    with _POOL_LOCK:
        _KEY_HOLD.clear()
        _NEXT_SLOT.clear()


class ModelChain:
    """Call the configured target(s) in order.

    Within a target, calls rotate through its API keys. A key that hits its daily quota is
    held until the quota resets; one that is rate limited is held for the provider's
    suggested delay; one that is rejected is held for the rest of the process. Overload
    (503) and timeouts are retried with backoff; a malformed reply gets one corrective retry.
    Only when a target has nothing left does the chain move to the next target (a backup model), whatever the reason
    it failed: an overloaded, slow, misconfigured or refusing backup simply hands over to the next one.
    With `handover_s`, every target that has a backup after it gets that many seconds in total (each call's timeout
    is cut to what is left): an overloaded provider can answer 503 only after minutes, or not at all, and a reader
    should not wait on it for longer than the backup takes.
    """

    TRANSIENT = {"rate_limited", "unavailable", "timeout"}
    MIN_CALL_S = 5.0  # less time than this left in a model's budget: hand over instead of starting another call

    def __init__(self, targets: list[ModelTarget], timeout_s: float = 60.0, reasoning_effort: str | None = None,
                 max_transient_attempts: int = 3, backoff_s: tuple[float, ...] = (4.0, 10.0),
                 max_timeout_attempts: int = 2, handover_s: float | None = None,
                 rate_limit_hold_s: float = 30.0, max_wait_s: float = 60.0, sleep=time.sleep, clock=time.time,
                 client_factory=None):
        self.targets = targets
        self.timeout_s = timeout_s
        self.reasoning_effort = reasoning_effort or None
        self.max_transient_attempts = max_transient_attempts
        # A timed-out call has usually been processed (and counted against the daily quota) by the provider, and an
        # immediate repeat tends to time out too: allow one repeat, not the full transient budget.
        self.max_timeout_attempts = max_timeout_attempts
        self.handover_s = handover_s
        self.backoff_s = backoff_s
        self.rate_limit_hold_s = rate_limit_hold_s
        self.max_wait_s = max_wait_s
        self._sleep = sleep
        self._clock = clock
        self._client_factory = client_factory or (lambda base_url, key, timeout: OpenAI(
            base_url=base_url, api_key=key, timeout=timeout, max_retries=0))
        self._clients: dict[str, OpenAI] = {}
        self._schema_unsupported: set[str] = set()

    @property
    def configured(self) -> bool:
        return bool(self.targets)

    def describe(self) -> str:
        parts = [t.label + (f" ({len(t.api_keys)} keys)" if len(t.api_keys) > 1 else "") for t in self.targets]
        return " → ".join(parts) or "not configured"

    # ---------------------------------------------------------------- keys
    def _client(self, t: ModelTarget, slot: int):
        fp = _fp(t.base_url, t.api_keys[slot])
        if fp not in self._clients:
            self._clients[fp] = self._client_factory(t.base_url, t.api_keys[slot], self.timeout_s)
        return self._clients[fp]

    def _pick_slot(self, t: ModelTarget) -> int | None:
        now = self._clock()
        with _POOL_LOCK:
            tfp = _target_fp(t)
            n = len(t.api_keys)
            start = _NEXT_SLOT.get(tfp, 0) % n
            for i in range(n):
                slot = (start + i) % n
                hold = _KEY_HOLD.get(_fp(t.base_url, t.api_keys[slot]))
                if hold is None or hold[0] <= now:
                    _NEXT_SLOT[tfp] = (slot + 1) % n  # round robin spreads per-minute load across keys
                    return slot
        return None

    def _hold(self, t: ModelTarget, slot: int, until: float, reason: str) -> None:
        with _POOL_LOCK:
            _KEY_HOLD[_fp(t.base_url, t.api_keys[slot])] = (until, reason)

    def _holds(self, t: ModelTarget) -> list[tuple[float, str]]:
        with _POOL_LOCK:
            return [_KEY_HOLD.get(_fp(t.base_url, k), (0.0, "")) for k in t.api_keys]

    def key_status(self) -> list[dict]:
        """Per-target key availability, without the keys themselves."""
        now = self._clock()
        out = []
        for t in self.targets:
            holds = self._holds(t)
            out.append({"target": t.label, "keys": len(t.api_keys),
                        "available": sum(1 for until, _ in holds if until <= now),
                        "held": [{"key": i + 1, "reason": r, "seconds_left": None if until == float("inf") else int(until - now)}
                                 for i, (until, r) in enumerate(holds) if until > now]})
        return out

    def _no_key_error(self, t: ModelTarget) -> LLMError:
        reasons = {r for until, r in self._holds(t) if until > self._clock()}
        n = len(t.api_keys)
        if reasons == {"auth"}:
            return LLMError("auth", "provider rejected the API key" if n == 1 else f"provider rejected all {n} API keys")
        if "quota_exhausted" in reasons and "rate_limited" not in reasons:
            return LLMError("quota_exhausted", ("the model's daily request quota is used up" if n == 1 else
                                                f"all {n} API keys have used up their daily quota") +
                            " (free tier: resets at midnight Pacific time); retry later or enable paid usage")
        return LLMError("rate_limited", "provider rate limit reached on every API key")

    # ---------------------------------------------------------------- calls
    def complete_json(self, system: str, content: list[dict], schema: dict, schema_name: str,
                      validate=None) -> CompletionResult:
        """Return parsed JSON. `validate(data)` may raise LLMError('bad_response') to trigger the next target."""
        if not self.targets:
            raise LLMError("not_configured", "no extraction model is configured (set LLM_API_KEY and LLM_MODEL)")
        attempts: list[Attempt] = []
        last: LLMError | None = None
        for t in self.targets:
            transient = bad = timeouts = 0
            deadline = self._clock() + self.handover_s if self.handover_s and t is not self.targets[-1] else None
            # Every key can be set aside once, plus the transient retries and one corrective retry.
            for _ in range(len(t.api_keys) * 2 + self.max_transient_attempts + 3):
                left = None if deadline is None else deadline - self._clock()
                if left is not None and left < self.MIN_CALL_S:
                    last = LLMError("timeout", f"no answer within {self.handover_s:g} s; handed over to the backup")
                    break
                slot = self._pick_slot(t)
                if slot is None:
                    holds = [until for until, r in self._holds(t) if r == "rate_limited" and until > self._clock()]
                    wait = min(holds) - self._clock() if holds else None
                    fits = left is None or wait is not None and wait < left - self.MIN_CALL_S
                    if wait is not None and wait <= self.max_wait_s and transient < self.max_transient_attempts and fits:
                        transient += 1
                        self._sleep(max(wait, 0.5))
                        continue
                    last = self._no_key_error(t)
                    break
                started = time.monotonic()
                try:
                    msgs = [{"role": "system", "content": system}, {"role": "user", "content": content}]
                    if bad and last is not None:
                        msgs.append({"role": "user", "content": f"Your previous reply was invalid ({last.message}). "
                                                                "Reply again with JSON only, matching the schema."})
                    kwargs: dict = {"model": t.model, "messages": msgs, "temperature": 0}
                    if left is not None and left < self.timeout_s:
                        kwargs["timeout"] = left  # the rest of this model's time; the backup is waiting
                    if self.reasoning_effort and t is self.targets[0]:
                        kwargs["reasoning_effort"] = self.reasoning_effort  # a main-model setting; a backup may reject it
                    if t.label in self._schema_unsupported:
                        kwargs["response_format"] = {"type": "json_object"}
                    else:
                        kwargs["response_format"] = {"type": "json_schema",
                                                     "json_schema": {"name": schema_name, "schema": schema}}
                    resp = self._client(t, slot).chat.completions.create(**kwargs)
                    data = _parse_json(resp.choices[0].message.content if resp.choices else None)
                    if validate:
                        validate(data)
                    ms = int((time.monotonic() - started) * 1000)
                    attempts.append(Attempt(t.slot_label(slot), "ok", ms))
                    usage = resp.usage.model_dump() if resp.usage else {}
                    return CompletionResult(data, t, ms, usage, attempts)
                except Exception as exc:  # noqa: BLE001 - classified below
                    err = _classify(exc)
                    ms = int((time.monotonic() - started) * 1000)
                    last = err
                    if err.kind == "rejected_request" and "response_format" in err.message.lower() \
                            and t.label not in self._schema_unsupported:
                        self._schema_unsupported.add(t.label)  # provider can't do json_schema: retry in JSON mode
                        attempts.append(Attempt(t.slot_label(slot), "schema_unsupported", ms, err.message))
                        continue
                    attempts.append(Attempt(t.slot_label(slot), err.kind, ms, err.message))
                    if err.kind == "quota_exhausted":
                        self._hold(t, slot, next_quota_reset(self._clock()), "quota_exhausted")
                        continue  # next key, no wait
                    if err.kind == "auth":
                        self._hold(t, slot, float("inf"), "auth")
                        continue
                    if err.kind == "rate_limited":
                        self._hold(t, slot, self._clock() + (err.retry_after_s or self.rate_limit_hold_s), "rate_limited")
                        continue  # next key if one is free; otherwise the wait above counts as a retry
                    if err.kind == "bad_response" and bad == 0:
                        bad += 1
                        continue
                    if err.kind == "timeout":
                        timeouts += 1
                        # With a backup still to try, one timeout is enough: hand over instead of waiting again.
                        if timeouts >= (self.max_timeout_attempts if t is self.targets[-1] else 1):
                            break
                    if err.kind in self.TRANSIENT:
                        transient += 1
                        delay = self.backoff_s[min(transient - 1, len(self.backoff_s) - 1)]
                        if transient < self.max_transient_attempts and (
                                deadline is None or self._clock() + delay < deadline - self.MIN_CALL_S):
                            self._sleep(delay)
                            continue
                    break
        assert last is not None
        last.attempts = attempts  # type: ignore[attr-defined]
        raise last


def chain_from_settings(settings) -> ModelChain:
    targets: list[ModelTarget] = []
    keys = tuple(settings.llm_api_keys)
    if keys and settings.llm_model:
        for m in [x.strip() for x in settings.llm_model.split(",") if x.strip()]:
            targets.append(ModelTarget(provider_label(settings.llm_base_url), settings.llm_base_url, keys, m))
    for _name, url, key, model in settings.llm_backups:  # in order; incomplete slots are refused at start-up
        if url and key and model:
            targets.append(ModelTarget(provider_label(url), url, (key,), model.strip()))
    return ModelChain(targets, timeout_s=settings.llm_timeout_s, reasoning_effort=settings.llm_reasoning_effort,
                      handover_s=settings.llm_handover_s or None)
