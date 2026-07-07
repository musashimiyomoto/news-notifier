# Single image, used for both the api and worker services (see docker-compose.yml)
# — they share the exact same dependency set, only the entrypoint command differs.
FROM python:3.12-slim

WORKDIR /srv

# System deps for asyncpg build + Playwright's Chromium runtime deps.
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    && rm -rf /var/lib/apt/lists/*

# --- Dependency layer -------------------------------------------------------
# Copy ONLY the dependency manifest, then install deps + Chromium. This layer is
# cached and only rebuilds when pyproject.toml changes — editing anything in app/
# no longer re-downloads Chromium (~150MB) or reinstalls the whole dep set.
#
# The install is editable (pip install -e .), which maps the /srv/app *directory*
# rather than snapshotting files, so the real code copied in below is picked up at
# runtime. A stub app/__init__.py is enough for setuptools' package discovery here.
COPY pyproject.toml ./
RUN --mount=type=cache,target=/root/.cache/pip \
    mkdir -p app && touch app/__init__.py \
    && pip install -e . \
    && playwright install --with-deps chromium

# --- Source layer -----------------------------------------------------------
# Copied after the heavy install, so code changes invalidate only from here down.
COPY app ./app
COPY migrations ./migrations
COPY alembic.ini ./

COPY docker-entrypoint.sh ./
RUN chmod +x docker-entrypoint.sh

ENTRYPOINT ["./docker-entrypoint.sh"]
