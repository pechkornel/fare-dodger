from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from fare_dodger.config import LiveConfig, NetworkConfig
from fare_dodger.live.bots import Agent, journey_state
from fare_dodger.live.game import Keys, LiveGame, Status
from fare_dodger.live.server import create_app
from fare_dodger.live.world import World, distance
from fare_dodger.network.timetable import Timetable
from fare_dodger.routing.raptor import raptor
from fare_dodger.timeutil import parse_hms
from fare_dodger.workspace import live_config

LIVE_TOML = Path(__file__).parents[1] / "configs" / "live.toml"
EIGHT = parse_hms("08:00")


@pytest.fixture(scope="module")
def world(timetable: Timetable, fixture_network_config: NetworkConfig) -> World:
    return World(timetable, fixture_network_config.area)


def node(world: World, name: str) -> int:
    return world.tt.find_nodes(name)[0]


def solo_game(world: World, at: str, **overrides: object) -> LiveGame:
    """A game with the Dodger at station ``at`` and no Inspectors."""
    game = LiveGame(world, LiveConfig.model_validate(overrides), seed=1)
    game.dodger.pos = world.node_xy[node(world, at)]
    game.inspectors.agents = []
    return game


def m1_east_at_eight(world: World) -> int:
    west = node(world, "West")
    return next(
        d.trip for d in world.departures(west, EIGHT, 60, 5) if world.headsign(d.pattern) == "East"
    )


def test_live_toml_matches_code_defaults() -> None:
    assert live_config(LIVE_TOML) == LiveConfig()


def test_vehicle_moves_between_stops(world: World) -> None:
    trip = m1_east_at_eight(world)
    west, central = world.node_xy[node(world, "West")], world.node_xy[node(world, "Central")]
    assert world.trip_state(trip, EIGHT - 1) is None
    start = world.trip_state(trip, EIGHT)
    assert start is not None and start.pos == west and start.at_stop
    half = world.trip_state(trip, EIGHT + 60)
    assert half is not None and not half.at_stop
    mid = ((west[0] + central[0]) / 2, (west[1] + central[1]) / 2)
    assert distance(half.pos, mid) < 1e-6
    assert world.trip_state(trip, EIGHT + 4 * 60 + 1) is None
    assert trip in {s.trip for s in world.active_trips(EIGHT + 60)}


def test_departures_are_sorted_and_skip_terminating_trips(world: World) -> None:
    deps = world.departures(node(world, "West"), EIGHT, 600, 10)
    assert [d.time for d in deps] == sorted(d.time for d in deps)
    # M1 westbound ends at West, so only eastbound trips leave from there.
    assert {d.headsign for d in deps} == {"East"}
    assert deps[0].time == EIGHT


def test_walking_moves_at_walk_speed_and_stays_inside(world: World) -> None:
    game = solo_game(world, "Central")
    x0, y0 = game.dodger.pos
    game.keys = Keys(right=True)
    game.step(10)
    assert game.dodger.pos[0] - x0 == pytest.approx(10 * game.cfg.walk_speed_mps)
    assert game.dodger.pos[1] == pytest.approx(y0)
    game.dodger.pos = (x0 + 4990, y0)  # the fixture area is a 5 km circle
    game.step(60)
    assert world.contains(game.dodger.pos)


def test_wait_board_and_get_off(world: World) -> None:
    game = solo_game(world, "West")
    game.wait_for(0)
    assert game.dodger.waiting_for is not None
    game.step(1)
    assert game.dodger.ride is not None
    assert game.dodger.ride.trip == m1_east_at_eight(world)
    game.step(30)
    game.use()  # get off at the next stop
    assert game.snapshot()["ride"]["alight_at"] == "Central"
    game.step(120)
    dodger = game.dodger  # re-read: mypy keeps the narrowing from before step()
    assert dodger.ride is None
    assert dodger.pos == world.node_xy[node(world, "Central")]
    assert dodger.rides == 1


def test_walking_away_cancels_waiting(world: World) -> None:
    game = solo_game(world, "Central")
    game.wait_for(0)
    game.keys = Keys(up=True)
    game.step(60)
    assert game.dodger.waiting_for is None and game.dodger.ride is None


def test_inspector_on_foot_catches_nearby_dodger(world: World) -> None:
    game = solo_game(world, "Market")
    x, y = game.dodger.pos
    game.inspectors.agents = [Agent(node(world, "Market"), (x + 60, y), node(world, "Market"))]
    game.step(60)
    assert game.status is Status.CAUGHT


def test_riding_past_an_inspector_is_safe_but_stopping_is_not(world: World) -> None:
    game = solo_game(world, "West")
    west, central = world.node_xy[node(world, "West")], world.node_xy[node(world, "Central")]
    mid = ((west[0] + central[0]) / 2, (west[1] + central[1]) / 2)
    watcher = Agent(node(world, "West"), mid, node(world, "West"), needs_replan=False)
    watcher.idle_until = 1e9  # stands still beside the track
    game.inspectors.agents = [watcher]
    game.wait_for(0)
    game.step(90)  # rides straight past the Inspector halfway to Central
    assert game.status.value == "running"
    watcher.pos = central
    game.step(60)  # the train stands at Central next to the Inspector
    assert game.status.value == "caught"


def test_inspector_on_the_same_trip_catches(world: World) -> None:
    tt = world.tt
    plan = raptor(tt, [node(world, "Central")], EIGHT, EIGHT + 900).journey(node(world, "East"))
    state = journey_state(world, plan, EIGHT + 150)
    assert state.trip == m1_east_at_eight(world)  # rides the 08:00 from West, like the Dodger
    game = solo_game(world, "West", catch_radius_m=1.0, sight_radius_m=0.0)
    inspector = Agent(node(world, "Central"), world.node_xy[node(world, "Central")], 0)
    inspector.plan, inspector.needs_replan = plan, False
    game.inspectors.agents = [inspector]
    game.wait_for(0)
    game.step(170)
    assert game.status is Status.CAUGHT
    assert "ticket" in (game.caught_by or "")


def test_pings_and_escape(world: World) -> None:
    game = solo_game(world, "Central", duration_minutes=7, ping_every_s=180)
    game.step(181)
    assert len(game.pings) == 1
    assert game.inspectors.last_ping == game.pings[0]
    assert game.pings[0].node == node(world, "Central")
    game.step(10_000)
    assert game.status is Status.ESCAPED
    assert game.t == game.cfg.end_seconds


def test_bot_inspectors_play_a_whole_game(world: World) -> None:
    game = LiveGame(world, LiveConfig(), seed=3)
    starts = [a.pos for a in game.inspectors.agents]
    moved = False
    for _ in range(3600):
        game.step(0.5)
        moved = moved or any(
            a.pos != s for a, s in zip(game.inspectors.agents, starts, strict=True)
        )
        if game.status is not Status.RUNNING:
            break
    assert moved
    assert game.status in {Status.CAUGHT, Status.ESCAPED}


def test_server_serves_world_and_streams_snapshots(world: World) -> None:
    client = TestClient(create_app(world, LiveConfig(seed=5)))
    data = client.get("/api/world").json()
    assert len(data["stations"]) == world.tt.n_nodes
    assert data["area"]["kind"] == "circle"
    assert "leaflet" in client.get("/").text
    with client.websocket_connect("/ws") as ws:
        first = ws.receive_json()
        assert first["status"] == "running" and first["player"]["state"] == "walk"
        ws.send_json({"type": "keys", "up": True})
        ws.send_json({"type": "restart"})
        assert "clock" in ws.receive_json()
