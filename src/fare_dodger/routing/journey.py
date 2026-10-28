"""Journeys: what a player submits for a round."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from fare_dodger.modes import Mode
from fare_dodger.timeutil import format_hm


@dataclass(frozen=True, slots=True)
class WalkLeg:
    from_node: int
    to_node: int
    dep: int
    arr: int
    kind: Literal["walk"] = "walk"

    @property
    def mode(self) -> Mode:
        return Mode.WALK


@dataclass(frozen=True, slots=True)
class RideLeg:
    trip: int
    pattern: int
    board_pos: int
    alight_pos: int
    from_node: int
    to_node: int
    dep: int
    arr: int
    mode: Mode
    route_name: str
    kind: Literal["ride"] = "ride"


Leg = WalkLeg | RideLeg


@dataclass(frozen=True, slots=True)
class Journey:
    """A sequence of legs starting at ``origin`` no earlier than ``depart``.

    An empty leg list means "stay where you are".
    """

    origin: int
    depart: int
    legs: tuple[Leg, ...] = ()

    @property
    def destination(self) -> int:
        return self.legs[-1].to_node if self.legs else self.origin

    @property
    def arrival(self) -> int:
        return self.legs[-1].arr if self.legs else self.depart

    @property
    def modes(self) -> tuple[Mode, ...]:
        """Modes used, in order (walking included)."""
        return tuple(leg.mode for leg in self.legs)

    @property
    def rides(self) -> tuple[RideLeg, ...]:
        return tuple(leg for leg in self.legs if isinstance(leg, RideLeg))

    def describe(self, names: list[str]) -> str:
        if not self.legs:
            return f"stay at {names[self.origin]}"
        parts = []
        for leg in self.legs:
            label = "walk" if isinstance(leg, WalkLeg) else f"{leg.mode.value} {leg.route_name}"
            parts.append(
                f"{format_hm(leg.dep)} {label} {names[leg.from_node]} -> "
                f"{names[leg.to_node]} {format_hm(leg.arr)}"
            )
        return "; ".join(parts)

    def to_json(self) -> dict[str, Any]:
        legs: list[dict[str, Any]] = []
        for leg in self.legs:
            item: dict[str, Any] = {
                "kind": leg.kind,
                "from": leg.from_node,
                "to": leg.to_node,
                "dep": leg.dep,
                "arr": leg.arr,
            }
            if isinstance(leg, RideLeg):
                item |= {
                    "trip": leg.trip,
                    "pattern": leg.pattern,
                    "board_pos": leg.board_pos,
                    "alight_pos": leg.alight_pos,
                    "mode": leg.mode.value,
                    "route": leg.route_name,
                }
            legs.append(item)
        return {"origin": self.origin, "depart": self.depart, "legs": legs}

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Journey:
        legs: list[Leg] = []
        for item in data["legs"]:
            if item["kind"] == "walk":
                legs.append(WalkLeg(item["from"], item["to"], item["dep"], item["arr"]))
            else:
                legs.append(
                    RideLeg(
                        trip=item["trip"],
                        pattern=item["pattern"],
                        board_pos=item["board_pos"],
                        alight_pos=item["alight_pos"],
                        from_node=item["from"],
                        to_node=item["to"],
                        dep=item["dep"],
                        arr=item["arr"],
                        mode=Mode(item["mode"]),
                        route_name=item["route"],
                    )
                )
        return cls(data["origin"], data["depart"], tuple(legs))
