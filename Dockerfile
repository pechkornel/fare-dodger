# syntax=docker/dockerfile:1
# Fare Dodger live server. Build: docker compose build. Run: docker compose up -d.

# --- build: resolve the locked dependencies and install the project into /app/.venv ---------
FROM python:3.12-slim-bookworm AS build
COPY --from=ghcr.io/astral-sh/uv:0.12.17 /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /src
# Dependencies first, so editing the code does not reinstall them.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-install-project
COPY README.md LICENSE ./
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable

# --- runtime: the virtualenv, the configs and an unprivileged user -------------------------
FROM python:3.12-slim-bookworm AS runtime
RUN useradd --create-home --uid 10001 --user-group game \
    && mkdir -p /app/data && chown game:game /app/data
WORKDIR /app
COPY --from=build /app/.venv /app/.venv
COPY configs ./configs
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    FARE_DODGER_DATA_DIR=/app/data
USER game
EXPOSE 8001
HEALTHCHECK --interval=30s --timeout=5s --start-period=10m --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8001/healthz', timeout=4)"
# Refresh the GTFS feed (conditional request; a cached feed is used if BKK is unreachable),
# then serve. The first start downloads the feed and OpenStreetMap layers and builds the
# networks into /app/data, so it takes a few minutes; later starts take seconds.
CMD ["sh", "-c", "fare-dodger download || echo 'feed download failed: using the cached feed'; exec fare-dodger play -c configs/live.toml --host 0.0.0.0 --port 8001"]
