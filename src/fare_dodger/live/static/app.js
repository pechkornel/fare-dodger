// Fare Dodger live client: draws server snapshots on a MapLibre map and sends input. Zoomed
// out the map is flat with DOM markers; zoomed in it tilts into the 3D toy town (world3d.js).
import { toonStyle } from "./toon-style.js";
import { MAX_PITCH, SWITCH_ZOOM, ToyTown, pitchForZoom } from "./world3d.js";
import { play, toggleMute, unlock } from "./sound.js";

const KEYMAP = {
  KeyW: "up", ArrowUp: "up", KeyS: "down", ArrowDown: "down",
  KeyA: "left", ArrowLeft: "left", KeyD: "right", ArrowRight: "right",
  ShiftLeft: "sprint", ShiftRight: "sprint",
};
const MODE_COLORS = { metro: "#7950f2", tram: "#fcc419", trolleybus: "#fd7e14", bus: "#339af0",
  suburban_rail: "#2f9e44" };
const MODE_ICONS = { metro: "🚇", tram: "🚋", trolleybus: "🚎", bus: "🚌", suburban_rail: "🚆" };
const INK = "#2b2118";
const MIN_ZOOM = 12.5;
const MAX_ZOOM = 19;
const START_ZOOM = 17.2;
// Budapest and a little around it ([west, south], [east, north]): the camera never leaves it,
// so no map tiles are fetched for the rest of the world.
const MAP_BOUNDS = [[18.86, 47.33], [19.36, 47.63]];
const MAX_PIXEL_RATIO = 1.5;
// The camera covers this share of the way to you in a 60th of a second (time-based, so a
// dropped frame doesn't jerk it).
const FOLLOW_RATE = -Math.log(1 - 0.2) * 60;

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s).replace(/[&<>"']/g, (c) => `&#${c.charCodeAt(0)};`);
const mmss = (s) => {
  s = Math.max(0, Math.round(s));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
};
const pill = (mode, route) =>
  `<span class="pill ${mode}">${MODE_ICONS[mode] || ""}${esc(route)}</span>`;
const ROLE_LABEL = { dodger: "🏃 Dodger", hunter: "🤠 Bounty Hunter" };
const lngLat = ([lat, lon]) => [lon, lat];

let world = null;        // static data of the district being played
let district = -1;       // its index, or -1 before the first one is loaded
let loading = false;
let map = null;
let town = null;         // the 3D layer
let ws = null;
let follow = true;
let dragging = false;
let rotating = false;
let pressed = false;       // a mouse button or finger is down on the map
let shownRound = null;   // round whose result overlay is showing
let burstTimer = null;
let match = null;
const keys = { up: false, down: false, left: false, right: false, sprint: false };
const last = { money: null, tickets: null };
// Camera: zoom eases towards cam.zoom, the tilt follows the zoom, and the centre follows you.
const cam = { zoom: START_ZOOM, target: null };

const markers = { vehicles: new Map(), hunters: [], player: null, dodger: null };

// The main menu shows over the first district's map; a match starts from there.
let leaving = false;     // closing the connection on purpose (back to the menu)
let pending = null;      // newest snapshot not drawn yet
let pendingEvents = [];  // messages from every snapshot since the last frame
let shownDepartures = []; // the departures on screen, for the 1-6 keys
let hoverLine = null;      // "pattern:pos" of the departure under the mouse
const aheadPaths = new Map(); // "district:pattern:pos" -> the line ahead ([lat, lon] points)
let aheadShown = null;     // what the "ahead" layer shows now
let lastState = null;      // the player's state in the previous snapshot (walk, ride...)
let nextBeat = 0;          // when the heartbeat sounds next (performance.now() ms)
let lastPingS = null;

function init() {
  setupMinimap();
  showMenu();
  loadBackdrop();
  requestAnimationFrame(frame);
}

function showMenu() {
  document.body.classList.add("in-menu");
  $("menu").hidden = false;
  $("overlay").hidden = true;
  $("countdown").hidden = true;
}

// --- menu and lobby --------------------------------------------------------------------

function playerName() {
  const name = $("player-name").value.trim();
  try { localStorage.setItem("fare-dodger-name", name); } catch { /* private mode */ }
  return name;
}

function menuError(message) {
  $("menu-error").textContent = message;
  $("menu-error").hidden = !message;
}

// Opens a connection and says what we want: {type: "solo" | "create" | "join", ...}.
function openGame(hello) {
  menuError("");
  shownRound = null;
  match = null;
  connect(hello);
}

function showLobby(lobby) {
  $("menu").hidden = true;
  $("lobby").hidden = false;
  $("lobby-code").textContent = lobby.code;
  $("lobby-size").textContent = `${lobby.size}v${lobby.size} · best of ${lobby.rounds}`;
  lobby.teams.forEach((team, t) => {
    const rows = team.players.map((name) => `<div>👤 ${esc(name)}</div>`)
      .concat(Array.from({ length: team.bots }, () => '<div class="bot">🤖 bot</div>'));
    const el = $(`lobby-team-${t}`);
    el.className = `lobby-team${t === lobby.team ? " mine" : ""}`;
    el.innerHTML = `<h4>Team ${"AB"[t]}${t === lobby.team ? " (you)" : ""}</h4>${rows.join("")}`;
  });
  $("start-game").hidden = !lobby.host || lobby.starting;
  $("lobby-wait").hidden = lobby.host && !lobby.starting;
  $("lobby-wait").textContent = lobby.starting ? "Loading the district…"
    : `Waiting for ${lobby.host_name || "the host"} to start…`;
}

function enterGame() {
  if (!$("lobby").hidden || !$("menu").hidden) {
    $("lobby").hidden = true;
    $("menu").hidden = true;
    document.body.classList.remove("in-menu");
  }
}

function quitToMenu() {
  if (ws) { leaving = true; ws.close(); ws = null; }
  for (const k of Object.keys(keys)) keys[k] = false;
  $("lobby").hidden = true;
  showMenu();
  district = -1;
  loadBackdrop();
}

function createMap(w) {
  map = new maplibregl.Map({
    container: "map",
    style: toonStyle(w.tiles.url, w.tiles.glyphs),
    center: lngLat(w.center), zoom: cam.zoom, pitch: pitchForZoom(cam.zoom),
    minZoom: MIN_ZOOM, maxZoom: MAX_ZOOM, maxPitch: MAX_PITCH, maxBounds: MAP_BOUNDS,
    attributionControl: { compact: true },
    // Retina and 4K screens: the houses' shader runs per pixel, so cap the pixels drawn.
    pixelRatio: Math.min(window.devicePixelRatio || 1, MAX_PIXEL_RATIO),
    // Right-click drag (or Ctrl+drag) turns the map; the tilt still follows the zoom.
    dragRotate: true, pitchWithRotate: false, touchPitch: false, keyboard: false,
    scrollZoom: false, doubleClickZoom: false, boxZoom: false,
  });
  map.on("dragstart", () => { dragging = true; setFollow(false); });
  // The middle mouse button (pressing the scroll wheel) pings that spot for your team.
  map.on("mousedown", (ev) => {
    if (ev.originalEvent.button !== 1) return;
    ev.originalEvent.preventDefault();   // no auto-scroll
    send({ type: "mark", pos: [ev.lngLat.lat, ev.lngLat.lng] });
  });
  // WASD are relative to the screen: tell the server whenever the map turns under held keys.
  map.on("rotate", () => {
    if (keys.up || keys.down || keys.left || keys.right) sendKeys();
  });
  map.on("dragend", () => { dragging = false; });
  // The camera loop's jumpTo stops any gesture in progress (even before MapLibre starts
  // it), so the loop pauses while a mouse button or a finger is down on the map.
  map.on("rotatestart", () => { rotating = true; });
  map.on("rotateend", () => { rotating = false; });
  const container = map.getCanvasContainer();
  container.addEventListener("mousedown", () => { pressed = true; });
  container.addEventListener("touchstart", () => { pressed = true; }, { passive: true });
  window.addEventListener("mouseup", () => { pressed = false; });
  window.addEventListener("touchend", () => { pressed = false; });
  container.addEventListener("contextmenu", (ev) => ev.preventDefault());
  map.on("zoom", (ev) => { if (ev.originalEvent) cam.zoom = map.getZoom(); }); // pinch
  map.getCanvasContainer().addEventListener("wheel", (ev) => {
    ev.preventDefault();
    zoomBy(-ev.deltaY * (ev.deltaMode ? 0.12 : 0.006));   // a wheel notch: about 0.6
  }, { passive: false });
  return new Promise((resolve) => map.on("load", () => {
    addOverlays();
    town = new ToyTown(map);
    map.addLayer(town);
    resolve();
  }));
}

function zoomBy(delta) {
  cam.zoom = Math.min(MAX_ZOOM, Math.max(MIN_ZOOM, cam.zoom + delta));
}

function frame() {
  requestAnimationFrame(frame);
  if (pending) {
    const snapshot = { ...pending, events: pendingEvents };
    pending = null;
    pendingEvents = [];
    render(snapshot);
  }
  if (!map || dragging || rotating || pressed) return;
  const z = map.getZoom();
  const now = performance.now();
  const dt = Math.min(0.1, (now - (cam.last || now)) / 1000);
  cam.last = now;
  // Ease quickly (about 90% of the way in a quarter of a second), independent of frame rate.
  const ease = 1 - Math.exp(-dt * 10);
  const zoom = Math.abs(cam.zoom - z) < 0.002 ? cam.zoom : z + (cam.zoom - z) * ease;
  const view = {};
  if (zoom !== z) view.zoom = zoom;
  const pitch = pitchForZoom(zoom);
  if (Math.abs(pitch - map.getPitch()) > 0.01) view.pitch = pitch;
  if (follow && cam.target) {
    const c = map.getCenter();
    const [lon, lat] = lngLat(cam.target);
    if (Math.abs(lon - c.lng) + Math.abs(lat - c.lat) > 1e-7) {
      const f = 1 - Math.exp(-dt * FOLLOW_RATE);
      view.center = [c.lng + (lon - c.lng) * f, c.lat + (lat - c.lat) * f];
    }
  }
  if (Object.keys(view).length) map.jumpTo(view);
  $("map").classList.toggle("toy", zoom >= SWITCH_ZOOM);
}

// Behind the menu: a district the server has ready, not necessarily the first one.
async function loadBackdrop() {
  let index = 0;
  try { index = (await (await fetch("api/backdrop")).json()).district; } catch { /* first one */ }
  return loadDistrict(index);
}

async function loadDistrict(index) {
  loading = true;
  try {
    const next = await (await fetch(`api/world/${index}`)).json();
    if (!map) await createMap(next);
    world = next;
    district = index;
    clearDynamic();
    drawStatic(world);
    town.setDistrict(world);
    cam.target = null;
    map.jumpTo({ center: lngLat(world.center) });
    setFollow(true);
  } finally {
    loading = false;
  }
}

// --- ground overlays (GeoJSON layers under the labels and the 3D town) --------------------

const fc = (features) => ({ type: "FeatureCollection", features });
const feature = (geometry, properties = {}) => ({ type: "Feature", geometry, properties });

function addOverlays() {
  for (const id of ["area", "lines", "stations", "dynamic"]) {
    map.addSource(id, { type: "geojson", data: fc([]) });
  }
  const add = (layer) => map.addLayer(layer, "street-names");
  add({ id: "area-dim", type: "fill", source: "area", filter: ["==", ["get", "kind"], "dim"],
    paint: { "fill-color": INK, "fill-opacity": 0.3 } });
  add({ id: "area-edge", type: "line", source: "area", filter: ["==", ["get", "kind"], "edge"],
    paint: { "line-color": INK, "line-width": 3, "line-dasharray": [3, 2.5] } });
  add({ id: "lines-casing", type: "line", source: "lines",
    layout: { "line-cap": "round", "line-join": "round" },
    paint: { "line-color": INK, "line-width": 8, "line-opacity": 0.18 } });
  add({ id: "lines", type: "line", source: "lines",
    layout: { "line-cap": "round", "line-join": "round" },
    paint: { "line-color": ["get", "color"], "line-width": 5, "line-opacity": 0.75 } });
  // The way the chosen departure (or your ride) goes next: a white-cased line with arrows.
  map.addSource("ahead", { type: "geojson", data: fc([]) });
  add({ id: "ahead-casing", type: "line", source: "ahead",
    layout: { "line-cap": "round", "line-join": "round" },
    paint: { "line-color": "#fff", "line-width": 13, "line-opacity": 0.95 } });
  add({ id: "ahead", type: "line", source: "ahead",
    layout: { "line-cap": "round", "line-join": "round" },
    paint: { "line-color": ["get", "color"], "line-width": 7 } });
  add({ id: "ahead-arrows", type: "symbol", source: "ahead",
    layout: { "symbol-placement": "line", "symbol-spacing": 60, "text-field": ">",
      "text-font": ["Noto Sans Bold"], "text-size": 20, "text-keep-upright": false,
      "text-rotation-alignment": "map", "text-allow-overlap": true },
    paint: { "text-color": "#fff", "text-halo-color": INK, "text-halo-width": 1.5 } });
  add({ id: "sight", type: "fill", source: "dynamic", filter: ["==", ["get", "kind"], "sight"],
    paint: { "fill-color": "#fa5252", "fill-opacity": 0.1 } });
  add({ id: "sight-edge", type: "line", source: "dynamic",
    filter: ["==", ["get", "kind"], "sight"],
    paint: { "line-color": "#fa5252", "line-width": 2, "line-opacity": 0.6,
      "line-dasharray": [3, 3] } });
  add({ id: "ping", type: "fill", source: "dynamic", filter: ["==", ["get", "kind"], "ping"],
    paint: { "fill-color": "#e03131", "fill-opacity": ["*", 0.15, ["get", "fade"]] } });
  add({ id: "ping-edge", type: "line", source: "dynamic", filter: ["==", ["get", "kind"], "ping"],
    paint: { "line-color": "#e03131", "line-width": 3, "line-opacity": ["get", "fade"],
      "line-dasharray": [1.5, 2] } });
  add({ id: "stations", type: "circle", source: "stations",
    paint: { "circle-radius": 5, "circle-color": "#fff", "circle-stroke-color": INK,
      "circle-stroke-width": 3 } });
  map.addLayer({ id: "station-names", type: "symbol", source: "stations", minzoom: 16,
    layout: { "text-field": ["get", "name"], "text-font": ["Noto Sans Bold"], "text-size": 13,
      "text-offset": [0, 1.2], "text-anchor": "top", "text-optional": true },
    paint: { "text-color": INK, "text-halo-color": "#fff", "text-halo-width": 2.5 } });
}

function drawStatic(w) {
  const area = w.area;
  let ring;
  if (area.kind === "polygon") ring = area.vertices.map(lngLat);
  else if (area.kind === "circle") ring = circle([area.center_lat, area.center_lon], area.radius_m);
  else {
    ring = [[area.min_lon, area.min_lat], [area.max_lon, area.min_lat],
      [area.max_lon, area.max_lat], [area.min_lon, area.max_lat]];
  }
  ring = [...ring, ring[0]];
  // Dim everything outside the play area: a world-sized ring with the area as a hole.
  const outside = [[-179, -85], [179, -85], [179, 85], [-179, 85], [-179, -85]];
  map.getSource("area").setData(fc([
    feature({ type: "Polygon", coordinates: [outside, ring] }, { kind: "dim" }),
    feature({ type: "LineString", coordinates: ring }, { kind: "edge" }),
  ]));
  map.getSource("lines").setData(fc(w.lines.map((line) => feature(
    { type: "LineString", coordinates: line.path.map(lngLat) },
    { color: MODE_COLORS[line.mode] || "#888" }))));
  map.getSource("stations").setData(fc(w.stations.map((st) => feature(
    { type: "Point", coordinates: lngLat(st.pos) }, { name: st.name }))));
  // Café signs stay visible in the 3D town too (they are not ``.entity`` markers).
  for (const m of cafeMarkers) m.remove();
  cafeMarkers = (w.cafes || []).map((cafe) => {
    const el = document.createElement("div");
    el.className = "cafe-sign";
    el.title = `${cafe.name}: coffee ${w.coffee_price_ft} Ft (C)`;
    el.textContent = "☕";
    return new maplibregl.Marker({ element: el }).setLngLat(lngLat(cafe.pos)).addTo(map);
  });
}

let cafeMarkers = [];
let jailMarker = null;

// The jail on the flat map (the 3D town draws its own cell): a police badge that flashes
// while someone is locked in.
function renderJail(jail) {
  if (!jail) return;
  if (!jailMarker) {
    const el = document.createElement("div");
    el.className = "entity";
    el.innerHTML = '<div class="jail-sign"><span class="bars">🚔</span>BÖRTÖN</div>';
    jailMarker = new maplibregl.Marker({ element: el }).setLngLat(lngLat(jail.pos)).addTo(map);
  }
  jailMarker.setLngLat(lngLat(jail.pos));
  const sign = jailMarker.getElement().firstChild;
  sign.title = `Jail: ${jail.name}`;
  sign.classList.toggle("busy", jail.busy);
}

const parcels = { markers: [], goal: null };
// Package destinations: an icon per kind of landmark (a flag for a plain street spot).
const LANDMARK_ICONS = {
  statue: "🗿", monument: "🗽", castle: "🏰", church: "⛪", building: "🏛️", fountain: "⛲",
  mural: "🎨", museum: "🏛️", attraction: "⭐", viewpoint: "👀", theatre: "🎭",
};
const landmarkIcon = (kind) => LANDMARK_ICONS[kind] || "🏁";
// "Szabadság-szobor (near Gellért tér)", or "near Gellért tér" for a street spot.
const destination = (d) =>
  d.landmark ? `<b>${esc(d.landmark)}</b> <span class="muted">near ${esc(d.near)}</span>`
    : `near <b>${esc(d.near)}</b>`;
const metres = (m) => (m >= 1000 ? `${(m / 1000).toFixed(1)} km` : `${m} m`);

function renderParcels(s) {
  const list = s.packages || [];
  while (parcels.markers.length > list.length) parcels.markers.pop().remove();
  list.forEach((pos, i) => {
    if (!parcels.markers[i]) {
      const el = document.createElement("div");
      el.className = "parcel-sign";
      el.title = "A package: walk here and press E";
      el.textContent = "📦";
      parcels.markers[i] = new maplibregl.Marker({ element: el }).setLngLat(lngLat(pos)).addTo(map);
    }
    parcels.markers[i].setLngLat(lngLat(pos));
    showIfOnScreen(parcels.markers[i], pos);
  });
  if (s.delivery) {
    if (!parcels.goal) {
      const el = document.createElement("div");
      el.className = "goal-sign";
      el.textContent = landmarkIcon(s.delivery.kind);
      parcels.goal = new maplibregl.Marker({ element: el }).setLngLat(lngLat(s.delivery.pos))
        .addTo(map);
    }
    parcels.goal.setLngLat(lngLat(s.delivery.pos));
    showIfOnScreen(parcels.goal, s.delivery.pos);
    parcels.goal.getElement().textContent = landmarkIcon(s.delivery.kind);
    parcels.goal.getElement().title = s.delivery.landmark
      ? `Deliver here: ${s.delivery.landmark} (near ${s.delivery.near})`
      : `Deliver here (near ${s.delivery.near})`;
  } else if (parcels.goal) {
    parcels.goal.remove();
    parcels.goal = null;
  }
}

/** Ring of [lon, lat] approximating a circle of ``radius`` metres. */
function circle([lat, lon], radius) {
  const dLat = radius / 111320;
  const dLon = dLat / Math.cos((lat * Math.PI) / 180);
  const ring = [];
  for (let i = 0; i <= 48; i++) {
    const a = (i / 48) * 2 * Math.PI;
    ring.push([lon + dLon * Math.cos(a), lat + dLat * Math.sin(a)]);
  }
  return ring;
}

function clearDynamic() {
  groundKey = null;
  for (const m of markers.vehicles.values()) m.remove();
  markers.vehicles.clear();
  for (const m of markers.hunters) m.remove();
  markers.hunters = [];
  for (const m of markers.marks || []) m.remove();
  markers.marks = [];
  for (const key of ["player", "dodger"]) {
    if (markers[key]) { markers[key].remove(); markers[key] = null; }
  }
  if (jailMarker) { jailMarker.remove(); jailMarker = null; }
  last.money = last.tickets = null;
  for (const m of parcels.markers) m.remove();
  parcels.markers = [];
  if (parcels.goal) { parcels.goal.remove(); parcels.goal = null; }
}

function connect(hello) {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  leaving = false;
  const socket = new WebSocket(`${proto}://${location.host}/ws`);
  ws = socket;
  socket.onopen = () => socket.send(JSON.stringify(hello));
  socket.onmessage = (ev) => {
    const msg = JSON.parse(ev.data);
    if (msg.type === "error") {
      leaving = true;
      quitToMenu();
      menuError(msg.message);
    } else if (msg.type === "closed") {
      leaving = true;
      quitToMenu();
      menuError(msg.message);
    } else if (msg.type === "lobby") {
      showLobby(msg);
    } else {
      // Draw only the newest snapshot (on the next frame), so a slow frame never builds up
      // a queue of stale ones; keep every snapshot's messages.
      pendingEvents.push(...msg.events);
      pending = msg;
    }
  };
  socket.onclose = () => {
    if (!leaving && ws === socket) toast("end", "Disconnected from the server. Reload to play again.");
  };
}

function send(msg) {
  if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(msg));
}

function setFollow(on) {
  follow = on;
  $("follow").hidden = on;
}

// --- rendering --------------------------------------------------------------------------

// Flat-view marker; hidden by CSS once the map is zoomed into the toy town.
function divMarker(pos, html, z) {
  const el = document.createElement("div");
  el.className = "entity";
  el.innerHTML = html;
  el.style.zIndex = z;
  return new maplibregl.Marker({ element: el }).setLngLat(lngLat(pos)).addTo(map);
}

function render(s) {
  enterGame();
  match = s.match;
  if (match.district !== district) {
    if (!loading) loadDistrict(match.district);
    return; // draw once the district's map is in
  }
  for (const e of s.events) {
    toast(e.kind, e.text);
    if (e.kind === "inspection") {
      const ok = e.text.includes("valid");
      burst(ok ? "good" : "bad");
      play(ok ? "ok" : "inspection");
    } else if (e.kind === "caught") {
      burst("caught", `🚨 ${e.text}`, 2600);
      shake();
      play("caught");
    } else if (e.kind === "end" && s.player.state === "caught") {
      burst("bad", "CAUGHT! 🚨", 2600);
    } else if (e.kind === "end" && s.role === "dodger" && s.status === "escaped") {
      play("escape");
    } else if (e.kind === "board") play("board");
    else if (e.kind === "alight") play("alight");
    else if (e.kind === "coffee" && e.text.startsWith("☕")) play("coffee");
    else if (e.kind === "package" || e.kind === "ticket") play("money");
  }
  renderDanger(s);
  renderAhead(s);
  renderJailBanner(s);
  // Zoomed into the toy town the flat markers are hidden: drop them instead of moving them
  // (MapLibre repositions every marker on every frame).
  const toy = $("map").classList.contains("toy");
  if (toy) clearFlatMarkers();
  else {
    renderVehicles(s.vehicles, s.ride ? s.ride.trip : null);
    renderHunters(s.hunters, s.role);
    renderDodgers(s.dodgers, s.role);
    renderPlayer(s.player, s.role);
  }
  renderMarks(s.marks || []);
  renderGround(s, toy);
  town.update(s);
  renderScore(match);
  renderHud(s);
  renderTimer(s, match);
  renderCountdown(s, match);
  renderToggle(s);
  drawMinimap(s);
  renderPingArrow(s);
  renderParcels(s);
  renderJail(s.jail);
  cam.target = s.player.pos;
  if (match.round_over && shownRound !== match.round) showResult(s, match);
  if (match.round_over && s.room && s.room.loading) {
    $("again").textContent = "Loading the next district…";
  } else if (match.round_over && s.room && s.room.next_round_in !== null && !s.room.host) {
    $("again").textContent = `Next round in ${Math.ceil(s.room.next_round_in)}…`;
  } else if (match.round_over && s.room && s.room.next_round_in !== null) {
    $("again").textContent = `Next round in ${Math.ceil(s.room.next_round_in)} · Enter: now`;
  }
  if (!match.round_over) { $("overlay").hidden = true; shownRound = null; }
}

function renderVehicles(list, myTrip) {
  const seen = new Set();
  for (const [trip, lat, lon, mode, route, , shown = 1] of list) {
    seen.add(trip);
    let m = markers.vehicles.get(trip);
    if (!m) {
      m = divMarker([lat, lon], `<div class="vehicle">${pill(mode, route)}</div>`, 1);
      markers.vehicles.set(trip, m);
    } else {
      m.setLngLat([lon, lat]);
    }
    const el = m.getElement();
    el.firstChild.classList.toggle("mine", trip === myTrip);
    el.style.opacity = shown;  // fading in before its first stop, out after its last
    el.style.zIndex = trip === myTrip ? 900 : 1;
  }
  for (const [trip, m] of markers.vehicles) {
    if (!seen.has(trip)) { m.remove(); markers.vehicles.delete(trip); }
  }
}

function renderHunters(list, role) {
  // As the Dodger these are the enemy (only the close ones); as a Bounty Hunter, the team.
  const team = role === "hunter" ? " team" : "";
  while (markers.hunters.length > list.length) markers.hunters.pop().remove();
  list.forEach((hunter, i) => {
    const cls = `toon hunter${team}${hunter.riding ? " riding" : ""}` +
      `${hunter.chasing ? " chasing" : ""}`;
    if (!markers.hunters[i]) {
      markers.hunters[i] = divMarker(hunter.pos, `<div class="${cls}">🤠</div>`, 800);
    }
    markers.hunters[i].setLngLat(lngLat(hunter.pos));
    markers.hunters[i].getElement().firstChild.className = cls;
  });
}

// Other Dodgers: your teammates when you run, the ones in sight when you hunt.
function renderDodgers(list, role) {
  markers.dodgers = markers.dodgers || [];
  while (markers.dodgers.length > list.length) markers.dodgers.pop().remove();
  list.forEach((d, i) => {
    const kind = role === "hunter" ? "wanted" : "mate";
    const cls = `toon ${kind}${d.held ? " held" : ""}${d.caught ? " out" : ""}`;
    if (!markers.dodgers[i]) {
      markers.dodgers[i] = divMarker(d.pos, `<div class="${cls}" title="${esc(d.name)}">🏃</div>`, 950);
    }
    markers.dodgers[i].setLngLat(lngLat(d.pos));
    markers.dodgers[i].getElement().firstChild.className = cls;
  });
}

// Your team's pings (the middle mouse button): a pulsing pin with who put it there.
function renderMarks(list) {
  markers.marks = markers.marks || [];
  while (markers.marks.length > list.length) markers.marks.pop().remove();
  list.forEach((m, i) => {
    const html = `<div class="mark">📍<small>${esc(m.name)}</small></div>`;
    if (!markers.marks[i]) markers.marks[i] = divMarker(m.pos, html, 1200);
    const marker = markers.marks[i];
    marker.setLngLat(lngLat(m.pos));
    const el = marker.getElement();
    if (el.dataset.key !== `${m.name}|${m.pos}`) {
      el.dataset.key = `${m.name}|${m.pos}`;
      el.innerHTML = html;
    }
    el.style.opacity = Math.min(1, Math.max(0, (8 - m.age_s) / 2));  // fades out at the end
  });
}

function clearFlatMarkers() {
  for (const m of markers.vehicles.values()) m.remove();
  markers.vehicles.clear();
  for (const m of markers.hunters) m.remove();
  markers.hunters = [];
  for (const m of markers.dodgers || []) m.remove();
  markers.dodgers = [];
  if (markers.player) { markers.player.remove(); markers.player = null; }
}

// Sight circles of the Bounty Hunters on foot and the last radar pings, drawn on the ground.
// In the toy town the sight circles are discs in the 3D layer; here the GeoJSON source (which
// MapLibre re-processes on every update) changes only when what it shows does, and with
// moving circles at most GROUND_HZ times a second.
const GROUND_HZ = 5;
let groundKey = null;
let groundAt = 0;
function renderGround(s, toy) {
  // A Dodger sees the Bounty Hunters (and so their circles) only zoomed in close.
  const sight = toy ? [] : s.hunters.filter((h) => !h.riding && s.role === "hunter");
  const pings = (s.pings || []).map((ping) => ({ pos: ping.pos,
    fade: Math.round(Math.max(0.15, 1 - ping.age_s / world.ping_every_s) * 20) / 20 }));
  const key = JSON.stringify([sight.map((h) => h.pos), pings]);
  const now = performance.now();
  if (key === groundKey || (sight.length && now - groundAt < 1000 / GROUND_HZ)) return;
  groundKey = key;
  groundAt = now;
  const features = sight.map((h) => feature(
    { type: "Polygon", coordinates: [circle(h.pos, world.sight_radius_m)] }, { kind: "sight" }));
  // Every Dodger's last report, not just the newest (as on the minimap).
  for (const ping of pings) {
    features.push(feature({ type: "Polygon", coordinates: [circle(ping.pos, 60)] },
      { kind: "ping", fade: ping.fade }));
  }
  map.getSource("dynamic").setData(fc(features));
}

function renderPlayer(p, role) {
  const moving = keys.up || keys.down || keys.left || keys.right;
  const state = p.state === "walk" && !moving ? "idle" : p.state;
  let face = { held: "😬", wait: "🧍", idle: "🧍" }[state] || "🏃";
  if (role === "hunter") face = "🤠";
  const cls = `toon player ${role} ${state}`;
  if (!markers.player) {
    markers.player = divMarker(p.pos, `<div class="${cls}">${face}</div>`, 1000);
  }
  markers.player.setLngLat(lngLat(p.pos));
  const el = markers.player.getElement().firstChild;
  el.className = cls;
  if (el.textContent !== face) el.textContent = face;
}

function renderScore(m) {
  $("score-you").textContent = m.score.you;
  $("score-bots").textContent = m.score.them;
  $("them-name").textContent = m.names.them;
  const round = m.overtime ? `Overtime · round ${m.round}` : `Round ${m.round} of ${m.regulation}`;
  $("round").textContent = round;
  $("role").textContent = ROLE_LABEL[m.role];
  $("role").className = `chip role ${m.role}`;
  $("district").textContent = `📍 ${world.name}`;
}

function setChip(id, value) {
  if (last[id] === value) return;
  const el = $(id);
  el.textContent = value;
  if (last[id] !== null) {
    const chip = el.parentElement;
    chip.classList.remove("bump");
    void chip.offsetWidth; // restart the animation
    chip.classList.add("bump");
  }
  last[id] = value;
}

function renderHud(s) {
  const hunter = s.role === "hunter";
  const hour = Number(s.clock.slice(0, 2)) % 24;
  const night = hour >= 21 || hour < 5;  // night buses only, mostly
  $("clock").textContent = `${night ? "🌙" : "🕗"} ${s.clock.slice(0, 5)}`; // small: the timer counts
  $("remaining").textContent = `⏳ ${mmss(s.remaining_s / world.time_scale)}`;
  $("wallet").hidden = !s.wallet;
  if (s.wallet) {
    setChip("money", s.wallet.money);
    setChip("tickets", s.wallet.tickets);
  }
  const coffee = s.coffee || {};
  $("boost").hidden = coffee.boost_s == null;
  if (coffee.boost_s != null) $("boost-s").textContent = mmss(coffee.boost_s / world.time_scale);
  $("radar-label").textContent = hunter ? "📡 next report" : "🤠 radar";
  const frac = 1 - s.next_ping_s / world.ping_every_s;
  $("ping-bar").style.width = `${Math.min(100, Math.max(0, frac * 100))}%`;
  $("ping-in").textContent = mmss(s.next_ping_s / world.time_scale);
  if (hunter) {
    $("nearest").textContent = s.seen
      ? "👀 The Dodger is in sight. Get them!"
      : "The Dodger is hidden. Follow the reports.";
    $("nearest").className = s.seen ? "danger" : "";
  } else {
    const near = s.nearest_hunter_m;
    const far = near >= 1000 ? (near / 1000).toFixed(1) + " km" : near + " m";
    $("nearest").textContent = `Nearest Bounty Hunter ${far}`;
    $("nearest").className = near <= world.sight_radius_m * 2 ? "danger" : "";
  }
  // The Dodger's top-bar sense of danger: red close, green far, neutral between.
  const threat = $("threat");
  threat.hidden = hunter || s.status !== "running" || s.nearest_hunter_m === null;
  if (!threat.hidden) {
    const near = s.nearest_hunter_m;
    threat.textContent = `🤠 ${near >= 1000 ? (near / 1000).toFixed(1) + " km" : near + " m"}`;
    threat.className = "threat" +
      (near <= world.see_radius_m * 1.5 ? " danger" : near >= world.see_radius_m * 4 ? " safe" : "");
  }

  let status;
  let held = false;
  if (s.player.state === "caught") {
    held = true;
    status = "🚨 You were caught. Watching your team…";
  } else if (s.ride) {
    const r = s.ride;
    const ticket = hunter ? "" :
      r.fined ? " · 🚨 fined" : r.ticket ? " · 🎫 valid" : " · 🙈 no ticket";
    const then = r.via.map(esc).join(", ");
    status = `Riding ${pill(r.mode, r.route)} ${arrow(r.bearing)} ${esc(r.headsign)}<br>` +
      `<span class="muted">Next: <b>${esc(r.next_stop)}</b>${then ? `, then ${then}` : ""}` +
      `${ticket}` +
      (r.alight_at ? ` · off at ${esc(r.alight_at)}` : " · E to get off") + "</span>";
  } else if (s.held_s !== null && s.jailed) {
    held = true;
    status = `🚔 In jail at ${esc(s.jail.name)}… <b>${mmss(s.held_s / world.time_scale)}</b><br>` +
      '<span class="muted">The Bounty Hunters know you\'re here. Run when the door opens!</span>';
  } else if (s.held_s !== null) {
    held = true;
    status = `📝 The inspectors are writing your fine… ` +
      `<b>${mmss(s.held_s / world.time_scale)}</b><br>` +
      `<span class="muted">${world.fine_ft} Ft. The Bounty Hunters know where you are!</span>`;
  } else if (coffee.brewing_s != null) {
    status = `☕ Making your coffee… <b>${mmss(coffee.brewing_s / world.time_scale)}</b><br>` +
      '<span class="muted">Stand still. Keep an eye out for Bounty Hunters!</span>';
  } else if (s.waiting) {
    status = `Waiting for ${esc(s.waiting.route)} → ${esc(s.waiting.headsign)} ` +
      `<span class="muted">(${mmss(s.waiting.in_s / world.time_scale)}, E to cancel)</span>` +
      (s.wallet && !s.wallet.tickets ? '<br><span class="danger">No ticket: risky!</span>' : "");
  } else {
    status = (coffee.boost_s != null ? "☕ Running on coffee" : "On foot") +
      (s.stop || coffee.cafe ? "" : ' <span class="muted">· walk to a stop to ride</span>');
  }
  $("status").innerHTML = status;
  $("status").className = held ? "status held" : "status";

  let parcel = "";
  if (s.delivery) {
    const d = s.delivery;
    parcel = `<div class="parcel-row">📦 → ${landmarkIcon(d.kind)} ${destination(d)} · ` +
      `${metres(d.distance_m)} · +${d.reward} Ft</div>`;
  } else if (s.package_here) {
    const p = s.package_here;
    parcel = `<h3><span>📦 A package</span><span class="chip" id="take-parcel">E · ` +
      `+${p.reward} Ft</span></h3>` +
      `<div>→ ${landmarkIcon(p.kind)} ${destination(p)} · ${metres(p.distance_m)}</div>`;
  }
  const cafe = parcel + (coffee.cafe && coffee.brewing_s == null
    ? `<h3><span>☕ ${esc(coffee.cafe)}</span>` +
      `<span class="chip" id="coffee">C · ☕ ${world.coffee_price_ft} Ft</span></h3>` +
      `<div class="muted">Runs you almost bus-fast for ` +
      `${mmss(world.coffee_boost_s / world.time_scale)}.</div>`
    : "");
  shownDepartures = s.stop ? s.stop.departures : [];
  if (!s.stop) { $("stop").innerHTML = cafe; return; }
  const rows = s.stop.departures.map((d, i) =>
    `<div class="dep${d.waiting ? " waiting" : ""}${lineKey(d) === hoverLine ? " hover" : ""}" ` +
    `data-line="${lineKey(d)}"><span class="key">${i + 1}</span>` +
    pill(d.mode, d.route) + arrow(d.bearing) +
    `<span class="headsign">${esc(d.headsign)}` +
    (d.via.length ? `<small>via ${d.via.map(esc).join(" · ")}</small>` : "") + "</span>" +
    `<span class="in">${d.in_s <= 0 ? "now" : mmss(d.in_s / world.time_scale)}</span></div>`);
  const buy = s.wallet
    ? `<span class="chip" id="buy">T · 🎫 ${world.ticket_price_ft} Ft</span>` : "";
  $("stop").innerHTML = cafe + `<h3><span>🚏 ${esc(s.stop.name)}</span>${buy}</h3>` +
    (rows.length ? rows.join("") : '<div class="muted">No departures soon.</div>');
}

const lineKey = (d) => `${d.pattern}:${d.pos}`;

// Which way a vehicle goes, as an arrow on the screen (it turns with the map).
function arrow(bearing) {
  if (bearing == null) return '<span class="dir"></span>';
  const turn = bearing - (map ? map.getBearing() : 0);
  return `<span class="dir" title="Heads ${compass(bearing)}">` +
    `<i style="transform: rotate(${Math.round(turn)}deg)">⬆</i></span>`;
}
const compass = (b) => ["north", "north-east", "east", "south-east", "south", "south-west",
  "west", "north-west"][Math.round(b / 45) % 8];

// The line ahead on the map: of the departure under the mouse, else the one you wait for,
// else your ride's (from where you are).
function renderAhead(s) {
  let want = null;
  let from = null;
  const deps = s.stop ? s.stop.departures : [];
  const hovered = deps.find((d) => lineKey(d) === hoverLine);
  const waited = deps.find((d) => d.waiting);
  const dep = hovered || waited;
  if (dep) want = { key: lineKey(dep), mode: dep.mode };
  else if (s.ride && s.ride.bearing != null) {
    want = { key: lineKey(s.ride), mode: s.ride.mode };
    from = s.player.pos;
  }
  const id = want ? `${district}:${want.key}` : null;
  if (!id) {
    if (aheadShown !== null) map.getSource("ahead").setData(fc([]));
    aheadShown = null;
    return;
  }
  const path = aheadPaths.get(id);
  if (path === undefined) {
    aheadPaths.set(id, null); // asked for
    const [pattern, pos] = want.key.split(":");
    fetch(`api/ahead/${district}/${pattern}/${pos}`)
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error(String(r.status)))))
      .then((body) => aheadPaths.set(id, body.path))
      .catch(() => setTimeout(() => aheadPaths.delete(id), 2000));   // ask again later
    return;
  }
  if (!path || path.length < 2) return;
  const shown = `${id}|${from ? from.join(",") : ""}`;
  if (shown === aheadShown) return;
  aheadShown = shown;
  const coords = (from ? [from, ...path] : path).map(lngLat);
  map.getSource("ahead").setData(fc([feature({ type: "LineString", coordinates: coords },
    { color: MODE_COLORS[want.mode] || "#888" })]));
}

