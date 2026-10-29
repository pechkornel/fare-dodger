"""Command-line interface (``fare-dodger``)."""

from __future__ import annotations

import json
import random
import statistics
import sys
import threading
import time
import tomllib
import weakref
import webbrowser
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Any

import httpx
import typer
import uvicorn

from fare_dodger import __version__
from fare_dodger.bots import DODGER_BOTS, INSPECTOR_BOTS
from fare_dodger.config import LiveConfig, NetworkConfig, RulesConfig
from fare_dodger.geo import distance_m
from fare_dodger.gtfs.download import download_feed
from fare_dodger.live.admin import Admin
from fare_dodger.live.buildings import (
    BUILDINGS_VERSION,
    Buildings,
    download_buildings,
    read_buildings,
)
from fare_dodger.live.cafes import CAFES_VERSION, download_cafes, pick_cafes, read_cafes
from fare_dodger.live.districts import DistrictPool, DistrictSpec
from fare_dodger.live.landmarks import download_landmarks, place_landmarks, read_landmarks
from fare_dodger.live.server import create_app
from fare_dodger.live.streets import (
    STREETS_VERSION,
    BBox,
    StreetGraph,
    area_bbox,
    covering_bbox,
    download_streets,
    read_streets,
)
from fare_dodger.live.water import WATER_VERSION, WaterMask, download_water, read_water
from fare_dodger.live.world import World
from fare_dodger.modes import VEHICLE_MODES
from fare_dodger.network.timetable import Timetable
from fare_dodger.routing.raptor import reachable
from fare_dodger.sim.report import rules_summary, summarise, write_reports
from fare_dodger.sim.runner import run_matchups
from fare_dodger.timeutil import format_hm, parse_hms
from fare_dodger.workspace import (
    Workspace,
    live_config,
    live_districts,
    network_config,
    rules_config,
)

app = typer.Typer(
    name="fare-dodger",
    help="Fare Dodger: a hidden-movement chase game on the Budapest transit network.",
    no_args_is_help=True,
    add_completion=False,
)

ConfigOpt = Annotated[
    Path | None,
    typer.Option("--config", "-c", exists=True, dir_okay=False, help="TOML config file"),
]
DataDirOpt = Annotated[
    Path | None,
    typer.Option("--data-dir", help="Where feeds and built networks are cached [default: ./data]"),
]


@app.callback()
def main() -> None:
    """Fare Dodger command-line tools."""
    # Hungarian stop names must print on legacy Windows consoles (cp1252) too.
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")


@app.command()
def version() -> None:
    """Print the package version."""
    typer.echo(__version__)


@app.command()
def download(
    force: Annotated[bool, typer.Option(help="Re-download even if the cache is current")] = False,
    url: Annotated[str | None, typer.Option(help="GTFS zip URL [default: BKK]")] = None,
    data: DataDirOpt = None,
) -> None:
    """Download the GTFS feed (cached; uses a conditional request when possible)."""
    target = Workspace.at(data).gtfs_dir
    result = download_feed(url or NetworkConfig().gtfs_url, target, force=force)
    state = "downloaded" if result.downloaded else "up to date"
    meta = result.meta
    typer.echo(
        f"{state}: {result.zip_path} ({meta.size_bytes / 1e6:.1f} MB, "
        f"feed_version={meta.feed_version}, valid {meta.feed_start_date}..{meta.feed_end_date})"
    )


def _open_network(config: Path | None, data: Path | None, *, rebuild: bool = False) -> Timetable:
    started = time.perf_counter()
    timetable, path, built = Workspace.at(data).network(network_config(config), rebuild=rebuild)
    action = "built" if built else "loaded"
    typer.echo(f"{action} network {path.name} in {time.perf_counter() - started:.1f}s", err=True)
    return timetable


@app.command()
def build(config: ConfigOpt = None, data: DataDirOpt = None, rebuild: bool = False) -> None:
    """Build (or load from cache) the playable network for a config."""
    tt = _open_network(config, data, rebuild=rebuild)
    typer.echo(
        f"{tt.n_nodes} nodes, {tt.n_patterns} patterns, {tt.n_trips} trips, "
        f"{tt.n_footpaths} footpaths (service day {tt.service_date}, feed {tt.feed_version})"
    )


