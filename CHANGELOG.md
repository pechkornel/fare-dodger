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
