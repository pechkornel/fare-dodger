"""Generate the tiny hand-designed GTFS fixture in ``tests/fixtures/mini_gtfs``.

Run ``uv run python tests/fixtures/make_mini_gtfs.py`` after editing; the output is committed.

Layout (north is up, roughly 1 km between labelled points)::

                 Ady Endre utca (B, 2.8 km north, bus B2 only)
                        |
                      North ------------ B2 (every 30 min: filtered as a minor bus)
                        |  tram T2
    West ===M1=== Central ===M1=== East ---- B1 ---- Far (8 km east, outside the area)
                        |                  /
                      Market  ..walk..  Park
                        |                 \\__ Ady Endre utca (A, same name as B, 3 km apart)
                      South

Features exercised by the tests:
* "Central": a parent station with two metro platforms and an entrance, plus two tram
  platforms of the same name without a parent (merged by name and distance).
* "Ady Endre utca": two unrelated stops with the same name that must NOT be merged.
* Market and Park are 300 m apart: a walking transfer joins the tram and bus B1.
* Bus B1 has an express trip that overtakes the regular one (forces a FIFO pattern split).
* Night bus N9 runs after midnight (times >= 24:00:00).
* A Saturday-only service that must be ignored on a weekday.
"""

from __future__ import annotations

import csv
from pathlib import Path

OUT = Path(__file__).parent / "mini_gtfs"

STOPS = [
    # stop_id, name, lat, lon, location_type, parent_station
    ("CEN", "Central", 47.5000, 19.0500, "1", ""),
    ("CEN_M1", "Central", 47.5000, 19.0501, "0", "CEN"),
    ("CEN_M2", "Central", 47.5000, 19.0499, "0", "CEN"),
    ("CEN_E", "Central", 47.5002, 19.0500, "2", "CEN"),
    ("CEN_T1", "Central", 47.5004, 19.0503, "0", ""),
    ("CEN_T2", "Central", 47.5004, 19.0497, "0", ""),
    ("WST_1", "West", 47.5000, 19.0340, "0", ""),
    ("WST_2", "West", 47.5001, 19.0340, "0", ""),
    ("EST_1", "East", 47.5000, 19.0660, "0", ""),
    ("EST_2", "East", 47.5001, 19.0660, "0", ""),
    ("NOR_1", "North", 47.5100, 19.0500, "0", ""),
    ("NOR_2", "North", 47.5100, 19.0501, "0", ""),
    ("MKT_1", "Market", 47.4950, 19.0500, "0", ""),
    ("MKT_2", "Market", 47.4950, 19.0501, "0", ""),
    ("STH_1", "South", 47.4880, 19.0500, "0", ""),
    ("STH_2", "South", 47.4880, 19.0501, "0", ""),
    ("PRK_1", "Park", 47.4950, 19.0540, "0", ""),
    ("PRK_2", "Park", 47.4951, 19.0540, "0", ""),
    ("ADA_1", "Ady Endre utca", 47.4950, 19.0700, "0", ""),
    ("ADA_2", "Ady Endre utca", 47.4951, 19.0700, "0", ""),
    ("ADB_1", "Ady Endre utca", 47.5250, 19.0500, "0", ""),
    ("FAR_1", "Far", 47.5000, 19.1600, "0", ""),
]

ROUTES = [
    # route_id, short_name, route_type, color
    ("M1", "M1", "1", "FFD800"),
    ("T2", "2", "0", "FFD800"),
    ("B1", "1", "3", "009EE3"),
    ("B2", "2", "3", "009EE3"),
    ("N9", "909", "3", "000000"),
]


