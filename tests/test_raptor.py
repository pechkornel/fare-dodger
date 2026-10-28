from __future__ import annotations

import dataclasses

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from fare_dodger.modes import ALL_VEHICLES_MASK, Mode, mask_of
from fare_dodger.network.timetable import Timetable
from fare_dodger.routing.journey import Journey, RideLeg, WalkLeg
from fare_dodger.routing.raptor import raptor, reachable
from fare_dodger.timeutil import parse_hms
from tests.routing_oracle import assert_consistent, earliest_arrivals

T0800 = parse_hms("08:00")
TEN_MIN = 600


def n(tt: Timetable, name: str) -> int:
    (idx,) = tt.find_nodes(name)
    return idx


def test_source_is_reached_at_departure(timetable: Timetable) -> None:
    result = reachable(timetable, [n(timetable, "Central")], T0800, TEN_MIN)
    central = n(timetable, "Central")
    assert result.arrival(central) == T0800
    assert result.journey(central) == Journey(central, T0800, ())


def test_metro_one_stop(timetable: Timetable) -> None:
    result = reachable(timetable, [n(timetable, "Central")], T0800, TEN_MIN)
    west = n(timetable, "West")
    assert result.arrival(west) == parse_hms("08:04")  # M1 westbound leaves Central 08:02
    (leg,) = result.journey(west).legs
    assert isinstance(leg, RideLeg)
    assert leg.mode is Mode.METRO
    assert leg.dep == parse_hms("08:02")


def test_tram_then_walk(timetable: Timetable) -> None:
    result = reachable(timetable, [n(timetable, "South")], T0800, TEN_MIN)
    journey = result.journey(n(timetable, "Park"))
    assert [leg.mode for leg in journey.legs] == [Mode.TRAM, Mode.WALK]
    walk = journey.legs[1]
    assert isinstance(walk, WalkLeg)
    assert walk.from_node == n(timetable, "Market")
    assert walk.dep == parse_hms("08:04")
    assert journey.arrival == walk.dep + 211


def test_tram_metro_bus(timetable: Timetable) -> None:
    result = reachable(timetable, [n(timetable, "North")], T0800, 30 * 60)
    journey = result.journey(n(timetable, "Ady Endre utca"))
    assert [leg.mode for leg in journey.legs] == [Mode.TRAM, Mode.METRO, Mode.BUS]
    assert journey.arrival == parse_hms("08:13")


def test_horizon_limits_reach(timetable: Timetable) -> None:
    central = n(timetable, "Central")
    assert reachable(timetable, [central], T0800, 3 * 60).arrival(n(timetable, "West")) is None
    assert set(reachable(timetable, [central], T0800, 0).reached()) == {central}


def test_change_time_after_vehicle(timetable: Timetable) -> None:
    # West 08:00 metro -> Central 08:02; southbound tram leaves Central at 08:04, 08:10, ...
    west, market = n(timetable, "West"), n(timetable, "Market")
    quick = reachable(timetable, [west], T0800, 20 * 60)
    assert quick.arrival(market) == parse_hms("08:07")  # 120 s change: 08:04 is catchable
    slow_tt = dataclasses.replace(
        timetable, node_change_s=[180] * timetable.n_nodes, _name_index={}
    )
    slow = reachable(slow_tt, [west], T0800, 20 * 60)
    assert slow.arrival(market) == parse_hms("08:13")


def test_mode_mask(timetable: Timetable) -> None:
    central, west = n(timetable, "Central"), n(timetable, "West")
    no_metro = mask_of({Mode.TRAM, Mode.BUS, Mode.TROLLEYBUS})
    assert reachable(timetable, [central], T0800, TEN_MIN, mode_mask=no_metro).arrival(west) is None


def test_max_rounds_zero_is_walking_only(timetable: Timetable) -> None:
    market = n(timetable, "Market")
    result = reachable(timetable, [market], T0800, TEN_MIN, max_rounds=0)
    assert set(result.reached()) == {market, n(timetable, "Park")}


def test_multi_source_is_pointwise_minimum(timetable: Timetable) -> None:
    a, b = n(timetable, "West"), n(timetable, "South")
    both = reachable(timetable, [a, b], T0800, 20 * 60).arrivals()
    ra = reachable(timetable, [a], T0800, 20 * 60).arrivals()
    rb = reachable(timetable, [b], T0800, 20 * 60).arrivals()
    for node in set(ra) | set(rb):
        assert both[node] == min(ra.get(node, 1 << 40), rb.get(node, 1 << 40))


def test_departure_after_max_time_reaches_nothing(timetable: Timetable) -> None:
    assert raptor(timetable, [0], T0800, T0800 - 1).reached() == []
    with pytest.raises(ValueError, match="not reachable"):
        raptor(timetable, [0], T0800, T0800 - 1).journey(0)


def test_journey_json_roundtrip(timetable: Timetable) -> None:
    result = reachable(timetable, [n(timetable, "South")], T0800, TEN_MIN)
    journey = result.journey(n(timetable, "Park"))
    assert Journey.from_json(journey.to_json()) == journey
    assert "walk Market -> Park" in journey.describe(timetable.node_name)


@settings(max_examples=150, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    data=st.data(),
    t_dep=st.integers(parse_hms("07:30"), parse_hms("10:30")),
    horizon=st.integers(0, 45 * 60),
    mask=st.integers(1, ALL_VEHICLES_MASK),
)
def test_matches_bruteforce_oracle(
    timetable: Timetable, data: st.DataObject, t_dep: int, horizon: int, mask: int
) -> None:
    sources = data.draw(
        st.lists(st.integers(0, timetable.n_nodes - 1), min_size=1, max_size=3, unique=True)
    )
    result = raptor(timetable, sources, t_dep, t_dep + horizon, mode_mask=mask)
    expected = earliest_arrivals(timetable, sources, t_dep, t_dep + horizon, mask)
    assert result.arrivals() == expected
    for node in result.reached():
        journey = result.journey(node)
        assert journey.arrival == expected[node]
        assert_consistent(timetable, journey, set(sources), t_dep + horizon)
        for ride in journey.rides:
            assert (mask >> list(Mode).index(ride.mode)) & 1
