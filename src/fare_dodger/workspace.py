"""Glue between the data directory, configuration files and cached networks."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from fare_dodger.config import LiveConfig, NetworkConfig, RulesConfig, data_dir, load_toml
from fare_dodger.gtfs.download import ZIP_NAME, extract_feed, read_meta
from fare_dodger.live.shapes import Hops, load_or_build_hops
from fare_dodger.network.build import cache_key, load_or_build
from fare_dodger.network.timetable import Timetable


@dataclass(frozen=True)
class Workspace:
    root: Path

    @classmethod
    def at(cls, root: Path | None) -> Workspace:
        return cls((root or data_dir()).resolve())

    @property
    def gtfs_dir(self) -> Path:
        return self.root / "gtfs"

    @property
    def networks_dir(self) -> Path:
        return self.root / "networks"

    def feed_dir(self) -> tuple[Path, str]:
        """Extracted feed directory and a fingerprint identifying the feed contents."""
        zip_path = self.gtfs_dir / ZIP_NAME
        meta = read_meta(self.gtfs_dir)
        if not zip_path.is_file() or meta is None:
            msg = f"no GTFS feed in {self.gtfs_dir}; run `fare-dodger download` first"
            raise FileNotFoundError(msg)
        extracted = extract_feed(zip_path, self.gtfs_dir / "extracted" / meta.sha256[:16])
        return extracted, meta.sha256

    def network(self, cfg: NetworkConfig, *, rebuild: bool = False) -> tuple[Timetable, Path, bool]:
        feed_dir, fingerprint = self.feed_dir()
        return load_or_build(feed_dir, cfg, self.networks_dir, fingerprint, rebuild=rebuild)

    def vehicle_paths(self, cfg: NetworkConfig, tt: Timetable) -> Hops:
        """GTFS shape paths of the hops of ``tt`` (the network built for ``cfg``), cached."""
        feed_dir, fingerprint = self.feed_dir()
        return load_or_build_hops(feed_dir, tt, self.networks_dir / cache_key(fingerprint, cfg))


def network_config(path: Path | None) -> NetworkConfig:
    """``[network]`` section of a TOML file, or the defaults.

    ``station_overrides = "<file>"`` (relative to the config file) loads the ``[merge]`` table
    of that file into ``station_merge_overrides``, so the overrides are part of the cache key.
    """
    if path is None:
        return NetworkConfig()
    section = dict(load_toml(path).get("network", {}))
    overrides = section.pop("station_overrides", None)
    if overrides is not None:
        merges = load_toml(path.parent / overrides).get("merge", {})
        inline = section.get("station_merge_overrides", {})
        section["station_merge_overrides"] = {**merges, **inline}
    return NetworkConfig.model_validate(section)


def live_districts(path: Path | None) -> list[tuple[str, NetworkConfig]]:
    """The live game's districts: each ``[[district]]`` table (``name`` and polygon
    ``vertices``) on top of the ``[network]`` section, or that section alone if there are none.
    """
    base = network_config(path)
    tables = load_toml(path).get("district", []) if path is not None else []
    if not tables:
        return [("District", base)]
    return [
        (
            str(table["name"]),
            NetworkConfig.model_validate(
                {
                    **base.model_dump(),
                    "area": {"kind": "polygon", "vertices": table["vertices"]},
                }
            ),
        )
        for table in tables
    ]


def rules_config(path: Path | None) -> RulesConfig:
    """``[rules]`` section of a TOML file, or the defaults."""
    if path is None:
        return RulesConfig()
    return RulesConfig.model_validate(load_toml(path).get("rules", {}))


def live_config(path: Path | None) -> LiveConfig:
    """``[live]`` section of a TOML file, or the defaults."""
    if path is None:
        return LiveConfig()
    return LiveConfig.model_validate(load_toml(path).get("live", {}))
