// The zoomed-in toy town: a three.js custom layer drawn into the MapLibre map. Houses grow out
// of the vector tiles' building footprints as rounded, toon-shaded blocks with windows; parks
// get blocky trees; vehicles and people are little rounded-cube models with ink outlines.
// Zoomed out, the layer draws nothing and the flat map with DOM markers takes over.
//
// Local frame: metres from the district centre, x east, y north, z up.

import * as THREE from "three";
import { RoundedBoxGeometry } from "three/addons/geometries/RoundedBoxGeometry.js";
import { mergeGeometries } from "three/addons/utils/BufferGeometryUtils.js";
import { Batcher, PROPS, cellOf, fromArrays, makeBuilding, pointInRing } from "./houses.js";

/** Zoom at which the map switches between the flat view and the toy town. */
export const SWITCH_ZOOM = 15.5;
export const MAX_PITCH = 52;

/** Camera tilt for a zoom level: flat when zoomed out, tilting in as you zoom in. */
export function pitchForZoom(zoom) {
  return MAX_PITCH * smoothstep(15, 16.8, zoom);
}

const INK = "#2b2118";
const SKIN = "#ffd6a5";
const MODE_COLORS = { metro: "#7950f2", tram: "#fcc419", trolleybus: "#fd7e14", bus: "#339af0",
  suburban_rail: "#2f9e44" };
const ROLE_COLORS = {
  dodger: "#40c057", hunter: "#fa5252", team: "#ffa8a8", wanted: "#ffd43b",
};
const MAX_TREES = 10000;
const TREE_SPACING_M = 16;
const REACH_MARGIN_M = 600;    // houses and trees are made this far beyond the play area
// Houses are built in a worker (houses-worker.js); without one, on the main thread for at
// most this long a frame.
const BUILD_BUDGET_MS = 2.5;
const BATCH_HOUSES = 40;       // footprints per message to the worker
// Trees: one instanced mesh per square of this many tree spacings a side, so the squares off
// screen are skipped.
const TREE_CELL = 25;

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

const OUTLINE_PERSON = outlineMaterial(0.07);
const OUTLINE_VEHICLE = outlineMaterial(0.15);

