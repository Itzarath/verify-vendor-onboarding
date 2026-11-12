from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.exc import DBAPIError

from .api.routes import api
from .config import get_settings
from .seed import seed
from .services.commands import ApiError

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


DEFAULT_SECRET = "dev-only-session-secret-change-me"


def check_production_settings(s) -> None:
    """Refuse to start a production server with demo-only or unsafe settings."""
    if s.app_env != "prod":
        return
    problems = []
    if s.session_secret == DEFAULT_SECRET or len(s.session_secret or "") < 32:
        problems.append("SESSION_SECRET must be set to a random value of at least 32 characters")
    if s.extraction_mode != "live":
        problems.append("EXTRACTION_MODE must be 'live' (the fixture reader is for tests only)")
    if not s.pdf_isolation:
        problems.append("PDF_ISOLATION cannot be disabled")
    # The shared access code is optional (the hosted demo is open); a code that is set must not be trivially short.
    if s.demo_access_code and len(s.demo_access_code.strip()) < 8:
        problems.append("DEMO_ACCESS_CODE, when set, must be at least 8 characters")
    if not s.llm_api_keys or not s.llm_model.strip():
        problems.append("LLM_MODEL and at least one LLM_API_KEY must be set")
    if "," in s.llm_model:
        problems.append("LLM_MODEL names one main model (backups go in LLM_FALLBACK_*, _2_*, _3_*)")
    for name, url, key, model in s.llm_backups:
        if (url or key or model) and not (url and key and model):
            problems.append(f"backup {name} needs {name}_BASE_URL, {name}_MODEL and {name}_API_KEY together")
        if model and "," in model:
            problems.append(f"{name}_MODEL names one model")
    if problems:
        raise RuntimeError("Unsafe production configuration: " + "; ".join(problems))


@asynccontextmanager
async def lifespan(app: FastAPI):
    check_production_settings(get_settings())
    seed()
    from .pdf.sandbox import self_test
    result = self_test()
    logging.getLogger("startup").log(logging.INFO if result.get("status") != "failed" else logging.ERROR,
                                     "PDF sandbox self-test: %s", result)
    if get_settings().worker_enabled:
        from .pipeline.runner import reassess_outdated
        from .pipeline.worker import start_worker
        reassess_outdated()
        start_worker()
    yield


app = FastAPI(title="Vendor onboarding (PS-2)", lifespan=lifespan)
app.include_router(api)


@app.middleware("http")
async def demo_access_gate(request: Request, call_next):
    """Every API call needs the shared demo access code, except the health check and entering the code itself.
    The page shell (HTML/JS) is public; it only shows the code prompt until the code is entered."""
    from .api import access

    path = request.url.path
    if path.startswith("/api/") and path.rstrip("/") not in access.OPEN_PATHS and not access.has_access(request):
        return JSONResponse({"error": "access_required", "message": "Enter the demo access code to continue."},
                            status_code=401)
    return await call_next(request)


@app.exception_handler(ApiError)
async def api_error(_: Request, exc: ApiError):
    return JSONResponse({"error": exc.code, "message": exc.message, **exc.extra}, status_code=exc.status)


# Two people changing the same application at the same moment: one wins, the other gets a clear retry message.
_CONCURRENCY_STATES = {"40P01", "40001", "55P03"}  # deadlock, serialization failure, lock not available


@app.exception_handler(DBAPIError)
async def db_error(_: Request, exc: DBAPIError):
    state = getattr(getattr(exc, "orig", None), "sqlstate", None)
    if state in _CONCURRENCY_STATES:
        return JSONResponse({"error": "busy", "message": "Someone else changed this application at the same moment. "
                                                         "Reload and try again."}, status_code=409)
    logging.getLogger("api").exception("database error")
    return JSONResponse({"error": "server_error", "message": "Something went wrong on our side."}, status_code=500)


dist = get_settings().frontend_dist
if (dist / "assets").exists():
    app.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")


@app.get("/{path:path}", include_in_schema=False)
async def spa(path: str):
    if path.startswith("api/"):
        return JSONResponse({"error": "not_found", "message": "Not found"}, status_code=404)
    f = dist / path
    if path and f.is_file() and dist in f.resolve().parents:
        return FileResponse(f)
    index = dist / "index.html"
    if index.exists():
        return FileResponse(index)
    return JSONResponse({"message": "Frontend not built yet. API is at /api/health"})
