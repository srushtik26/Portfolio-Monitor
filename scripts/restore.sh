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
set -a
# shellcheck disable=SC1091
source .env
set +a

PG_USER="${POSTGRES_USER:-portfolio_app}"
PG_DB="${POSTGRES_DB:-portfolio_monitor}"

if [[ -f "${BACKUP_DIR}/SHA256SUMS" ]]; then
  echo "Verifying backup checksums..."
  (cd "${BACKUP_DIR}" && sha256sum -c SHA256SUMS)
fi

echo "Stopping api and worker before restore..."
docker compose stop api worker

echo "Restoring PostgreSQL database (${PG_DB})..."
gunzip -c "${BACKUP_DIR}/database.sql.gz" | docker compose exec -T db psql -U "${PG_USER}" -d "${PG_DB}"

if [[ -f "${BACKUP_DIR}/private_uploads.tar.gz" ]]; then
  echo "Restoring private uploaded Excel files..."
  docker compose up -d api
  docker compose exec -T api tar -xzf - -C /app/var < "${BACKUP_DIR}/private_uploads.tar.gz"
fi

echo "Running Alembic migrations and restarting services..."
docker compose run --rm api alembic upgrade head
docker compose up -d

echo "Verifying API readiness..."
sleep 4
docker compose exec -T api python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8000/api/health/ready', timeout=5).read().decode())"
echo "Restore completed successfully."
