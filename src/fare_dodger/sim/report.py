"""Aggregate simulation results and write JSON, CSV and Markdown reports.

Outputs contain no timestamps or timings, so the same command produces byte-identical files.
"""

from __future__ import annotations

import csv
import json
import math
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from fare_dodger.sim.runner import GameResult
from fare_dodger.timeutil import format_hm


def wilson_interval(successes: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """95% Wilson score interval for a binomial proportion."""
    if n == 0:
        return 0.0, 1.0
    p = successes / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return max(0.0, centre - half), min(1.0, centre + half)


@dataclass(frozen=True, slots=True)
class MatchupSummary:
    dodger_bot: str
    inspector_bot: str
    games: int
    dodger_wins: int
    dodger_win_rate: float
    dodger_win_rate_ci95: tuple[float, float]
    mean_rounds_survived: float
    catches_by_kind: dict[str, int]
    catches_by_round: dict[int, int]
    top_catch_locations: list[tuple[str, int]]
    mean_belief_size: float | None


def summarise(results: list[GameResult], top: int = 5) -> list[MatchupSummary]:
    groups: dict[tuple[str, str], list[GameResult]] = {}
    for r in results:
        groups.setdefault((r.dodger_bot, r.inspector_bot), []).append(r)
    summaries = []
    for (dodger_bot, inspector_bot), games in groups.items():
        wins = sum(1 for g in games if g.winner == "dodger")
        caught = [g for g in games if g.catch_kind is not None]
        locations = Counter(g.catch_node_name for g in caught if g.catch_node_name)
        beliefs = [g.mean_belief_size for g in games if g.mean_belief_size is not None]
        lo, hi = wilson_interval(wins, len(games))
        summaries.append(
            MatchupSummary(
                dodger_bot=dodger_bot,
                inspector_bot=inspector_bot,
                games=len(games),
                dodger_wins=wins,
                dodger_win_rate=round(wins / len(games), 4),
                dodger_win_rate_ci95=(round(lo, 4), round(hi, 4)),
                mean_rounds_survived=round(sum(g.rounds_survived for g in games) / len(games), 3),
                catches_by_kind=dict(sorted(Counter(str(g.catch_kind) for g in caught).items())),
                catches_by_round=dict(
                    sorted(Counter(int(g.catch_round or 0) for g in caught).items())
                ),
                top_catch_locations=sorted(locations.items(), key=lambda kv: (-kv[1], kv[0]))[:top],
                mean_belief_size=round(sum(beliefs) / len(beliefs), 1) if beliefs else None,
            )
        )
    return summaries


def markdown(summaries: list[MatchupSummary], meta: dict[str, Any]) -> str:
    lines = [
        "# Simulation summary",
        "",
        f"- Feed `{meta['feed_version']}`, service day {meta['service_date']}, "
        f"{meta['nodes']} stations",
        f"- {meta['games_per_matchup']} games per matchup, seeds {meta['base_seed']}.."
        f"{meta['base_seed'] + meta['games_per_matchup'] - 1}",
        f"- Rules: {meta['rules_summary']}",
        "",
        "| Dodger | Inspectors | Dodger wins | 95% CI | Mean rounds survived | Catches "
        "(stop / vehicle) | Mean belief size |",
        "|---|---|---:|---:|---:|---:|---:|",
    ]
    for s in summaries:
        lo, hi = s.dodger_win_rate_ci95
        belief = "" if s.mean_belief_size is None else f"{s.mean_belief_size:.0f}"
        lines.append(
            f"| {s.dodger_bot} | {s.inspector_bot} | {s.dodger_win_rate:.1%} | "
            f"{lo:.0%}-{hi:.0%} | {s.mean_rounds_survived:.2f} | "
            f"{s.catches_by_kind.get('stop', 0)} / {s.catches_by_kind.get('vehicle', 0)} | "
            f"{belief} |"
        )
    lines.append("")
    for s in summaries:
        if s.top_catch_locations:
            spots = ", ".join(f"{name} ({count})" for name, count in s.top_catch_locations)
            rounds = ", ".join(f"r{k}: {v}" for k, v in s.catches_by_round.items())
            lines.append(f"- **{s.dodger_bot} vs {s.inspector_bot}**: catches at {spots}")
            lines.append(f"  - by round: {rounds}")
    return "\n".join(lines) + "\n"


def rules_summary(rules: dict[str, Any]) -> str:
    return (
        f"{rules['rounds']} x {rules['round_minutes']} min from {rules['start_time']}, "
        f"{rules['n_inspectors']} Inspectors, reveal every {rules['reveal_every']}, "
        f"mode reveals {'on' if rules['reveal_modes'] else 'off'}, "
        f"min start distance {rules['min_start_distance_min']} min"
    )


def write_reports(
    out_dir: Path,
    results: list[GameResult],
    summaries: list[MatchupSummary],
    meta: dict[str, Any],
) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "meta": meta,
        "summaries": [asdict(s) for s in summaries],
        "games": [r.to_json() for r in results],
    }
    json_path = out_dir / "results.json"
    json_path.write_text(
        json.dumps(payload, indent=1, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    csv_path = out_dir / "games.csv"
    columns = [
        "dodger_bot", "inspector_bot", "seed", "winner", "rounds_survived", "catch_round",
        "catch_kind", "catch_node", "catch_node_name", "catch_time", "catching_inspector",
        "mean_belief_size",
    ]  # fmt: skip
    with csv_path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh, lineterminator="\n")
        writer.writerow(columns)
        for r in results:
            row = r.to_json()
            if row["catch_time"] is not None:
                row["catch_time"] = format_hm(row["catch_time"])
            writer.writerow(["" if row[c] is None else row[c] for c in columns])
    md_path = out_dir / "summary.md"
    md_path.write_text(markdown(summaries, meta), encoding="utf-8", newline="\n")
    return [json_path, csv_path, md_path]
