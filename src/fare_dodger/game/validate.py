"""Validate a submitted journey against the timetable.

The rules mirror the router (see ``fare_dodger.routing.raptor``), so every RAPTOR journey is
valid. A hand-made journey is valid if it could have been travelled for real: contiguous legs,
real trips at their real times, change time after a vehicle, walking only along footpaths and
never twice in a row, all inside the round's time window.
"""

from __future__ import annotations

from fare_dodger.network.timetable import Timetable
from fare_dodger.routing.journey import Journey, RideLeg, WalkLeg


class InvalidJourneyError(ValueError):
    """A submitted journey breaks the rules or does not match the timetable."""


def journey_errors(
    tt: Timetable, journey: Journey, origin: int, window: tuple[int, int]
) -> list[str]:
    """All rule violations of ``journey`` for a player at ``origin`` in ``window``."""
    start, end = window
    errors: list[str] = []
    if journey.origin != origin:
        errors.append(f"journey starts at node {journey.origin}, player is at {origin}")
    if journey.depart < start:
        errors.append("journey departs before the round starts")

    position = journey.origin
    clock = max(journey.depart, start)
    prev = "start"
    for i, leg in enumerate(journey.legs):
        where = f"leg {i + 1}"
        if leg.from_node != position:
            errors.append(f"{where} starts at node {leg.from_node}, expected {position}")
        if not 0 <= leg.to_node < tt.n_nodes:
            errors.append(f"{where} ends at unknown node {leg.to_node}")
            return errors
        if isinstance(leg, WalkLeg):
            if prev == "walk":
                errors.append(f"{where}: two walks in a row are not allowed")
            duration = tt.walk_duration(leg.from_node, leg.to_node)
            if duration is None:
                errors.append(f"{where}: no footpath between these stations")
            elif leg.arr != leg.dep + duration:
                errors.append(f"{where}: walking takes {duration} s")
            if leg.dep < clock:
                errors.append(f"{where}: leaves before arriving at the station")
            prev = "walk"
        elif isinstance(leg, RideLeg):
            errors.extend(f"{where}: {e}" for e in _ride_errors(tt, leg))
            ready = clock + (tt.node_change_s[position] if prev == "ride" else 0)
            if leg.dep < ready:
                errors.append(f"{where}: departs {leg.dep}, but boarding is possible from {ready}")
            prev = "ride"
        position = leg.to_node
        clock = leg.arr
    if journey.legs and journey.arrival > end:
        errors.append("journey arrives after the round ends")
    return errors


def _ride_errors(tt: Timetable, leg: RideLeg) -> list[str]:
    if not 0 <= leg.trip < tt.n_trips:
        return [f"unknown trip {leg.trip}"]
    pattern = tt.pattern_of_trip(leg.trip)
    if pattern != leg.pattern:
        return [f"trip {leg.trip} belongs to pattern {pattern}"]
    stops = tt.pattern_stops(pattern)
    if not 0 <= leg.board_pos < leg.alight_pos < len(stops):
        return ["invalid board/alight positions"]
    errors = []
    if stops[leg.board_pos] != leg.from_node or stops[leg.alight_pos] != leg.to_node:
        errors.append("trip does not stop at these stations at these positions")
    off = tt.trip_time_ptr[leg.trip]
    if tt.trip_dep[off + leg.board_pos] != leg.dep:
        errors.append("departure time does not match the timetable")
    if tt.trip_arr[off + leg.alight_pos] != leg.arr:
        errors.append("arrival time does not match the timetable")
    if tt.pattern_mode(pattern) != leg.mode:
        errors.append("mode does not match the route")
    return errors


def validate_journey(tt: Timetable, journey: Journey, origin: int, window: tuple[int, int]) -> None:
    """Raise :class:`InvalidJourneyError` listing every violation, if any."""
    errors = journey_errors(tt, journey, origin, window)
    if errors:
        raise InvalidJourneyError("; ".join(errors))
