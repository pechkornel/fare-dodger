// Builds the toy town's houses off the main thread (see houses.js): the page sends batches
// of footprints in local metres, the worker sends back merged arrays per map square.
import { Batcher, cellOf, makeBuilding, propGeometry } from "./houses.js";

let props = null;
let gen = -1;   // the district the houses are for; older batches are dropped
const batcher = new Batcher((arrays) => {
  self.postMessage({ type: "chunk", gen, arrays }, Object.values(arrays).map((a) => a.buffer));
});

self.onmessage = ({ data }) => {
  if (data.type === "props") {
    props = Object.fromEntries(Object.entries(data.props).map(([k, v]) => [k, propGeometry(v)]));
    return;
  }
  if (data.gen !== gen) { batcher.clear(); gen = data.gen; }
  for (const house of data.houses) {
    const geos = makeBuilding(house, props);
    if (geos) batcher.add(cellOf(house.ring[0][0], house.ring[0][1]), geos);
  }
  batcher.tick(data.last);
  self.postMessage({ type: "done", gen });
};