@app.command()
def info(
    config: ConfigOpt = None,
    data: DataDirOpt = None,
    top: Annotated[int, typer.Option(help="How many largest stations to list")] = 10,
) -> None:
    """Describe the playable network: sizes, routes per mode, largest merged stations."""
    tt = _open_network(config, data)
    typer.echo(f"feed {tt.feed_version}, service day {tt.service_date}")
    typer.echo(
        f"nodes {tt.n_nodes}  patterns {tt.n_patterns}  trips {tt.n_trips}  "
        f"footpaths {tt.n_footpaths}  stop events {len(tt.trip_arr)}"
    )
    typer.echo(f"dropped disconnected nodes: {tt.meta.get('dropped_disconnected_nodes', 0)}")
    for mode, names in tt.meta.get("routes_by_mode", {}).items():
        typer.echo(f"  {mode:<14}{len(names):>4} routes: {', '.join(names)}")
    mode_nodes = Counter(
        m.value
        for n in range(tt.n_nodes)
        for i, m in enumerate(VEHICLE_MODES)
        if tt.node_mode_mask[n] >> i & 1
    )
    typer.echo("nodes served per mode: " + ", ".join(f"{k} {v}" for k, v in mode_nodes.items()))
    typer.echo("largest merged stations (by platforms):")
    by_size = sorted(range(tt.n_nodes), key=lambda n: (-len(tt.node_platforms[n]), n))[:top]
    for n in by_size:
        typer.echo(f"  {tt.node_name[n]:<40}{len(tt.node_platforms[n]):>3} platforms")
    dup = Counter(tt.node_name)
    repeated = sorted(name for name, c in dup.items() if c > 1)
    typer.echo(f"names shared by several nodes: {len(repeated)}")
    for name in repeated[:top]:
        idx = [n for n in range(tt.n_nodes) if tt.node_name[n] == name]
        gap = min(
            distance_m(tt.node_lat[a], tt.node_lon[a], tt.node_lat[b], tt.node_lon[b])
            for a in idx
            for b in idx
            if a < b
        )
        typer.echo(f"  {name:<40}{len(idx):>3} nodes, closest pair {gap:.0f} m apart")


def _resolve_node(tt: Timetable, query: str) -> int:
    if query.isdigit() and int(query) < tt.n_nodes:
        return int(query)
    matches = tt.find_nodes(query)
    if not matches:
        raise typer.BadParameter(f"no station matches {query!r}")
    if len(matches) > 1:
        names = ", ".join(f"{tt.node_name[m]} (#{m})" for m in matches[:8])
        raise typer.BadParameter(f"{query!r} is ambiguous: {names}; use the #number")
    return matches[0]


@app.command()
def reach(
    station: Annotated[str, typer.Argument(help="Station name (or node number)")],
    at: Annotated[str, typer.Option(help="Departure time HH:MM")] = "08:00",
    minutes: Annotated[int, typer.Option(help="Time budget in minutes")] = 10,
    journeys: Annotated[bool, typer.Option(help="Print the journey to each station")] = False,
    config: ConfigOpt = None,
    data: DataDirOpt = None,
) -> None:
    """List every station reachable from STATION within the time budget."""
    tt = _open_network(config, data)
    origin = _resolve_node(tt, station)
    t_dep = parse_hms(at)
    started = time.perf_counter()
    result = reachable(tt, [origin], t_dep, minutes * 60)
    elapsed = (time.perf_counter() - started) * 1000
    reached = result.reached()
    typer.echo(
        f"{len(reached)} stations reachable from {tt.node_name[origin]} "
        f"leaving {format_hm(t_dep)} within {minutes} min ({elapsed:.1f} ms)"
    )
    for node in reached:
        arrival = result.arrival(node)
        assert arrival is not None
        line = f"  {format_hm(arrival)}  {tt.node_name[node]}"
        if journeys:
            line += f"    [{result.journey(node).describe(tt.node_name)}]"
        typer.echo(line)


@app.command()
def bench(
    queries: Annotated[int, typer.Option(help="Number of random origins")] = 500,
    at: Annotated[str, typer.Option(help="Departure time HH:MM")] = "08:00",
    minutes: Annotated[int, typer.Option(help="Time budget in minutes")] = 10,
    seed: int = 0,
    config: ConfigOpt = None,
    data: DataDirOpt = None,
) -> None:
    """Time single-origin reachability queries (RAPTOR) on random stations."""
    tt = _open_network(config, data)
    rng = random.Random(seed)
    t_dep = parse_hms(at)
    timings: list[float] = []
    sizes: list[int] = []
    for _ in range(queries):
        origin = rng.randrange(tt.n_nodes)
        started = time.perf_counter()
        result = reachable(tt, [origin], t_dep, minutes * 60)
        timings.append((time.perf_counter() - started) * 1000)
        sizes.append(len(result.reached()))
    timings.sort()
    p95 = timings[int(0.95 * (len(timings) - 1))]
    typer.echo(
        f"{queries} queries, {minutes} min from {at}: median {statistics.median(timings):.2f} ms, "
        f"p95 {p95:.2f} ms, max {timings[-1]:.2f} ms; "
        f"median {statistics.median(sizes):.0f} stations reached"
    )


