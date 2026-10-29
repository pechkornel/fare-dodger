from __future__ import annotations

import itertools
from datetime import date
from pathlib import Path

import pytest

from fare_dodger.config import CircleArea, NetworkConfig
from fare_dodger.gtfs.load import RawFeed, load_feed
from fare_dodger.modes import Mode
from fare_dodger.network.build import build_timetable, cache_key, load_or_build
from fare_dodger.network.graph import strongly_connected_components
from fare_dodger.network.patterns import TripSequence, build_patterns, collapse_repeats
from fare_dodger.network.timetable import Timetable
from fare_dodger.network.transfers import walking_transfers


def node_by_name(tt: Timetable, name: str) -> int:
    (idx,) = tt.find_nodes(name)
    return idx


def test_fixture_network_shape(timetable: Timetable) -> None:
    assert sorted(timetable.node_name) == [
        "Ady Endre utca", "Central", "East", "Market", "North", "Park", "South", "West",
    ]  # fmt: skip
    central = timetable.node(node_by_name(timetable, "Central"))
    assert central.platform_ids == ("CEN_M1", "CEN_M2", "CEN_T1", "CEN_T2")
    assert central.modes == {Mode.METRO, Mode.TRAM}
    assert central.change_s == 120
    assert timetable.node(node_by_name(timetable, "Market")).change_s == 60
    assert sorted(set(timetable.pat_route_name)) == ["1", "2", "M1"]  # B2 and N9 filtered


def test_walking_transfer_market_park(timetable: Timetable) -> None:
    market, park = node_by_name(timetable, "Market"), node_by_name(timetable, "Park")
    duration = timetable.walk_duration(market, park)
    assert duration is not None
    assert duration == timetable.walk_duration(park, market)
    assert 200 <= duration <= 230  # ~295 m at 5 km/h
    assert timetable.walk_duration(market, node_by_name(timetable, "Central")) is None


def test_overtaking_express_gets_its_own_pattern(timetable: Timetable) -> None:
    bus_patterns = [p for p in range(timetable.n_patterns) if timetable.pat_route_name[p] == "1"]
    assert len(bus_patterns) == 3  # westbound + eastbound regular + eastbound express
    for p in range(timetable.n_patterns):
        trips = range(timetable.pat_trip_ptr[p], timetable.pat_trip_ptr[p + 1])
        times = [timetable.trip_times(t) for t in trips]
        for (arr_a, dep_a), (arr_b, dep_b) in itertools.pairwise(times):
            assert all(x <= y for x, y in zip(arr_a, arr_b, strict=True))
            assert all(x <= y for x, y in zip(dep_a, dep_b, strict=True))


def test_node_pattern_index_is_consistent(timetable: Timetable) -> None:
    for node in range(timetable.n_nodes):
        for k in range(timetable.node_pat_ptr[node], timetable.node_pat_ptr[node + 1]):
            pattern, pos = timetable.node_pat[k], timetable.node_pat_pos[k]
            assert timetable.pattern_stops(pattern)[pos] == node
    for t in range(timetable.n_trips):
        p = timetable.pattern_of_trip(t)
        assert timetable.pat_trip_ptr[p] <= t < timetable.pat_trip_ptr[p + 1]


def test_minor_bus_kept_when_threshold_disabled(
    fixture_feed: RawFeed, fixture_network_config: NetworkConfig
) -> None:
    cfg = fixture_network_config.model_copy(update={"bus_min_departures_per_hour": 0})
    tt = build_timetable(fixture_feed, cfg)
    assert len(tt.find_nodes("Ady Endre utca")) == 2, "same-name stops 3 km apart stay separate"
    included = fixture_network_config.model_copy(update={"include_routes": frozenset({"2"})})
    assert "2" in build_timetable(fixture_feed, included).pat_route_name


def test_mode_and_route_exclusion(
    fixture_feed: RawFeed, fixture_network_config: NetworkConfig
) -> None:
    no_tram = fixture_network_config.model_copy(update={"modes": frozenset({Mode.METRO, Mode.BUS})})
    assert "2" not in build_timetable(fixture_feed, no_tram).pat_route_name
    no_metro = fixture_network_config.model_copy(update={"exclude_routes": frozenset({"M1"})})
    assert "M1" not in build_timetable(fixture_feed, no_metro).pat_route_name


def test_area_clips_trips(fixture_feed: RawFeed) -> None:
    small = NetworkConfig(
        area=CircleArea(center_lat=47.5, center_lon=19.05, radius_m=900),
        largest_component_only=False,
    )
    tt = build_timetable(fixture_feed, small)
    # North is 1.1 km out; Park is inside but its only bus is clipped to a single stop.
    assert sorted(tt.node_name) == ["Central", "Market"]


