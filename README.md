# Fare Dodger

A hidden-movement chase game played on the **real Budapest public transport timetable**
(Hungarian title: *Bliccelő*). One hidden **Fare Dodger** tries to stay ahead of three
**Inspectors** for 12 rounds. Every round is 10 minutes of real weekday service: metro,
tram, trolleybus and the main bus lines inside the inner districts.

> **Status:** 0.2.0. Phase 1 is the engine: GTFS ingestion, a playable station network, a
> self-written RAPTOR router, the rules engine, bots and a deterministic simulation runner.
> Phase 2 is the live game: a real-time browser game in 3D, played online in teams (1v1 to
> 3v3, bots fill the empty places) and ready to self-host with Docker.

## The game (v0 rules, all configurable)

- Rounds are **simultaneous**. Each player privately submits a concrete journey (walk and
  ride legs on real trips) to any station they can reach within the round. All moves resolve
  at once, and the server validates every journey against the timetable.
- The Dodger's position is revealed to the Inspectors after every 3rd round. Optionally, the
  modes the Dodger used each round are revealed too.
- The Inspectors win if one of them ends a round at the Dodger's station, or rode the same
  vehicle trip as the Dodger over an overlapping stretch. The Dodger wins by surviving 12
  rounds.
- Start positions are random, with every Inspector at least 20 minutes of travel from the
  Dodger.

## Quick start

