# 0004. Belief-state Inspector bots

- Status: accepted
- Date: 2026-10-28

## Context
Simulation needs Inspectors that play reasonably without seeing the Dodger. Otherwise the
win rates say nothing about the rules. The Inspectors' information is public:
- their own positions;
- the Dodger's position after every 3rd round;
- optionally, the modes the Dodger used each round;
- the fact that no catch happened at the stations where they ended a round.

## Decision
**Belief tracking.** The Inspector team keeps the set of stations the Dodger could be at,
as a *sound* over-approximation: the true position is never excluded, which a test checks
every round.

- At the start: every station that no Inspector can reach within the minimum start distance.
- Each round: expand the set by one multi-source RAPTOR query from the whole belief set,
  which takes about a millisecond. If modes were revealed, the query uses only those vehicle
  modes and at most as many rides as were revealed.
- After the expansion, remove the stations where Inspectors ended the round without a catch.
- After a reveal, collapse the set to the revealed station.

A uniform set rather than a probability distribution keeps updates exact and cheap.

**Placement.** The team predicts the Dodger's possible end-of-round stations (the *target
area*) and places Inspectors with one of two strategies, both shipped as separate bots:
- `belief` (coverage, default): greedy maximum coverage. The most constrained Inspector picks
  first. Each picks the reachable station that maximises the number of uncovered target
  stations it could reach next round, plus a bonus of 2 for ending on a target station.
- `belief-kmeans`: seeded k-means on target-station coordinates, clusters assigned by travel
  time, each Inspector moving to the reachable station nearest its cluster centre.

## Evidence (200 games per matchup, default rules, seeds 42–241)

| Dodger | random Inspectors | belief (coverage) | belief-kmeans |
|---|---:|---:|---:|
| random | 90.5% Dodger wins | **50.5%** | 64.0% |
| greedy | 99.0% | 87.0% | **76.5%** |

Neither placement strategy dominates. Coverage exploits a Dodger that wanders. k-means
heads straight for the centre of the belief and gets more shared-vehicle catches against a
Dodger whose escape routes are predictable. Two coverage variants did not close the gap
against the greedy Dodger: two-step-ahead targets reached 93.3%, and a catch bonus of 8
reached 84.7% (150 games each).

## Consequences
- Belief size is reported per game (`mean_belief_size`), which shows how much each
  rule (reveal frequency, mode reveals) actually informs the Inspectors.
- A stronger Inspector would need a probability distribution over the belief, e.g. a
  particle filter weighted by plausible Dodger policies, plus lookahead. That is left for later.
- Bots receive only view objects, so the bots also serve as a test that the views leak no
  hidden state.
