#!/bin/sh
set -e

# Shared image for api/worker/migrate — docker-compose.yml picks the mode via
# the `command` override on each service, defaulting to `api` if run bare.
MODE="${1:-api}"

case "$MODE" in
  api)
    exec uvicorn app.api.main:app --host 0.0.0.0 --port 8000
    ;;
  worker)
    exec arq app.worker.settings.WorkerSettings
    ;;
  migrate)
    alembic upgrade head
    exec python -m app.sources_seed
    ;;
  *)
    echo "Unknown mode: $MODE (expected api|worker|migrate)" >&2
    exit 1
    ;;
esac
