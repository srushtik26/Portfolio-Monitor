from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.models import Company, NotificationOutbox, PriceQuote, TargetAlert
from app.prices import (
    FixtureExchangeCalendar,
    ProviderError,
    YAHOO_TICKER_MAP,
    YFinanceProvider,
    record_quote,
    serialize_quote,
    yahoo_ticker_for,
)
from app.worker import poll_once


class FakeHistory:
    def __init__(
        self,
        close: object = 123.45,
        timestamp: datetime | None = None,
        *,
        currency: str | None = "INR",
        empty: bool = False,
    ) -> None:
        self.empty = empty
        self.index = [] if empty else [timestamp or datetime.now(timezone.utc)]
        self.attrs = {} if currency is None else {"currency": currency}
        self.iloc = self
        self.close = close

    def __getitem__(self, _index: int) -> dict[str, object]:
        return {"Close": self.close}


class MultiHistory:
    def __init__(self, rows: list[tuple[object, datetime]], currency: str = "INR") -> None:
        self.empty = not rows
        self.rows = [{"Close": close} for close, _timestamp in rows]
        self.index = [timestamp for _close, timestamp in rows]
        self.attrs = {"currency": currency}
        self.iloc = self

    def __getitem__(self, position: int) -> dict[str, object]:
        return self.rows[position]


class FakeBatchFrame:
    def __init__(self, frames: dict[str, FakeHistory]) -> None:
        self.frames = frames

    def __getitem__(self, ticker: str) -> FakeHistory:
        return self.frames[ticker]


@pytest.fixture
def db_session() -> Iterator[Session]:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    test_session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with test_session() as session:
        yield session
    Base.metadata.drop_all(bind=engine)
    engine.dispose()


def test_yahoo_mapping_is_explicit_and_exchange_specific() -> None:
    assert yahoo_ticker_for("nse", "tega") == "TEGA.NS"
    with pytest.raises(ProviderError, match="No verified Yahoo Finance ticker mapping"):
        yahoo_ticker_for("BSE", "TEGA")
    with pytest.raises(ProviderError, match="No verified Yahoo Finance ticker mapping"):
        yahoo_ticker_for("NSE", "UNKNOWN")


def test_provider_uses_actual_timezone_aware_bar_timestamp_and_currency() -> None:
    now = datetime(2026, 6, 15, 10, 0, tzinfo=timezone.utc)
    bar_time = now - timedelta(minutes=2)
    provider = YFinanceProvider(
        history_fetcher=lambda ticker: FakeHistory(123.45, bar_time),
        clock=lambda: now,
        cache_ttl_seconds=60,
    )

    quote = provider.fetch_quote("NSE", "TEGA")

    assert quote.price == Decimal("123.45")
    assert quote.currency == "INR"
    assert quote.provider == "Yahoo Finance (unofficial)"
    assert quote.quoted_at == bar_time
    assert quote.quote_type == "candle_close"
    assert quote.freshness_seconds == 300
    assert quote.is_simulated is False


def test_default_fetcher_uses_yahoo_history_currency_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    history = FakeHistory(123, datetime(2026, 6, 15, 10, tzinfo=timezone.utc), currency=None)

    class FakeTicker:
        def __init__(self, ticker: str) -> None:
            assert ticker == "TEGA.NS"

        def history(self, **kwargs: object) -> FakeHistory:
            assert kwargs["interval"] == "1m"
            return history

        def get_history_metadata(self) -> dict[str, str]:
            return {"currency": "INR"}

    monkeypatch.setitem(
        __import__("sys").modules,
        "yfinance",
        SimpleNamespace(Ticker=FakeTicker),
    )
    fetched = YFinanceProvider._default_history_fetcher("TEGA.NS")

    assert fetched.attrs["currency"] == "INR"


def test_provider_rejects_empty_data_invalid_price_and_missing_timezone() -> None:
    now = datetime(2026, 6, 15, 10, 0, tzinfo=timezone.utc)
    cases = [
        FakeHistory(empty=True),
        FakeHistory(float("nan"), now),
        FakeHistory(10, now.replace(tzinfo=None)),
        FakeHistory(10, now, currency=None),
    ]
    for history in cases:
        provider = YFinanceProvider(
            history_fetcher=lambda _ticker, value=history: value,
            clock=lambda: now,
            sleeper=lambda _delay: None,
        )
        with pytest.raises(ProviderError):
            provider.fetch_quote("NSE", "TEGA")


def test_provider_marks_bar_stale_and_rejects_future_timestamp() -> None:
    now = datetime(2026, 6, 15, 10, 0, tzinfo=timezone.utc)
    stale = YFinanceProvider(
        history_fetcher=lambda _ticker: FakeHistory(100, now - timedelta(minutes=6)),
        clock=lambda: now,
    ).fetch_quote("NSE", "TEGA")
    assert stale.freshness_seconds == 300

    future_provider = YFinanceProvider(
        history_fetcher=lambda _ticker: FakeHistory(100, now + timedelta(seconds=1)),
        clock=lambda: now,
        sleeper=lambda _delay: None,
    )
    with pytest.raises(ProviderError, match="future candle timestamp"):
        future_provider.fetch_quote("NSE", "TEGA")


