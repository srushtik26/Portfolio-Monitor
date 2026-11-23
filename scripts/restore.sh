#!/usr/bin/env bash
# Restore script for Northstar Portfolio Monitor (PostgreSQL + Private Uploads)
# Usage: ./scripts/restore.sh /var/backups/portfolio-monitor/20261119T203000Z
set -euo pipefail

if [[ $# -ne 1 ]]; then
  echo "Usage: $0 <backup_directory>" >&2
  exit 1
fi

BACKUP_DIR="$1"
APP_DIR="${APP_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"

if [[ ! -f "${BACKUP_DIR}/database.sql.gz" ]]; then
  echo "ERROR: ${BACKUP_DIR}/database.sql.gz not found." >&2
  exit 1
fi

cd "${APP_DIR}"
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

if [[ -f "${BACKUP_DIR}/SHA256SUMS" ]]; then
  echo "Verifying backup checksums..."
  (cd "${BACKUP_DIR}" && sha256sum -c SHA256SUMS)
fi

echo "Stopping api and worker before restore..."
docker compose stop api worker

echo "Restoring PostgreSQL database (${PG_DB})..."
gunzip -c "${BACKUP_DIR}/database.sql.gz" | docker compose exec -T db psql -v ON_ERROR_STOP=1 -U "${PG_USER}" -d "${PG_DB}"

echo "Running Alembic migrations before starting services..."
docker compose run --rm api alembic upgrade head

if [[ -f "${BACKUP_DIR}/private_uploads.tar.gz" ]]; then
  echo "Restoring private uploaded Excel files..."
  docker compose run --rm -T api sh -c 'rm -rf /app/var/private_uploads/* && tar -xzf - -C /app/var' < "${BACKUP_DIR}/private_uploads.tar.gz"
fi

echo "Starting application services..."
docker compose up -d

echo "Verifying API readiness..."
sleep 4
docker compose exec -T api python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8000/api/health/ready', timeout=5).read().decode())"
echo "Restore completed successfully."
