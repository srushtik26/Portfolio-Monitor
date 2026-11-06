from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Callable

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.models import Company, NotificationOutbox, PriceQuote, TargetAlert, TargetRevision, WorkerHeartbeat
from app.shoonya_feed import (
    ShoonyaAuthError,
    ShoonyaConfig,
    ShoonyaPriceProvider,
    ShoonyaSubscriptionError,
    ShoonyaTokenResolver,
    ShoonyaWebSocketService,
    generate_totp,
    parse_shoonya_tick,
)

test_engine = create_engine(
    "sqlite://",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
TestSession = sessionmaker(bind=test_engine, autoflush=False, expire_on_commit=False)


@pytest.fixture(autouse=True)
def _setup_db():
    Base.metadata.create_all(bind=test_engine)
    yield
    Base.metadata.drop_all(bind=test_engine)


class FakeNorenSdk:
    """Deterministic fake of NorenRestApiPy.NorenApi for testing WebSocket & REST flows."""

    def __init__(self, *, fail_logins: int = 0) -> None:
        self.fail_logins = fail_logins
        self.login_calls: list[dict[str, str]] = []
        self.subscribed_batches: list[Any] = []
        self.unsubscribed_batches: list[Any] = []
        self.closed = False
        self.subscribe_callback: Callable[[dict[str, Any]], None] | None = None
        self.socket_open_callback: Callable[[], None] | None = None
        self.socket_close_callback: Callable[[], None] | None = None
        self.socket_error_callback: Callable[[Any], None] | None = None

    def login(
        self,
        userid: str,
        password: str,
        twoFA: str,
        vendor_code: str,
        api_secret: str,
        imei: str,
    ) -> dict[str, Any]:
        self.login_calls.append(
            {
                "userid": userid,
                "password": password,
                "twoFA": twoFA,
                "vendor_code": vendor_code,
                "api_secret": api_secret,
                "imei": imei,
            }
        )
        if len(self.login_calls) <= self.fail_logins:
            return {"stat": "Not_Ok", "emsg": "Invalid TOTP or Session Expired"}
        return {"stat": "Ok", "susertoken": "fake-session-token-123"}

    def searchscrip(self, exchange: str, searchtext: str) -> dict[str, Any]:
        if exchange == "NSE" and searchtext == "CUSTOMCO":
            return {
                "stat": "Ok",
                "values": [
                    {"exch": "NSE", "token": "998877", "tsym": "CUSTOMCO-EQ", "cname": "Custom Co Ltd"},
                ],
            }
        return {"stat": "Not_Ok", "emsg": "No Data"}

    def get_quotes(self, exchange: str, token: str) -> dict[str, Any]:
        return {
            "stat": "Ok",
            "exch": exchange,
            "token": token,
            "lp": "426.50",
            "ft": str(int(datetime(2026, 11, 6, 6, 0, tzinfo=timezone.utc).timestamp())),
        }

    def start_websocket(
        self,
        subscribe_callback: Callable[[dict[str, Any]], None] | None = None,
        order_update_callback: Callable[[dict[str, Any]], None] | None = None,
        socket_open_callback: Callable[[], None] | None = None,
        socket_close_callback: Callable[[], None] | None = None,
        socket_error_callback: Callable[[Any], None] | None = None,
    ) -> None:
        self.subscribe_callback = subscribe_callback
        self.socket_open_callback = socket_open_callback
        self.socket_close_callback = socket_close_callback
        self.socket_error_callback = socket_error_callback
        if self.socket_open_callback:
            self.socket_open_callback()

    def subscribe(self, instrument: str | list[str], feed_type: Any = None) -> None:
        self.subscribed_batches.append(instrument)

    def unsubscribe(self, instrument: str | list[str], feed_type: Any = None) -> None:
        self.unsubscribed_batches.append(instrument)

    def close_websocket(self) -> None:
        self.closed = True


def test_shoonya_config_from_env_and_totp_generation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SHOONYA_USER_ID", "FA98765")
    monkeypatch.setenv("SHOONYA_PASSWORD", "super-secret-pwd")
    monkeypatch.setenv("SHOONYA_TOTP_SECRET", "JBSWY3DPEHPK3PXP")
    monkeypatch.setenv("SHOONYA_VENDOR_CODE", "FA98765_U")
    monkeypatch.setenv("SHOONYA_API_SECRET", "app-key-xyz")
    monkeypatch.setenv("SHOONYA_TOKEN_MAP_JSON", '{"NSE:NEWSTOCK": "44556"}')

    cfg = ShoonyaConfig.from_env()
    assert cfg.user_id == "FA98765"
    assert cfg.vendor_code == "FA98765_U"
    assert cfg.api_secret == "app-key-xyz"
    assert cfg.custom_token_map == {("NSE", "NEWSTOCK"): "44556"}

    otp = cfg.current_two_fa()
    assert len(otp) == 6 and otp.isdigit()
    assert generate_totp("JBSWY3DPEHPK3PXP", for_time=1700000000) == generate_totp(
        "JBSWY3DPEHPK3PXP", for_time=1700000005
    )


def test_shoonya_config_missing_env_raises_auth_error(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in (
        "SHOONYA_USER_ID",
        "SHOONYA_UID",
        "SHOONYA_PASSWORD",
        "SHOONYA_PWD",
        "SHOONYA_TOTP_SECRET",
        "SHOONYA_FACTOR2",
        "SHOONYA_API_SECRET",
        "SHOONYA_APP_KEY",
    ):
        monkeypatch.delenv(key, raising=False)

    with pytest.raises(ShoonyaAuthError, match="Missing required Shoonya environment variables"):
        ShoonyaConfig.from_env()


def test_token_resolver_supports_default_custom_and_searchscrip() -> None:
    sdk = FakeNorenSdk()
    resolver = ShoonyaTokenResolver(custom_tokens={("BSE", "CUSTOMBSE"): "599999"})

    # Default mapped BHEL
    assert resolver.format_subscription_key("NSE", "BHEL") == "NSE|438"
    # Custom mapped BSE token
    assert resolver.format_subscription_key("BSE", "CUSTOMBSE") == "BSE|599999"
    # Dynamic lookup via searchscrip
    assert resolver.format_subscription_key("NSE", "CUSTOMCO", sdk=sdk) == "NSE|998877"
    assert resolver.resolve_symbol("NSE", "998877") == "CUSTOMCO"

    with pytest.raises(ShoonyaSubscriptionError):
        resolver.format_subscription_key("NSE", "UNMAPPED_SYMBOL", sdk=sdk)


def test_websocket_subscribes_processes_ticks_and_triggers_bhel_target_alert() -> None:
    with TestSession() as session:
        company = Company(
            exchange="NSE",
            symbol="BHEL",
            name="Bharat Heavy Electricals Ltd",
            cmp=Decimal("433.70"),
            base_target=Decimal("426.91"),
            bull_target=Decimal("561.09"),
            base_direction="below",
            bull_direction="above",
        )
        session.add(company)
        session.flush()
        session.add(
            TargetRevision(
                exchange="NSE",
                symbol="BHEL",
                version=1,
                base_target=company.base_target,
                bull_target=company.bull_target,
                base_direction="below",
                bull_direction="above",
                source="import",
                changed_at=datetime(2026, 11, 6, 5, 0, tzinfo=timezone.utc),
            )
        )
        session.commit()

    cfg = ShoonyaConfig(
        user_id="FA11111",
        password="secret",
        vendor_code="FA11111_U",
        api_secret="api-key-111",
        totp_secret="JBSWY3DPEHPK3PXP",
    )
    fake_sdk = FakeNorenSdk()
    service = ShoonyaWebSocketService(
        cfg,
        sdk_factory=lambda h, w: fake_sdk,
        session_factory=TestSession,
    )

    service.connect_once()
    assert service.is_connected is True
    assert fake_sdk.subscribed_batches == ["NSE|438"]

    # Simulate incoming live touchline feed ('tf') tick for BHEL at 426.85 (<= 426.91 Base Target)
    assert fake_sdk.subscribe_callback is not None
    fake_sdk.subscribe_callback(
        {
            "t": "tf",
            "e": "NSE",
            "tk": "438",
            "lp": "426.85",
            "ft": str(int(datetime.now(timezone.utc).timestamp())),
        }
    )

    with TestSession() as session:
        quote = session.scalar(select(PriceQuote).where(PriceQuote.symbol == "BHEL"))
        assert quote is not None
        assert quote.provider == "shoonya"
        assert quote.price == Decimal("426.850000")
        assert quote.status == "fresh"

        alert = session.scalar(select(TargetAlert))
        assert alert is not None
        assert alert.target_kind == "base"
        assert alert.price == Decimal("426.850000")
        assert session.scalar(select(func.count(NotificationOutbox.id))) >= 1

        hb = session.get(WorkerHeartbeat, "price")
        assert hb is not None
        assert hb.provider == "shoonya"
        assert hb.websocket_status == "connected"


def test_websocket_reconnection_with_exponential_backoff() -> None:
    cfg = ShoonyaConfig(
        user_id="FA11111",
        password="secret",
        vendor_code="FA11111_U",
        api_secret="api-key-111",
        totp_secret="JBSWY3DPEHPK3PXP",
        reconnect_base_seconds=1.5,
        reconnect_max_seconds=10.0,
        max_reconnect_attempts=3,
    )
    sleeps: list[float] = []
    created_sdks: list[FakeNorenSdk] = []

    def sdk_factory(host: str, ws: str) -> FakeNorenSdk:
        idx = len(created_sdks)
        # First SDK fails login; second SDK succeeds and opens WebSocket, then stops service
        sdk = FakeNorenSdk(fail_logins=1 if idx == 0 else 0)
        created_sdks.append(sdk)
        return sdk

    service: ShoonyaWebSocketService | None = None

    def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        if service is not None and service.is_connected:
            service.stop()

    service = ShoonyaWebSocketService(
        cfg,
        sdk_factory=sdk_factory,
        session_factory=TestSession,
        sleep_fn=fake_sleep,
    )
    service.run_forever(health_check_interval=0.25)

    # First attempt failed login -> slept 1.5s -> second attempt reconnected & succeeded!
    assert sleeps[0] == 1.5
    assert len(created_sdks) == 2
    with TestSession() as session:
        hb = session.get(WorkerHeartbeat, "price")
        assert hb is not None
        assert hb.reconnect_count == 1


def test_shoonya_rest_price_provider_fetches_quote() -> None:
    cfg = ShoonyaConfig(
        user_id="FA11111",
        password="secret",
        vendor_code="FA11111_U",
        api_secret="api-key-111",
        totp_secret="JBSWY3DPEHPK3PXP",
    )
    provider = ShoonyaPriceProvider(cfg, sdk=FakeNorenSdk())
    quote = provider.fetch_quote("NSE", "BHEL")
    assert quote.exchange == "NSE"
    assert quote.symbol == "BHEL"
    assert quote.provider == "shoonya"
    assert quote.price == Decimal("426.50")


def test_async_worker_consumes_live_tick_stream_and_evaluates_target_rules() -> None:
    import asyncio
    from collections.abc import AsyncIterator
    from app.prices import NseBseExchangeCalendar
    from app.worker import consume_tick_stream

    with TestSession() as session:
        company = Company(
            exchange="NSE",
            symbol="BHEL",
            name="Bharat Heavy Electricals Ltd",
            cmp=Decimal("433.70"),
            base_target=Decimal("426.91"),
            bull_target=Decimal("561.09"),
            base_direction="below",
            bull_direction="above",
            base_trigger_mode="crosses",
            bull_trigger_mode="crosses",
            rearm_policy="rearm_after_recovery",
        )
        session.add(company)
        session.flush()
        session.add(
            TargetRevision(
                exchange="NSE",
                symbol="BHEL",
                version=1,
                base_target=company.base_target,
                bull_target=company.bull_target,
                base_direction="below",
                bull_direction="above",
                source="import",
                changed_at=datetime(2026, 11, 6, 5, 0, tzinfo=timezone.utc),
            )
        )
        session.commit()

    base_epoch = int(datetime.now(timezone.utc).timestamp()) - 30

    async def fake_shoonya_stream() -> AsyncIterator[dict[str, Any]]:
        # 1. Tick at 430.00 (between base 426.91 and bull 561.09)
        yield {"t": "tf", "e": "NSE", "tk": "438", "lp": "430.00", "ft": str(base_epoch + 1)}
        # 2. Tick crosses downward to 426.80 -> triggers Base Target Alert
        yield {"t": "tf", "e": "NSE", "tk": "438", "lp": "426.80", "ft": str(base_epoch + 2)}
        # 3. Tick crosses upward to 562.50 -> triggers Bull Target Alert
        yield {"t": "tf", "e": "NSE", "tk": "438", "lp": "562.50", "ft": str(base_epoch + 3)}

    result = asyncio.run(
        consume_tick_stream(
            fake_shoonya_stream(),
            NseBseExchangeCalendar(),
            session_factory=TestSession,
            max_ticks=3,
        )
    )

    assert result.received == 3
    assert result.recorded == 3
    assert result.failed == 0

    with TestSession() as session:
        quotes = list(session.scalars(select(PriceQuote).order_by(PriceQuote.id)))
        assert [q.price for q in quotes] == [
            Decimal("430.000000"),
            Decimal("426.800000"),
            Decimal("562.500000"),
        ]
        alerts = list(session.scalars(select(TargetAlert).order_by(TargetAlert.id)))
        assert [(a.target_kind, a.price, a.comparison_direction) for a in alerts] == [
            ("base", Decimal("426.800000"), "below"),
            ("bull", Decimal("562.500000"), "above"),
        ]
        hb = session.get(WorkerHeartbeat, "price")
        assert hb is not None
        assert hb.provider == "shoonya"
        assert hb.websocket_status == "connected"


def test_uploaded_excel_rules_query_and_high_frequency_tick_pool_optimization() -> None:
    import asyncio
    from collections.abc import AsyncIterator
    from app.models import ImportBatch, ImportRow
    from app.prices import (
        LIVE_TICK_DEDUPLICATOR,
        NseBseExchangeCalendar,
        ProviderQuote,
        evaluate_live_cmp_tick,
        query_uploaded_excel_rules,
    )
    from app.worker import consume_tick_stream

    LIVE_TICK_DEDUPLICATOR.invalidate()
    now = datetime.now(timezone.utc)
    with TestSession() as session:
        batch = ImportBatch(
            id="excel-batch-1",
            filename="portfolio_targets.xlsx",
            version_number=1,
            uploaded_by="Srushti Kadam",
            status="confirmed",
            created_at=now,
            approved_at=now,
        )
        session.add(batch)
        session.flush()
        row = ImportRow(
            batch_id=batch.id,
            row_number=2,
            exchange="NSE",
            symbol="BHEL",
            name="Bharat Heavy Electricals Ltd",
            cmp=Decimal("433.70"),
            base_target=Decimal("426.91"),
            bull_target=Decimal("561.09"),
            cell_refs={"base_target": "E2", "bull_target": "F2"},
            approval_status="approved",
            errors=[],
            warnings=[],
        )
        session.add(row)
        company = Company(
            exchange="NSE",
            symbol="BHEL",
            name="Bharat Heavy Electricals Ltd",
            cmp=Decimal("433.70"),
            base_target=Decimal("426.91"),
            bull_target=Decimal("561.09"),
            base_direction="below",
            bull_direction="above",
            version=1,
        )
        session.add(company)
        session.commit()

        # Verify query_uploaded_excel_rules retrieves the confirmed ImportRow and materializes TargetRevision
        rules = query_uploaded_excel_rules(session, "NSE", "BHEL", current_time=now)
        assert rules is not None
        assert rules.company.symbol == "BHEL"
        assert rules.import_row is not None
        assert rules.import_row.batch_id == "excel-batch-1"
        assert rules.revision is not None
        assert rules.revision.base_target == Decimal("426.910000")
        assert rules.revision.bull_target == Decimal("561.090000")
        session.commit()

    # Track how many sessions are checked out during a high-frequency burst of 25 ticks
    session_checkouts = 0

    def counting_session_factory() -> Session:
        nonlocal session_checkouts
        session_checkouts += 1
        return TestSession()

    base_epoch = int(now.timestamp()) - 40

    async def high_freq_burst_stream() -> AsyncIterator[dict[str, Any]]:
        # 20 rapid identical-price ticks at 430.00 (no alert; 19 of 20 deduplicated in memory)
        for i in range(20):
            yield {"t": "tf", "e": "NSE", "tk": "438", "lp": "430.00", "ft": str(base_epoch + i)}
        # 1 tick dropping below Base target (426.50 <= 426.91) -> writes Base alert to notification_outbox
        yield {"t": "tf", "e": "NSE", "tk": "438", "lp": "426.50", "ft": str(base_epoch + 21)}
        # 1 tick rising above Bull target (565.00 >= 561.09) -> writes Bull alert to notification_outbox
        yield {"t": "tf", "e": "NSE", "tk": "438", "lp": "565.00", "ft": str(base_epoch + 22)}

    result = asyncio.run(
        consume_tick_stream(
            high_freq_burst_stream(),
            NseBseExchangeCalendar(),
            session_factory=counting_session_factory,
            max_ticks=22,
            heartbeat_interval_seconds=60.0,
            dedup_window_seconds=2.0,
        )
    )

    assert result.received == 22
    # Only 3 distinct price changes hit the DB (430.00, 426.50, 565.00)
    assert result.recorded == 3
    assert result.failed == 0
    # Total session checkouts across 22 ticks is strictly bounded (<< 22)
    assert session_checkouts <= 12

    with TestSession() as session:
        outbox_rows = list(session.scalars(select(NotificationOutbox).order_by(NotificationOutbox.created_at)))
        assert len(outbox_rows) == 2
        alerts = list(session.scalars(select(TargetAlert).order_by(TargetAlert.id)))
        assert len(alerts) == 2
        assert alerts[0].target_kind == "base"
        assert alerts[0].price == Decimal("426.500000")
        assert alerts[1].target_kind == "bull"
        assert alerts[1].price == Decimal("565.000000")


def test_sse_live_stream_emits_price_tick_distance_metrics_and_outbox_alert() -> None:
    from collections.abc import Generator
    from fastapi.testclient import TestClient
    from app.database import get_db
    from app.main import app
    from app.prices import ProviderQuote, record_quote

    def override_get_db() -> Generator[Session, None, None]:
        with TestSession() as session:
            yield session

    now = datetime.now(timezone.utc)
    with TestSession() as session:
        company = Company(
            exchange="NSE",
            symbol="BHEL",
            name="Bharat Heavy Electricals Ltd",
            cmp=Decimal("433.70"),
            base_target=Decimal("426.91"),
            bull_target=Decimal("561.09"),
            base_direction="below",
            bull_direction="above",
            version=1,
        )
        session.add(company)
        session.flush()
        session.add(
            TargetRevision(
                exchange="NSE",
                symbol="BHEL",
                version=1,
                base_target=company.base_target,
                bull_target=company.bull_target,
                base_direction="below",
                bull_direction="above",
                source="import",
                changed_at=now,
            )
        )
        session.commit()

        record_quote(
            session,
            ProviderQuote(
                exchange="NSE",
                symbol="BHEL",
                price=Decimal("426.85"),
                currency="INR",
                provider="shoonya",
                quoted_at=now,
                is_simulated=False,
                quote_type="websocket_tick",
                freshness_seconds=300,
            ),
            now=now,
            alerts_enabled=True,
        )

    previous_override = app.dependency_overrides.get(get_db)
    app.dependency_overrides[get_db] = override_get_db
    try:
        with TestClient(app) as client:
            res = client.get(
                "/api/stream/live?since_quote_id=0&include_existing_outbox=true&max_events=3"
            )
            assert res.status_code == 200
            assert "text/event-stream" in res.headers.get("content-type", "")
            body = res.text
            assert "event: connected" in body
            assert "event: price_tick" in body
            assert "event: outbox_alert" in body
            assert '"distance_to_base_abs": -0.06' in body
            assert '"alert_status": "base_triggered"' in body
            assert '"symbol": "BHEL"' in body
    finally:
        if previous_override is None:
            app.dependency_overrides.pop(get_db, None)
        else:
            app.dependency_overrides[get_db] = previous_override




