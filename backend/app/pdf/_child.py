"""Child process entry point: python -m app.pdf._child <op> <json kwargs> <memory MB>  (PDF bytes on stdin).

Imports only PyMuPDF and the pure parsing functions; never the app's settings, database or keys.
Writes one pickled (status, payload) tuple to its original stdout. Everything else that writes to stdout (MuPDF
prints its own error messages there) is sent to stderr, so it can never corrupt the result.
"""

import json
import os
import pickle
import sys


def _limit_memory(mb: int) -> None:
    if mb <= 0:
        return
    try:
        import resource  # Linux / macOS only

        cap = mb * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (cap, cap))
    except Exception:  # noqa: BLE001 - not available on Windows; the timeout still applies
        pass


def _memprobe(_data: bytes, mb: int) -> str:
    """Start-up check of the memory cap: try to allocate more than the cap allows."""
    block = bytearray(mb * 1024 * 1024)
    block[-1] = 1
    return "allocated"


def main() -> None:
    result_fd = os.dup(1)  # keep the real stdout for the result...
    os.dup2(2, 1)  # ...and send anything else written to stdout (including native code) to stderr
    sys.stdout = sys.stderr
    op, kw, mb = sys.argv[1], json.loads(sys.argv[2]), int(sys.argv[3])
    _limit_memory(mb)
    data = sys.stdin.buffer.read()
    try:
        if op == "memprobe":
            out = ("ok", _memprobe(data, **kw))
        else:
            from app.pdf import inspect as impl

            fn = {"inspect": impl._inspect_impl, "texts": impl._texts_impl, "render": impl._render_impl,
                  "prepare": impl._prepare_impl, "signatures": impl._signatures_impl}[op]
            try:
                out = ("ok", fn(data, **kw))
            except impl.IntakeError as e:
                out = ("intake", (e.code, e.message))
    except MemoryError:
        out = ("error", "memory")
    except Exception as e:  # noqa: BLE001
        out = ("error", "memory" if "malloc" in str(e).lower() or "memory" in str(e).lower() else type(e).__name__)
    with os.fdopen(result_fd, "wb") as f:
        f.write(pickle.dumps(out))


if __name__ == "__main__":
    main()
