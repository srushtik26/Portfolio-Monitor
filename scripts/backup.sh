#!/usr/bin/env bash
# Daily backup script for Northstar Portfolio Monitor (PostgreSQL + Private Excel Uploads)
# Recommended cron (02:00 IST / 20:30 UTC daily):
#   30 20 * * * /opt/portfolio-monitor/scripts/backup.sh >> /var/log/portfolio-backup.log 2>&1
set -euo pipefail

APP_DIR="${APP_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
BACKUP_ROOT="${BACKUP_ROOT:-/var/backups/portfolio-monitor}"
RETENTION_DAYS="${RETENTION_DAYS:-14}"
TIMESTAMP="$(date -u +%Y%m%dT%H%M%SZ)"
DEST_DIR="${BACKUP_ROOT}/${TIMESTAMP}"

mkdir -p "${DEST_DIR}"
cd "${APP_DIR}"

if [[ ! -f ".env" ]]; then
  echo "ERROR: ${APP_DIR}/.env not found." >&2
  exit 1
fi

# Read POSTGRES_USER / POSTGRES_DB literally without executing .env as shell code
read_env_var() {
  local key="$1"
  local default_val="$2"
  local val
  val="$(grep -E "^${key}=" .env | tail -n 1 | cut -d'=' -f2- | tr -d '\r')"
  val="${val#\"}"
  val="${val%\"}"
  val="${val#\'}"
  val="${val%\'}"
  printf '%s' "${val:-$default_val}"
}

PG_USER="$(read_env_var POSTGRES_USER portfolio_app)"
PG_DB="$(read_env_var POSTGRES_DB portfolio_monitor)"

echo "[${TIMESTAMP}] Starting PostgreSQL backup (${PG_DB})..."
docker compose exec -T db pg_dump -U "${PG_USER}" -d "${PG_DB}" --clean --if-exists \
  | gzip -9 > "${DEST_DIR}/database.sql.gz"

echo "[${TIMESTAMP}] Validating gzip integrity..."
gzip -t "${DEST_DIR}/database.sql.gz"

echo "[${TIMESTAMP}] Archiving private uploaded Excel workbooks..."
docker compose exec -T api tar -czf - -C /app/var private_uploads > "${DEST_DIR}/private_uploads.tar.gz"
gzip -t "${DEST_DIR}/private_uploads.tar.gz"

(cd "${DEST_DIR}" && sha256sum "database.sql.gz" "private_uploads.tar.gz" > "SHA256SUMS")

# Rotate local backups older than RETENTION_DAYS
find "${BACKUP_ROOT}" -mindepth 1 -maxdepth 1 -type d -mtime +"${RETENTION_DAYS}" -exec rm -rf {} +

echo "[${TIMESTAMP}] Backup complete: ${DEST_DIR}"
echo "NOTE: Local VM backups alone are NOT sufficient for disaster recovery. Configure rclone/rsync to copy ${BACKUP_ROOT} to offsite storage (e.g., OCI Object Storage Always Free bucket or Google Drive)."
