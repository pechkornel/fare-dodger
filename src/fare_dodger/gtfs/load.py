"""Read the GTFS tables needed for one service day and time window.

All CSV columns are read as strings (stop IDs such as ``008271`` must keep their leading zeros)
and converted explicitly. ``stop_times.txt`` is scanned lazily so only the rows of trips that
run on the chosen day and inside the time window are materialised.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Literal

import polars as pl

_TUE_WED_THU = (1, 2, 3)


@dataclass(frozen=True)
class RawFeed:
    """The subset of a GTFS feed used to build a playable network."""

    feed_version: str
    service_date: date
    #: stop_id, stop_name, stop_lat, stop_lon, location_type, parent_station
    stops: pl.DataFrame
    #: route_id, route_short_name, route_type, route_color
    routes: pl.DataFrame
    #: trip_id, route_id, service_id, direction_id
    trips: pl.DataFrame
    #: trip_id, stop_id, stop_sequence, arrival, departure (seconds), sorted by trip, sequence
    stop_times: pl.DataFrame


def _path(feed_dir: Path, name: str) -> Path:
    return feed_dir / f"{name}.txt"


def read_table(
    feed_dir: Path, name: str, required: list[str], optional: list[str] | None = None
) -> pl.DataFrame:
    """Read ``name.txt`` with every column as a string, adding missing optional columns."""
    path = _path(feed_dir, name)
    if not path.is_file():
        msg = f"GTFS table {name}.txt missing in {feed_dir}"
        raise FileNotFoundError(msg)
    df = pl.read_csv(path, infer_schema=False, encoding="utf8-lossy")
    df = df.rename({c: c.lstrip("﻿").strip() for c in df.columns})
    missing = [c for c in required if c not in df.columns]
    if missing:
        msg = f"{name}.txt lacks required columns {missing}"
        raise ValueError(msg)
    for col in optional or []:
        if col not in df.columns:
            df = df.with_columns(pl.lit(None, dtype=pl.String).alias(col))
    return df.select([*required, *(optional or [])])


def _parse_date(value: str) -> date:
    return date(int(value[:4]), int(value[4:6]), int(value[6:8]))


def active_services_by_date(feed_dir: Path) -> dict[date, set[str]]:
    """Active service IDs per date from ``calendar.txt`` and ``calendar_dates.txt``."""
    services: dict[date, set[str]] = defaultdict(set)
    if _path(feed_dir, "calendar").is_file():
        weekdays = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
        cal = read_table(feed_dir, "calendar", ["service_id", *weekdays, "start_date", "end_date"])
        for row in cal.iter_rows(named=True):
            day = _parse_date(row["start_date"])
            end = _parse_date(row["end_date"])
            while day <= end:
                if row[weekdays[day.weekday()]] == "1":
                    services[day].add(row["service_id"])
                day += timedelta(days=1)
    if _path(feed_dir, "calendar_dates").is_file():
        cd = read_table(feed_dir, "calendar_dates", ["service_id", "date", "exception_type"])
        for row in cd.iter_rows(named=True):
            day = _parse_date(row["date"])
            if row["exception_type"] == "1":
                services[day].add(row["service_id"])
            elif row["exception_type"] == "2":
                services[day].discard(row["service_id"])
    return {d: s for d, s in services.items() if s}


def feed_validity(feed_dir: Path) -> tuple[date, date] | None:
    """``feed_start_date`` and ``feed_end_date`` from ``feed_info.txt``, if both are given."""
    if not _path(feed_dir, "feed_info").is_file():
        return None
    info = read_table(feed_dir, "feed_info", [], ["feed_start_date", "feed_end_date"])
    if not info.height:
        return None
    start, end = info["feed_start_date"][0], info["feed_end_date"][0]
    if not start or not end:
        return None
    return _parse_date(start), _parse_date(end)


def pick_service_date(feed_dir: Path, preference: date | Literal["auto"] = "auto") -> date:
    """Resolve the configured service date, inside the feed's validity when it states one.

    ``"auto"`` returns the Tuesday, Wednesday or Thursday with the most active trips (the
    earliest such date on ties), which avoids holidays and atypical Mondays and Fridays. A
    fixed date must have service and lie inside the feed's validity.
    """
    services = active_services_by_date(feed_dir)
    validity = feed_validity(feed_dir)
    if validity is not None:
        first, last = validity
        services = {d: s for d, s in services.items() if first <= d <= last}
    if preference != "auto":
        if preference not in services:
            window = (
                f" (valid {validity[0].isoformat()} to {validity[1].isoformat()})"
                if validity
                else ""
            )
            msg = (
                f"no service on {preference.isoformat()} in this feed{window}; set [network] "
                'service_date to a date the feed covers, or to "auto"'
            )
            raise ValueError(msg)
        return preference
    trips = read_table(feed_dir, "trips", ["trip_id", "service_id"])
    per_service = dict(trips.group_by("service_id").len().select("service_id", "len").iter_rows())
    candidates = [d for d in services if d.weekday() in _TUE_WED_THU] or list(services)
    if not candidates:
        msg = "feed has no active service dates"
        raise ValueError(msg)

    def trip_count(day: date) -> int:
        return sum(per_service.get(s, 0) for s in services[day])

    return min(candidates, key=lambda d: (-trip_count(d), d))


def _hms_to_seconds(col: str) -> pl.Expr:
    parts = pl.col(col).str.strip_chars().str.split(":")
    return (
        parts.list.get(0).cast(pl.Int32) * 3600
        + parts.list.get(1).cast(pl.Int32) * 60
        + parts.list.get(2, null_on_oob=True).fill_null("0").cast(pl.Int32)
    )


def feed_version(feed_dir: Path) -> str:
    if not _path(feed_dir, "feed_info").is_file():
        return "unknown"
    info = read_table(feed_dir, "feed_info", [], ["feed_version"])
    value = info["feed_version"][0] if info.height else None
    return str(value) if value else "unknown"


def load_feed(
    feed_dir: Path,
    service_date: date,
    window: tuple[int, int],
) -> RawFeed:
    """Load stops, routes and the trips of ``service_date`` overlapping ``window`` (seconds)."""
    services = active_services_by_date(feed_dir).get(service_date, set())
    if not services:
        msg = f"no service on {service_date.isoformat()}"
        raise ValueError(msg)

    stops = read_table(
        feed_dir,
        "stops",
        ["stop_id", "stop_name", "stop_lat", "stop_lon"],
        ["location_type", "parent_station"],
    ).with_columns(
        pl.col("stop_lat").cast(pl.Float64),
        pl.col("stop_lon").cast(pl.Float64),
        pl.col("location_type").fill_null("0").cast(pl.Int32),
    )
    routes = read_table(
        feed_dir, "routes", ["route_id", "route_type"], ["route_short_name", "route_color"]
    ).with_columns(
        pl.col("route_type").cast(pl.Int32),
        pl.col("route_short_name").fill_null(pl.col("route_id")),
    )
    trips = read_table(
        feed_dir, "trips", ["route_id", "service_id", "trip_id"], ["direction_id", "trip_headsign"]
    ).filter(pl.col("service_id").is_in(sorted(services)))

    stop_times_lf = (
        pl.scan_csv(_path(feed_dir, "stop_times"), infer_schema=False, encoding="utf8-lossy")
        .select("trip_id", "stop_id", "stop_sequence", "arrival_time", "departure_time")
        .join(trips.lazy().select("trip_id"), on="trip_id", how="semi")
        .with_columns(
            _hms_to_seconds("arrival_time").alias("arrival"),
            _hms_to_seconds("departure_time").alias("departure"),
            pl.col("stop_sequence").cast(pl.Int32),
        )
        .with_columns(
            pl.coalesce("arrival", "departure").alias("arrival"),
            pl.coalesce("departure", "arrival").alias("departure"),
        )
        .drop("arrival_time", "departure_time")
        .drop_nulls(["arrival", "departure"])
    )
    start, end = window
    spans = stop_times_lf.group_by("trip_id").agg(
        pl.col("departure").min().alias("first"), pl.col("arrival").max().alias("last")
    )
    in_window = spans.filter((pl.col("last") >= start) & (pl.col("first") <= end)).select("trip_id")
    stop_times = (
        stop_times_lf.join(in_window, on="trip_id", how="semi")
        .sort("trip_id", "stop_sequence")
        .collect()
    )
    trips = trips.join(stop_times.select("trip_id").unique(), on="trip_id", how="semi").sort(
        "trip_id"
    )
    return RawFeed(
        feed_version=feed_version(feed_dir),
        service_date=service_date,
        stops=stops,
        routes=routes,
        trips=trips,
        stop_times=stop_times,
    )
