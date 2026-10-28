from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from fare_dodger.gtfs.load import (
    active_services_by_date,
    feed_version,
    load_feed,
    pick_service_date,
)
from fare_dodger.timeutil import parse_hms

WINDOW = (parse_hms("07:30"), parse_hms("11:30"))


def test_services_by_date(mini_gtfs_dir: Path) -> None:
    services = active_services_by_date(mini_gtfs_dir)
    assert services[date(2026, 11, 4)] == {"WK"}
    assert services[date(2026, 11, 7)] == {"SA"}


def test_auto_service_date_prefers_busiest_midweek_day(mini_gtfs_dir: Path) -> None:
    # Wednesday and Thursday tie; the earlier one wins. Saturday is never chosen.
    assert pick_service_date(mini_gtfs_dir) == date(2026, 11, 4)


def test_explicit_service_date_must_exist(mini_gtfs_dir: Path) -> None:
    assert pick_service_date(mini_gtfs_dir, date(2026, 11, 7)) == date(2026, 11, 7)
    with pytest.raises(ValueError, match="no service"):
        pick_service_date(mini_gtfs_dir, date(2026, 1, 1))


def test_load_feed_filters_day_and_window(mini_gtfs_dir: Path) -> None:
    feed = load_feed(mini_gtfs_dir, date(2026, 11, 4), WINDOW)
    assert feed.feed_version == feed_version(mini_gtfs_dir) == "fixture-1"
    trip_ids = set(feed.trips["trip_id"])
    assert not any(t.startswith("M1S_") for t in trip_ids), "Saturday trips leaked in"
    assert "N9_0" not in trip_ids, "night bus is outside the window"
    assert "M1E_00" not in trip_ids, "07:00 metro ends before 07:30"
    assert "M1E_08" in trip_ids  # 07:32 departure
    assert set(feed.stop_times["trip_id"]) == trip_ids
    first = feed.stop_times.filter(feed.stop_times["trip_id"] == "B1E_X").row(0, named=True)
    assert first["stop_id"] == "PRK_1"
    assert first["departure"] == parse_hms("08:05")


def test_load_feed_handles_times_after_midnight(mini_gtfs_dir: Path) -> None:
    feed = load_feed(mini_gtfs_dir, date(2026, 11, 4), (parse_hms("24:00"), parse_hms("26:00")))
    assert set(feed.trips["trip_id"]) == {"N9_0"}
    assert feed.stop_times["arrival"].max() == parse_hms("24:40")


def test_stop_ids_keep_leading_zeros(tmp_path: Path, mini_gtfs_dir: Path) -> None:
    for name in ("stops", "routes", "trips", "stop_times", "calendar_dates"):
        text = (mini_gtfs_dir / f"{name}.txt").read_text(encoding="utf-8")
        (tmp_path / f"{name}.txt").write_text(text.replace("CEN_M1", "008271"), encoding="utf-8")
    feed = load_feed(tmp_path, date(2026, 11, 4), WINDOW)
    assert "008271" in set(feed.stops["stop_id"])
    assert "008271" in set(feed.stop_times["stop_id"])
    assert feed.feed_version == "unknown"
