from __future__ import annotations

from fare_dodger.network.merge import Platform, merge_platforms, station_key

LAT, LON = 47.5, 19.05
M_PER_DEG_LON = 75_200


def p(
    stop_id: str,
    name: str,
    metres_east: float = 0.0,
    *,
    parent_station: str | None = None,
    is_rail: bool = False,
) -> Platform:
    return Platform(stop_id, name, LAT, LON + metres_east / M_PER_DEG_LON, parent_station, is_rail)


def test_station_key_strips_rail_suffixes() -> None:
    assert station_key("Deák Ferenc tér M") == "Deák Ferenc tér"
    assert station_key("Örs vezér tere M+H") == "Örs vezér tere"
    assert station_key("Batthyány tér H") == "Batthyány tér"
    assert station_key("Astoria M (Dohány utca)") == "Astoria"
    assert station_key("Hild tér (Deák Ferenc tér M)") == "Hild tér (Deák Ferenc tér M)"
    assert station_key("Mexikói út") == "Mexikói út"
    assert station_key("Hamzsabégi út") == "Hamzsabégi út"


def test_same_name_close_platforms_merge() -> None:
    clusters = merge_platforms([p("a", "X"), p("b", "X", 120)], radius_m=150)
    assert [c.platform_ids for c in clusters] == [("a", "b")]


def test_same_name_far_platforms_stay_apart() -> None:
    clusters = merge_platforms([p("a", "Ady Endre utca"), p("b", "Ady Endre utca", 3000)], 150)
    assert len(clusters) == 2


def test_different_names_never_merge_by_distance() -> None:
    assert len(merge_platforms([p("a", "X"), p("b", "Y", 10)], 150)) == 2


def test_parent_station_merges_regardless_of_name_and_distance() -> None:
    clusters = merge_platforms(
        [p("a", "Station", parent_station="S"), p("b", "Exit", 600, parent_station="S")], 150
    )
    assert len(clusters) == 1
    assert clusters[0].name in {"Station", "Exit"}


def test_rail_platform_uses_wider_radius_and_suffix() -> None:
    metro = p("m", "Keleti pályaudvar", 0, is_rail=True)
    surface = p("s", "Keleti pályaudvar M", 250)
    merged = merge_platforms([metro, surface], 150, station_radius_m=300)
    assert len(merged) == 1
    assert merged[0].name == "Keleti pályaudvar"
    assert len(merge_platforms([metro, surface], 150, station_radius_m=None)) == 2
    bus_only = [p("x", "Kossuth"), p("y", "Kossuth M", 250)]
    assert len(merge_platforms(bus_only, 150, station_radius_m=300)) == 2


def test_merging_is_transitive_and_deterministic() -> None:
    chain = [p("c", "X", 200), p("a", "X", 0), p("b", "X", 100)]
    first = merge_platforms(chain, 150)
    assert first == merge_platforms(list(reversed(chain)), 150)
    assert first[0].platform_ids == ("a", "b", "c")
    assert first[0].name == "X"