// Danger: a heartbeat that quickens and a red screen edge as a Bounty Hunter closes in (the
// Dodger); a blip on every report (the Bounty Hunters). Also the whoosh of a sprint.
function renderDanger(s) {
  const running = s.status === "running" && s.player.state !== "caught";
  const near = s.nearest_hunter_m;
  const red = world.see_radius_m * 1.5;
  let level = 0;
  if (running && s.role === "dodger" && near !== null && near <= red) level = 1 - near / red;
  $("vignette").style.opacity = level ? String(0.35 + 0.65 * level) : "0";
  if (level) {
    const now = performance.now();
    if (now >= nextBeat) {
      play("heartbeat");
      nextBeat = now + 1100 - 650 * level;
      const v = $("vignette");
      v.classList.remove("beat");
      void v.offsetWidth;
      v.classList.add("beat");
    }
  }
  if (s.role === "hunter" && running && lastPingS !== null && s.next_ping_s > lastPingS + 5) {
    play("ping");
  }
  lastPingS = s.next_ping_s;
  if (s.player.sprinting && lastState !== "sprint") play("sprint");
  lastState = s.player.sprinting ? "sprint" : s.player.state;
  const st = $("stamina");
  st.hidden = !running || s.player.state !== "walk" ||
    (s.player.stamina >= 1 && !s.player.sprinting);
  $("stamina-bar").style.width = `${Math.round(s.player.stamina * 100)}%`;
  st.classList.toggle("low", s.player.stamina < 0.25);
}

