from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from fare_dodger.config import LiveConfig, NetworkConfig
from fare_dodger.live.game import LiveGame, Role, Seat, Status
from fare_dodger.live.match import District, Match, Team, match_winner
from fare_dodger.live.server import Limits, create_app
from fare_dodger.live.world import World
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
    assert view["score"] == {"you": 1, "them": 1} and view["target"] == 11
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


def test_idle_rooms_close_and_health(world: World) -> None:
    with app(world, idle_s=0.3) as client:
        with client.websocket_connect("/ws") as ws:
            ws.send_json({"type": "solo"})
            closed = until(ws, "closed")
            assert "without anyone playing" in closed["message"]
            with pytest.raises(WebSocketDisconnect):
                while True:
                    ws.receive_json()
        assert client.get("/healthz").json() == {"ok": True, "rooms": 0, "players": 0}
