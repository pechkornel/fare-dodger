from __future__ import annotations

import threading
import time
from typing import Any

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from fare_dodger.config import LiveConfig, NetworkConfig
from fare_dodger.live.districts import DistrictPool
from fare_dodger.live.game import LiveGame, Role, Seat, Status
from fare_dodger.live.match import District, Match, Team, match_winner, role_for
from fare_dodger.live.server import Limits, create_app
from fare_dodger.live.world import World, distance
from fare_dodger.network.timetable import Timetable


@pytest.fixture(scope="module")
def world(timetable: Timetable, fixture_network_config: NetworkConfig) -> World:
    return World(timetable, fixture_network_config.area)


def two_v_two(world: World) -> LiveGame:
    """Ann and a bot run; Bob, a bot and the two extra bots hunt."""
    return LiveGame(
        world,
        LiveConfig(inspected_share=0.0),
        seed=4,
        runners=[Seat("ann", "Ann"), Seat(None, "Dodger bot")],
        hunters=[Seat("bob", "Bob"), Seat(None, "Hunter bot")],
    )


def test_teams_and_the_two_extra_hunters(world: World) -> None:
    game = two_v_two(world)
    assert len(game.runners) == 2
    assert game.runners[0].player == "ann" and game.runners[1].bot is not None
    assert len(game.hunters.agents) == 1 + 2  # the bot seat plus the two extra bots
    assert {pid: h.role for pid, h in game.humans.items()} == {
        "ann": Role.DODGER,
        "bob": Role.HUNTER,
    }


def test_each_dodger_is_a_point_and_a_round_can_end_one_all(world: World) -> None:
    game = two_v_two(world)
    game.hunters.agents = []
    bob = game.humans["bob"].traveller
    bob.pos = game.runners[0].traveller.pos  # Bob stands right on Ann
    game.runners[1].bot = None  # the other Dodger stays put, far away
    game.step(1)
    assert game.runners[0].caught and not game.runners[1].caught
    assert game.status is Status.RUNNING  # one Dodger is still free
    events = [e.text for e in game.drain_events("bob")]
    assert any("You caught Ann" in t for t in events)
    game.step(10_000)
    status: Status = game.status  # re-read: mypy keeps the narrowing from before step()
    assert status is Status.ESCAPED and game.result() == (1, 1)
    assert game.won("ann") is None and game.won("bob") is None  # 1-1


def test_catching_everyone_ends_the_round_early(world: World) -> None:
    game = two_v_two(world)
    game.hunters.agents = []
    for runner in game.runners:
        runner.bot = None
    game.humans["bob"].traveller.pos = game.runners[0].traveller.pos
    game.step(1)
    game.humans["bob"].traveller.pos = game.runners[1].traveller.pos
    game.step(1)
    assert game.status is Status.CAUGHT and game.result() == (0, 2)
    assert game.won("bob") is True and game.won("ann") is False


def test_views_are_per_player(world: World) -> None:
    game = two_v_two(world)
    ann, bob = game.snapshot("ann"), game.snapshot("bob")
    assert ann["role"] == "dodger" and bob["role"] == "hunter"
    # The Dodgers always see every Bounty Hunter, Bob included.
    assert len(ann["hunters"]) == 3 + 1
    assert [d["name"] for d in ann["dodgers"]] == ["Dodger bot"]  # the teammate
    # Bob sees only the Dodgers in sight (none at the start) and his three bot teammates.
    assert bob["dodgers"] == [] and len(bob["hunters"]) == 3
    assert bob["wallet"] is None and ann["wallet"] is not None


def test_a_player_who_leaves_is_replaced_by_a_bot(world: World) -> None:
    game = two_v_two(world)
    game.make_bot("ann")
    assert game.runners[0].bot is not None and game.runners[0].player is None


def test_a_bounty_hunter_who_leaves_becomes_a_bot_not_a_statue(world: World) -> None:
    game = two_v_two(world)
    bob = game.humans["bob"].traveller
    x, y = bob.pos
    bob.pos = (x + 150.0, y)  # off the network, in a street
    game.make_bot("bob")
    assert "bob" not in game.humans
    assert len(game.hunters.agents) == 3 + 1  # one more bot where Bob stood
    new = game.hunters.agents[-1]
    assert new.pos == bob.pos
    assert len(game.hunter_states()) == 4  # and no frozen human left behind
    game.step(20)
    assert distance(new.pos, (x, y)) < 150.0 - 20  # walking back to the network


