# 0008. Toy-town graphics: flat when zoomed out, 3D when zoomed in

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

## Addendum (2026-10-28): more detail, smaller people, bigger vehicles
The owner liked the houses but wanted more detail all round. Vehicles should be more realistic,
characters less simple, and characters smaller relative to vehicles.
- **Vehicles** are built from real proportions per mode (`VEHICLES`), as one merged,
  vertex-coloured mesh per mode plus an outline hull.
  - The bus is a 12 m solo bus with two axles, a roof AC unit and mirrors.
  - The trolleybus is an 18 m articulated bus with three axles and trolley poles.
  - The tram has four sections on three bogies, plus a pantograph.
  - The metro is three cars on six bogies, with couplers.
  - All get window panes split by pillars, three-part doors on the right-hand side,
    windscreens, destination signs, lights, bumpers, a livery stripe, bellows between sections,
    and hubcaps.
- **Characters**
  - The Dodger has a hoodie with pocket, zip and hood, a backpack with straps, a cap, sneakers
    with soles, hair, ears, eyes with pupils and highlights, brows, nose, mouth and blush.
  - The Bounty Hunter has a cowboy hat with a curled brim and band, a vest with a sheriff
    star, a bandana, a belt buckle, a holster, boots with heels and a moustache.
- **Scale:** people are about 34 px tall at any zoom (was 56). A bus is about 130 px long,
  clamped to 1.4–3.2× real size.
- **Silhouettes:** they are now solid and drawn before the people, so a character never shows
  through their own hat or backpack.
- **Houses**
  - The facade shader adds a stone plinth and ground floors with doors or shopfronts under
    striped awnings.
  - Upper windows get frames, sills, cross bars and a glint. Some houses get shutters or
    balconies with railings, and every house gets storey lines and a cornice with a shadow
    line.
  - Facade details fade to plain walls when far away, so they don't shimmer.
  - Chimneys go on smaller roofs and AC boxes on big flat ones, merged into the house meshes.
- `static/models.html` is a model sheet of every person and vehicle, for design work.

## Addendum (2026-10-28): riders inside, vehicles that bend, faster zoom
After playing the combined game, the owner asked for three things: characters should not
stand on the roof of the vehicle they ride, zooming should be faster, and vehicles should move
less stiffly, bending at their joints.
- **Riders** are hidden inside their vehicle. A pin in their colour floats above the roof
  instead: bigger and bobbing for the player, red for a riding Bounty Hunter. Riders stay
  easy to spot.
- **Articulation:** vehicles are built from rigid sections plus joints (bellows or couplers),
  each with its own origin.
  - Every vehicle records the path it has driven, with its nose at the reported position.
  - Each section is laid along that trail: placed between the trail points under its two
    ends, and facing from one to the other.
  - Joints sit between the sections, at their mean heading.
  - Articulated trolleybuses and trams fold at their bellows, and metro cars at their
    couplers.
- **Suspension:** every section rolls out of curves (with the yaw rate), lifts its nose when
  pulling away and dips it when braking, and jiggles slightly while moving.
- **Zoom:** a wheel notch zooms about 0.6 (was 0.25), and +/− a whole level (was 0.75). The
  camera eases about 90% of the way in a quarter of a second at any frame rate (was 18% per
  frame).