def test_provider_uses_latest_valid_bar_and_its_original_timestamp() -> None:
    now = datetime(2026, 6, 15, 10, 0, tzinfo=timezone.utc)
    previous_bar = now - timedelta(minutes=2)
    newest_bar = now - timedelta(minutes=1)
    history = MultiHistory([(Decimal("100"), previous_bar), (float("nan"), newest_bar)])

    quote = YFinanceProvider(
        history_fetcher=lambda _ticker: history,
        clock=lambda: now,
    ).fetch_quote("NSE", "TEGA")

    assert quote.price == Decimal("100")
    assert quote.quoted_at == previous_bar
    assert quote.freshness_seconds == 300


def test_provider_caches_success_and_retries_with_bounded_backoff() -> None:
    now = datetime(2026, 6, 15, 10, 0, tzinfo=timezone.utc)
    calls = 0

    def fetch(_ticker: str) -> FakeHistory:
        nonlocal calls
        calls += 1
        return FakeHistory(100, now - timedelta(minutes=1))

    provider = YFinanceProvider(history_fetcher=fetch, clock=lambda: now)
    assert provider.fetch_quote("NSE", "TEGA") == provider.fetch_quote("NSE", "TEGA")
    assert calls == 1

    failures = 0
    delays: list[float] = []

    def rate_limited(_ticker: str) -> FakeHistory:
        nonlocal failures
        failures += 1
        raise RuntimeError("HTTP 429 Too Many Requests")

    retrying_provider = YFinanceProvider(
        history_fetcher=rate_limited,
        clock=lambda: now,
        sleeper=delays.append,
    )
    with pytest.raises(ProviderError, match="after 3 attempts"):
        retrying_provider.fetch_quote("NSE", "TEGA")
    assert failures == 3
    assert delays == [2, 4]


def test_provider_batches_only_verified_tickers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(YAHOO_TICKER_MAP, ("NSE", "TEST"), "TEST.NS")
    monkeypatch.setitem(YAHOO_TICKER_MAP, ("NSE", "TEST2"), "TEST2.NS")
    now = datetime(2026, 6, 15, 10, 0, tzinfo=timezone.utc)
    batch_calls: list[dict[str, object]] = []
    frames = {
        "TEST.NS": FakeHistory(10, now - timedelta(minutes=1)),
        "TEST2.NS": FakeHistory(20, now - timedelta(minutes=1)),
    }

    def download(**kwargs: object) -> FakeBatchFrame:
        batch_calls.append(kwargs)
        return FakeBatchFrame(frames)

    monkeypatch.setitem(
        __import__("sys").modules,
        "yfinance",
        SimpleNamespace(download=download),
    )
    provider = YFinanceProvider(clock=lambda: now)

    quotes = provider.fetch_quotes([("NSE", "TEST"), ("NSE", "TEST2")])

    assert set(quotes) == {("NSE", "TEST"), ("NSE", "TEST2")}
    assert len(batch_calls) == 1
    assert batch_calls[0]["tickers"] == "TEST.NS TEST2.NS"
    assert batch_calls[0]["threads"] is False


def test_stale_and_out_of_order_quotes_persist_without_alert_or_outbox(
    db_session: Session,
) -> None:
    now = datetime(2026, 6, 15, 10, 0, tzinfo=timezone.utc)
    db_session.add(
        Company(
            exchange="NSE",
            symbol="TEGA",
            name="Tega Industries",
            base_target=Decimal("100"),
            bull_target=Decimal("150"),
        )
    )
    db_session.commit()
    provider = YFinanceProvider(
        history_fetcher=lambda _ticker: FakeHistory(160, now - timedelta(minutes=6)),
        clock=lambda: now,
    )
    stale = provider.fetch_quote("NSE", "TEGA")
    stale_row = record_quote(db_session, stale, now=now)
    assert stale_row.status == "stale"
    assert stale_row.is_simulated is False
    assert serialize_quote(stale_row, now)["is_fresh"] is False

    fresh_quote = provider._quote_from_history(
        ("NSE", "TEGA"),
        "TEGA.NS",
        FakeHistory(160, now - timedelta(minutes=1)),
    )
    fresh_row = record_quote(db_session, fresh_quote, now=now, alerts_enabled=False)
    db_session.commit()
    older_quote = provider._quote_from_history(
        ("NSE", "TEGA"),
        "TEGA.NS",
        FakeHistory(161, now - timedelta(minutes=2)),
    )
    out_of_order = record_quote(db_session, older_quote, now=now, alerts_enabled=False)
    db_session.commit()

    assert fresh_row.status == "fresh"
    assert out_of_order.status == "out_of_order"
    assert db_session.scalar(select(func.count(TargetAlert.id))) == 0
    assert db_session.scalar(select(func.count(NotificationOutbox.id))) == 0
    assert db_session.scalar(select(func.count(PriceQuote.id))) == 3


def test_price_worker_refuses_yfinance_without_authoritative_calendar() -> None:
    with pytest.raises(ValueError, match="authoritative exchange calendar"):
        poll_once(
            YFinanceProvider(history_fetcher=lambda _ticker: FakeHistory()),
            FixtureExchangeCalendar(),
        )
