# 0007. Walking on streets, pace and the catch rule

- Status: accepted
- Date: 2026-10-28

## Context
After playing, the owner reported four things:
- characters could walk through buildings;
- time ran too fast and vehicles moved too fast;
- characters were a bit too slow;
- a Bounty Hunter on the pavement could catch a Dodger who was still aboard a vehicle
  standing at a stop.

Riding should still beat walking.

## Decision
- **Streets** (`live/streets.py`):
  - **Data:** walkable OpenStreetMap ways (every `highway` except motorways, construction and
    the like, minus `foot=no` and private access) are downloaded once from the Overpass API,
    for a box covering every district. The download is ~11 MB and ~1 minute, cached as
    `data/osm/streets.json`.
  - **Graph:** each district builds a street graph of the edges inside its polygon, largest
    connected component only, from 2,900 to 13,100 nodes. `play --free-walk` (or no data)
    keeps the old free walking.
  - **Steering (human, Pac-Man style):** the key direction picks the street that matches it
    best at a junction (dot product ≥ 0.35). Bends are followed automatically when you arrive
    at them while walking, unless the street turns back. Between junctions, a key across the
    street walks to the nearer junction where you can turn that way. Holding a random
    direction from a random station left the player stuck in 3–7% of tries, mostly where no
    street goes that way.
  - **Bots** walk shortest street routes (A*, 0.3–1.6 ms):
    - Bounty Hunters reroute when their target moves 25 m.
    - Walking legs of RAPTOR journeys follow a cached street path between the two stations.
    - The bot Dodger flees by steering like a human, and hides on a street.
  - **Boarding:** stations are snapped to their nearest street point. You can board within
    `board_radius_m` of either the station or that point, because a few stations are up to
    50 m from the nearest street.
- **Pace:** `time_scale` goes from 10 to 6, so vehicles move 40% slower on screen.
  `duration_minutes` goes from 30 to 25, so a round lasts about 4 real minutes, inside the
  3–5 minutes the owner wants.
  *Later:* once the districts were 1.5 times wider, walking and riding felt slow again.
  `time_scale` goes to 9 and `duration_minutes` to 35, so a round is still about 4 real
  minutes. The timings meant in real seconds keep their length: pings every 270 s (30 real
  seconds), fine hold 90 s (10), coffee 27 + 180 s (3 + 20).
- **Jogging speed:** `walk_speed_kmh` goes from 5 to 10, so characters move about 20% faster
  on screen. Street routes are about 1.3× longer than the straight line, so jogging covers
  about 7.7 km/h as the crow flies. Buses and trams average 15–20 km/h along their route and
  the metro over 30, so riding still wins over a few stops. RAPTOR's walking transfers use the
  same speed with a 1.3 detour factor (`[network]` in `configs/live.toml`), so the bots'
  plans match how fast they actually walk.
- **Catch rule:** a Bounty Hunter catches the Dodger only on the same vehicle trip, or when
  both are on foot within `catch_radius_m`. Waiting at a stop and being held for a fine count
  as on foot. Staying aboard a vehicle standing at a stop is now safe; getting off next to a
  Bounty Hunter is not.

## Consequences
- The first `play` needs network access to Overpass. Later runs use the cache. The data is
  OpenStreetMap (ODbL), and the map already credits it.
- Sight and catch distances are still straight-line, so they reach across a block.
- Walking near a vehicle is no longer dangerous for a Dodger who stays aboard, which
  favours the Dodger. Retune with playtests.