Requires [uv](https://docs.astral.sh/uv/). All commands work the same in PowerShell, bash
and zsh.

```sh
uv sync
uv run fare-dodger download            # ~62 MB BKK GTFS, cached (conditional requests)
uv run fare-dodger build               # build + cache the playable network (~1 s)
uv run fare-dodger info                # stations, routes per mode, merged stations
uv run fare-dodger reach "Deák Ferenc tér" --at 08:00 --minutes 10 --journeys
uv run fare-dodger bench               # RAPTOR timing on random stations
uv run fare-dodger sim --matrix --games 200 --workers 4 --out results/matrix
uv run fare-dodger sim --dodger greedy --inspectors belief --rule reveal_modes=true
```

### Live game

```sh
uv run fare-dodger play --open         # http://127.0.0.1:8000/ (uses configs/live.toml)
```

A fast, cartoony real-time game on real timetables, played online as a **match** between two
teams, CS-style.
- **Playing together:** the menu offers **Play solo** (you against bots), **Create a game**
  (1v1, 2v2 or 3v3) and **Join a game** by its 4-letter code. In the lobby, players join the
  smaller team and can switch. The host starts the match whenever they like: empty places are
  played by bots, the hunting side always gets three extra bots, and a random team runs first.
- **Scoring:** every Dodger is a point. Escaping scores for the runners, being caught scores
  for the hunters, so a 2v2 round can end 1–1. The hunters win the round outright by catching
  everyone. A match is quick (best of 3) or long (best of 6), picked in the menu: the first
  team past half of the regulation points wins (2 of 3 or 4 of 6 in 1v1).
- **Rounds:** each round starts at a random time of day, from the 🌙 night buses of the small
  hours to the evening rush. It lasts about 4 real minutes (35 timetable minutes at 9× speed) and is
  played on a different Budapest district, picked at random from 21 around the city. The
  Danube is a wall: cross it on the bridges.
- **Sides:** teams switch sides at halftime. After a tie, they switch every round until one
  team leads by two. **Esc** leaves to the menu.
- **Controls:** **WASD** move you up, left, down and right on the screen; hold two for a
  diagonal. You can walk anywhere except through buildings: streets, rail lines, parks and
  squares are all open, and you slide along walls. **Right-drag** turns the map, and **N** turns
  it back to north. The streets (for the bots' routes), buildings and cafés come from
  OpenStreetMap and are downloaded once, about 40 MB; use `--no-osm` to skip them. Stand at a
  stop and press **E** (or **1–6** to pick a
  departure) to wait for a real tram, trolleybus, bus or metro. Press **E** again to get off at
  the next stop. Press **E** again (or click the switch, bottom right: blue on board, red getting
  off) to stay on. **Enter** starts the next round.
- **As the Dodger:** survive. You start with 1,000 Ft. Buy a ticket at a stop with **T**
  (450 Ft), or *bliccel* and ride without one.
  - Earn money with packages: 📦 boxes wait around the district (only the Dodgers see them).
    Each one goes to a landmark (a statue, monument, museum, church or theatre) one or two
    rides away, shown when you stand at it. Press **E** to take it. The landmark is marked on
    the map, with an arrow when it is off screen, and delivering pays 1,200 Ft. Get caught and
    the package is lost.
  - Real cafés (☕ on the map) sell coffee with **C**: 500 Ft and 3 seconds standing still,
    then 20 seconds running almost as fast as a bus. The barista reports you to the Bounty
    Hunters, at that café.
  - Ticket inspectors ride about 35% of the trips, and you only find out once you are aboard.
  - Without a ticket, you are put off at the next stop and held while the fine is written,
    and the Bounty Hunters are told where you are. If you can't pay it all, they take what
    you have and you go to jail: the district's 🚔 police station, for 30 seconds. The
    Bounty Hunters are told, so it's a race: run when the door opens.
  - You see the Bounty Hunters only within 200 m and zoomed in. Otherwise the top bar shows
    how far the nearest one is: red when close, green when far.
  - Four Bounty Hunters learn your position every 180 timetable seconds (20 real seconds), chase you within 200 m,
    and catch you when you are both on foot (waiting at a stop counts), or when they ride
    the same vehicle. Staying aboard is safe.
- **As a Bounty Hunter:** hunt the Dodgers with your team. You see them only up close (200 m, zoomed in) or
  through the reports on the minimap, and an arrow at the screen edge points to the latest one.
- **Pings:** press the scroll wheel (middle click) on the map to mark a spot for your team,
  for 8 seconds. Bots ping too: Bounty Hunter bots when they spot a Dodger, Dodger bots
  when a Bounty Hunter comes close.
- **Map:** zoom with the mouse wheel or **+/−**. Zoomed out, the map is flat. Zoomed in, it
  tilts into a 3D toy town of rounded-cube houses, trams, buses and people.

See [ADR 0005](docs/decisions/0005-live-prototype.md),
[ADR 0006](docs/decisions/0006-live-matches.md),
[ADR 0007](docs/decisions/0007-street-walking-and-pace.md),
[ADR 0008](docs/decisions/0008-toy-town-graphics.md),
[ADR 0009](docs/decisions/0009-coffee.md),
[ADR 0010](docs/decisions/0010-menu-minimap-parks.md),
[ADR 0012](docs/decisions/0012-free-walking-and-rotation.md),
[ADR 0013](docs/decisions/0013-packages.md) and
[ADR 0011](docs/decisions/0011-online-and-deployment.md).

Every command takes `-c configs/default.toml`, which documents every network and rule
setting. `--rule key=value` overrides a single rule for a simulation. The config pins the
service date and applies the manual station merges in `configs/station_overrides.toml`, so
pass it to reproduce published numbers. Without `-c`, the service day is picked
automatically from whatever feed is downloaded and no station overrides are applied.

## Deploying on a home server

The live game ships as a Docker image (multi-stage build with uv, runs as an unprivileged
user) with a Compose file: a `game` service on port **8001**, and an optional `tunnel` service
(Cloudflare Tunnel) to publish it without opening a port.

```sh
git clone https://github.com/pechkornel/fare-dodger.git && cd fare-dodger
cp .env.example .env            # set PUID/PGID (`id -u`, `id -g`) and, for the tunnel, the token
mkdir -p data                   # the feed, OpenStreetMap layers and built networks live here
docker compose up -d --build    # the game on http://<server>:8001/
docker compose --profile tunnel up -d   # also start cloudflared
docker compose logs -f game
```

- **First start:** the game downloads the BKK feed (62 MB) and builds the first district.
  Then it downloads the other districts' OpenStreetMap streets, buildings, cafés and water in
  the background, about 2–4 MB each, from Overpass with mirrors as fallback. They go into
  `data/osm/<district>/`. Overpass can be slow, so this can take an hour or two for all 21
  districts. Matches use the districts already downloaded, so the game is playable right
  away. Districts load into memory when a round needs them (at most 6 at once, `--keep`),
  and the next round's district loads during the current round. Later starts take seconds. On
  every start it checks for a newer feed (a conditional request), and keeps the cached one if
  BKK can't be reached. The service day is picked automatically: the Tuesday, Wednesday or
  Thursday with the most trips inside the feed's validity (`service_date = "auto"` in
  `configs/live.toml`; set a date to pin one).
- **Health:** `GET /healthz` answers `{"ok": true, "rooms": …, "players": …}`. Compose uses it
  as the healthcheck (with a 10-minute start period for the first build), and the tunnel
  waits for it.
- **Cloudflare Tunnel:** create a tunnel in Cloudflare Zero Trust (Networks > Tunnels), copy
  its token into `CLOUDFLARE_TUNNEL_TOKEN` in `.env`, and give it a public hostname pointing at
  `http://game:8001`. WebSockets work through tunnels without extra settings. To reach the game
  only through the tunnel, set `GAME_BIND=127.0.0.1`.
- **Safety caps** (environment variables in `.env`):

  | Variable | Default | What |
  |---|---:|---|
  | `FARE_DODGER_MAX_MATCHES` | 20 | rooms (lobbies and matches) at once; more are told the server is full |
  | `FARE_DODGER_MAX_MATCHES_PER_IP` | 2 | rooms one address can be in at once (`CF-Connecting-IP` behind the tunnel) |
  | `FARE_DODGER_IDLE_S` | 120 | a room closes after this many seconds without input from anyone |

  A room also stops as soon as its last WebSocket closes.
- **Memory:** measured on the first start (Docker, cold network cache, feed and OSM layers
  already downloaded):
  - the Python process peaked at **859 MiB** while building the networks;
  - the container's cgroup peaked at **1,215 MiB**, including reclaimable file cache from
    unpacking the 340 MB feed;
  - serving, the process uses about **300 MiB** (the building footprints add about 75 MiB),
    plus little per room.

  `GAME_MEMORY` (the Compose memory limit) defaults to 1,536 MiB; don't go below about
  1 GiB, or the first build can be killed.
- **No CDNs:** MapLibre GL JS, three.js and the Fredoka font are served by the game itself
  (`src/fare_dodger/live/static/vendor/`, with their licences). The map tiles and label glyphs
  come from OpenFreeMap.
- **Admin page:** set `FARE_DODGER_ADMIN_TOKEN` in `.env` to a long random password
  (`openssl rand -hex 24`), then open `/admin` and type it.
  - **What it shows:** the running version and commit, uptime, memory, players, every game
    (code, size, players, round and score) with a **Close** button, and the timetable in use.
  - **Update now:** asks the server to update itself. Install the updater once with
    `./deploy/install-updater.sh`. It adds a systemd path unit that, when the button is
    pressed, runs `deploy/update.sh` as you: `git pull`, then
    `docker compose up -d --build`, with your git login and Docker group. The admin page
    shows the progress and the log. Running games are cut off, and the restart also refreshes
    the timetable.
  - **Locking it down:** behind Cloudflare Access, add a second Access application for the
    path `/admin` (and `/api/admin`) that allows only your email.
- **Updating by hand:** `./deploy/update.sh`, or `git pull && docker compose up -d --build`. The cached data in `./data` is
  reused. Networks are rebuilt automatically when the feed or the config changes.

## Architecture

```
GTFS zip ──download──▶ gtfs/ ──load (polars, one service day + window)──▶ RawFeed
     ──network/ (area, bus frequency filter, station merging, footpaths, FIFO patterns, SCC)──▶
Timetable (flat CSR arrays, cached .npz) ──▶ routing/raptor.py ──▶ game/ (validate, engine)
                                                         └──▶ bots/ ──▶ sim/ (runner, reports)
```

| Package | Responsibility |
|---|---|
| `gtfs` | Cached download (ETag/Last-Modified), safe extraction, polars loaders, service-day choice |
| `network` | Platforms → station nodes, walking transfers, route patterns, compact `Timetable` |
| `routing` | RAPTOR (multi-source, mode mask, change times), journeys |
| `game` | Rules config, journey validation, simultaneous rounds, catches, reveals, player views |
| `bots` | Random, greedy Dodger, belief-state Inspectors (coverage and k-means placement) |
| `sim` | Seeded matchups, multiprocessing, JSON/CSV/Markdown reports |

Design decisions are recorded as ADRs in [`docs/decisions/`](docs/decisions/):
station merging, RAPTOR, simultaneous rounds and belief-state bots. Plan and progress
are tracked in [`docs/plan.md`](docs/plan.md).

## Numbers

On the default network (feed 2821.20261028, pinned service day 2026-11-17): 421 stations,
204 route patterns, 5,401 trips and 1,886 footpaths.

| | |
|---|---|
| Network build from the extracted feed | ~0.5 s |
| 10-min reachability query (RAPTOR), median / p95 | **0.30 ms / 0.76 ms** |
| 45-min query covering the whole network, median | ~3.6 ms |
| Full 12-round bot game | 0.03–0.6 s |

Simulation results and rule-change suggestions are in [`docs/results.md`](docs/results.md).
In short: under the v0 rules the greedy Dodger beats the best Inspector bots 76% of the
time. Revealing the Dodger's modes every round and its position every 2nd round brings
that to 46.5%, close to the 45% balance target.

## Development

```sh
uv run pytest                 # offline; uses a hand-made GTFS fixture
uv run ruff check && uv run ruff format --check && uv run mypy
uv run pre-commit install     # ruff, mypy and hygiene hooks on commit
```

- The tests include a hypothesis property test comparing RAPTOR with a brute-force Dijkstra
  oracle, and a check that the Inspectors' belief never loses the true Dodger.
- Tests marked `realdata` compare against the real feed and are skipped when it has not been
  downloaded.
- CI runs on Ubuntu and Windows. `.gitattributes` enforces LF line endings.
- Commits follow Conventional Commits; features land via pull requests;
  see [CHANGELOG.md](CHANGELOG.md).

## Data source

Timetable data: **BKK Budapesti Közlekedési Központ** public GTFS feed
(`https://go.bkk.hu/api/static/v1/public-gtfs/budapest_gtfs.zip`). The data is downloaded
on demand and never committed. This project is not affiliated with or endorsed by BKK and
uses none of its logos or branding.

## License

MIT, see [LICENSE](LICENSE).
