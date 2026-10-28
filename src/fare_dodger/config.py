"""Validated configuration models, loadable from TOML.

Every rule and network parameter that is expected to change lives here rather than in code.
"""

from __future__ import annotations

import os
import tomllib
from datetime import date
from pathlib import Path
from typing import Annotated, Any, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PlainSerializer,
    field_validator,
    model_validator,
)

from fare_dodger.geo import distance_m, point_in_polygon
from fare_dodger.modes import Mode
from fare_dodger.timeutil import parse_hms

BKK_GTFS_URL = "https://go.bkk.hu/api/static/v1/public-gtfs/budapest_gtfs.zip"
DATA_DIR_ENV = "FARE_DODGER_DATA_DIR"

#: Deák Ferenc tér, where the three old metro lines meet.
DEAK_TER = (47.4979, 19.0544)


def _sorted_list(values: frozenset[Any]) -> list[Any]:
    return sorted(values)


#: Sets serialise as sorted lists so dumps (and cache keys) are stable across processes.
ModeSet = Annotated[frozenset[Mode], PlainSerializer(_sorted_list, return_type=list[str])]
NameSet = Annotated[frozenset[str], PlainSerializer(_sorted_list, return_type=list[str])]


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class CircleArea(_Frozen):
    kind: Literal["circle"] = "circle"
    center_lat: float = DEAK_TER[0]
    center_lon: float = DEAK_TER[1]
    radius_m: float = Field(default=4500.0, gt=0)

    def contains(self, lat: float, lon: float) -> bool:
        return distance_m(self.center_lat, self.center_lon, lat, lon) <= self.radius_m


class BBoxArea(_Frozen):
    kind: Literal["bbox"] = "bbox"
    min_lat: float
    min_lon: float
    max_lat: float
    max_lon: float

    def contains(self, lat: float, lon: float) -> bool:
        return self.min_lat <= lat <= self.max_lat and self.min_lon <= lon <= self.max_lon


class PolygonArea(_Frozen):
    kind: Literal["polygon"] = "polygon"
    #: ``[lat, lon]`` vertices.
    vertices: list[tuple[float, float]] = Field(min_length=3)

    def contains(self, lat: float, lon: float) -> bool:
        return point_in_polygon(lat, lon, self.vertices)


Area = Annotated[CircleArea | BBoxArea | PolygonArea, Field(discriminator="kind")]


class NetworkConfig(_Frozen):
    """How the raw GTFS feed is turned into the playable network."""

    gtfs_url: str = BKK_GTFS_URL
    #: ``"auto"`` picks the Tue/Wed/Thu in the feed with the most active trips.
    service_date: date | Literal["auto"] = "auto"
    #: Only trips running inside this window are kept (must cover the game plus look-ahead).
    window_start: str = "07:30"
    window_end: str = "11:30"
    area: Area = Field(default_factory=CircleArea)
    modes: ModeSet = frozenset({Mode.METRO, Mode.TRAM, Mode.TROLLEYBUS, Mode.BUS})
    #: A bus route is kept only if, in the frequency window, its busiest in-area stop
    #: sees at least this many departures. Set to 0 to keep every bus route.
    bus_min_departures_per_hour: float = Field(default=6.0, ge=0)
    bus_frequency_window: tuple[str, str] = ("08:00", "09:00")
    #: Route short names always kept (if their mode is enabled) / always dropped.
    include_routes: NameSet = frozenset()
    exclude_routes: NameSet = frozenset()
    merge_radius_m: float = Field(default=150.0, ge=0)
    #: Same-name radius when one platform is a metro/suburban-rail platform or has a parent.
    station_merge_radius_m: float = Field(default=300.0, ge=0)
    walk_radius_m: float = Field(default=400.0, ge=0)
    walk_speed_kmh: float = Field(default=5.0, gt=0)
    #: Multiplier on straight-line distance to approximate street routes.
    walk_detour_factor: float = Field(default=1.0, ge=1.0)
    #: Minimum time to change vehicles inside a station.
    change_time_s: int = Field(default=60, ge=0)
    metro_change_time_s: int = Field(default=120, ge=0)
    #: Keep only the largest strongly connected part of the network.
    largest_component_only: bool = True

    @field_validator("window_start", "window_end")
    @classmethod
    def _valid_time(cls, value: str) -> str:
        parse_hms(value)
        return value

    @field_validator("bus_frequency_window")
    @classmethod
    def _valid_window(cls, value: tuple[str, str]) -> tuple[str, str]:
        if parse_hms(value[0]) >= parse_hms(value[1]):
            msg = "bus_frequency_window must be (start, end) with start < end"
            raise ValueError(msg)
        return value

    @field_validator("modes")
    @classmethod
    def _no_walk(cls, value: frozenset[Mode]) -> frozenset[Mode]:
        if Mode.WALK in value:
            msg = "walking is always allowed; list only vehicle modes"
            raise ValueError(msg)
        return value

    @model_validator(mode="after")
    def _window_order(self) -> Self:
        if parse_hms(self.window_start) >= parse_hms(self.window_end):
            msg = "window_start must be before window_end"
            raise ValueError(msg)
        return self

    @property
    def window_seconds(self) -> tuple[int, int]:
        return parse_hms(self.window_start), parse_hms(self.window_end)

    @property
    def walk_speed_mps(self) -> float:
        return self.walk_speed_kmh / 3.6


def data_dir() -> Path:
    """Directory for downloaded feeds and built networks (``./data`` by default)."""
    return Path(os.environ.get(DATA_DIR_ENV, "data")).resolve()


def load_toml(path: Path) -> dict[str, Any]:
    """Read a TOML file into a plain dict."""
    with path.open("rb") as fh:
        return tomllib.load(fh)
