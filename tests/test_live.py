from __future__ import annotations

import dataclasses
import itertools
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from fare_dodger.config import LiveConfig, NetworkConfig
from fare_dodger.live.bots import Agent, Ping, journey_state
from fare_dodger.live.game import Keys, LiveGame, Role, Status, world_view
from fare_dodger.live.match import District, Match, match_winner, role_for
from fare_dodger.live.server import create_app
from fare_dodger.live.shapes import (
    HOPS_FILE,
    Hops,
    build_hops,
    cut_shape,
    load_or_build_hops,
    smooth_path,
)
from fare_dodger.live.world import World, distance
from fare_dodger.network.timetable import Timetable
from fare_dodger.routing.raptor import raptor
from fare_dodger.timeutil import parse_hms
from fare_dodger.workspace import live_config, live_districts

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
    """A game with the Dodger at station ``at``, no Bounty Hunters and (unless overridden) no
    ticket inspectors."""
    game = LiveGame(world, LiveConfig.model_validate({"inspected_share": 0.0, **overrides}), seed=1)
    game.dodger.pos = world.node_xy[node(world, at)]
    game.hunters.agents = []
    return game


def m1_east_at_eight(world: World) -> int:
    west = node(world, "West")
    return next(
        d.trip for d in world.departures(west, EIGHT, 60, 5) if world.headsign(d.trip) == "East"
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


def test_hunter_on_foot_catches_nearby_dodger(world: World) -> None:
    game = solo_game(world, "Market")
    x, y = game.dodger.pos
    game.hunters.agents = [Agent(node(world, "Market"), (x + 60, y), node(world, "Market"))]
    game.step(60)
    assert game.status is Status.CAUGHT


def test_hunters_on_foot_catch_only_a_dodger_on_foot(world: World) -> None:
    central = world.node_xy[node(world, "Central")]
    for get_off in (False, True):
        game = solo_game(world, "West")
        watcher = Agent(node(world, "Central"), central, node(world, "Central"))
        watcher.needs_replan, watcher.idle_until = False, 1e9  # stands at Central
        game.hunters.agents = [watcher]
        game.wait_for(0)
        game.step(90)  # riding towards Central
        if get_off:
            game.use()
        game.step(40)  # the train stops at Central at 08:02, next to the Bounty Hunter
        expected = Status.CAUGHT if get_off else Status.RUNNING
        assert game.status is expected


def test_hunter_on_the_same_trip_catches(world: World) -> None:
    tt = world.tt
    plan = raptor(tt, [node(world, "Central")], EIGHT, EIGHT + 900).journey(node(world, "East"))
    state = journey_state(world, plan, EIGHT + 150)
    assert state.trip == m1_east_at_eight(world)  # rides the 08:00 from West, like the Dodger
    game = solo_game(world, "West", catch_radius_m=1.0, sight_radius_m=0.0)
    hunter = Agent(node(world, "Central"), world.node_xy[node(world, "Central")], 0)
    hunter.plan, hunter.needs_replan = plan, False
    game.hunters.agents = [hunter]
    game.wait_for(0)
    game.step(170)
    assert game.status is Status.CAUGHT
    assert "riding the M1" in (game.caught_by or "")


def test_pings_and_escape(world: World) -> None:
    game = solo_game(world, "Central", duration_minutes=7, ping_every_s=180)
    game.step(181)
    assert len(game.pings) == 1
    assert game.hunters.last_ping == game.pings[0]
    assert game.pings[0].node == node(world, "Central")
    game.step(10_000)
    assert game.status is Status.ESCAPED
    assert game.t == game.cfg.end_seconds


def test_bot_hunters_play_a_whole_game(world: World) -> None:
    game = LiveGame(world, LiveConfig(), seed=3)
    starts = [a.pos for a in game.hunters.agents]
    moved = False
    for _ in range(3600):
        game.step(0.5)
        moved = moved or any(a.pos != s for a, s in zip(game.hunters.agents, starts, strict=True))
        if game.status is not Status.RUNNING:
            break
    assert moved
    assert game.status in {Status.CAUGHT, Status.ESCAPED}


def test_server_serves_districts_and_streams_a_match(world: World) -> None:
    client = TestClient(create_app([District("Fixture", world)], LiveConfig(seed=5)))
    data = client.get("/api/world/0").json()
    assert len(data["stations"]) == world.tt.n_nodes
    assert data["area"]["kind"] == "circle" and data["name"] == "Fixture"
    assert client.get("/api/world/1").status_code == 404
    assert "maplibre-gl" in client.get("/").text
    with client.websocket_connect("/ws") as ws:
        ws.send_json({"type": "solo", "role": "dodger"})
        first = ws.receive_json()
        assert first["type"] == "snapshot"
        assert first["status"] == "running" and first["player"]["state"] == "walk"
        assert first["match"]["round"] == 1 and first["match"]["role"] == "dodger"
        assert first["match"]["score"] == {"you": 0, "them": 0}
        ws.send_json({"type": "keys", "up": True})
        ws.send_json({"type": "next"})  # ignored: the round is still running
        ws.send_json({"type": "restart"})  # ignored: the match is not over
        assert ws.receive_json()["match"]["round"] == 1


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
    pattern = world.trip_pattern[trip]
    west, central = world.node_xy[node(world, "West")], world.node_xy[node(world, "Central")]
    # Vehicles stand where the shape passes the platform, not at the merged station centre.
    platform = world.proj.to_xy(47.5000, 19.0340)  # WST_1
    start = world.trip_state(trip, EIGHT)
    assert start is not None and start.pos == world.stop_point(pattern, 0)
    assert distance(start.pos, platform) < 1e-6 and 0 < distance(start.pos, west) < 15
    bend = world.proj.to_xy(*BEND)
    # Halfway in time is halfway along the bent path: near the bend, far north of the track.
    half = world.trip_state(trip, EIGHT + 60)
    assert half is not None and distance(half.pos, bend) < 30
    assert half.pos[1] - west[1] > 250
    arrive = world.trip_state(trip, EIGHT + 120)
    assert arrive is not None and arrive.pos == world.stop_point(pattern, 1)
    assert distance(arrive.pos, central) < 30
    assert world.stop_distance(arrive.pos, node(world, "Central")) == 0
    previous = start.pos
    for s in range(1, 241):
        state = world.trip_state(trip, EIGHT + s)
        assert state is not None
        assert distance(previous, state.pos) < 15  # ~1.4 km in 120 s: no jumps, even at stops
        previous = state.pos


def test_pattern_paths_are_continuous_through_stops(shaped_world: World) -> None:
    world = shaped_world
    for p in range(world.tt.n_patterns):
        hops = world._paths[p]
        for (before, _), (after, _) in itertools.pairwise(hops):
            assert before[-1] == after[0]


def test_world_view_draws_shaped_lines(shaped_world: World) -> None:
    lines = world_view(shaped_world, LiveConfig())["lines"]
    m1 = [line for line in lines if line["route"] == "M1"]
    bend = shaped_world.proj.to_xy(*BEND)
    assert any(
        any(distance(shaped_world.proj.to_xy(lat, lon), bend) < 10 for lat, lon in line["path"])
        for line in m1
    )  # through the bend (its corner rounded off a little)


def test_hops_are_cached(timetable: Timetable, mini_gtfs_dir: Path, tmp_path: Path) -> None:
    built = load_or_build_hops(mini_gtfs_dir, timetable, tmp_path)
    assert (tmp_path / HOPS_FILE).is_file()
    assert load_or_build_hops(tmp_path / "no-feed-here", timetable, tmp_path) == built


def board_first_departure(game: LiveGame) -> None:
    game.wait_for(0)
    game.step(1)
    assert game.dodger.ride is not None


def test_buy_ticket_only_at_a_stop_and_with_money(world: World) -> None:
    game = solo_game(world, "West", start_money_ft=500, ticket_price_ft=450)
    game.buy_ticket()
    assert (game.dodger.money, game.dodger.tickets) == (50, 1)
    game.buy_ticket()  # not enough money left
    assert (game.dodger.money, game.dodger.tickets) == (50, 1)
    game.dodger.pos = (game.dodger.pos[0], game.dodger.pos[1] + 500)  # away from any stop
    game.dodger.money = 1000
    game.buy_ticket()
    assert game.dodger.tickets == 1


def test_inspected_share_and_determinism(world: World) -> None:
    share = len(LiveGame(world, LiveConfig(), seed=7).inspected) / world.tt.n_trips
    assert 0.2 < share < 0.5
    assert (
        LiveGame(world, LiveConfig(), seed=7).inspected
        == LiveGame(world, LiveConfig(), seed=7).inspected
    )
    assert not LiveGame(world, LiveConfig(inspected_share=0.0), seed=7).inspected


def test_ticket_is_used_on_boarding_and_passes_inspection(world: World) -> None:
    game = solo_game(world, "West", inspected_share=1.0)
    game.buy_ticket()
    money = game.dodger.money
    board_first_departure(game)
    ride = game.dodger.ride
    assert ride is not None and ride.ticket and not ride.fined
    assert game.dodger.tickets == 0
    game.step(300)
    assert game.status is Status.RUNNING
    assert (game.dodger.money, game.dodger.fines) == (money, 0)


def test_no_ticket_means_a_fine_and_being_held(world: World) -> None:
    game = solo_game(
        world, "West", inspected_share=1.0, start_money_ft=2000, fine_ft=1000, fine_hold_s=60
    )
    board_first_departure(game)
    ride = game.dodger.ride
    assert ride is not None and ride.fined
    game.use()  # can't choose where to get off any more
    assert game.snapshot()["ride"]["alight_at"] == "Central"
    game.step(120)  # arrives at Central at 08:02
    central = world.node_xy[node(world, "Central")]
    assert game.dodger.pos == central and game.dodger.held_until is not None
    assert game.pings and game.pings[-1].node == node(world, "Central")  # reported
    game.keys = Keys(up=True)
    game.step(30)
    assert game.dodger.pos == central  # held: can't walk away
    assert game.snapshot()["player"]["state"] == "held"
    game.step(31)
    dodger = game.dodger  # re-read: mypy keeps the narrowing from before step()
    assert dodger.held_until is None
    assert (dodger.money, dodger.fines) == (1000, 1)
    game.step(10)
    assert dodger.pos != central  # free to go


def test_cannot_pay_the_fine_means_jail_not_game_over(world: World) -> None:
    game = solo_game(
        world,
        "West",
        inspected_share=1.0,
        start_money_ft=300,
        fine_ft=1000,
        jail_s=30.0,
        time_scale=6.0,
    )
    board_first_departure(game)
    assert game.status is Status.RUNNING
    assert any("jail" in e.text for e in game.drain_events())
    game.step(120)  # put off at Central at 08:02, and taken to the jail
    jail, name = world.jail_spot()  # no police data here: a post at a central station
    dodger = game.dodger
    assert dodger.jailed and dodger.pos == jail and name.startswith("the police post at")
    held_until = dodger.held_until
    assert held_until is not None and 0 < held_until - game.t <= 30 * 6  # 30 real seconds
    view = game.snapshot()
    assert view["jailed"] and view["jail"]["name"] == name and view["jail"]["busy"]
    ping = game.pings[-1]  # the Bounty Hunters know where, and until when
    assert ping.pos == jail and ping.until == held_until
    game.keys = Keys(up=True)
    game.step(held_until - game.t - 1)
    assert game.dodger.pos == jail  # can't walk out early
    game.step(2)
    dodger = game.dodger  # re-read: mypy keeps the narrowing from before step()
    assert dodger.held_until is None and not dodger.jailed and game.status is Status.RUNNING
    assert (dodger.money, dodger.fines) == (0, 1)  # they kept everything they had
    game.step(5)
    assert dodger.pos != jail  # out and running


def test_bot_hunters_walk_from_the_station_to_the_jail_door(world: World) -> None:
    game = solo_game(world, "West")
    station = node(world, "Central")
    door = (world.node_xy[station][0] + 120.0, world.node_xy[station][1])  # a street away
    agent = Agent(station, world.node_xy[station], station, needs_replan=False, idle_until=1e9)
    game.hunters.agents = [agent]
    game.hunters.last_pings = [Ping(int(game.t), door, station, 0, until=game.t + 60)]
    game.hunters.advance(game.t + 20, 20, [])
    assert distance(agent.pos, door) < 120 - 20  # on the way to the door
    walked = agent.pos
    game.hunters.advance(game.t + 61, 1, [])  # the Dodger is out: back to the network
    assert agent.returning_to == station
    assert distance(agent.pos, world.node_xy[station]) < distance(walked, world.node_xy[station])


def test_sides_switch_at_halftime_and_every_overtime_round() -> None:
    d, h = Role.DODGER, Role.HUNTER
    assert [role_for(n, 10, d) for n in range(1, 11)] == [d] * 5 + [h] * 5
    assert [role_for(n, 10, d) for n in range(11, 15)] == [d, h, d, h]
    assert role_for(1, 10, h) is h


def test_match_winner_needs_six_or_a_two_round_lead_in_overtime() -> None:
    assert match_winner(6, 4, 10, 10) is True
    assert match_winner(1, 6, 7, 10) is False
    assert match_winner(5, 4, 9, 10) is None
    assert match_winner(5, 5, 10, 10) is None  # overtime
    assert match_winner(6, 5, 11, 10) is None
    assert match_winner(7, 5, 12, 10) is True
    assert match_winner(6, 8, 14, 10) is False


def finish_round(match: Match, won: bool) -> None:
    """End the current round with the human's side winning or losing."""
    game = match.game
    dodger_wins = won == (game.role is Role.DODGER)
    for runner in game.runners:
        runner.caught = not dodger_wins
    game.status = Status.ESCAPED if dodger_wins else Status.CAUGHT
    match.update()


def test_match_plays_rounds_switches_sides_and_ends(world: World) -> None:
    districts = [District("A", world), District("B", world)]
    match = Match(districts, LiveConfig(regulation_rounds=4), seed=3)
    assert match.next_round() is False  # round 1 still running
    seen = []
    for won in (True, False, True):
        seen.append((match.round_number, match.role, match.district))
        finish_round(match, won)
        assert match.view()["round_over"]
        assert match.next_round()
    assert [s[1] for s in seen] == [Role.DODGER, Role.DODGER, Role.HUNTER]
    assert all(a[2] != b[2] for a, b in itertools.pairwise(seen))  # districts rotate
    assert match.game.role is Role.HUNTER
    finish_round(match, True)  # 3-1: first to 3 of 4
    view = match.view()
    assert view["winner"] == "you" and view["score"] == {"you": 3, "them": 1}
    assert [r["won"] for r in view["history"]] == [True, False, True, True]
    assert match.next_round() is False


def test_human_hunter_catches_the_dodger_on_foot(world: World) -> None:
    game = LiveGame(world, LiveConfig(inspected_share=0.0), seed=2, role=Role.HUNTER)
    assert game.player is not game.dodger and not game.player.pays_fares
    assert len(game.hunters.agents) == LiveConfig().n_hunters - 1
    assert game.snapshot()["wallet"] is None
    game.hunters.agents = []
    game.dodger_bot = None  # hold the Dodger still
    game.player.pos = (game.dodger.pos[0] + 60, game.dodger.pos[1])
    assert game.snapshot()["dodger"] is not None  # within sight
    game.keys = Keys(left=True)
    game.step(30)
    assert game.status is Status.CAUGHT and game.player_won is True
    assert game.caught_by == "You caught the Dodger!"


def test_dodger_is_hidden_from_hunters_when_far(world: World) -> None:
    game = LiveGame(world, LiveConfig(), seed=2, role=Role.HUNTER)
    game.hunters.agents = []
    game.player.pos = (game.dodger.pos[0] + 2000, game.dodger.pos[1])
    snap = game.snapshot()
    assert snap["dodger"] is None and snap["seen"] is False and snap["role"] == "hunter"


def test_bot_dodger_travels_and_rounds_finish(world: World) -> None:
    outcomes = set()
    moved = 0
    for seed in range(6):
        game = LiveGame(world, LiveConfig(), seed=seed, role=Role.HUNTER)
        start = game.dodger.pos
        while game.status is Status.RUNNING:
            game.step(1.0)
        moved += game.dodger.pos != start  # some are caught waiting for their first vehicle
        outcomes.add(game.status)
        assert game.player_won is (game.status is Status.CAUGHT)
    assert moved >= 3
    assert Status.CAUGHT in outcomes


def test_bot_hunters_never_outrun_their_legs(world: World) -> None:
    # The bug: a journey ending at its arrival second left the bot at its origin station, so
    # the next one started there: a jump across the district, or a sprint through houses.
    cfg = LiveConfig()
    for seed in range(6):
        game = LiveGame(world, cfg, seed=seed, role=Role.HUNTER)
        while game.status is Status.RUNNING:
            before = [(a.pos, a.trip) for a in game.hunters.agents]
            game.step(1.0)
            dt = cfg.time_scale
            for (pos, trip), agent in zip(before, game.hunters.agents, strict=True):
                if trip is None and agent.trip is None:
                    assert distance(pos, agent.pos) <= cfg.walk_speed_mps * dt + 1e-6


def test_live_toml_districts() -> None:
    districts = live_districts(LIVE_TOML)
    assert len(districts) >= 4
    assert districts[0][0] == "Inner Pest"
    assert all(net.area.kind in ("polygon", "circle") for _, net in districts)
    assert sum(net.area.kind == "circle" for _, net in districts) >= 10  # around the city
    assert len({net.area.model_dump_json() for _, net in districts}) == len(districts)
    assert live_districts(None)[0][0] == "District"


def test_smooth_path_drops_spikes_and_jitter_but_keeps_the_stops() -> None:
    # A straight 300 m run with 0.5 m jitter and a 10 m out-and-back spike in the middle.
    path = [(0.0, 0.0), (50.0, 0.4), (100.0, -0.3), (150.0, 0.0), (150.0, 10.0), (150.5, 0.2)]
    path += [(200.0, -0.4), (300.0, 0.0)]
    smooth = smooth_path(path)
    assert smooth[0] == (0.0, 0.0) and smooth[-1] == (300.0, 0.0)
    assert max(abs(y) for _, y in smooth) < 0.5
    # A right angle gets rounded, inside the corner.
    corner = smooth_path([(0.0, 0.0), (100.0, 0.0), (100.0, 100.0)])
    assert corner[0] == (0.0, 0.0) and corner[-1] == (100.0, 100.0)
    assert (100.0, 0.0) not in corner and len(corner) > 3


def test_server_starts_on_the_side_picked_in_the_menu_after_a_countdown(world: World) -> None:
    client = TestClient(create_app([District("Fixture", world)], LiveConfig(seed=5)))
    with client.websocket_connect("/ws?role=hunter") as ws:
        first = ws.receive_json()
        assert first["match"]["role"] == "hunter" and first["role"] == "hunter"
        assert 0 < first["freeze_s"] <= LiveConfig().freeze_s
        ws.send_json({"type": "use"})  # ignored during the countdown
        assert ws.receive_json()["t"] == first["t"]  # the clock waits too
    with client.websocket_connect("/ws?role=nonsense") as ws:
        assert ws.receive_json()["match"]["role"] in ("dodger", "hunter")  # picked at random


def test_vehicles_report_their_heading(world: World) -> None:
    trip = m1_east_at_eight(world)
    standing = world.trip_state(trip, EIGHT)
    moving = world.trip_state(trip, EIGHT + 60)
    assert standing is not None and moving is not None
    assert standing.heading == pytest.approx(0.0, abs=0.05)  # M1 runs (nearly) due east
    assert moving.heading == pytest.approx(0.0, abs=0.05)
    game = solo_game(world, "West")
    row = next(v for v in game.snapshot()["vehicles"] if v[0] == trip)
    assert len(row) == 7 and row[5] == pytest.approx(standing.heading, abs=1e-3)
    assert row[6] == 1.0  # fully shown


def test_vehicles_drive_in_before_their_first_stop_and_away_after_their_last(
    world: World,
) -> None:
    # Instead of popping up at a stop: shown (but not boardable) a little before and after.
    trip = m1_east_at_eight(world)
    arr, _ = world.times(trip)
    lead = world.SHOW_LEAD_S

    def shown(t: float) -> tuple[float, float] | None:
        found = [(s.pos[0], v) for s, v in world.shown_trips(t) if s.trip == trip]
        return found[0] if found else None

    first = world.stop_point(world.trip_pattern[trip], 0)
    assert shown(arr[0] - lead - 1) is None
    x, vis = shown(arr[0] - lead / 2) or (0.0, 0.0)
    assert vis == pytest.approx(0.5) and x < first[0]  # coming from the west, eastbound
    assert world.trip_state(trip, arr[0] - lead / 2) is None  # nobody can board it yet
    assert shown(arr[0]) == (first[0], 1.0)
    last = world.stop_point(world.trip_pattern[trip], len(arr) - 1)
    x, vis = shown(arr[-1] + lead / 4) or (0.0, 0.0)
    assert vis == pytest.approx(0.75) and x > last[0]  # driving on east
    assert shown(arr[-1] + lead + 1) is None


def test_asking_to_get_off_can_be_cancelled(world: World) -> None:
    game = solo_game(world, "West")
    game.wait_for(0)
    game.step(30)
    game.use()  # get off at Central
    assert game.snapshot()["ride"]["alight_at"] == "Central"
    game.use()  # no, stay on
    assert game.snapshot()["ride"]["alight_at"] is None
    game.step(220)  # past Central, on to the end of the line at East
    assert game.dodger.ride is None
    assert game.dodger.pos == world.node_xy[node(world, "East")]


def test_bounty_hunters_outnumber_the_dodger_by_two() -> None:
    assert LiveConfig().n_hunters == 3
    assert LiveConfig(hunter_extra=4).n_hunters == 5


def test_a_district_drawn_bigger_downloads_its_map_again(tmp_path: Path) -> None:
    # The bug: the marker only held layer versions, so a resized district still counted as
    # downloaded and its first round stalled on Overpass.
    from fare_dodger.cli import DistrictLoader
    from fare_dodger.live.districts import DistrictSpec
    from fare_dodger.workspace import Workspace

    name, net = live_districts(LIVE_TOML)[-1]
    assert net.area.kind == "circle"
    loader = DistrictLoader(
        Workspace.at(tmp_path), [DistrictSpec(name, net)], LiveConfig(), osm=True
    )
    marker = loader._marker(0)
    marker.parent.mkdir(parents=True)
    marker.write_text(json.dumps(loader.versions), encoding="utf-8")  # an old marker
    assert not loader.cached(0)
    marker.write_text(json.dumps(loader._wanted(0)), encoding="utf-8")
    assert loader.cached(0)
    area = net.area.model_copy(update={"radius_m": net.area.radius_m * 1.5})
    bigger = DistrictSpec(name, net.model_copy(update={"area": area}))
    assert not DistrictLoader(loader.workspace, [bigger], LiveConfig(), osm=True).cached(0)


def test_vehicles_show_where_they_really_go(
    timetable: Timetable, fixture_network_config: NetworkConfig
) -> None:
    # The headsign was the last stop inside the district; the feed's trip_headsign says
    # where the vehicle really goes (the mini feed has none: the last stop stands in).
    plain = World(timetable, fixture_network_config.area)
    trip = m1_east_at_eight(plain)
    assert plain.headsign(trip) == "East"
    signs = [""] * timetable.n_trips
    signs[trip] = "Airport"
    world = World(dataclasses.replace(timetable, trip_headsign=signs), plain.area)
    assert world.headsign(trip) == "Airport"
    west = node(world, "West")
    assert any(d.headsign == "Airport" for d in world.departures(west, EIGHT, 60, 5))