function shake() {
  const el = $("map");
  el.classList.remove("shake");
  void el.offsetWidth;
  el.classList.add("shake");
}

// In jail: a big countdown if it's you, and who is locked in (and for how long) for the rest.
function renderJailBanner(s) {
  const el = $("jail-banner");
  const running = s.status === "running";
  if (running && s.jailed && s.held_s !== null) {
    el.className = "me";
    el.innerHTML = '<div class="title">🚔 IN JAIL</div>' +
      `<div class="clock">${mmss(s.held_s / world.time_scale)}</div>` +
      `<div class="where">${esc(s.jail.name)} · the Bounty Hunters know you're here. ` +
      "Run when the door opens!</div>";
    el.hidden = false;
    return;
  }
  const inmates = running ? (s.jail.inmates || []) : [];
  if (!inmates.length) { el.hidden = true; return; }
  el.className = "";
  el.innerHTML = inmates.map((m) => `🚔 ${esc(m.name)} is in jail at ${esc(s.jail.name)} · ` +
    `door opens in <b>${mmss(m.open_s / world.time_scale)}</b>`).join("<br>");
  el.hidden = false;
}

function toast(kind, text) {
  const el = document.createElement("div");
  el.className = `toast ${kind}`;
  el.textContent = text;
  $("feed").prepend(el);
  setTimeout(() => el.remove(), 6000);
  while ($("feed").children.length > 5) $("feed").lastChild.remove();
}

