"""Command-line interface (``fare-dodger``)."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Annotated

import typer

from fare_dodger import __version__
from fare_dodger.config import NetworkConfig, data_dir
from fare_dodger.gtfs.download import download_feed

app = typer.Typer(
    name="fare-dodger",
    help="Fare Dodger: a hidden-movement chase game on the Budapest transit network.",
    no_args_is_help=True,
    add_completion=False,
)

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
    target = (data or data_dir()) / "gtfs"
    result = download_feed(url or NetworkConfig().gtfs_url, target, force=force)
    state = "downloaded" if result.downloaded else "up to date"
    meta = result.meta
    typer.echo(
        f"{state}: {result.zip_path} ({meta.size_bytes / 1e6:.1f} MB, "
        f"feed_version={meta.feed_version}, valid {meta.feed_start_date}..{meta.feed_end_date})"
    )
