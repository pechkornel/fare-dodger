# Changelog

All notable changes to this project are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and the project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
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

[Unreleased]: https://github.com/pechkornel/fare-dodger/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/pechkornel/fare-dodger/releases/tag/v0.1.0