function burst(kind, text = null, ms = 1600) {
  $("burst-text").textContent = text ?? (kind === "good" ? "Ticket OK! 🎫" : "KONTROLL! 🚨");
  $("burst").className = kind;
  $("burst").hidden = false;
  clearTimeout(burstTimer);
  burstTimer = setTimeout(() => { $("burst").hidden = true; }, ms);
}

function showResult(s, m) {
  shownRound = m.round;
  const score = `You ${m.score.you} : ${m.score.them} ${m.names.them}`;
  const roundWon = s.won;
  const last = m.history[m.history.length - 1];
  const points = last ? ` This round: ${last.you}–${last.them}.` : "";
  let text = (s.message ||
    (s.runners.total > 1
      ? `${s.runners.total - s.runners.caught} of ${s.runners.total} Dodgers got away.`
      : s.role === "dodger"
        ? `You gave the Bounty Hunters the slip with ${s.stats.money} Ft left in your pocket.`
        : "The Dodger got away.")) + points;
  const host = s.room ? s.room.host : true;
  if (m.winner) {
    const won = m.winner === "you";
    $("result-emoji").textContent = won ? "🏆" : "💀";
    $("result-title").textContent = won ? "Match won!" : "Match lost!";
    $("result-text").textContent = `${text} Final score: ${score}.`;
    $("again").textContent = host ? "Back to the lobby (R)" : "Waiting for the host…";
    $("again").disabled = !host;
    $("again").dataset.action = "restart";
    $("to-menu").hidden = false;
  } else {
    $("result-emoji").textContent = roundWon ? "🎉" : roundWon === null ? "🤝" : "🚨";
    $("result-title").textContent =
      roundWon ? "Round won!" : roundWon === null ? "Round drawn" : "Round lost!";
    if (m.next_role && m.next_role !== s.role) {
      const why = m.round === Math.floor(m.regulation / 2) ? "Halftime! "
        : m.round === m.regulation ? "Overtime: win by two! " : "";
      text += ` ${why}You switch sides: next round you are the ${ROLE_LABEL[m.next_role]}.`;
    }
    $("result-text").textContent = `${text} ${score}.`;
    $("again").textContent = host ? "Next round now (Enter)" : "Next round soon…";
    $("again").disabled = !host;
    $("again").dataset.action = "next";
    $("to-menu").hidden = true;
  }
  $("result-stats").textContent = s.role === "dodger"
    ? `${s.clock} · ${s.stats.rides} rides · ${s.stats.tickets_bought} tickets · ` +
      `${s.stats.fines} fines · walked ${s.stats.walked_m} m`
    : `${s.clock} · ${s.stats.rides} rides · walked ${s.stats.walked_m} m`;
  $("history").innerHTML = m.history
    .map((r) => `<span class="dot ${r.won ? "won" : r.you === r.them ? "" : "lost"}" ` +
      `title="${ROLE_LABEL[r.role]} · ${r.you}–${r.them}">` +
      `${r.role === "dodger" ? "🏃" : "🤠"}</span>`)
    .join("");
  $("overlay").hidden = false;
}

