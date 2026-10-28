// The zoomed-in toy town: a three.js custom layer drawn into the MapLibre map. Houses grow out
// of the vector tiles' building footprints as rounded, toon-shaded blocks with windows; parks
// get blocky trees; vehicles and people are little rounded-cube models with ink outlines.
// Zoomed out, the layer draws nothing and the flat map with DOM markers takes over.
//
// Local frame: metres from the district centre, x east, y north, z up.

import * as THREE from "three";
import { RoundedBoxGeometry } from "three/addons/geometries/RoundedBoxGeometry.js";
import { mergeGeometries } from "three/addons/utils/BufferGeometryUtils.js";

/** Zoom at which the map switches between the flat view and the toy town. */
export const SWITCH_ZOOM = 15.5;
export const MAX_PITCH = 52;

/** Camera tilt for a zoom level: flat when zoomed out, tilting in as you zoom in. */
export function pitchForZoom(zoom) {
  return MAX_PITCH * smoothstep(15, 16.8, zoom);
}

const INK = "#2b2118";
const SKIN = "#ffd6a5";
const MODE_COLORS = { metro: "#7950f2", tram: "#fcc419", trolleybus: "#fd7e14", bus: "#339af0" };
const ROLE_COLORS = {
  dodger: "#40c057", hunter: "#fa5252", team: "#ffa8a8", wanted: "#ffd43b",
};
const WALLS = ["#ffe8c2", "#ffd1b3", "#ffe08a", "#cdeccf", "#cfe3ff", "#f3d1ff", "#ffc4c4",
  "#fff4e0"];
const ROOFS = ["#ea8a66", "#e8775a", "#7395cf", "#5cc2a8", "#f2b25c", "#a98be0", "#ef7d7a",
  "#8cc47a"];
// Real buildings are squashed so the streets between them stay readable from above.
const HEIGHT_SCALE = 0.6;
const MAX_TREES = 10000;
const TREE_SPACING_M = 16;
const REACH_MARGIN_M = 600;    // houses and trees are made this far beyond the play area
const BUILD_BUDGET_MS = 6;     // building geometry made per frame
const CHUNK_SIZE = 250;        // buildings merged into one mesh

const LIGHT = new THREE.Vector3(-0.38, -0.57, 0.73).normalize();

function smoothstep(a, b, x) {
  const t = Math.min(1, Math.max(0, (x - a) / (b - a)));
  return t * t * (3 - 2 * t);
}

function hash(n) {
  const s = Math.sin(n * 127.1 + 311.7) * 43758.5453;
  return s - Math.floor(s);
}

// --- materials and parts -------------------------------------------------------------------

const gradientMap = (() => {
  const tex = new THREE.DataTexture(new Uint8Array([120, 190, 255]), 3, 1, THREE.RedFormat);
  tex.minFilter = tex.magFilter = THREE.NearestFilter;
  tex.needsUpdate = true;
  return tex;
})();

const toonCache = new Map();
function toon(color) {
  if (!toonCache.has(color)) {
    toonCache.set(color, new THREE.MeshToonMaterial({ color, gradientMap }));
  }
  return toonCache.get(color);
}

function outlineMaterial(width) {
  return new THREE.ShaderMaterial({
    uniforms: { uWidth: { value: width }, uColor: { value: new THREE.Color(INK) } },
    vertexShader: `
      uniform float uWidth;
      void main() {
        vec3 p = position + normal * uWidth;
        gl_Position = projectionMatrix * modelViewMatrix * vec4(p, 1.0);
      }`,
    fragmentShader: `
      uniform vec3 uColor;
      void main() {
        gl_FragColor = vec4(uColor, 1.0);
        #include <colorspace_fragment>
      }`,
    side: THREE.BackSide,
  });
}

const OUTLINE_PERSON = outlineMaterial(0.08);
const OUTLINE_VEHICLE = outlineMaterial(0.22);

// Seen through a building: a flat tinted silhouette, drawn only where something is in front.
const xrayCache = new Map();
function xray(color) {
  if (!xrayCache.has(color)) {
    xrayCache.set(color, new THREE.MeshBasicMaterial({
      color, transparent: true, opacity: 0.55, depthFunc: THREE.GreaterDepth, depthWrite: false,
    }));
  }
  return xrayCache.get(color);
}

