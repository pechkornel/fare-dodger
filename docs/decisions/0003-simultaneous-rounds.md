# 0003. Simultaneous rounds of concrete, validated journeys

- Status: accepted
- Date: 2026-10-28

## Context
Classic hidden-movement games (Scotland Yard) are turn-based: the fugitive moves, then each
detective moves, one hop at a time on an abstract graph. On a real timetable that breaks
down. Vehicles run on time whether or not it is your turn, a single "hop" can be one
metro stop or a 25-minute bus ride, and sequential turns would let later movers react to
earlier ones within the same moment of game time. We also want a later web version where
four people play at once.

## Decision
- The game runs on the real weekday timetable from a configurable start time (08:00), in
  rounds of 10 minutes of game time (12 rounds by default).
- In each round every player privately submits a **concrete journey**: walk and ride legs
  with real trips, positions and times, ending at any station they can reach before the
  round ends. Staying put is the empty journey.
- When all journeys are in, the server validates them and resolves them **at once**. The
  validator uses exactly the router's semantics: contiguous legs, timetable-exact
  times, change time after a vehicle, walking only on footpaths and never twice in a row,
  inside the round window.
- Catches are evaluated on the resolved round:
  1. an Inspector ends the round at the Dodger's station, or
  2. an Inspector rode the same trip as the Dodger with overlapping hops
     (`[board, alight)` intervals intersect). Consecutive hops of the same train do not count.
- Information flow: Inspector positions are public; the Dodger's position is revealed after
  every 3rd round, and optionally the modes they used each round. Bots and (later) clients
  only receive view objects (`DodgerView`, `InspectorView`), so hidden state cannot leak by
  accident.
- Every catch rule, the reveal schedule and visibility are toggles in `[rules]`.

## Consequences
- Each round is a full journey-planning problem. That is why RAPTOR has to answer "all
  reachable stations within 10 minutes" in about a millisecond (ADR 0002).
- Passing through a station without stopping there at round end is safe: the Dodger can cross
  an Inspector's path unless they share a vehicle. That is deliberate. It is what makes the
  vehicle rule meaningful.
- Rounds are synchronised in game time, so nobody waits for a slower player's journey
  to "finish". A round's state is fully determined by the four submissions, which makes
  replays and deterministic simulations straightforward (`Game.to_json`).
- A journey that ends before the round ends just waits at the destination. Players cannot
  be "in transit" across a round boundary.