function nextRound() {
  if (match && match.winner) restart();
  else send({ type: "next" });
}

function restart() {
  setFollow(true);
  send({ type: "restart" });
}

// --- input ------------------------------------------------------------------------------

function sendKeys() {
  send({ type: "keys", ...keys, bearing: map ? map.getBearing() : 0 });
}

window.addEventListener("keydown", (ev) => {
  unlock();
  if (ev.ctrlKey || ev.metaKey || ev.altKey) return;
  if (ev.target instanceof HTMLInputElement) return; // typing a name or a code
  if (!$("menu").hidden || !$("lobby").hidden) return;
  const dir = KEYMAP[ev.code];
  if (dir) {
    ev.preventDefault();
    if (!keys[dir]) { keys[dir] = true; sendKeys(); }
    return;
  }
  if (ev.repeat) return;
  if (ev.code === "KeyM") { toast("info", toggleMute() ? "🔇 Sound off (M)" : "🔊 Sound on (M)"); return; }
  if (ev.code === "Escape") {
    if (match && match.winner) quitToMenu();
    else if (confirm("Leave the match and go back to the main menu?")) quitToMenu();
    return;
  }
  if (ev.code === "Enter" || ev.code === "NumpadEnter") { ev.preventDefault(); nextRound(); }
  else if (ev.code === "KeyE" || ev.code === "Space") {
    ev.preventDefault(); send({ type: "use" });
  }
  else if (/^Digit[1-6]$/.test(ev.code)) {
    // Send the trip on screen: by the time this arrives, the list may have moved on.
    const index = Number(ev.code.slice(5)) - 1;
    send({ type: "wait", index, trip: shownDepartures[index]?.trip });
  }
  else if (ev.code === "KeyT") send({ type: "ticket" });
  else if (ev.code === "KeyC") send({ type: "coffee" });
  else if (ev.code === "KeyF") setFollow(true);
  else if (ev.code === "KeyN" && map) { map.setBearing(0); sendKeys(); }
  else if (ev.code === "Equal" || ev.code === "NumpadAdd") zoomBy(1);
  else if (ev.code === "Minus" || ev.code === "NumpadSubtract") zoomBy(-1);
  else if (ev.code === "KeyR") restart();
});