const geoCache = new Map();
/** Rounded box: x length, y width, z height. */
function rbox(x, y, z, radius = 0.12, segments = 2) {
  const key = `${x},${y},${z},${radius},${segments}`;
  if (!geoCache.has(key)) {
    const r = Math.min(radius, Math.min(x, y, z) / 2 - 0.001);
    geoCache.set(key, new RoundedBoxGeometry(x, y, z, segments, r));
  }
  return geoCache.get(key);
}

function part(parent, geo, color, x, y, z, outline = null, xrayColor = null) {
  const mesh = new THREE.Mesh(geo, toon(color));
  mesh.position.set(x, y, z);
  if (outline) mesh.add(new THREE.Mesh(geo, outline));
  if (xrayColor) {
    const ghost = new THREE.Mesh(geo, xray(xrayColor));
    ghost.renderOrder = 10;
    mesh.add(ghost);
  }
  parent.add(mesh);
  return mesh;
}

function coloured(geo, color, x, y, z) {
  const g = geo.clone().translate(x, y, z);
  const c = new THREE.Color(color);
  const n = g.attributes.position.count;
  const arr = new Float32Array(n * 3);
  for (let i = 0; i < n; i++) arr.set([c.r, c.g, c.b], i * 3);
  g.setAttribute("color", new THREE.BufferAttribute(arr, 3));
  return g;
}

const shadowGeo = new THREE.CircleGeometry(1, 24);
const shadowMat = new THREE.MeshBasicMaterial({
  color: INK, transparent: true, opacity: 0.22, depthWrite: false,
});

function shadow(parent, sx, sy) {
  const m = new THREE.Mesh(shadowGeo, shadowMat);
  m.scale.set(sx, sy, 1);
  m.position.z = 0.04;
  parent.add(m);
  return m;
}

// --- vehicles ------------------------------------------------------------------------------

const VEHICLE_DIMS = {
  // Chunkier than the real thing (about 2.5 m wide) so they read from above.
  bus: [12, 3.4, 3.0], trolleybus: [12, 3.4, 3.0], tram: [22, 3.3, 3.2], metro: [28, 3.5, 3.1],
};

function lighten(hex, amount) {
  return "#" + new THREE.Color(hex).lerp(new THREE.Color("#ffffff"), amount).getHexString();
}

function makeVehicle(mode) {
  const [L, W, H] = VEHICLE_DIMS[mode] || VEHICLE_DIMS.bus;
  const color = MODE_COLORS[mode] || "#888888";
  const root = new THREE.Group();
  const body = new THREE.Group();
  root.add(body);
  shadow(body, L * 0.58, W * 0.85);
  const z0 = 0.45;
  part(body, rbox(L, W, H, 0.55, 3), color, 0, 0, z0 + H / 2, OUTLINE_VEHICLE);
  part(body, rbox(L * 0.93, W + 0.08, H * 0.34, 0.16), "#d6f0ff", 0, 0, z0 + H * 0.64);
  part(body, rbox(L * 0.86, W * 0.8, 0.3, 0.12), lighten(color, 0.45), 0, 0, z0 + H + 0.1);
  const wheel = rbox(1.0, 0.36, 1.0, 0.16);
  for (const x of [-L * 0.33, L * 0.33]) {
    for (const y of [-W / 2, W / 2]) part(body, wheel, INK, x, y, 0.5);
  }
  if (mode === "trolleybus") {
    for (const y of [-0.45, 0.45]) {
      const pole = part(body, rbox(5.2, 0.12, 0.12, 0.05), "#495057", -L * 0.2, y, z0 + H + 1.2);
      pole.rotation.y = -0.38;
    }
  } else if (mode === "tram") {
    part(body, rbox(1.6, 1.2, 0.5, 0.1), "#495057", 0, 0, z0 + H + 0.45);
  }
  return { root, body, roof: z0 + H + 0.3, length: L };
}

// --- people --------------------------------------------------------------------------------

