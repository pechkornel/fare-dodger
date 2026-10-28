# 0007. Toy-town graphics: flat when zoomed out, 3D when zoomed in

- Status: accepted
- Date: 2026-10-28

## Context
The owner wants more polished graphics. Zoomed in, they want a bright, cartoony and slightly
3D world made mostly of cubes with rounded edges: houses, buses and people. Zoomed out, the map
should stay flat, much like the current one, but in the same theme. Their reference was a
stylised isometric log cabin, made brighter and suited to the game. The client was Leaflet on
tinted OpenStreetMap raster tiles, which cannot tilt and has no building shapes.

## Decision
- **MapLibre GL on vector tiles.** OpenFreeMap serves OpenMapTiles-schema tiles for free
  without a key. `tile_url` is now that TileJSON, plus a new `glyphs_url`. Attribution comes
  from the TileJSON, so `tile_attribution` is gone. The client draws the tiles in its own
  cartoon style (`static/toon-style.js`): cream land, bright parks and water, white roads with
  tan edges, and peach building footprints. The play area, route lines, stations, sight
  circles and radar pings are GeoJSON layers on top.
- **The camera tilts with zoom** (`pitchForZoom`): flat up to zoom 15, then tilting up to 52°
  by zoom 16.8. The client drives zoom, tilt and follow from a single animation loop. Mouse
  wheel and +/− zoom, and rotation is off, so WASD always matches the screen.
- **3D toy town** (`static/world3d.js`): a three.js custom layer drawn into the map from zoom
  15.5 up. Below that, the flat DOM markers show instead.
  - Houses are extruded in the browser from the tiles' building footprints. Their corners are
    rounded, their tops bevelled, and their heights squashed (×0.6, 4.5–26 m) so the streets
    stay readable.
  - A toon shader adds banded light, storey-by-storey windows (a few of them lit), roof planks
    and a contact shadow at street level. Houses outside the play area are greyed out.
  - Houses grow out of the ground as you zoom in. The nearest are built first, a few
    milliseconds per frame, and merged into chunks.
  - Parks and woods get blocky trees, and every stop gets a little sign.
  - Buses, trolleybuses, trams and metros are rounded boxes in their mode colour, with window
    bands and ink outlines. They turn to face their direction of travel.
  - People are rounded-cube figures: green Dodger with a cap, red Bounty Hunters with cowboy
    hats, pink teammates and a yellow wanted Dodger. They swing their arms and legs when
    walking, and a riding player stands on the vehicle's roof.
  - A "!" bubble marks a chasing hunter or a seen Dodger. When a building hides a character, a
    tinted silhouette shows through.
  - People and vehicles keep a roughly constant size on screen, so they stay visible at every
    zoom. Positions are smoothed between the 20 Hz snapshots.
- **Data:** building footprints come from the vector tiles already loaded
  (`querySourceFeatures`), limited to the play area plus 600 m. The server is unchanged apart
  from `world_view`'s `tiles` field. Overpass was considered for footprints, but it was
  returning errors and would have added a server-side fetch and cache.

## Consequences
- The game needs WebGL 2, and its CDN scripts are pinned: MapLibre 4.7.1 and three 0.169.0.
- Footprints on z14 tile borders can be duplicated or cut. This is rare and only cosmetic.
- OpenFreeMap has no usage limits, but a public deployment should still check its terms or
  self-host the tiles.
