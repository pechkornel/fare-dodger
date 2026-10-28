"""HTTP + WebSocket server for the live game.

The server owns the game state (authoritative, so multiplayer can be added later). Each
WebSocket connection is one single-player game: the browser sends key state and actions,
and the server advances the game in real time and sends a snapshot ~20 times a second.

Client messages::

    {"type": "keys", "up": bool, "down": bool, "left": bool, "right": bool}
    {"type": "use"}                 # board / get off / stop waiting
    {"type": "wait", "index": int}  # wait for a listed departure
    {"type": "restart"}
"""

from __future__ import annotations

import asyncio
import random
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles

from fare_dodger.config import LiveConfig
from fare_dodger.live.game import Keys, LiveGame, world_view
from fare_dodger.live.world import World

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
    elif kind == "wait":
        index = msg.get("index")
        if isinstance(index, int):
            game.wait_for(index)


def create_app(world: World, cfg: LiveConfig) -> FastAPI:
    app = FastAPI(title="Fare Dodger live", docs_url=None, redoc_url=None)
    world_json = world_view(world, cfg)

    def new_game() -> LiveGame:
        seed = cfg.seed if cfg.seed is not None else random.randrange(2**31)
        return LiveGame(world, cfg, seed)

    @app.get("/api/world")
    def get_world() -> dict[str, Any]:
        return world_json

    @app.websocket("/ws")
    async def play(ws: WebSocket) -> None:
        await ws.accept()
        current = [new_game()]

        async def read() -> None:
            while True:
                msg = await ws.receive_json()
                if isinstance(msg, dict):
                    if msg.get("type") == "restart":
                        current[0] = new_game()
                    else:
                        apply_message(current[0], msg)

        reader = asyncio.create_task(read())
        last = time.monotonic()
        try:
            while not reader.done():
                now = time.monotonic()
                current[0].step(min(now - last, MAX_TICK_S) * cfg.time_scale)
                last = now
                await ws.send_json(current[0].snapshot())
                await asyncio.sleep(TICK_S)
        except WebSocketDisconnect:
            pass
        finally:
            reader.cancel()

    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
    return app
