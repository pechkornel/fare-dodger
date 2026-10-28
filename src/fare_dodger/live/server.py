"""HTTP + WebSocket server for the live game.

The server owns the game state (authoritative, so multiplayer can be added later). Each
WebSocket connection plays one match against the bots: the browser sends key state and
actions, and the server advances the current round in real time and sends a snapshot ~20
times a second, with the match state under ``"match"``. Static map data for district ``i``
is served at ``/api/world/{i}``.

Client messages::

    {"type": "keys", "up": bool, "down": bool, "left": bool, "right": bool}
    {"type": "use"}                 # board / get off / stop waiting
    {"type": "wait", "index": int}  # wait for a listed departure
    {"type": "ticket"}              # buy a ticket at the current stop
    {"type": "coffee"}              # buy a coffee at the current café
    {"type": "next"}                # start the next round once this one is over
    {"type": "restart"}             # start a new match
"""

from __future__ import annotations

import asyncio
import random
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles

from fare_dodger.config import LiveConfig
from fare_dodger.live.game import Keys, LiveGame, world_view
from fare_dodger.live.match import District, Match

STATIC_DIR = Path(__file__).parent / "static"
TICK_S = 0.05
#: Longest real-time gap simulated in one tick (avoids jumps after a stall).
MAX_TICK_S = 0.25


def apply_message(game: LiveGame, msg: dict[str, Any]) -> None:
    kind = msg.get("type")
    if kind == "keys":
        game.keys = Keys(
            up=bool(msg.get("up")),
            down=bool(msg.get("down")),
            left=bool(msg.get("left")),
            right=bool(msg.get("right")),
        )
    elif kind == "use":
        game.use()
    elif kind == "ticket":
        game.buy_ticket()
    elif kind == "coffee":
        game.buy_coffee()
    elif kind == "wait":
        index = msg.get("index")
        if isinstance(index, int):
            game.wait_for(index)


def create_app(districts: list[District], cfg: LiveConfig) -> FastAPI:
    app = FastAPI(title="Fare Dodger live", docs_url=None, redoc_url=None)
    views = [{**world_view(d.world, cfg), "name": d.name} for d in districts]

    def new_match(start_role: str | None = None) -> Match:
        seed = cfg.seed if cfg.seed is not None else random.randrange(2**31)
        match_cfg = cfg
        if start_role in ("dodger", "hunter"):
            match_cfg = cfg.model_copy(update={"start_role": start_role})
        return Match(districts, match_cfg, seed)

    @app.get("/api/world/{index}")
    def get_world(index: int) -> dict[str, Any]:
        if not 0 <= index < len(views):
            raise HTTPException(status_code=404, detail="no such district")
        return views[index]

    @app.websocket("/ws")
    async def play(ws: WebSocket) -> None:
        await ws.accept()
        # The main menu picks the side to start on: /ws?role=dodger or /ws?role=hunter.
        start_role = ws.query_params.get("role")
        current = [new_match(start_role)]
        # Each round starts with a short countdown (real seconds) while nobody can move.
        frozen_game: list[LiveGame | None] = [None]
        frozen_until = [0.0]

        def frozen() -> bool:
            return time.monotonic() < frozen_until[0]

        async def read() -> None:
            while True:
                msg = await ws.receive_json()
                if not isinstance(msg, dict):
                    continue
                kind = msg.get("type")
                if kind == "restart":
                    current[0] = new_match(start_role)
                elif kind == "next":
                    current[0].next_round()
                elif kind == "keys" or not frozen():
                    apply_message(current[0].game, msg)

        reader = asyncio.create_task(read())
        last = time.monotonic()
        try:
            while not reader.done():
                now = time.monotonic()
                match = current[0]
                if frozen_game[0] is not match.game:
                    frozen_game[0], frozen_until[0] = match.game, now + cfg.freeze_s
                if not frozen():
                    match.game.step(min(now - last, MAX_TICK_S) * cfg.time_scale)
                match.update()
                last = now
                await ws.send_json(
                    {
                        **match.game.snapshot(),
                        "match": match.view(),
                        "freeze_s": round(max(0.0, frozen_until[0] - now), 2),
                    }
                )
                await asyncio.sleep(TICK_S)
        except WebSocketDisconnect:
            pass
        finally:
            reader.cancel()

    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
    return app
