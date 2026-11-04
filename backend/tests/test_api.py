from collections.abc import Generator
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from zipfile import ZIP_DEFLATED, ZipFile

import pytest
from fastapi.testclient import TestClient
from openpyxl import Workbook
from sqlalchemy import create_engine, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base, get_db
from app.imports import MAX_FILE_BYTES, MAX_IMPORT_ROWS
from app.main import app
from app.models import (
    Company,
    ImportBatch,
    NotificationOutbox,
    PriceQuote,
    TargetAlert,
    TargetRevision,
    WorkerHeartbeat,
)
from app.notifications import (
    CLAIM_LEASE,
    DryRunNotifier,
    PermanentSendError,
    RetryableSendError,
    SendReceipt,
    drain_outbox,
    retry_delay,
)
from app.prices import (
    ExchangeSession,
    FixtureExchangeCalendar,
    FixturePriceProvider,
    ProviderQuote,
    RateLimitError,
    record_quote,
)
from app.worker import poll_once, single_worker_lock

test_engine = create_engine(
    "sqlite://",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
TestSession = sessionmaker(bind=test_engine, autoflush=False, expire_on_commit=False)


@pytest.fixture
def client() -> Generator[TestClient, None, None]:
    Base.metadata.create_all(bind=test_engine)

    def override_get_db() -> Generator[Session, None, None]:
        with TestSession() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()
    Base.metadata.drop_all(bind=test_engine)


def test_health(client: TestClient) -> None:
    response = client.get("/api/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_live_price_feed_and_alerts_are_disabled_until_configured(
    client: TestClient,
) -> None:
    status = client.get("/api/price-feed/status")

    assert status.status_code == 200
    assert status.json()["provider"] is None
    assert status.json()["exchange_calendar"] is None
    assert status.json()["live_mode"] == "not_configured"
    assert status.json()["alert_rules_enabled"] is False
    assert status.json()["trigger"] == "price >= target"
    assert status.json()["already_above"] == "alert on first eligible quote"


def test_cors_is_restricted_to_local_frontend(client: TestClient) -> None:
    allowed = client.get("/api/health", headers={"Origin": "http://localhost:5173"})
    rejected = client.get("/api/health", headers={"Origin": "https://example.com"})

    assert allowed.headers["access-control-allow-origin"] == "http://localhost:5173"
    assert "access-control-allow-origin" not in rejected.headers


def test_companies_empty_database(client: TestClient) -> None:
    response = client.get("/api/companies")

    assert response.status_code == 200
    assert response.json() == []


def test_companies_return_imported_cmp_without_using_it_as_a_quote(client: TestClient) -> None:
    with TestSession() as session:
        session.add(
            Company(
                exchange="NSE",
                symbol="EXAMPLE",
                name="Example Company",
                sector="Technology",
                cmp=Decimal("101.25"),
                base_target=Decimal("100.00"),
                bull_target=Decimal("125.00"),
            )
        )
        session.commit()

    response = client.get("/api/companies")

    assert response.status_code == 200
    assert response.json() == [
        {
            "name": "Example Company",
            "symbol": "EXAMPLE",
            "exchange": "NSE",
            "sector": "Technology",
            "cmp": 101.25,
            "base_target": 100.0,
            "bull_target": 125.0,
            "version": 1,
            "latest_quote": None,
        }
    ]


def test_exchange_and_symbol_are_company_identity(client: TestClient) -> None:
    with TestSession() as session:
        session.add_all(
            [
                Company(exchange="NSE", symbol="SAME", name="NSE Company"),
                Company(exchange="BSE", symbol="SAME", name="BSE Company"),
            ]
        )
        session.commit()
        session.add(Company(exchange="NSE", symbol="SAME", name="Duplicate"))
        with pytest.raises(IntegrityError):
            session.commit()

    response = client.get("/api/companies")

    assert response.status_code == 200
    assert [(company["exchange"], company["symbol"]) for company in response.json()] == [
        ("BSE", "SAME"),
        ("NSE", "SAME"),
    ]


def csv_upload(
    client: TestClient,
    rows: str,
    *,
    cmp_column: str | None = "D",
    base_column: str | None = "E",
    bull_column: str | None = "F",
):
    data = {
        "header_row": "1",
        "sheet_name": "CSV",
        "exchange_column": "A",
        "symbol_column": "B",
        "name_column": "C",
    }
    if cmp_column:
        data["cmp_column"] = cmp_column
    if base_column:
        data["base_target_column"] = base_column
    if bull_column:
        data["bull_target_column"] = bull_column
    return client.post(
        "/api/imports/preview",
        data=data,
        files={"file": ("mapping.csv", rows.encode("utf-8"), "text/csv")},
    )


def test_preview_does_not_write_targets_and_blank_cmp_stays_missing(client: TestClient) -> None:
    with TestSession() as session:
        session.add(
            Company(
                exchange="NSE",
                symbol="EXAMPLE",
                name="Example Company",
                cmp=Decimal("101.00"),
                base_target=Decimal("90.00"),
                bull_target=Decimal("130.00"),
            )
        )
        session.commit()

    response = csv_upload(
        client,
        "Exchange,Symbol,Company,CMP,Base,Bull\nNSE,EXAMPLE,Example Company,,95,135\n",
    )

    assert response.status_code == 200
    row = response.json()["rows"][0]
    assert row["cmp"] is None
    assert row["current_cmp"] == 101.0
    assert row["current_base_target"] == 90.0
    with TestSession() as session:
        company = session.get(Company, ("NSE", "EXAMPLE"))
        assert company is not None
        assert company.cmp == Decimal("101.00")
        assert company.base_target == Decimal("90.00")
        assert company.bull_target == Decimal("130.00")

    confirmed = client.post(f"/api/imports/{response.json()['id']}/confirm", json={})
    assert confirmed.status_code == 200
    with TestSession() as session:
        company = session.get(Company, ("NSE", "EXAMPLE"))
        assert company is not None
        assert company.cmp is None


def test_labeled_csv_fixture_maps_targets_without_a_real_workbook(client: TestClient) -> None:
    fixture_path = Path(__file__).parent / "fixtures" / "labeled_mapping.csv"
    response = client.post(
        "/api/imports/preview",
        data={
            "header_row": "1",
            "sheet_name": "CSV",
            "exchange_column": "A",
            "symbol_column": "B",
            "name_column": "C",
            "cmp_column": "D",
            "base_target_column": "E",
            "bull_target_column": "F",
            "sector_column": "G",
        },
        files={"file": ("labeled_mapping.csv", fixture_path.read_bytes(), "text/csv")},
    )

    assert response.status_code == 200
    row = response.json()["rows"][0]
    assert row["symbol"] == "CSVFIXTURE"
    assert row["cmp"] is None
    assert row["base_target"] == 88
    assert row["bull_target"] == 102


def test_invalid_and_duplicate_rows_are_reported_and_cannot_be_confirmed(
    client: TestClient,
) -> None:
    response = csv_upload(
        client,
        "Exchange,Symbol,Company,CMP,Base,Bull\n"
        "NSE,DUPLICATE,Duplicate Co,,0,200\n"
        "NSE,DUPLICATE,Duplicate Co,,100,200\n",
    )

    assert response.status_code == 200
    rows = response.json()["rows"]
    assert all(any("duplicate company identity" in error for error in row["errors"]) for row in rows)
    assert any("greater than zero" in error for error in rows[0]["errors"])
    confirmation = client.post(
        f"/api/imports/{response.json()['id']}/confirm", json={}
    )
    assert confirmation.status_code == 409
    assert client.get("/api/companies").json() == []


def test_confirmation_is_idempotent_and_unchanged_targets_create_no_revision(
    client: TestClient,
) -> None:
    with TestSession() as session:
        session.add(
            Company(
                exchange="NSE",
                symbol="SAME",
                name="Same Company",
                base_target=Decimal("100.00"),
                bull_target=Decimal("125.00"),
            )
        )
        session.commit()
    preview = csv_upload(
        client,
        "Exchange,Symbol,Company,CMP,Base,Bull\nNSE,SAME,Same Company,,100,125\n",
    ).json()

    first = client.post(f"/api/imports/{preview['id']}/confirm", json={})
    second = client.post(f"/api/imports/{preview['id']}/confirm", json={})

    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()
    assert first.json()["unchanged"] == 1
    with TestSession() as session:
        assert session.scalar(select(func.count(TargetRevision.id))) == 0
        assert session.scalar(select(func.count(ImportBatch.id))) == 1


def _cached_formula_workbook() -> bytes:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Manager sheet"
    sheet.append(["ignored"])
    sheet.append(["Exchange", "Symbol", "Company", "Base Target", "Bull Target"])
    sheet.append(["NSE", "FORMULA", "Formula Company", "=90+10", 125])
    output = BytesIO()
    workbook.save(output)
    source = BytesIO(output.getvalue())
    patched = BytesIO()
    with ZipFile(source) as original, ZipFile(patched, "w", ZIP_DEFLATED) as result:
        for item in original.infolist():
            contents = original.read(item.filename)
            if item.filename == "xl/worksheets/sheet1.xml":
                contents = contents.replace(
                    b"<f>90+10</f><v /></c>", b"<f>90+10</f><v>100</v></c>"
                )
            result.writestr(item, contents)
    return patched.getvalue()


def test_xlsx_sheet_mapping_and_cached_formula_confirmation(client: TestClient) -> None:
    content = _cached_formula_workbook()
    inspection = client.post(
        "/api/imports/inspect",
        data={"header_row": "2"},
        files={"file": ("targets.xlsx", content, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
    )

    assert inspection.status_code == 200
    assert inspection.json()["sheets"][0]["name"] == "Manager sheet"
    assert inspection.json()["sheets"][0]["headers"][3] == {
        "column": "D",
        "header": "Base Target",
    }
    preview = client.post(
        "/api/imports/preview",
        data={
            "header_row": "2",
            "sheet_name": "Manager sheet",
            "exchange_column": "A",
            "symbol_column": "B",
            "name_column": "C",
            "base_target_column": "D",
            "bull_target_column": "E",
        },
        files={"file": ("targets.xlsx", content, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
    )
    assert preview.status_code == 200
    result = preview.json()
    assert result["formula_review_required"] is True
    assert result["rows"][0]["base_target"] == 100
    assert result["rows"][0]["warnings"]
    blocked = client.post(f"/api/imports/{result['id']}/confirm", json={})
    assert blocked.status_code == 409
    confirmed = client.post(
        f"/api/imports/{result['id']}/confirm",
        json={"review_cached_targets": True},
    )
    assert confirmed.status_code == 200
    assert client.get("/api/companies").json()[0]["base_target"] == 100


def test_formula_without_cached_value_is_blocked(client: TestClient) -> None:
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["Exchange", "Symbol", "Company", "Base", "Bull"])
    sheet.append(["NSE", "NO-CACHE", "No Cache", "=90+10", 125])
    content = BytesIO()
    workbook.save(content)
    preview = client.post(
        "/api/imports/preview",
        data={
            "sheet_name": "Sheet",
            "exchange_column": "A",
            "symbol_column": "B",
            "name_column": "C",
            "base_target_column": "D",
            "bull_target_column": "E",
        },
        files={"file": ("no-cache.xlsx", content.getvalue(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
    )
    assert preview.status_code == 200
    assert any("formula result is missing" in error for error in preview.json()["rows"][0]["errors"])


def test_target_edit_uses_version_and_preserves_history(client: TestClient) -> None:
    with TestSession() as session:
        session.add(
            Company(
                exchange="BSE",
                symbol="VERSIONED",
                name="Versioned Company",
                base_target=Decimal("100.00"),
                bull_target=Decimal("120.00"),
            )
        )
        session.commit()

    first = client.put(
        "/api/companies/BSE/VERSIONED/targets",
        json={"expected_version": 1, "base_target": 105, "bull_target": 120},
    )
    stale = client.put(
        "/api/companies/BSE/VERSIONED/targets",
        json={"expected_version": 1, "base_target": 110},
    )

    assert first.status_code == 200
    assert first.json()["version"] == 2
    assert stale.status_code == 409
    with TestSession() as session:
        assert session.scalar(select(func.count(TargetRevision.id))) == 1
        assert session.get(Company, ("BSE", "VERSIONED")).base_target == Decimal("105.00")


def test_stale_import_preview_is_rejected_after_target_edit(client: TestClient) -> None:
    with TestSession() as session:
        session.add(
            Company(
                exchange="NSE",
                symbol="STALE",
                name="Stale Preview Co",
                base_target=Decimal("100.00"),
                bull_target=Decimal("125.00"),
            )
        )
        session.commit()
    preview = csv_upload(
        client,
        "Exchange,Symbol,Company,CMP,Base,Bull\nNSE,STALE,Stale Preview Co,,110,130\n",
    ).json()
    edited = client.put(
        "/api/companies/NSE/STALE/targets",
        json={"expected_version": 1, "base_target": 105},
    )
    confirmed = client.post(f"/api/imports/{preview['id']}/confirm", json={})

    assert edited.status_code == 200
    assert confirmed.status_code == 409
    company = client.get("/api/companies").json()[0]
    assert company["base_target"] == 105


def test_import_file_and_row_limits(client: TestClient) -> None:
    too_many_rows = (
        "Exchange,Symbol,Company,Base\n"
        + "".join(f"NSE,ROW{number},Company {number},{number + 1}\n" for number in range(MAX_IMPORT_ROWS + 1))
    )
    row_limit = client.post(
        "/api/imports/preview",
        data={
            "exchange_column": "A",
            "symbol_column": "B",
            "name_column": "C",
            "base_target_column": "D",
        },
        files={"file": ("rows.csv", too_many_rows.encode(), "text/csv")},
    )
    file_limit = client.post(
        "/api/imports/inspect",
        data={"header_row": "1"},
        files={"file": ("large.csv", b"x" * (MAX_FILE_BYTES + 1), "text/csv")},
    )

    assert row_limit.status_code == 413
    assert file_limit.status_code == 413


def _create_price_company(
    session: Session,
    *,
    symbol: str = "PRICE",
    base_target: Decimal = Decimal("100.00"),
    bull_target: Decimal = Decimal("120.00"),
) -> Company:
    company = Company(
        exchange="NSE",
        symbol=symbol,
        name="Price Test Company",
        base_target=base_target,
        bull_target=bull_target,
    )
    session.add(company)
    session.flush()
    session.add(
        TargetRevision(
            exchange=company.exchange,
            symbol=company.symbol,
            version=company.version,
            base_target=company.base_target,
            bull_target=company.bull_target,
            source="manual",
            changed_at=datetime(2026, 11, 3, 0, tzinfo=timezone.utc),
        )
    )
    session.commit()
    return company


def _provider_quote(price: str, quoted_at: datetime, *, simulated: bool = False) -> ProviderQuote:
    return ProviderQuote(
        exchange="NSE",
        symbol="PRICE",
        price=Decimal(price),
        currency="INR",
        provider="test-provider",
        quoted_at=quoted_at,
        is_simulated=simulated,
    )


def test_target_rules_cover_below_equal_above_repeat_and_both_targets(
    client: TestClient,
) -> None:
    with TestSession() as session:
        _create_price_company(session)
        start = datetime(2026, 11, 3, 9, tzinfo=timezone.utc)
        recorded = [
            record_quote(
                session,
                _provider_quote(str(value), start + timedelta(seconds=index)),
                now=start + timedelta(seconds=index),
                alerts_enabled=True,
            )
            for index, value in enumerate(("99.99", "100.00", "119.99", "120.00", "130.00"))
        ]
        assert [quote.status for quote in recorded] == ["fresh"] * 5
        alerts = list(session.scalars(select(TargetAlert).order_by(TargetAlert.target_kind)))
        assert [(alert.target_kind, alert.price) for alert in alerts] == [
            ("base", Decimal("100.000000")),
            ("bull", Decimal("120.000000")),
        ]

        repeated = record_quote(
            session,
            _provider_quote("130.00", start + timedelta(seconds=5)),
            now=start + timedelta(seconds=5),
            alerts_enabled=True,
        )
        assert repeated.status == "fresh"
        assert session.scalar(select(func.count(TargetAlert.id))) == 2


def test_stale_simulated_out_of_order_and_invalid_quotes_never_alert(
    client: TestClient,
) -> None:
    with TestSession() as session:
        _create_price_company(session)
        now = datetime(2026, 11, 3, 9, tzinfo=timezone.utc)

        stale = record_quote(
            session,
            _provider_quote("150", now - timedelta(minutes=16)),
            now=now,
            alerts_enabled=True,
        )
        simulated = record_quote(
            session,
            _provider_quote("150", now, simulated=True),
            now=now,
            alerts_enabled=True,
        )
        out_of_order = record_quote(
            session,
            _provider_quote("150", now - timedelta(seconds=1)),
            now=now,
            alerts_enabled=True,
        )
        with pytest.raises(ValueError, match="must not be in the future"):
            record_quote(
                session,
                _provider_quote("150", now + timedelta(seconds=1)),
                now=now,
                alerts_enabled=True,
            )
        with pytest.raises(ValueError, match="positive range"):
            record_quote(
                session,
                _provider_quote("0", now + timedelta(seconds=1)),
                now=now + timedelta(seconds=1),
                alerts_enabled=True,
            )

        assert (stale.status, simulated.status, out_of_order.status) == (
            "stale",
            "simulated",
            "out_of_order",
        )
        assert session.scalar(select(func.count(TargetAlert.id))) == 0
        assert session.scalar(select(func.count(NotificationOutbox.id))) == 0
        assert session.scalar(select(func.count(PriceQuote.id))) == 3


def test_target_revision_change_starts_a_new_once_only_alert_key(
    client: TestClient,
) -> None:
    with TestSession() as session:
        company = _create_price_company(session)
        first_time = datetime(2026, 11, 3, 9, tzinfo=timezone.utc)
        record_quote(
            session,
            _provider_quote("105", first_time),
            now=first_time,
            alerts_enabled=True,
        )
        company.base_target = Decimal("110")
        company.bull_target = Decimal("115")
        company.version += 1
        session.add(
            TargetRevision(
                exchange=company.exchange,
                symbol=company.symbol,
                version=company.version,
                base_target=company.base_target,
                bull_target=company.bull_target,
                source="manual",
                changed_at=first_time + timedelta(seconds=1),
            )
        )
        session.commit()
        second_time = first_time + timedelta(seconds=2)
        record_quote(
            session,
            _provider_quote("116", second_time),
            now=second_time,
            alerts_enabled=True,
        )
        alerts = list(session.scalars(select(TargetAlert)))
        assert len(alerts) == 3
        assert len({(alert.target_revision_id, alert.target_kind) for alert in alerts}) == 3


def test_price_history_and_company_quote_freshness(client: TestClient) -> None:
    with TestSession() as session:
        _create_price_company(session)
        now = datetime.now(timezone.utc)
        record_quote(
            session,
            _provider_quote("101.25", now),
            now=now,
        )

    company = client.get("/api/companies").json()[0]
    history = client.get("/api/companies/NSE/PRICE/history")

    assert company["latest_quote"]["price"] == 101.25
    assert company["latest_quote"]["is_fresh"] is True
    assert company["latest_quote"]["quote_type"] == "last_trade"
    assert company["latest_quote"]["freshness_seconds"] == 900
    assert history.status_code == 200
    assert history.json()["items"][0]["provider"] == "test-provider"
    assert history.json()["items"][0]["currency"] == "INR"
    assert history.json()["items"][0]["quote_type"] == "last_trade"
    assert client.get("/api/companies/NSE/UNKNOWN/history").status_code == 404
    assert client.get("/api/companies/NSE/PRICE/history?limit=501").status_code == 422


def test_target_alerts_and_quotes_persist_after_database_restart(tmp_path: Path) -> None:
    database_path = tmp_path / "restart.db"
    first_engine = create_engine(
        f"sqlite:///{database_path}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=first_engine)
    FirstSession = sessionmaker(bind=first_engine, autoflush=False, expire_on_commit=False)
    now = datetime(2026, 11, 3, 9, tzinfo=timezone.utc)
    with FirstSession() as session:
        _create_price_company(session)
        record_quote(
            session,
            _provider_quote("125", now),
            now=now,
            alerts_enabled=True,
        )
        assert session.scalar(select(func.count(TargetAlert.id))) == 2
    first_engine.dispose()

    restarted_engine = create_engine(
        f"sqlite:///{database_path}",
        connect_args={"check_same_thread": False},
    )
    RestartedSession = sessionmaker(
        bind=restarted_engine, autoflush=False, expire_on_commit=False
    )

    def override_get_db() -> Generator[Session, None, None]:
        with RestartedSession() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    try:
        with TestClient(app) as restarted_client:
            history = restarted_client.get("/api/companies/NSE/PRICE/history")
            assert history.status_code == 200
            assert history.json()["items"][0]["price"] == 125
            with RestartedSession() as session:
                assert session.scalar(select(func.count(TargetAlert.id))) == 2
                assert session.scalar(select(func.count(TargetRevision.id))) == 1
    finally:
        app.dependency_overrides.clear()
        restarted_engine.dispose()


def test_fixture_calendar_honors_holidays_and_special_sessions() -> None:
    calendar = FixtureExchangeCalendar()
    assert calendar.session_for("NSE", date(2026, 1, 1)) is None
    assert calendar.session_for("NSE", date(2026, 1, 3)) is None
    assert calendar.session_for("NSE", date(2026, 11, 3)) == ExchangeSession(
        time(9, 15), time(15, 30)
    )
    special_open = datetime(2026, 11, 8, 12, 30, tzinfo=timezone.utc)
    special_closed = datetime(2026, 11, 8, 13, 31, tzinfo=timezone.utc)
    assert calendar.is_open("NSE", special_open)
    assert not calendar.is_open("NSE", special_closed)


def test_worker_lock_prevents_a_second_worker(tmp_path: Path) -> None:
    lock_path = tmp_path / "worker.lock"
    with single_worker_lock(lock_path):
        with pytest.raises(RuntimeError, match="already holds the lock"):
            with single_worker_lock(lock_path):
                pass


def test_fixture_worker_persists_separate_simulated_event(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class OpenFixtureCalendar(FixtureExchangeCalendar):
        def session_for(self, exchange: str, session_date: date) -> ExchangeSession:
            return self.regular_session

    now = datetime(2026, 11, 3, 4, tzinfo=timezone.utc)
    with TestSession() as session:
        company = Company(
            exchange="NSE",
            symbol="FIXTURE",
            name="Fixture Quote Company",
            base_target=Decimal("90"),
            bull_target=Decimal("110"),
        )
        session.add(company)
        session.flush()
        session.add(
            TargetRevision(
                exchange=company.exchange,
                symbol=company.symbol,
                version=company.version,
                base_target=company.base_target,
                bull_target=company.bull_target,
                source="manual",
                changed_at=datetime.now(timezone.utc),
            )
        )
        session.commit()

    monkeypatch.setenv("ALERT_RULES_ENABLED", "true")
    result = poll_once(
        FixturePriceProvider(clock=lambda: now),
        OpenFixtureCalendar(),
        now=now + timedelta(seconds=5),
        session_factory=TestSession,
    )

    assert result.attempted == result.recorded == 1
    assert result.alerts_enabled is False
    with TestSession() as session:
        quote = session.scalar(select(PriceQuote))
        assert quote is not None
        assert quote.status == "simulated"
        assert quote.provider == "fixture"
        assert quote.is_simulated is True
        fixture_event = session.scalar(select(TargetAlert))
        assert fixture_event is not None
        assert fixture_event.is_simulated is True
        assert session.scalar(select(func.count(NotificationOutbox.id))) == 1


def test_worker_propagates_provider_retry_after(client: TestClient) -> None:
    class LimitedProvider:
        name = "limited-test"
        is_simulated = False

        def fetch_quote(self, exchange: str, symbol: str) -> ProviderQuote:
            raise RateLimitError(45)

    class OpenFixtureCalendar(FixtureExchangeCalendar):
        def session_for(self, exchange: str, session_date: date) -> ExchangeSession:
            return self.regular_session

    with TestSession() as session:
        session.add(Company(exchange="NSE", symbol="LIMITED", name="Limited Company"))
        session.commit()

    with pytest.raises(RateLimitError) as error:
        poll_once(
            LimitedProvider(),
            OpenFixtureCalendar(),
            now=datetime(2026, 11, 3, 4, tzinfo=timezone.utc),
            session_factory=TestSession,
        )
    assert error.value.retry_after_seconds == 45


def _create_notification_event(session: Session, *, now: datetime) -> NotificationOutbox:
    company = _create_price_company(session, symbol="NOTICE")
    quote = PriceQuote(
        exchange=company.exchange,
        symbol=company.symbol,
        price=Decimal("125"),
        currency="INR",
        provider="fixture",
        quote_type="fixture",
        quoted_at=now,
        received_at=now,
        is_simulated=True,
        status="simulated",
    )
    session.add(quote)
    session.flush()
    event = TargetAlert(
        target_revision_id=session.scalar(select(TargetRevision.id)),
        target_kind="base",
        quote_id=quote.id,
        triggered_at=now,
        price=quote.price,
        is_simulated=True,
    )
    session.add(event)
    session.flush()
    outbox = NotificationOutbox(
        id="dry-run-notification",
        target_alert_id=event.id,
        channel="dry-run",
        destination=None,
        delivery_mode="dry_run",
        status="queued",
        attempts=0,
        max_attempts=5,
        created_at=now,
        next_attempt_at=now,
        idempotency_key="portfolio-alert-dry-run-notification",
    )
    session.add(outbox)
    session.commit()
    return outbox


def test_fixture_import_confirmation_quote_event_and_dry_run_notification(
    client: TestClient,
) -> None:
    fixture_path = Path(__file__).parent / "fixtures" / "labeled_mapping.csv"
    preview = client.post(
        "/api/imports/preview",
        data={
            "header_row": "1",
            "sheet_name": "CSV",
            "exchange_column": "A",
            "symbol_column": "B",
            "name_column": "C",
            "cmp_column": "D",
            "base_target_column": "E",
            "bull_target_column": "F",
            "sector_column": "G",
        },
        files={
            "file": (
                "labeled_mapping.csv",
                fixture_path.read_bytes(),
                "text/csv",
            )
        },
    )
    assert preview.status_code == 200
    confirmed = client.post(f"/api/imports/{preview.json()['id']}/confirm", json={})
    assert confirmed.status_code == 200
    assert confirmed.json()["created"] == 1

    now = datetime.now(timezone.utc)
    with TestSession() as session:
        company = session.get(Company, ("NSE", "CSVFIXTURE"))
        assert company is not None
        quote = ProviderQuote(
            exchange="NSE",
            symbol="CSVFIXTURE",
            price=Decimal("95"),
            currency="INR",
            provider="fixture",
            quoted_at=now,
            is_simulated=True,
        )
        saved = record_quote(
            session,
            quote,
            now=now,
            alerts_enabled=True,
            fixture_alerts_enabled=True,
        )
        assert saved.status == "simulated"
        assert session.scalar(select(func.count(TargetAlert.id))) == 1
        assert session.scalar(select(func.count(NotificationOutbox.id))) == 1

    delivery = drain_outbox(
        DryRunNotifier(),
        session_factory=TestSession,
        now=now,
    )
    assert delivery.sent == 1
    assert delivery.retried == delivery.failed == 0
    events = client.get("/api/alerts")
    assert events.status_code == 200
    assert len(events.json()) == 1
    event = events.json()[0]
    assert event["event_type"] == "fixture"
    assert event["delivery_status"] == "sent"
    assert event["delivery_label"] == "Dry run (no network)"
    assert event["is_simulated"] is True
    assert event["quote_status"] == "simulated"
    assert event["last_error"] is None


def test_outbox_retries_with_bounded_backoff_and_eventually_sends(
    client: TestClient,
) -> None:
    now = datetime(2026, 11, 3, 10, tzinfo=timezone.utc)
    with TestSession() as session:
        _create_notification_event(session, now=now)

    class FlakyNotifier:
        supports_idempotency = True

        def __init__(self) -> None:
            self.calls = 0
            self.keys: list[str] = []

        def send(self, message, idempotency_key: str) -> SendReceipt:
            self.calls += 1
            self.keys.append(idempotency_key)
            if self.calls == 1:
                raise RetryableSendError("temporary network error", retry_after_seconds=2)
            return SendReceipt("provider-message-1")

    notifier = FlakyNotifier()
    first = drain_outbox(notifier, session_factory=TestSession, now=now)
    assert first.retried == 1
    with TestSession() as session:
        outbox = session.get(NotificationOutbox, "dry-run-notification")
        assert outbox is not None
        assert outbox.status == "retry"
        assert outbox.attempts == 1
        assert outbox.last_error == "temporary network error"
        retry_at = outbox.next_attempt_at
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=timezone.utc)
        assert retry_at == now + timedelta(seconds=15)
    too_soon = drain_outbox(
        notifier,
        session_factory=TestSession,
        now=retry_at - timedelta(seconds=1),
    )
    assert too_soon.sent == 0
    second = drain_outbox(
        notifier,
        session_factory=TestSession,
        now=retry_at,
    )
    assert second.sent == 1
    assert notifier.calls == 2
    assert notifier.keys[0] == notifier.keys[1]
    with TestSession() as session:
        outbox = session.get(NotificationOutbox, "dry-run-notification")
        assert outbox is not None
        assert outbox.status == "sent"
        assert outbox.provider_message_id == "provider-message-1"
        assert outbox.attempts == 2


def test_permanent_failure_is_exposed_and_does_not_retry(client: TestClient) -> None:
    now = datetime(2026, 11, 3, 10, tzinfo=timezone.utc)
    with TestSession() as session:
        _create_notification_event(session, now=now)

    class RejectedNotifier:
        supports_idempotency = True

        def send(self, message, idempotency_key: str) -> SendReceipt:
            raise PermanentSendError("destination rejected")

    result = drain_outbox(
        RejectedNotifier(),
        session_factory=TestSession,
        now=now,
    )
    assert result.failed == 1
    with TestSession() as session:
        outbox = session.get(NotificationOutbox, "dry-run-notification")
        assert outbox is not None
        assert outbox.status == "failed"
        assert outbox.attempts == 1
        assert outbox.last_error == "destination rejected"
    event = client.get("/api/alerts").json()[0]
    assert event["delivery_status"] == "failed"
    assert event["last_error"] == "destination rejected"


def test_interrupted_send_recovers_on_restart_with_same_idempotency_key(
    client: TestClient,
) -> None:
    now = datetime(2026, 11, 3, 10, tzinfo=timezone.utc)
    with TestSession() as session:
        _create_notification_event(session, now=now)
        outbox = session.get(NotificationOutbox, "dry-run-notification")
        assert outbox is not None
        outbox.status = "sending"
        outbox.attempts = 1
        outbox.claimed_at = now - CLAIM_LEASE - timedelta(seconds=1)
        outbox.claim_token = "abandoned-worker"
        session.commit()

    class CapturingNotifier:
        supports_idempotency = True

        def __init__(self) -> None:
            self.keys: list[str] = []

        def send(self, message, idempotency_key: str) -> SendReceipt:
            self.keys.append(idempotency_key)
            return SendReceipt("recovered-message")

    notifier = CapturingNotifier()
    result = drain_outbox(notifier, session_factory=TestSession, now=now)
    assert result.sent == 1
    assert notifier.keys == ["portfolio-alert-dry-run-notification"]
    with TestSession() as session:
        outbox = session.get(NotificationOutbox, "dry-run-notification")
        assert outbox is not None
        assert outbox.status == "sent"
        assert outbox.attempts == 2
        assert outbox.last_error is None


def test_retry_delay_is_bounded_and_worker_status_is_visible(client: TestClient) -> None:
    assert retry_delay(1) == timedelta(seconds=15)
    assert retry_delay(10) == timedelta(seconds=900)
    assert retry_delay(1, retry_after_seconds=3600) == timedelta(seconds=900)
    status = client.get("/api/worker/status")
    assert status.status_code == 200
    assert status.json()["state"] == "stopped"
    assert status.json()["live_provider_configured"] is False
    assert status.json()["authoritative_calendar_configured"] is False
    assert status.json()["live_alerts_enabled"] is False


def test_confirmed_import_and_history_survive_database_reopen() -> None:
    with TemporaryDirectory() as temporary_directory:
        path = Path(temporary_directory) / "restart.db"
        database_engine = create_engine(
            f"sqlite:///{path.as_posix()}",
            connect_args={"check_same_thread": False},
        )
        Base.metadata.create_all(bind=database_engine)
        database_sessions = sessionmaker(bind=database_engine, autoflush=False, expire_on_commit=False)

        def file_database() -> Generator[Session, None, None]:
            with database_sessions() as session:
                yield session

        previous_override = app.dependency_overrides.get(get_db)
        app.dependency_overrides[get_db] = file_database
        try:
            with TestClient(app) as persistent_client:
                preview = csv_upload(
                    persistent_client,
                    "Exchange,Symbol,Company,CMP,Base,Bull\nNSE,PERSIST,Persistence Co,,75,100\n",
                ).json()
                confirmed = persistent_client.post(
                    f"/api/imports/{preview['id']}/confirm", json={}
                )
                assert confirmed.status_code == 200
        finally:
            if previous_override is None:
                app.dependency_overrides.pop(get_db, None)
            else:
                app.dependency_overrides[get_db] = previous_override
            database_engine.dispose()

        reopened_engine = create_engine(f"sqlite:///{path.as_posix()}")
        reopened_sessions = sessionmaker(bind=reopened_engine)
        with reopened_sessions() as session:
            company = session.get(Company, ("NSE", "PERSIST"))
            assert company is not None
            assert company.base_target == Decimal("75.00")
            assert session.scalar(select(func.count(TargetRevision.id))) == 1
            batch = session.get(ImportBatch, preview["id"])
            assert batch is not None
            assert batch.status == "confirmed"
        reopened_engine.dispose()