function makePerson(kind) {
  const color = ROLE_COLORS[kind];
  const isHunter = kind === "hunter" || kind === "team";
  const root = new THREE.Group();
  const body = new THREE.Group();
  root.add(body);
  shadow(root, 0.75, 0.75);
  const o = OUTLINE_PERSON;
  const legs = [];
  const arms = [];
  for (const y of [-0.2, 0.2]) {
    const hip = new THREE.Group();
    hip.position.set(0, y, 0.62);
    part(hip, rbox(0.34, 0.3, 0.62, 0.1), "#3b4a6b", 0, 0, -0.31, o);
    body.add(hip);
    legs.push(hip);
    const shoulder = new THREE.Group();
    shoulder.position.set(0, y * 2.85, 1.32);
    part(shoulder, rbox(0.26, 0.22, 0.6, 0.09), color, 0, 0, -0.26, o);
    body.add(shoulder);
    arms.push(shoulder);
  }
  part(body, rbox(0.56, 0.92, 0.82, 0.2, 3), color, 0, 0, 1.03, o, color);
  part(body, rbox(0.76, 0.76, 0.7, 0.22, 3), SKIN, 0, 0, 1.78, o, color);
  for (const y of [-0.16, 0.16]) part(body, rbox(0.06, 0.1, 0.17, 0.03), INK, 0.39, y, 1.82);
  if (isHunter) {
    part(body, rbox(1.2, 1.2, 0.1, 0.05), "#8b5a2b", 0, 0, 2.17, o);
    part(body, rbox(0.64, 0.64, 0.42, 0.14), "#9c6b3c", 0, 0, 2.4, o);
    part(body, rbox(0.66, 0.66, 0.1, 0.04), INK, 0, 0, 2.27);
  } else {
    const cap = kind === "dodger" ? "#2b8a3e" : "#e8590c";
    part(body, rbox(0.82, 0.82, 0.24, 0.1), cap, 0, 0, 2.18, o);
    part(body, rbox(0.42, 0.66, 0.08, 0.04), cap, 0.5, 0, 2.08, o);
  }
  // "!" bubble, above the head and not turning with the body.
  const bubble = new THREE.Group();
  bubble.position.z = 3.2;
  part(bubble, rbox(0.7, 0.3, 0.9, 0.14), "#ffffff", 0, 0, 0, o);
  part(bubble, rbox(0.16, 0.08, 0.44, 0.04), "#e03131", 0, -0.17, 0.12);
  part(bubble, rbox(0.16, 0.08, 0.14, 0.04), "#e03131", 0, -0.17, -0.27);
  bubble.visible = false;
  root.add(bubble);
  return { root, body, legs, arms, bubble, kind };
}

function makeRing(color) {
  const ring = new THREE.Mesh(
    new THREE.RingGeometry(1.0, 1.35, 40),
    new THREE.MeshBasicMaterial({ color, transparent: true, opacity: 0.8, depthWrite: false }),
  );
  ring.position.z = 0.06;
  return ring;
}

// --- trees and stops -----------------------------------------------------------------------

const TREE_GEO = mergeGeometries([
  coloured(rbox(0.55, 0.55, 1.8, 0.12), "#8a5a3b", 0, 0, 0.9),
  coloured(rbox(2.7, 2.7, 2.1, 0.5, 3), "#4fbf5a", 0, 0, 2.6),
  coloured(rbox(1.8, 1.8, 1.3, 0.4, 3), "#7ad66a", 0.15, -0.1, 4.0),
]);

const STOP_GEO = mergeGeometries([
  coloured(rbox(0.2, 0.2, 3.0, 0.06), "#868e96", 0, 0, 1.5),
  coloured(rbox(1.2, 0.18, 0.9, 0.1), "#ffd43b", 0, 0, 3.05),
  coloured(rbox(0.5, 0.2, 0.2, 0.05), "#2f9e44", 0, -0.02, 3.05),
]);

// --- buildings -----------------------------------------------------------------------------

const BUILDING_VERT = `
  attribute vec3 wall;
  attribute vec3 roof;
  attribute float top;
  attribute float seed;
  attribute float inside;
  varying vec3 vWall;
  varying vec3 vRoof;
  varying float vTop;
  varying float vSeed;
  varying float vInside;
  varying vec3 vPos;
  varying vec3 vNormal;
  void main() {
    vWall = wall; vRoof = roof; vTop = top; vSeed = seed; vInside = inside;
    vPos = position; vNormal = normal;
    gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0);
  }`;

