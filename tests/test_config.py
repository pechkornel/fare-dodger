from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from fare_dodger.config import NetworkConfig, RulesConfig
from fare_dodger.workspace import network_config, rules_config

DEFAULT = Path(__file__).parents[1] / "configs" / "default.toml"


def test_default_toml_matches_code_defaults() -> None:
    assert network_config(DEFAULT) == NetworkConfig()
    assert rules_config(DEFAULT) == RulesConfig()
    assert network_config(None) == NetworkConfig()


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
