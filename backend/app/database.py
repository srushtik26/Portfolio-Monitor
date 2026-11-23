import os
from collections.abc import Generator
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker


def load_dotenv_if_present() -> None:
    """Load key=value pairs from .env when not running inside pytest."""
    if "PYTEST_CURRENT_TEST" in os.environ or "pytest" in os.sys.modules:
        return
    repo_root = Path(__file__).resolve().parents[2]
    backend_root = Path(__file__).resolve().parents[1]
    for env_path in (backend_root / ".env", repo_root / ".env"):
        if not env_path.is_file():
            continue
        try:
            for raw_line in env_path.read_text(encoding="utf-8").splitlines():
                line = raw_line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                k = key.strip()
                v = val.strip().strip("'").strip('"').strip()
                if k:
                    os.environ.setdefault(k, v)
        except OSError:
            pass


load_dotenv_if_present()


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


def ensure_sqlite_schema(db_engine: Engine = engine) -> None:
    """Ensure SQLite dev/test tables exist and migrate legacy SQLite constraints if needed.

    PostgreSQL schemas are managed exclusively by Alembic migrations (`alembic upgrade head`)
    to prevent `create_all()` from conflicting with Alembic version tracking.
    """
    if not str(db_engine.url).startswith("sqlite"):
        return

    from . import models  # noqa: F401

    Base.metadata.create_all(bind=db_engine)
    with db_engine.begin() as conn:
        for table_name, col_defs in (
            (
                "import_batches",
                (
                    ("uploader_user_id", "INTEGER"),
                    ("approver_user_id", "INTEGER"),
                    ("rejector_user_id", "INTEGER"),
                    ("stored_file_path", "VARCHAR(500)"),
                ),
            ),
            ("target_revisions", (("changed_by_user_id", "INTEGER"),)),
            ("audit_logs", (("user_id", "INTEGER"),)),
            ("users", (("email", "VARCHAR(320)"),)),
        ):
            existing_cols = {
                r[1]
                for r in conn.exec_driver_sql(f"PRAGMA table_info({table_name})").fetchall()
            }
            if existing_cols:
                for col_name, col_type in col_defs:
                    if col_name not in existing_cols:
                        conn.exec_driver_sql(
                            f"ALTER TABLE {table_name} ADD COLUMN {col_name} {col_type}"
                        )

        row = conn.exec_driver_sql(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='price_quotes'"
        ).fetchone()
        if not row or not row[0]:
            return
        ddl: str = row[0]
        needs_migration = (
            "freshness_unknown" not in ddl
            or "quoted_at DATETIME NOT NULL" in ddl
        )
        if not needs_migration:
            return
        conn.exec_driver_sql("PRAGMA foreign_keys=OFF;")
        try:
            conn.exec_driver_sql(
                """
                CREATE TABLE price_quotes_new (
                    id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
                    exchange VARCHAR(3) NOT NULL,
                    symbol VARCHAR(32) NOT NULL,
                    price NUMERIC(18, 6) NOT NULL,
                    currency VARCHAR(3) NOT NULL,
                    provider VARCHAR(100) NOT NULL,
                    quote_type VARCHAR(24) NOT NULL DEFAULT 'last_trade',
                    freshness_seconds INTEGER NOT NULL DEFAULT 900,
                    quoted_at DATETIME NULL,
                    received_at DATETIME NOT NULL,
                    is_simulated BOOLEAN NOT NULL,
                    status VARCHAR(24) NOT NULL,
                    CONSTRAINT ck_price_quotes_positive_price CHECK (price > 0),
                    CONSTRAINT ck_price_quotes_exchange CHECK (exchange IN ('NSE', 'BSE')),
                    CONSTRAINT ck_price_quotes_status CHECK (status IN ('fresh', 'stale', 'out_of_order', 'simulated', 'freshness_unknown')),
                    CONSTRAINT fk_price_quotes_company FOREIGN KEY(exchange, symbol) REFERENCES companies (exchange, symbol)
                );
                """
            )
            conn.exec_driver_sql(
                """
                INSERT INTO price_quotes_new (
                    id, exchange, symbol, price, currency, provider,
                    quote_type, freshness_seconds, quoted_at, received_at,
                    is_simulated, status
                )
                SELECT
                    id, exchange, symbol, price, currency, provider,
                    COALESCE(quote_type, 'last_trade'),
                    COALESCE(freshness_seconds, 900),
                    quoted_at, received_at, is_simulated, status
                FROM price_quotes;
                """
            )
            conn.exec_driver_sql("DROP TABLE price_quotes;")
            conn.exec_driver_sql("ALTER TABLE price_quotes_new RENAME TO price_quotes;")
            conn.exec_driver_sql(
                "CREATE INDEX IF NOT EXISTS ix_price_quotes_company_time ON price_quotes (exchange, symbol, quoted_at);"
            )
        finally:
            conn.exec_driver_sql("PRAGMA foreign_keys=ON;")


