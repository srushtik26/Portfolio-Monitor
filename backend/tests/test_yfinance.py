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


def _make_angel_mock_provider(
    *,
    ltp: float = 435.50,
    quote_time_str: str = "03-Nov-2026 10:00:00",
    clock_now: datetime | None = None,
    call_log: list[str] | None = None,
):
    from app.angel_rest import AngelRestConfig, AngelRestQuoteProvider

    cfg = AngelRestConfig(
        api_key="test-angel-key",
        client_code="TEST001",
        pin="1234",
        totp_secret="JBSWY3DPEHPK3PXP",
        stale_after_seconds=300,
    )

    def fake_http_post(url: str, payload: dict, headers: dict, timeout: float) -> dict:
        if call_log is not None:
            call_log.append(url)
        if url.endswith("/loginByPassword"):
            assert headers["X-PrivateKey"] == "test-angel-key"
            return {"status": True, "data": {"jwtToken": "mock-jwt-token"}}
        assert url.endswith("/market/v1/quote/")
        assert headers["Authorization"] == "Bearer mock-jwt-token"
        return {
            "status": True,
            "data": {
                "fetched": [
                    {
                        "exchange": "NSE",
                        "tradingSymbol": "BHEL-EQ",
                        "ltp": ltp,
                        "exchFeedTime": quote_time_str,
                    }
                ]
            },
        }

    return AngelRestQuoteProvider(
        cfg,
        http_post=fake_http_post,
        totp_generator=lambda _s: "123456",
        clock=(lambda: clock_now) if clock_now is not None else None,
    )


def test_coordinator_uses_yfinance_primary_first_and_labels_delayed_candle_data() -> None:
    from app.provider_coordinator import SharedQuoteCoordinator, YFINANCE_DELAYED_LABEL

    now = datetime(2026, 11, 3, 4, 30, tzinfo=timezone.utc)
    angel_calls: list[str] = []
    primary = YFinanceProvider(
        history_fetcher=lambda _ticker: FakeHistory(432.50, now - timedelta(seconds=30)),
        clock=lambda: now,
        cache_ttl_seconds=10,
        stale_after_seconds=300,
    )
    backup = _make_angel_mock_provider(ltp=440.00, clock_now=now, call_log=angel_calls)
    coord = SharedQuoteCoordinator(
        primary=primary,
        backup=backup,
        clock=lambda: now,
        cache_ttl_seconds=0,
        stale_after_seconds=300,
        min_request_interval_seconds=0.0,
    )

    res = coord.fetch_coordinated_quote("NSE", "BHEL", allow_backup=True, now=now)
    assert res.source == "yfinance"
    assert res.source_label == YFINANCE_DELAYED_LABEL
    assert "delayed candle data" in res.source_label.lower()
    assert res.fallback_active is False
    assert res.quote.price == Decimal("432.5")
    assert angel_calls == []  # Angel backup must NOT be called when yfinance succeeds


