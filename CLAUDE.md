# Fare Dodger

A hidden-movement chase game on the real Budapest (BKK GTFS) public transport timetable. Phase 1 is a headless engine (self-written RAPTOR router, rules, bots, simulation); phase 2 is a real-time online browser game served by FastAPI.

## Commands

```sh
uv sync
uv run fare-dodger download          # BKK GTFS (~62 MB) into data/, conditional requests
uv run fare-dodger build             # build and cache the playable network
uv run fare-dodger play --open       # live game at http://127.0.0.1:8000/ (configs/live.toml)
uv run fare-dodger play --no-osm     # skip the ~40 MB OpenStreetMap download
uv run fare-dodger sim --matrix --games 200 --workers 4 --out results/matrix
uv run fare-dodger balance           # rule variants from configs/balance.toml, writes results/balance/
uv run fare-dodger info | reach | bench | districts | lengths | version   # other CLI subcommands

uv run pytest                        # offline, uses tests/fixtures/mini_gtfs
uv run pytest --cov --cov-report=term-missing   # what CI runs
uv run ruff check && uv run ruff format --check && uv run mypy   # CI lint, format, type-check (mypy is strict)
uv run pre-commit install            # hooks: ruff, mypy, whitespace/yaml/toml checks, 1 MB file cap
```

Verified on Windows: `uv run pytest -q` gives 248 passed, 1 skipped in about 16 s.

## Architecture

All under `src/fare_dodger/`. CLI entry point is `cli.py` (typer, script `fare-dodger`).

```
gtfs/ (download, load with polars) -> network/ (build, merge, patterns, transfers)
  -> Timetable (flat CSR arrays, cached .npz) -> routing/raptor.py
  -> game/ (validate, engine) -> bots/ -> sim/ (runner, report)
```

- `gtfs/`: cached download (ETag/Last-Modified), loaders, service-day choice.
- `network/`: platforms to station nodes, manual merges, footpaths, route patterns, `timetable.py`.
- `routing/`: RAPTOR (`raptor.py`, `reachable()`), journeys.
- `game/`, `bots/`, `sim/`: phase 1 rules engine, bots (`belief.py`, `greedy_dodger.py`, `random_bot.py`), seeded multiprocess simulation.
- `live/`: phase 2. `server.py` (HTTP + `/ws` WebSocket rooms, `/healthz`), `match.py` and `game.py` (match and round logic), `districts.py` (pool of 21 districts, loaded lazily), `streets.py`, `buildings.py`, `cafes.py`, `water.py`, `landmarks.py` (OpenStreetMap layers), `bots.py`, `dodger_bot.py`, `balance.py`, `admin.py`, `static/` (plain JS front end, no build step).
- `config.py` (pydantic models) and `workspace.py` load TOML from `configs/`: `default.toml` (documents every setting), `live.toml`, `balance.toml`, `station_overrides.toml` (manual station merges).
- Design decisions are ADRs in `docs/decisions/`; plan and progress in `docs/plan.md`.

## Gotchas

- `data/` and `results/` are git-ignored: the GTFS feed, OSM layers (`data/osm/<district>/`) and built networks live there. Override the location with `FARE_DODGER_DATA_DIR` or `--data`.
- `tests/test_raptor_realdata.py` is marked `realdata` and skips unless the real feed was downloaded with `fare-dodger download`. Everything else runs offline.
- Pass `-c configs/default.toml` to reproduce published numbers: it pins the service date and applies `station_overrides.toml`. Without `-c` the service day is auto-picked and no overrides apply. The live config uses `service_date = "auto"`.
- `src/fare_dodger/live/static/vendor/` holds vendored MapLibre, three.js and Fredoka (versions in its README.md). Do not edit them; the game uses no CDN, only OpenFreeMap for tiles and glyphs.
- OSM layers come from Overpass (with mirrors) and can be slow; the first full download of 21 districts can take an hour or two. Matches use only districts already downloaded.
- `cp .env.example .env` for deployment settings (PUID/PGID, GAME_BIND, GAME_MEMORY, FARE_DODGER_MAX_MATCHES, FARE_DODGER_MAX_MATCHES_PER_IP, FARE_DODGER_IDLE_S, FARE_DODGER_ADMIN_TOKEN, CLOUDFLARE_TUNNEL_TOKEN). `.env` is git-ignored. The admin page `/admin` is off unless FARE_DODGER_ADMIN_TOKEN is set.
- `deploy/*.sh` are bash and systemd only (Linux server); they need `python3`, git and docker.
- Behind the tunnel the per-IP room cap reads the `CF-Connecting-IP` header.
- First container start needs about 1 GiB of memory (peak 1.2 GiB cgroup); the default limit is 1536m, do not go below about 1 GiB.
- CI (`.github/workflows/ci.yml`) runs on ubuntu and windows, with `uv sync --locked`, so `uv.lock` must be up to date.
- pytest warns about `httpx` with `starlette.testclient` (deprecation); it is harmless.

## Deployment

Documented in README.md ("Deploying on a home server"); ADR in `docs/decisions/0011-online-and-deployment.md`.

- `Dockerfile`: multi-stage (uv 0.12.17 build, python:3.12-slim runtime), unprivileged user, port 8001, healthcheck on `/healthz` (10 min start period). It runs `fare-dodger download` (falls back to the cached feed) then `fare-dodger play -c configs/live.toml --host 0.0.0.0 --port 8001`.
- `compose.yaml`: service `game` (port 8001, `./data` mounted at `/app/data`, runs as PUID:PGID) and optional service `tunnel` (cloudflared, profile `tunnel`, token from `.env`, public hostname points at `http://game:8001`).
- Start: `mkdir -p data && docker compose up -d --build`; with tunnel: `docker compose --profile tunnel up -d`; logs: `docker compose logs -f game`. Set `GAME_BIND=127.0.0.1` to expose only through the tunnel.
- Updates: `./deploy/update.sh` (git pull --ff-only, then `docker compose up -d --build`, status written to `data/admin/`). `./deploy/install-updater.sh` (run once, uses sudo) adds systemd units so the admin page's "Update now" button triggers it.
- CI only checks (lint, format, mypy, tests); it does not build or deploy images.