def test_a_player_who_left_is_a_bot_in_the_next_rounds(world: World) -> None:
    teams = [Team("A", [("ann", "Ann")]), Team("B", [("bob", "Bob")])]
    match = Match([District("X", world)], LiveConfig(), seed=1, teams=teams, start=Role.DODGER)
    match.drop_player("bob")
    match.game.status = Status.ESCAPED
    assert match.next_round()
    assert set(match.game.humans) == {"ann"}  # Bob's seat went to a bot
    assert len(match.game.runners) + len(match.game.hunters.agents) == 1 + 1 + 2


def test_waiting_for_the_departure_on_screen(world: World) -> None:
    game = LiveGame(world, LiveConfig(inspected_share=0.0), seed=3)
    game.hunters.agents = []
    you = game.player
    stop = max(world.boarding_nodes, key=lambda n: len(game.world.departures(n, game.t, 900, 6)))
    you.pos = world.node_xy[stop]
    shown = game.departures()
    first = shown[0]
    row, second = next((i, d) for i, d in enumerate(shown) if d.time > first.time)
    game.t = first.time + 1  # the first one leaves before the key press arrives
    game.wait_for(row, trip=second.trip)  # the row as it was on screen
    assert you.waiting_for is not None and you.waiting_for.trip == second.trip
    you.waiting_for = None
    game.wait_for(0, trip=first.trip)
    assert you.waiting_for is None
    assert any("has left" in e.text for e in game.drain_events())


def test_match_points_and_target() -> None:
    assert match_winner(11, 9, 10, 10, size=2) is True
    assert match_winner(10, 10, 10, 10, size=2) is None  # overtime
    assert match_winner(12, 10, 11, 10, size=2) is True


def test_a_two_v_two_match_scores_points(world: World) -> None:
    teams = [Team("A", [("ann", "Ann")]), Team("B", [("bob", "Bob")])]
    match = Match(
        [District("X", world)], LiveConfig(), seed=1, teams=teams, size=2, start=Role.DODGER
    )
    game = match.game
    assert game.humans["ann"].role is Role.DODGER and game.humans["bob"].role is Role.HUNTER
    game.runners[0].caught = True
    game.status = Status.ESCAPED
    match.update()
    assert match.score == (1, 1)
    view = match.view("bob")
    assert view["score"] == {"you": 1, "them": 1} and view["target"] == 7  # 6 rounds x 2 points
    assert view["role"] == "hunter" and view["size"] == 2


# --- the room server -----------------------------------------------------------------------
# The client is used as a context manager, so every WebSocket shares one event loop (as under
# uvicorn); otherwise each connection gets its own loop and rooms cannot reach other sockets.


def app(world: World, **limits: Any) -> TestClient:
    return TestClient(
        create_app([District("Fixture", world)], LiveConfig(seed=5), Limits(**limits))
    )


def until(ws: Any, kind: str) -> dict[str, Any]:
    while True:
        msg: dict[str, Any] = ws.receive_json()
        if msg["type"] == kind:
            return msg


def test_create_join_and_start_a_two_v_two(world: World) -> None:
    with app(world, max_per_ip=5) as client, client.websocket_connect("/ws") as host:
        host.send_json({"type": "create", "size": 2, "name": "Ann"})
        lobby = until(host, "lobby")
        assert lobby["host"] and lobby["size"] == 2
        assert lobby["teams"][lobby["team"]]["players"] == ["Ann"]
        with client.websocket_connect("/ws") as guest:
            guest.send_json({"type": "join", "code": lobby["code"].lower(), "name": "Bob"})
            joined = until(guest, "lobby")
            assert not joined["host"] and joined["team"] != lobby["team"]  # the smaller team
            guest.send_json({"type": "start"})  # only the host can start
            host.send_json({"type": "start"})
            ann = until(host, "snapshot")
            bob = until(guest, "snapshot")
            assert {ann["role"], bob["role"]} == {"dodger", "hunter"}
            assert ann["match"]["size"] == 2 and ann["room"]["host"]
            assert ann["runners"]["total"] == 2


