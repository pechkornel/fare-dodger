# Changelog

All notable changes to this project are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and the project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
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
