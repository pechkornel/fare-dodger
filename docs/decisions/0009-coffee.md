# 0009. Coffee: a paid speed boost at real cafés

- Status: accepted
- Date: 2026-10-28

## Context
The owner wants a few coffee shops on the map. A coffee costs 500 Ft, takes 3 seconds to get,
and then lets you run at roughly vehicle speed, a bit less, for about 20 seconds.

## Decision
- **Cafés** are real OpenStreetMap `amenity=cafe` nodes (`live/cafes.py`).
  - They are downloaded once from Overpass, for the box covering every district, and cached
    as `data/osm/cafes.json`.
  - Each district picks `cafes_per_district` (4) inside its area. They are spread out: the
    one nearest the centre first, then farthest-point sampling. Each café sits on its nearest
    street point, and cafés more than 30 m from a street are skipped.
  - If the download fails, the game runs without cafés and says so.
- **Rules** (the Dodger only):
  - Stand within `cafe_radius_m` (25 m) of a café and press **C**. You pay `coffee_price_ft`
    (500 Ft).
  - You then stand still for `coffee_brew_s` (18 timetable seconds, 3 real seconds at 6×).
    You can't move, wait for a vehicle or buy a ticket, and you can be caught like anyone on
    foot.
  - After that you run at `coffee_speed_kmh` (16 km/h, against 10 for jogging and 15–20 for
    the average bus or tram) for `coffee_boost_s` (120 timetable seconds, 20 real seconds).
  - Buying another coffee during a boost starts a new boost when it is ready.
- **Bounty Hunters can't buy coffee.** A hunter running at 16 km/h would catch a jogging
  Dodger almost at once. The bot Dodger doesn't use cafés yet.
- **Overpass mirrors:** both Overpass downloads (streets and cafés) now try
  overpass-api.de, overpass.kumi.systems and overpass.private.coffee in turn. The main
  instance answered 502 during development.

## Consequences
- The Dodger gets a money trade-off: tickets (safety on board) against coffee (speed on foot).
- Cafés are fixed points, so Bounty Hunters could camp at them later. The bots don't yet.
