"""Balance sprees: bot-only rounds under rule variants, to find the rules that play best.

A :class:`Variant` is a name and :class:`~fare_dodger.config.LiveConfig` overrides. Every
variant plays the same rounds (district, seed, start time), so the differences between them
come from the rules and not from the dice. Each round is played by the bot Dodger (which knows
only what a human Dodger knows) and, as a check, by a Dodger standing still: hiding in one
place must lose.

A variant scores by how far the bot Dodger's escape rate is from :data:`TARGET`, plus half
the gap between day and night rounds, plus a penalty when standing still escapes more than
:data:`STILL_MAX`. Lower is better.
"""

from __future__ import annotations

import csv
import math
import random
import statistics
import tomllib
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

from fare_dodger.config import LiveConfig
from fare_dodger.live.game import LiveGame, Seat, Status
from fare_dodger.live.world import World
from fare_dodger.timeutil import format_hms, parse_hms

#: The bot Dodger should escape this share of rounds.
TARGET = 0.45
#: A Dodger standing still should escape at most this share.
STILL_MAX = 0.10
#: Rounds starting before this hour or from ``NIGHT_FROM`` on count as night rounds.
NIGHT_UNTIL, NIGHT_FROM = 5, 21
#: Simulation step (timetable seconds).
STEP_S = 5.0
#: The two Dodgers: the bot, and one standing still (a human seat that never moves).
DODGERS = ("bot", "still")


@dataclass(frozen=True, slots=True)
class Variant:
    """A rule set to try: overrides on the live config."""

    name: str
    overrides: dict[str, Any] = field(default_factory=dict)

    def apply(self, cfg: LiveConfig) -> LiveConfig:
        return LiveConfig.model_validate({**cfg.model_dump(), **self.overrides})


def _label(value: Any) -> str:
    return str(value).lower() if isinstance(value, bool) else str(value)


def read_variants(path: Path, cfg: LiveConfig) -> list[Variant]:
    """The variants in a TOML file: ``baseline`` (the ``[base]`` overrides, or none), then one
    variant per value under ``[sweep]`` (each changing one rule from the baseline), then every
    ``[[variant]]`` (a ``name`` and overrides on top of the baseline). Every variant is
    checked against ``cfg`` so a typo fails before anything runs."""
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    unknown = set(data) - {"base", "sweep", "variant"}
    if unknown:
        raise ValueError(f"{path}: unknown tables {sorted(unknown)}")
    base: dict[str, Any] = data.get("base", {})
    variants = [Variant("baseline", dict(base))]
    for key, values in data.get("sweep", {}).items():
        if not isinstance(values, list):
            raise ValueError(f"{path}: sweep.{key} must be a list of values")
        variants += [Variant(f"{key}={_label(v)}", {**base, key: v}) for v in values]
    for spec in data.get("variant", []):
        entry = dict(spec)
        name = entry.pop("name", None)
        if not isinstance(name, str):
            raise ValueError(f"{path}: every [[variant]] needs a name")
        variants.append(Variant(name, {**base, **entry}))
    names = [v.name for v in variants]
    if len(set(names)) != len(names):
        raise ValueError(f"{path}: variant names repeat: {names}")
    fields = set(LiveConfig.model_fields)
    for v in variants:
        if bad := set(v.overrides) - fields:
            raise ValueError(f"{path}: {v.name}: not live config fields: {sorted(bad)}")
        v.apply(cfg)
    return variants


@dataclass(frozen=True, slots=True)
class Round:
    """One round's dice: where, which seed, and when the clock starts."""

    district: int
    seed: int
    start: str

    @property
    def night(self) -> bool:
        hour = int(self.start[:2]) % 24
        return hour < NIGHT_UNTIL or hour >= NIGHT_FROM


def plan_rounds(cfg: LiveConfig, districts: Iterable[int], n: int, seed: int) -> list[Round]:
    """``n`` rounds per district, at the config's random start times (or its fixed one)."""
    rounds = []
    for district in districts:
        rng = random.Random(f"{seed}:{district}")
        for i in range(n):
            if cfg.start_times:
                at = parse_hms(rng.choice(cfg.start_times))
                at += 60 * rng.randint(0, cfg.start_jitter_minutes)
                start = format_hms(at)
            else:
                start = cfg.start_time
            rounds.append(Round(district, seed + i, start))
    return rounds


@dataclass(frozen=True, slots=True)
class Outcome:
    variant: str
    dodger: str
    district: int
    seed: int
    start: str
    night: bool
    escaped: bool
    #: ``escaped``, ``on foot`` or ``same vehicle``.
    how: str
    #: Minutes into the round of the catch.
    caught_min: float | None
    rides: int
    fines: int


