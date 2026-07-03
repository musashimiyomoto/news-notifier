# Single image, used for both the api and worker services (see docker-compose.yml)
# — they share the exact same dependency set, only the entrypoint command differs.
FROM python:3.12-slim

WORKDIR /srv

# System deps for asyncpg build + Playwright's Chromium runtime deps.
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml ./
COPY app ./app
COPY migrations ./migrations
COPY alembic.ini ./

RUN pip install --no-cache-dir -e . \
    && playwright install --with-deps chromium

COPY docker-entrypoint.sh ./
RUN chmod +x docker-entrypoint.sh

ENTRYPOINT ["./docker-entrypoint.sh"]
