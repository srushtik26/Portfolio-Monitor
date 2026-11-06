import os
from collections.abc import Generator
from typing import Any

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker


def get_database_url() -> str:
    return os.environ.get("DATABASE_URL", "sqlite:///./portfolio.db")


class Base(DeclarativeBase):
    pass


def _create_engine() -> Engine:
    url = get_database_url()
    is_sqlite = url.startswith("sqlite")
    connect_args: dict[str, Any] = (
        {"check_same_thread": False, "timeout": 30.0} if is_sqlite else {}
    )
    engine_kwargs: dict[str, Any] = {"connect_args": connect_args}
    if not is_sqlite:
        engine_kwargs.update(
            {
                "pool_size": int(os.environ.get("DB_POOL_SIZE", "20")),
                "max_overflow": int(os.environ.get("DB_MAX_OVERFLOW", "30")),
                "pool_timeout": float(os.environ.get("DB_POOL_TIMEOUT", "15")),
                "pool_recycle": int(os.environ.get("DB_POOL_RECYCLE", "1800")),
                "pool_pre_ping": True,
            }
        )
    db_engine = create_engine(url, **engine_kwargs)
    if is_sqlite and ":memory:" not in url:

        @event.listens_for(db_engine, "connect")
        def _configure_sqlite_connection(dbapi_connection: Any, _connection_record: Any) -> None:
            cursor = dbapi_connection.cursor()
            try:
                cursor.execute("PRAGMA journal_mode=WAL;")
                cursor.execute("PRAGMA synchronous=NORMAL;")
                cursor.execute("PRAGMA busy_timeout=5000;")
            finally:
                cursor.close()

    return db_engine


engine = _create_engine()
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_db() -> Generator[Session, None, None]:
    with SessionLocal() as session:
        yield session

