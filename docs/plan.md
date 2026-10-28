# Phase 1 plan and progress

Fare Dodger (Hungarian: *Bliccelő*): one hidden Fare Dodger against 3 Inspectors on the real
BKK network. Phase 1 covers the rules engine, transit routing, bots and simulation, with no
UI yet. This file holds the approved plan and the current status. Decisions with lasting
consequences are recorded in [`decisions/`](decisions/).

*Last updated: 2026-10-28 (simulation runs stopped part-way; see "Remaining steps")*.

## Status at a glance

| # | Milestone | Branch / PR | Status |
|---|---|---|---|
| 0 | Scaffold: uv, ruff, mypy strict, pytest, pre-commit, CI (ubuntu + windows) | `main` | ✅ done |
| 1 | GTFS download + loading | #1 `feat/gtfs` | ✅ merged |
| 2 | Playable network (merging, transfers, patterns, cache), ADR 0001 | #2 `feat/network` | ✅ merged |
| 3 | RAPTOR + journeys + `reach`/`bench`, ADR 0002 | #3 `feat/raptor` | ✅ merged |
| 4 | Rules engine, ADR 0003 | #4 `feat/game-engine` | ✅ merged |
| 5 | Bots (random, greedy, belief) | #5 `feat/bots` | ✅ merged |
| 6 | Simulation runner + reports, bot tuning, ADR 0004 | `feat/sim` (pushed, no PR yet) | 🔵 committed; rule-variant runs incomplete |
| 7 | `docs/results.md`, CHANGELOG 0.1.0, tag `v0.1.0` | `docs/release-0.1.0` | ⚪ next |

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
- Real network (feed 2821.20261028, auto service day Tue 2026-11-17): **434 stations, 205
  patterns, 5,401 trips, 1,964 footpaths**. It builds in about 0.5 s from the extracted feed.
- RAPTOR on the real network: a 10-minute query takes **0.35 ms median and 0.84 ms p95**
  (target: < 200 ms). A 45-minute query covering the whole network takes about 3.6 ms.
  Results match the oracle on both the fixture and the real network.
- 80+ offline tests on a hand-made GTFS fixture. CI is green on ubuntu and windows for
  every merged PR.

- Simulation runner (`feat/sim`, committed and pushed). It covers:
  - JSON/CSV/Markdown reports with Wilson 95% intervals, byte-identical on rerun;
  - `fare-dodger sim --matrix --rule key=value`;
  - a coverage placement strategy for BeliefInspectors (`belief`, the default) next to
    `belief-kmeans`, documented in ADR 0004.

  105 offline tests pass.

### Results so far
Default-rules matrix (200 games per matchup, seeds 42–241):

| Dodger | random Inspectors | belief (coverage) | belief-kmeans |
|---|---:|---:|---:|
| random | 90.5% Dodger wins | **50.5%** | 64.0% |
| greedy | 99.0% | 87.0% | **76.5%** |

The Dodger is strongly favoured under the v0 rules. Neither Inspector strategy dominates
(ADR 0004).

Rule variants against the greedy Dodger (200 games each). **The run was stopped
part-way:** only `baseline` and `modes` finished, and `reveal2` was interrupted.

| Variant | vs belief | vs belief-kmeans |
|---|---:|---:|
| baseline (default rules) | 87.0% [82–91%] | 76.5% [70–82%] |
| `reveal_modes=true` | 55.0% [48–62%] | 57.0% [50–64%] |
| `reveal_every=2` | not run | not run |
| `n_inspectors=4` | not run | not run |
| `reveal_modes=true` + `reveal_every=2` | not run | not run |
| `n_inspectors=4` + `reveal_modes=true` | not run | not run |

First signal: revealing the Dodger's modes each round is the strongest single lever found so
far. It takes the greedy Dodger from 77–87% down to 55–57%.

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

### Remaining steps
1. **Open the `feat/sim` PR** against `main`, wait for CI on ubuntu + windows, then merge.
2. **Finish the rule-variant runs** with the commands above. Only the four missing variants
   (`reveal2`, `four`, `modes_reveal2`, `four_modes`) need running; `baseline` and `modes`
   are already done.
3. **Write `docs/results.md`** on a `docs/release-0.1.0` branch, covering:
   - the default-rules matrix and the full rule-variant table, with 95% intervals;
   - catch-type split (stop vs. vehicle), catches by round and the most common catch
     locations;
   - rule-change suggestions derived from the numbers, e.g. turning on mode reveals and
     whichever reveal frequency / Inspector count brings the greedy Dodger towards the
     balance target in open decision 1.

   Restore the README link to `docs/results.md` once the file exists.
4. **Release 0.1.0**, on the same `docs/release-0.1.0` branch:
   - Move the CHANGELOG's `[Unreleased]` entries under `## [0.1.0] - <date>` and update
     this plan's status table.
   - Open the PR, wait for CI to pass, then merge.
   - Tag the merge commit `v0.1.0` and push the tag (`git tag -a v0.1.0 -m "v0.1.0"`,
     then `git push origin v0.1.0`).

## Open decisions (need input)
1. **Balance target.** What win rate should the Dodger have against good Inspectors? 40–50%
   is a common goal. Rule changes will be proposed from the simulations, but the target is
   a design choice.
2. **Inspector positions public.** "Last known positions" was taken to mean that Inspectors
   are visible to the Dodger every round. This can be toggled with `inspectors_visible`.
3. **Pass-through is safe.** Crossing an Inspector's station mid-round is not a catch; only
   end positions and shared vehicles count (ADR 0003). Should passing *through* the same
   station at the same minute count too?
4. **Reproducibility across feed updates.** BKK replaces the feed regularly, so `auto` picks
   a new service day each time. Should simulation configs pin `service_date` and keep the
   feed zip used for published results?
5. **Residual station splits.** 14 names still map to 2–3 nodes 150–490 m apart (separate
   surface stops joined by walks). Is that acceptable, or should the radius be raised?
6. **PR workflow.** PRs are merged by me (merge commits) once CI is green. Do you want to
   review each PR before it is merged?
