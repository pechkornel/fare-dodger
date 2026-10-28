from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
from pydantic import ValidationError

from fare_dodger.config import NetworkConfig, RulesConfig
from fare_dodger.workspace import network_config, rules_config

DEFAULT = Path(__file__).parents[1] / "configs" / "default.toml"


def test_default_toml_matches_code_defaults() -> None:
    pinned = network_config(DEFAULT)
    # Only the reproducibility settings differ from the code defaults.
    assert pinned.service_date == date(2026, 11, 17)
    assert "Döbrentei tér" in pinned.station_merge_overrides
    unpinned = pinned.model_copy(update={"service_date": "auto", "station_merge_overrides": {}})
    assert unpinned == NetworkConfig()
    assert rules_config(DEFAULT) == RulesConfig()
    assert network_config(None) == NetworkConfig()


def test_station_overrides_file_is_inlined(tmp_path: Path) -> None:
    (tmp_path / "merges.toml").write_text('[merge]\n"B" = 250\n"A" = 300\n', encoding="utf-8")
    config = tmp_path / "c.toml"
    config.write_text('[network]\nstation_overrides = "merges.toml"\n', encoding="utf-8")
    cfg = network_config(config)
    assert cfg.station_merge_overrides == {"A": 300, "B": 250}
    # Serialised sorted, so the network cache key does not depend on file order.
    assert list(cfg.model_dump()["station_merge_overrides"]) == ["A", "B"]


def test_station_merge_override_radius_must_be_positive() -> None:
    with pytest.raises(ValidationError):
        NetworkConfig.model_validate({"station_merge_overrides": {"X": 0}})


def test_rules_schedule() -> None:
    rules = RulesConfig()
    assert rules.round_window(0) == (28_800, 29_400)
    assert rules.end_seconds == 28_800 + 12 * 600
    assert [r for r in range(1, 13) if rules.is_reveal_round(r)] == [3, 6, 9, 12]
    assert not RulesConfig(reveal_every=0).is_reveal_round(3)


@pytest.mark.parametrize(
    "bad",
    [
        {"window_start": "12:00", "window_end": "11:00"},
        {"modes": ["walk"]},
        {"bus_frequency_window": ["09:00", "08:00"]},
        {"unknown_key": 1},
    ],
)
def test_network_config_validation(bad: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        NetworkConfig.model_validate(bad)


def test_area_shapes() -> None:
    cfg = NetworkConfig.model_validate(
        {
            "area": {
                "kind": "bbox",
                "min_lat": 47.0,
                "min_lon": 19.0,
                "max_lat": 48.0,
                "max_lon": 20.0,
            }
        }
    )
    assert cfg.area.contains(47.5, 19.05)
    poly = NetworkConfig.model_validate(
        {"area": {"kind": "polygon", "vertices": [[47.0, 19.0], [48.0, 19.0], [48.0, 20.0]]}}
    )
    assert poly.area.contains(47.8, 19.1)
    assert not poly.area.contains(47.2, 19.9)
