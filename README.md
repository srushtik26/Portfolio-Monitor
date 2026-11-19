# Northstar Portfolio Monitor — Institutional Price-Monitoring & Target-Alert Terminal

A production-ready **React + TypeScript + FastAPI + PostgreSQL** price-monitoring and target-alert system for Indian **NSE & BSE** equities, designed for secure, **₹0 free-tier deployment** on an Oracle Cloud Always Free VM with Docker Compose and Caddy automatic HTTPS.

> **IMPORTANT**: This is strictly a **price-monitoring & target-alert application**, NOT a trading application. It **never** executes buy or sell orders.

---

## 1. Key Features

- **Institutional Dark Navy Monitoring Terminal**: Compact, high-contrast dark terminal (`#0B1120` / `#111827` / `#172033`) with responsive desktop tables and mobile company cards.
- **Backend-Enforced Target Alert Engine (`backend/app/prices.py`)**:
  - Tracks **Base Target** (`below`/`above`/`pending`) and **Bull Target** (`above`/`below`/`pending`) per `(exchange, symbol)` identity.
  - Supports `at_or_beyond` and `crosses` trigger modes, plus `once_per_revision`, `rearm_on_reset`, `rearm_after_recovery`, and `cooldown` re-trigger policies.
  - Saved Excel `CMP` is strictly historical reference data and is **never** treated as a live market price.
  - All target-trigger decisions run strictly on the backend.
- **Full-Day Market Session & Closing Price Chart**:
  - Plots the full NSE/BSE trading session (`09:15 AM – 03:30 PM IST`), **Previous Day Close** reference line, intraday OHLC summary, and multi-day **Daily Closing Price** history.
- **Version-Controlled Excel Import & Rollback (`backend/app/imports.py`, `src/UploadPage.tsx`)**:
  - Drag-and-drop `.xlsx` / `.csv` upload, automatic header/column detection, row-level validation, Excel cell-coordinate lineage (`D2`, `E2`, `F2`), inline preview editing, versioned snapshots, and one-click rollback.
- **Secure Team Authentication & RBAC (`backend/app/security.py`, `backend/app/auth_routes.py`)**:
  - Name + Password login with normalized case-insensitive matching, Argon2id hashing, `HttpOnly` session cookies, double-submit CSRF tokens (`X-CSRF-Token`), login rate limiting (5 attempts / 15 min), and activity audit logs.
  - **Admin-Only**: `Admin Users` (user creation, password reset, activation/deactivation), `Activity` / audit logs, and `Clear All Data`.
  - **Employees**: Full access to shared portfolio, Excel upload & direct confirmation, target configuration, alerts, settings, and live polling.
- **Dedicated System Health Telemetry**:
  - Secret-free `/api/health`, `/api/health/ready`, and `/api/health/worker` endpoints distinguishing `healthy`, `degraded`, `disconnected`, `stale`, and `unavailable` states.
- **Multi-Channel Notification Outbox (`backend/app/notifications.py`)**:
  - Safe `NOTIFICATION_MODE=dry_run` default. Live SMTP email delivery requires **both** `NOTIFICATION_MODE=live` and complete SMTP settings. Active user email IDs and Settings recipients automatically receive HTML + plain-text alert emails with exponential retry backoff.

---

## 2. Production Architecture (₹0 Oracle Cloud Always Free VM)

```text
                       Internet (Ports 80 / 443 ONLY)
                                     │
                             ┌───────▼───────┐
                             │  Caddy Proxy  │  (Automatic Let's Encrypt HTTPS)
                             └───────┬───────┘
                   ┌─────────────────┴─────────────────┐
                   │ / (SPA)                           │ /api/* & /healthz
           ┌───────▼────────┐                 ┌────────▼────────┐
           │    frontend    │                 │   FastAPI api   │
           │ (nginx :8080)  │                 │ (uvicorn :8000) │
           └────────────────┘                 └────────┬────────┘
                                                       │ (Private Docker Network)
                        ┌──────────────────────────────┼──────────────────────────────┐
                        │                              │                              │
                ┌───────▼────────┐            ┌────────▼────────┐            ┌────────▼────────┐
                │   PostgreSQL   │◄───────────┤  Price Worker   │            │ Private Volumes │
                │  (db :5432)    │            │ (independent)   │            │ (uploads/pgdata)│
                └────────────────┘            └─────────────────┘            └─────────────────┘
```

- **Database (`db`)**, **API (`api`)**, **Worker (`worker`)**, and **Frontend (`frontend`)** are never publicly exposed; only **Caddy** exposes ports `80` and `443`.
- The monitoring **worker** runs independently of browser sessions and automatically restarts after crashes or VM reboots (`restart: unless-stopped`).

---

## 3. Local Development Setup

### Prerequisites
- Python **3.12+** (3.13 recommended)
- Node.js **20+** (22 recommended)

