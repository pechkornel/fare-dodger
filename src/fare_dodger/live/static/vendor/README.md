# Vendored front-end files

Served by the game itself, so it needs no third-party CDN at runtime (the map tiles and glyphs
still come from OpenFreeMap, see `tile_url` in `configs/live.toml`).

| Directory | What | Version | Licence | Source |
|---|---|---|---|---|
| `maplibre-gl/` | MapLibre GL JS (`dist/maplibre-gl.js`, `.css`) | 4.7.1 | BSD-3-Clause (`LICENSE.txt`) | https://unpkg.com/maplibre-gl@4.7.1/ |
| `three/` | three.js (`build/three.module.js`) and the add-ons the game imports (`RoundedBoxGeometry`, `BufferGeometryUtils`) | 0.169.0 | MIT (`LICENSE`) | https://cdn.jsdelivr.net/npm/three@0.169.0/ |
| `fredoka/` | Fredoka variable font, Latin and Latin Extended subsets (woff2) | Google Fonts v17 | SIL OFL 1.1 (`OFL.txt`) | https://fonts.google.com/specimen/Fredoka |

To update a library, download the same files for the new version, keep the paths, and update
this table.
