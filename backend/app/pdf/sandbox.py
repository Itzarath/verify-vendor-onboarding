"""Parse untrusted PDFs in a separate process that can be killed.

A small file can still demand a lot of work (for example one text run of hundreds of
thousands of glyphs). Each operation runs in a fresh child process with a wall-clock
timeout and, on Linux, an address-space limit. Only a few children (PDF_MAX_CHILDREN) run at once, so a hostile
upload can't starve the web server or the evaluation worker.

`PDF_ISOLATION=false` runs the same code in-process (unit tests only; refused in prod).
"""

from __future__ import annotations

import json
import os
import pickle
import subprocess
import sys
import threading

from ..config import BACKEND_ROOT, get_settings
from .inspect import IntakeError

_SLOTS = threading.BoundedSemaphore(max(1, get_settings().pdf_max_children))

OPS = {"inspect", "texts", "render", "prepare", "signatures", "memprobe"}
TOO_COMPLEX = ("This PDF could not be processed within the safety limits (memory or structure). Please upload a "
               "simpler copy (for example a scan, or a 'print to PDF' version of the document).")
SELF_TEST: dict = {}  # result of the start-up check, shown in /api/health?details=1


def _in_process(op: str, data: bytes, kw: dict):
    from . import inspect as impl

    return {"inspect": impl._inspect_impl, "texts": impl._texts_impl, "render": impl._render_impl,
            "prepare": impl._prepare_impl, "signatures": impl._signatures_impl}[op](data, **kw)


def _child(op: str, data: bytes, memory_mb: int | None, kw: dict) -> tuple[str, object]:
    """Run one operation in a fresh child. Returns (outcome, payload); outcome is the child's own status ("ok",
    "intake", "error") or what went wrong around it ("busy", "timeout", "crash", "unreadable")."""
    s = get_settings()
    # "prepare" extracts text AND renders every page as an image for the model, so it gets three times the budget.
    timeout = s.pdf_timeout_s * (3 if op == "prepare" else 1)
    # Minimal environment: the child never needs (and never gets) API keys or database credentials.
    env = {k: os.environ[k] for k in ("PATH", "SYSTEMROOT", "TEMP", "TMP", "HOME", "LANG") if k in os.environ}
    # Waiting for a free slot is bounded too, so a burst of hostile uploads can't queue requests indefinitely.
    if not _SLOTS.acquire(timeout=timeout):
        return "busy", None
    try:
        cap = s.pdf_memory_mb if memory_mb is None else memory_mb
        proc = subprocess.run([sys.executable, "-m", "app.pdf._child", op, json.dumps(kw), str(cap)],
                              input=data, capture_output=True, timeout=timeout, cwd=str(BACKEND_ROOT), env=env)
    except subprocess.TimeoutExpired:
        return "timeout", None
    finally:
        _SLOTS.release()
    if proc.returncode != 0 or not proc.stdout:
        return "crash", proc.returncode  # killed, crashed, or out of memory before it could report
    try:
        status, payload = pickle.loads(proc.stdout)  # our own child's output, not user data
    except Exception:  # noqa: BLE001 - an unreadable result is a refusal, never a server error
        return "unreadable", None
    return status, payload


def run(op: str, data: bytes, memory_mb: int | None = None, **kw):
    assert op in OPS
    s = get_settings()
    if not s.pdf_isolation:
        if s.app_env == "prod":
            raise RuntimeError("PDF isolation cannot be disabled in production")
        return _in_process(op, data, kw)
    status, payload = _child(op, data, memory_mb, kw)
    if status == "ok":
        return payload
    if status == "intake":
        raise IntakeError(*payload)
    if status == "busy":
        raise IntakeError("busy", "Document processing is busy right now. Please try again in a minute.")
    if status == "timeout":
        raise IntakeError("too_complex", "This PDF takes too long to process safely. Please upload a simpler copy "
                                         "(for example a scan, or a 'print to PDF' version of the document).")
    if status in ("crash", "unreadable") or payload == "memory":
        raise IntakeError("too_complex", TOO_COMPLEX)
    raise IntakeError("corrupt_pdf", "The PDF could not be read; it may be damaged.")


def self_test() -> dict:
    """Start-up check: run one small PDF through the real child process with the configured limits, so a hosted
    environment where the sandbox can't work (for example a memory cap too low to start PyMuPDF) is visible at once."""
    import time

    s = get_settings()
    if not s.pdf_isolation:
        SELF_TEST.update(status="skipped", reason="isolation off (tests only)")
        return SELF_TEST
    import pymupdf

    from .inspect import inspect_pdf, prepare_pages

    doc = pymupdf.open()
    doc.new_page().insert_text((72, 90), "SYNTHETIC SELF TEST - PAN AAHCN7316M - visible text for the start-up check")
    data = doc.tobytes()
    t = time.monotonic()
    try:
        info = inspect_pdf(data, s.max_file_bytes, s.max_pages_per_file)
        texts, images = prepare_pages(data)
        ok = info.layout == "digital" and "AAHCN7316M" in texts[0] and images and images[0][:4] == b"\x89PNG"
        SELF_TEST.update(status="ok" if ok else "failed", reason=None if ok else "unexpected result")
    except Exception as exc:  # noqa: BLE001 - reported, not raised: the API stays up and says what is wrong
        SELF_TEST.update(status="failed", reason=f"{type(exc).__name__}: {exc}"[:300])
    SELF_TEST.update(seconds=round(time.monotonic() - t, 2), memory_cap_mb=s.pdf_memory_mb,
                     max_children=s.pdf_max_children)
    # Is the memory cap actually enforced here? A child capped at 200 MB tries to allocate 300 MB. Safe either way:
    # if the cap is missing, 300 MB still fits a 512 MB instance.
    # Only an allocation refused inside the child counts as proof; a busy slot, timeout or crash proves nothing.
    if sys.platform.startswith("linux"):
        enforced, note = memory_cap_check()
        SELF_TEST["memory_cap_enforced"] = enforced
        if note:
            SELF_TEST["memory_probe"] = note
    else:
        SELF_TEST["memory_cap_enforced"] = None  # address-space limits are a Linux/macOS feature
    try:
        import resource

        SELF_TEST["child_peak_rss_mb"] = round(resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss / 1024, 1)
    except Exception:  # noqa: BLE001 - not available on Windows
        pass
    return SELF_TEST


def memory_cap_check() -> tuple[bool | None, str | None]:
    """A child capped at 200 MB tries to allocate 300 MB (safe either way on a 512 MB instance). True only if the
    allocation was refused inside the child; False if it succeeded; None (with the reason) for anything else."""
    outcome, payload = _child("memprobe", b"", 200, {"mb": 300})
    if (outcome, payload) == ("error", "memory"):
        return True, None
    if (outcome, payload) == ("ok", "allocated"):
        return False, None
    return None, f"inconclusive: {outcome} {payload}"[:120]
