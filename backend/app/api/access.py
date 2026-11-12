"""Demo access gate: one shared code, entered once per browser, in front of the whole API.

The hosted demo uses synthetic personas that anyone can pick, so this gate is what keeps strangers from uploading
files or spending the model quota. It is not user authentication: everyone with the code is the same "evaluator".

The browser gets a signed cookie derived from the code, so changing DEMO_ACCESS_CODE signs everyone out.
"""

from __future__ import annotations

import hashlib
import hmac
import threading
import time

from fastapi import Request

from ..config import get_settings

ACCESS_COOKIE = "vob_access"
ACCESS_MAX_AGE = 60 * 60 * 24 * 14
OPEN_PATHS = {"/api/health", "/api/access"}  # the platform health check, and entering the code

MAX_FAILURES = 10  # wrong codes per client per window
FAILURE_WINDOW_S = 600
# Bookkeeping bound. The client address can be forged behind the proxy, so this limit only slows casual guessing; a
# limit across all clients would let strangers lock out an evaluator who has the right code (the gate protects the model quota; it is not
# user authentication).
MAX_TRACKED_CLIENTS = 5000
_failures: dict[str, list[float]] = {}
_lock = threading.Lock()


def gate_enabled() -> bool:
    return bool(get_settings().demo_access_code)


def access_token() -> str:
    s = get_settings()
    return hmac.new(s.session_secret.encode(), b"demo-access:" + (s.demo_access_code or "").encode(),
                    hashlib.sha256).hexdigest()


def has_access(request: Request) -> bool:
    if not gate_enabled():
        return True
    raw = request.cookies.get(ACCESS_COOKIE) or ""
    return hmac.compare_digest(raw, access_token())


def code_matches(code: str) -> bool:
    expected = get_settings().demo_access_code or ""
    return bool(expected) and hmac.compare_digest((code or "").strip().encode(), expected.encode())


def too_many_failures(client: str) -> bool:
    now = time.monotonic()
    with _lock:
        for key in list(_failures):  # drop everything outside the window, so the table can't grow without bound
            kept = [t for t in _failures[key] if now - t < FAILURE_WINDOW_S]
            if kept:
                _failures[key] = kept
            else:
                del _failures[key]
        if len(_failures) > MAX_TRACKED_CLIENTS:
            _failures.clear()
        return len(_failures.get(client, [])) >= MAX_FAILURES


def record_failure(client: str) -> None:
    with _lock:
        _failures.setdefault(client, []).append(time.monotonic())


def reset_failures() -> None:
    with _lock:
        _failures.clear()
