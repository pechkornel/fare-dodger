"""The admin side of a self-hosted server: status, and asking the host to update the game.

The admin page (``/admin``) is enabled by setting ``FARE_DODGER_ADMIN_TOKEN``; every admin API
call must send it as ``Authorization: Bearer <token>``.

Updating needs the host, not the container: pulling the private repository uses the owner's
git login, and rebuilding needs Docker. So "Update now" only writes a request file into the
data directory (``admin/update-request``). On the host, a systemd path unit
(``deploy/install-updater.sh``) notices it and runs ``deploy/update.sh``: ``git pull`` and
``docker compose up -d --build``, writing its progress to ``admin/update-status.json``,
which the admin page shows.
"""

from __future__ import annotations

import importlib
import json
import os
import secrets
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

#: A request older than this that nobody picked up means the updater is not installed.
UPDATER_TIMEOUT_S = 60.0


@dataclass(frozen=True)
class Admin:
    token: str
    data_dir: Path
    #: What is running: version, commit, feed, service day.
    info: dict[str, str] = field(default_factory=dict)
    started: float = field(default_factory=time.time)

    @classmethod
    def from_env(cls, data_dir: Path, info: dict[str, str]) -> Admin | None:
        """The admin side, if ``FARE_DODGER_ADMIN_TOKEN`` is set (it is off otherwise)."""
        token = os.environ.get("FARE_DODGER_ADMIN_TOKEN", "").strip()
        if not token:
            return None
        return cls(token, data_dir, {**info, "commit": os.environ.get("FARE_DODGER_COMMIT", "")})

    def allows(self, authorization: str | None) -> bool:
        given = (authorization or "").removeprefix("Bearer ").strip()
        return bool(given) and secrets.compare_digest(given, self.token)

    @property
    def folder(self) -> Path:
        return self.data_dir / "admin"

    @property
    def request_file(self) -> Path:
        return self.folder / "update-request"

    @property
    def status_file(self) -> Path:
        return self.folder / "update-status.json"

    def request_update(self) -> None:
        self.folder.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).isoformat(timespec="seconds")
        self.request_file.write_text(json.dumps({"requested": stamp}), encoding="utf-8")

    def update_state(self) -> dict[str, Any]:
        """The last update as the host's updater reported it, plus any pending request."""
        state: dict[str, Any] = {"state": "never"}
        if self.status_file.is_file():
            try:
                state = json.loads(self.status_file.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                state = {"state": "unknown"}
        pending = self.request_file.is_file()
        state["pending"] = pending
        if pending:
            age = time.time() - self.request_file.stat().st_mtime
            state["pending_s"] = round(age)
            state["updater_missing"] = age > UPDATER_TIMEOUT_S and state.get("state") != "running"
        return state


def memory_mib() -> float:
    """This process's resident memory (MiB)."""
    status = Path("/proc/self/status")
    if status.is_file():
        for line in status.read_text().splitlines():
            if line.startswith("VmRSS:"):
                return round(int(line.split()[1]) / 1024, 1)
    try:
        resource = importlib.import_module("resource")  # Unix only
    except ImportError:
        return 0.0  # not measured on Windows
    peak = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)  # macOS: bytes, peak only
    return round(peak / (1024 * 1024 if sys.platform == "darwin" else 1024), 1)
