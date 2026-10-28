from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from fare_dodger.config import CircleArea, NetworkConfig
from fare_dodger.gtfs.load import RawFeed, load_feed
from fare_dodger.network.build import build_timetable
from fare_dodger.network.timetable import Timetable

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "mini_gtfs"


@pytest.fixture(scope="session")
def mini_gtfs_dir() -> Path:
    return FIXTURE_DIR


@pytest.fixture(scope="session")
def fixture_network_config() -> NetworkConfig:
    return NetworkConfig(area=CircleArea(center_lat=47.5, center_lon=19.05, radius_m=5000))


@pytest.fixture(scope="session")
def fixture_feed(mini_gtfs_dir: Path, fixture_network_config: NetworkConfig) -> RawFeed:
    return load_feed(mini_gtfs_dir, date(2026, 11, 4), fixture_network_config.window_seconds)


@pytest.fixture(scope="session")
def timetable(fixture_feed: RawFeed, fixture_network_config: NetworkConfig) -> Timetable:
    return build_timetable(fixture_feed, fixture_network_config)
