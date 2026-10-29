# 0018. Rounds around the clock, night buses included

- Status: accepted
- Date: 2026-10-29

## Context
Every round started at 08:00, and the networks held only 07:30–09:30. The owner wants
variety: other times of day, night buses too.

## Decision
- **The whole service day.** The live districts' networks keep 00:00–26:00 of the picked
  service day. Inner Pest grows from about 1,800 to 11,000 trips: it builds in under a second,
  and drawing or planning stays at a few milliseconds.
- **Night buses.** The bus filter kept a route only if its busiest stop in the area saw
  6 departures an hour at 08:00–09:00, which no night bus does. A bus route is now also kept
  if it sees `night_bus_min_departures_per_hour` (1) in `night_bus_window` (00:30–04:30).
  Inner Pest at 01:00: 908, 909, 914, 934, 941, 950, 966, 969, 984. The metro does not run
  at night, as in reality. Trips listed at 01:00 are the small hours *before* the service
  day's morning, so night rounds start between 00:30 and 03:00.
- **Start times.** `LiveConfig.start_times` lists when rounds may start, plus up to
  `start_jitter_minutes`. The match picks one at random each round (`Match._round_config`).
  `configs/live.toml`: 01:00, 02:30, 06:30, 08:00, 10:30, 13:00, 16:30, 18:00, 20:30, 22:30,
  plus up to 25 minutes. The code default stays a fixed 08:00, so the tests and the
  turn-based game keep their fixed clock.
- The clock shows 🌙 instead of 🕗 between 21:00 and 05:00.
- `BUILD_VERSION` 8: networks rebuild once.

## Consequences
- Night rounds have a handful of buses, and much of the play is on foot.
- Night lighting for the toy town is a cosmetic follow-up.
