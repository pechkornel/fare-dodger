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
