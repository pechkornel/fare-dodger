# 0015. The whole city: districts loaded on demand, and the Danube

- Status: accepted
- Date: 2026-10-29

## Context
The owner wants random districts from all of Budapest, not just the six inner ones, and the
Danube as a wall with bridges, so that one bank can be cut off. Building the whole city as
one world would mean about 300,000 buildings and the city's street graph in memory, more
than the home server should hold for one game. Each district on its own is small: its
network builds in 0.2–0.5 s, and its OpenStreetMap layers are 1–3 MB.

## Decision
- **Districts:** `configs/live.toml` keeps the six polygons and adds 15 circles (1.1–1.4 km
  radius). Five sit on bridges (Lánchíd, Margit, Szabadság, Petőfi and Rákóczi, Árpád), and
  the rest are outer neighbourhoods: Óbuda, Újpest, Zugló, Kőbánya, Kispest, Csepel,
  Pesterzsébet, Újbuda, Városmajor and Népliget. The network now includes the HÉV
  (`suburban_rail`), drawn as a green three-car train.
- **On demand:** a `DistrictPool` builds a district's world when a round needs it and keeps
  at most `--keep` (default 6) of them, dropping the least recently used. A round in
  progress keeps its own reference, so dropping a world is safe.
  - Loading runs in a worker thread (`asyncio.to_thread`). Rooms asking for the same
    district share one load.
- **Preloading:** when a round starts, its room starts loading the next round's district
  (`Match.upcoming_district()`). If that load isn't finished when the break ends, the break
  lasts longer, and the result screen says "Loading the next district…". A new match is set
  up with `Match(begin=False)`; it starts once its first district is in, and the lobby says
  "Loading the district…" until then. `/api/world/{i}` loads its district on first request.
- **Map data per district:** streets, buildings, cafés and water are cached in
  `data/osm/<district>/`, each for the district's own bounding box. A `layers.json` marker
  records the layer versions once all four are on disk.
  - Matches pick only districts that have a marker. If none has one, any district can be
    picked.
  - A background thread downloads the missing districts one after another. It makes up to
    five passes, 5 minutes apart, so districts that hit a busy or failing Overpass are
    retried later.
  - Districts join the rotation as they finish. The server is playable from the first
    district.
- **Water:** each district downloads `natural=water` and `waterway=riverbank` areas (ways and
  multipolygon relations, with islands as inner rings) and the `highway` ways tagged
  `bridge`.
  - These become a 4 m raster on the district's flat projection, filled even-odd per area,
    so Margitsziget stays dry.
  - The raster is cleared within 9 m of every bridge.
  - Walking into water is blocked like walking into a building, including sliding along
    the bank. A player standing in water can still step out.
  - Random spawns avoid water.
  - Vehicles are unaffected: trams cross on their bridges, and the metro runs under the
    river.

## Consequences
- Memory stays at about that of `keep` districts, whatever the number of districts. Adding
  a district is just a config entry.
- The first full download is slow: Overpass took about 5½ minutes for one district's four
  layers on 2026-10-29. All 21 districts can take an hour or two in the background. Until
  then, matches rotate over the districts already downloaded.
- The old whole-area caches (`data/osm/streets.json`, `buildings.json`, `cafes.json`) are no
  longer read and can be deleted.
- Bridges are open everywhere near their line, even where a bridge has no footway; the
  raster is coarse enough (4 m) that narrow canals may close. Both are acceptable for the
  game.
