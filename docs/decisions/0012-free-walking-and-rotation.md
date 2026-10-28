# 0012. Free walking around buildings, and a map you can turn

- Status: accepted (supersedes the street steering of ADR 0007 and the open ground of ADR 0010)
- Date: 2026-10-29

## Context
From the first online test, the owner reported:
- the controls felt wonky;
- crossing streets and rail lines was buggy;
- the walkable edges of houses and parks felt wrong.

They asked for three changes:
- characters walk anywhere except through solid buildings;
- the map can be rotated with right-click;
- W and S move up and down on the screen, and two keys can be held at once for a diagonal.

## Decision
- **Buildings, not streets, limit walking** (`live/buildings.py`).
  - **Data:** OpenStreetMap footprints (`building=*` ways and multipolygon relations, with
    outer rings stitched from their member ways) are downloaded once from Overpass and cached
    as `data/osm/buildings.json`: 27,117 footprints, about 6 MB cached.
  - **What doesn't block:** canopies and shelters (`building=roof`), stations, ruins,
    construction sites and anything underground.
  - **Per district:** each district keeps the footprints with a corner inside it, 1,400 to
    3,900, with a 50 m grid index. A footprint that contains a station, a stop point, a
    platform or a café doesn't block, so those can always be reached.
- **Walking:**
  - A player walks straight along their direction inside the play area. If the step would
    end inside a building, it tries turns of 0.6 and 1.2 radians either way, at the matching
    share of the step, so a player slides along walls instead of stopping.
  - A player who starts inside a footprint can walk out of it.
  - The street graph now only routes the bots (Bounty Hunters chasing and walking, the bot
    Dodger heading for stops). The street-steering of ADR 0007 and the park rules of
    ADR 0010 are gone. `--streets/--free-walk` became `--osm/--no-osm`.
- **Controls follow the screen:**
  - Key messages carry the map's bearing, and the server turns the screen-space key
    direction into a map direction. W is always the top of the screen, and two keys make a
    diagonal.
  - The client resends the keys while the map turns under held keys.
  - **Right-drag** (or Ctrl+drag) rotates the map, and **N** turns it back to north.
- **Gestures:** the camera loop (zoom easing, tilt, follow) pauses while a mouse button or
  finger is down on the map. Its `jumpTo` had been stopping MapLibre's gestures before they
  started, which is why rotation could not work.

## Consequences
- The 3D houses come from the same OpenStreetMap footprints (through the vector tiles), so
  the walls match what players see.
- Bot hunters still keep to streets, so a Dodger cutting across a park or a square gains a
  little. Bot routes through open ground could be added later.
- Serving memory grows by about 75 MiB for the footprints, to about 300 MiB in all.