def test_coordinator_falls_back_to_angel_on_failure_and_staleness_recovers_after_two_good_fetches_and_resets_baselines(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.models import TargetAlertState, TargetRevision
    from app.prices import NseBseExchangeCalendar
    from app.provider_coordinator import SharedQuoteCoordinator

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    SessionFactory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    now = datetime(2026, 11, 3, 4, 30, tzinfo=timezone.utc)
    with SessionFactory() as session:
        session.add(
            Company(
                exchange="NSE",
                symbol="BHEL",
                name="Bharat Heavy Electricals Ltd",
                base_target=Decimal("400.00"),
                bull_target=Decimal("500.00"),
                base_direction="below",
                bull_direction="above",
                base_trigger_mode="crosses",
                bull_trigger_mode="crosses",
                is_approved=True,
                monitoring_active=True,
            )
        )
        session.flush()
        rev = TargetRevision(
            exchange="NSE",
            symbol="BHEL",
            version=1,
            base_target=Decimal("400.00"),
            bull_target=Decimal("500.00"),
            base_direction="below",
            bull_direction="above",
            source="import",
            changed_at=now,
        )
        session.add(rev)
        session.flush()
        session.add(
            TargetAlertState(
                exchange="NSE",
                symbol="BHEL",
                target_kind="base",
                is_simulated=False,
                state="armed",
                target_revision_id=rev.id,
                last_evaluated_price=Decimal("410.00"),
                last_evaluated_at=now - timedelta(minutes=1),
                last_condition_met=False,
                trigger_cycle=0,
            )
        )
        session.commit()

    # Step 1: yfinance returns stale bar -> falls back to Angel REST + resets cross baseline
    # Step 2: yfinance returns 1st good bar -> stays on Angel (needs 2 consecutive good fetches)
    # Step 3: yfinance returns 2nd good bar -> returns to yfinance + resets cross baseline
    yf_sequence = iter(
        [
            FakeHistory(415.00, now - timedelta(minutes=10)),  # stale (>300s)
            FakeHistory(420.00, now - timedelta(seconds=20)),  # 1st good
            FakeHistory(421.00, now - timedelta(seconds=10)),  # 2nd good -> recover
        ]
    )
    primary = YFinanceProvider(
        history_fetcher=lambda _ticker: next(yf_sequence),
        clock=lambda: now,
        cache_ttl_seconds=10,
        stale_after_seconds=300,
        sleeper=lambda _d: None,
    )
    backup = _make_angel_mock_provider(ltp=395.00, clock_now=now)
    coord = SharedQuoteCoordinator(
        primary=primary,
        backup=backup,
        clock=lambda: now,
        cache_ttl_seconds=0,
        stale_after_seconds=300,
        min_request_interval_seconds=0.0,
    )

    monkeypatch.setenv("ANGEL_OWNER_ENABLED", "true")
    monkeypatch.setenv("ALERT_RULES_ENABLED", "true")

    # Cycle 1: yfinance stale -> falls back to Angel (395.00 <= 400.00, but cross baseline was reset!)
    r1 = poll_once(coord, NseBseExchangeCalendar(), now=now, session_factory=SessionFactory)
    assert r1.recorded == 1
    with SessionFactory() as session:
        q1 = session.scalars(select(PriceQuote).order_by(PriceQuote.id.desc())).first()
        assert q1 is not None
        assert "Angel One REST" in q1.provider
        assert q1.price == Decimal("395.000000")
        # Crossing baseline reset means no inferred crossing alert across the source change
        assert session.scalar(select(func.count(TargetAlert.id))) == 0

    # Cycle 2: 1st good primary fetch -> still on Angel (1 < 2)
    r2 = poll_once(
        coord,
        NseBseExchangeCalendar(),
        now=now + timedelta(seconds=15),
        session_factory=SessionFactory,
    )
    assert r2.recorded == 1
    with SessionFactory() as session:
        q2 = session.scalars(select(PriceQuote).order_by(PriceQuote.id.desc())).first()
        assert q2 is not None
        assert "Angel One REST" in q2.provider

    # Cycle 3: 2nd consecutive good primary fetch -> returns to yfinance!
    r3 = poll_once(
        coord,
        NseBseExchangeCalendar(),
        now=now + timedelta(seconds=30),
        session_factory=SessionFactory,
    )
    assert r3.recorded == 1
    with SessionFactory() as session:
        q3 = session.scalars(select(PriceQuote).order_by(PriceQuote.id.desc())).first()
        assert q3 is not None
        assert "Yahoo Finance" in q3.provider
        assert q3.price == Decimal("421.000000")

    Base.metadata.drop_all(bind=engine)
    engine.dispose()


def test_missing_angel_config_keeps_yfinance_working_and_never_uses_fixture(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.provider_coordinator import SharedQuoteCoordinator

    for key in ("ANGEL_API_KEY", "ANGEL_CLIENT_CODE", "ANGEL_PIN", "ANGEL_TOTP_SECRET", "SHOONYA_USER_ID"):
        monkeypatch.delenv(key, raising=False)
    # Even if SHOONYA_USER_ID is set, it must NOT activate Shoonya or Angel
    monkeypatch.setenv("SHOONYA_USER_ID", "legacy-shoonya-id")

    now = datetime(2026, 11, 3, 4, 30, tzinfo=timezone.utc)
    primary = YFinanceProvider(
        history_fetcher=lambda _ticker: FakeHistory(430.00, now - timedelta(seconds=20)),
        clock=lambda: now,
        cache_ttl_seconds=10,
    )
    coord = SharedQuoteCoordinator(
        primary=primary,
        clock=lambda: now,
        cache_ttl_seconds=0,
        min_request_interval_seconds=0.0,
    )
    assert coord.backup_status(allow_backup=True) == "not_configured"
    res = coord.fetch_coordinated_quote("NSE", "BHEL", allow_backup=True, now=now)
    assert res.source == "yfinance"
    assert res.quote.is_simulated is False

    # When yfinance fails and Angel is unconfigured, it must raise ProviderError (never fixture fallback!)
    failing_primary = YFinanceProvider(
        history_fetcher=lambda _ticker: (_ for _ in ()).throw(ProviderError("Yahoo down")),
        clock=lambda: now,
        cache_ttl_seconds=10,
        sleeper=lambda _d: None,
    )
    failing_coord = SharedQuoteCoordinator(
        primary=failing_primary,
        clock=lambda: now,
        cache_ttl_seconds=0,
        min_request_interval_seconds=0.0,
    )
    with pytest.raises(ProviderError, match="Quote unavailable"):
        failing_coord.fetch_coordinated_quote("NSE", "BHEL", allow_backup=True, now=now)


def test_shared_queue_and_cache_and_manual_poll_safety_gates_and_non_owner_isolation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fastapi.testclient import TestClient
    from app.database import get_db
    from app.main import app
    from app.models import User
    from app.provider_coordinator import (
        SharedQuoteCoordinator,
        reset_shared_coordinator,
    )
    from app.security import get_current_user

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    SessionFactory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    now = datetime(2026, 11, 3, 4, 30, tzinfo=timezone.utc)
    with SessionFactory() as session:
        owner = User(
            name="Owner Admin",
            normalized_name="owner admin",
            role="ADMIN",
            password_hash="x",
            is_active=True,
            created_at=now,
            updated_at=now,
        )
        employee = User(
            name="Team Employee",
            normalized_name="team employee",
            role="EMPLOYEE",
            password_hash="x",
            is_active=True,
            created_at=now,
            updated_at=now,
        )
        session.add_all([owner, employee])
        session.add(
            Company(
                exchange="NSE",
                symbol="BHEL",
                name="Bharat Heavy Electricals Ltd",
                base_target=Decimal("450.00"),
                bull_target=Decimal("500.00"),
                base_direction="below",
                bull_direction="above",
                is_approved=True,
                monitoring_active=True,
            )
        )
        session.commit()
        owner_id, emp_id = owner.id, employee.id

    yf_fetch_count = 0

    def stale_yf(_ticker: str) -> FakeHistory:
        nonlocal yf_fetch_count
        yf_fetch_count += 1
        # 10 minutes old -> exceeds configured 300s staleness (proves no 86400s override)
        return FakeHistory(410.00, now - timedelta(minutes=10))

    primary = YFinanceProvider(
        history_fetcher=stale_yf,
        clock=lambda: now,
        cache_ttl_seconds=60,
        stale_after_seconds=300,
        sleeper=lambda _d: None,
    )
    backup = _make_angel_mock_provider(ltp=438.25, clock_now=now)
    coord = SharedQuoteCoordinator(
        primary=primary,
        backup=backup,
        clock=lambda: now,
        cache_ttl_seconds=60,
        stale_after_seconds=300,
        min_request_interval_seconds=0.0,
    )
    reset_shared_coordinator(coord)

    # Alert safety gate: ALERT_RULES_ENABLED=false must prevent target alerts on manual poll
    monkeypatch.setenv("ALERT_RULES_ENABLED", "false")
    monkeypatch.setenv("NOTIFICATION_MODE", "dry_run")

    active_user_id = {"id": owner_id}

    def override_get_db():
        with SessionFactory() as s:
            yield s

    def override_user():
        with SessionFactory() as s:
            return s.get(User, active_user_id["id"])

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user] = override_user

    try:
        client = TestClient(app)
        # 1. Owner calls manual poll -> yfinance is stale (>300s), so Angel backup activates
        r_owner_poll = client.post("/api/price-feed/poll?force_refresh=true")
        assert r_owner_poll.status_code == 200
        body = r_owner_poll.json()
        assert body["recorded"] == 1
        assert body["fallback_used"] is True
        assert body["backup_status"] == "configured"
        assert body["alerts_enabled"] is False
        assert body["symbols"][0]["source"] == "angel_rest"
        assert body["symbols"][0]["fallback_reason"] == "primary_stale"

        # Safety gate held: no target alerts fired while ALERT_RULES_ENABLED=false
        with SessionFactory() as s:
            assert s.scalar(select(func.count(TargetAlert.id))) == 0

        # 2. Cached read via shared coordinator does not re-hit upstream when force_refresh=false
        prev_yf_count = yf_fetch_count
        r_cached = client.post("/api/price-feed/poll?force_refresh=false")
        assert r_cached.status_code == 200
        assert r_cached.json()["symbols"][0]["from_cache"] is True
        assert yf_fetch_count == prev_yf_count

        # 3. Owner sees Angel quote in companies, history, and worker status
        owner_companies = client.get("/api/companies").json()
        assert owner_companies[0]["latest_quote"]["provider"] == "Angel One REST (backup)"
        owner_history = client.get("/api/companies/NSE/BHEL/history").json()
        assert len(owner_history["items"]) >= 1
        owner_status = client.get("/api/worker/status").json()
        assert owner_status["backup_status"] == "configured"
        assert owner_status["fallback_active_count"] == 1

        # 4. Switch to non-owner EMPLOYEE: Angel quotes, history, health, and cache MUST be isolated!
        active_user_id["id"] = emp_id
        emp_companies = client.get("/api/companies").json()
        assert emp_companies[0]["latest_quote"] is None
        emp_history = client.get("/api/companies/NSE/BHEL/history").json()
        assert emp_history["items"] == []
        emp_details = client.get("/api/companies/NSE/BHEL/details").json()
        assert emp_details["quote_history"] == []
        emp_status = client.get("/api/worker/status").json()
        assert emp_status["backup_status"] == "restricted"
        assert emp_status["fallback_active_count"] == 0
        assert all(s["source"] != "angel_rest" for s in emp_status["symbols"])

        # Non-owner calling manual poll MUST NOT receive Angel's cached quote or activate Angel
        r_emp_poll = client.post("/api/price-feed/poll?force_refresh=false")
        assert r_emp_poll.status_code == 200
        emp_body = r_emp_poll.json()
        assert emp_body["backup_status"] == "restricted"
        assert emp_body["fallback_used"] is False
        assert emp_body["symbols"][0]["source"] == "yfinance"
    finally:
        app.dependency_overrides.clear()
        reset_shared_coordinator(None)
        Base.metadata.drop_all(bind=engine)
        engine.dispose()
