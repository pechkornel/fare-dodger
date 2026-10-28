# Simulation results (v0.1.0)

These are bot-vs-bot results for the v0 rules and five rule variants, with suggested rule
changes. All numbers come from `fare-dodger sim` and can be reproduced exactly (see
[Reproducing](#reproducing)).

**Setup.** Feed `2821.20261028`, pinned service day Tue 2026-11-17, default network (421
stations inside 4.5 km of Deák Ferenc tér; metro, tram, trolleybus and frequent buses). 12
rounds × 10 minutes from 08:00, and 200 games per matchup (seeds 42–241). Ranges in
brackets are Wilson 95% intervals. With 200 games an interval is about ±7 points, so gaps
under about 10 points between two rows are not reliable.

**Balance target** ([plan](plan.md#decisions-resolved-2026-10-28)): the Dodger should win
**about 45%** of games against the *best* Inspector strategy for a given rule set.

## Bots in brief
- **random**: picks a random reachable station each round (both roles).
- **greedy Dodger**: moves to the reachable station that maximises the minimum Inspector
  travel time over a 45-minute horizon.
- **belief**: the Inspectors track every station the Dodger could be at, given reveals and
  timetable reachability, and spread over that set to cover as much of it as possible
  (coverage placement, ADR 0004).
- **belief-kmeans**: the same belief set, but each Inspector heads for a k-means centre.

## 1. Bot matchups under the v0 rules
Rules: 3 Inspectors, Dodger revealed after every 3rd round, no mode reveals.

| Dodger \ Inspectors | random | belief | belief-kmeans |
|---|---:|---:|---:|
| random | 93.0% [89–96%] | 56.5% [50–63%] | 64.0% [57–70%] |
| greedy | 99.5% [97–100%] | **76.0% [70–81%]** | 82.0% [76–87%] |

- Belief tracking is what makes the Inspectors competitive. Random Inspectors almost never
  catch anyone: 15 catches in 400 games.
- The greedy Dodger is the strongest Dodger, and coverage placement (`belief`) is the
  strongest Inspector strategy. The gap between the two Inspector strategies is within
  noise here.
- **Under the v0 rules the Dodger wins 76% against the best Inspectors**, far above the 45%
  target. Catches come late: 29 of the 48 catches by `belief` against the greedy Dodger
  happen in rounds 11–12, after the last reveal that still matters (round 9). The reveal
  after round 12 comes after the game is over and carries no information.

## 2. Rule variants (greedy Dodger)

| Variant | Rules changed | vs belief | vs belief-kmeans | vs best | Mean belief size¹ |
|---|---|---:|---:|---:|---:|
| baseline | none | 76.0% [70–81%] | 82.0% [76–87%] | 76.0% | 96 |
| modes | `reveal_modes=true` | 57.0% [50–64%] | 62.5% [56–69%] | 57.0% | 77 |
| reveal2 | `reveal_every=2` | 39.0% [33–46%] | 56.0% [49–63%] | 39.0% | 57 |
| four | `n_inspectors=4` | 71.0% [64–77%] | 72.0% [65–78%] | 71.0% | 93 |
| **modes_reveal2** | `reveal_modes=true`, `reveal_every=2` | **46.5% [40–53%]** | 52.0% [45–59%] | **46.5%** | 49 |
| four_modes | `n_inspectors=4`, `reveal_modes=true` | 53.5% [47–60%] | 48.5% [42–55%] | 48.5% | 70 |

¹ The average number of stations the Inspectors still consider possible, averaged over both
Inspector strategies.

What the numbers say:
- **Information is the bottleneck, not manpower.** A fourth Inspector alone barely moves
  the result (76% → 71%), and the belief set stays at about 93 stations. Mode reveals
  (76% → 57%) and more frequent position reveals (76% → 39%) each shrink the belief set and
  cut the Dodger's win rate far more.
- **`reveal_every=2` on its own overshoots and depends on Inspector skill.** It gives 39%
  against `belief` but 56% against `belief-kmeans`, a 17-point gap. A rule whose balance
  depends that much on how well the Inspectors play is fragile with human players.
- **Two variants land on the target:** `modes_reveal2` (46.5% against the best strategy) and
  `four_modes` (48.5%). Both are within noise of 45% and of each other, and both are fairly
  stable across the two Inspector strategies (5-point gaps).

## 3. How catches happen
Catches from both Inspector strategies combined, 400 games per variant:

| Variant | Catches | at a stop / on a vehicle | Rounds with most catches |
|---|---:|---:|---|
| baseline | 84 | 27 / 57 | r11 (33), r12 (11) |
| modes | 161 | 90 / 71 | r8 (34), r7 (31) |
| reveal2 | 210 | 92 / 118 | r6 (44), r8 (36) |
| four | 114 | 36 / 78 | r8 (30), r9 (22), r11 (22) |
| modes_reveal2 | 203 | 103 / 100 | r6 (38), r5 (36), r7 (35) |
| four_modes | 196 | 108 / 88 | r7 (40), r8 (33) |

- **Vehicle catches matter.** Under the v0 rules, two thirds of catches (57 of 84) happen on
  a shared vehicle trip, not at a station. Once modes are revealed, the Inspectors can also
  predict where the Dodger ends a round, and stop catches rise to 51–56% of catches.
- **More information moves catches earlier.** Catches shift from the endgame (rounds 11–12)
  to the middle of the game (rounds 5–8), so games are decided by play rather than by the
  clock running out.
- **The Dodger gets caught at the edge of the map.** The most common catch locations are
  Szállodák (Hotels), Bereck utca, Csalit utca, Gábor Áron utca / Pasaréti út, Baba utca,
  Júlia utca and Közvágóhíd. All of them are 3.7–4.5 km from Deák Ferenc tér (the network
  median is 2.9 km; the area edge is 4.5 km), and all but Közvágóhíd are served by only one
  or two bus lines. The greedy Dodger runs away from the Inspectors towards the boundary and ends up at
  stops with only one way out. This is a weakness of the bot, not of the rules (see caveats).

## 4. Suggested rule changes
1. **Turn on mode reveals and reveal the position every 2nd round.** This is the main
   suggestion for the v1 rules:
   ```toml
   [rules]
   reveal_every = 2
   reveal_modes = true
   ```
   This gives 46.5% [40–53%] against the best Inspectors, on target and with 3 Inspectors
   (4 players). With `reveal_every = 2` the Inspectors get five reveals that still matter
   (after rounds 2, 4, 6, 8 and 10) instead of three. The reveal after round 12 still comes
   after the game is over.
2. **Alternative for a 5-player game:** `n_inspectors = 4` with `reveal_modes = true` (48.5%
   [42–55%]), keeping position reveals every 3rd round. Use this when there are four people
   to play Inspector. Do not add a fourth Inspector *without* mode reveals: it only reaches
   71%.
3. **Keep vehicle catches** (`catch_on_same_vehicle = true`). They are the most common way
   to catch the Dodger under every variant without mode reveals.
4. **Do not use `reveal_every = 2` without mode reveals.** It overshoots against good
   Inspectors (39%) and swings by 17 points between Inspector strategies.

The v0 defaults in `configs/default.toml` are unchanged in 0.1.0. Adopting a rule set is a
design decision for the next phase.

## Caveats
- These are **bot** results. The greedy Dodger has a known weakness (it flees to dead-end
  stops at the edge of the area), so a careful human Dodger will probably do better than
  these numbers show, while human Inspectors may do worse than `belief`. The suggested
  rule sets should be checked again with stronger bots or playtests.
- All results use one service day, one 08:00 start and one play area. Off-peak timetables
  have fewer departures and would likely favour the Inspectors.
- The station merges in 0.1.0 (434 → 421 stations, ADR 0001 addendum) changed the numbers
  noticeably. Under the v0 rules on the old network, the Dodger won 87.0% against `belief` and
  76.5% against `belief-kmeans`; now it wins 76.0% and 82.0%. So which Inspector strategy is
  "best" is not stable, and the target should always be checked against the *best*
  strategy.

## Reproducing
These commands need the downloaded feed `2821.20261028` (`uv run fare-dodger download`
while BKK still serves it). Simulation results do not depend on the number of workers;
reruns are byte-identical.

```sh
uv run fare-dodger sim --matrix --games 200 --workers 4 --out results/m200 -c configs/default.toml
```

The rule-variant loop (bash and PowerShell) is in [plan.md](plan.md#rerunning-the-rule-variant-simulations);
it writes to `results/rules/<variant>-<inspectors>/`.
