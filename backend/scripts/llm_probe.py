"""Check LLM access without printing any API key.

Usage: python scripts/llm_probe.py            -> one tiny call per configured key (costs 1 request per key)
       python scripts/llm_probe.py --models   -> also list the flash/lite models visible to key 1
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from openai import OpenAI  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.extraction.provider import _classify  # noqa: E402

s = get_settings()
keys = s.llm_api_keys
model = (s.llm_model.split(",")[0] or "").strip()
print(f"endpoint: {s.llm_base_url}  model: {model or '(not set)'}  keys configured: {len(keys)}")
if not keys or not model:
    sys.exit(1)

if "--models" in sys.argv:
    client = OpenAI(base_url=s.llm_base_url, api_key=keys[0], timeout=30)
    for m in sorted(m.id for m in client.models.list()):
        if "flash" in m or "lite" in m:
            print("  ", m)

for i, key in enumerate(keys, 1):
    client = OpenAI(base_url=s.llm_base_url, api_key=key, timeout=120, max_retries=0)
    t = time.time()
    try:
        r = client.chat.completions.create(model=model, messages=[{"role": "user", "content": "Reply with the word OK."}],
                                           reasoning_effort="low", max_tokens=200)
        text = (r.choices[0].message.content or "").strip()
        print(f"key {i} (length {len(key)}): ok in {time.time() - t:.1f}s, reply {text[:20]!r}")
    except Exception as e:  # report the classification and a short message only
        err = _classify(e)
        detail = str(e).replace(key, "***")
        quota_metric = next((w for w in detail.replace("'", " ").replace('"', " ").split() if "PerDay" in w or "PerMinute" in w), "")
        print(f"key {i} (length {len(key)}): {err.kind} after {time.time() - t:.1f}s {quota_metric}")
