from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from fare_dodger.config import LiveConfig, NetworkConfig
from fare_dodger.live.admin import UPDATER_TIMEOUT_S, Admin, memory_mib
from fare_dodger.live.match import District
from fare_dodger.live.server import Limits, create_app
from fare_dodger.live.world import World
from fare_dodger.network.timetable import Timetable

TOKEN = "s3cret-admin-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture(scope="module")
def world(timetable: Timetable, fixture_network_config: NetworkConfig) -> World:
    return World(timetable, fixture_network_config.area)


def app(world: World, tmp_path: Path, admin: bool = True) -> TestClient:
    the_admin = Admin(TOKEN, tmp_path, {"version": "0.2.0", "commit": "abc1234"}) if admin else None
    return TestClient(
        create_app([District("X", world)], LiveConfig(seed=1), Limits(max_per_ip=5), the_admin)
    )


def until(ws: Any, kind: str) -> dict[str, Any]:
    while True:
        msg: dict[str, Any] = ws.receive_json()
        if msg["type"] == kind:
            return msg


def test_admin_is_off_without_a_password(
    world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = app(world, tmp_path, admin=False)
    assert client.get("/api/admin/status", headers=AUTH).status_code == 404
    monkeypatch.delenv("FARE_DODGER_ADMIN_TOKEN", raising=False)
    assert Admin.from_env(tmp_path, {}) is None
    monkeypatch.setenv("FARE_DODGER_ADMIN_TOKEN", TOKEN)
    monkeypatch.setenv("FARE_DODGER_COMMIT", "abc1234")
    admin = Admin.from_env(tmp_path, {"version": "0.2.0"})
    assert admin is not None and admin.info == {"version": "0.2.0", "commit": "abc1234"}


def test_admin_needs_the_password(world: World, tmp_path: Path) -> None:
    client = app(world, tmp_path)
    assert client.get("/api/admin/status").status_code == 401
    assert (
        client.get("/api/admin/status", headers={"Authorization": "Bearer nope"}).status_code == 401
    )
    assert client.post("/api/admin/update").status_code == 401
    status = client.get("/api/admin/status", headers=AUTH).json()
    assert status["info"]["commit"] == "abc1234" and status["rooms"] == []
    assert status["update"] == {"state": "never", "pending": False}
    assert client.get("/admin", follow_redirects=False).headers["location"] == "/admin.html"
    assert "Update now" in client.get("/admin.html").text


def test_update_now_asks_the_host(world: World, tmp_path: Path) -> None:
    client = app(world, tmp_path)
    answer = client.post("/api/admin/update", headers=AUTH).json()
    request = tmp_path / "admin" / "update-request"
    assert request.is_file() and answer["update"]["pending"]
    assert not answer["update"]["updater_missing"]
    old = time.time() - UPDATER_TIMEOUT_S - 5
    os.utime(request, (old, old))
    state = client.get("/api/admin/status", headers=AUTH).json()["update"]
    assert state["updater_missing"] is True
    # What the host's updater writes, read back.
    request.unlink()
    (tmp_path / "admin" / "update-status.json").write_text(
        json.dumps({"state": "done", "before": "abc1234", "after": "def5678", "log": "ok"})
    )
    state = client.get("/api/admin/status", headers=AUTH).json()["update"]
    assert state["state"] == "done" and state["after"] == "def5678" and not state["pending"]


def test_admin_sees_and_closes_games(world: World, tmp_path: Path) -> None:
    with app(world, tmp_path) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"type": "solo", "name": "Ann"})
        until(ws, "snapshot")
        rooms = client.get("/api/admin/status", headers=AUTH).json()["rooms"]
        assert len(rooms) == 1 and rooms[0]["players"] == [{"name": "Ann", "team": "A"}]
        assert rooms[0]["state"] == "playing" and rooms[0]["round"] == 1
        code = rooms[0]["code"]
        assert client.post(f"/api/admin/rooms/{code}/close", headers=AUTH).json() == {"ok": True}
        assert until(ws, "closed")["message"] == "The admin closed this game."
        assert client.post("/api/admin/rooms/ZZZZ/close", headers=AUTH).status_code == 404


def test_memory_is_reported() -> None:
    assert memory_mib() > 0
