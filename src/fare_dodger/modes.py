"""Transport modes and their mapping from GTFS ``route_type`` values."""

from __future__ import annotations

from enum import StrEnum


class Mode(StrEnum):
    """A way of moving between stations. ``WALK`` only appears in journeys and reveals."""

    METRO = "metro"
    TRAM = "tram"
    TROLLEYBUS = "trolleybus"
    BUS = "bus"
    SUBURBAN_RAIL = "suburban_rail"
    FERRY = "ferry"
    WALK = "walk"


#: Vehicle modes in a stable order; the index is used as a bit position in mode masks.
VEHICLE_MODES: tuple[Mode, ...] = (
    Mode.METRO,
    Mode.TRAM,
    Mode.TROLLEYBUS,
    Mode.BUS,
    Mode.SUBURBAN_RAIL,
    Mode.FERRY,
)

_BASIC_ROUTE_TYPES: dict[int, Mode] = {
    0: Mode.TRAM,
    1: Mode.METRO,
    2: Mode.SUBURBAN_RAIL,
    3: Mode.BUS,
    4: Mode.FERRY,
    11: Mode.TROLLEYBUS,
}


def mode_from_route_type(route_type: int) -> Mode | None:
    """Map a basic or extended GTFS route type to a :class:`Mode` (``None`` if unsupported).

    BKK uses the extended type 109 (suburban railway) for HÉV lines.
    """
    if route_type in _BASIC_ROUTE_TYPES:
        return _BASIC_ROUTE_TYPES[route_type]
    families: list[tuple[range, Mode]] = [
        (range(100, 200), Mode.SUBURBAN_RAIL),
        (range(400, 500), Mode.METRO),
        (range(700, 800), Mode.BUS),
        (range(800, 900), Mode.TROLLEYBUS),
        (range(900, 1000), Mode.TRAM),
        (range(1000, 1100), Mode.FERRY),
        (range(1200, 1300), Mode.FERRY),
    ]
    for family, mode in families:
        if route_type in family:
            return mode
    return None


def mode_bit(mode: Mode) -> int:
    """Bit for ``mode`` in a vehicle-mode mask."""
    return 1 << VEHICLE_MODES.index(mode)


def mask_of(modes: frozenset[Mode] | set[Mode]) -> int:
    """Mode mask containing every vehicle mode in ``modes`` (walking is ignored)."""
    mask = 0
    for mode in modes:
        if mode is not Mode.WALK:
            mask |= mode_bit(mode)
    return mask


ALL_VEHICLES_MASK = mask_of(set(VEHICLE_MODES))
