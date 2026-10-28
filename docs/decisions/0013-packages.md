# 0013. Packages: Dodgers earn money by delivering

- Status: accepted
- Date: 2026-10-29

## Context
The owner wants the Dodgers to start with less money and earn more by delivering packages.
A package shows on the map; at it, the Dodger presses E to accept it, the destination is
shown with a ping, and delivering it earns about 1,200 Ft.

## Decision
- **Money:** `start_money_ft` goes from 2,000 to **1,000**. That buys two tickets, or a coffee
  and a ticket, or covers one fine.
- **Waiting packages:** `packages_waiting` (3) wait at random walkable spots. A spot is on a
  street, inside the area, outside every building, and at least 60 m from any stop, so **E**
  never has two meanings. Only the Dodgers see them: on the map, on the minimap, and as a
  "📦 A package · E" hint when they stand within `package_radius_m` (25 m).
- **Accepting:** **E** takes the package (or click its hint). It gets a destination
  `package_min_m` to `package_max_m` (600–1,600 m) away, named after the nearest station, and
  a new package appears elsewhere.
- **Delivering:** the destination is a pulsing 🏁 flag, a green minimap dot, and a green edge
  arrow with the distance when it is off screen. Walking within 25 m of it (on foot) delivers
  the package and pays `package_reward_ft` (1,200 Ft).
- **Rules:**
  - A Dodger carries one package at a time.
  - Getting caught loses the package.
  - The Bounty Hunters never see packages, so delivering is a gamble on exposure, not a
    give-away.
  - Bot Dodgers don't deliver.

## Also fixed with this change
- **Result text per player:** the round's text now comes from per-player summaries
  (`LiveGame.summary`). Before, only the first human seated got the real text, so a
  catcher in a 1v1 read "The Dodger got away" under "Round won!". Catches now record who,
  where (which vehicle) and how (inspectors).
- **Arrows when zoomed in:** with the camera tilted, a far target can be beyond the
  horizon, where `project()` returns nonsense, so the hunters' arrow pointed at random
  places. Edge arrows now take their direction from the map's rotation and the real offset,
  and use the projection only to tell whether the target is on screen, checked by projecting
  back. Package and flag markers that are not really in view are hidden the same way.