def hms(seconds: int) -> str:
    h, rest = divmod(seconds, 3600)
    m, s = divmod(rest, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def t(hh: int, mm: int) -> int:
    return hh * 3600 + mm * 60


def main() -> None:
    OUT.mkdir(exist_ok=True)
    trips: list[tuple[str, str, str, str]] = []
    stop_times: list[tuple[str, str, str, str, int]] = []

    def add_trip(
        route: str,
        service: str,
        trip_id: str,
        direction: int,
        start: int,
        legs: list[tuple[str, int]],
    ) -> None:
        """``legs`` = [(stop_id, minutes after start), ...]; no dwell time."""
        trips.append((route, service, trip_id, str(direction)))
        for seq, (stop, offset) in enumerate(legs, start=1):
            clock = hms(start + offset * 60)
            stop_times.append((trip_id, clock, clock, stop, seq))

    # Metro M1 West <-> Central <-> East, every 4 minutes, 2 minutes per hop.
    for k in range(46):
        dep = t(7, 0) + 240 * k
        add_trip("M1", "WK", f"M1E_{k:02d}", 0, dep, [("WST_1", 0), ("CEN_M1", 2), ("EST_1", 4)])
        add_trip("M1", "WK", f"M1W_{k:02d}", 1, dep, [("EST_2", 0), ("CEN_M2", 2), ("WST_2", 4)])
    # Saturday metro (different service): must not appear on a weekday.
    for k in range(10):
        dep = t(7, 0) + 600 * k
        add_trip("M1", "SA", f"M1S_{k:02d}", 0, dep, [("WST_1", 0), ("CEN_M1", 3), ("EST_1", 6)])
    # Tram 2 North <-> Central <-> Market <-> South, every 6 minutes, 3 minutes per hop.
    for k in range(31):
        dep = t(7, 1) + 360 * k
        add_trip(
            "T2",
            "WK",
            f"T2S_{k:02d}",
            0,
            dep,
            [("NOR_1", 0), ("CEN_T1", 3), ("MKT_1", 6), ("STH_1", 9)],
        )
        add_trip(
            "T2",
            "WK",
            f"T2N_{k:02d}",
            1,
            dep,
            [("STH_2", 0), ("MKT_2", 3), ("CEN_T2", 6), ("NOR_2", 9)],
        )
    # Bus 1 Park <-> Ady Endre utca (A) <-> East <-> Far, every 10 minutes.
    for k in range(19):
        dep = t(7, 2) + 600 * k
        add_trip(
            "B1",
            "WK",
            f"B1E_{k:02d}",
            0,
            dep,
            [("PRK_1", 0), ("ADA_1", 4), ("EST_1", 7), ("FAR_1", 17)],
        )
        add_trip(
            "B1",
            "WK",
            f"B1W_{k:02d}",
            1,
            dep - 2 * 60,
            [("FAR_1", 0), ("EST_2", 10), ("ADA_2", 13), ("PRK_2", 17)],
        )
    # Express bus 1 that leaves at 08:05 but reaches Far before the 08:02 departure.
    add_trip(
        "B1", "WK", "B1E_X", 0, t(8, 5), [("PRK_1", 0), ("ADA_1", 2), ("EST_1", 3), ("FAR_1", 10)]
    )
    # Bus 2 North <-> Ady Endre utca (B), every 30 minutes (below the main-bus threshold).
    for k in range(7):
        dep = t(7, 10) + 1800 * k
        add_trip("B2", "WK", f"B2N_{k}", 0, dep, [("NOR_1", 0), ("ADB_1", 5)])
        add_trip("B2", "WK", f"B2S_{k}", 1, dep + 600, [("ADB_1", 0), ("NOR_2", 5)])
    # Night bus after midnight.
    add_trip("N9", "WK", "N9_0", 0, t(24, 30), [("CEN_T1", 0), ("MKT_1", 5), ("STH_1", 10)])

    def write(name: str, header: list[str], rows: list[tuple[object, ...]]) -> None:
        with (OUT / f"{name}.txt").open("w", encoding="utf-8", newline="") as fh:
            writer = csv.writer(fh, lineterminator="\n")
            writer.writerow(header)
            writer.writerows(rows)

    write(
        "agency",
        ["agency_id", "agency_name", "agency_url", "agency_timezone"],
        [("FX", "Fixture Transit", "https://example.invalid", "Europe/Budapest")],
    )
    write(
        "feed_info",
        ["feed_publisher_name", "feed_publisher_url", "feed_lang", "feed_version"],
        [("Fixture", "https://example.invalid", "en", "fixture-1")],
    )
    write(
        "stops",
        ["stop_id", "stop_name", "stop_lat", "stop_lon", "location_type", "parent_station"],
        [(s[0], s[1], f"{s[2]:.6f}", f"{s[3]:.6f}", s[4], s[5]) for s in STOPS],
    )
    write(
        "routes",
        ["agency_id", "route_id", "route_short_name", "route_type", "route_color"],
        [("FX", *r) for r in ROUTES],
    )
    write("trips", ["route_id", "service_id", "trip_id", "direction_id"], list(trips))
    write(
        "stop_times",
        ["trip_id", "arrival_time", "departure_time", "stop_id", "stop_sequence"],
        list(stop_times),
    )
    # Wednesday 2026-11-04 and Thursday 2026-11-05 are weekdays; 2026-11-07 is a Saturday.
    write(
        "calendar_dates",
        ["service_id", "date", "exception_type"],
        [("WK", "20261104", "1"), ("WK", "20261105", "1"), ("SA", "20261107", "1")],
    )


if __name__ == "__main__":
    main()