### Backend Setup
```powershell
cd backend
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m alembic upgrade head
# One-time initial Administrator bootstrap (interactive hidden prompt)
.\.venv\Scripts\python.exe -m app.bootstrap_admin
# Start API server
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

### Independent Background Worker (Local)
```powershell
cd backend
$env:ALERT_RULES_ENABLED = "true"
$env:NOTIFICATION_MODE = "dry_run"
.\.venv\Scripts\python.exe -m app.worker --provider yfinance --calendar nse_bse --interval-seconds 60
```

### Frontend Setup
```powershell
npm ci
$env:VITE_API_BASE_URL = "http://127.0.0.1:8000"
npm run dev -- --host 127.0.0.1 --port 5173
```

### Automated Tests & Checks
```powershell
# Backend tests (all simulated / dry-run, no live network calls)
cd backend
python -m pytest -q

# Frontend typecheck & production build
cd ..
npx tsc --noEmit
npm run build
```

---

## 4. Environment Variables

See [`.env.example`](.env.example) (local) and [`.env.production.example`](.env.production.example) (production). Never commit `.env` files.

| Variable | Default | Purpose |
| --- | --- | --- |
| `DOMAIN` | `localhost` | Public domain for Caddy automatic HTTPS (`monitor.example.com`) |
| `APP_ENV` | `development` | Set `production` in Docker/Oracle VM (enables `Secure` cookies) |
| `DATABASE_URL` | `sqlite:///./portfolio.db` | PostgreSQL URL in production (`postgresql+psycopg://...`) |
| `CORS_ORIGINS` | `http://localhost:5173,...` | Comma-separated allowed browser origins |
| `AUTH_SESSION_HOURS` | `12` | Session cookie expiry in hours |
| `AUTH_COOKIE_SECURE` | `false` (`true` in prod) | Require HTTPS for session/CSRF cookies |
| `BOOTSTRAP_ADMIN_NAME` | *(unset)* | Optional one-time admin bootstrap name |
| `BOOTSTRAP_ADMIN_PASSWORD` | *(unset)* | Optional one-time admin password (12+ chars, 3+ classes) |
| `PRICE_PROVIDER` | `fixture` | `yfinance`, `fixture`, or `shoonya_ws` (gated/unverified) |
| `EXCHANGE_CALENDAR` | `fixture` | `nse_bse` (authoritative IST calendar) or `fixture` |
| `ALERT_RULES_ENABLED` | `false` | Set `true` to evaluate target triggers |
| `NOTIFICATION_MODE` | `dry_run` | `dry_run` (safe default) or `live` (requires complete SMTP) |
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `SMTP_FROM`, `SMTP_USE_TLS` | *(unset)* | SMTP credentials for live email alerts (never exposed to frontend) |
| `SHOONYA_ALLOW_UNVERIFIED_AUTH` | `false` | Must remain `false` until Shoonya OAuth/WS endpoints are verified |

---

## 5. Market-Data Providers & Limitations

- **Yahoo Finance (`yfinance`, ₹0 Free)**:
  - Unofficial polling provider (`5m`/`1m` delayed candles). Never labeled as real-time WebSocket streaming; stale thresholds and candle timestamp validation are strictly enforced.
  - Suitable for ₹0 monitoring where ~1-minute polling delay is acceptable.
- **Shoonya / Finvasia (`shoonya_ws`) — UNVERIFIED & DISABLED BY DEFAULT**:
  - Current official Shoonya docs specify OAuth login and `wss://api.shoonya.com/NorenWSAPI/` alongside conflicting legacy TOTP guidance.
  - Starting Shoonya without `SHOONYA_ALLOW_UNVERIFIED_AUTH=true` fails fast (`Shoonya auth flow unverified against current official docs`).
  - Requires a registered static public IP and verified OAuth flow + scrip master tokens before live use.
- **Fixture Mode (`fixture`)**: Deterministic offline testing mode; never mixes simulated quotes with live alerts.

---

## 6. Admin Bootstrap & User Management

1. **First Administrator Account**:
   - **Browser One-Time Setup**: When `users` has `0` admins, visiting the app opens a one-time **Create Administrator Account** form (permanently disabled once created).
   - **CLI Bootstrap**: Run `docker compose exec api python -m app.bootstrap_admin` (hidden password prompt).
   - **Environment Bootstrap**: Set `BOOTSTRAP_ADMIN_NAME` and `BOOTSTRAP_ADMIN_PASSWORD` for first boot, then remove them from `.env`.
2. **Adding Team Members**:
   - Sign in as Administrator → **Admin Users** → enter **Employee Name**, **Email ID (Alert Notifications)**, and **Initial Password** (12+ chars, 3+ character classes).
   - The employee's email is automatically synced to alert notification recipients.