const BUILDING_FRAG = `
  uniform vec3 uLight;
  varying vec3 vWall;
  varying vec3 vRoof;
  varying float vTop;
  varying float vSeed;
  varying float vInside;
  varying vec3 vPos;
  varying vec3 vNormal;
  float hash2(vec2 p) { return fract(sin(dot(p, vec2(12.9898, 78.233))) * 43758.5453); }
  void main() {
    vec3 n = normalize(vNormal);
    float roofness = smoothstep(0.45, 0.75, n.z);
    // Roof planks: soft stripes, turned per building.
    float a = vSeed * 0.7;
    float along = dot(vPos.xy, vec2(cos(a), sin(a))) / 1.6;
    float plank = fract(along);
    float far = smoothstep(0.12, 0.35, fwidth(along));   // fade out before they shimmer
    float stripe = smoothstep(0.1, 0.2, plank) * (1.0 - smoothstep(0.85, 0.95, plank));
    vec3 roof = vRoof * mix(0.93 + 0.07 * stripe, 0.965, far);
    vec3 col = mix(vWall, roof, roofness);
    if (abs(n.z) < 0.35) {
      // Windows on a grid in metres along the wall, one row per storey.
      vec2 t = normalize(vec2(-n.y, n.x) + 1e-5);
      vec2 cell = vec2(dot(vPos.xy, t) / 3.2, (vPos.z - 0.4) / 2.8);
      vec2 f = fract(cell);
      vec2 aa = fwidth(cell) * 1.2;
      float wx = smoothstep(0.3 - aa.x, 0.3 + aa.x, f.x) * (1.0 - smoothstep(0.7 - aa.x, 0.7 + aa.x, f.x));
      float wy = smoothstep(0.25 - aa.y, 0.25 + aa.y, f.y) * (1.0 - smoothstep(0.75 - aa.y, 0.75 + aa.y, f.y));
      float rows = step(1.6, vPos.z) * step(vPos.z, vTop - 1.4);
      float lit = step(0.8, hash2(floor(cell) + vSeed));
      vec3 glass = mix(vec3(0.42, 0.7, 0.92), vec3(1.0, 0.86, 0.35), lit);
      float frame = step(0.24, f.x) * step(f.x, 0.76) * step(0.19, f.y) * step(f.y, 0.81) * rows;
      col = mix(col, col * 0.62, frame);
      col = mix(col, glass, wx * wy * rows);
    }
    float ndl = dot(n, uLight);
    col *= ndl > 0.5 ? 1.0 : (ndl > 0.05 ? 0.84 : 0.7);
    col *= mix(0.68, 1.0, smoothstep(0.0, 4.5, vPos.z));   // contact shadow at street level
    float grey = dot(col, vec3(0.299, 0.587, 0.114));
    col = mix(col, vec3(grey) * 0.75, (1.0 - vInside) * 0.6);
    gl_FragColor = vec4(col, 1.0);
    #include <colorspace_fragment>
  }`;

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

function pointInRing(x, y, ring) {
  let inside = false;
  for (let i = 0, j = ring.length - 1; i < ring.length; j = i++) {
    const a = ring[i], b = ring[j];
    if ((a.y > y) !== (b.y > y) && x < ((b.x - a.x) * (y - a.y)) / (b.y - a.y) + a.x) {
      inside = !inside;
    }
  }
  return inside;
}

// --- the layer -----------------------------------------------------------------------------

export class ToyTown {
  constructor(map) {
    this.id = "toy-town";
    this.type = "custom";
    this.renderingMode = "3d";
    this.map = map;
    this.scene = new THREE.Scene();
    this.camera = new THREE.Camera();
    this.origin = null;
    this.world = null;
    this.seen = new Set();          // buildings and tree cells already made
    this.queue = [];
    this.pending = [];
    this.flushedAt = 0;
    this.buildingMeshes = [];
    this.vehicles = new Map();
    this.people = new Map();
    this.clock = new THREE.Clock();
    this.time = 0;

    this.scene.add(new THREE.AmbientLight(0xffffff, 1.5));
    const sun = new THREE.DirectionalLight(0xffffff, 2.2);
    sun.position.copy(LIGHT).multiplyScalar(100);
    this.scene.add(sun);

    this.buildingMaterial = new THREE.ShaderMaterial({
      uniforms: { uLight: { value: LIGHT } },
      vertexShader: BUILDING_VERT,
      fragmentShader: BUILDING_FRAG,
    });
    this.town = new THREE.Group();    // buildings, trees and stops: they grow with zoom
    this.scene.add(this.town);
    this.trees = new THREE.InstancedMesh(TREE_GEO,
      new THREE.MeshToonMaterial({ vertexColors: true, gradientMap }), MAX_TREES);
    this.trees.count = 0;
    this.trees.frustumCulled = false;
    this.town.add(this.trees);
    this.stops = null;
    this.actors = new THREE.Group();  // vehicles and people
    this.scene.add(this.actors);
  }

