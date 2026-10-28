"""Service-day time helpers.

GTFS times are "HH:MM:SS" strings measured from noon minus twelve hours of the service day
and may exceed 24:00:00 for trips running past midnight. Internally every time is an ``int``
number of seconds since the start of the service day; ``datetime`` is never used for them.
"""

from __future__ import annotations

SECONDS_PER_MINUTE = 60
SECONDS_PER_HOUR = 3600


def parse_hms(value: str) -> int:
    """Parse ``H:MM`` or ``H:MM:SS`` (hours may exceed 23) into seconds."""
    parts = value.strip().split(":")
    if len(parts) not in (2, 3) or not all(p.isdigit() for p in parts):
        msg = f"invalid time of day: {value!r}"
        raise ValueError(msg)
    hours, minutes = int(parts[0]), int(parts[1])
    seconds = int(parts[2]) if len(parts) == 3 else 0
    if minutes >= 60 or seconds >= 60:
        msg = f"invalid time of day: {value!r}"
        raise ValueError(msg)
    return hours * SECONDS_PER_HOUR + minutes * SECONDS_PER_MINUTE + seconds


def format_hms(seconds: int) -> str:
    """Format seconds since service-day start as ``HH:MM:SS``."""
    if seconds < 0:
        msg = f"negative time: {seconds}"
        raise ValueError(msg)
    hours, rest = divmod(seconds, SECONDS_PER_HOUR)
    minutes, secs = divmod(rest, SECONDS_PER_MINUTE)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def format_hm(seconds: int) -> str:
    """Format seconds since service-day start as ``HH:MM`` (seconds truncated)."""
    return format_hms(seconds)[:5]
