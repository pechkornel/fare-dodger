# 0019: A fair bot Dodger and balance sprees

## Status
Accepted

## Context
The live game's balance was measured with ad-hoc scripts: one district, one rule set at a
time. The bot Dodger saw every Bounty Hunter, but since ADR 0017 a human Dodger doesn't. So
the bot's escape rate (48% on Inner Pest) said little about a human's chances. We want each
rule change measured the same way, and the candidates compared with each other.

## Decision
- **The bot Dodger knows only what a human Dodger knows.**
  - It sees the Bounty Hunters within `see_radius_m`, the vehicles they ride, and where it
    was last reported.
  - It remembers a sighting for 180 timetable seconds (20 real seconds).
  - It knows the distance to the nearest Bounty Hunter, as the top bar shows it. When that
    turns red (within 1.5 sight radii), it stops hiding and moves on.
  - It only flees from, avoids, and refuses to board with the Bounty Hunters it has seen.
- **`fare-dodger balance` runs a balance spree.**
  - Each variant in `configs/balance.toml` changes rules from `configs/live.toml`. A
    `[sweep]` gives values for one rule at a time, and a `[[variant]]` names a combination.
  - Every variant plays the same rounds: the same district, seed and live random start time
    for each. In each round two Dodgers face the bot Bounty Hunters: the bot Dodger, and one
    standing still.
  - By default it runs 100 rounds per district on four districts (Inner Pest, Buda Castle &
    Gellért, Árpád híd, Kispest), in parallel processes.
- **Score** = |escaped − 45%| + ½·|day − night| + how far standing still escapes more than
  10%. Lower is better. The report gives the 95% interval of each escape rate, so you can
  tell a real difference from noise.

## Results (first spree, seed 1, 400 rounds per variant)
The current rules are already on target with the fair bot Dodger:

| Variant | Escaped (95% CI) | Day | Night | Still |
|---|---|---|---|---|
| **baseline (live.toml)** | **44% (39–49%)** | 42% | 47% | 6% |
| inspected_share 0.2 | 45% (40–50%) | 44% | 49% | 6% |
| ping_every_s 150 / 240 | 45% / 44% | 43% / 42% | 50% / 50% | 6% / 8% |
| ping_every_s 300 | 50% | 49% | 52% | 8% |
| hunter_extra 2 / 4 | 49% / 40% | 46% / 40% | 56% / 39% | 9% / 5% |
| sight_radius_m 150 / 300 | 54% / 31% | 53% / 27% | 56% / 42% | 6% / 6% |
| see_radius_m 150 / 300 | 32% / 42% | 31% / 41% | 35% / 44% | 6% / 6% |
| catch_radius_m 15 / 40 | 49% / 40% | 46% / 36% | 55% / 49% | 6% / 6% |
| duration_minutes 25 / 45 | 60% / 32% | 57% / 29% | 67% / 38% | 21% / 4% |

- No variant beats the baseline by more than noise, so the rules stay as they are.
  `inspected_share` 0.2 ranked first, but only by luck within the interval.
- **The levers, strongest first:**
  - Round length: 25 minutes makes even standing still escape 21%.
  - `sight_radius_m`, how close a bot Bounty Hunter starts chasing.
  - `see_radius_m`. Smaller makes it *harder* for the Dodger, which also sees less.
  - `hunter_extra` and `catch_radius_m`.
  - Report frequency and the inspectors hardly matter between 150 and 240 s.
- **Districts differ more than most rules.** On the baseline the Dodger escapes 34% in Inner
  Pest and 56% in Buda Castle & Gellért. Nights are still a little easier (47% vs 42%).

## Consequences
- The escape rates are what a bot achieves with a human's knowledge. A good human player will
  do better, a new one worse.
- A rule change comes with a spree: add its values to `configs/balance.toml`, run
  `uv run fare-dodger balance`, and keep it if it scores better than the baseline by more than
  noise.
- A full spree takes about 20 minutes on 15 processes, once the districts are downloaded.
  `--district`, `--rounds` and `--only` cut it down.
- The per-district spread suggests evening out districts (size, or hunters per district)
  rather than changing global rules. That's left for later.
