"""HTTP + WebSocket server for the live game: rooms of human players, filled up with bots.

The server owns the game state. A player opens ``/ws`` and sends one of::

    {"type": "create", "size": 1 | 2 | 3, "rounds": 3 | 6, "name": str}  # 1v1, 2v2 or 3v3
    {"type": "join", "code": str, "name": str}           # an open room, by its code
    {"type": "solo", "role": "dodger" | "hunter" | null, "rounds": 3 | 6}  # started at once

``rounds`` is the match length: best of 3 (quick) or best of 6 (long; the default).

(``/ws?role=dodger`` is a shortcut for ``solo``.) In the lobby players join the smaller team;
any player may ``{"type": "team"}`` to switch if there is room, and the host sends
``{"type": "start"}`` when ready: empty places are played by bots, and a random team runs
first. During a match each player sends key state and actions::

    {"type": "keys", "up": bool, "down": bool, "left": bool, "right": bool}
    {"type": "use"}                 # board / get off / stay on / stop waiting
    {"type": "wait", "index": int, "trip": int}  # wait for a listed departure (by trip)
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
leaves or after ``FARE_DODGER_IDLE_S`` seconds (120) without input from anyone; five times
that while a match is being played, since standing still is part of the game.
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

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from fare_dodger.config import LiveConfig
from fare_dodger.live.admin import Admin, memory_mib
from fare_dodger.live.districts import DistrictPool
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
#: During a match, a room closes after this many times ``idle_s`` without input.
IN_MATCH_IDLE_FACTOR = 5
SIZES = (1, 2, 3)
#: Match lengths a room can pick (regulation rounds): quick and long.
ROUNDS = (3, 6)
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
    #: Regulation rounds of the room's matches.
    rounds: int = ROUNDS[-1]
    members: dict[str, Member] = field(default_factory=dict)
    match: Match | None = None
    last_input: float = field(default_factory=time.monotonic)
    task: asyncio.Task[None] | None = None
    lobby_dirty: bool = True
    #: The first district is loading (in the background) for a match about to start.
    starting: bool = False
    #: Background work (district loading) kept alive while it runs.
    tasks: set[asyncio.Task[Any]] = field(default_factory=set)
    #: Each round starts with a countdown (real seconds) while nobody can move.
    frozen_game: LiveGame | None = None
    frozen_until: float = 0.0
    round_over_at: float | None = None
    #: Set to close the room (with this message for its players), e.g. by the admin.
    close_reason: str | None = None
    created: float = field(default_factory=time.time)

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
            "rounds": self.rounds,
            "you": pid,
            "team": self.members[pid].team,
            "host": self.host == pid,
            "host_name": self.members[self.host].name if self.host in self.members else "",
            "teams": teams,
            "starting": self.starting,
        }


def minutes(seconds: float) -> str:
    """``"2 minutes"`` (or seconds, for short test limits)."""
    return f"{round(seconds / 60)} minutes" if seconds >= 90 else f"{round(seconds)} seconds"


def clean_name(value: object, fallback: str) -> str:
    name = " ".join(str(value or "").split())[:NAME_MAX]
    return name or fallback


def create_app(
    districts: list[District] | DistrictPool,
    cfg: LiveConfig,
    limits: Limits | None = None,
    admin: Admin | None = None,
) -> FastAPI:
    app = FastAPI(title="Fare Dodger live", docs_url=None, redoc_url=None)
    pool = districts if isinstance(districts, DistrictPool) else DistrictPool.of(districts)
    views: dict[int, dict[str, Any]] = {}
    caps = limits or Limits.from_env()
    rooms: dict[str, Room] = {}

    @app.get("/api/world/{index}")
    async def get_world(index: int) -> dict[str, Any]:
        if not 0 <= index < len(pool):
            raise HTTPException(status_code=404, detail="no such district")
        if index not in views:
            world = await pool.prepare(index)
            views[index] = {**world_view(world, cfg), "name": pool.name(index)}
        return views[index]

    @app.get("/api/backdrop")
    def backdrop() -> dict[str, int]:
        """The district behind the menu: one in memory, else a downloaded one (never one that
        would have to be downloaded first)."""
        playable = pool.playable()
        loaded = [i for i in pool.loaded() if i in playable]
        return {"district": loaded[-1] if loaded else playable[0]}

    @app.get("/healthz")
    def health() -> dict[str, Any]:
        return {
            "ok": True,
            "rooms": len(rooms),
            "players": sum(len(r.members) for r in rooms.values()),
        }

    # --- admin -------------------------------------------------------------------------------

    def admin_or_401(request: Request) -> Admin:
        if admin is None:
            raise HTTPException(404, "The admin page is off: set FARE_DODGER_ADMIN_TOKEN.")
        if not admin.allows(request.headers.get("authorization")):
            raise HTTPException(401, "Wrong admin password.")
        return admin

    def room_view(room: Room) -> dict[str, Any]:
        match = room.match
        view: dict[str, Any] = {
            "code": room.code,
            "size": room.size,
            "host": room.members[room.host].name if room.host in room.members else "",
            "players": [{"name": m.name, "team": "AB"[m.team]} for m in room.members.values()],
            "state": "lobby" if match is None else "playing",
            "idle_s": round(time.monotonic() - room.last_input),
            "age_s": round(time.time() - room.created),
        }
        if match is not None:
            a, b = match.score
            view.update(
                round=match.round_number,
                score=[a, b],
                district=match.districts.name(match.district),
                over=match.winner is not None,
            )
        return view

    @app.get("/admin", include_in_schema=False)
    def admin_page() -> RedirectResponse:
        return RedirectResponse("/admin.html")

    @app.get("/api/admin/status")
    def admin_status(request: Request) -> dict[str, Any]:
        a = admin_or_401(request)
        return {
            "info": a.info,
            "uptime_s": round(time.time() - a.started),
            "memory_mib": memory_mib(),
            "limits": {
                "max_matches": caps.max_matches,
                "max_per_ip": caps.max_per_ip,
                "idle_s": caps.idle_s,
            },
            "players": sum(len(r.members) for r in rooms.values()),
            "districts": {
                "loaded": [pool.name(i) for i in pool.loaded()],
                "playable": len(pool.playable()),
                "total": len(pool),
            },
            "rooms": [room_view(r) for r in rooms.values()],
            "update": a.update_state(),
        }

    @app.post("/api/admin/update")
    def admin_update(request: Request) -> dict[str, Any]:
        a = admin_or_401(request)
        a.request_update()
        return {"ok": True, "update": a.update_state()}

    @app.post("/api/admin/rooms/{code}/close")
    def admin_close(code: str, request: Request) -> dict[str, Any]:
        admin_or_401(request)
        room = rooms.get(code.upper())
        if room is None:
            raise HTTPException(404, "No such room.")
        room.close_reason = "The admin closed this game."
        return {"ok": True}

    # --- rooms -----------------------------------------------------------------------------

    def rooms_of_ip(ip: str) -> int:
        return sum(any(m.ip == ip for m in r.members.values()) for r in rooms.values())

    def check_ip(ip: str) -> None:
        if rooms_of_ip(ip) >= caps.max_per_ip:
            msg = f"You are already in {caps.max_per_ip} games from this connection."
            raise RefusedError(msg)

    def new_room(size: int, member: Member, rounds: object = None) -> Room:
        if len(rooms) >= caps.max_matches:
            msg = "The server is full right now. Try again in a few minutes."
            raise RefusedError(msg)
        check_ip(member.ip)
        code = "".join(secrets.choice(CODE_LETTERS) for _ in range(4))
        while code in rooms:
            code = "".join(secrets.choice(CODE_LETTERS) for _ in range(4))
        room = Room(code, size, member.pid, rounds if rounds in ROUNDS else ROUNDS[-1])
        room.members[member.pid] = member
        rooms[code] = room
        room.task = asyncio.create_task(run_room(room))
        return room

    def join_room(code: str, member: Member) -> Room:
        room = rooms.get(code.strip().upper())
        if room is None:
            msg = f"No game with code {code.strip().upper() or '?'}."
            raise RefusedError(msg)
        # Starting: the teams are set while the first district loads.
        if room.match is not None or room.starting:
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

    def background(room: Room, coro: Any) -> None:
        task = asyncio.ensure_future(coro)
        room.tasks.add(task)
        task.add_done_callback(room.tasks.discard)

    def preload(room: Room, index: int) -> None:
        """Load a district in the background (a no-op when it is ready or loading)."""
        if not pool.ready(index):
            background(room, pool.prepare(index))

    def start_match(room: Room, start: Role | None = None) -> None:
        """Set up a match; it starts once its first district is loaded (in the background)."""
        if room.starting or room.match is not None:
            return
        teams = []
        for t in (0, 1):
            players = [(m.pid, m.name) for m in room.team_members(t)]
            default = "Bots" if not players else f"Team {'AB'[t]}"
            teams.append(Team(default if len(players) != 1 else players[0][1], players))
        seed = cfg.seed if cfg.seed is not None else random.randrange(2**31)
        first = start or random.choice((Role.DODGER, Role.HUNTER))
        match_cfg = cfg.model_copy(update={"regulation_rounds": room.rounds})
        match = Match(pool, match_cfg, seed, teams=teams, size=room.size, start=first, begin=False)

        def begin() -> None:
            match.begin()
            room.match = match
            room.round_over_at = None

        if pool.ready(match.upcoming_district()):
            begin()
            return
        room.starting = True
        room.lobby_dirty = True

        async def load_then_begin() -> None:
            try:
                await pool.prepare(match.upcoming_district())
            finally:
                room.starting = False
                room.lobby_dirty = True
            if room.code in rooms:
                begin()

        background(room, load_then_begin())

    def leave(room: Room, pid: str) -> None:
        room.members.pop(pid, None)
        if room.match is not None:
            room.match.drop_player(pid)  # bots take their seat in the next rounds
            if pid in room.match.game.humans:
                room.match.game.make_bot(pid)  # and in this one
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
                # Hiding still or riding a long way is playing too, so a match in progress
                # gets longer: only an abandoned match goes that long without a key pressed.
                playing = room.match is not None and room.match.winner is None
                idle_s = caps.idle_s * (IN_MATCH_IDLE_FACTOR if playing else 1)
                if room.close_reason is None and now - room.last_input > idle_s:
                    room.close_reason = f"Closed after {minutes(idle_s)} without anyone playing."
                if room.close_reason is not None:
                    for m in list(room.members.values()):
                        await send(m, {"type": "closed", "message": room.close_reason})
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
            # The next round's district loads while this round is played.
            preload(room, match.upcoming_district())
        if not room.frozen():
            match.game.step(dt * cfg.time_scale)
        match.update()
        if match.round_recorded and match.winner is None:
            if room.round_over_at is None:
                room.round_over_at = now
            elif now - room.round_over_at >= ROUND_BREAK_S:
                next_round(room, match)

    def next_round(room: Room, match: Match) -> None:
        """Start the next round, or wait (a little longer break) until its district is in."""
        upcoming = match.upcoming_district()
        if pool.ready(upcoming):
            match.next_round()
        else:
            preload(room, upcoming)

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
                "loading": next_in is not None and not pool.ready(match.upcoming_district()),
            },
        }

    def handle(room: Room, pid: str, msg: dict[str, Any]) -> None:
        kind = msg.get("type")
        room.last_input = time.monotonic()
        match = room.match
        host = room.host == pid
        if match is None:
            if kind == "team" and not room.starting:
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
        elif kind == "next" and host and match.round_recorded:
            next_round(room, match)
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
                room = new_room(size if size in SIZES else 1, member, hello.get("rounds"))
            elif kind == "join":
                room = join_room(str(hello.get("code", "")), member)
            elif kind == "solo":
                member.name = clean_name(hello.get("name"), "You")
                room = new_room(1, member, hello.get("rounds"))
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
                bearing=float(msg.get("bearing") or 0.0)
                if isinstance(msg.get("bearing"), (int, float))
                else 0.0,
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
        index, trip = msg.get("index"), msg.get("trip")
        if isinstance(index, int):
            game.wait_for(index, pid, trip if isinstance(trip, int) else None)
