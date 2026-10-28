# 0001. Merge platforms into station nodes

- Status: accepted
- Date: 2026-10-28

## Context
The game is played on stations ("I'm at Deák Ferenc tér"), but GTFS models platforms: the BKK
feed has 6,300 stops, often 2–18 per physical place. Players must not be able to hide on
the far platform of the same square. They also need one obvious name per place.

What the BKK feed (version 2821.20261028) looks like:

- Only ~10% of platforms have a `parent_station`, so the hierarchy alone is not enough.
- Same names recur across the city. "Ady Endre utca" exists 16 times, up to 25 km apart, and 358
  name groups span more than 150 m. Merging by name alone would teleport players.
- Metro platforms carry the bare name ("Keleti pályaudvar"), while the surface stops next to
  them carry a rail suffix ("Keleti pályaudvar M", "… H", "… M+H"). Deep metro platforms can
  be 200–300 m from those surface stops.

## Decision
Platforms are merged with a union-find over two rules:

1. Platforms that share a `parent_station` belong together.
2. Platforms whose *station key* (the name with a trailing ` M`, ` H` or ` M+H` removed) is
   equal belong together if they are within `merge_radius_m` (150 m). If either platform is
   served by metro or suburban rail (or has a parent), the radius is `station_merge_radius_m`
   (300 m).

The relation is transitive, so chains of close same-name platforms merge. The node's name is
the most frequent station key, and its position is the mean of its platforms.
Entrances, boarding areas and generic nodes (`location_type` ≠ 0) are ignored. Nodes are
kept only if a vehicle still serves them after clipping to the area, and only the largest
strongly connected component is kept.

Walking transfers (≤ 400 m at 5 km/h, measured between the closest platforms of two nodes)
connect nodes that remain separate.

## Consequences
- On the default area (r = 4.5 km around Deák Ferenc tér), 1,200+ platforms become 446
  nodes. Széll Kálmán tér (18 platforms), Keleti pályaudvar (17) and Deák Ferenc tér (15) each
  became a single node.
- 14 names still map to two or three nodes 150–490 m apart (e.g. "Bajza utca"). These are
  separate surface stops joined by walking edges. That is acceptable: they are different
  places to stand.
- A merged node gets a single change time (60 s, or 120 s if it has a metro platform), so
  long interchange walks inside a big station are approximated. `pathways.txt` is not used.
- The rules and radii are configuration and the cache key covers them, so changing them simply
  rebuilds the network.
