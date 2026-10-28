# Changelog

All notable changes to this project are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and the project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
- `fare-dodger download`: cached GTFS download with conditional requests and safe extraction.
- GTFS loading for one service day and time window (polars), automatic midweek date selection.
- Hand-designed mini GTFS fixture for offline tests.
- Project scaffold: uv, src layout, ruff, mypy (strict), pytest, pre-commit, GitHub Actions CI.