def play_round(world: World, cfg: LiveConfig, variant: str, dodger: str, rnd: Round) -> Outcome:
    """One bot-only round: a Dodger (``bot`` or ``still``) against the bot Bounty Hunters."""
    cfg = cfg.model_copy(update={"start_time": rnd.start})
    runner = Seat(None, "Bot") if dodger == "bot" else Seat("still", "Still")
    game = LiveGame(world, cfg, rnd.seed, runners=[runner], hunters=[Seat(None, "Hunter")])
    while game.status is Status.RUNNING:
        game.step(STEP_S)
    r = game.runners[0]
    how = "escaped" if not r.caught else "same vehicle" if r.route else "on foot"
    return Outcome(
        variant,
        dodger,
        rnd.district,
        rnd.seed,
        rnd.start,
        rnd.night,
        not r.caught,
        how,
        round((game.t - cfg.start_seconds) / 60, 2) if r.caught else None,
        r.traveller.rides,
        r.traveller.fines,
    )


def play_batch(
    world: Callable[[int], World],
    cfg: LiveConfig,
    variants: list[Variant],
    rounds: list[Round],
) -> list[Outcome]:
    """Every variant and Dodger over ``rounds``; ``world`` loads a district."""
    out = []
    for v in variants:
        vcfg = v.apply(cfg)
        for dodger in DODGERS:
            out += [play_round(world(r.district), vcfg, v.name, dodger, r) for r in rounds]
    return out


def _rate(outcomes: list[Outcome]) -> float:
    return sum(o.escaped for o in outcomes) / len(outcomes) if outcomes else math.nan


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """The 95% Wilson interval of ``k`` successes in ``n``."""
    if n == 0:
        return (math.nan, math.nan)
    p = k / n
    centre = (p + z * z / (2 * n)) / (1 + z * z / n)
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return (centre - half, centre + half)


@dataclass(frozen=True, slots=True)
class Summary:
    variant: str
    rounds: int
    escaped: float
    ci95: tuple[float, float]
    day: float
    night: float
    still: float
    median_catch_min: float | None
    rides: float
    fines: float
    by_district: dict[int, float]
    score: float


def summarise(outcomes: list[Outcome], variants: list[Variant]) -> list[Summary]:
    """One summary per variant, best (lowest score) first."""
    out = []
    for v in variants:
        bot = [o for o in outcomes if o.variant == v.name and o.dodger == "bot"]
        still = [o for o in outcomes if o.variant == v.name and o.dodger == "still"]
        if not bot:
            continue
        escaped = _rate(bot)
        day = _rate([o for o in bot if not o.night])
        night = _rate([o for o in bot if o.night])
        still_rate = _rate(still)
        gap = abs(day - night) if not (math.isnan(day) or math.isnan(night)) else 0.0
        score = abs(escaped - TARGET) + 0.5 * gap + max(0.0, still_rate - STILL_MAX)
        catches = [o.caught_min for o in bot if o.caught_min is not None]
        out.append(
            Summary(
                v.name,
                len(bot),
                escaped,
                wilson(sum(o.escaped for o in bot), len(bot)),
                day,
                night,
                still_rate,
                statistics.median(catches) if catches else None,
                statistics.fmean(o.rides for o in bot),
                statistics.fmean(o.fines for o in bot),
                {
                    d: _rate([o for o in bot if o.district == d])
                    for d in sorted({o.district for o in bot})
                },
                score,
            )
        )
    return sorted(out, key=lambda s: s.score)


def _pct(x: float) -> str:
    return "-" if math.isnan(x) else f"{100 * x:.0f}%"


def report(
    summaries: list[Summary], variants: list[Variant], district_names: dict[int, str]
) -> str:
    """A markdown report: the ranking, then the escape rate per district."""
    overrides = {v.name: v.overrides for v in variants}
    lines = [
        f"Bot Dodger escape target {_pct(TARGET)}; standing still at most {_pct(STILL_MAX)}. "
        "Score = |escaped - target| + 0.5 x |day - night| + standing-still excess "
        "(lower is better).",
        "",
        "| # | variant | escaped (95% CI) | day | night | still | catch min | rides | fines "
        "| score |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for i, s in enumerate(summaries, 1):
        lo, hi = s.ci95
        catch = f"{s.median_catch_min:.1f}" if s.median_catch_min is not None else "-"
        lines.append(
            f"| {i} | {s.variant} | {_pct(s.escaped)} ({_pct(lo)}-{_pct(hi)}) | {_pct(s.day)} "
            f"| {_pct(s.night)} | {_pct(s.still)} | {catch} | {s.rides:.1f} | {s.fines:.2f} "
            f"| {s.score:.3f} |"
        )
    districts = sorted({d for s in summaries for d in s.by_district})
    lines += [
        "",
        "Bot Dodger escapes per district:",
        "",
        "| variant | " + " | ".join(district_names.get(d, str(d)) for d in districts) + " |",
        "|---|" + "---|" * len(districts),
    ]
    for s in summaries:
        cells = " | ".join(_pct(s.by_district.get(d, math.nan)) for d in districts)
        lines.append(f"| {s.variant} | {cells} |")
    lines += ["", "Overrides:", ""]
    lines += [f"- **{s.variant}**: `{overrides[s.variant] or 'none'}`" for s in summaries]
    return "\n".join(lines) + "\n"


def write_outcomes(path: Path, outcomes: list[Outcome]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=[f.name for f in fields(Outcome)])
        writer.writeheader()
        writer.writerows(asdict(o) for o in outcomes)