window.addEventListener("pointerdown", unlock);

window.addEventListener("keyup", (ev) => {
  const dir = KEYMAP[ev.code];
  if (dir && keys[dir]) { keys[dir] = false; sendKeys(); }
});

window.addEventListener("blur", () => {
  for (const k of Object.keys(keys)) keys[k] = false;
  sendKeys();
});

// The stop panel is redrawn every snapshot, so a click (down and up on one element) can miss.
$("stop").addEventListener("pointerdown", (ev) => {
  if (ev.target.closest("#buy")) send({ type: "ticket" });
  if (ev.target.closest("#coffee")) send({ type: "coffee" });
  if (ev.target.closest("#take-parcel")) send({ type: "use" });
});
// Hovering a departure draws where it goes.
$("stop").addEventListener("mouseover", (ev) => {
  hoverLine = ev.target.closest(".dep")?.dataset.line ?? null;
});
$("stop").addEventListener("mouseleave", () => { hoverLine = null; });
$("again").addEventListener("click", () => {
  if ($("again").dataset.action === "restart") restart();
  else nextRound();
});
$("follow").addEventListener("click", () => setFollow(true));
$("to-menu").addEventListener("click", quitToMenu);
$("ride-toggle").addEventListener("click", () => send({ type: "use" }));
try { $("player-name").value = localStorage.getItem("fare-dodger-name") || ""; } catch { /* */ }
// Match length (best of 3 or 6), remembered like the name.
let rounds = 3;
try { rounds = Number(localStorage.getItem("fare-dodger-rounds")) || 3; } catch { /* */ }
function pickRounds(n) {
  rounds = n;
  for (const b of document.querySelectorAll("#match-length [data-rounds]")) {
    b.classList.toggle("chosen", Number(b.dataset.rounds) === n);
  }
  try { localStorage.setItem("fare-dodger-rounds", String(n)); } catch { /* */ }
}
for (const button of document.querySelectorAll("#match-length [data-rounds]")) {
  button.addEventListener("click", () => pickRounds(Number(button.dataset.rounds)));
}
pickRounds(rounds);
$("play-solo").addEventListener("click", () =>
  openGame({ type: "solo", rounds, name: playerName() }));
