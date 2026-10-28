# 0010. Main menu, round flow, minimap, ping arrow, the ride switch and open ground

- Status: accepted
- Date: 2026-10-28

## Context
After playing the 3D build, the owner asked for nine things:
1. vehicles that look right when they appear;
2. a main menu;
3. a round timer;
4. parks walkable all across, not only along their paths;
5. a minimap: Bounty Hunters see the reports on it, while the Dodger's map must not show the
   hunters;
6. arrows at the screen edge pointing hunters towards the reports;
7. the hunter team always two players larger than the runners ("2-2 → +2 bots");
8. a way to cancel getting off;
9. a clear red/blue toggle switch, bottom right, for getting on and off.

## Decision
- **Spawning vehicles:** each vehicle's heading comes from the timetable path. A standing
  vehicle faces the hop it leaves on, or the one it came in on at its last stop. The heading
  is sent in the snapshot (`vehicles[i][5]`, radians counter-clockwise from east). A new
  vehicle starts facing it, straight along its track, and pops in over 0.35 s. Before this,
  new vehicles appeared facing east, folded wrongly, and swung round.
- **Main menu:** the page opens on a menu over the first district's map, with two buttons:
  start as the Dodger, or start as a Bounty Hunter. The choice is passed as `/ws?role=…`,
  overriding `start_role`. **Esc** leaves a match (after a confirmation), and the end screen
  of a match has a "Main menu" button.
- **Round flow:**
  - A CS-style top bar shows your score, the round timer in real minutes (red under 30 s) and
    the bots' score.
  - Each round starts with a `freeze_s` (3 s) countdown, showing the round, your side and the
    district.
  - During the countdown the server doesn't advance the round and ignores actions other than
    movement keys.
- **Minimap** (top right):
  - It shows the district, its lines, stations and cafés.
  - The Dodger sees only themselves on it, never the Bounty Hunters.
  - A Bounty Hunter also sees their teammates, the Dodger when spotted, and the latest report
    as a circle that grows and fades with its age.
- **Ping arrow:** for a Bounty Hunter, when the latest report is off screen, a red arrow with
  the distance sits at the screen edge, pointing towards it. It stays inside a frame clear of
  the top bar and the bottom controls.
- **Team sizes:** `n_hunters` is now derived as one Dodger plus `hunter_extra` (2). Bots fill
  the Bounty Hunter places, so when teams come the hunters stay two ahead.
- **Getting off:** pressing **E** while riding asks to get off; pressing it again cancels
  ("You stay on").
- **The switch** (bottom right) mirrors E and can be clicked:
  - grey "GET ON" at a stop;
  - blue "GETTING ON" while waiting, and "ON BOARD" while riding;
  - red "GETTING OFF" once you have asked to get off, or locked red when the inspectors take
    you off.
- **Open ground** (`live/parks.py`):
  - **Data:** public parks, gardens, playgrounds, commons, lawns, village greens,
    recreation grounds and pedestrian squares are downloaded once from Overpass (`out geom`)
    and cached as `data/osm/parks.json`.
  - **Which shapes:** closed ways, and the closed outer rings of relations. Holes are
    ignored, and shapes under 150 m² are dropped.
  - **Per district:** each district keeps the shapes with a corner inside it, from 52 to 251.
  - **Walking:** on open ground you walk in any direction. From a street you step onto open
    ground straight ahead, even across a pavement up to 12 m wide. Leaving it, you step back
    onto a street within 15 m, or slide along the edge.
  - **Bots** still walk the street graph.
- **Snapshot coalescing:** the client draws only the newest snapshot on each animation
  frame, while keeping every snapshot's messages. A slow frame used to build a queue of stale
  snapshots, and the server, waiting on the socket, fell behind real time with it.

## Consequences
- Cafés and parks are optional layers: without Overpass, the game starts without them.
- The Dodger's minimap gives no information that the main view doesn't.
- Bots can't cut across parks yet, so a Dodger who does gains a little. Revisit with
  playtests.