def _apply_overrides(rules: RulesConfig, overrides: list[str]) -> RulesConfig:
    """Apply ``key=value`` rule overrides; values use TOML syntax (bare words are strings)."""
    data = rules.model_dump()
    for item in overrides:
        key, sep, raw = item.partition("=")
        if not sep:
            raise typer.BadParameter(f"--rule expects key=value, got {item!r}")
        try:
            value = tomllib.loads(f"v = {raw}")["v"]
        except tomllib.TOMLDecodeError:
            value = raw
        data[key.strip()] = value
    return RulesConfig.model_validate(data)


@app.command()
def sim(
    dodger: Annotated[str, typer.Option(help=f"Dodger bot: {', '.join(DODGER_BOTS)}")] = "greedy",
    inspectors: Annotated[
        str, typer.Option(help=f"Inspector bot: {', '.join(INSPECTOR_BOTS)}")
    ] = "belief",
    matrix: Annotated[bool, typer.Option(help="Run every Dodger x Inspector matchup")] = False,
    games: Annotated[int, typer.Option(min=1, help="Games per matchup")] = 200,
    seed: Annotated[int, typer.Option(help="Base seed; game i uses seed + i")] = 42,
    workers: Annotated[int, typer.Option(min=1, help="Worker processes")] = 1,
    rule: Annotated[
        list[str] | None, typer.Option(help="Rule override key=value (repeatable)")
    ] = None,
    out: Annotated[Path, typer.Option(help="Output directory")] = Path("results"),
    config: ConfigOpt = None,
    data: DataDirOpt = None,
) -> None:
    """Simulate seeded bot-vs-bot games and write results.json, games.csv and summary.md."""
    tt = _open_network(config, data)
    rules = _apply_overrides(rules_config(config), rule or [])
    if matrix:
        matchups = [(d, i) for d in DODGER_BOTS for i in INSPECTOR_BOTS]
    else:
        matchups = [(dodger, inspectors)]
    started = time.perf_counter()
    results = run_matchups(tt, rules, matchups, games, seed, workers)
    elapsed = time.perf_counter() - started
    summaries = summarise(results)
    rules_json = rules.model_dump(mode="json")
    meta = {
        "feed_version": tt.feed_version,
        "service_date": tt.service_date,
        "nodes": tt.n_nodes,
        "network_config": tt.meta.get("config"),
        "rules": rules_json,
        "rules_summary": rules_summary(rules_json),
        "matchups": [list(m) for m in matchups],
        "games_per_matchup": games,
        "base_seed": seed,
    }
    paths = write_reports(out, results, summaries, meta)
    for s in summaries:
        lo, hi = s.dodger_win_rate_ci95
        typer.echo(
            f"{s.dodger_bot:>7} vs {s.inspector_bot:<7} Dodger wins {s.dodger_win_rate:6.1%} "
            f"[{lo:.0%}-{hi:.0%}]  survived {s.mean_rounds_survived:5.2f} rounds  "
            f"catches {dict(s.catches_by_kind)}"
        )
    typer.echo(f"{len(results)} games in {elapsed:.1f}s -> " + ", ".join(str(p) for p in paths))


LIVE_CONFIG = Path("configs/live.toml")


def _load_osm_extra[T](
    what: str,
    path: Path,
    bbox: BBox,
    read: Callable[[Path, BBox], T | None],
    download: Callable[[Path, BBox], Path],
) -> T | None:
    """An optional OpenStreetMap layer, downloaded once; ``None`` (with a warning) if that
    fails."""
    found = read(path, bbox)
    if found is None:
        typer.echo(f"  downloading {what} from OpenStreetMap (Overpass)...", err=True)
        try:
            download(path, bbox)
        except httpx.HTTPError as exc:
            typer.echo(f"  no {what} this time ({exc}); playing without them", err=True)
            return None
        found = read(path, bbox)
    return found


