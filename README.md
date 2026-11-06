# Northstar Portfolio Monitor — Production Price-Monitoring & Target-Alert System

A production-ready React + TypeScript and FastAPI **price-monitoring and target-alert system** for Indian NSE & BSE equities.

> **IMPORTANT**: This is strictly a **price-monitoring application**, NOT a trading application. It never executes buy or sell orders.

---

## Key Capabilities

1. **Configurable Target Alert Engine (`backend/app/prices.py`)**:
   - Tracks both **Base Target Price** and **Bull Target Price** per `(exchange, symbol)` identity.
   - Configurable comparison direction per target: `below` (`price <= target`), `above` (`price >= target`), or `pending` (awaits explicit configuration rather than silently assuming a direction).
   - Configurable trigger conditions: `at_or_beyond` (triggers immediately if already beyond target when monitoring starts) or `crosses` (requires crossing from the opposite side).
   - Configurable re-trigger policies: `once_per_revision`, `rearm_on_reset`, `rearm_after_recovery`, and `cooldown` (with configurable cooldown minutes).
   - Saved CMP from Excel uploads is stored strictly as historical reference data and is **never** treated as a live market price.

2. **Version-Controlled Excel Upload & Approval Workflow (`backend/app/imports.py`, `src/UploadPage.tsx`)**:
   - **Workflow**: `Upload → Parse → Preview → Validate → Approve → Save → Start Monitoring`.
   - Extracts original Excel cell coordinates (`cell_refs`, e.g., `D2`, `E2`, `F2`) for every extracted value.
   - Supports inline preview editing of extracted values and target comparison directions, plus per-row Approve/Reject toggles.
   - Every approved upload creates a new numbered version (`version_number`) with a complete before/after target snapshot (`changes_snapshot`) and allows one-click rollback/restore of any earlier approved version.

3. **Independent Background Monitoring Service (`backend/app/worker.py`)**:
   - Runs independently of browser tabs using a single-instance file lock (`single_worker_lock`).
   - Supports NSE and BSE stocks with an authoritative Indian market calendar (`NseBseExchangeCalendar`, 09:15–15:30 IST with NSE/BSE holidays and Muhurat sessions) and automatic Yahoo Finance polling with retry/backoff and stale-price detection.

4. **Reliable Multi-Channel Notification System (`backend/app/notifications.py`)**:
   - Transactional notification outbox (`NotificationOutbox`) with bounded exponential backoff retries (15s to 15m), claim leases, and idempotency keys.
   - Supports **Dashboard notifications** and **Email notifications** (`SmtpEmailNotifier` via SMTP TLS or local `.eml` spool directory `SMTP_SPOOL_DIR`) with configurable recipient lists (`GET/PUT /api/notifications/preferences`).

---

## Running Locally & in Production

### 1. Backend API Server

```powershell
cd "Portfolio-Monitor/backend"
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m alembic upgrade head
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

### 2. Independent Background Price Monitoring Worker

Run in a separate always-on process (e.g., systemd service, Windows Service / NSSM, Docker container, or supervisor):

```powershell
cd "Portfolio-Monitor/backend"
$env:ALERT_RULES_ENABLED = "true"
$env:NOTIFICATION_DELIVERY_MODE = "live"
.\.venv\Scripts\python.exe -m app.worker --provider yfinance --calendar nse_bse --interval-seconds 60
```

### 3. Frontend Dashboard

```powershell
cd "Portfolio-Monitor"
$env:VITE_API_BASE_URL = "http://127.0.0.1:8000"
npm install
npm run dev -- --host 127.0.0.1
```

Build the production frontend bundle with:

```powershell
npm run build
```

### 4. Running Automated Tests

```powershell
cd "Portfolio-Monitor/backend"
python -m pytest tests -q
```