---

## 7. Oracle Cloud Always Free VM + Docker Deployment (₹0/month)

### Step 1: Provision Oracle Cloud Always Free VM
1. Create an **Ubuntu 22.04 / 24.04** instance using the **VM.Standard.A1.Flex** (ARM, up to 4 OCPU / 24 GB RAM Always Free) or **VM.Standard.E2.1.Micro** shape.
2. In **Oracle VCN Security List**, allow inbound TCP ports **22** (SSH), **80** (HTTP), and **443** (HTTPS) ONLY. Do **NOT** open 5432, 8000, or 5173.
3. Point an **A record** in your DNS provider (e.g., Cloudflare DNS, free) from `monitor.yourdomain.com` to the VM's public IP.

### Step 2: Install Docker & Firewall on Ubuntu VM
```bash
sudo apt-get update && sudo apt-get install -y ca-certificates curl git ufw
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker $USER
newgrp docker

# Configure Ubuntu firewall (allow only SSH, HTTP, HTTPS)
sudo ufw allow OpenSSH
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
sudo ufw --force enable
```

### Step 3: Clone, Configure `.env`, Migrate & Launch
```bash
sudo mkdir -p /opt/portfolio-monitor && sudo chown $USER:$USER /opt/portfolio-monitor
git clone https://github.com/srushtik26/Portfolio-Monitor.git /opt/portfolio-monitor
cd /opt/portfolio-monitor

cp .env.production.example .env
nano .env   # Set DOMAIN, POSTGRES_PASSWORD, CORS_ORIGINS, SMTP_* (if live mail desired)

chmod +x scripts/deploy.sh scripts/backup.sh scripts/restore.sh
./scripts/deploy.sh

# Create your initial Administrator account
docker compose exec api python -m app.bootstrap_admin
```

Caddy automatically obtains and renews a free Let's Encrypt TLS certificate for `https://$DOMAIN`.

---

## 8. Automatic GitHub Actions Deployment

Workflow file: [`.github/workflows/deploy.yml`](.github/workflows/deploy.yml)

On every push to `main`:
1. Runs `npm ci`, `npx tsc --noEmit`, and `npm run build`.
2. Runs `pytest -q` and validates `alembic upgrade head` on a fresh database.
3. Deploys over SSH **only if all checks pass**.

### Required GitHub Repository Secrets
Add these under **GitHub Repo → Settings → Secrets and variables → Actions**:
- `VM_HOST`: Public IP or domain of your Oracle Cloud VM
- `VM_USERNAME`: SSH username (e.g., `ubuntu`)
- `VM_SSH_PRIVATE_KEY`: Private SSH key authorized in `~/.ssh/authorized_keys` on the VM
- `VM_APP_DIR` *(optional)*: Defaults to `/opt/portfolio-monitor`
- `VM_SSH_PORT` *(optional)*: Defaults to `22`

### Rollback Procedure
```bash
cd /opt/portfolio-monitor
git log --oneline -n 5
git checkout <previous-good-commit-sha>
docker compose build && docker compose up -d
```

---

## 9. Automated Backup & Restore

### Daily Backup (`scripts/backup.sh`)
Backs up PostgreSQL (`pg_dump | gzip`) and `/app/var/private_uploads`, validates gzip integrity and SHA-256 checksums, and rotates backups older than 14 days:
```bash
# Test manually:
/opt/portfolio-monitor/scripts/backup.sh

# Schedule daily at 02:00 IST (20:30 UTC) via crontab -e:
30 20 * * * /opt/portfolio-monitor/scripts/backup.sh >> /var/log/portfolio-backup.log 2>&1
```
> **Disaster Recovery Warning**: Local VM backups alone do **not** protect against VM loss or disk corruption. Configure `rclone sync /var/backups/portfolio-monitor <remote>:portfolio-backups` to copy backups to an Oracle Cloud Object Storage Always Free bucket or encrypted offsite storage.

### Restore (`scripts/restore.sh`)
```bash
/opt/portfolio-monitor/scripts/restore.sh /var/backups/portfolio-monitor/<TIMESTAMP>
```

---

## 10. Troubleshooting & Security Notes

- **Check service status**: `docker compose ps`
- **Inspect logs**: `docker compose logs -f api worker caddy`
- **Verify health**: `curl -s https://monitor.yourdomain.com/api/health/ready` and `/api/health/worker`
- **Security Notes**:
  - Passwords are hashed with **Argon2id**; plaintext passwords and session tokens are never stored or returned in API responses.
  - Uploaded Excel files are stored in a private directory (`0700`/`0600`), never publicly served.
  - All state-changing API calls require a valid `HttpOnly` session cookie **and** matching `X-CSRF-Token` header.
  - Public health endpoints expose operational status only — never credentials, DSNs, or stack traces.
