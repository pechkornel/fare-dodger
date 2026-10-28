"""HTTP + WebSocket server for the live game: rooms of human players, filled up with bots.

The server owns the game state. A player opens ``/ws`` and sends one of::

    {"type": "create", "size": 1 | 2 | 3, "name": str}   # a new room (1v1, 2v2 or 3v3)
    {"type": "join", "code": str, "name": str}           # an open room, by its code
    {"type": "solo", "role": "dodger" | "hunter" | null}  # a room of one, started at once

(``/ws?role=dodger`` is a shortcut for ``solo``.) In the lobby players join the smaller team;
any player may ``{"type": "team"}`` to switch if there is room, and the host sends
``{"type": "start"}`` when ready: empty places are played by bots, and a random team runs
first. During a match each player sends key state and actions::

    {"type": "keys", "up": bool, "down": bool, "left": bool, "right": bool}
    {"type": "use"}                 # board / get off / stay on / stop waiting
    {"type": "wait", "index": int}  # wait for a listed departure
    {"type": "ticket"}              # buy a ticket at the current stop
    {"type": "coffee"}              # buy a coffee at the current café
    {"type": "next"}                # host: start the next round now
    {"type": "restart"}             # host: back to the lobby once the match is over

The room sends every player ``{"type": "lobby", ...}`` while waiting, and ~20 times a second
``{"type": "snapshot", ...}`` (their own view of the round, plus ``"match"`` and ``"room"``)
while playing. Errors arrive as ``{"type": "error", "message": str}``.

Safety caps (environment variables, for a public server): at most ``FARE_DODGER_MAX_MATCHES``
rooms at once (20), at most ``FARE_DODGER_MAX_MATCHES_PER_IP`` rooms per client address (2;
behind Cloudflare the ``CF-Connecting-IP`` header), and a room closes when its last player
leaves or after ``FARE_DODGER_IDLE_S`` seconds (120) without input from anyone.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import random
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles

from fare_dodger.config import LiveConfig
from fare_dodger.live.game import Keys, LiveGame, Role, world_view
from fare_dodger.live.match import District, Match, Team

STATIC_DIR = Path(__file__).parent / "static"
TICK_S = 0.05
#: Longest real-time gap simulated in one tick (avoids jumps after a stall).
MAX_TICK_S = 0.25
#: Real seconds between the end of a round and the next one (the host can skip it).
ROUND_BREAK_S = 8.0
#: Seconds a new connection has to say what it wants.
HELLO_TIMEOUT_S = 30.0
#: A snapshot that cannot be sent this fast is skipped for that player.
SEND_TIMEOUT_S = 0.5
SIZES = (1, 2, 3)
CODE_LETTERS = "ABCDEFGHJKLMNPQRSTUVWXYZ"  # no I or O
NAME_MAX = 16


@dataclass(frozen=True)
class Limits:
    max_matches: int = 20
    max_per_ip: int = 2
    idle_s: float = 120.0

    @classmethod
    def from_env(cls) -> Limits:
        env = os.environ
        return cls(
            max_matches=int(env.get("FARE_DODGER_MAX_MATCHES", cls.max_matches)),
            max_per_ip=int(env.get("FARE_DODGER_MAX_MATCHES_PER_IP", cls.max_per_ip)),
            idle_s=float(env.get("FARE_DODGER_IDLE_S", cls.idle_s)),
        )


class RefusedError(Exception):
    """A request the server turns down, with a message for the player."""


@dataclass
class Member:
    pid: str
    name: str
    ws: WebSocket
    ip: str
    team: int


@dataclass
class Room:
    code: str
    size: int
    host: str
    members: dict[str, Member] = field(default_factory=dict)
    match: Match | None = None
    last_input: float = field(default_factory=time.monotonic)
    task: asyncio.Task[None] | None = None
    lobby_dirty: bool = True
    #: Each round starts with a countdown (real seconds) while nobody can move.
    frozen_game: LiveGame | None = None
    frozen_until: float = 0.0
    round_over_at: float | None = None

    def frozen(self) -> bool:
        return time.monotonic() < self.frozen_until

    def team_members(self, team: int) -> list[Member]:
        return [m for m in self.members.values() if m.team == team]

    def lobby(self, pid: str) -> dict[str, Any]:
        teams = [
            {
                "players": [m.name for m in self.team_members(t)],
                "bots": self.size - len(self.team_members(t)),
            }
            for t in (0, 1)
        ]
        return {
            "type": "lobby",
            "code": self.code,
            "size": self.size,
            "you": pid,
            "team": self.members[pid].team,
            "host": self.host == pid,
            "host_name": self.members[self.host].name if self.host in self.members else "",
            "teams": teams,
        }


def clean_name(value: object, fallback: str) -> str:
    name = " ".join(str(value or "").split())[:NAME_MAX]
    return name or fallback


def create_app(districts: list[District], cfg: LiveConfig, limits: Limits | None = None) -> FastAPI:
    app = FastAPI(title="Fare Dodger live", docs_url=None, redoc_url=None)
    views = [{**world_view(d.world, cfg), "name": d.name} for d in districts]
    caps = limits or Limits.from_env()
    rooms: dict[str, Room] = {}

    @app.get("/api/world/{index}")
    def get_world(index: int) -> dict[str, Any]:
        if not 0 <= index < len(views):
            raise HTTPException(status_code=404, detail="no such district")
        return views[index]

    @app.get("/healthz")
    def health() -> dict[str, Any]:
        return {
            "ok": True,
            "rooms": len(rooms),
            "players": sum(len(r.members) for r in rooms.values()),
        }

    # --- rooms -----------------------------------------------------------------------------

    def rooms_of_ip(ip: str) -> int:
        return sum(any(m.ip == ip for m in r.members.values()) for r in rooms.values())

    def check_ip(ip: str) -> None:
        if rooms_of_ip(ip) >= caps.max_per_ip:
            msg = f"You are already in {caps.max_per_ip} games from this connection."
            raise RefusedError(msg)

    def new_room(size: int, member: Member) -> Room:
        if len(rooms) >= caps.max_matches:
            msg = "The server is full right now. Try again in a few minutes."
            raise RefusedError(msg)
        check_ip(member.ip)
        code = "".join(secrets.choice(CODE_LETTERS) for _ in range(4))
        while code in rooms:
            code = "".join(secrets.choice(CODE_LETTERS) for _ in range(4))
        room = Room(code, size, member.pid)
        room.members[member.pid] = member
        rooms[code] = room
        room.task = asyncio.create_task(run_room(room))
        return room

    def join_room(code: str, member: Member) -> Room:
        room = rooms.get(code.strip().upper())
        if room is None:
            msg = f"No game with code {code.strip().upper() or '?'}."
            raise RefusedError(msg)
        if room.match is not None:
            msg = "That game has already started."
            raise RefusedError(msg)
        counts = [len(room.team_members(t)) for t in (0, 1)]
        if min(counts) >= room.size:
            msg = "That game is full."
            raise RefusedError(msg)
        check_ip(member.ip)
        if counts[0] == counts[1]:
            member.team = random.randrange(2)
        else:
            member.team = 0 if counts[0] < counts[1] else 1
        room.members[member.pid] = member
        room.lobby_dirty = True
        return room

    def start_match(room: Room, start: Role | None = None) -> None:
        teams = []
        for t in (0, 1):
            players = [(m.pid, m.name) for m in room.team_members(t)]
            default = "Bots" if not players else f"Team {'AB'[t]}"
            teams.append(Team(default if len(players) != 1 else players[0][1], players))
        seed = cfg.seed if cfg.seed is not None else random.randrange(2**31)
        first = start or random.choice((Role.DODGER, Role.HUNTER))
        room.match = Match(districts, cfg, seed, teams=teams, size=room.size, start=first)
        room.round_over_at = None

    def leave(room: Room, pid: str) -> None:
        room.members.pop(pid, None)
        if room.match is not None and pid in room.match.game.humans:
            room.match.game.make_bot(pid)
        if room.host == pid and room.members:
            room.host = next(iter(room.members))
        room.lobby_dirty = True
        if not room.members:
            close(room)

    def close(room: Room) -> None:
        rooms.pop(room.code, None)
        if room.task is not None and room.task is not asyncio.current_task():
            room.task.cancel()

    async def send(member: Member, message: dict[str, Any]) -> None:
        with contextlib.suppress(Exception):
            await asyncio.wait_for(member.ws.send_json(message), SEND_TIMEOUT_S)

    async def run_room(room: Room) -> None:
        last = time.monotonic()
        try:
            while room.code in rooms:
                now = time.monotonic()
                if now - room.last_input > caps.idle_s:
                    for m in list(room.members.values()):
                        await send(
                            m,
                            {
                                "type": "closed",
                                "message": "Closed after two minutes without anyone playing.",
                            },
                        )
                        with contextlib.suppress(Exception):
                            await m.ws.close()
                    close(room)
                    return
                match = room.match
                if match is None:
                    if room.lobby_dirty:
                        room.lobby_dirty = False
                        await asyncio.gather(
                            *(send(m, room.lobby(m.pid)) for m in room.members.values())
                        )
                else:
                    step_match(room, match, now, min(now - last, MAX_TICK_S))
                    await asyncio.gather(
                        *(
                            send(m, snapshot(room, match, m.pid, now))
                            for m in list(room.members.values())
                            if m.pid in match.game.humans
                        )
                    )
                last = now
                await asyncio.sleep(TICK_S)
        except asyncio.CancelledError:
            pass

    def step_match(room: Room, match: Match, now: float, dt: float) -> None:
        if room.frozen_game is not match.game:
            room.frozen_game, room.frozen_until = match.game, now + cfg.freeze_s
            room.round_over_at = None
        if not room.frozen():
            match.game.step(dt * cfg.time_scale)
        match.update()
        if match.round_recorded and match.winner is None:
            if room.round_over_at is None:
                room.round_over_at = now
            elif now - room.round_over_at >= ROUND_BREAK_S:
                match.next_round()

    def snapshot(room: Room, match: Match, pid: str, now: float) -> dict[str, Any]:
        next_in = None
        if room.round_over_at is not None and match.winner is None:
            next_in = round(max(0.0, ROUND_BREAK_S - (now - room.round_over_at)), 1)
        return {
            "type": "snapshot",
            **match.game.snapshot(pid),
            "match": match.view(pid),
            "freeze_s": round(max(0.0, room.frozen_until - now), 2),
            "room": {
                "code": room.code,
                "host": room.host == pid,
                "next_round_in": next_in,
                "players": len(room.members),
            },
        }

    def handle(room: Room, pid: str, msg: dict[str, Any]) -> None:
        kind = msg.get("type")
        room.last_input = time.monotonic()
        match = room.match
        host = room.host == pid
        if match is None:
            if kind == "team":
                member = room.members[pid]
                other = 1 - member.team
                if len(room.team_members(other)) < room.size:
                    member.team = other
                    room.lobby_dirty = True
            elif kind == "start" and host:
                start_match(room)
            return
        if kind == "restart" and host and match.winner is not None:
            room.match = None
            room.lobby_dirty = True
        elif kind == "next" and host:
            match.next_round()
        elif pid in match.game.humans and (kind == "keys" or not room.frozen()):
            apply_message(match.game, msg, pid)

    @app.websocket("/ws")
    async def play(ws: WebSocket) -> None:
        await ws.accept()
        ip = ws.headers.get("cf-connecting-ip") or (ws.client.host if ws.client else "?")
        pid = secrets.token_hex(6)
        role = ws.query_params.get("role")
        try:
            hello: Any = (
                {"type": "solo", "role": role}
                if role
                else await asyncio.wait_for(ws.receive_json(), HELLO_TIMEOUT_S)
            )
            if not isinstance(hello, dict):
                hello = {}
            kind = hello.get("type")
            member = Member(pid, clean_name(hello.get("name"), "Player"), ws, ip, 0)
            if kind == "create":
                size = hello.get("size")
                room = new_room(size if size in SIZES else 1, member)
            elif kind == "join":
                room = join_room(str(hello.get("code", "")), member)
            elif kind == "solo":
                member.name = clean_name(hello.get("name"), "You")
                room = new_room(1, member)
                wanted = hello.get("role")
                start_match(room, Role(wanted) if wanted in ("dodger", "hunter") else None)
            else:
                msg = "Say create, join or solo first."
                raise RefusedError(msg)
        except RefusedError as exc:
            await ws.send_json({"type": "error", "message": str(exc)})
            await ws.close()
            return
        except (WebSocketDisconnect, TimeoutError, ValueError):
            with contextlib.suppress(Exception):
                await ws.close()
            return
        try:
            while True:
                msg = await ws.receive_json()
                if isinstance(msg, dict) and room.code in rooms:
                    handle(room, pid, msg)
        except (WebSocketDisconnect, RuntimeError, ValueError):
            pass
        finally:
            leave(room, pid)

    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
    return app


def apply_message(game: LiveGame, msg: dict[str, Any], pid: str | None = None) -> None:
    kind = msg.get("type")
    if kind == "keys":
        game.set_keys(
            Keys(
                up=bool(msg.get("up")),
                down=bool(msg.get("down")),
                left=bool(msg.get("left")),
                right=bool(msg.get("right")),
            ),
            pid,
        )
    elif kind == "use":
        game.use(pid)
    elif kind == "ticket":
        game.buy_ticket(pid)
    elif kind == "coffee":
        game.buy_coffee(pid)
    elif kind == "wait":
        index = msg.get("index")
        if isinstance(index, int):
            game.wait_for(index, pid)
