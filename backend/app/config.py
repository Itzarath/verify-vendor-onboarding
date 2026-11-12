from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]
BACKEND_ROOT = PROJECT_ROOT / "backend"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=PROJECT_ROOT / ".env", extra="ignore")

    app_env: str = "dev"  # dev | test | prod
    database_url: str = "postgresql+psycopg://postgres@127.0.0.1:55432/vendor_onboarding"
    session_secret: str = "dev-only-session-secret-change-me"
    # Shared code that must be entered before anything else (hosted demo: keeps strangers from uploading files or
    # spending the model quota). Unset = no gate (local development). Required in production.
    demo_access_code: str | None = None
    db_pool_size: int = 5

    # Extraction model (OpenAI-compatible endpoint). Gemini by default.
    llm_base_url: str = "https://generativelanguage.googleapis.com/v1beta/openai/"
    llm_api_key: str | None = None
    # Extra keys for the same endpoint and model, used in rotation (LLM_API_KEY_2, LLM_API_KEY_3).
    llm_api_key_2: str | None = None
    llm_api_key_3: str | None = None
    llm_model: str = ""  # the main model (hosted: gemini-3.8-flash); backups go in LLM_FALLBACK_*
    llm_timeout_s: float = 240.0  # measured reads of one document took 10-121 s (median 73 s); leave headroom
    llm_reasoning_effort: str | None = "low"
    # Optional backup models, tried in this order only when every earlier model failed (overloaded, timed out,
    # refused): LLM_FALLBACK_* first, then LLM_FALLBACK_2_*, then LLM_FALLBACK_3_*. Each needs all three settings.
    llm_fallback_base_url: str | None = None
    llm_fallback_api_key: str | None = None
    llm_fallback_model: str | None = None
    llm_fallback_2_base_url: str | None = None
    llm_fallback_2_api_key: str | None = None
    llm_fallback_2_model: str | None = None
    llm_fallback_3_base_url: str | None = None
    llm_fallback_3_api_key: str | None = None
    llm_fallback_3_model: str | None = None
    # With a backup configured, a model gets this many seconds in total before the next one takes over (0 = no limit).
    # Live, an overloaded main model answered 503 only after up to 2.5 minutes or not at all, while the backup reads a
    # document in 10-55 s: waiting the full timeout (twice, with retries) made a fresh read take 5-7 minutes.
    llm_handover_s: float = 90.0

    # Calls to free public verification sources (the RBI/Razorpay IFSC directory). Off in tests: no network there.
    live_lookups: bool = True

    # Upload limits (technical, separate from business document requirements).
    max_file_bytes: int = 10 * 1024 * 1024
    max_pages_per_file: int = 10
    max_files_per_revision: int = 8
    max_pages_per_revision: int = 30
    max_total_upload_mb: int = 300  # whole-demo storage budget (documents are stored in the database)

    # Untrusted PDFs are parsed in a killable child process with these limits (isolation off = unit tests only).
    pdf_isolation: bool = True
    pdf_timeout_s: float = 20.0
    pdf_memory_mb: int = 700  # address-space cap for the child on Linux; ignored on Windows
    pdf_max_children: int = 2  # PDF child processes at once (1 on a small hosted instance)

    # live = the configured model; fixture = TEST ONLY gold-label reader (labelled in the UI, refused in prod)
    extraction_mode: str = "live"

    worker_enabled: bool = True
    # Safety re-check when the queue is truly empty. New work wakes the worker at once (nudge), a stopped worker's run
    # is picked up when its lease expires, and database errors are retried within a minute, so this can be long: each
    # check wakes a scale-to-zero database (Neon pauses 5 idle minutes later), so hourly costs ~15 CU-hours a month.
    worker_poll_s: float = 3600.0
    run_lease_s: int = 180
    max_run_attempts: int = 3

    frontend_dist: Path = PROJECT_ROOT / "frontend" / "dist"

    @field_validator("database_url")
    @classmethod
    def _psycopg_url(cls, v: str) -> str:
        """Hosted providers hand out postgres:// or postgresql:// URLs; this app talks to Postgres through psycopg 3."""
        for prefix in ("postgres://", "postgresql://"):
            if v.startswith(prefix):
                return "postgresql+psycopg://" + v[len(prefix):]
        return v

    @field_validator("demo_access_code")
    @classmethod
    def _trimmed_code(cls, v: str | None) -> str | None:
        """Pasted settings often carry spaces or a newline; entered codes are trimmed too, so trim the setting once
        here. The start-up check, code matching and the access cookie all use this one value."""
        v = (v or "").strip()
        return v or None

    @property
    def llm_backups(self) -> list[tuple[str, str | None, str | None, str | None]]:
        """(name, base URL, API key, model) of each backup slot, in order, including incomplete ones."""
        return [(f"LLM_FALLBACK{sfx.upper()}", getattr(self, f"llm_fallback{sfx}_base_url"),
                 getattr(self, f"llm_fallback{sfx}_api_key"), getattr(self, f"llm_fallback{sfx}_model"))
                for sfx in ("", "_2", "_3")]

    @property
    def llm_api_keys(self) -> list[str]:
        """All configured keys for the main endpoint, in order, blanks and duplicates removed."""
        keys: list[str] = []
        for k in (self.llm_api_key, self.llm_api_key_2, self.llm_api_key_3):
            k = (k or "").strip()
            if k and k not in keys:
                keys.append(k)
        return keys


@lru_cache
def get_settings() -> Settings:
    return Settings()
