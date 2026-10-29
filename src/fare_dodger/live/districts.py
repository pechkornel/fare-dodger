"""Districts loaded on demand: the round's district now, the next one in the background.

A server can offer many districts (the whole city) without holding them all: a
:class:`DistrictPool` keeps at most ``keep`` worlds, building one when a round needs it. Rooms
call :meth:`DistrictPool.prepare` for the next round's district while the current round is
played, so it is ready (network, street graph, buildings, water) when the round starts.
Matches only pick *playable* districts (whose map data is on disk) while the rest download.
"""

from __future__ import annotations

import asyncio
import re
import threading
import unicodedata
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass

from fare_dodger.config import NetworkConfig
from fare_dodger.live.world import World


@dataclass(frozen=True, slots=True)
class District:
    """A district that is already built (tests, tools)."""

    name: str
    world: World


@dataclass(frozen=True, slots=True)
class DistrictSpec:
    """A district to build when it is needed."""

    name: str
    net: NetworkConfig

    @property
    def slug(self) -> str:
        """A file-system name: ``"Buda: Castle & Gellért"`` -> ``"buda-castle-gellert"``."""
        ascii_name = unicodedata.normalize("NFKD", self.name).encode("ascii", "ignore").decode()
        return re.sub(r"[^a-z0-9]+", "-", ascii_name.lower()).strip("-")


class DistrictPool:
    def __init__(
        self,
        names: list[str],
        load: Callable[[int], World],
        keep: int = 6,
        built: dict[int, World] | None = None,
        playable: Callable[[int], bool] | None = None,
    ) -> None:
        if not names:
            msg = "no districts"
            raise ValueError(msg)
        self.names = names
        self._load = load
        self.keep = max(1, keep)
        self._worlds: OrderedDict[int, World] = OrderedDict(built or {})
        self._lock = threading.Lock()
        self._pending: dict[int, asyncio.Future[World]] = {}
        self._playable = playable or (lambda _: True)

    @classmethod
    def of(cls, districts: list[District]) -> DistrictPool:
        """A pool of districts that are already built (nothing is ever loaded)."""

        def unavailable(i: int) -> World:
            return districts[i].world

        return cls(
            [d.name for d in districts],
            unavailable,
            keep=len(districts),
            built={i: d.world for i, d in enumerate(districts)},
        )

    def __len__(self) -> int:
        return len(self.names)

    def name(self, i: int) -> str:
        return self.names[i]

    def playable(self) -> list[int]:
        """The districts a new round may pick: those that can load without downloading (all of
        them if none can)."""
        found = [i for i in range(len(self.names)) if self._playable(i)]
        return found or list(range(len(self.names)))

    def ready(self, i: int) -> bool:
        with self._lock:
            return i in self._worlds

    def loaded(self) -> list[int]:
        with self._lock:
            return list(self._worlds)

    def get(self, i: int) -> World:
        """District ``i``'s world, building it now (blocking) if it is not loaded."""
        with self._lock:
            world = self._worlds.get(i)
            if world is not None:
                self._worlds.move_to_end(i)
                return world
        world = self._load(i)
        with self._lock:
            self._worlds[i] = world
            self._worlds.move_to_end(i)
            # Rounds in progress keep their own reference, so dropping one here is safe.
            while len(self._worlds) > self.keep:
                self._worlds.popitem(last=False)
        return world

    async def prepare(self, i: int) -> World:
        """District ``i``'s world, built in a worker thread if needed (once, however many
        rooms ask at the same time)."""
        if self.ready(i):
            return self.get(i)
        pending = self._pending.get(i)
        if pending is None:
            pending = asyncio.ensure_future(asyncio.to_thread(self.get, i))
            self._pending[i] = pending
            pending.add_done_callback(lambda _: self._pending.pop(i, None))
        return await pending
