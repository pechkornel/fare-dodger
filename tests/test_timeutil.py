import pytest

from fare_dodger.timeutil import format_hm, format_hms, parse_hms


@pytest.mark.parametrize(
    ("text", "seconds"),
    [("00:00:00", 0), ("08:00", 28_800), ("8:05:30", 29_130), ("28:45:00", 103_500)],
)
def test_parse_hms(text: str, seconds: int) -> None:
    assert parse_hms(text) == seconds


@pytest.mark.parametrize("text", ["", "8", "08:60", "08:00:61", "aa:bb", "1:2:3:4"])
def test_parse_hms_rejects_garbage(text: str) -> None:
    with pytest.raises(ValueError, match="invalid time"):
        parse_hms(text)


def test_format_roundtrip() -> None:
    assert format_hms(103_500) == "28:45:00"
    assert format_hm(29_130) == "08:05"
    with pytest.raises(ValueError, match="negative"):
        format_hms(-1)
