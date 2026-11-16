# Northstar Portfolio Monitor — Production Price-Monitoring & Target-Alert System

A production-ready React + TypeScript and FastAPI **price-monitoring and target-alert system** for Indian NSE & BSE equities.

> **IMPORTANT**: This is strictly a **price-monitoring application**, NOT a trading application. It never executes buy or sell orders.

---

## Key Capabilities

1. **Configurable Target Alert Engine (`backend/app/prices.py`)**:
   - Tracks both **Base Target Price** and **Bull Target Price** per `(exchange, symbol)` identity.
   - Configurable comparison direction per target: `below` (`price <= target`), `above` (`price >= target`), or `pending` (awaits explicit configuration rather than silently assuming a direction).
   - Configurable trigger conditions: `at_or_beyond` (triggers immediately if already beyond target when monitoring starts) or `crosses` (requires crossing from the opposite side, with automatic cross-baseline reset on feed gaps/reconnects).
   - Configurable re-trigger policies: `once_per_revision`, `rearm_on_reset`, `rearm_after_recovery`, and `cooldown` (with configurable cooldown minutes).
   - Saved CMP from Excel uploads is stored strictly as historical reference data and is **never** treated as a live market price.

2. **Version-Controlled Excel Upload & Approval Workflow (`backend/app/imports.py`, `src/UploadPage.tsx`)**:
   - **Workflow**: `Upload → Parse → Preview → Validate → Approve → Save → Start Monitoring`.
   - Extracts original Excel cell coordinates (`cell_refs`, e.g., `D2`, `E2`, `F2`) for every extracted value.
   - Supports inline preview editing of extracted values and target comparison directions, plus per-row Approve/Reject toggles.
   - Every approved upload creates a new numbered version (`version_number`) with a complete before/after target snapshot (`changes_snapshot`) and allows one-click rollback/restore of any earlier approved version.

3. **Independent Background Monitoring Service (`backend/app/worker.py`)**:
   - Runs independently of browser tabs using a single-instance file lock (`single_worker_lock`).
   - Supports NSE and BSE stocks with an authoritative Indian market calendar (`NseBseExchangeCalendar`, 09:15–15:30 IST with NSE/BSE holidays and Muhurat sessions), honest source-only tick timestamps (marking ticks without a valid source timestamp as `freshness_unknown`), bounded tick queues with overflow counters, and explicit reconnect gap tracking.

4. **Reliable Multi-Channel Notification System (`backend/app/notifications.py`)**:
   - Controlled by a single environment variable: `NOTIFICATION_MODE` (`dry_run` by default, or `live`).
   - Even if live channels are enabled in the Settings UI, the worker requires **BOTH** `NOTIFICATION_MODE=live` **AND** complete SMTP configuration (`SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `SMTP_FROM`) before attempting any real delivery; otherwise it stays in `dry_run` mode and logs the reason.

---

## Shoonya prerequisites before enabling

The Shoonya (`backend/app/shoonya_feed.py`) adapter is **disabled by default** and marked **UNVERIFIED** in code and configuration. Starting the Shoonya provider without explicit opt-in (`SHOONYA_ALLOW_UNVERIFIED_AUTH=true`) fails fast with:

```text
Shoonya auth flow unverified against current official docs
```

Before enabling Shoonya in any live environment, verify and complete the following prerequisites:

1. **Supported Login Flow (OAuth vs Legacy SDK Login)**:
   - Current official Shoonya API documentation specifies an **OAuth-based authentication flow**, while also containing conflicting legacy TOTP guidance.
   - The legacy `NorenRestApiPy` direct login (`userid`, `password`, `twoFA` TOTP, `vendor_code`, `api_secret`, `imei`) implemented in `backend/app/shoonya_feed.py` is **UNVERIFIED** against the current official specification and must be confirmed with Finvasia support or updated to the verified OAuth token exchange before live use.
2. **Exact WebSocket & REST Endpoints**:
   - Current official documentation references `wss://api.shoonya.com/NorenWSAPI/` for WebSocket connections, whereas legacy `NorenRestApiPy` defaults use `https://api.shoonya.com/NorenWClientTP/` (REST) and `wss://api.shoonya.com/NorenWSTP/` (WebSocket).
   - Confirm the exact production endpoints for your account tier and set `SHOONYA_HOST` and `SHOONYA_WS_ENDPOINT` accordingly.
3. **Token & Field Mapping Verification**:
   - All static scrip tokens in `DEFAULT_SHOONYA_TOKENS` and payload field mappings (`e`, `tk`, `lp`, `ft`, sequence fields) are marked **UNVERIFIED**.
   - Replace static token mappings with values dynamically verified from the official Shoonya scrip master / authenticated login response before enabling live monitoring.
4. **Static-IP Requirement**:
   - Finvasia / NSE / BSE API regulations require registering a **whitelisted static public IP address** against your API key/vendor code before authentication and market-data subscriptions will succeed.
5. **Credential Storage**:
   - Store all broker credentials and secrets exclusively in runtime environment variables or an encrypted secret manager outside the repository (e.g., `.env` excluded via `.gitignore`, OS secret store, or vault). Never commit credentials, tokens, or TOTP secrets to source control.

---

## Running Locally & in Production

### 1. Backend API Server & First Admin Bootstrap

```powershell
cd "Portfolio-Monitor/backend"
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m alembic upgrade head
# One-time setup: create the initial Administrator account (interactive hidden prompt or via BOOTSTRAP_ADMIN_NAME / BOOTSTRAP_ADMIN_PASSWORD)
.\.venv\Scripts\python.exe -m app.bootstrap_admin
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

### 2. Team Authentication & Role-Based Access Control (`ADMIN` vs `EMPLOYEE`)

- **Authentication**: Name + Password login (`POST /api/auth/login`) using normalized case-insensitive name matching, Argon2id password hashing, `HttpOnly` session cookies (`portfolio_session`), double-submit CSRF tokens (`X-CSRF-Token`), and per-client login rate limiting (5 attempts / 15-minute window).
- **Administrator (`ADMIN`)**: Can manage employee accounts (`Admin Users`), reset employee passwords (one-time credential display), activate/deactivate accounts, approve or reject employee Excel uploads, restore earlier portfolio versions, configure target rules, and inspect the full `Activity` audit log.
- **Employee (`EMPLOYEE`)**: Can sign in with their name and password, view the shared portfolio and live market prices/alerts, upload and preview Excel files, submit uploads for administrator approval (`pending_approval`), track their own upload approval status, and change their own password.

### 2. Independent Background Price Monitoring Worker

Run in a separate always-on process (defaults to `NOTIFICATION_MODE=dry_run`):

```powershell
cd "Portfolio-Monitor/backend"
$env:ALERT_RULES_ENABLED = "true"
$env:NOTIFICATION_MODE = "dry_run"
.\.venv\Scripts\python.exe -m app.worker --provider fixture --calendar fixture --interval-seconds 60
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
npx tsc --noEmit
npm run build
```

### 4. Running Automated Tests

All tests run in simulated/fixture dry-run mode without network calls:

```powershell
cd "Portfolio-Monitor/backend"
python -m pytest tests -q
```
