"""Run the app locally:  python backend/scripts/serve.py [--fixture-reader] [--port 8000]

--fixture-reader  use the TEST-ONLY gold-label reader instead of Gemini (e.g. when the free quota is used up).
                  Every reading is labelled "test-fixture" in the UI. Never used in production.
"""
import argparse
import os
import subprocess
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

p = argparse.ArgumentParser()
p.add_argument("--fixture-reader", action="store_true")
p.add_argument("--port", type=int, default=8000)
args = p.parse_args()
if args.fixture_reader:
    os.environ["EXTRACTION_MODE"] = "fixture"

subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], cwd=BACKEND, check=True)

import uvicorn  # noqa: E402

os.chdir(BACKEND)
uvicorn.run("app.main:app", host="127.0.0.1", port=args.port, log_level="info")
