"""Command-line interface (``fare-dodger``)."""

from __future__ import annotations

import typer

from fare_dodger import __version__

app = typer.Typer(
    name="fare-dodger",
    help="Fare Dodger: a hidden-movement chase game on the Budapest transit network.",
    no_args_is_help=True,
    add_completion=False,
)


@app.callback()
def main() -> None:
    """Fare Dodger command-line tools."""


@app.command()
def version() -> None:
    """Print the package version."""
    typer.echo(__version__)
