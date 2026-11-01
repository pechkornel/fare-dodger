from __future__ import annotations

import itertools
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from fare_dodger.config import NetworkConfig
from fare_dodger.live.balance import Outcome
from fare_dodger.live.district_map import (
    DistrictPage,
    chains,
    district_facts,
    district_svg,
    districts_markdown,
    relative,
    simplify,
)
from fare_dodger.live.streets import StreetGraph
from fare_dodger.live.world import World
from fare_dodger.network.timetable import Timetable

SVG = "{http://www.w3.org/2000/svg}"


@pytest.fixture(scope="module")
def world(timetable: Timetable, fixture_network_config: NetworkConfig) -> World:
    w = World(timetable, fixture_network_config.area)
    # A little grid of streets around the first station.
    x, y = w.node_xy[0]
    xy = [(x + dx, y + dy) for dy in (0, 100, 200) for dx in (0, 100, 200)]
    edges = [(i, i + 1) for i in range(9) if i % 3 != 2] + [(i, i + 3) for i in range(6)]
    w.set_streets(StreetGraph(xy, edges))
    return w


def test_the_picture_is_an_svg_with_every_station_and_line(world: World) -> None:
    root = ET.fromstring(district_svg(world, size=300))
    assert (
        root.tag == f"{SVG}svg"
        and max(int(root.get("width", 0)), int(root.get("height", 0))) == 300
    )
    stations = [c for c in root.iter(f"{SVG}circle") if c.get("fill") == "#fff"]
    assert len(stations) == world.tt.n_nodes
    lines = [p for p in root.iter(f"{SVG}path") if p.get("stroke-linecap") == "round"]
    assert lines and {p.get("stroke") for p in lines} & {"#f5c400", "#e41f18", "#fcc419"}
    streets = [p for p in root.iter(f"{SVG}path") if p.get("stroke") == "#c9c2b4"]
    assert len(streets) == 1 and streets[0].get("d", "").startswith("M")


def test_facts(world: World) -> None:
    facts = district_facts(world)
    assert facts.stations == world.tt.n_nodes
    assert facts.area_km2 > 0 and facts.jail
    assert facts.street_km == round(1.2)  # 12 blocks of 100 m
    assert sum(facts.lines.values()) >= 2 and "metro" in facts.lines


def test_simplify_keeps_corners_and_drops_straight_points() -> None:
    line = [(0.0, 0.0), (5.0, 0.1), (10.0, 0.0), (10.0, 10.0)]
    assert simplify(line) == [(0, 0), (10, 0), (10, 10)]
    assert simplify([(0.2, 0.2), (0.4, 0.3)]) == [(0, 0)]
    assert relative([(3, 4), (5, 4), (5, 1)]) == "M3 4l2 0l0 -3"


def test_chains_join_edges_through_plain_corners() -> None:
    # An L (a-b-c) with a branch at c (c-d, c-e), and a separate triangle (a loop).
    a, b, c, d, e = (0, 0), (1, 0), (1, 1), (2, 1), (1, 2)
    t1, t2, t3 = (5, 5), (6, 5), (5, 6)
    edges = {(a, b), (b, c), (c, d), (c, e), (t1, t2), (t2, t3), (t3, t1)}
    lines = chains(edges)
    assert sorted(len(line) for line in lines) == [2, 2, 3, 4]
    covered = {(min(p, q), max(p, q)) for line in lines for p, q in itertools.pairwise(line)}
    assert covered == {(min(p, q), max(p, q)) for p, q in edges}


def outcome(district: int, variant: str, escaped: bool, dodger: str = "bot") -> Outcome:
    return Outcome(variant, dodger, district, 0, "10:00", False, escaped, "", None, 1, 0)


def test_the_district_page_lists_every_district_with_its_picture_and_results(
    world: World, tmp_path: Path
) -> None:
    facts = district_facts(world)
    pages = [
        DistrictPage(0, "Inner Pest", "districts/inner-pest.svg", facts, 3, False),
        DistrictPage(1, "Csepel", "districts/csepel.svg", facts, 5, True),
    ]
    sweep = [outcome(1, "hunter_extra=4", i < 6) for i in range(10)]
    final = [outcome(d, "baseline", i < 4 + d) for d in (0, 1) for i in range(10)]
    final += [outcome(0, "baseline", False, "still")]
    text = districts_markdown(pages, [("Hunter sweep", sweep), ("Final", final)], 3)
    assert "| 0 | [Inner Pest](#inner-pest) | " in text
    assert "| 1 | [Csepel](#csepel) |" in text and "| **5** | 50% |" in text
    assert "| 3 | 40% | 0% |" in text
    assert text.count("<img ") == 2 and 'src="districts/csepel.svg"' in text
    assert "Extra Bounty Hunters: 5 (its own)" in text
    assert "| hunter_extra=4 | 60% (31-83%) |" in text
