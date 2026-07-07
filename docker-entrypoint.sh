#!/bin/sh
set -e

# Shared image for api/worker — docker-compose.yml picks the mode via the
# `command` override on each service, defaulting to `api` if run bare.
MODE="${1:-api}"

case "$MODE" in
  api)
    # Migrations run here now (the standalone `migrate` service was dropped).
    # api is the single writer of schema; worker only reads/uses the DB.
    alembic upgrade head
    python -m app.sources_seed
    exec uvicorn app.api.main:app --host 0.0.0.0 --port 8000
    ;;
  worker)
    exec arq app.worker.settings.WorkerSettings
    ;;
  *)
    echo "Unknown mode: $MODE (expected api|worker)" >&2
    exit 1
    ;;
esac
