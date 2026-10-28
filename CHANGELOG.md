# Changelog

All notable changes to this project are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and the project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.2.0] - 2026-10-28

The live game: a real-time, online, 3D version of Fare Dodger, ready to self-host.

### Added
- Online play: create a room (1v1, 2v2 or 3v3) or join one by its code, or play solo. Empty
  places are bots, the hunting side gets two extra bots, a random team runs first, and the
  host starts the match. Every Dodger is a point (escaped: runners, caught: hunters), so a
  round can end 1-1. Players who leave are replaced by bots. ADR 0011.
- Deployment: a multi-stage Dockerfile (uv, non-root) and `compose.yaml` with a `game`
  service on port 8001, `./data` as a volume, a `/healthz` healthcheck, `restart:
  unless-stopped`, a memory limit, and a `tunnel` service (cloudflared, token from `.env`,
  profile `tunnel`). See the README's deployment section.
- Safety caps from the environment: at most 20 rooms, at most 2 rooms per address (using
  `CF-Connecting-IP` behind the tunnel), and rooms close when empty or after 2 minutes without
  input.
- Live prototype (`fare-dodger play`): a real-time, single-player browser game on an inner-Pest
  district. Walk with WASD, wait for and ride real trips, and avoid bot Inspectors who are pinged
  your position every 3 minutes. FastAPI WebSocket server, Leaflet client on OpenStreetMap tiles,
  `configs/live.toml`; ADR 0005.
- Live game: vehicles follow their GTFS shapes between stops instead of straight lines, and
  the map draws route lines along the same paths (cached as `hops.json`).
- Live game: tickets (buy at a stop with T, or ride without one), hidden ticket inspectors on
  35% of trips, and fines that hold you at the next stop and alert the chasers. The chasers are
  now called Bounty Hunters (`n_hunters`). The client has a cartoon look.
- Live game: matches against bots. First to 6 rounds wins, sides switch at halftime, and at
  5-5 sides switch every round until one team leads by two. You can now play a Bounty Hunter
  hunting a bot Dodger. Each round is on one of six Budapest districts (`[[district]]` in
  `configs/live.toml`). ADR 0006.
- Live game: walking follows real OpenStreetMap streets (downloaded once; `--free-walk` to
  skip), for you and the bots. Time runs at 6× instead of 10× (rounds are about 4 minutes),
  and characters jog at 10 km/h. Bounty Hunters now catch you only on the same vehicle or when
  you are both on foot. ADR 0007.
- Live game: smooth route lines. Vehicles stop at their platforms on the route shape instead
  of spiking into the station centre, and shapes are cleaned of spikes and jitter.
- Live game: toy-town graphics. MapLibre on OpenFreeMap vector tiles in a cartoon style, flat
  when zoomed out. Zoomed in, the camera tilts into a three.js 3D town of rounded-cube houses,
  trees, stops, vehicles and people. `tile_url` is now a TileJSON, with a new `glyphs_url`;
  `tile_attribution` is removed. ADR 0008.
- Live game: detailed vehicles (articulated trolleybuses, multi-section trams, metro cars with
  bogies), redesigned characters, facade details and roof props, and smaller people next to
  bigger vehicles. `/models.html` shows every model. ADR 0008 addendum.
- Live game: riders sit inside their vehicle (a pin floats above it), and vehicles bend at their
  bellows and couplers along the path they drive, with some suspension motion. Zooming is
  faster. ADR 0008 addendum.
- Live game: coffee. Real cafés from OpenStreetMap, 4 per district. Press C: 500 Ft and 3
  seconds standing still, then 20 seconds at 16 km/h (the Dodger only). Overpass downloads
  fall back to mirrors. ADR 0009.
- Live game: main menu (pick your starting side, Esc to return), a CS-style top bar with the
  round timer, a 3-second countdown before each round, a minimap (with the reports and your
  team for Bounty Hunters; never the hunters for the Dodger), an edge arrow pointing hunters
  at the latest report, a blue/red get-on/get-off switch, cancelling a get-off with E,
  Bounty Hunters always two more than the Dodgers (`hunter_extra`), parks and squares walkable
  in any direction, and vehicles that appear facing their direction of travel. ADR 0010.

### Changed
- `configs/live.toml` picks the service day automatically (`service_date = "auto"`): the
  busiest Tuesday, Wednesday or Thursday inside the downloaded feed's validity, which is now
  respected by `"auto"` and checked for pinned dates. `configs/default.toml` stays pinned to
  keep the published simulation numbers reproducible.
- MapLibre GL JS 4.7.1, three.js 0.169.0 and the Fredoka font are vendored into the static
  files instead of loaded from unpkg, jsdelivr and Google Fonts.

## [0.1.0] - 2026-10-28

### Added
- `docs/results.md`: bot matchups, rule-variant results on the pinned network, and
  suggested rule changes (mode reveals + position reveal every 2nd round).
- Manual station merges: `station_merge_overrides` in `NetworkConfig`, loaded from the file
  named by `[network] station_overrides` (`configs/station_overrides.toml` merges 12 split
  interchanges; the default network has 421 stations instead of 434). ADR 0001 addendum.
- Simulation runner (`fare-dodger sim`): seeded matchups or `--matrix`, multiprocessing with
  results independent of worker count, `--rule key=value` overrides, results.json / games.csv /
  summary.md with Wilson 95% intervals.
- BeliefInspectors coverage placement (default) and `belief-kmeans` for comparison; ADR 0004.
- Bots: RandomDodger, RandomInspectors, GreedyDodger (max-min Inspector travel time),
  BeliefInspectors (sound belief set via multi-source RAPTOR, k-means coverage).
- Rules engine (ADR 0003): simultaneous 10-minute rounds, journey validation, same-stop and
  same-vehicle catches, position/mode reveals, information-hiding views, random starts with a
  minimum travel-time distance, JSON replay log. `[rules]` section in the config.
- RAPTOR router (ADR 0002): multi-source, mode mask, change times, journey reconstruction;
  property-tested against a brute-force oracle. `fare-dodger reach` and `fare-dodger bench`.
- Playable network build: area filter, frequency-based bus selection, station merging (ADR 0001),
  walking transfers, FIFO route patterns, largest strongly connected component, cached `.npz` timetable.
- `fare-dodger build` and `fare-dodger info`; `configs/default.toml`.
- `fare-dodger download`: cached GTFS download with conditional requests and safe extraction.
- GTFS loading for one service day and time window (polars), automatic midweek date selection.
- Hand-designed mini GTFS fixture for offline tests.
- Project scaffold: uv, src layout, ruff, mypy (strict), pytest, pre-commit, GitHub Actions CI.

### Changed
- `configs/default.toml` pins `service_date = "2026-11-17"` so simulations are reproducible;
  a pinned date missing from the feed now gives an error saying how to fix it.

[Unreleased]: https://github.com/pechkornel/fare-dodger/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/pechkornel/fare-dodger/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/pechkornel/fare-dodger/releases/tag/v0.1.0
