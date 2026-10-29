"""The compact, cacheable playable network.

Everything RAPTOR touches is a flat list of ints indexed through CSR-style ``*_ptr`` offsets.
Python lists are used at runtime (indexing a list is several times faster than indexing a
numpy array element by element); numpy is only used for compact storage in the ``.npz`` cache.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from fare_dodger.modes import VEHICLE_MODES, Mode

FORMAT_VERSION = 2

_INT_ARRAYS = (
    "node_mode_mask",
    "node_change_s",
    "pat_mode",
    "pat_stop_ptr",
    "pat_stops",
    "pat_trip_ptr",
    "trip_time_ptr",
    "trip_arr",
    "trip_dep",
    "node_pat_ptr",
    "node_pat",
    "node_pat_pos",
    "fp_ptr",
    "fp_to",
    "fp_dur",
)
_FLOAT_ARRAYS = ("node_lat", "node_lon")
_STR_LISTS = ("node_name", "pat_route_id", "pat_route_name", "trip_id", "trip_headsign")


@dataclass(frozen=True, slots=True)
class NodeInfo:
    idx: int
    name: str
    lat: float
    lon: float
    modes: frozenset[Mode]
    change_s: int
    platform_ids: tuple[str, ...]


@dataclass(eq=False)
class Timetable:
    """Compact network for one service day.

    Layout (``P`` patterns, ``T`` trips, ``N`` nodes):

    * pattern ``p`` visits ``pat_stops[pat_stop_ptr[p]:pat_stop_ptr[p+1]]``
    * its trips are ``pat_trip_ptr[p] .. pat_trip_ptr[p+1]-1``, sorted by departure, FIFO
    * trip ``t`` at position ``i`` arrives ``trip_arr[trip_time_ptr[t] + i]`` and departs
      ``trip_dep[...]``
    * node ``n`` is served by ``(node_pat[k], node_pat_pos[k])`` for ``k`` in
      ``node_pat_ptr[n] .. node_pat_ptr[n+1]-1``
    * footpaths from ``n``: ``fp_to[k]``, ``fp_dur[k]`` for ``k`` in ``fp_ptr[n] ..``
    """

    feed_version: str
    service_date: str
    window: tuple[int, int]
    node_name: list[str]
    node_lat: list[float]
    node_lon: list[float]
    node_mode_mask: list[int]
    node_change_s: list[int]
    node_platforms: list[list[str]]
    pat_route_id: list[str]
    pat_route_name: list[str]
    pat_mode: list[int]
    pat_stop_ptr: list[int]
    pat_stops: list[int]
    pat_trip_ptr: list[int]
    trip_id: list[str]
    #: Where each trip really goes (GTFS ``trip_headsign``; "" if the feed has none), which
    #: can be well beyond the last stop kept in the network's area.
    trip_headsign: list[str]
    trip_time_ptr: list[int]
    trip_arr: list[int]
    trip_dep: list[int]
    node_pat_ptr: list[int]
    node_pat: list[int]
    node_pat_pos: list[int]
    fp_ptr: list[int]
    fp_to: list[int]
    fp_dur: list[int]
    meta: dict[str, Any] = field(default_factory=dict)
    _name_index: dict[str, list[int]] = field(default_factory=dict, repr=False)

    # --- sizes ---------------------------------------------------------------------------
    @property
    def n_nodes(self) -> int:
        return len(self.node_name)

    @property
    def n_patterns(self) -> int:
        return len(self.pat_mode)

    @property
    def n_trips(self) -> int:
        return len(self.trip_id)

    @property
    def n_footpaths(self) -> int:
        return len(self.fp_to)

    # --- accessors -----------------------------------------------------------------------
    def node(self, idx: int) -> NodeInfo:
        mask = self.node_mode_mask[idx]
        return NodeInfo(
            idx=idx,
            name=self.node_name[idx],
            lat=self.node_lat[idx],
            lon=self.node_lon[idx],
            modes=frozenset(m for i, m in enumerate(VEHICLE_MODES) if mask >> i & 1),
            change_s=self.node_change_s[idx],
            platform_ids=tuple(self.node_platforms[idx]),
        )

    def pattern_stops(self, pattern: int) -> list[int]:
        return self.pat_stops[self.pat_stop_ptr[pattern] : self.pat_stop_ptr[pattern + 1]]

    def pattern_mode(self, pattern: int) -> Mode:
        return VEHICLE_MODES[self.pat_mode[pattern]]

    def pattern_of_trip(self, trip: int) -> int:
        """Pattern containing ``trip`` (binary search over ``pat_trip_ptr``)."""
        lo, hi = 0, self.n_patterns - 1
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if self.pat_trip_ptr[mid] <= trip:
                lo = mid
            else:
                hi = mid - 1
        return lo

    def trip_times(self, trip: int) -> tuple[list[int], list[int]]:
        pattern = self.pattern_of_trip(trip)
        length = self.pat_stop_ptr[pattern + 1] - self.pat_stop_ptr[pattern]
        off = self.trip_time_ptr[trip]
        return self.trip_arr[off : off + length], self.trip_dep[off : off + length]

    def footpaths(self, node: int) -> list[tuple[int, int]]:
        """``(to_node, duration_s)`` pairs leaving ``node``."""
        lo, hi = self.fp_ptr[node], self.fp_ptr[node + 1]
        return list(zip(self.fp_to[lo:hi], self.fp_dur[lo:hi], strict=True))

    def walk_duration(self, a: int, b: int) -> int | None:
        for to, dur in self.footpaths(a):
            if to == b:
                return dur
        return None

    def find_nodes(self, query: str) -> list[int]:
        """Nodes whose name equals ``query`` (case-insensitive), else those containing it."""
        if not self._name_index:
            for i, name in enumerate(self.node_name):
                self._name_index.setdefault(name.casefold(), []).append(i)
        key = query.casefold()
        if key in self._name_index:
            return list(self._name_index[key])
        return [i for i, name in enumerate(self.node_name) if key in name.casefold()]

    # --- persistence ---------------------------------------------------------------------
    def save(self, directory: Path) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        arrays: dict[str, Any] = {
            name: np.asarray(getattr(self, name), dtype=np.int32) for name in _INT_ARRAYS
        }
        arrays.update(
            {name: np.asarray(getattr(self, name), dtype=np.float64) for name in _FLOAT_ARRAYS}
        )
        np.savez_compressed(directory / "timetable.npz", **arrays)
        sidecar = {
            "format_version": FORMAT_VERSION,
            "feed_version": self.feed_version,
            "service_date": self.service_date,
            "window": list(self.window),
            "node_platforms": self.node_platforms,
            "meta": self.meta,
            **{name: getattr(self, name) for name in _STR_LISTS},
        }
        (directory / "timetable.json").write_text(
            json.dumps(sidecar, ensure_ascii=False, separators=(",", ":")) + "\n",
            encoding="utf-8",
            newline="\n",
        )

    @classmethod
    def load(cls, directory: Path) -> Timetable:
        sidecar = json.loads((directory / "timetable.json").read_text(encoding="utf-8"))
        if sidecar.get("format_version") != FORMAT_VERSION:
            msg = f"unsupported timetable format in {directory}; rebuild the network"
            raise ValueError(msg)
        with np.load(directory / "timetable.npz") as data:
            lists: dict[str, Any] = {name: data[name].tolist() for name in _INT_ARRAYS}
            lists.update({name: data[name].tolist() for name in _FLOAT_ARRAYS})
        return cls(
            feed_version=sidecar["feed_version"],
            service_date=sidecar["service_date"],
            window=(sidecar["window"][0], sidecar["window"][1]),
            node_platforms=sidecar["node_platforms"],
            meta=sidecar["meta"],
            **{name: sidecar[name] for name in _STR_LISTS},
            **lists,
        )