for (const button of document.querySelectorAll("#menu [data-size]")) {
  button.addEventListener("click", () =>
    openGame({ type: "create", size: Number(button.dataset.size), rounds, name: playerName() }));
}
$("join").addEventListener("click", () => {
  const code = $("join-code").value.trim().toUpperCase();
  if (code.length !== 4) { menuError("Type the 4-letter code of the game."); return; }
  openGame({ type: "join", code, name: playerName() });
});
$("join-code").addEventListener("keydown", (ev) => { if (ev.key === "Enter") $("join").click(); });
$("switch-team").addEventListener("click", () => send({ type: "team" }));
$("start-game").addEventListener("click", () => send({ type: "start" }));
$("leave-lobby").addEventListener("click", quitToMenu);

// --- round timer and countdown ------------------------------------------------------------

function renderTimer(s, m) {
  $("topbar").hidden = false;
  const real = s.remaining_s / world.time_scale;
  $("round-timer").textContent = mmss(real);
  $("round-timer").parentElement.classList.toggle("low", real <= 30 && s.status === "running");
  const free = s.runners.total > 1
    ? ` · ${s.runners.total - s.runners.caught}/${s.runners.total} free` : "";
  $("round-label").textContent =
    (m.overtime ? `Overtime ${m.round}` : `Round ${m.round}/${m.regulation}`) + free;
}

function renderCountdown(s, m) {
  const left = s.freeze_s || 0;
  $("countdown").hidden = left <= 0;
  if (left <= 0) return;
  const n = String(Math.ceil(left));
  const el = $("countdown-number");
  if (el.textContent !== n) {
    el.textContent = n;
    el.style.animation = "none";
    void el.offsetWidth; // restart the pop
    el.style.animation = "";
  }
  $("countdown-what").textContent =
    `Round ${m.round} · you are the ${ROLE_LABEL[m.role]} · ${world.name}`;
}

// --- get on / get off switch (blue: riding or getting on, red: getting off) ----------------

function renderToggle(s) {
  const el = $("ride-toggle");
  let state = null;
  let title = "";
  let detail = "";
  if (s.ride) {
    const r = s.ride;
    if (r.fined) {
      state = "off-req locked"; title = "GETTING OFF";
      detail = `Inspectors: off at ${r.alight_at}`;
    } else if (r.alight_at) {
      state = "off-req"; title = "GETTING OFF"; detail = `at ${r.alight_at} · tap to stay on`;
    } else {
      state = "on"; title = "ON BOARD"; detail = `${r.route} → ${r.headsign} · tap to get off`;
    }
  } else if (s.waiting) {
    state = "on"; title = "GETTING ON";
    detail = `${s.waiting.route} in ${mmss(s.waiting.in_s / world.time_scale)} · tap to cancel`;
  } else if (s.stop && s.stop.departures.length && s.player.state === "walk") {
    state = ""; title = "GET ON";
    const d = s.stop.departures[0];
    detail = `${d.route} → ${d.headsign} · tap or E`;
  }
  el.hidden = state === null || s.status !== "running";
  if (el.hidden) return;
  el.className = state;
  $("ride-title").textContent = title;
  $("ride-detail").textContent = detail;
}

// --- minimap: the district, you, and for Bounty Hunters the reports and the team ----------

const mini = { canvas: null, ctx: null, base: null, district: -1, project: null, dpr: 1 };

function setupMinimap() {
  mini.canvas = $("minimap");
  mini.dpr = window.devicePixelRatio || 1;
  mini.canvas.width = 220 * mini.dpr;
  mini.canvas.height = 220 * mini.dpr;
  mini.ctx = mini.canvas.getContext("2d");
}

function minimapPath(c, points) {
  c.beginPath();
  points.forEach((p, i) => {
    const [x, y] = mini.project(p);
    if (i) c.lineTo(x, y); else c.moveTo(x, y);
  });
}

// The district's outline as [lat, lon] points: its polygon, or its circle (most districts).
function areaRing(area) {
  if (area.kind === "polygon") return area.vertices;
  const dLat = area.radius_m / 111320;
  const dLon = dLat / Math.cos((area.center_lat * Math.PI) / 180);
  return Array.from({ length: 64 }, (_, i) => {
    const a = (i / 64) * 2 * Math.PI;
    return [area.center_lat + dLat * Math.sin(a), area.center_lon + dLon * Math.cos(a)];
  });
}

