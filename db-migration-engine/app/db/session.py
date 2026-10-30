from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, DeclarativeBase
from sqlalchemy.pool import StaticPool
from app.core.config import settings


def _build_engine(url: str):
    """
    Build the internal app engine.
    - PostgreSQL / MySQL: real connection pool.
    - SQLite (used by the test suite): no pool args, shared connection so
      the DB behaves consistently across threads.
    """
    if url.startswith("sqlite"):
        return create_engine(
            url,
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
    return create_engine(
        url,
        pool_pre_ping=True,   # reconnect on stale connections
        pool_size=10,
        max_overflow=20,
        echo=settings.DEBUG,
    )


# Internal app engine — stores migration configs, runs, audit logs
engine = _build_engine(settings.DATABASE_URL)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    pass


def get_db():
    """FastAPI dependency — yields a DB session and closes it after request."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