def test_join_errors(world: World) -> None:
    with app(world, max_per_ip=5) as client:
        with client.websocket_connect("/ws") as ws:
            ws.send_json({"type": "join", "code": "ZZZZ", "name": "Bob"})
            assert "No game" in ws.receive_json()["message"]
        with client.websocket_connect("/ws") as host:
            host.send_json({"type": "create", "size": 1, "name": "Ann"})
            code = until(host, "lobby")["code"]
            with client.websocket_connect("/ws") as one:
                one.send_json({"type": "join", "code": code, "name": "Bob"})
                until(one, "lobby")
                with client.websocket_connect("/ws") as two:
                    two.send_json({"type": "join", "code": code, "name": "Cy"})
                    assert "full" in two.receive_json()["message"]


def test_caps_on_matches_and_per_address(world: World) -> None:
    with (
        app(world, max_matches=1, max_per_ip=5) as client,
        client.websocket_connect("/ws") as first,
    ):
        first.send_json({"type": "solo"})
        until(first, "snapshot")
        with client.websocket_connect("/ws") as second:
            second.send_json({"type": "solo"})
            assert "full" in second.receive_json()["message"]
    with app(world, max_per_ip=1) as client:
        headers = {"cf-connecting-ip": "203.0.113.7"}
        with client.websocket_connect("/ws", headers=headers) as first:
            first.send_json({"type": "solo"})
            until(first, "snapshot")
            with client.websocket_connect("/ws", headers=headers) as second:
                second.send_json({"type": "solo"})
                assert "already in 1 games" in second.receive_json()["message"]
            elsewhere = {"cf-connecting-ip": "198.51.100.1"}
            with client.websocket_connect("/ws", headers=elsewhere) as other:
                other.send_json({"type": "solo"})
                assert until(other, "snapshot")["type"] == "snapshot"


def test_no_joining_while_the_match_loads(world: World) -> None:
    # The bug: a player joining while the first district loaded was left out of the teams
    # and stuck in the lobby for the whole match.
    gate = threading.Event()

    def slow(_: int) -> World:
        gate.wait(10)
        return world

    pool = DistrictPool(["Slow"], slow)
    client = TestClient(create_app(pool, LiveConfig(seed=5), Limits(max_per_ip=5)))
    with client, client.websocket_connect("/ws") as host:
        host.send_json({"type": "create", "size": 2, "name": "Ann"})
        code = until(host, "lobby")["code"]
        host.send_json({"type": "start"})
        while not until(host, "lobby")["starting"]:
            pass
        with client.websocket_connect("/ws") as late:
            late.send_json({"type": "join", "code": code, "name": "Bob"})
            assert "already started" in late.receive_json()["message"]
        gate.set()
        assert until(host, "snapshot")["role"] in ("dodger", "hunter")


def test_idle_rooms_close_and_health(world: World) -> None:
    with app(world, idle_s=0.3) as client:
        with client.websocket_connect("/ws") as ws:
            ws.send_json({"type": "solo"})
            # Standing still is playing: a match gets five times as long (1.5 s here).
            started = time.monotonic()
            while time.monotonic() - started < 0.6:
                assert ws.receive_json()["type"] == "snapshot"
            closed = until(ws, "closed")
            assert "without anyone playing" in closed["message"]
            with pytest.raises(WebSocketDisconnect):
                while True:
                    ws.receive_json()
        assert client.get("/healthz").json() == {"ok": True, "rooms": 0, "players": 0}


def test_rooms_pick_a_quick_or_a_long_match(world: World) -> None:
    client = TestClient(create_app([District("X", world)], LiveConfig(seed=1)))
    for rounds, target in ((3, 2), (6, 4), (10, 4), (None, 4)):  # others: the long match
        with client.websocket_connect("/ws") as ws:
            ws.send_json({"type": "solo", "role": "dodger", "rounds": rounds})
            first = ws.receive_json()
            assert first["match"]["regulation"] == (rounds if rounds in (3, 6) else 6)
            assert first["match"]["target"] == target
        with client.websocket_connect("/ws") as ws:
            ws.send_json({"type": "create", "size": 2, "rounds": rounds})
            lobby = ws.receive_json()
            assert lobby["rounds"] == (rounds if rounds in (3, 6) else 6)


def test_best_of_three() -> None:
    # One round each way, then the decider; 1-1 after three in a 2v2 goes to overtime.
    assert [role_for(n, 3, Role.DODGER) for n in (1, 2, 3, 4)] == [
        Role.DODGER,
        Role.HUNTER,
        Role.HUNTER,
        Role.DODGER,
    ]
    assert match_winner(2, 0, 2, 3) is True
    assert match_winner(1, 1, 2, 3) is None
    assert match_winner(3, 3, 3, 3, size=2) is None