// Seen through a building: a flat silhouette, drawn only where something is in front. Ghosts
// draw before the people themselves (render order 1 and 2), so a person never ghosts through
// their own hat or backpack, only through houses and vehicles.
const xrayCache = new Map();
function xray(color) {
  if (!xrayCache.has(color)) {
    xrayCache.set(color, new THREE.MeshBasicMaterial({
      color: shade(color, 0.35), depthFunc: THREE.GreaterDepth, depthWrite: false,
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

function coloured(geo, color, x, y, z, rotY = 0, rotX = 0) {
  const g = geo.clone();
  if (rotX) g.rotateX(rotX);
  if (rotY) g.rotateY(rotY);
  g.translate(x, y, z);
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

// Real proportions, a little wider so they read from above. Sections are the rigid parts,
// joined by bellows (articulated buses, trams) or couplers (metro cars), front first.
// Axles and bogies are x offsets from their section's centre.
const VEHICLES = {
  bus: { sections: [12], width: 2.7, height: 2.95, floor: 0.4, wheel: 1.0,
    axles: [[3.3, -2.6]], roofBox: true, mirrors: true },
  trolleybus: { sections: [10.6, 7.4], width: 2.7, height: 2.95, floor: 0.4, wheel: 1.0,
    axles: [[2.9, -3.4], [-1.6]], poles: true, mirrors: true },
  tram: { sections: [8, 5.6, 5.6, 8], width: 2.55, height: 3.25, floor: 0.35, wheel: 0.75,
    bogies: [[1.2], [], [0], [-1.2]], pantograph: 2, cabs: 2 },
  metro: { sections: [17, 17, 17], width: 2.8, height: 3.1, floor: 0.95, wheel: 0.85,
    bogies: [[5.8, -5.8], [5.8, -5.8], [5.8, -5.8]], cabs: 2, couplers: true },
  // HÉV: a three-car suburban train, overhead power.
  suburban_rail: { sections: [18, 18, 18], width: 2.85, height: 3.3, floor: 1.0, wheel: 0.85,
    bogies: [[6, -6], [6, -6], [6, -6]], cabs: 2, couplers: true, pantograph: 1 },
};
const GLASS = "#a5d8f3";
const VEHICLE_MAT = new THREE.MeshToonMaterial({ vertexColors: true, gradientMap });

function shade(hex, amount) {
  const target = new THREE.Color(amount > 0 ? "#ffffff" : "#000000");
  return "#" + new THREE.Color(hex).lerp(target, Math.abs(amount)).getHexString();
}

// Per mode: one merged, vertex-coloured mesh and outline hull per rigid section, and one mesh
// per joint (bellows or coupler), each centred on its own origin so it can turn on its own.
const vehicleTemplates = new Map();
function vehicleTemplate(mode) {
  if (vehicleTemplates.has(mode)) return vehicleTemplates.get(mode);
  const spec = VEHICLES[mode] || VEHICLES.bus;
  const color = MODE_COLORS[mode] || "#888888";
  const { width: W, height: H, floor: z0, wheel } = spec;
  const gap = spec.couplers ? 1.1 : 0.7;
  const total = spec.sections.reduce((a, b) => a + b, 0) + gap * (spec.sections.length - 1);
  const sections = [];
  const joints = [];
  let parts = [];   // the bucket being filled: the current section's, or a joint's
  const add = (geo, c, x, y, z, rotY = 0, rotX = 0) => {
    parts.push(coloured(geo, c, x, y, z, rotY, rotX));
  };
  const top = z0 + H;
  const cab = (x, dir) => {
    // A driver's end: windscreen, destination sign, lights and bumper. dir +1 front, -1 back.
    const end = x + dir * 0.02;
    add(rbox(0.12, W * 0.86, H * 0.5, 0.05), GLASS, end, 0, z0 + H * 0.58);
    add(rbox(0.14, W * 0.6, 0.32, 0.05), "#212529", end, 0, z0 + H * 0.9);
    add(rbox(0.16, W * 0.42, 0.11, 0.03), "#ffb000", end + dir * 0.01, 0, z0 + H * 0.9);
    for (const y of [-1, 1]) {
      add(rbox(0.12, 0.42, 0.22, 0.06), "#fff3bf", end, y * (W / 2 - 0.42), z0 + 0.6);
      add(rbox(0.13, 0.2, 0.16, 0.05), "#ff6b6b", end, y * (W / 2 - 0.16), z0 + 0.6);
    }
    add(rbox(0.32, W + 0.06, 0.32, 0.1), "#495057", x, 0, z0 + 0.2);
  };
  let front = total / 2;
  spec.sections.forEach((L, i) => {
    const xc = front - L / 2;
    const body = coloured(rbox(L, W, H, 0.42, 3), color, xc, 0, z0 + H / 2);
    const section = { x: xc, length: L, parts: [body], hull: body };
    sections.push(section);
    parts = section.parts;
    add(rbox(L - 0.1, W + 0.04, 0.45, 0.12), shade(color, -0.3), xc, 0, z0 + 0.25);
    add(rbox(L - 0.3, W + 0.03, 0.12, 0.04), "#f8f9fa", xc, 0, z0 + H * 0.4);
    add(rbox(L * 0.94, W * 0.86, 0.2, 0.08), shade(color, 0.45), xc, 0, top + 0.08);
    // Window band split into panes by pillars.
    const band = L - 0.7;
    add(rbox(band, W + 0.05, H * 0.36, 0.12), GLASS, xc, 0, z0 + H * 0.63);
    const panes = Math.max(2, Math.round(band / 1.7));
    for (let k = 1; k < panes; k++) {
      add(rbox(0.16, W + 0.09, H * 0.36, 0.04), color, xc - band / 2 + (k * band) / panes, 0,
        z0 + H * 0.63);
    }
    // Doors on the right-hand side (-y): front, middle and rear on long sections.
    const doors = L > 9 ? [L / 2 - 1.4, -0.3, -L / 2 + 1.6] : [0];
    if (i > 0 && L > 9) doors.shift();
    for (const dx of doors) {
      const x = xc + dx;
      add(rbox(1.25, 0.08, H * 0.82, 0.05), "#3d5a6c", x, -W / 2 - 0.02, z0 + H * 0.45);
      add(rbox(0.5, 0.1, H * 0.4, 0.04), GLASS, x - 0.31, -W / 2 - 0.03, z0 + H * 0.57);
      add(rbox(0.5, 0.1, H * 0.4, 0.04), GLASS, x + 0.31, -W / 2 - 0.03, z0 + H * 0.57);
    }
    for (const ax of (spec.axles || [])[i] || []) {
      for (const y of [-1, 1]) {
        add(rbox(wheel, 0.42, wheel, 0.3, 3), "#212529", xc + ax, y * (W / 2 - 0.12), wheel / 2);
        add(rbox(wheel * 0.5, 0.06, wheel * 0.5, 0.1), "#ced4da", xc + ax, y * (W / 2 + 0.1),
          wheel / 2);
      }
    }
    for (const bx of (spec.bogies || [])[i] || []) {
      add(rbox(2.7, W * 0.78, 0.4, 0.1), "#343a40", xc + bx, 0, wheel * 0.55);
      for (const ax of [-0.95, 0.95]) {
        for (const y of [-1, 1]) {
          add(rbox(wheel, 0.2, wheel, 0.25, 3), "#495057", xc + bx + ax, y * W * 0.4, wheel / 2);
        }
      }
    }
    if (i === 0) {
      cab(front, 1);
      if (spec.mirrors) {
        for (const y of [-1, 1]) {
          add(rbox(0.55, 0.08, 0.08, 0.03), "#212529", front - 0.1, y * (W / 2 + 0.22), z0 + H * 0.75);
          add(rbox(0.1, 0.16, 0.45, 0.04), "#212529", front - 0.1, y * (W / 2 + 0.48), z0 + H * 0.68);
        }
      }
    }
    const back = front - L;
    if (i === spec.sections.length - 1) {
      if (spec.cabs === 2) cab(back, -1);
      else {
        add(rbox(0.12, W * 0.7, H * 0.3, 0.05), GLASS, back - 0.02, 0, z0 + H * 0.66);
        for (const y of [-1, 1]) {
          add(rbox(0.12, 0.3, 0.34, 0.06), "#e03131", back - 0.02, y * (W / 2 - 0.3), z0 + 0.65);
        }
        add(rbox(0.32, W + 0.06, 0.32, 0.1), "#495057", back, 0, z0 + 0.2);
      }
    } else {
      // Bellows or a coupler to the next section.
      const jx = back - gap / 2;
      const joint = { x: jx, parts: [] };
      joints.push(joint);
      parts = joint.parts;
      if (spec.couplers) {
        add(rbox(gap + 0.3, W * 0.6, H * 0.75, 0.1), "#343a40", jx, 0, z0 + H * 0.45);
        add(rbox(gap + 0.4, 0.4, 0.35, 0.08), "#212529", jx, 0, z0 + 0.25);
      } else {
        add(rbox(gap + 0.35, W * 0.9, H * 0.86, 0.12), "#343a40", jx, 0, z0 + H * 0.47);
        for (const dx of [-0.2, 0, 0.2]) {
          add(rbox(0.06, W * 0.92, H * 0.84, 0.02), "#5c636a", jx + dx, 0, z0 + H * 0.47);
        }
      }
      parts = section.parts;
    }
    if (i === 0 && spec.roofBox) {
      add(rbox(2.6, 1.8, 0.42, 0.15), "#dee2e6", xc - 1.2, 0, top + 0.3);
      add(rbox(1.8, 1.2, 0.08, 0.03), "#adb5bd", xc - 1.2, 0, top + 0.53);
    }
    if (i === 0 && spec.poles) {
      // Trolley poles, from a base at the back of the front section up to the wires.
      const bx = back + 1.4;
      add(rbox(1.8, 1.3, 0.35, 0.1), "#868e96", bx, 0, top + 0.25);
      for (const y of [-0.4, 0.4]) {
        add(rbox(6.6, 0.1, 0.1, 0.04), "#343a40", bx - 3.0, y, top + 1.35, 0.33);
        add(rbox(0.3, 0.16, 0.16, 0.05), "#212529", bx - 6.1, y, top + 2.4);
      }
    }
    if (i === spec.pantograph) {
      // Pantograph: a folded arm on a base with a contact bar on top.
      add(rbox(1.6, 1.2, 0.25, 0.08), "#868e96", xc, 0, top + 0.2);
      add(rbox(1.7, 0.1, 0.1, 0.04), "#343a40", xc - 0.45, 0, top + 0.75, -0.75);
      add(rbox(1.7, 0.1, 0.1, 0.04), "#343a40", xc - 0.45, 0, top + 1.75, 0.75);
      add(rbox(0.16, 1.7, 0.1, 0.04), "#212529", xc - 1.07, 0, top + 2.36);
    }
    front = back - gap;
  });
  const centred = (geos, x) => mergeGeometries(geos).translate(-x, 0, 0);
  const template = {
    sections: sections.map((sec) => ({
      x: sec.x, length: sec.length,
      geo: centred(sec.parts, sec.x), hull: sec.hull.clone().translate(-sec.x, 0, 0),
    })),
    joints: joints.map((j) => ({ x: j.x, geo: centred(j.parts, j.x) })),
    length: total, width: W, roof: top + 0.25,
  };
  vehicleTemplates.set(mode, template);
  return template;
}

// A vehicle: its sections and joints start in a straight line; ToyTown bends them along the
// path the vehicle has driven (see ``articulate``).
export function makeVehicle(mode) {
  const t = vehicleTemplate(mode);
  const root = new THREE.Group();
  const body = new THREE.Group();
  root.add(body);
  const group = (x) => {
    const g = new THREE.Group();
    g.rotation.order = "ZYX";   // heading outermost; roll and pitch about the section's axes
    g.position.x = x;
    body.add(g);
    return g;
  };
  const sections = t.sections.map((sec) => {
    const g = group(sec.x);
    shadow(g, sec.length * 0.56, t.width * 0.8);
    g.add(new THREE.Mesh(sec.geo, VEHICLE_MAT), new THREE.Mesh(sec.hull, OUTLINE_VEHICLE));
    return { group: g, x: sec.x, length: sec.length, roll: 0, pitch: 0, heading: 0 };
  });
  const joints = t.joints.map((j) => {
    const g = group(j.x);
    g.add(new THREE.Mesh(j.geo, VEHICLE_MAT));
    return { group: g, x: j.x };
  });
  return { root, body, roof: t.roof, length: t.length, sections, joints };
}

// --- people --------------------------------------------------------------------------------

const OUTFITS = {
  dodger: { top: "#40c057", trim: "#2b8a3e", pants: "#364fc7", shoes: "#f8f9fa",
    hat: "#2b8a3e", hair: "#6b3e1f", pack: "#fab005" },
  wanted: { top: "#ffd43b", trim: "#f59f00", pants: "#364fc7", shoes: "#f8f9fa",
    hat: "#e8590c", hair: "#2b2118", pack: "#4dabf7" },
  hunter: { top: "#fa5252", trim: "#c92a2a", pants: "#3b5b92", shoes: "#6b4226",
    hat: "#9c6b3c", hair: "#4a2f1b", vest: "#6b4226", scarf: "#1c7ed6" },
  team: { top: "#ffa8a8", trim: "#e64980", pants: "#3b5b92", shoes: "#6b4226",
    hat: "#c08a52", hair: "#2b2118", vest: "#a47148", scarf: "#f59f00" },
};

// A person's parts are merged per rigid piece (the body with its head and hat, each leg, each
// arm, the "!" bubble), each piece a vertex-coloured mesh, an outline hull and a see-through
// ghost: about a dozen draw calls a person instead of a hundred. Made once per outfit, like
// the vehicles. Leg and arm pieces are centred on their hip and shoulder joints.
const PERSON_MAT = new THREE.MeshToonMaterial({ vertexColors: true, gradientMap });
const HIP = [0.19, 0.8];          // y (times the side) and z of the hip joints
const SHOULDER = [0.5, 1.44];
const personTemplates = new Map();

function personTemplate(kind) {
  if (personTemplates.has(kind)) return personTemplates.get(kind);
  const fit = OUTFITS[kind];
  const cowboy = kind === "hunter" || kind === "team";
  const o = true;   // with an outline
  const piece = () => ({ solid: [], hull: [], ghost: [] });
  const body = piece();
  const legs = [piece(), piece()];
  const arms = [piece(), piece()];
  const bubble = piece();
  const part = (into, geo, color, x, y, z, outline = false, ghost = null, rotX = 0) => {
    const g = coloured(geo, color, x, y, z, 0, rotX);
    into.solid.push(g);
    if (outline) into.hull.push(g);
    if (ghost) into.ghost.push(g);
  };

  [-1, 1].forEach((side, i) => {
    const hip = legs[i];
    part(hip, rbox(0.3, 0.28, 0.62, 0.1), fit.pants, 0, 0, -0.32, o);
    if (cowboy) {
      part(hip, rbox(0.42, 0.3, 0.3, 0.1), fit.shoes, 0.05, 0, -0.62, o);
      part(hip, rbox(0.12, 0.28, 0.08, 0.03), INK, -0.12, 0, -0.78);
    } else {
      part(hip, rbox(0.46, 0.3, 0.17, 0.07), fit.shoes, 0.07, 0, -0.68, o);
      part(hip, rbox(0.48, 0.32, 0.06, 0.03), "#adb5bd", 0.07, 0, -0.77);
      part(hip, rbox(0.06, 0.31, 0.05, 0.02), fit.trim, 0.2, 0, -0.66);
    }
    const shoulder = arms[i];
    part(shoulder, rbox(0.24, 0.22, 0.5, 0.08), fit.top, 0, 0, -0.22, o);
    part(shoulder, rbox(0.25, 0.23, 0.08, 0.03), fit.trim, 0, 0, -0.46);
    part(shoulder, rbox(0.2, 0.2, 0.18, 0.07), SKIN, 0, 0, -0.58, o);
  });

  // Torso.
  part(body, rbox(0.5, 0.82, 0.74, 0.18, 3), fit.top, 0, 0, 1.15, o, fit.top);
  if (cowboy) {
    for (const y of [-0.24, 0.24]) part(body, rbox(0.05, 0.3, 0.64, 0.03), fit.vest, 0.26, y, 1.15);
    part(body, rbox(0.05, 0.8, 0.66, 0.03), fit.vest, -0.26, 0, 1.15);
    part(body, rbox(0.52, 0.85, 0.1, 0.03), "#3d2b1f", 0, 0, 0.82);
    part(body, rbox(0.04, 0.17, 0.11, 0.02), "#fcc419", 0.27, 0, 0.82);
    part(body, rbox(0.03, 0.13, 0.13, 0.01), "#ffd43b", 0.3, 0.24, 1.27);
    part(body, rbox(0.03, 0.13, 0.13, 0.01), "#ffd43b", 0.3, 0.24, 1.27, false, null, Math.PI / 4);
    part(body, rbox(0.52, 0.8, 0.1, 0.04), fit.scarf, 0, 0, 1.5);
    part(body, rbox(0.04, 0.22, 0.2, 0.03), fit.scarf, 0.27, 0, 1.4, false, null, Math.PI / 4);
    part(body, rbox(0.16, 0.08, 0.34, 0.04), "#6b4226", 0, -0.46, 0.76, o);
    part(body, rbox(0.12, 0.1, 0.14, 0.03), "#495057", -0.02, -0.46, 0.98);
  } else {
    part(body, rbox(0.05, 0.48, 0.2, 0.04), fit.trim, 0.25, 0, 0.98);
    part(body, rbox(0.03, 0.04, 0.55, 0.01), "#f1f3f5", 0.26, 0, 1.18);
    for (const y of [-0.1, 0.1]) part(body, rbox(0.03, 0.03, 0.18, 0.01), "#f8f9fa", 0.26, y, 1.36);
    part(body, rbox(0.26, 0.62, 0.32, 0.1), fit.trim, -0.3, 0, 1.5);
    part(body, rbox(0.3, 0.6, 0.6, 0.12), fit.pack, -0.4, 0, 1.13, o);
    part(body, rbox(0.06, 0.42, 0.22, 0.05), shade(fit.pack, -0.25), -0.57, 0, 1.0);
    for (const y of [-0.24, 0.24]) {
      part(body, rbox(0.58, 0.09, 0.06, 0.02), shade(fit.pack, -0.35), -0.02, y, 1.52);
      part(body, rbox(0.04, 0.09, 0.45, 0.02), shade(fit.pack, -0.35), 0.26, y, 1.28);
    }
  }

  // Head and face (the face looks along +x).
  part(body, rbox(0.7, 0.72, 0.64, 0.2, 3), SKIN, 0, 0, 1.94, o, fit.top);
  for (const y of [-0.38, 0.38]) part(body, rbox(0.14, 0.08, 0.18, 0.04), SKIN, 0, y, 1.92);
  for (const y of [-0.15, 0.15]) {
    part(body, rbox(0.04, 0.15, 0.17, 0.03), "#ffffff", 0.355, y, 1.97);
    part(body, rbox(0.04, 0.08, 0.11, 0.02), INK, 0.37, y * 0.85, 1.96);
    part(body, rbox(0.02, 0.03, 0.03, 0.005), "#ffffff", 0.39, y * 0.75, 1.99);
    part(body, rbox(0.04, 0.16, 0.04, 0.01), fit.hair, 0.36, y, 2.09, false, null,
      cowboy ? (y > 0 ? 0.3 : -0.3) : 0);
    part(body, rbox(0.02, 0.1, 0.06, 0.02), "#ffb3a7", 0.355, y * 1.5, 1.85);
  }
  part(body, rbox(0.08, 0.1, 0.1, 0.03), "#f4b183", 0.38, 0, 1.9);
  part(body, rbox(0.03, 0.18, 0.04, 0.01), "#c0504d", 0.355, cowboy ? 0 : 0.04, 1.77);
  part(body, rbox(0.2, 0.72, 0.34, 0.08), fit.hair, -0.3, 0, 2.02);
  if (cowboy) {
    part(body, rbox(0.06, 0.34, 0.08, 0.03), fit.hair, 0.38, 0, 1.82);
    part(body, rbox(1.08, 1.0, 0.07, 0.03), shade(fit.hat, -0.15), 0, 0, 2.26, o);
    for (const y of [-0.5, 0.5]) part(body, rbox(0.86, 0.1, 0.18, 0.04), shade(fit.hat, -0.15), 0, y, 2.32);
    part(body, rbox(0.56, 0.58, 0.4, 0.14), fit.hat, 0, 0, 2.48, o);
    part(body, rbox(0.58, 0.6, 0.09, 0.03), "#3d2b1f", 0, 0, 2.33);
    part(body, rbox(0.32, 0.1, 0.07, 0.03), shade(fit.hat, -0.25), 0, 0, 2.68);
  } else {
    for (const y of [-0.36, 0.36]) part(body, rbox(0.3, 0.06, 0.22, 0.02), fit.hair, -0.1, y, 2.06);
    part(body, rbox(0.76, 0.78, 0.24, 0.1), fit.hat, 0, 0, 2.29, o);
    part(body, rbox(0.42, 0.66, 0.06, 0.03), fit.hat, 0.52, 0, 2.19, o);
    part(body, rbox(0.1, 0.1, 0.06, 0.02), shade(fit.hat, 0.3), 0, 0, 2.43);
  }

  // "!" bubble, above the head and not turning with the body.
  part(bubble, rbox(0.7, 0.3, 0.9, 0.14), "#ffffff", 0, 0, 0, o);
  part(bubble, rbox(0.16, 0.08, 0.44, 0.04), "#e03131", 0, -0.17, 0.12);
  part(bubble, rbox(0.16, 0.08, 0.14, 0.04), "#e03131", 0, -0.17, -0.27);
  const merge = (geos) => (geos.length ? mergeGeometries(geos) : null);
  const done = (p) => ({ solid: merge(p.solid), hull: merge(p.hull), ghost: merge(p.ghost) });
  const template = {
    body: done(body), legs: legs.map(done), arms: arms.map(done), bubble: done(bubble),
    ghost: fit.top,
  };
  personTemplates.set(kind, template);
  return template;
}

export function makePerson(kind) {
  const t = personTemplate(kind);
  const root = new THREE.Group();
  const body = new THREE.Group();
  root.add(body);
  shadow(root, 0.8, 0.8);
  const add = (parent, piece) => {
    const mesh = new THREE.Mesh(piece.solid, PERSON_MAT);
    mesh.renderOrder = 2;
    parent.add(mesh);
    if (piece.hull) {
      const hull = new THREE.Mesh(piece.hull, OUTLINE_PERSON);
      hull.renderOrder = 2;
      parent.add(hull);
    }
    if (piece.ghost) {
      const ghost = new THREE.Mesh(piece.ghost, xray(t.ghost));
      ghost.renderOrder = 1;   // before the person, so it never shows through its own parts
      parent.add(ghost);
    }
  };
  const joint = (piece, y, z) => {
    const g = new THREE.Group();
    g.position.set(0, y, z);
    add(g, piece);
    body.add(g);
    return g;
  };
  const legs = [-1, 1].map((side, i) => joint(t.legs[i], side * HIP[0], HIP[1]));
  const arms = [-1, 1].map((side, i) => joint(t.arms[i], side * SHOULDER[0], SHOULDER[1]));
  add(body, t.body);
  const bubble = new THREE.Group();
  bubble.position.z = 3.4;
  add(bubble, t.bubble);
  bubble.visible = false;
  root.add(bubble);
  return { root, body, legs, arms, bubble, kind };
}

// A rider is inside the vehicle: instead of the figure, a pin in their colour floats above
// the roof (pointing down at the vehicle), so riders stay easy to spot.
function makePin(color) {
  const pin = new THREE.Group();
  const cone = new THREE.ConeGeometry(0.55, 1.2, 16);
  cone.rotateX(-Math.PI / 2);      // tip pointing down (-z)
  const head = new THREE.SphereGeometry(0.62, 20, 14);
  head.translate(0, 0, 0.75);
  for (const [geo, z] of [[cone, 0], [head, 0]]) {
    const outline = new THREE.Mesh(
      geo,
      new THREE.MeshBasicMaterial({ color: INK, side: THREE.BackSide }),
    );
    outline.scale.setScalar(1.18);
    outline.position.z = z;
    pin.add(outline, new THREE.Mesh(geo, new THREE.MeshBasicMaterial({ color })));
  }
  pin.visible = false;
  return pin;
}

// A Bounty Hunter's sight circle on the ground (zoomed out, the flat map draws it).
const SIGHT_DISC = new THREE.CircleGeometry(1, 72);
const SIGHT_EDGE = new THREE.RingGeometry(0.99, 1, 96);
const SIGHT_FILL_MAT = new THREE.MeshBasicMaterial({
  color: "#fa5252", transparent: true, opacity: 0.1, depthWrite: false,
});
const SIGHT_EDGE_MAT = new THREE.MeshBasicMaterial({
  color: "#fa5252", transparent: true, opacity: 0.6, depthWrite: false,
});

function makeSight() {
  const g = new THREE.Group();
  g.add(new THREE.Mesh(SIGHT_DISC, SIGHT_FILL_MAT), new THREE.Mesh(SIGHT_EDGE, SIGHT_EDGE_MAT));
  g.position.z = 0.08;
  return g;
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

export const TREE_GEO = mergeGeometries([
  coloured(rbox(0.55, 0.55, 1.8, 0.12), "#8a5a3b", 0, 0, 0.9),
  coloured(rbox(2.7, 2.7, 2.1, 0.5, 3), "#4fbf5a", 0, 0, 2.6),
  coloured(rbox(1.8, 1.8, 1.3, 0.4, 3), "#7ad66a", 0.15, -0.1, 4.0),
]);

export const STOP_GEO = mergeGeometries([
  coloured(rbox(0.2, 0.2, 3.0, 0.06), "#868e96", 0, 0, 1.5),
  coloured(rbox(1.2, 0.18, 0.9, 0.1), "#ffd43b", 0, 0, 3.05),
  coloured(rbox(0.5, 0.2, 0.2, 0.05), "#2f9e44", 0, -0.02, 3.05),
]);

// --- the jail ------------------------------------------------------------------------------

// A barred cell in front of the district's police station, sized for one person (ToyTown
// scales it with the people): a concrete base, steel bars with a padlocked door, a bench, a
// police-blue roof with "BÖRTÖN" signs, and a siren that flashes while someone is locked in.
// Tall, with a roof that does not overhang: the game's camera looks down at about 40°, and
// the prisoner must show between the front bars, not hide under the roof.
const JAIL_HALF = 1.6;     // half the cell's width
const JAIL_BARS = 4.3;     // bar height
const JAIL_BASE = 0.2;
const JAIL_ROOF = JAIL_BASE + JAIL_BARS;

function signTexture(text) {
  const canvas = document.createElement("canvas");
  canvas.width = 512;
  canvas.height = 112;
  const g = canvas.getContext("2d");
  g.fillStyle = "#ffd43b";
  g.fillRect(0, 0, 512, 112);
  g.lineWidth = 12;
  g.strokeStyle = INK;
  g.strokeRect(6, 6, 500, 100);
  g.fillStyle = INK;
  g.font = '700 72px Fredoka, "Trebuchet MS", sans-serif';
  g.textAlign = "center";
  g.textBaseline = "middle";
  g.fillText(text, 256, 60);
  const tex = new THREE.CanvasTexture(canvas);
  tex.colorSpace = THREE.SRGBColorSpace;
  tex.anisotropy = 4;
  return tex;
}

const JAIL_SOLID = (() => {
  const h = JAIL_HALF, top = JAIL_ROOF;
  const parts = [
    coloured(rbox(3.8, 3.8, JAIL_BASE, 0.1), "#adb5bd", 0, 0, JAIL_BASE / 2),
    coloured(rbox(3.1, 3.1, 0.05, 0.02), "#868e96", 0, 0, JAIL_BASE + 0.02),
    // Roof: police blue over a white band, and the siren's base.
    coloured(rbox(3.75, 3.75, 0.18, 0.07), "#f8f9fa", 0, 0, top + 0.09),
    coloured(rbox(3.65, 3.65, 0.6, 0.18), "#1971c2", 0, 0, top + 0.47),
    coloured(rbox(1.3, 0.6, 0.24, 0.08), "#dee2e6", 0, 0, top + 0.89),
    // A bench at the back, and the padlock on the door.
    coloured(rbox(1.6, 0.55, 0.16, 0.05), "#c0814a", 0, h - 0.5, JAIL_BASE + 0.62),
    coloured(rbox(0.14, 0.45, 0.6, 0.04), "#8a5a3b", -0.6, h - 0.5, JAIL_BASE + 0.3),
    coloured(rbox(0.14, 0.45, 0.6, 0.04), "#8a5a3b", 0.6, h - 0.5, JAIL_BASE + 0.3),
    coloured(rbox(0.4, 0.22, 0.42, 0.08), "#fcc419", 0.62, -h - 0.12, JAIL_BASE + 1.55),
    coloured(rbox(0.26, 0.09, 0.26, 0.04), "#ced4da", 0.62, -h - 0.12, JAIL_BASE + 1.85),
  ];
  // Corner posts, and rails round the top and halfway up.
  for (const [x, y] of [[-h, -h], [h, -h], [-h, h], [h, h]]) {
    parts.push(coloured(rbox(0.36, 0.36, JAIL_BARS, 0.08), "#343a40", x, y, JAIL_BASE + JAIL_BARS / 2));
  }
  for (const z of [top - 0.1, JAIL_BASE + JAIL_BARS * 0.5]) {
    for (const s of [-h, h]) {
      parts.push(coloured(rbox(2 * h, 0.18, 0.18, 0.06), "#343a40", 0, s, z));
      parts.push(coloured(rbox(0.18, 2 * h, 0.18, 0.06), "#343a40", s, 0, z));
    }
  }
  return mergeGeometries(parts);
})();

// Bars: five a side, thin and without outlines so the prisoner shows between them. The front
// door (the middle three bars on the south side) gets a yellow frame.
const JAIL_BARS_GEO = (() => {
  const h = JAIL_HALF;
  const parts = [];
  for (let i = 1; i <= 5; i++) {
    const u = -h + (2 * h * i) / 6;
    for (const s of [-h, h]) {
      parts.push(coloured(rbox(0.1, 0.1, JAIL_BARS, 0.04), "#5c6670", u, s, JAIL_BASE + JAIL_BARS / 2));
      parts.push(coloured(rbox(0.1, 0.1, JAIL_BARS, 0.04), "#5c6670", s, u, JAIL_BASE + JAIL_BARS / 2));
    }
  }
  for (const x of [-0.8, 0.8]) {
    parts.push(coloured(rbox(0.14, 0.14, JAIL_BARS - 0.3, 0.05), "#fab005", x, -h - 0.02,
      JAIL_BASE + JAIL_BARS / 2));
  }
  return mergeGeometries(parts);
})();

export function makeJail() {
  const root = new THREE.Group();
  shadow(root, 2.6, 2.6);
  root.add(new THREE.Mesh(JAIL_SOLID, VEHICLE_MAT), new THREE.Mesh(JAIL_SOLID, OUTLINE_PERSON));
  root.add(new THREE.Mesh(JAIL_BARS_GEO, VEHICLE_MAT));
  // "BÖRTÖN" (jail) on the roof's front and back.
  const sign = new THREE.MeshBasicMaterial({ map: signTexture("BÖRTÖN") });
  for (const side of [-1, 1]) {
    const plane = new THREE.PlaneGeometry(2.9, 0.5);
    plane.rotateX(Math.PI / 2);          // facing south
    if (side > 0) plane.rotateZ(Math.PI);  // facing north
    const mesh = new THREE.Mesh(plane, sign);
    mesh.position.set(0, side * 1.85, JAIL_ROOF + 0.47);
    root.add(mesh);
  }
  // The siren: unlit, so it glows; ToyTown flashes it while someone is locked in.
  const lamp = (x) => {
    const mesh = new THREE.Mesh(rbox(0.5, 0.44, 0.42, 0.15, 3), new THREE.MeshBasicMaterial());
    mesh.position.set(x, 0, JAIL_ROOF + 1.21);
    mesh.add(new THREE.Mesh(mesh.geometry, OUTLINE_PERSON));
    root.add(mesh);
    return mesh.material;
  };
  const red = lamp(-0.3), blue = lamp(0.3);
  // Drawn after the people's see-through silhouettes (render order 1), so the prisoner shows
  // plainly between the bars instead of as a ghost under the roof.
  root.traverse((obj) => { obj.renderOrder = 3; });
  const siren = (on, t) => {
    const flash = on ? Math.floor(t * 5) % 2 : -1;
    red.color.set(flash === 0 ? "#ff4d4f" : "#8f2d2d");
    blue.color.set(flash === 1 ? "#4dabf7" : "#1d4e89");
  };
  siren(false, 0);
  return { root, siren };
}

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
  float hash1(float x) { return fract(sin(x * 91.7 + 13.1) * 43758.5453); }
  // Anti-aliased rectangle [lo, hi] in p's space.
  float rect(vec2 p, vec2 lo, vec2 hi, vec2 aa) {
    vec2 m = smoothstep(lo - aa, lo + aa, p) * (1.0 - smoothstep(hi - aa, hi + aa, p));
    return m.x * m.y;
  }
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
    if (abs(n.z) < 0.35 && vTop > 0.5) {
      // Facade on a grid in metres along the wall: 3.2 m bays, 2.8 m storeys.
      vec2 t = normalize(vec2(-n.y, n.x) + 1e-5);
      float u = dot(vPos.xy, t);
      float z = vPos.z;
      vec2 cell = vec2(u / 3.2, (z - 0.4) / 2.8);
      vec2 f = fract(cell);
      vec2 aa = fwidth(cell) * 0.8 + 1e-4;
      vec2 g = vec2(f.x, z);                       // ground floor: bay fraction, metres
      vec2 ga = vec2(aa.x, fwidth(z) * 0.8 + 1e-4);
      float detail = 1.0 - smoothstep(0.2, 0.45, max(aa.x, aa.y));
      float row = floor(cell.y);

      vec3 frame = mix(vWall, vec3(1.0), 0.7);
      vec3 shutter = mix(vec3(0.27, 0.6, 0.38), vec3(0.3, 0.47, 0.8), hash1(vSeed + 3.0));
      vec3 awning = hash1(vSeed + 5.0) > 0.5 ? vec3(0.92, 0.32, 0.3) : vec3(0.2, 0.62, 0.56);
      vec3 wood = vec3(0.58, 0.35, 0.2);
      float shutters = step(0.55, hash1(vSeed + 1.0));
      float shop = step(0.45, hash1(vSeed + 2.0)) * step(7.0, vTop);
      float balconies = step(0.6, hash1(vSeed + 4.0));
      float lit = step(0.88, hash2(floor(cell) + vSeed));
      vec3 glass = mix(vec3(0.45, 0.72, 0.92), vec3(1.0, 0.86, 0.4), lit);
      float glint = rect(f, vec2(0.34, 0.55), vec2(0.42, 0.72), aa) * (1.0 - lit);
      glass = mix(glass, vec3(0.88, 0.96, 1.0), glint * 0.7);

      vec3 c = vWall;
      if (z < 3.2) {
        c = mix(c * 0.78, c, smoothstep(0.66, 0.7, z));               // stone plinth
        if (shop > 0.5) {
          c = mix(c, vec3(0.33, 0.28, 0.26), rect(g, vec2(0.07, 0.7), vec2(0.93, 2.5), ga));
          c = mix(c, glass, rect(g, vec2(0.11, 0.76), vec2(0.89, 2.44), ga));
          float stripes = step(0.5, fract(u / 0.7));
          c = mix(c, mix(awning, vec3(1.0), stripes * 0.85),
            rect(g, vec2(0.0, 2.6), vec2(1.0, 3.05), ga));
        } else {
          float door = step(0.7, hash2(vec2(floor(cell.x), vSeed + 7.0)));
          float panel = 0.9 + 0.1 * step(0.5, fract(f.x * 10.0));
          c = mix(c, frame, mix(rect(g, vec2(0.27, 1.15), vec2(0.73, 2.58), ga),
            rect(g, vec2(0.27, 0.7), vec2(0.73, 2.75), ga), door));
          c = mix(c, mix(glass, wood * panel, door), mix(
            rect(g, vec2(0.31, 1.25), vec2(0.69, 2.5), ga),
            rect(g, vec2(0.31, 0.7), vec2(0.69, 2.65), ga), door));
          c = mix(c, vec3(0.95, 0.8, 0.3), door * rect(g, vec2(0.6, 1.55), vec2(0.64, 1.7), ga));
        }
      } else if (0.4 + (row + 1.0) * 2.8 <= vTop - 1.1) {
        // Upper storeys: framed windows with sills and a cross, shutters, balconies.
        c = mix(c, c * 0.9, rect(f, vec2(-1.0, 0.0), vec2(2.0, 0.025), aa));   // storey line
        float win = rect(f, vec2(0.31, 0.25), vec2(0.69, 0.76), aa);
        float sh = shutters * max(rect(f, vec2(0.15, 0.25), vec2(0.27, 0.76), aa),
          rect(f, vec2(0.73, 0.25), vec2(0.85, 0.76), aa));
        c = mix(c, shutter * (0.85 + 0.15 * step(0.5, fract(f.y * 18.0))), sh);
        c = mix(c, frame, max(rect(f, vec2(0.27, 0.2), vec2(0.73, 0.8), aa),
          rect(f, vec2(0.24, 0.14), vec2(0.76, 0.21), aa)));
        c = mix(c, glass, win);
        c = mix(c, frame, win * max(rect(f, vec2(0.49, 0.0), vec2(0.51, 1.0), aa),
          rect(f, vec2(0.0, 0.56), vec2(1.0, 0.585), aa)));
        float balcony = balconies * step(0.5, mod(row, 2.0));
        c = mix(c, vec3(0.55, 0.53, 0.5), balcony * rect(f, vec2(0.2, 0.12), vec2(0.8, 0.2), aa));
        float rail = max(rect(f, vec2(0.21, 0.42), vec2(0.79, 0.46), aa),
          rect(f, vec2(0.21, 0.2), vec2(0.79, 0.44), aa) * step(0.6, fract(f.x * 16.0)));
        c = mix(c, vec3(0.25, 0.25, 0.28), balcony * rail);
      }
      // Cornice under the roof, with a shadow line below it.
      vec2 zc = vec2(0.5, z);
      c = mix(c, c * 0.72, rect(zc, vec2(0.0, vTop - 1.18), vec2(1.0, vTop - 1.02), ga));
      c = mix(c, frame, rect(zc, vec2(0.0, vTop - 1.02), vec2(1.0, vTop - 0.6), ga));
      col = mix(vWall * 0.96, c, detail);
    }
    float ndl = dot(n, uLight);
    col *= ndl > 0.5 ? 1.0 : (ndl > 0.05 ? 0.84 : 0.7);
    col *= mix(0.68, 1.0, smoothstep(0.0, 4.5, vPos.z));   // contact shadow at street level
    float grey = dot(col, vec3(0.299, 0.587, 0.114));
    col = mix(col, vec3(grey) * 0.75, (1.0 - vInside) * 0.6);
    gl_FragColor = vec4(col, 1.0);
    #include <colorspace_fragment>
  }`;

// Roof props share the houses' material: non-indexed, position and normal only, like the
// extruded houses.
const ROOF_PROPS = Object.fromEntries(Object.entries(PROPS).map(([name, [x, y, z, r]]) => {
  const g = rbox(x, y, z, r).toNonIndexed();
  g.deleteAttribute("uv");
  return [name, g];
}));

// --- the layer -----------------------------------------------------------------------------

// --- vehicle motion ------------------------------------------------------------------------

//: Trail points are recorded this far apart (map metres).
const TRAIL_STEP = 0.4;
const SUSPENSION = 1 - Math.exp(-1 / 6);   // per-frame easing of roll and pitch at 60 fps

function recordTrail(rec, keep) {
  const trail = rec.trail;
  if (!trail.length || trail[0].distanceTo(rec.pos) >= TRAIL_STEP) trail.unshift(rec.pos.clone());
  let length = 0;
  for (let i = 1; i < trail.length; i++) {
    length += trail[i].distanceTo(trail[i - 1]);
    if (length > keep) { trail.length = i + 1; break; }
  }
}

// The point ``u`` model metres along the vehicle from its centre (forward positive). The
// vehicle's nose is at its reported position, so every section lies on the path it has just
// driven; beyond the recorded trail it continues straight back.
function pointAlong(rec, u, k) {
  const dir = new THREE.Vector3(Math.cos(rec.angle), Math.sin(rec.angle), 0);
  let remaining = (rec.length / 2 - u) * k;
  let prev = rec.pos;
  for (const p of rec.trail) {
    const seg = prev.distanceTo(p);
    if (seg >= remaining && seg > 0) return prev.clone().lerp(p, remaining / seg);
    remaining -= seg;
    prev = p;
  }
  return prev.clone().addScaledVector(dir, -remaining);
}

const wrap = (a) => Math.atan2(Math.sin(a), Math.cos(a));

// Ease-out with a little overshoot, 0 -> 1 over x in [0, 1].
function popIn(x) {
  if (x >= 1) return 1;
  const c = 1.7;
  const u = Math.max(0, x) - 1;
  return Math.max(0.001, 1 + (c + 1) * u * u * u + c * u * u);
}

// Bend the sections along the driven path (articulated buses and trams fold at their bellows,
// metro cars at their couplers), and give each section a little suspension: it leans out of
// curves, dips when braking, squats when pulling away and jiggles while moving.
function articulate(rec, k, dt, t) {
  recordTrail(rec, (rec.length + 4) * k);
  const moving = Math.min(1, rec.speed / (4 * k));
  const ease = 1 - (1 - SUSPENSION) ** (dt * 60);
  rec.sections.forEach((sec, i) => {
    const front = pointAlong(rec, sec.x + sec.length / 2, k);
    const back = pointAlong(rec, sec.x - sec.length / 2, k);
    const heading = Math.atan2(front.y - back.y, front.x - back.x);
    const yawRate = dt > 0 ? wrap(heading - sec.heading) / dt : 0;
    sec.heading = heading;
    // Rolls out of the curve; the nose lifts when pulling away and dips when braking.
    const roll = THREE.MathUtils.clamp(yawRate * moving * 0.12, -0.07, 0.07);
    const pitch = THREE.MathUtils.clamp((-(rec.accel || 0) / k) * 0.004, -0.035, 0.035);
    sec.roll += (roll - sec.roll) * ease;
    sec.pitch += (pitch - sec.pitch) * ease;
    const mid = front.add(back).multiplyScalar(0.5).sub(rec.pos).divideScalar(k);
    const jiggle = Math.sin(t * 13 + i * 1.9) * 0.03 * moving;
    sec.group.position.set(mid.x, mid.y, jiggle);
    sec.group.rotation.set(sec.roll, sec.pitch, heading);
  });
  rec.joints.forEach((joint, i) => {
    const p = pointAlong(rec, joint.x, k).sub(rec.pos).divideScalar(k);
    const a = rec.sections[i].heading;
    const b = rec.sections[i + 1].heading;
    joint.group.position.set(p.x, p.y, 0);
    joint.group.rotation.set(0, 0, a + wrap(b - a) / 2);
  });
}

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
    this.gen = 0;                   // district number, so late houses of the last one are dropped
    this.buildingMeshes = [];
    this.batcher = new Batcher((arrays) => this.addHouses(arrays));
    this.startWorker();
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
    this.treeMaterial = new THREE.MeshToonMaterial({ vertexColors: true, gradientMap });
    this.treeCells = new Map();
    this.treeCount = 0;
    this.stops = null;
    this.actors = new THREE.Group();  // vehicles, people and the jail
    this.scene.add(this.actors);
    this.ground = new THREE.Group();  // sight circles: on the ground, not scaled with people
    this.actors.add(this.ground);
    this.jail = null;
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
    this.gen++;
    this.batcher.clear();
    this.seen.clear();
    for (const mesh of this.treeCells.values()) { this.town.remove(mesh); mesh.dispose(); }
    this.treeCells.clear();
    this.treeCount = 0;
    for (const rec of [...this.vehicles.values(), ...this.people.values()]) {
      this.actors.remove(rec.root);
      if (rec.sight) this.ground.remove(rec.sight);
    }
    this.vehicles.clear();
    this.people.clear();
    if (this.jail) { this.actors.remove(this.jail.root); this.jail = null; }

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

  // The instanced trees of square (ci, cj), made when the first tree is planted there.
  treeCell(ci, cj) {
    const key = `${ci},${cj}`;
    let mesh = this.treeCells.get(key);
    if (!mesh) {
      mesh = new THREE.InstancedMesh(TREE_GEO, this.treeMaterial, TREE_CELL * TREE_CELL);
      mesh.count = 0;
      this.treeCells.set(key, mesh);
      this.town.add(mesh);
    }
    return mesh;
  }

  plantTrees(poly, density) {
    if (this.treeCount >= MAX_TREES) return;
    const ring = poly[0].map(([lon, lat]) => this.local(lat, lon));
    const holes = poly.slice(1).map((h) => h.map(([lon, lat]) => this.local(lat, lon)));
    let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
    for (const v of ring) {
      x0 = Math.min(x0, v.x); y0 = Math.min(y0, v.y); x1 = Math.max(x1, v.x); y1 = Math.max(y1, v.y);
    }
    const m = new THREE.Matrix4();
    const q = new THREE.Quaternion();
    const color = new THREE.Color();
    const touched = new Set();
    for (let i = Math.floor(x0 / TREE_SPACING_M); i <= x1 / TREE_SPACING_M; i++) {
      for (let j = Math.floor(y0 / TREE_SPACING_M); j <= y1 / TREE_SPACING_M; j++) {
        if (this.treeCount >= MAX_TREES) break;
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
        const cell = this.treeCell(Math.floor(i / TREE_CELL), Math.floor(j / TREE_CELL));
        const k = cell.count++;
        this.treeCount++;
        cell.setMatrixAt(k, m);
        cell.setColorAt(k, color.setHSL(0.0, 0, 0.85 + 0.15 * hash(h * 31)));
        touched.add(cell);
      }
    }
    for (const cell of touched) {
      cell.instanceMatrix.needsUpdate = true;
      if (cell.instanceColor) cell.instanceColor.needsUpdate = true;
      cell.computeBoundingSphere();   // for culling: the trees planted so far
    }
  }

  // Houses are made off the main thread: a module worker gets footprints in local metres and
  // sends back merged arrays per map square. Without workers (or if it fails to start), they
  // are made here, a couple of milliseconds a frame.
  startWorker() {
    this.worker = null;
    this.inflight = 0;
    try {
      const worker = new Worker(new URL("./houses-worker.js", import.meta.url), { type: "module" });
      worker.onmessage = ({ data }) => {
        if (data.type === "done") this.inflight = Math.max(0, this.inflight - 1);
        else if (data.gen === this.gen) this.addHouses(data.arrays);
      };
      worker.onerror = () => { worker.terminate(); this.worker = null; this.inflight = 0; };
      const props = {};
      const transfer = [];
      for (const [name, g] of Object.entries(ROOF_PROPS)) {
        const position = g.attributes.position.array.slice();
        const normal = g.attributes.normal.array.slice();
        props[name] = { position, normal };
        transfer.push(position.buffer, normal.buffer);
      }
      worker.postMessage({ type: "props", props }, transfer);
      this.worker = worker;
    } catch {
      this.worker = null;
    }
  }

  // A queued footprint, in local metres for houses.js.
  footprint({ poly, height, id }) {
    const pts = (ring) => ring.slice(0, -1).map(([lon, lat]) => {
      const v = this.local(lat, lon);
      return [v.x, v.y];
    });
    const ring = pts(poly[0]);
    const inside = ring.length > 0 && this.area(ring[0][0], ring[0][1]);
    return { ring, holes: poly.slice(1).map(pts), height, id, inside };
  }

  buildSome() {
    if (!this.queue.length) return;
    if (this.worker) {
      // Two batches in flight keep the worker busy without queueing far ahead of the camera.
      while (this.queue.length && this.inflight < 2) {
        const houses = this.queue.splice(-BATCH_HOUSES).reverse().map((f) => this.footprint(f));
        this.worker.postMessage({ type: "build", gen: this.gen, houses, last: !this.queue.length });
        this.inflight++;
      }
      return;
    }
    const t0 = performance.now();
    while (this.queue.length && performance.now() - t0 < BUILD_BUDGET_MS) {
      const house = this.footprint(this.queue.pop());
      const geos = house.ring.length ? makeBuilding(house, ROOF_PROPS) : null;
      if (geos) this.batcher.add(cellOf(house.ring[0][0], house.ring[0][1]), geos);
    }
    this.batcher.tick(!this.queue.length);
  }

  addHouses(arrays) {
    const mesh = new THREE.Mesh(fromArrays(arrays), this.buildingMaterial);
    this.buildingMeshes.push(mesh);
    this.town.add(mesh);
  }

  // --- actors ----------------------------------------------------------------------------

  /** Positions from a server snapshot; drawn smoothly between snapshots. */
  update(s) {
    if (!this.origin) return;
    this.focus = this.local(s.player.pos[0], s.player.pos[1]);
    const seen = new Set();
    for (const [trip, lat, lon, mode, , heading, shown = 1] of s.vehicles) {
      seen.add(trip);
      let rec = this.vehicles.get(trip);
      if (!rec) {
        // Born facing the way it drives and lying straight along its track (no trail yet),
        // then growing in as it drives up to its first stop, instead of appearing sideways.
        rec = { ...makeVehicle(mode), heading, angle: heading, trail: [], vis: 0 };
        rec.root.scale.setScalar(0.001);
        this.vehicles.set(trip, rec);
        this.actors.add(rec.root);
      }
      this.retarget(rec, this.local(lat, lon));
      rec.heading = heading;   // the timetable knows which way it is going
      rec.shown = shown;       // under 1 while driving in before its first stop or away after
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
      state: h.riding ? "ride" : "walk", alert: h.chasing, sight: true,
    }));
    // Other Dodgers: teammates when you run, the ones in sight when you hunt. The caught
    // are out of the round.
    (s.dodgers || []).forEach((d, i) => {
      if (d.caught) return;
      people.push({
        key: `dodger${i}`, kind: role === "hunter" ? "wanted" : "dodger", pos: d.pos,
        state: d.riding ? "ride" : d.held ? "held" : "walk", alert: role === "hunter",
      });
    });
    const keys = new Set();
    for (const p of people) {
      keys.add(p.key);
      let rec = this.people.get(p.key);
      if (rec && rec.kind !== p.kind) { this.dropPerson(rec); rec = null; }
      if (!rec) {
        rec = { ...makePerson(p.kind), heading: null, angle: 0 };
        rec.pin = makePin(ROLE_COLORS[p.kind]);
        if (p.player) rec.pin.scale.setScalar(1.3);
        rec.root.add(rec.pin);
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
      if (p.sight && !rec.sight) {
        rec.sight = makeSight();
        this.ground.add(rec.sight);
      }
      if (rec.sight) rec.sight.visible = !!p.sight && p.state !== "ride";
      rec.trip = p.player && s.ride ? s.ride.trip : null;
    }
    for (const [key, rec] of this.people) {
      if (!keys.has(key)) { this.dropPerson(rec); this.people.delete(key); }
    }

    if (s.jail) {
      if (!this.jail) {
        this.jail = makeJail();
        this.actors.add(this.jail.root);
      }
      const at = this.local(s.jail.pos[0], s.jail.pos[1]);
      this.jail.root.position.set(at.x, at.y, 0);
      this.jail.busy = s.jail.busy;
    }
  }

  dropPerson(rec) {
    this.actors.remove(rec.root);
    if (rec.sight) this.ground.remove(rec.sight);
  }

  retarget(rec, target) {
    if (!rec.target || rec.target.distanceTo(target) > 400) {
      rec.pos = target.clone();          // new, or teleported: no sliding across the map
      if (rec.trail) rec.trail.length = 0;
    } else {
      const dx = target.x - rec.target.x, dy = target.y - rec.target.y;
      if (dx * dx + dy * dy > 0.04) rec.heading = Math.atan2(dy, dx);
    }
    rec.target = target;
  }

  animate(dt, zoom) {
    const mpp = (40075016.686 * Math.cos((this.world.center[0] * Math.PI) / 180)) /
      (512 * 2 ** zoom);
    const personK = Math.max(1.2, (mpp * 34) / 2.6);              // about 34 px tall
    const vehicleK = Math.min(3.2, Math.max(1.4, (mpp * 130) / 12));  // a bus about 130 px
    const follow = 1 - Math.exp(-dt * 12);
    const turn = 1 - Math.exp(-dt * 8);
    const t = this.time;

    const step = (rec) => {
      const before = rec.pos.clone();
      rec.pos.lerp(rec.target, follow);
      const speed = before.distanceTo(rec.pos) / Math.max(dt, 1e-3);
      rec.accel = (speed - (rec.speed || 0)) / Math.max(dt, 1e-3);
      rec.speed = speed;
      if (rec.heading !== null) {
        let d = rec.heading - rec.angle;
        d = Math.atan2(Math.sin(d), Math.cos(d));
        rec.angle += d * turn;
      }
      rec.root.position.set(rec.pos.x, rec.pos.y, 0);
    };

    for (const rec of this.vehicles.values()) {
      step(rec);
      // Eased towards the server's value, so it grows and shrinks smoothly between snapshots.
      rec.vis += (rec.shown - rec.vis) * Math.min(1, dt * 6);
      rec.root.scale.setScalar(vehicleK * popIn(rec.vis));
      rec.body.position.z = rec.mine ? Math.abs(Math.sin(t * 6)) * 0.25 : 0;
      articulate(rec, vehicleK, dt, t);
    }
    for (const rec of this.people.values()) {
      step(rec);
      rec.body.rotation.z = rec.angle;
      const riding = rec.state === "ride";
      const mine = rec.trip !== null && this.vehicles.get(rec.trip);
      // Riders sit inside: hide the figure and float their pin just above the roof.
      if (mine) {
        const middle = pointAlong(mine, 0, vehicleK);
        rec.root.position.set(middle.x, middle.y, mine.roof * vehicleK);
      }
      else if (riding) rec.root.position.z = 3.6 * vehicleK;   // a typical roof height
      rec.body.visible = !riding;
      rec.pin.visible = riding;
      if (riding) rec.pin.position.z = 1.4 + Math.abs(Math.sin(t * 3)) * 0.35;
      rec.root.scale.setScalar(personK);
      if (rec.sight) {
        rec.sight.position.set(rec.pos.x, rec.pos.y, 0.08);
        rec.sight.scale.setScalar(this.world.sight_radius_m);
      }
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

    if (this.jail) {
      this.jail.root.scale.setScalar(personK);   // a cell that fits the people
      this.jail.siren(this.jail.busy, t);
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
