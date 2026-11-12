import os

# Point the app at the TEST database and disable the background worker before anything imports settings.
os.environ["DATABASE_URL"] = os.environ.get("TEST_DATABASE_URL",
                                            "postgresql+psycopg://postgres@127.0.0.1:55432/vendor_onboarding_test")
os.environ["WORKER_ENABLED"] = "false"
# PDF parsing runs in-process for speed here; test_pdf_isolation.py exercises the real child-process sandbox.
os.environ.setdefault("PDF_ISOLATION", "false")
os.environ["APP_ENV"] = "test"
os.environ["LLM_API_KEY"] = ""
os.environ["LIVE_LOOKUPS"] = "false"  # no network calls to public verification sources from tests

import pytest  # noqa: E402


@pytest.fixture(scope="session")
def migrated_db():
    from pathlib import Path

    from alembic import command
    from alembic.config import Config
    from sqlalchemy import create_engine, text

    url = os.environ["DATABASE_URL"]
    # This fixture DROPS the whole schema: refuse anything that isn't clearly a test database.
    from sqlalchemy.engine import make_url
    name = make_url(url).database or ""
    assert "test" in name and name != "vendor_onboarding", f"refusing to reset non-test database {name!r}"
    eng = create_engine(url)
    with eng.begin() as c:
        c.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public;"))
    eng.dispose()
    cfg = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    cfg.set_main_option("script_location", str(Path(__file__).resolve().parents[1] / "migrations"))
    command.upgrade(cfg, "head")
    yield url


@pytest.fixture(scope="session")
def app_client(migrated_db):
    from fastapi.testclient import TestClient

    from app.extraction.service import FixtureReader, set_reader_override
    from app.main import app

    set_reader_override(FixtureReader())
    with TestClient(app) as c:
        yield c


# Every row lock the application takes must be FOR NO KEY UPDATE (see app/db.py): a plain FOR UPDATE conflicts with
# the KEY SHARE locks of foreign-key checks, which is how an audit insert joined a lock cycle. This
# runs for every statement the whole suite sends, so any code path the tests exercise is covered, raw SQL included.
def _no_plain_row_locks(conn, cursor, statement, parameters, context, executemany):
    import re

    if re.search(r"\bFOR\s+UPDATE\b", statement, re.IGNORECASE):
        raise AssertionError(f"plain FOR UPDATE row lock (use FOR NO KEY UPDATE, see app/db.py): {statement[:300]}")


def _install_row_lock_guard():
    from sqlalchemy import event
    from sqlalchemy.engine import Engine

    event.listen(Engine, "before_cursor_execute", _no_plain_row_locks)


_install_row_lock_guard()
