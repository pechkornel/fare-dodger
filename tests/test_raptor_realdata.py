"""RAPTOR vs. the brute-force oracle on the real BKK network (skipped without the feed)."""

from __future__ import annotations

import random

import pytest

from fare_dodger.config import NetworkConfig
from fare_dodger.modes import ALL_VEHICLES_MASK
from fare_dodger.network.timetable import Timetable
from fare_dodger.routing.raptor import reachable
from fare_dodger.timeutil import parse_hms
from fare_dodger.workspace import Workspace
from tests.routing_oracle import assert_consistent, earliest_arrivals

pytestmark = pytest.mark.realdata


@pytest.fixture(scope="module")
def real_tt() -> Timetable:
    try:
        tt, _, _ = Workspace.at(None).network(NetworkConfig())
    except FileNotFoundError:
        pytest.skip("BKK feed not downloaded (run `fare-dodger download`)")
    return tt


def test_raptor_matches_oracle_on_real_network(real_tt: Timetable) -> None:
    rng = random.Random(7)
    for _ in range(15):
        origin = rng.randrange(real_tt.n_nodes)
        t_dep = parse_hms("08:00") + rng.randrange(0, 7200)
        horizon = rng.choice([600, 1200])
        result = reachable(real_tt, [origin], t_dep, horizon)
        expected = earliest_arrivals(real_tt, [origin], t_dep, t_dep + horizon, ALL_VEHICLES_MASK)
        assert result.arrivals() == expected
        for node in result.reached():
            assert_consistent(real_tt, result.journey(node), {origin}, t_dep + horizon)
