from __future__ import annotations

import math
from pathlib import Path

import pytest

from fare_dodger.config import LiveConfig, NetworkConfig
from fare_dodger.live import balance as bal
from fare_dodger.live.world import World
from fare_dodger.network.timetable import Timetable
from fare_dodger.workspace import live_config

ROOT = Path(__file__).parents[1]


@pytest.fixture(scope="module")
def world(timetable: Timetable, fixture_network_config: NetworkConfig) -> World:
    return World(timetable, fixture_network_config.area)


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "variants.toml"
    path.write_text(text, encoding="utf-8")
    return path


def test_variants_are_a_baseline_one_rule_sweeps_and_named_combinations(tmp_path: Path) -> None:
    path = write(
        tmp_path,
        """
[base]
duration_minutes = 20
[sweep]
ping_every_s = [120, 240]
fine_alerts_hunters = [false]
[[variant]]
name = "both"
ping_every_s = 90
hunter_extra = 1
""",
    )
    variants = bal.read_variants(path, LiveConfig())
    assert [v.name for v in variants] == [
        "baseline",
        "ping_every_s=120",
        "ping_every_s=240",
        "fine_alerts_hunters=false",
        "both",
    ]
    # The [base] overrides go under every variant.
    assert all(v.overrides["duration_minutes"] == 20 for v in variants)
    both = variants[-1].apply(LiveConfig())
    assert (both.ping_every_s, both.hunter_extra, both.duration_minutes) == (90, 1, 20)


@pytest.mark.parametrize(
    "text",
    [
        "[sweep]\nping_evry_s = [1]",  # a typo
        "[sweep]\nping_every_s = [-5]",  # out of range
        "[sweep]\nping_every_s = 120",  # not a list
        "[[variant]]\nhunter_extra = 1",  # no name
        "[[variant]]\nname = 'baseline'",  # a repeated name
        "[swep]\n",
    ],
)
def test_bad_variants_fail_before_anything_runs(tmp_path: Path, text: str) -> None:
    with pytest.raises(ValueError):
        bal.read_variants(write(tmp_path, text), LiveConfig())


def test_the_shipped_variants_are_valid() -> None:
    cfg = live_config(ROOT / "configs" / "live.toml")
    variants = bal.read_variants(ROOT / "configs" / "balance.toml", cfg)
    assert variants[0].name == "baseline" and len(variants) > 10


def test_rounds_use_the_live_start_times_and_repeat_for_every_variant() -> None:
    cfg = LiveConfig(start_times=("01:00", "08:00"), start_jitter_minutes=10)
    rounds = bal.plan_rounds(cfg, [0, 3], 20, seed=5)
    assert rounds == bal.plan_rounds(cfg, [0, 3], 20, seed=5)
    assert len(rounds) == 40 and {r.district for r in rounds} == {0, 3}
    assert {r.start[:4] for r in rounds} <= {"01:0", "01:1", "08:0", "08:1"}
    assert any(r.night for r in rounds) and not all(r.night for r in rounds)
    fixed = bal.plan_rounds(LiveConfig(), [0], 3, seed=0)
    assert {r.start for r in fixed} == {LiveConfig().start_time}


def test_a_batch_plays_every_variant_and_dodger_on_the_same_rounds(world: World) -> None:
    cfg = LiveConfig(duration_minutes=8)
    variants = [bal.Variant("baseline"), bal.Variant("blind", {"sight_radius_m": 0.0})]
    rounds = bal.plan_rounds(cfg, [0], 3, seed=1)
    out = bal.play_batch(lambda _: world, cfg, variants, rounds)
    assert len(out) == len(variants) * len(bal.DODGERS) * len(rounds)
    for v in variants:
        for dodger in bal.DODGERS:
            mine = [o for o in out if o.variant == v.name and o.dodger == dodger]
            assert [(o.seed, o.start) for o in mine] == [(r.seed, r.start) for r in rounds]
    for o in out:
        assert o.escaped is (o.how == "escaped") and o.escaped is (o.caught_min is None)
        if o.dodger == "still":
            assert o.rides == 0
    summaries = bal.summarise(out, variants)
    assert {s.variant for s in summaries} == {"baseline", "blind"}
    text = bal.report(summaries, variants, {0: "Fixture"})
    assert "| blind |" in text and "Fixture" in text