  onAdd(map, gl) {
    this.renderer = new THREE.WebGLRenderer({ canvas: map.getCanvas(), context: gl, antialias: true });
    this.renderer.autoClear = false;
    // The layer repaints every frame, so the map never goes idle: scan shortly after tiles land.
    map.on("sourcedata", (ev) => {
      if (ev.sourceId !== "omt" || !ev.tile) return;
      clearTimeout(this.scanTimer);
      this.scanTimer = setTimeout(() => this.scan(), 300);
    });
  }

  // Local metres for a [lat, lon].
  local(lat, lon) {
    const m = maplibregl.MercatorCoordinate.fromLngLat([lon, lat]);
    return new THREE.Vector2((m.x - this.origin.x) / this.unit, -(m.y - this.origin.y) / this.unit);
  }

  setDistrict(world) {
    this.world = world;
    const [lat, lon] = world.center;
    this.origin = maplibregl.MercatorCoordinate.fromLngLat([lon, lat], 0);
    this.unit = this.origin.meterInMercatorCoordinateUnits();
    this.originMatrix = new THREE.Matrix4()
      .makeTranslation(this.origin.x, this.origin.y, this.origin.z)
      .scale(new THREE.Vector3(this.unit, -this.unit, this.unit));
    this.area = this.areaTest(world.area);
    this.focus = new THREE.Vector2();

    for (const mesh of this.buildingMeshes) { this.town.remove(mesh); mesh.geometry.dispose(); }
    this.buildingMeshes = [];
    this.queue = [];
    this.pending = [];
    this.seen.clear();
    this.trees.count = 0;
    for (const rec of [...this.vehicles.values(), ...this.people.values()]) {
      this.actors.remove(rec.root);
    }
    this.vehicles.clear();
    this.people.clear();

    if (this.stops) this.town.remove(this.stops);
    this.stops = new THREE.InstancedMesh(STOP_GEO,
      new THREE.MeshToonMaterial({ vertexColors: true, gradientMap }), world.stations.length);
    this.stopPos = world.stations.map((st) => this.local(st.pos[0], st.pos[1]));
    this.stopScale = 0;
    this.stops.frustumCulled = false;
    this.town.add(this.stops);
    this.scan();
  }

  // Point-in-area test in local metres; also sets how far from the centre to build.
  areaTest(area) {
    if (area.kind === "polygon") {
      const ring = area.vertices.map(([lat, lon]) => this.local(lat, lon));
      this.reach = Math.max(...ring.map((v) => v.length())) + REACH_MARGIN_M;
      return (x, y) => pointInRing(x, y, ring);
    }
    if (area.kind === "circle") {
      const c = this.local(area.center_lat, area.center_lon);
      this.reach = c.length() + area.radius_m + REACH_MARGIN_M;
      return (x, y) => Math.hypot(x - c.x, y - c.y) <= area.radius_m;
    }
    const lo = this.local(area.min_lat, area.min_lon);
    const hi = this.local(area.max_lat, area.max_lon);
    this.reach = Math.max(lo.length(), hi.length()) + REACH_MARGIN_M;
    return (x, y) => x >= lo.x && x <= hi.x && y >= lo.y && y <= hi.y;
  }

  near([lon, lat]) {
    return this.local(lat, lon).length() <= this.reach;
  }

