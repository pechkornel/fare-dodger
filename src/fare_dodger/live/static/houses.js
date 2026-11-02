// House geometry for the toy town: rounded blocks extruded from building footprints, with
// chimneys and air-conditioning boxes on the roofs. Plain three.js core only (no add-ons, no
// map), so the same code runs in the house worker (houses-worker.js) and, as a fallback, on
// the main thread. Footprints come in local metres (x east, y north).

// The same file the page's import map calls "three", so both share one copy of three.js.
import * as THREE from "./vendor/three/three.module.js";

export const WALLS = ["#ffe8c2", "#ffd1b3", "#ffe08a", "#cdeccf", "#cfe3ff", "#f3d1ff", "#ffc4c4",
  "#fff4e0"];
export const ROOFS = ["#ea8a66", "#e8775a", "#7395cf", "#5cc2a8", "#f2b25c", "#a98be0", "#ef7d7a",
  "#8cc47a"];
// Real buildings are squashed so the streets between them stay readable from above.
const HEIGHT_SCALE = 0.6;
/** Houses are merged into one mesh per square of this size (metres), so whole squares off
 * screen are skipped, and at most this many houses go into one mesh. */
export const CELL_M = 300;
export const CHUNK_SIZE = 250;
/** The vertex attributes of a house mesh, and their sizes. */
export const ATTRIBUTES = { position: 3, normal: 3, wall: 3, roof: 3, top: 1, seed: 1, inside: 1 };

export function hash(n) {
  const s = Math.sin(n * 127.1 + 311.7) * 43758.5453;
  return s - Math.floor(s);
}

export const cellOf = (x, y) => `${Math.floor(x / CELL_M)},${Math.floor(y / CELL_M)}`;

/** Replace each sharp corner of a ring with a short curve, so blocks have soft vertical edges. */
function roundRing(ring, radius) {
  const out = [];
  const n = ring.length;
  for (let i = 0; i < n; i++) {
    const p = ring[(i + n - 1) % n];
    const v = ring[i];
    const q = ring[(i + 1) % n];
    const ax = p.x - v.x, ay = p.y - v.y, bx = q.x - v.x, by = q.y - v.y;
    const la = Math.hypot(ax, ay), lb = Math.hypot(bx, by);
    if (la < 1e-6 || lb < 1e-6) continue;
    const cos = (ax * bx + ay * by) / (la * lb);
    if (cos < -0.94) { out.push(v); continue; }   // nearly straight
    const r = Math.min(radius, la * 0.4, lb * 0.4);
    const a = new THREE.Vector2(v.x + (ax / la) * r, v.y + (ay / la) * r);
    const b = new THREE.Vector2(v.x + (bx / lb) * r, v.y + (by / lb) * r);
    out.push(a);
    for (const t of [0.33, 0.67]) {
      const u = 1 - t;
      out.push(new THREE.Vector2(u * u * a.x + 2 * u * t * v.x + t * t * b.x,
        u * u * a.y + 2 * u * t * v.y + t * t * b.y));
    }
    out.push(b);
  }
  return out;
}

function ringArea(ring) {
  let s = 0;
  for (let i = 0, j = ring.length - 1; i < ring.length; j = i++) {
    s += (ring[j].x - ring[i].x) * (ring[j].y + ring[i].y);
  }
  return Math.abs(s) / 2;
}

export function pointInRing(x, y, ring) {
  let inside = false;
  for (let i = 0, j = ring.length - 1; i < ring.length; j = i++) {
    const a = ring[i], b = ring[j];
    if ((a.y > y) !== (b.y > y) && x < ((b.x - a.x) * (y - a.y)) / (b.y - a.y) + a.x) {
      inside = !inside;
    }
  }
  return inside;
}

// Chimneys on smaller roofs, air-conditioning boxes on big flat ones: [prop, x, y, z, side
// colour, cap colour], ``prop`` naming one of PROPS (made by the caller, see world3d.js).
export const PROPS = {
  unit: [2.4, 1.8, 1.0, 0.18], vent: [1.2, 1.2, 0.1, 0.04],
  chimney: [0.8, 0.8, 1.6, 0.12], pot: [1.0, 1.0, 0.22, 0.06],
};

function roofProps(ring, h, seed) {
  const area = ringArea(ring);
  const props = [];
  if (area < 30) return props;
  let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
  for (const v of ring) {
    x0 = Math.min(x0, v.x); y0 = Math.min(y0, v.y); x1 = Math.max(x1, v.x); y1 = Math.max(y1, v.y);
  }
  const spot = (k) => {
    for (let tries = 0; tries < 12; tries++) {
      const x = x0 + (x1 - x0) * hash(seed * 31 + k * 7 + tries);
      const y = y0 + (y1 - y0) * hash(seed * 57 + k * 11 + tries * 3);
      if (pointInRing(x, y, ring)) return [x, y];
    }
    return null;
  };
  const big = area > 250;
  const count = big ? 1 + Math.floor(hash(seed * 3) * 3) : Math.floor(hash(seed * 5) * 3);
  for (let k = 0; k < count; k++) {
    const at = spot(k);
    if (!at) continue;
    const [x, y] = at;
    if (big && k % 2 === 0) {
      props.push(["unit", x, y, h + 0.4, "#dee2e6", "#adb5bd"]);
      props.push(["vent", x, y, h + 0.95, "#868e96", "#868e96"]);
    } else {
      props.push(["chimney", x, y, h + 0.6, "#c8553d", "#c8553d"]);
      props.push(["pot", x, y, h + 1.45, "#5c4033", "#5c4033"]);
    }
  }
  return props;
}

