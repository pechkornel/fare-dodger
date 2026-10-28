# 0006. Live matches: rounds, switching sides and districts

- Status: accepted
- Date: 2026-10-28

## Context
The owner wants the live game to be played as a match, the way CS:GO is: short, fast rounds
of 3 to 5 minutes, the first team to 6 round wins takes the match, sides switch at halftime,
and at 5-5 the teams play on, switching sides every round, until one leads by two. Every
round should be played on a different part of Budapest. Multiplayer is still far off, so the
human's team is the human (plus bot teammates), against bots.

## Decision
- **Match rules** (`live/match.py`): `regulation_rounds = 10`. The first team to win
  `regulation_rounds / 2 + 1` (6) rounds wins. The human plays `start_role` (the Dodger by
  default) for rounds 1 to 5 and the other side for rounds 6 to 10. At 5-5, overtime rounds
  alternate sides, starting with `start_role`, until one team leads by two. A round is won by
  the Dodger's side if the Dodger lasts `duration_minutes` (3 real minutes by default), and by
  the Bounty Hunters' side otherwise.
- **Both sides are playable.** `LiveGame` takes a `role`. Everyone uses the same `Traveller`
  mechanics (walking, waiting, boarding, getting off, and for the Dodger tickets and fines).
  - As a Bounty Hunter, the human is one of `n_hunters` and rides for free, with the other
    hunters as bot teammates.
  - The human hunter sees the Dodger only within `sight_radius_m` of any hunter while the
    Dodger is not riding, or through the reports (pings every 3 timetable minutes, and fines).
  - Catching works as before: same trip, or on foot within `catch_radius_m` of an exposed
    Dodger.
- **Bot Dodger** (`live/dodger_bot.py`).
  - It sees the hunters, like a human Dodger. It plans RAPTOR journeys to stations far from
    them and from its last report.
  - It skips journeys that use a trip a hunter is on or stop near a hunter on foot.
  - It hides 90–140 m from its destination station, because the hunters search stations,
    and dwells there for 20–90 timetable seconds.
  - It flees on foot from a hunter within 220 m, and buys a ticket before half of its rides.
  - In 30 simulated rounds on Inner Pest against two bot hunters (the human idle), it escaped
    12 times. A human hunter makes the difference.
- **Districts.** `configs/live.toml` lists six `[[district]]` polygons:
  - Inner Pest;
  - Lipótváros & Terézváros;
  - Buda: Castle & Gellért;
  - Újlipótváros & Angyalföld;
  - Ferencváros & Józsefváros;
  - Városliget & Zugló.

  Each one replaces `[network.area]` and gets its own cached network and shape paths. Each
  stays on one bank of the Danube, with a margin, which was checked on a map rendering,
  because walking is free inside the polygon. The rounds visit them in a shuffled order that
  repeats, never the same district twice in a row. The networks have 15–59 stations.
- **Protocol.** Each snapshot carries `"match"` (round, score, side, district, history,
  next side, winner). The client loads `/api/world/{district}` when the district changes.
  `next` starts the next round, and `restart` starts a new match.

## Consequences
- The human sees the bots as "Bots" on the scoreboard. A team-vs-team multiplayer mode can
  reuse `Match` with human players on both sides.
- The bot Dodger's balance is a first guess. Playtesting should tune `FLEE_M`, the hiding
  distance and `TICKET_PROB`, and if needed the hunters' sight and catch radii per side.
- The districts differ in size and density (Ferencváros & Józsefváros is small), so some
  rounds favour one side. District-specific round lengths are possible later.
