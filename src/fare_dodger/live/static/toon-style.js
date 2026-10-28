// Cartoon MapLibre style on OpenMapTiles-schema vector tiles. The same palette is used flat
// (zoomed out) and as the ground under the 3D toy town (zoomed in, see world3d.js).

export const PALETTE = {
  ink: "#2b2118",
  land: "#fbf1d6",
  residential: "#f7e6c0",
  park: "#8fdc6a",
  parkEdge: "#5fb444",
  wood: "#6cc95a",
  water: "#5ec8f8",
  waterEdge: "#2f9fd8",
  road: "#ffffff",
  roadEdge: "#d9c49a",
  major: "#fff1a8",
  majorEdge: "#e2b65a",
  path: "#f1dcae",
  rail: "#a08a6c",
  building: "#f4c79c",
  buildingEdge: "#d39b6a",
  pitch: "#b8eb8c",
  label: "#4a3a2a",
  halo: "#fffaf0",
};

const P = PALETTE;
const FONT = ["Noto Sans Bold"];
const classIn = (classes) => ["in", ["get", "class"], ["literal", classes]];
// Road width in pixels, doubling per zoom level like the real road does on screen.
const width = (base) => ["interpolate", ["exponential", 1.6], ["zoom"],
  12, base * 0.5, 15, base * 2, 18, base * 9];

export function toonStyle(tilesUrl, glyphsUrl) {
  return {
    version: 8,
    glyphs: glyphsUrl,
    sources: { omt: { type: "vector", url: tilesUrl } },
    layers: [
      { id: "land", type: "background", paint: { "background-color": P.land } },
      {
        id: "residential", type: "fill", source: "omt", "source-layer": "landuse",
        filter: classIn(["residential", "suburb", "neighbourhood"]),
        paint: { "fill-color": P.residential },
      },
      {
        id: "pitch", type: "fill", source: "omt", "source-layer": "landuse",
        filter: classIn(["pitch", "playground", "stadium"]),
        paint: { "fill-color": P.pitch },
      },
      {
        id: "green", type: "fill", source: "omt", "source-layer": "landcover",
        filter: classIn(["grass", "wood", "farmland"]),
        paint: { "fill-color": ["match", ["get", "class"], "wood", P.wood, P.park] },
      },
      {
        id: "park", type: "fill", source: "omt", "source-layer": "park",
        paint: { "fill-color": P.park, "fill-outline-color": P.parkEdge },
      },
      {
        id: "water", type: "fill", source: "omt", "source-layer": "water",
        filter: ["!=", ["get", "brunnel"], "tunnel"],
        paint: { "fill-color": P.water },
      },
      {
        id: "water-edge", type: "line", source: "omt", "source-layer": "water",
        filter: ["!=", ["get", "brunnel"], "tunnel"],
        paint: { "line-color": P.waterEdge, "line-width": 2 },
      },
      {
        id: "path", type: "line", source: "omt", "source-layer": "transportation",
        filter: ["all", classIn(["path", "track"]), ["!=", ["get", "brunnel"], "tunnel"]],
        minzoom: 15,
        paint: { "line-color": P.path, "line-width": width(1.2), "line-dasharray": [2, 1] },
      },
      ...roadPair("minor", ["minor", "service"], P.road, P.roadEdge, 3),
      ...roadPair("major", ["primary", "secondary", "tertiary", "trunk", "motorway"],
        P.major, P.majorEdge, 5),
      {
        id: "rail", type: "line", source: "omt", "source-layer": "transportation",
        filter: ["all", classIn(["rail", "transit"]), ["!=", ["get", "brunnel"], "tunnel"]],
        paint: { "line-color": P.rail, "line-width": 2, "line-dasharray": [3, 2] },
      },
      {
        // Flat footprints: what you see zoomed out, and the floor plan the 3D houses grow from.
        id: "building", type: "fill", source: "omt", "source-layer": "building",
        minzoom: 13,
        paint: {
          "fill-color": P.building, "fill-outline-color": P.buildingEdge,
          "fill-opacity": ["interpolate", ["linear"], ["zoom"], 13, 0, 14, 1],
        },
      },
      {
        id: "street-names", type: "symbol", source: "omt", "source-layer": "transportation_name",
        minzoom: 15,
        layout: {
          "symbol-placement": "line", "text-field": ["get", "name"], "text-font": FONT,
          "text-size": 12, "text-max-angle": 30,
        },
        paint: { "text-color": P.label, "text-halo-color": P.halo, "text-halo-width": 2 },
      },
      {
        id: "places", type: "symbol", source: "omt", "source-layer": "place",
        filter: classIn(["suburb", "quarter", "neighbourhood"]),
        maxzoom: 15.5,
        layout: {
          "text-field": ["get", "name"], "text-font": FONT, "text-size": 14,
          "text-transform": "uppercase", "text-letter-spacing": 0.1,
        },
        paint: { "text-color": P.label, "text-halo-color": P.halo, "text-halo-width": 2.5 },
      },
    ],
  };
}

function roadPair(id, classes, fill, edge, base) {
  const filter = ["all", classIn(classes), ["!=", ["get", "brunnel"], "tunnel"]];
  const common = { type: "line", source: "omt", "source-layer": "transportation", filter,
    layout: { "line-cap": "round", "line-join": "round" } };
  return [
    { ...common, id: `${id}-edge`, paint: { "line-color": edge, "line-width": width(base + 1.5) } },
    { ...common, id, paint: { "line-color": fill, "line-width": width(base) } },
  ];
}