/**
 * One house: ``{ring, holes, height, id, inside}``, the rings as [x, y] pairs (open, in local
 * metres). ``props`` maps each PROPS name to its geometry (non-indexed, position and normal).
 * Returns its geometries, labelled for the house shader, or null for a degenerate footprint.
 */
export function makeBuilding({ ring: points, holes, height, id, inside }, props) {
  const ring = points.map(([x, y]) => new THREE.Vector2(x, y));
  if (ring.length < 3) return null;
  const area = ringArea(ring);
  if (area < 12) return null;
  const h = Math.min(26, Math.max(4.5, height * HEIGHT_SCALE));
  const bevel = Math.min(0.9, Math.sqrt(area) * 0.07);
  const shape = new THREE.Shape(roundRing(ring, 1.6));
  for (const hole of holes) {
    if (hole.length >= 3) shape.holes.push(new THREE.Path(hole.map(([x, y]) => new THREE.Vector2(x, y))));
  }
  let geo;
  try {
    geo = new THREE.ExtrudeGeometry(shape, {
      depth: Math.max(0.5, h - bevel), bevelEnabled: true, bevelThickness: bevel,
      bevelSize: bevel, bevelOffset: -bevel, bevelSegments: 2, curveSegments: 1,
    });
  } catch {
    return null;
  }
  geo.deleteAttribute("uv");
  if (!geo.attributes.position.count) { geo.dispose(); return null; }
  const c = ring[0];
  const seed = hash(c.x * 0.37 + c.y * 1.71 + id * 0.013);
  const wall = new THREE.Color(WALLS[Math.floor(seed * WALLS.length)]);
  const roof = new THREE.Color(ROOFS[Math.floor(hash(seed * 91) * ROOFS.length)]);
  const label = (g, wallColor, roofColor, top) => {
    const n = g.attributes.position.count;
    const attr = (size, values) => {
      const arr = new Float32Array(n * size);
      for (let i = 0; i < n; i++) arr.set(values, i * size);
      return new THREE.BufferAttribute(arr, size);
    };
    g.setAttribute("wall", attr(3, [wallColor.r, wallColor.g, wallColor.b]));
    g.setAttribute("roof", attr(3, [roofColor.r, roofColor.g, roofColor.b]));
    g.setAttribute("top", attr(1, [top]));   // 0: no facade (chimneys and the like)
    g.setAttribute("seed", attr(1, [seed * 100]));
    g.setAttribute("inside", attr(1, [inside ? 1 : 0]));
    return g;
  };
  const geos = [label(geo, wall, roof, h)];
  for (const [name, x, y, z, side, cap] of roofProps(ring, h, seed)) {
    geos.push(label(props[name].clone().translate(x, y, z), new THREE.Color(side),
      new THREE.Color(cap), 0));
  }
  return geos;
}

/** Concatenate non-indexed house geometries into plain arrays, one per attribute. */
export function concat(geos) {
  const n = geos.reduce((sum, g) => sum + g.attributes.position.count, 0);
  const out = {};
  for (const [name, size] of Object.entries(ATTRIBUTES)) {
    const arr = new Float32Array(n * size);
    let at = 0;
    for (const g of geos) {
      arr.set(g.attributes[name].array, at);
      at += g.attributes[name].array.length;
    }
    out[name] = arr;
  }
  for (const g of geos) g.dispose();
  return out;
}

/** A geometry from concat's arrays. */
export function fromArrays(arrays) {
  const g = new THREE.BufferGeometry();
  for (const [name, size] of Object.entries(ATTRIBUTES)) {
    g.setAttribute(name, new THREE.BufferAttribute(arrays[name], size));
  }
  g.computeBoundingSphere();
  return g;
}

/**
 * Collects houses per map square and hands each square's merged arrays to ``emit`` once it
 * holds CHUNK_SIZE houses, or (``tick``) when nothing was handed over for a while, so the
 * nearest houses show up first.
 */
export class Batcher {
  constructor(emit) {
    this.emit = emit;
    this.cells = new Map();
    this.last = 0;
  }

  add(cell, geos) {
    let c = this.cells.get(cell);
    if (!c) { c = { geos: [], houses: 0 }; this.cells.set(cell, c); }
    c.geos.push(...geos);
    if (++c.houses >= CHUNK_SIZE) this.send(cell);
  }

  send(cell) {
    const c = this.cells.get(cell);
    this.cells.delete(cell);
    this.last = performance.now();
    if (c.geos.length) this.emit(concat(c.geos));
  }

  tick(force) {
    if (force || performance.now() - this.last > 400) {
      for (const cell of [...this.cells.keys()]) this.send(cell);
    }
  }

  clear() {
    for (const c of this.cells.values()) for (const g of c.geos) g.dispose();
    this.cells.clear();
  }
}

/** Plain {position, normal} arrays into a geometry (roof props sent to the worker). */
export function propGeometry({ position, normal }) {
  const g = new THREE.BufferGeometry();
  g.setAttribute("position", new THREE.BufferAttribute(position, 3));
  g.setAttribute("normal", new THREE.BufferAttribute(normal, 3));
  return g;
}
