"""Glue between the data directory, configuration files and cached networks."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from fare_dodger.config import NetworkConfig, data_dir, load_toml
from fare_dodger.gtfs.download import ZIP_NAME, extract_feed, read_meta
from fare_dodger.network.build import load_or_build
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


def network_config(path: Path | None) -> NetworkConfig:
    """``[network]`` section of a TOML file, or the defaults."""
    if path is None:
        return NetworkConfig()
    return NetworkConfig.model_validate(load_toml(path).get("network", {}))
