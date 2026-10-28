# Phase 1 plan and progress

Fare Dodger (Hungarian: *Bliccelő*): one hidden Fare Dodger against 3 Inspectors on the real
BKK network. Phase 1 covers the rules engine, transit routing, bots and simulation, with no
UI yet. This file holds the approved plan and the current status. Decisions with lasting
consequences are recorded in [`decisions/`](decisions/).

*Last updated: 2026-10-28 (phase 1 released as v0.1.0; phase 2 live prototype in progress)*.

## Status at a glance

| # | Milestone | Branch / PR | Status |
|---|---|---|---|
| 0 | Scaffold: uv, ruff, mypy strict, pytest, pre-commit, CI (ubuntu + windows) | `main` | ✅ done |
| 1 | GTFS download + loading | #1 `feat/gtfs` | ✅ merged |
| 2 | Playable network (merging, transfers, patterns, cache), ADR 0001 | #2 `feat/network` | ✅ merged |
| 3 | RAPTOR + journeys + `reach`/`bench`, ADR 0002 | #3 `feat/raptor` | ✅ merged |
| 4 | Rules engine, ADR 0003 | #4 `feat/game-engine` | ✅ merged |
| 5 | Bots (random, greedy, belief) | #5 `feat/bots` | ✅ merged |
| 6 | Simulation runner + reports, bot tuning, ADR 0004 | #7 `feat/sim` | ✅ merged |
| 6b | Pinned service date, station merge overrides, decisions recorded | #8 `feat/network-pinning` | ✅ merged |
| 7 | `docs/results.md`, CHANGELOG 0.1.0, tag `v0.1.0` | #9 `docs/release-0.1.0` | ✅ released |

## Approved plan

### Context
This is a greenfield portfolio project. Phase 1 delivers a deterministic, tested Python core:
GTFS ingestion, then a playable station graph, a self-written RAPTOR router, the rules
engine, bots, and a seeded simulation runner.

### What the GTFS inspection showed (feed 2821.20261028, valid 2026-10-26 to 2026-12-01)
- 62 MB zipped, 340 MB unzipped. `stop_times.txt` has 6.3 M rows in total, but one weekday
  has ~40 k trips, and the game window has only ~158 k stop events. So only one service day
  and one time window are built.
- There is no `calendar.txt`: service comes from `calendar_dates.txt` only. Times go past
  24:00, so all times are seconds since the start of the service day.
- Route types: bus 3 (including 9xx night buses and rail-replacement buses), tram 0, metro 1,
  trolleybus 11, HÉV 109, ferry 4.