#: The background download goes over the missing districts this often, a while apart.
WARM_PASSES = 5
WARM_RETRY_S = 300.0


class DistrictLoader:
    """Builds one district's world: its network, and its own OpenStreetMap layers (streets
    for the bots, buildings and water as walls, cafés), cached in ``data/osm/<district>/``."""

    def __init__(
        self, workspace: Workspace, specs: list[DistrictSpec], cfg: LiveConfig, *, osm: bool
    ) -> None:
        self.workspace, self.specs, self.cfg, self.osm = workspace, specs, cfg, osm
        self._locks = [threading.Lock() for _ in specs]
        #: The worlds built and still in use, to hand them landmarks downloaded later.
        self._built: weakref.WeakValueDictionary[int, World] = weakref.WeakValueDictionary()
        self.versions = {
            "cafes": CAFES_VERSION,
            **(
                {"streets": STREETS_VERSION, "buildings": BUILDINGS_VERSION, "water": WATER_VERSION}
                if osm
                else {}
            ),
        }

    def _marker(self, i: int) -> Path:
        return self.workspace.osm_dir(self.specs[i].slug) / "layers.json"

    def cached(self, i: int) -> bool:
        """Whether district ``i``'s layers are all on disk (it loads without downloading)."""
        try:
            found = json.loads(self._marker(i).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        return all(found.get(k) == v for k, v in self.versions.items())

    def _landmarks_at(self, i: int) -> tuple[Path, BBox]:
        spec = self.specs[i]
        bbox = covering_bbox([area_bbox(spec.net.area)])
        return self.workspace.osm_dir(spec.slug) / "landmarks.json", bbox

    def has_landmarks(self, i: int) -> bool:
        return read_landmarks(*self._landmarks_at(i)) is not None

    def fetch_landmarks(self, i: int) -> None:
        """Download district ``i``'s landmarks if missing, and hand them to its world if it is
        loaded already, so its next packages go to them."""
        path, bbox = self._landmarks_at(i)
        with self._locks[i]:
            found = _load_osm_extra("landmarks", path, bbox, read_landmarks, download_landmarks)
        world = self._built.get(i)
        if found is not None and world is not None and not world.landmarks:
            world.landmarks = place_landmarks(found, world)
            name = self.specs[i].name
            typer.echo(f"  {name}: {len(world.landmarks)} landmarks for packages", err=True)

    def layers(self, i: int) -> dict[str, Any]:
        """District ``i``'s OpenStreetMap layers, downloading the missing ones."""
        spec = self.specs[i]
        bbox = covering_bbox([area_bbox(spec.net.area)])
        folder = self.workspace.osm_dir(spec.slug)
        get = _load_osm_extra
        with self._locks[i]:  # the warmer and a starting round never download twice
            out: dict[str, Any] = {
                "cafes": get("cafés", folder / "cafes.json", bbox, read_cafes, download_cafes),
                # Optional and never waited for: only the warmer downloads them (see
                # fetch_landmarks). Without them, packages go to street spots.
                "landmarks": read_landmarks(folder / "landmarks.json", bbox),
            }
            if self.osm:
                out["streets"] = get(
                    "streets", folder / "streets.json", bbox, read_streets, download_streets
                )
                out["buildings"] = get(
                    "buildings",
                    folder / "buildings.json",
                    bbox,
                    read_buildings,
                    download_buildings,
                )
                out["water"] = get("water", folder / "water.json", bbox, read_water, download_water)
            if all(out.get(k) is not None for k in self.versions):
                self._marker(i).write_text(json.dumps(self.versions), encoding="utf-8")
        return out

    def __call__(self, i: int) -> World:
        spec, cfg = self.specs[i], self.cfg
        started = time.perf_counter()
        layers = self.layers(i)
        tt, _, _ = self.workspace.network(spec.net)
        world = World(tt, spec.net.area, self.workspace.vehicle_paths(spec.net, tt))
        if layers.get("streets"):
            nodes, ways = layers["streets"]
            world.set_streets(
                StreetGraph.from_osm(nodes, ways, world.proj.to_xy, spec.net.area.contains)
            )
        world.cafes = pick_cafes(layers["cafes"] or [], world, cfg.cafes_per_district)
        world.landmarks = place_landmarks(layers["landmarks"] or [], world)
        if layers.get("buildings"):
            # Stops, stations and cafés stay reachable even when drawn inside a footprint.
            reachable_points = [
                *world.node_xy,
                *world.stop_xy,
                *(p for points in world.platform_xy for p in points),
                *(c.pos for c in world.cafes),
            ]
            world.buildings = Buildings.from_latlon(
                layers["buildings"], world.proj.to_xy, spec.net.area.contains, reachable_points
            )
        if layers.get("water"):
            s, w, n, e = covering_bbox([area_bbox(spec.net.area)])
            (x0, y0), (x1, y1) = world.proj.to_xy(s, w), world.proj.to_xy(n, e)
            world.water = WaterMask.from_latlon(layers["water"], world.proj.to_xy, (x0, y0, x1, y1))
        info = f", {len(world.streets)} street nodes" if world.streets else ""
        info += f", {len(world.cafes)} cafés, {len(world.landmarks)} landmarks"
        if world.buildings is not None:
            info += f", {len(world.buildings)} buildings"
        if world.water is not None:
            info += f", {int(world.water.water.sum()) * 16 // 10_000} ha of water"
        typer.echo(
            f"  {spec.name}: {tt.n_nodes} stations, {tt.n_patterns} patterns{info} "
            f"({time.perf_counter() - started:.1f}s)",
            err=True,
        )
        self._built[i] = world
        return world

    def warm(self) -> None:
        """Download every district's layers, one after the other (a background thread), so
        later rounds do not wait for Overpass. Landmarks for the playable districts come
        first: they are one small query each."""
        for attempt in range(WARM_PASSES):
            missing = [
                i for i in range(len(self.specs)) if not self.cached(i) or not self.has_landmarks(i)
            ]
            if not missing:
                return
            if attempt:
                time.sleep(WARM_RETRY_S)  # Overpass was busy: try the failures again later
            for i in sorted(missing, key=lambda i: not self.cached(i)):
                try:
                    self.layers(i)
                    self.fetch_landmarks(i)
                except Exception as exc:  # the warmer must never stop the server
                    name = self.specs[i].name
                    typer.echo(f"  {name}: OpenStreetMap download failed ({exc})", err=True)


@app.command()
def play(
    config: Annotated[
        Path,
        typer.Option("--config", "-c", exists=True, dir_okay=False, help="TOML config file"),
    ] = LIVE_CONFIG,
    data: DataDirOpt = None,
    host: Annotated[str, typer.Option(help="Interface to listen on")] = "127.0.0.1",
    port: Annotated[int, typer.Option(help="Port to listen on")] = 8000,
    seed: Annotated[int | None, typer.Option(help="Fixed game seed (default: random)")] = None,
    open_browser: Annotated[
        bool, typer.Option("--open", help="Open the game in a browser")
    ] = False,
    osm: Annotated[
        bool,
        typer.Option(
            "--osm/--no-osm",
            help="Use OpenStreetMap streets (bot routes), buildings and water (walls), "
            "cafés (downloaded once per district)",
        ),
    ] = True,
    keep: Annotated[
        int, typer.Option(help="Districts kept in memory at once (others load when needed)")
    ] = 6,
) -> None:
    """Play the live game in the browser: a match of rounds on Budapest districts, as the
    Dodger and as a Bounty Hunter. Walk with WASD, ride real trams and buses."""
    cfg = live_config(config)
    if seed is not None:
        cfg = cfg.model_copy(update={"seed": seed})
    workspace = Workspace.at(data)
    specs = [DistrictSpec(name, net) for name, net in live_districts(config)]
    loader = DistrictLoader(workspace, specs, cfg, osm=osm)
    pool = DistrictPool([d.name for d in specs], loader, keep=keep, playable=loader.cached)
    # One district now (a downloaded one if there is any): this also builds or checks the feed.
    tt0 = pool.get(pool.playable()[0]).tt
    threading.Thread(target=loader.warm, name="osm-warmer", daemon=True).start()
    admin = Admin.from_env(
        workspace.root,
        {
            "version": __version__,
            "feed": tt0.feed_version,
            "service_date": tt0.service_date,
            "districts": str(len(pool)),
        },
    )
    if admin is not None:
        typer.echo("  admin page: /admin (FARE_DODGER_ADMIN_TOKEN is set)", err=True)
    app_ = create_app(pool, cfg, admin=admin)
    url = f"http://{host}:{port}/"
    typer.echo(f"Fare Dodger live: {len(pool)} districts. Open {url} (Ctrl+C to stop)")
    if open_browser:
        webbrowser.open(url)
    uvicorn.run(app_, host=host, port=port, log_level="warning")
