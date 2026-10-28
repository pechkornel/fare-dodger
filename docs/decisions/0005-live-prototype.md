# 0005. Live (real-time) prototype

- Status: accepted
- Date: 2026-10-28

## Context
The owner wants the game to play as a live action game rather than a board game: walk
around a real map with WASD, get on and off real trams and buses, and later play team
against team online. The first step is a single-player prototype: one human Dodger against
bot Inspectors, on a small district instead of the whole city, on real map tiles. Free
walking is acceptable for now; walking along streets comes later.

## Decision
- **Server-authoritative, browser client.** The Python core runs the game (`live/`) and a
  FastAPI WebSocket server streams snapshots about 20 times a second. A plain-JavaScript Leaflet
  client draws them on OpenStreetMap tiles and sends key state and actions. One connection
  is one game. Multiplayer can reuse the same loop with several connections per game.
- **Continuous timetable time.** The clock runs `time_scale` times faster than real time
  (default 10×, so a 30-minute game takes 3 real minutes). Vehicles move in straight lines
  between consecutive stops on their real timetable. GTFS shapes are not used yet.
- **Boarding by waiting.** The Dodger stands within 40 m of a stop and waits for a
  departure (the next one, or one picked from the board). The Dodger is aboard when it
  leaves, and gets off at the next stop on request. This avoids pixel-perfect timing at 10×
  speed.
- **Catching.** An Inspector catches the Dodger by riding the same trip, or by being on foot
  within 25 m while the Dodger is exposed: on foot, or on a vehicle standing at a stop.
  Riding past an Inspector between stops is safe, matching "pass-through is safe" (plan
  decision 3).
- **Information.** The Dodger always sees the Inspectors (plan decision 2). The Inspectors
  learn the Dodger's position every 3 timetable minutes, and see a Dodger on foot within
  100 m.
- **Bots.** Inspectors follow RAPTOR journeys to target stations. After a ping they spread
  over the stations the Dodger could reach in the next 5 minutes, chase on sight, and walk
  back to the network afterwards.
- **District.** `configs/live.toml` uses a polygon over inner Pest (districts VII–VIII)
  on the pinned service day, giving 41 stations and 29 lines. Walking is kept inside the
  polygon, so the Danube is never crossed on foot.

## Consequences
- The routing, timetable and station-merging code is reused unchanged; only `live/` is new.
- Trips are clipped to the district, so a few stations at the edge are terminals in the
  game, and a vehicle's direction shows its last stop inside the district. Starts avoid
  those dead ends.
- Straight-line walking ignores buildings, and straight-line vehicles cut corners. Street
  walking (OpenStreetMap) and GTFS shapes are the next steps for realism.
- OpenStreetMap's public tiles are fine for local play, but a public deployment needs
  another tile provider (`tile_url` in `configs/live.toml`).
- The balance numbers from the round-based simulations do not carry over directly.

## Addendum (2026-10-28): GTFS shapes for vehicle paths
Vehicles now follow their real route geometry. Each hop of a pattern (two consecutive
stations) gets the part of its first trip's GTFS shape between the two stops, cut by
`shape_dist_traveled` (`live/shapes.py`). The path runs from the merged station's centre to
the platform's point on the shape, along the shape, and on to the next station's centre, at
constant speed per hop. Hops without shape data, or whose cut is longer than 3× the straight
distance plus 300 m, stay straight lines. The route lines on the map use the same paths.

On the live district every one of the 193 hops has a shape. The longest detour is 2.4× the
straight distance, where trolleybus 76 loops around Keleti pályaudvar. The paths are built in
about 0.5 s on the first `play` and cached as `hops.json` next to the network. The network
build, RAPTOR and the round-based game are unchanged. Inspectors walking between stations
still move in straight lines.

## Addendum (2026-10-28): Bounty Hunters, tickets and a cartoon look
The owner asked for a more cartoony game with ticket inspectors on some vehicles, a new name
for the chasers, and a choice between buying a ticket and riding without one.
- **Bounty Hunters.** The bots on foot are now called Bounty Hunters (`BountyHunters`,
  `n_hunters`). Their behaviour is unchanged, and a ticket does not protect against them.
  The round-based phase 1 engine keeps the name Inspectors.
- **Tickets.** The Dodger starts with `start_money_ft` (2,000 Ft) and can buy single tickets
  (`ticket_price_ft`, 450 Ft) at any stop. A ticket is used automatically on the next boarding.
- **Ticket inspectors.** A seeded `inspected_share` (35%) of trips carry inspectors. They are
  not shown on the map, so riding without a ticket is a real gamble: about one ride in three.
  On boarding an inspected trip without a ticket, the Dodger is put off at the next stop and
  held for `fine_hold_s` (60 timetable seconds) while the fine is written. The position is
  reported to the Bounty Hunters (`fine_alerts_hunters`), and then `fine_ft` (1,000 Ft) is
  paid. A Dodger who cannot pay the fine loses at once.
- **Look.** The client uses the Fredoka font, cream panels with thick outlines and hard
  shadows, emoji characters (🏃 Dodger, 🤠 Bounty Hunters), vehicle badges per mode, tinted
  OpenStreetMap tiles, and a "KONTROLL!" burst on inspections. Motion respects
  `prefers-reduced-motion`. CARTO's styled tiles were tried, but they now need an API key.
