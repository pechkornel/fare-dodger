from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest
import typer

from fare_dodger.cli import _apply_overrides
from fare_dodger.config import RulesConfig
from fare_dodger.network.timetable import Timetable
from fare_dodger.sim.report import summarise, wilson_interval, write_reports
from fare_dodger.sim.runner import run_matchups

RULES = RulesConfig(n_inspectors=2, min_start_distance_min=4, rounds=6)
MATCHUPS = [("random", "belief"), ("greedy", "random")]
META = {
    "feed_version": "fixture-1",
    "service_date": "2026-11-04",
    "nodes": 8,
    "rules_summary": "test",
    "games_per_matchup": 4,
    "base_seed": 5,
}


def test_results_do_not_depend_on_worker_count(timetable: Timetable) -> None:
    serial = run_matchups(timetable, RULES, MATCHUPS, games=4, base_seed=5, workers=1)
    parallel = run_matchups(timetable, RULES, MATCHUPS, games=4, base_seed=5, workers=2)
    assert serial == parallel
    assert [r.seed for r in serial] == [5, 6, 7, 8] * 2
    for r in serial:
        assert r.rounds_survived <= r.rounds_played <= RULES.rounds
        assert (r.winner == "inspectors") == (r.catch_kind is not None)
        assert len(r.dodger_path) == r.rounds_played + 1


def test_reports_are_byte_identical(timetable: Timetable, tmp_path: Path) -> None:
    outputs = []
    for run in ("a", "b"):
        results = run_matchups(timetable, RULES, MATCHUPS, games=4, base_seed=5)
        paths = write_reports(tmp_path / run, results, summarise(results), META)
        outputs.append([p.read_bytes() for p in paths])
    assert outputs[0] == outputs[1]
    payload = json.loads((tmp_path / "a" / "results.json").read_text(encoding="utf-8"))
    assert len(payload["games"]) == 8
    assert {(s["dodger_bot"], s["inspector_bot"]) for s in payload["summaries"]} == set(MATCHUPS)
    with (tmp_path / "a" / "games.csv").open(encoding="utf-8") as fh:
        assert len(list(csv.DictReader(fh))) == 8
    assert "| random | belief |" in (tmp_path / "a" / "summary.md").read_text(encoding="utf-8")


def test_unknown_bot_is_rejected(timetable: Timetable) -> None:
    with pytest.raises(ValueError, match="unknown Dodger bot"):
        run_matchups(timetable, RULES, [("clever", "random")], games=1, base_seed=0)


def test_wilson_interval() -> None:
    lo, hi = wilson_interval(50, 100)
    assert lo == pytest.approx(0.404, abs=1e-3)
    assert hi == pytest.approx(0.596, abs=1e-3)
    assert wilson_interval(0, 0) == (0.0, 1.0)
    assert wilson_interval(10, 10)[1] == 1.0


def test_rule_overrides() -> None:
    rules = _apply_overrides(RulesConfig(), ["reveal_modes=true", "n_inspectors=4"])
    assert rules.reveal_modes is True
    assert rules.n_inspectors == 4
    assert _apply_overrides(RulesConfig(), ["start_time=08:30"]).start_time == "08:30"
    with pytest.raises(typer.BadParameter):
        _apply_overrides(RulesConfig(), ["oops"])
