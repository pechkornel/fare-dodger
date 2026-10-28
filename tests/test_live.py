from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from fare_dodger.config import LiveConfig, NetworkConfig
from fare_dodger.live.bots import Agent, journey_state
from fare_dodger.live.game import Keys, LiveGame, Status, world_view
from fare_dodger.live.server import create_app
from fare_dodger.live.shapes import HOPS_FILE, Hops, build_hops, cut_shape, load_or_build_hops
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


@pytest.fixture(scope="module")
def hops(timetable: Timetable, mini_gtfs_dir: Path) -> Hops:
    return build_hops(mini_gtfs_dir, timetable)


@pytest.fixture(scope="module")
def shaped_world(timetable: Timetable, fixture_network_config: NetworkConfig, hops: Hops) -> World:
    return World(timetable, fixture_network_config.area, hops)


#: The fixture's eastbound M1 shape bends north through this point between West and Central.
BEND = (47.5030, 19.0420)


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


def test_cut_shape_interpolates_the_ends() -> None:
    points = [(0.0, 0.0), (0.0, 1.0), (1.0, 1.0)]
    dists = [0.0, 10.0, 20.0]
    assert cut_shape(points, dists, 5.0, 15.0) == [(0.0, 0.5), (0.0, 1.0), (0.5, 1.0)]
    assert cut_shape(points, dists, 0.0, 10.0) == [(0.0, 0.0), (0.0, 1.0)]


def test_hops_follow_the_shape_only_where_there_is_one(timetable: Timetable, hops: Hops) -> None:
    for p in range(timetable.n_patterns):
        stops = [timetable.node_name[n] for n in timetable.pattern_stops(p)]
        if timetable.pat_route_name[p] == "M1" and stops == ["West", "Central", "East"]:
            assert BEND in hops[p][0]
            assert all(len(hop) >= 2 for hop in hops[p])
        else:
            assert all(hop == [] for hop in hops[p])


def test_vehicle_follows_the_shape(shaped_world: World) -> None:
    world = shaped_world
    trip = m1_east_at_eight(world)
    west, central = world.node_xy[node(world, "West")], world.node_xy[node(world, "Central")]
    start = world.trip_state(trip, EIGHT)
    assert start is not None and start.pos == west
    bend = world.proj.to_xy(*BEND)
    # Halfway in time is halfway along the bent path: near the bend, far north of the track.
    half = world.trip_state(trip, EIGHT + 60)
    assert half is not None and distance(half.pos, bend) < 30
    assert half.pos[1] - west[1] > 250
    arrive = world.trip_state(trip, EIGHT + 120)
    assert arrive is not None and arrive.pos == central
    previous = west
    for s in range(1, 121):
        state = world.trip_state(trip, EIGHT + s)
        assert state is not None
        assert distance(previous, state.pos) < 15  # ~1.4 km in 120 s: no jumps
        previous = state.pos


def test_world_view_draws_shaped_lines(shaped_world: World) -> None:
    lines = world_view(shaped_world, LiveConfig())["lines"]
    m1 = [line for line in lines if line["route"] == "M1"]
    assert any(
        any(abs(lat - BEND[0]) < 1e-6 and abs(lon - BEND[1]) < 1e-6 for lat, lon in line["path"])
        for line in m1
    )


def test_hops_are_cached(timetable: Timetable, mini_gtfs_dir: Path, tmp_path: Path) -> None:
    built = load_or_build_hops(mini_gtfs_dir, timetable, tmp_path)
    assert (tmp_path / HOPS_FILE).is_file()
    assert load_or_build_hops(tmp_path / "no-feed-here", timetable, tmp_path) == built
