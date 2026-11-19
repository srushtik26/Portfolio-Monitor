#!/usr/bin/env bash
# Zero-downtime-oriented deployment script for Oracle Cloud Always Free VM
set -euo pipefail

APP_DIR="${APP_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
BRANCH="${DEPLOY_BRANCH:-main}"

cd "${APP_DIR}"

echo "==> Pulling latest code from origin/${BRANCH}..."
git fetch origin "${BRANCH}"
git checkout "${BRANCH}"
git pull --ff-only origin "${BRANCH}"

echo "==> Building updated Docker images..."
docker compose build --pull

echo "==> Starting database and running Alembic migrations..."
docker compose up -d db
docker compose run --rm api alembic upgrade head

echo "==> Starting all services (api, worker, frontend, caddy)..."
docker compose up -d --remove-orphans

echo "==> Verifying health endpoints..."
for attempt in $(seq 1 15); do
  if docker compose exec -T api python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health/ready', timeout=4)" >/dev/null 2>&1; then
    echo "API readiness check passed."
    break
  fi
  if [[ "${attempt}" -eq 15 ]]; then
    echo "ERROR: API readiness check failed after 15 attempts." >&2
    docker compose logs --tail=60 api worker
    exit 1
  fi
  sleep 2
done

docker compose ps
echo "==> Deployment complete."