- Only ~10% of platforms have a `parent_station`. Names repeat across the city ("Ady Endre
  utca" ×16, up to 25 km apart), so merging must use name **and** distance.
- The Windows console is cp1252. The CLI reconfigures stdout to UTF-8, and all file I/O is
  explicitly UTF-8.

### Stack
Python 3.12, uv, src layout, Typer CLI `fare-dodger`. numpy (`.npz` cache), polars (GTFS
loading), pydantic v2 (validated config), httpx (download). Dev tools: pytest, hypothesis,
ruff, mypy strict, pre-commit. Configuration is TOML (`configs/default.toml`). Data lives in
`./data/` (gitignored); override with `FARE_DODGER_DATA_DIR`.

### Module structure (as built)
```
src/fare_dodger/
  cli.py          download, build, info, reach, bench, sim
  config.py       NetworkConfig (+ circle/bbox/polygon areas), RulesConfig
  workspace.py    data dir, feed extraction, cached network loading, TOML sections
  geo.py, modes.py, timeutil.py
  gtfs/           download.py (ETag cache, safe extraction), load.py (polars, service day)
  network/        merge.py, transfers.py, patterns.py, graph.py (SCC), timetable.py, build.py
  routing/        raptor.py (RAPTOR + reachable()), journey.py (Walk/Ride legs, JSON)
  game/           validate.py (journey validation), engine.py (Game, views, catches, replay JSON)
  bots/           base.py, random_bot.py, greedy_dodger.py, belief.py
  sim/            play.py (one game), runner.py (seeded, multiprocessing), report.py
```
Deviations from the original sketch, none of which change the design:
- `reachable()` lives in `routing/raptor.py` rather than a separate `query.py`.
- The area tests live on the config models, not in `network/area.py`.
- The event log is `Game.to_json()` rather than `events.py`.
- A `play-bot` CLI command is not built yet; `sim` covers it.

### Data model
- **Node**: a merged station with name, coordinates, platform IDs, mode mask, and change
  time (60 s, or 120 s with metro).
- **Timetable**: flat CSR int lists for patterns, trips, stop times, node→pattern index and
  footpaths. At runtime these are Python lists; on disk they are `.npz` + JSON. The cache
  key is feed sha256 + config + format version + build version.
- **Journey**: origin, departure, and `WalkLeg` / `RideLeg(trip, pattern, board_pos,
  alight_pos, …)`. The empty journey means staying put.

### Key decisions
- **Station merging (ADR 0001)**: platforms merge if they share a parent, or if their name
  (with BKK's ` M` / ` H` / ` M+H` / ` M (street)` suffix stripped) matches within 150 m,
  or 300 m if a rail platform is involved. Only served nodes are kept, then only the largest
  strongly connected component. Walking transfers are ≤ 400 m at 5 km/h.
- **Playable area**: a circle of r = 4.5 km around Deák Ferenc tér. It includes metro,
  tram, trolleybus, and buses with ≥ 6 departures/h between 08:00 and 09:00. HÉV and ferry
  can be switched on.
- **RAPTOR (ADR 0002)**:
  - Features: multi-source queries, a mode mask, and change time only between two vehicles.
  - Semantics: no walk-to-walk; separate vehicle and foot labels make it exact.
  - Testing: checked against a brute-force Dijkstra oracle.
- **Simultaneous rounds (ADR 0003)**: concrete journeys are validated with the same
  semantics as the router. A catch is either the same final station or the same trip with
  overlapping hops. Reveals come after every 3rd round, with optional mode reveals.
  Inspector positions are public. Bots receive view objects only.
- **Bots**: RandomDodger/RandomInspectors; GreedyDodger maximises the minimum Inspector
  travel time over a 45-minute horizon; BeliefInspectors keep a sound belief set and spread
  using k-means.
- **Determinism**: game `i` uses seed `base + i`. Bot RNGs are seeded per (seed, role).
  Results are sorted by seed and reports contain no timestamps.

## Progress

### Done
- Real network (feed 2821.20261028, pinned service day Tue 2026-11-17, with station
  overrides): **421 stations, 204 patterns, 5,401 trips, 1,886 footpaths** (434 / 205 / 1,964
  before the overrides). It builds in about 0.5 s from the extracted feed.
- RAPTOR on the real network: a 10-minute query takes **0.30 ms median and 0.76 ms p95**
  (target: < 200 ms). A 45-minute query covering the whole network takes about 3.6 ms.
  Results match the oracle on both the fixture and the real network.
- 108 offline tests on a hand-made GTFS fixture. CI is green on ubuntu and windows for
  every merged PR.
- Simulation runner (#7): JSON/CSV/Markdown reports with Wilson 95% intervals, byte-identical
  on rerun and independent of the worker count; `fare-dodger sim --matrix --rule key=value`;
  coverage placement for BeliefInspectors (`belief`, the default) next to `belief-kmeans`
  (ADR 0004).
- All rule-variant runs and the default-rules matrix, rerun on the final 421-station network.

### Results
The full write-up is in [`results.md`](results.md). Greedy Dodger win rate against the best
Inspector strategy (200 games each):

| Variant | vs best Inspectors |
|---|---:|
| v0 rules | 76.0% [70–81%] |
| `reveal_modes=true` | 57.0% [50–64%] |
| `reveal_every=2` | 39.0% [33–46%] |
| `n_inspectors=4` | 71.0% [64–77%] |
| `reveal_modes=true` + `reveal_every=2` | **46.5% [40–53%]** |
| `n_inspectors=4` + `reveal_modes=true` | 48.5% [42–55%] |

The suggested v1 rules are mode reveals plus a position reveal every 2nd round, which is on
the 45% target with 3 Inspectors. The v0 defaults are unchanged in 0.1.0.

### Rerunning the rule-variant simulations
Run these from the repository root. They need the downloaded feed (`uv run fare-dodger
download`). Each run is 200 games with 4 workers and the default base seed 42, and takes
about 1–2 minutes, so all 12 take about 15–25 minutes. Output goes to
`results/rules/<variant>-<inspectors>/` (gitignored), and the same command gives
byte-identical results.

bash / zsh (macOS, Git Bash):

```sh
for v in "baseline|" \
         "modes|--rule reveal_modes=true" \
         "reveal2|--rule reveal_every=2" \
         "four|--rule n_inspectors=4" \
         "modes_reveal2|--rule reveal_modes=true --rule reveal_every=2" \
         "four_modes|--rule n_inspectors=4 --rule reveal_modes=true"; do
  name="${v%%|*}"; args="${v#*|}"
  for insp in belief belief-kmeans; do
    echo "== $name greedy vs $insp"
    uv run fare-dodger sim --dodger greedy --inspectors "$insp" --games 200 --workers 4 \
      $args --out "results/rules/$name-$insp" -c configs/default.toml
  done
done
```

PowerShell (Windows):

```powershell
$variants = [ordered]@{
  baseline      = @()
  modes         = @('--rule', 'reveal_modes=true')
  reveal2       = @('--rule', 'reveal_every=2')
  four          = @('--rule', 'n_inspectors=4')
  modes_reveal2 = @('--rule', 'reveal_modes=true', '--rule', 'reveal_every=2')
  four_modes    = @('--rule', 'n_inspectors=4', '--rule', 'reveal_modes=true')
}
foreach ($name in $variants.Keys) {
  foreach ($insp in 'belief', 'belief-kmeans') {
    "== $name greedy vs $insp"
    uv run fare-dodger sim --dodger greedy --inspectors $insp --games 200 --workers 4 `
      $variants[$name] --out "results/rules/$name-$insp" -c configs/default.toml
  }
}
```

The default-rules matrix (all 6 matchups) can be reproduced with:

```sh
uv run fare-dodger sim --matrix --games 200 --workers 4 --out results/m200 -c configs/default.toml
```

### Phase 2: live game (started 2026-10-28)
The owner chose a real-time direction: walk around a real map with WASD and ride real
vehicles, single-player against bots first, team-vs-team multiplayer later. Street walking
comes later; the first prototype uses one district on real map tiles. See ADR 0005.

- ✅ Prototype: `fare-dodger play` on an inner-Pest district (`feat/live-prototype`).
- ✅ Vehicles follow GTFS shapes between stops (`feat/live-shapes`, ADR 0005 addendum).
- ✅ Bounty Hunters, tickets, hidden ticket inspectors on 35% of trips, fines with a hold, and a
  cartoon look (`feat/live-tickets`, ADR 0005 addendum). Games stay fast: about 3 real
  minutes.
- ✅ Matches: first to 6 rounds, sides switch at halftime and every overtime round (win by
  two), a playable Bounty Hunter side with a bot Dodger, and six districts in rotation
  (`feat/live-match`, ADR 0006).
- Next: playtest and tune the bot Dodger, prices and radii per side, and polish the graphics
  (the owner wants more). Then street walking. Multiplayer is further away.

### Next (phase 1 follow-ups)
1. Decide whether to adopt the suggested v1 rules (`reveal_modes = true`, `reveal_every = 2`)
   as the defaults.
2. Make the greedy Dodger avoid dead-end stops at the edge of the area (the most common catch
   locations, see results.md), then rerun the variants.
3. Try other start times and service days to check that the balance holds off-peak.
4. `play-bot` CLI / first UI.

## Decisions (resolved 2026-10-28)
These were open questions; the owner chose the defaults below.

1. **Balance target: the Dodger should win about 45%** of games against the best Inspector
   strategy (the one with the lowest Dodger win rate for that rule set). Rule-change
   suggestions in `docs/results.md` aim at this number.
2. **Visibility: all Inspector positions are public.** Inspectors always see each other, and
   the Dodger always sees the Inspectors (`inspectors_visible = true`, unchanged).
3. **Pass-through is safe.** Passing through the Dodger's station mid-round is not a catch.
   Only ending the round at the same station or sharing a vehicle trip over an overlapping
   stretch counts (ADR 0003, unchanged).
4. **Pinned service date.** `configs/default.toml` pins `service_date = "2026-11-17"` (a
   Tuesday in feed 2821.20261028, the date `auto` picked), so published simulations are
   reproducible. It stays configurable, and `"auto"` is still the code default. Once BKK
   replaces the feed and drops that date, the build fails with a message saying to set a new
   date. Published results name the feed version they used.
5. **Station splits: obvious ones fixed by hand.** `configs/station_overrides.toml` merges 12
   of the 14 residual splits (one interchange each, 150–290 m apart). "Bajza utca" (486 m)
   and "Tímár utca" (323 m) stay split. The network drops from 434 to 421 nodes. See the ADR
   0001 addendum.
6. **PR workflow.** PRs are merged by Claude (merge commits) once CI is green on ubuntu and
   windows, with a two-line summary of each PR given to the owner.