def outcome(
    variant: str, escaped: bool, *, night: bool = False, dodger: str = "bot"
) -> bal.Outcome:
    return bal.Outcome(
        variant,
        dodger,
        0,
        0,
        "02:00" if night else "10:00",
        night,
        escaped,
        "escaped" if escaped else "on foot",
        None if escaped else 10.0,
        1,
        0,
    )


def test_variants_rank_by_distance_from_the_target_and_the_day_night_gap() -> None:
    def rounds(variant: str, day: int, night: int, still: int = 0) -> list[bal.Outcome]:
        return (
            [outcome(variant, i < day) for i in range(10)]
            + [outcome(variant, i < night, night=True) for i in range(10)]
            + [outcome(variant, i < still, dodger="still") for i in range(10)]
        )

    variants = [bal.Variant(n) for n in ("even", "lopsided", "too easy", "hiding wins")]
    out = (
        rounds("even", 4, 5)  # 45%, small gap
        + rounds("lopsided", 1, 8)  # 45%, big gap
        + rounds("too easy", 9, 9)
        + rounds("hiding wins", 4, 5, still=6)
    )
    ranked = bal.summarise(out, variants)
    assert [s.variant for s in ranked] == ["even", "lopsided", "too easy", "hiding wins"]
    best = ranked[0]
    assert best.escaped == pytest.approx(0.45) and best.score == pytest.approx(0.05)
    lo, hi = best.ci95
    assert lo < 0.45 < hi


def test_wilson_interval() -> None:
    lo, hi = bal.wilson(45, 100)
    assert lo == pytest.approx(0.356, abs=1e-3) and hi == pytest.approx(0.548, abs=1e-3)
    assert all(math.isnan(x) for x in bal.wilson(0, 0))


def test_a_grid_tries_every_combination(tmp_path: Path) -> None:
    path = write(tmp_path, "[grid]\nduration_minutes = [25, 45]\nhunter_extra = [2, 3, 4]")
    variants = bal.read_variants(path, LiveConfig())
    assert len(variants) == 1 + 6
    assert variants[1].name == "duration_minutes=25 hunter_extra=2"
    assert variants[-1].overrides == {"duration_minutes": 45, "hunter_extra": 4}


def test_matches_follow_the_live_rules() -> None:
    # Evenly matched teams: about half each; an odd 1v1 match never ties.
    even = bal.simulate_matches(5, 0.45, 0.0, [4.0], n=4000)
    assert 0.45 < even.better_wins < 0.55 and even.overtime == 0
    assert 3 <= even.rounds <= 5 and even.minutes == pytest.approx(4.0 * even.rounds)
    # An even one can, and a longer match picks the better team more often.
    six = bal.simulate_matches(6, 0.45, 0.15, [4.0], n=4000)
    three = bal.simulate_matches(3, 0.45, 0.15, [4.0], n=4000)
    assert six.overtime > 0 and six.rounds >= 6 * (1 - six.overtime) * 0.6
    assert six.better_wins > three.better_wins > 0.5
    assert six.p90_minutes >= six.minutes
    # A team that always wins its rounds takes best of 3 in two.
    sure = bal.simulate_matches(3, 0.5, 0.5, [1.0], n=200)
    assert sure.better_wins == 1 and sure.rounds == 2


def test_round_minutes_count_real_time() -> None:
    cfg = LiveConfig(duration_minutes=36, time_scale=9.0)
    caught = outcome("v", False)  # caught at 10 timetable minutes
    free = outcome("v", True)
    got = bal.round_minutes([caught, free, outcome("v", True, dodger="still")], cfg)
    pause = bal.ROUND_OVERHEAD_S / 60
    assert got == pytest.approx([10 / 9 + pause, 4 + pause])


def test_the_lengths_report_simulates_matches_for_balanced_variants() -> None:
    variants = [bal.Variant("baseline"), bal.Variant("easy", {"duration_minutes": 20})]
    out = [outcome("baseline", i < 9) for i in range(20)]
    out += [outcome("easy", i < 18) for i in range(20)]
    text = bal.lengths_report(variants, LiveConfig(), out, [3, 6], skill=0.1, n=500)
    assert "| baseline | 35 | 45% |" in text and "| easy | 20 | 90% |" in text
    assert "| baseline | 3 |" in text and "| baseline | 6 |" in text
    assert "| easy | 3 |" not in text  # not balanced
