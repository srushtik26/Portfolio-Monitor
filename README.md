# Northstar Portfolio Monitor

A responsive React + TypeScript portfolio monitoring dashboard. Portfolio identity and base/bull targets are stored in a local FastAPI backend. A separate single-instance worker can persist explicitly simulated fixture quotes for development. No live price provider or authoritative exchange calendar is selected, so live alerts remain disabled.

## Run locally

```powershell
cd "C:\path\to\Portfolio-Monitor\backend"
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m alembic upgrade head
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

In a second PowerShell terminal:

```powershell
cd "C:\path\to\Portfolio-Monitor"
$env:VITE_API_BASE_URL = "http://127.0.0.1:8000"
npm install
npm run dev -- --host 127.0.0.1
```

The API listens on loopback only. Its database defaults to `portfolio.db` in the backend working directory; this file is ignored by git. `GET /api/health` checks the API, and `GET /api/companies` returns an initially empty list. Companies are identified by the `(exchange, symbol)` pair. Base and bull targets are independent fields; existing demo buy/sell targets are not copied into them.

The Import targets screen accepts `.xlsx` and `.csv` files. It inspects available worksheets and columns, and requires explicit worksheet, header-row, and column mapping for exchange, symbol, company name, and at least one target. Optional CMP and sector columns are also mapped explicitly. Previews are stored without changing companies; confirmation applies valid rows atomically. Duplicate identities, invalid values, name mismatches, missing formula results, stale previews, and files over 10 MB or 2,000 data rows are rejected. Blank CMP imports are stored as missing values. Cached formula targets are flagged and require explicit review because openpyxl does not recalculate formulas. Repeated confirmation is idempotent.

Target Management edits base/bull targets through a version-checked API. Target revisions are retained in SQLite, and unchanged values do not create new revisions. XLSX parsing requires `openpyxl`; multipart file uploads require `python-multipart`. Backend tests, including labeled XLSX fixtures because no real workbook was supplied, run with `python -m pytest`.

## Price worker and target alerts

The web application does not poll prices. Run the separate worker in its own PowerShell terminal after database migrations. The only included adapter is a clearly labeled fixture; it is simulated, only defines `NSE:FIXTURE`, and cannot trigger live alerts:

```powershell
cd "C:\path\to\Portfolio-Monitor\backend"
.\.venv\Scripts\python.exe -m app.worker --provider fixture --calendar fixture --once
```

For repeated fixture polling, omit `--once` and optionally set `--interval-seconds 60`. The worker acquires a process lock so another worker cannot run concurrently. Rate limits are handled using the provider's retry-after delay. Its fixture calendar uses Asia/Kolkata time, regular weekday session hours, and test-only holiday/special-session examples; it is not an authoritative NSE/BSE calendar. No paid, broker, or free-library data provider is included or selected. Live mode stays `not_configured` until a live provider and an authoritative exchange calendar are explicitly chosen and implemented.

Quotes record exchange/symbol, price, currency, provider, quote time, receive time, simulated provenance, and freshness/order status. `GET /api/companies/{exchange}/{symbol}/history` returns recent quote history; `GET /api/price-feed/status` reports the unconfigured live mode. Portfolio shows the latest quote and freshness separately from imported CMP.

The proposed target rule is **one alert per target revision, target kind, and provenance**; the database keeps simulated fixture events separate from live-rule events. Both base and bull targets trigger when price is greater than or equal to the target; the first eligible quote already above the target triggers immediately. These semantics were confirmed, but live activation remains disabled because there is no selected live provider or exchange calendar. Stale, future/invalid, out-of-order, and simulated quotes cannot trigger live alerts. Fixture quotes can create only separately marked fixture events for no-network testing. Polling samples prices at intervals and can miss brief touches that occur between polls; it does not guarantee detection of every intraday crossing.

## Notifications and backups

Target event insertion and its notification outbox row are committed in the same database transaction. The outbox uses queued/retry/sending/sent/failed states, attempt counts, last errors, a five-attempt limit, bounded exponential backoff (15 seconds through 15 minutes), a single-worker process lock, conditional claims, and five-minute interrupted-send recovery. The only notifier in this project is a no-network dry-run sink; no real channel or destination is configured. Fixture events are marked simulated and always remain on the dry-run path.

Each outbox row keeps the same idempotency key across retry/recovery. A future live adapter must pass that key to the provider when supported. If a provider accepts a message but the process stops before recording success, local deduplication alone cannot guarantee exactly-once delivery; without provider idempotency delivery can be duplicated. Permanent errors and exhausted attempts are visible in Price Alerts and `GET /api/alerts`.

The Price Alerts screen shows backend events, delivery state/attempts/errors, quote freshness, and worker/provider/calendar status separately from illustrative demo notifications. Notifications are not sent by entering a phone number. Before adding any channel integration, explicitly select a channel and verify its destination; WhatsApp is not enabled just by providing a number. Keep all provider credentials in ignored backend-only environment configuration, never Vite variables.

To back up SQLite safely (including any WAL state), stop the API and worker, then from the backend folder run:

```powershell
New-Item -ItemType Directory -Force .\backups | Out-Null
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
python -c "import sqlite3,sys; src=sqlite3.connect(sys.argv[1]); dst=sqlite3.connect(sys.argv[2]); src.backup(dst); dst.close(); src.close()" .\portfolio.db ".\backups\portfolio-$stamp.db"
```

Keep the backup access-controlled and outside source control. To restore, stop API/worker processes, preserve the current database as a separate backup, then copy the chosen backup to `portfolio.db` before starting the API.

The API allows browser requests only from `http://localhost:5173` and `http://127.0.0.1:5173`. Configure `VITE_API_BASE_URL` in the frontend terminal or a local, ignored `.env` file. Do not put backend secrets in Vite environment variables.

## Demonstration screens

Overview, alerts, market activity, reports, and settings still use illustrative local data, and are marked as mock/demo. Their prices, alerts, and settings are not persisted. Portfolio, target management, and workbook imports read/write the local API. Fixture quotes are stored with simulated status; live market data and production alert rules are not enabled.

Create a production build with `npm run build`.

The project uses Tailwind CSS for utility styling alongside purpose-built responsive styles, Lucide icons, Recharts, and Framer Motion.