  // Pick up building and park features from the tiles loaded so far.
  scan() {
    if (!this.origin || this.map.getZoom() < 13.5) return;
    for (const f of this.map.querySourceFeatures("omt", { sourceLayer: "building" })) {
      const p = f.properties;
      if (p.hide_3d || (p.render_min_height || 0) > 0) continue;
      const polys = f.geometry.type === "Polygon" ? [f.geometry.coordinates]
        : f.geometry.type === "MultiPolygon" ? f.geometry.coordinates : [];
      for (const poly of polys) {
        const first = poly[0][0];
        if (!this.near(first)) continue;
        const key = `b${f.id}:${first[0].toFixed(6)},${first[1].toFixed(6)}:${poly[0].length}`;
        if (this.seen.has(key)) continue;
        this.seen.add(key);
        const at = this.local(first[1], first[0]);
        this.queue.push({ poly, at, height: p.render_height || 10, id: Number(f.id) || 0 });
      }
    }
    // Nearest last, as the builder pops from the end.
    this.queue.sort((a, b) => b.at.distanceTo(this.focus) - a.at.distanceTo(this.focus));
    for (const layer of ["park", "landcover"]) {
      for (const f of this.map.querySourceFeatures("omt", { sourceLayer: layer })) {
        const cls = f.properties.class;
        if (layer === "landcover" && cls !== "wood" && cls !== "grass") continue;
        const polys = f.geometry.type === "Polygon" ? [f.geometry.coordinates]
          : f.geometry.type === "MultiPolygon" ? f.geometry.coordinates : [];
        for (const poly of polys) {
          if (this.near(poly[0][0])) this.plantTrees(poly, cls === "grass" ? 0.35 : 0.85);
        }
      }
    }
  }

  plantTrees(poly, density) {
    if (this.trees.count >= MAX_TREES) return;
    const ring = poly[0].map(([lon, lat]) => this.local(lat, lon));
    const holes = poly.slice(1).map((h) => h.map(([lon, lat]) => this.local(lat, lon)));
    let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
    for (const v of ring) {
      x0 = Math.min(x0, v.x); y0 = Math.min(y0, v.y); x1 = Math.max(x1, v.x); y1 = Math.max(y1, v.y);
    }
    const m = new THREE.Matrix4();
    const q = new THREE.Quaternion();
    const color = new THREE.Color();
    for (let i = Math.floor(x0 / TREE_SPACING_M); i <= x1 / TREE_SPACING_M; i++) {
      for (let j = Math.floor(y0 / TREE_SPACING_M); j <= y1 / TREE_SPACING_M; j++) {
        if (this.trees.count >= MAX_TREES) break;
        const key = `t${i},${j}`;
        if (this.seen.has(key)) continue;
        const h = hash(i * 7919 + j * 104729);
        if (h > density) continue;
        const x = (i + 0.2 + 0.6 * hash(h * 13 + 1)) * TREE_SPACING_M;
        const y = (j + 0.2 + 0.6 * hash(h * 17 + 2)) * TREE_SPACING_M;
        if (!pointInRing(x, y, ring) || holes.some((hole) => pointInRing(x, y, hole))) continue;
        this.seen.add(key);
        const s = 1.1 + 0.5 * hash(h * 23 + 3);
        q.setFromAxisAngle(new THREE.Vector3(0, 0, 1), hash(h * 29) * Math.PI);
        m.compose(new THREE.Vector3(x, y, 0), q, new THREE.Vector3(s, s, s));
        const k = this.trees.count++;
        this.trees.setMatrixAt(k, m);
        this.trees.setColorAt(k, color.setHSL(0.0, 0, 0.85 + 0.15 * hash(h * 31)));
      }
    }
    this.trees.instanceMatrix.needsUpdate = true;
    if (this.trees.instanceColor) this.trees.instanceColor.needsUpdate = true;
  }

  // Turn queued footprints into rounded blocks, a few milliseconds per frame.
  buildSome() {
    const t0 = performance.now();
    while (this.queue.length && performance.now() - t0 < BUILD_BUDGET_MS) {
      const geo = this.makeBuilding(this.queue.pop());
      if (geo) this.pending.push(geo);
      if (this.pending.length >= CHUNK_SIZE) this.flush();
    }
    // Show what is ready every so often, so the nearest houses appear first.
    if (this.pending.length && (!this.queue.length || t0 - this.flushedAt > 400)) this.flush();
  }

  flush() {
    this.flushedAt = performance.now();
    const merged = mergeGeometries(this.pending, false);
    for (const g of this.pending) g.dispose();
    this.pending = [];
    if (!merged) return;
    merged.computeBoundingSphere();
    const mesh = new THREE.Mesh(merged, this.buildingMaterial);
    this.buildingMeshes.push(mesh);
    this.town.add(mesh);
  }