def test_largest_component_drops_one_way_dead_ends(
    fixture_feed: RawFeed, fixture_network_config: NetworkConfig
) -> None:
    # Far (outside the area) is clipped; with a huge area it is part of the bus loop.
    big = fixture_network_config.model_copy(
        update={"area": CircleArea(center_lat=47.5, center_lon=19.05, radius_m=20_000)}
    )
    assert "Far" in build_timetable(fixture_feed, big).node_name


def test_save_load_roundtrip(timetable: Timetable, tmp_path: Path) -> None:
    timetable.save(tmp_path)
    loaded = Timetable.load(tmp_path)
    for name in ("node_name", "pat_stops", "trip_arr", "trip_dep", "fp_to", "fp_dur", "trip_id"):
        assert getattr(loaded, name) == getattr(timetable, name)
    assert loaded.window == timetable.window
    assert loaded.node(1) == timetable.node(1)


def test_cache_key_depends_on_config_values() -> None:
    a = NetworkConfig()
    b = NetworkConfig(modes=frozenset(sorted(a.modes, reverse=True)))
    assert cache_key("x", a) == cache_key("x", b)
    assert cache_key("x", a) != cache_key("y", a)
    assert cache_key("x", a) != cache_key("x", NetworkConfig(walk_radius_m=300))


def test_load_or_build_uses_cache(
    mini_gtfs_dir: Path, fixture_network_config: NetworkConfig, tmp_path: Path
) -> None:
    first, path, built = load_or_build(mini_gtfs_dir, fixture_network_config, tmp_path, "fp")
    assert built
    second, path2, built2 = load_or_build(mini_gtfs_dir, fixture_network_config, tmp_path, "fp")
    assert not built2
    assert path == path2
    assert second.trip_id == first.trip_id


def test_collapse_repeats() -> None:
    assert collapse_repeats([1, 1, 2, 2, 1], [0, 1, 5, 6, 9], [0, 2, 5, 7, 9]) == (
        [1, 2, 1], [0, 5, 9], [2, 7, 9],
    )  # fmt: skip


def test_fifo_split() -> None:
    slow = TripSequence("slow", "r", (0, 1), (100, 400), (100, 400))
    fast = TripSequence("fast", "r", (0, 1), (200, 300), (200, 300))
    late = TripSequence("late", "r", (0, 1), (500, 800), (500, 800))
    patterns = build_patterns([late, fast, slow])
    assert [[t.trip_id for t in p.trips] for p in patterns] == [["slow", "late"], ["fast"]]


def test_strongly_connected_components() -> None:
    comps = strongly_connected_components(5, [(0, 1), (1, 2), (2, 0), (2, 3), (3, 4)])
    assert sorted(comps) == [[0, 1, 2], [3], [4]]


def test_walking_transfers_radius() -> None:
    pts = [[(47.5, 19.05)], [(47.5, 19.054)], [(47.5, 19.07)]]
    paths = walking_transfers(pts, radius_m=400, speed_mps=5 / 3.6)
    assert {(f.from_node, f.to_node) for f in paths} == {(0, 1), (1, 0)}
    assert walking_transfers(pts, radius_m=0, speed_mps=1) == []


@pytest.mark.parametrize("detour", [1.0, 1.3])
def test_walk_detour_factor(detour: float) -> None:
    pts = [[(47.5, 19.05)], [(47.5, 19.054)]]
    path = walking_transfers(pts, 400, 1.0, detour)[0]
    assert path.duration_s == pytest.approx(path.distance_m * detour, abs=1)


def test_night_buses_are_kept_though_they_never_run_at_rush_hour(
    mini_gtfs_dir: Path, fixture_network_config: NetworkConfig
) -> None:
    # N9 runs once, at 24:30: never in the 08:00-09:00 frequency window.
    cfg = fixture_network_config.model_copy(
        update={
            "window_start": "07:30",
            "window_end": "26:00",
            "night_bus_window": ("24:00", "26:00"),
        }
    )
    feed = load_feed(mini_gtfs_dir, date(2026, 11, 4), cfg.window_seconds)
    rare = cfg.model_copy(update={"night_bus_min_departures_per_hour": 2.0})
    assert "909" not in build_timetable(feed, rare).pat_route_name  # too rare even at night
    night = cfg.model_copy(update={"night_bus_min_departures_per_hour": 0.5})
    tt = build_timetable(feed, night)
    assert "909" in tt.pat_route_name and "B2" not in tt.pat_route_id  # B2 stays out