function minimapBase() {
  // The district outline, lines, stations and cafés, drawn once per district. Sized to the
  // whole district: before, circle districts were sized to their stations only, so you
  // could walk off the minimap's edge.
  const size = 220 * mini.dpr;
  const pad = 14 * mini.dpr;
  const ring = areaRing(world.area);
  const kx = Math.cos((world.center[0] * Math.PI) / 180);
  const xs = ring.map(([, lon]) => lon * kx);
  const ys = ring.map(([lat]) => lat);
  const [x0, x1, y0, y1] = [Math.min(...xs), Math.max(...xs), Math.min(...ys), Math.max(...ys)];
  const scale = (size - 2 * pad) / Math.max(x1 - x0, y1 - y0);
  const ox = (size - (x1 - x0) * scale) / 2;
  const oy = (size - (y1 - y0) * scale) / 2;
  mini.project = ([lat, lon]) => [ox + (lon * kx - x0) * scale, oy + (y1 - lat) * scale];
  mini.perMetre = scale / 111320;

  const base = document.createElement("canvas");
  base.width = base.height = size;
  const c = base.getContext("2d");
  c.fillStyle = "#e9dcc0";
  c.fillRect(0, 0, size, size);
  minimapPath(c, ring);
  c.closePath();
  c.fillStyle = "#fffaf0";
  c.fill();
  c.lineWidth = 2 * mini.dpr;
  c.setLineDash([6 * mini.dpr, 4 * mini.dpr]);
  c.strokeStyle = INK;
  c.stroke();
  c.setLineDash([]);
  c.lineCap = c.lineJoin = "round";
  c.globalAlpha = 0.7;
  c.lineWidth = 2.2 * mini.dpr;
  for (const line of world.lines) {
    minimapPath(c, line.path);
    c.strokeStyle = MODE_COLORS[line.mode] || "#888";
    c.stroke();
  }
  c.globalAlpha = 1;
  for (const st of world.stations) {
    const [x, y] = mini.project(st.pos);
    c.beginPath();
    c.arc(x, y, 2.2 * mini.dpr, 0, 2 * Math.PI);
    c.fillStyle = "#fff";
    c.fill();
    c.lineWidth = 1.2 * mini.dpr;
    c.strokeStyle = INK;
    c.stroke();
  }
  c.fillStyle = "#8b5a2b";
  for (const cafe of world.cafes || []) {
    const [x, y] = mini.project(cafe.pos);
    c.fillRect(x - 3 * mini.dpr, y - 3 * mini.dpr, 6 * mini.dpr, 6 * mini.dpr);
  }
  mini.base = base;
  mini.district = district;
}

function miniDot(c, pos, color, radius, ring = INK) {
  const [x, y] = mini.project(pos);
  c.beginPath();
  c.arc(x, y, radius * mini.dpr, 0, 2 * Math.PI);
  c.fillStyle = color;
  c.fill();
  c.lineWidth = 2 * mini.dpr;
  c.strokeStyle = ring;
  c.stroke();
}

function drawMinimap(s) {
  mini.canvas.hidden = false;
  if (mini.district !== district) minimapBase();
  const c = mini.ctx;
  c.drawImage(mini.base, 0, 0);
  if (s.jail) miniDot(c, s.jail.pos, s.jail.busy ? "#e03131" : "#1971c2", 4.5, "#fff");
  if (s.role === "hunter") {
    for (const ping of s.pings || []) {
      // Where each Dodger was last reported: a circle that fades and grows with its age.
      const [x, y] = mini.project(ping.pos);
      const fresh = Math.max(0, 1 - ping.age_s / world.ping_every_s);
      const r = Math.max(6 * mini.dpr, (60 + ping.age_s * 1.4) * mini.perMetre);
      c.beginPath();
      c.arc(x, y, r, 0, 2 * Math.PI);
      c.fillStyle = `rgba(224, 49, 49, ${0.12 + 0.2 * fresh})`;
      c.fill();
      c.lineWidth = 2 * mini.dpr;
      c.strokeStyle = `rgba(224, 49, 49, ${0.4 + 0.6 * fresh})`;
      c.stroke();
      c.font = `700 ${13 * mini.dpr}px Fredoka, sans-serif`;
      c.textAlign = "center";
      c.textBaseline = "middle";
      c.fillStyle = INK;
      c.fillText("?", x, y);
    }
    for (const h of s.hunters) miniDot(c, h.pos, "#ffa8a8", 4);
    for (const m of s.marks || []) miniDot(c, m.pos, "#fff", 4, "#7950f2");
    miniDot(c, s.player.pos, "#fa5252", 5.5, "#fff");
  } else {
    for (const pos of s.packages || []) miniDot(c, pos, "#ffd43b", 3.5);
    for (const m of s.marks || []) miniDot(c, m.pos, "#fff", 4, "#7950f2");
    if (s.delivery) miniDot(c, s.delivery.pos, "#2f9e44", 5, "#fff");
    // The Dodgers' map shows their own team, never the Bounty Hunters.
    for (const d of s.dodgers) miniDot(c, d.pos, d.caught ? "#adb5bd" : "#8ce99a", 4);
    miniDot(c, s.player.pos, "#40c057", 5.5, "#fff");
  }
}

// --- arrow at the screen edge towards the latest report (Bounty Hunters) -------------------

function metresBetween([lat1, lon1], [lat2, lon2]) {
  const k = Math.cos((((lat1 + lat2) / 2) * Math.PI) / 180);
  return Math.hypot((lat2 - lat1) * 111320, (lon2 - lon1) * 111320 * k);
}

function renderPingArrow(s) {
  const show = s.role === "hunter" && s.ping && s.status === "running";
  edgeArrow($("ping-arrow"), $("ping-pointer"), $("ping-dist"), show ? s.ping.pos : null, s);
  const goal = s.delivery && s.status === "running" ? s.delivery.pos : null;
  edgeArrow($("goal-arrow"), $("goal-pointer"), $("goal-dist"), goal, s);
}

// Zoomed in and tilted, a far point can lie beyond the horizon, where project() returns
// nonsense: projecting back and comparing tells whether it really is in view.
function inView(pos, margin = 0) {
  const box = map.getContainer().getBoundingClientRect();
  const p = map.project(lngLat(pos));
  if (p.x < margin || p.y < margin || p.x > box.width - margin || p.y > box.height - margin) {
    return false;
  }
  const back = map.unproject(p);
  return metresBetween([back.lat, back.lng], pos) < 25;
}

function showIfOnScreen(marker, pos) {
  marker.getElement().style.visibility = inView(pos) ? "" : "hidden";
}

// An arrow at the screen edge pointing at ``target`` (lat, lon) when it is off screen.
function edgeArrow(el, pointer, label, target, s) {
  if (!target) { el.hidden = true; return; }
  const box = map.getContainer().getBoundingClientRect();
  // The frame the arrow runs along: clear of the top bar and the bottom controls.
  const left = 60;
  const right = box.width - 60;
  const top = 110;
  const bottom = box.height - 120;
  // On screen? Zoomed in and tilted, a far point can lie beyond the horizon, where
  // project() returns nonsense; projecting back and comparing catches that.
  const p = map.project(lngLat(target));
  const back = map.unproject(p);
  const onScreen = p.x > left && p.x < right && p.y > top && p.y < bottom &&
    metresBetween([back.lat, back.lng], target) < 25;
  if (onScreen) { el.hidden = true; return; }
  // The direction from you to the target, turned with the map (not from the projection).
  const from = s.player.pos;
  const k = Math.cos((from[0] * Math.PI) / 180);
  const east = (target[1] - from[1]) * 111320 * k;
  const north = (target[0] - from[0]) * 111320;
  const b = (map.getBearing() * Math.PI) / 180;
  const sx = east * Math.cos(b) - north * Math.sin(b);
  const sy = -(east * Math.sin(b) + north * Math.cos(b)); // screen y grows downwards
  if (!sx && !sy) { el.hidden = true; return; }
  const me = map.project(lngLat(from));
  const cx = Math.min(right - 1, Math.max(left + 1, me.x));
  const cy = Math.min(bottom - 1, Math.max(top + 1, me.y));
  const tx = sx > 0 ? (right - cx) / sx : sx < 0 ? (left - cx) / sx : Infinity;
  const ty = sy > 0 ? (bottom - cy) / sy : sy < 0 ? (top - cy) / sy : Infinity;
  const t = Math.min(tx, ty);
  el.style.left = `${cx + sx * t}px`;
  el.style.top = `${cy + sy * t}px`;
  pointer.style.transform = `rotate(${Math.atan2(sy, sx)}rad)`;
  const m = metresBetween(from, target);
  label.textContent = m >= 1000 ? `${(m / 1000).toFixed(1)} km` : `${Math.round(m)} m`;
  el.hidden = false;
}

init();