  makeBuilding({ poly, height, id }) {
    const ring = poly[0].slice(0, -1).map(([lon, lat]) => this.local(lat, lon));
    if (ring.length < 3) return null;
    const area = ringArea(ring);
    if (area < 12) return null;
    const h = Math.min(26, Math.max(4.5, height * HEIGHT_SCALE));
    const bevel = Math.min(0.9, Math.sqrt(area) * 0.07);
    const shape = new THREE.Shape(roundRing(ring, 1.6));
    for (const hole of poly.slice(1)) {
      const pts = hole.slice(0, -1).map(([lon, lat]) => this.local(lat, lon));
      if (pts.length >= 3) shape.holes.push(new THREE.Path(pts));
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
    const n = geo.attributes.position.count;
    if (!n) { geo.dispose(); return null; }
    const c = ring[0];
    const seed = hash(c.x * 0.37 + c.y * 1.71 + id * 0.013);
    const wall = new THREE.Color(WALLS[Math.floor(seed * WALLS.length)]);
    const roof = new THREE.Color(ROOFS[Math.floor(hash(seed * 91) * ROOFS.length)]);
    const inside = this.area(c.x, c.y) ? 1 : 0;
    const attr = (size, values) => {
      const arr = new Float32Array(n * size);
      for (let i = 0; i < n; i++) arr.set(values, i * size);
      return new THREE.BufferAttribute(arr, size);
    };
    geo.setAttribute("wall", attr(3, [wall.r, wall.g, wall.b]));
    geo.setAttribute("roof", attr(3, [roof.r, roof.g, roof.b]));
    geo.setAttribute("top", attr(1, [h]));
    geo.setAttribute("seed", attr(1, [seed * 100]));
    geo.setAttribute("inside", attr(1, [inside]));
    return geo;
  }

  // --- actors ----------------------------------------------------------------------------

  /** Positions from a server snapshot; drawn smoothly between snapshots. */
  update(s) {
    if (!this.origin) return;
    this.focus = this.local(s.player.pos[0], s.player.pos[1]);
    const seen = new Set();
    for (const [trip, lat, lon, mode] of s.vehicles) {
      seen.add(trip);
      let rec = this.vehicles.get(trip);
      if (!rec) {
        rec = { ...makeVehicle(mode), heading: null };
        this.vehicles.set(trip, rec);
        this.actors.add(rec.root);
      }
      this.retarget(rec, this.local(lat, lon));
      rec.mine = s.ride && s.ride.trip === trip;
    }
    for (const [trip, rec] of this.vehicles) {
      if (!seen.has(trip)) { this.actors.remove(rec.root); this.vehicles.delete(trip); }
    }

    const people = [];
    const role = s.role;
    people.push({ key: "player", kind: role, pos: s.player.pos, state: s.player.state,
      player: true });
    s.hunters.forEach((h, i) => people.push({
      key: `hunter${i}`, kind: role === "hunter" ? "team" : "hunter", pos: h.pos,
      state: h.riding ? "ride" : "walk", alert: h.chasing,
    }));
    if (s.dodger) {
      people.push({ key: "dodger", kind: "wanted", pos: s.dodger.pos,
        state: s.dodger.held ? "held" : "walk", alert: true });
    }
    const keys = new Set();
    for (const p of people) {
      keys.add(p.key);
      let rec = this.people.get(p.key);
      if (rec && rec.kind !== p.kind) { this.actors.remove(rec.root); rec = null; }
      if (!rec) {
        rec = { ...makePerson(p.kind), heading: null };
        if (p.player) {
          rec.ring = makeRing(ROLE_COLORS[p.kind]);
          rec.root.add(rec.ring);
        }
        this.people.set(p.key, rec);
        this.actors.add(rec.root);
      }
      this.retarget(rec, this.local(p.pos[0], p.pos[1]));
      rec.state = p.state;
      rec.bubble.visible = !!p.alert;
      rec.trip = p.player && s.ride ? s.ride.trip : null;
    }
    for (const [key, rec] of this.people) {
      if (!keys.has(key)) { this.actors.remove(rec.root); this.people.delete(key); }
    }
  }

  retarget(rec, target) {
    if (!rec.target || rec.target.distanceTo(target) > 400) {
      rec.pos = target.clone();          // new, or teleported: no sliding across the map
    } else {
      const dx = target.x - rec.target.x, dy = target.y - rec.target.y;
      if (dx * dx + dy * dy > 0.04) rec.heading = Math.atan2(dy, dx);
    }
    rec.target = target;
  }

  animate(dt, zoom) {
    const mpp = (40075016.686 * Math.cos((this.world.center[0] * Math.PI) / 180)) /
      (512 * 2 ** zoom);
    const personK = Math.max(1, (mpp * 56) / 2.2);   // about 56 px tall at any zoom
    const vehicleK = Math.min(6, Math.max(1, (mpp * 48) / 12));
    const follow = 1 - Math.exp(-dt * 12);
    const turn = 1 - Math.exp(-dt * 8);
    const t = this.time;

    const step = (rec) => {
      const before = rec.pos.clone();
      rec.pos.lerp(rec.target, follow);
      rec.speed = before.distanceTo(rec.pos) / Math.max(dt, 1e-3);
      if (rec.heading !== null) {
        let d = rec.heading - rec.body.rotation.z;
        d = Math.atan2(Math.sin(d), Math.cos(d));
        rec.body.rotation.z += d * turn;
      }
      rec.root.position.set(rec.pos.x, rec.pos.y, 0);
    };

    for (const rec of this.vehicles.values()) {
      step(rec);
      rec.root.scale.setScalar(vehicleK);
      rec.body.position.z = rec.mine ? Math.abs(Math.sin(t * 6)) * 0.25 : 0;
    }
    for (const rec of this.people.values()) {
      step(rec);
      const riding = rec.state === "ride";
      const mine = rec.trip !== null && this.vehicles.get(rec.trip);
      if (mine) rec.root.position.set(mine.pos.x, mine.pos.y, mine.roof * vehicleK);
      else if (riding) rec.root.position.z = 3.6 * vehicleK;
      rec.root.scale.setScalar(personK);
      const walking = !riding && rec.speed > 0.4;
      const swing = walking ? Math.sin(t * 11) * 0.6 : 0;
      rec.legs[0].rotation.y = swing;
      rec.legs[1].rotation.y = -swing;
      rec.arms[0].rotation.y = -swing * 0.8;
      rec.arms[1].rotation.y = swing * 0.8;
      rec.body.position.z = walking ? Math.abs(Math.sin(t * 11)) * 0.12 : 0;
      rec.body.rotation.x = rec.state === "held" ? Math.sin(t * 9) * 0.18 : 0;
      rec.bubble.position.z = 3.2 + Math.sin(t * 5) * 0.12;
      if (rec.ring) {
        const pulse = (t * 0.8) % 1;
        rec.ring.scale.setScalar(1 + pulse * 1.2);
        rec.ring.material.opacity = 0.85 * (1 - pulse);
        rec.ring.visible = !riding;
      }
    }

    if (this.stops && Math.abs(this.stopScale - personK) > 0.05) {
      this.stopScale = personK;
      const s = Math.max(1, personK * 0.5);
      const m = new THREE.Matrix4();
      this.stopPos.forEach((p, i) => {
        m.makeScale(s, s, s).setPosition(p.x, p.y, 0);
        this.stops.setMatrixAt(i, m);
      });
      this.stops.instanceMatrix.needsUpdate = true;
    }
  }

  render(gl, matrix) {
    const dt = Math.min(0.1, this.clock.getDelta());
    this.time += dt;
    const zoom = this.map.getZoom();
    if (!this.origin || zoom < SWITCH_ZOOM - 0.5) return;
    this.buildSome();
    const grow = smoothstep(SWITCH_ZOOM - 0.5, SWITCH_ZOOM + 0.6, zoom);
    this.town.scale.z = Math.max(grow, 0.001);
    this.actors.visible = zoom >= SWITCH_ZOOM;
    this.animate(dt, zoom);

    this.camera.projectionMatrix = new THREE.Matrix4().fromArray(matrix)
      .multiply(this.originMatrix);
    this.camera.projectionMatrixInverse.copy(this.camera.projectionMatrix).invert();
    this.renderer.resetState();
    this.renderer.render(this.scene, this.camera);
    this.map.triggerRepaint();
  }
}
