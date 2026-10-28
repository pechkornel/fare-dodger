# Fare Dodger

A hidden-movement chase game played on the **real Budapest public transport timetable**
(Hungarian title: *Bliccelő*). One hidden **Fare Dodger** tries to stay ahead of three
**Inspectors** for 12 rounds. Every round is 10 minutes of real weekday service: metro,
tram, trolleybus and the main bus lines inside the inner districts.

> **Status:** Phase 1, the engine. It covers GTFS ingestion, a playable station network, a
> self-written RAPTOR router, the rules engine, bots and a deterministic simulation runner.
> There is no UI yet.

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

### Live prototype

```sh
uv run fare-dodger play --open         # http://127.0.0.1:8000/ (uses configs/live.toml)
```

A fast, cartoony real-time game on real timetables, played as a **match** against bots,
CS-style.
- **Rounds:** each round lasts 3 real minutes and is played on a different Budapest district
  (six on both banks of the Danube).
- **Winning the match:** the first team to win 6 rounds takes it. You play the Dodger for
  rounds 1–5 and a Bounty Hunter for rounds 6–10. At 5-5, sides switch every round until one
  team leads by two.
- **Controls:** walk with **WASD**. Stand at a stop and press **E** (or **1–6** to pick a
  departure) to wait for a real tram, trolleybus, bus or metro. Press **E** again to get off at
  the next stop. **Enter** starts the next round.
- **As the Dodger:** survive. Buy a ticket at a stop with **T** (450 Ft of your 2,000 Ft), or
  *bliccel* and ride without one.
  - Ticket inspectors ride about 35% of the trips, and you only find out once you are aboard.
  - Without a ticket, you are put off at the next stop and held while the fine is written,
    and the Bounty Hunters are told where you are. If you can't pay, you lose the round.
  - Three Bounty Hunters learn your position every 3 timetable minutes, chase you on sight,
    and catch you on foot, at a stop, or on the same vehicle.
- **As a Bounty Hunter:** hunt the bot Dodger with two bot teammates. You see the Dodger only
  up close or through the reports.

See [ADR 0005](docs/decisions/0005-live-prototype.md) and
[ADR 0006](docs/decisions/0006-live-matches.md).

Every command takes `-c configs/default.toml`, which documents every network and rule
setting. `--rule key=value` overrides a single rule for a simulation. The config pins the
service date and applies the manual station merges in `configs/station_overrides.toml`, so
pass it to reproduce published numbers. Without `-c`, the service day is picked
automatically from whatever feed is downloaded and no station overrides are applied.

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
