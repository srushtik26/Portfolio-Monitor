import os
import pytest

_ISOLATED_ENV_KEYS = (
    "PRICE_PROVIDER",
    "EXCHANGE_CALENDAR",
    "ALERT_RULES_ENABLED",
    "MARKET_HOURS_OVERRIDE",
    "YFINANCE_CACHE_TTL_SECONDS",
    "YFINANCE_STALE_AFTER_SECONDS",
    "SHOONYA_ALLOW_UNVERIFIED_AUTH",
    "SHOONYA_USER_ID",
    "SHOONYA_PASSWORD",
    "SHOONYA_TOTP_SECRET",
    "SHOONYA_VENDOR_CODE",
    "SHOONYA_API_SECRET",
    "SHOONYA_IMEI",
    "SHOONYA_HOST",
    "SHOONYA_WS_ENDPOINT",
    "SHOONYA_QUOTE_FRESHNESS_SECONDS",
    "SHOONYA_TICK_QUEUE_MAXSIZE",
    "SHOONYA_RECONNECT_BASE_SECONDS",
    "SHOONYA_RECONNECT_MAX_SECONDS",
    "SHOONYA_MAX_RECONNECT_ATTEMPTS",
    "SHOONYA_TOKEN_MAP_JSON",
    "NOTIFICATION_MODE",
    "ALERT_EMAIL_RECIPIENTS",
    "SMTP_HOST",
    "SMTP_PORT",
    "SMTP_USER",
    "SMTP_PASSWORD",
    "SMTP_FROM",
    "SMTP_USE_TLS",
    "SMTP_SPOOL_DIR",
)


@pytest.fixture(autouse=True)
def _isolate_test_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure local .env or shell variables never leak into simulated unit tests."""
    for key in _ISOLATED_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)

