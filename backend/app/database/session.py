"""SQLAlchemy engine and request-scoped session management."""

from collections.abc import Generator
from functools import lru_cache

from sqlalchemy import Engine, create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings


def ensure_test_database_url(database_url: str) -> None:
    """Refuse destructive test setup unless the database is clearly isolated."""
    database_name = make_url(database_url).database or ""
    if database_name != "sou2ai_test":
        raise ValueError("Tests must use the isolated sou2ai_test database.")


@lru_cache
def get_engine() -> Engine:
    """Create one process-wide thread-safe engine and connection pool."""
    settings = get_settings()
    return create_engine(
        settings.postgresql_database_url,
        pool_pre_ping=True,
        connect_args={
            "connect_timeout": settings.postgresql_connect_timeout_seconds,
        },
    )


@lru_cache
def get_session_factory() -> sessionmaker[Session]:
    """Return the session factory used by FastAPI dependencies."""
    return sessionmaker(bind=get_engine(), expire_on_commit=False)


def get_db_session() -> Generator[Session]:
    """Yield one session for a request and always release its connection."""
    with get_session_factory()() as session:
        try:
            yield session
        except Exception:
            session.rollback()
            raise
