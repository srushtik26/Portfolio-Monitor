# Northstar Portfolio Monitor

A responsive React + TypeScript portfolio monitoring dashboard. Portfolio identity and base/bull targets are stored in a local FastAPI backend. A separate single-instance worker can persist explicitly simulated fixture quotes for development. An optional one-off Yahoo Finance adapter can fetch and persist a mapped demo candle, but it is unofficial and is not configured as a continuous feed. No authoritative exchange calendar is selected, so live alerts remain disabled.

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

The web application does not poll prices. Run the separate worker in its own PowerShell terminal after database migrations. The worker's only usable adapter is a clearly labeled fixture; it is simulated, only defines `NSE:FIXTURE`, and cannot trigger live alerts:

```powershell
cd "C:\path\to\Portfolio-Monitor\backend"
.\.venv\Scripts\python.exe -m app.worker --provider fixture --calendar fixture --once
```

For repeated fixture polling, omit `--once` and optionally set `--interval-seconds 60`. The worker acquires a process lock so another worker cannot run concurrently. Rate limits are handled using the provider's retry-after delay. Its fixture calendar uses Asia/Kolkata time, regular weekday session hours, and test-only holiday/special-session examples; it is not an authoritative NSE/BSE calendar. Live mode stays `not_configured` until an authoritative exchange calendar and live-alert policy are configured. The Yahoo Finance adapter cannot be used with the fixture worker calendar.

Quotes record exchange/symbol, price, currency, provider, quote type, provider timestamp, receive time, simulated provenance, freshness threshold, and freshness/order status. `GET /api/companies/{exchange}/{symbol}/history` returns recent quote history; `GET /api/price-feed/status` reports the unconfigured live mode. Portfolio's CMP is the latest provider price or unavailable; Excel CMP is displayed separately as Imported CMP. A previously stored provider price remains visible with its timestamp and is marked stale when it exceeds its freshness threshold.

### Optional Yahoo Finance personal-demo fetch

The adapter uses the unofficial `yfinance` library, requires no API key, and does not guarantee real-time availability or timing. Its only verified mapping is `NSE:TEGA` → `TEGA.NS`. Other NSE symbols and all BSE symbols are rejected unless individually verified and explicitly mapped; ticker suffixes are never guessed. It reads Yahoo's `1m` bar `Close`, stores Yahoo's timezone-aware bar timestamp and currency metadata, and identifies the value in the UI as a **1-minute candle close**, not a last-traded quote.

Install the requirements and apply migrations before using the optional smoke command. Import/track `NSE:TEGA` first; the command will not create a company or targets:

```powershell
cd "C:\path\to\Portfolio-Monitor\backend"
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m alembic upgrade head
.\.venv\Scripts\python.exe -m app.yfinance_smoke
```

The command fetches only the explicit TEGA mapping and persists the returned quote if Yahoo supplies a valid price, timezone-aware bar timestamp, and currency. It has no fallback quote and records no alert or notification. A missing company, empty response, missing timestamp/currency, invalid/future price data, or Yahoo/network/rate-limit error is reported as failure. In a long-lived adapter, successful responses are cached for 60 seconds; each standalone smoke invocation is a fresh process. Cache and retries are bounded (`YFINANCE_CACHE_TTL_SECONDS`, default 60; three attempts with 2/4-second backoff, capped at 30 seconds). Set `YFINANCE_STALE_AFTER_SECONDS` to adjust the conservative freshness threshold (default 300 seconds; allowed range 60–86400). Since no authoritative calendar is configured, age is not relaxed outside exchange hours or holidays; old candles become stale rather than appearing current.

The Portfolio labels the Yahoo source and actual bar timestamp and displays stale retained prices as stale. Yahoo can be delayed, rate limited, unavailable, or corrected; this is demo price display only. The optional fetch command does not change `ALERT_RULES_ENABLED`, activate rules, send notifications, or place orders. The continuous worker refuses Yahoo polling until an authoritative exchange calendar is selected. This laptop-based worker also stops while the laptop is asleep/offline; Vercel/serverless hosting is not suitable for this continuous process.

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
