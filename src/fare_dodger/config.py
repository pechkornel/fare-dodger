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


def _sorted_dict(values: dict[str, float]) -> dict[str, float]:
    return dict(sorted(values.items()))


NameRadii = Annotated[
    dict[str, Annotated[float, Field(gt=0)]],
    PlainSerializer(_sorted_dict, return_type=dict[str, float]),
]


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
    #: Manual fixes: station name (without rail suffix) -> merge radius in metres, for
    #: junctions whose same-name stops lie further apart than the radii above. In TOML,
    #: usually loaded from the file named by ``[network] station_overrides``.
    station_merge_overrides: NameRadii = Field(default_factory=dict)
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


class RulesConfig(_Frozen):
    """Game rules (v0). Expected to change, so every knob lives here."""

    start_time: str = "08:00"
    round_minutes: int = Field(default=10, gt=0)
    rounds: int = Field(default=12, gt=0)
    n_inspectors: int = Field(default=3, ge=1)
    #: The Dodger's position is revealed after every ``reveal_every``-th round (0 = never).
    reveal_every: int = Field(default=3, ge=0)
    #: Also reveal the transport modes the Dodger used in every round.
    reveal_modes: bool = False
    #: Inspector positions are public at the end of every round.
    inspectors_visible: bool = True
    catch_on_same_stop: bool = True
    catch_on_same_vehicle: bool = True
    #: Minimum travel time (minutes) from every Inspector to the Dodger at the start.
    min_start_distance_min: int = Field(default=20, ge=0)
    #: Inspectors may not start at the same station.
    distinct_inspector_starts: bool = True

    @field_validator("start_time")
    @classmethod
    def _valid_start(cls, value: str) -> str:
        parse_hms(value)
        return value

    @property
    def start_seconds(self) -> int:
        return parse_hms(self.start_time)

    @property
    def round_seconds(self) -> int:
        return self.round_minutes * 60

    def round_window(self, round_index: int) -> tuple[int, int]:
        """``(start, end)`` of 0-based round ``round_index`` in service-day seconds."""
        start = self.start_seconds + round_index * self.round_seconds
        return start, start + self.round_seconds

    def is_reveal_round(self, round_number: int) -> bool:
        """Whether the Dodger is revealed after 1-based round ``round_number``."""
        return self.reveal_every > 0 and round_number % self.reveal_every == 0

    @property
    def end_seconds(self) -> int:
        return self.round_window(self.rounds - 1)[1]


class LiveConfig(_Frozen):
    """Real-time prototype: one human Dodger walking and riding against bot Bounty Hunters."""

    start_time: str = "08:00"
    duration_minutes: int = Field(default=30, gt=0)
    #: Timetable seconds that pass per real second.
    time_scale: float = Field(default=10.0, gt=0)
    #: The Bounty Hunters learn the Dodger's position this often (timetable seconds).
    ping_every_s: int = Field(default=180, gt=0)
    n_hunters: int = Field(default=3, ge=1)
    walk_speed_kmh: float = Field(default=5.0, gt=0)
    #: A Bounty Hunter on foot this close to an exposed Dodger catches them.
    catch_radius_m: float = Field(default=25.0, gt=0)
    #: A Bounty Hunter on foot spots a Dodger on foot within this distance and gives chase.
    sight_radius_m: float = Field(default=100.0, ge=0)
    #: How close to a stop the Dodger must stand to wait for and board a vehicle.
    board_radius_m: float = Field(default=40.0, gt=0)
    #: Minimum travel time (minutes) from every Bounty Hunter to the Dodger at the start.
    min_start_distance_min: int = Field(default=8, ge=0)
    #: Money (forints) at the start; a single ticket covers one ride and is used on boarding.
    start_money_ft: int = Field(default=2000, ge=0)
    ticket_price_ft: int = Field(default=450, ge=0)
    #: Share of trips with ticket inspectors aboard; nobody knows which until they board.
    inspected_share: float = Field(default=0.35, ge=0, le=1)
    #: Caught without a ticket: put off at the next stop and held there this long (timetable
    #: seconds) while the fine is processed. Game over if the Dodger cannot pay.
    fine_ft: int = Field(default=1000, ge=0)
    fine_hold_s: int = Field(default=60, ge=0)
    #: The inspectors report a fined Dodger's position to the Bounty Hunters.
    fine_alerts_hunters: bool = True
    #: Match: the first side to win more than half of ``regulation_rounds`` takes the match.
    #: Sides switch after half of them; at a tie after regulation, sides switch every round
    #: until one team leads by two.
    regulation_rounds: int = Field(default=10, ge=2, multiple_of=2)
    #: The human's side in the first round.
    start_role: Literal["dodger", "hunter"] = "dodger"
    #: Fixed seed for reproducible matches; ``None`` picks a new one per match.
    seed: int | None = None
    tile_url: str = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
    tile_attribution: str = (
        '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'
    )

    @field_validator("start_time")
    @classmethod
    def _valid_start(cls, value: str) -> str:
        parse_hms(value)
        return value

    @property
    def start_seconds(self) -> int:
        return parse_hms(self.start_time)

    @property
    def end_seconds(self) -> int:
        return self.start_seconds + self.duration_minutes * 60

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
