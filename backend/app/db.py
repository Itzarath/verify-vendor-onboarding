"""Database engine and sessions.

Row locks. Every row lock the application takes is FOR NO KEY UPDATE (SQLAlchemy `with_for_update(key_share=True)`),
in one global order: vendor -> request -> run, then the bank-account transaction lock last (pipeline/crossfacts.py).
FOR NO KEY UPDATE still serialises our own writers on that row. Unlike FOR UPDATE, it does not conflict with the KEY
SHARE lock that PostgreSQL's foreign-key check takes on a parent row whenever a child row (an audit event, a run event)
is inserted. So inserting a row that references a locked vendor, request or run never waits, and foreign-key checks
cannot join a lock cycle. tests/test_cp3r_fixes.py fails if a plain FOR UPDATE is added.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import get_settings


class Base(DeclarativeBase):
    pass


_engine = None
_SessionLocal: sessionmaker | None = None


def get_engine():
    global _engine, _SessionLocal
    if _engine is None:
        settings = get_settings()
        # pre-ping + recycle: managed Postgres (e.g. Neon) closes idle connections and suspends idle compute
        _engine = create_engine(settings.database_url, pool_pre_ping=True, pool_size=settings.db_pool_size,
                                max_overflow=settings.db_pool_size, pool_recycle=300)
        _SessionLocal = sessionmaker(bind=_engine, expire_on_commit=False)
    return _engine


def reset_engine(url: str | None = None):
    """Used by tests to point the app at another database."""
    global _engine, _SessionLocal
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _SessionLocal = None
    if url:
        get_settings().database_url = url
    get_engine()


@contextmanager
def session_scope() -> Iterator[Session]:
    get_engine()
    assert _SessionLocal is not None
    session = _SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_db() -> Iterator[Session]:
    """FastAPI dependency; caller commits explicitly."""
    get_engine()
    assert _SessionLocal is not None
    session = _SessionLocal()
    try:
        yield session
    finally:
        session.close()
