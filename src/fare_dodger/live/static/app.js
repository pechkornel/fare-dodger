// Fare Dodger live client: draws server snapshots on a MapLibre map and sends input. Zoomed
// out the map is flat with DOM markers; zoomed in it tilts into the 3D toy town (world3d.js).
import { toonStyle } from "./toon-style.js";
import { MAX_PITCH, SWITCH_ZOOM, ToyTown, pitchForZoom } from "./world3d.js";

const KEYMAP = {
  KeyW: "up", ArrowUp: "up", KeyS: "down", ArrowDown: "down",
  KeyA: "left", ArrowLeft: "left", KeyD: "right", ArrowRight: "right",
};
const MODE_COLORS = { metro: "#7950f2", tram: "#fcc419", trolleybus: "#fd7e14", bus: "#339af0" };
const MODE_ICONS = { metro: "🚇", tram: "🚋", trolleybus: "🚎", bus: "🚌" };
const INK = "#2b2118";
const MIN_ZOOM = 12.5;
const MAX_ZOOM = 19;
const START_ZOOM = 17.2;

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
let shownRound = null;   // round whose result overlay is showing
let burstTimer = null;
let match = null;
const keys = { up: false, down: false, left: false, right: false };
const last = { money: null, tickets: null };
// Camera: zoom eases towards cam.zoom, the tilt follows the zoom, and the centre follows you.
const cam = { zoom: START_ZOOM, target: null };

const markers = { vehicles: new Map(), hunters: [], player: null, dodger: null };

// The main menu shows over the first district's map; a match starts from there.
let leaving = false;     // closing the connection on purpose (back to the menu)
let pending = null;      // newest snapshot not drawn yet
let pendingEvents = [];  // messages from every snapshot since the last frame

function init() {
  setupMinimap();
  showMenu();
  loadDistrict(0);
  requestAnimationFrame(frame);
}

function showMenu() {
  document.body.classList.add("in-menu");
  $("menu").hidden = false;
  $("overlay").hidden = true;
  $("countdown").hidden = true;
}

function startMatch(role) {
  document.body.classList.remove("in-menu");
  $("menu").hidden = true;
  shownRound = null;
  match = null;
  connect(role);
}

function quitToMenu() {
  if (ws) { leaving = true; ws.close(); ws = null; }
  for (const k of Object.keys(keys)) keys[k] = false;
  showMenu();
  district = -1;
  loadDistrict(0);
}

function createMap(w) {
  map = new maplibregl.Map({
    container: "map",
    style: toonStyle(w.tiles.url, w.tiles.glyphs),
    center: lngLat(w.center), zoom: cam.zoom, pitch: pitchForZoom(cam.zoom),
    minZoom: MIN_ZOOM, maxZoom: MAX_ZOOM, maxPitch: MAX_PITCH,
    attributionControl: { compact: true },
    dragRotate: false, pitchWithRotate: false, touchPitch: false, keyboard: false,
    scrollZoom: false, doubleClickZoom: false, boxZoom: false,
  });
  map.touchZoomRotate.disableRotation();
  map.on("dragstart", () => { dragging = true; setFollow(false); });
  map.on("dragend", () => { dragging = false; });
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
  if (!map || dragging) return;
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
      view.center = [c.lng + (lon - c.lng) * 0.2, c.lat + (lat - c.lat) * 0.2];
    }
  }
  if (Object.keys(view).length) map.jumpTo(view);
  $("map").classList.toggle("toy", zoom >= SWITCH_ZOOM);
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
  for (const m of markers.vehicles.values()) m.remove();
  markers.vehicles.clear();
  for (const m of markers.hunters) m.remove();
  markers.hunters = [];
  for (const key of ["player", "dodger"]) {
    if (markers[key]) { markers[key].remove(); markers[key] = null; }
  }
  last.money = last.tickets = null;
}

function connect(role) {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  leaving = false;
  ws = new WebSocket(`${proto}://${location.host}/ws?role=${encodeURIComponent(role)}`);
  ws.onmessage = (ev) => {
    // Draw only the newest snapshot (on the next frame), so a slow frame never builds up a
    // queue of stale ones; keep every snapshot's messages.
    const snapshot = JSON.parse(ev.data);
    pendingEvents.push(...snapshot.events);
    pending = snapshot;
  };
  ws.onclose = () => {
    if (!leaving) toast("end", "Disconnected from the server. Reload to play again.");
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
  match = s.match;
  if (match.district !== district) {
    if (!loading) loadDistrict(match.district);
    return; // draw once the district's map is in
  }
  for (const e of s.events) {
    toast(e.kind, e.text);
    if (e.kind === "inspection") burst(e.text.includes("valid") ? "good" : "bad");
  }
  renderVehicles(s.vehicles, s.ride ? s.ride.trip : null);
  renderHunters(s.hunters, s.role);
  renderDodger(s.dodger);
  renderGround(s);
  renderPlayer(s.player, s.role);
  town.update(s);
  renderScore(match);
  renderHud(s);
  renderTimer(s, match);
  renderCountdown(s, match);
  renderToggle(s);
  drawMinimap(s);
  renderPingArrow(s);
  cam.target = s.player.pos;
  if (match.round_over && shownRound !== match.round) showResult(s, match);
  if (!match.round_over) { $("overlay").hidden = true; shownRound = null; }
}

function renderVehicles(list, myTrip) {
  const seen = new Set();
  for (const [trip, lat, lon, mode, route] of list) {
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
    el.style.zIndex = trip === myTrip ? 900 : 1;
  }
  for (const [trip, m] of markers.vehicles) {
    if (!seen.has(trip)) { m.remove(); markers.vehicles.delete(trip); }
  }
}

function renderHunters(list, role) {
  // As the Dodger these are the enemy; as a Bounty Hunter, your bot teammates.
  const team = role === "hunter" ? " team" : "";
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

function renderDodger(dodger) {
  if (!dodger) {
    if (markers.dodger) { markers.dodger.remove(); markers.dodger = null; }
    return;
  }
  const cls = `toon wanted${dodger.held ? " held" : ""}`;
  if (!markers.dodger) {
    markers.dodger = divMarker(dodger.pos, `<div class="${cls}">🏃</div>`, 950);
  }
  markers.dodger.setLngLat(lngLat(dodger.pos));
  markers.dodger.getElement().firstChild.className = cls;
}

// Sight circles of the Bounty Hunters on foot and the last radar ping, drawn on the ground.
function renderGround(s) {
  const features = s.hunters.filter((h) => !h.riding).map((h) => feature(
    { type: "Polygon", coordinates: [circle(h.pos, world.sight_radius_m)] }, { kind: "sight" }));
  if (s.ping) {
    const fade = Math.max(0.15, 1 - s.ping.age_s / world.ping_every_s);
    features.push(feature({ type: "Polygon", coordinates: [circle(s.ping.pos, 60)] },
      { kind: "ping", fade }));
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
  $("score-bots").textContent = m.score.bots;
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
  $("clock").textContent = s.clock;
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
    $("nearest").textContent =
      `Nearest Bounty Hunter ${near >= 1000 ? (near / 1000).toFixed(1) + " km" : near + " m"}`;
    $("nearest").className = near <= world.sight_radius_m * 2 ? "danger" : "";
  }

  let status;
  let held = false;
  if (s.ride) {
    const r = s.ride;
    const ticket = hunter ? "" :
      r.fined ? " · 🚨 fined" : r.ticket ? " · 🎫 valid" : " · 🙈 no ticket";
    status = `Riding ${pill(r.mode, r.route)} → ${esc(r.headsign)}<br>` +
      `<span class="muted">Next: ${esc(r.next_stop)}${ticket}` +
      (r.alight_at ? ` · off at ${esc(r.alight_at)}` : " · E to get off") + "</span>";
  } else if (s.held_s !== null) {
    held = true;
    status = `📝 The inspectors are writing your fine… ` +
      `<b>${mmss(s.held_s / world.time_scale)}</b><br>` +
      `<span class="muted">${world.fine_ft} Ft, and the Bounty Hunters know where you are!</span>`;
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

  const cafe = coffee.cafe && coffee.brewing_s == null
    ? `<h3><span>☕ ${esc(coffee.cafe)}</span>` +
      `<span class="chip" id="coffee">C · ☕ ${world.coffee_price_ft} Ft</span></h3>` +
      `<div class="muted">Runs you almost bus-fast for ` +
      `${mmss(world.coffee_boost_s / world.time_scale)}.</div>`
    : "";
  if (!s.stop) { $("stop").innerHTML = cafe; return; }
  const rows = s.stop.departures.map((d, i) =>
    `<div class="dep${d.waiting ? " waiting" : ""}"><span class="key">${i + 1}</span>` +
    pill(d.mode, d.route) +
    `<span class="headsign">${esc(d.headsign)}</span>` +
    `<span class="in">${d.in_s <= 0 ? "now" : mmss(d.in_s / world.time_scale)}</span></div>`);
  const buy = s.wallet
    ? `<span class="chip" id="buy">T · 🎫 ${world.ticket_price_ft} Ft</span>` : "";
  $("stop").innerHTML = cafe + `<h3><span>🚏 ${esc(s.stop.name)}</span>${buy}</h3>` +
    (rows.length ? rows.join("") : '<div class="muted">No departures soon.</div>');
}

function toast(kind, text) {
  const el = document.createElement("div");
  el.className = `toast ${kind}`;
  el.textContent = text;
  $("feed").prepend(el);
  setTimeout(() => el.remove(), 6000);
  while ($("feed").children.length > 5) $("feed").lastChild.remove();
}

function burst(kind) {
  $("burst-text").textContent = kind === "good" ? "Ticket OK! 🎫" : "KONTROLL! 🚨";
  $("burst").className = kind;
  $("burst").hidden = false;
  clearTimeout(burstTimer);
  burstTimer = setTimeout(() => { $("burst").hidden = true; }, 1600);
}

function showResult(s, m) {
  shownRound = m.round;
  const score = `You ${m.score.you} : ${m.score.bots} Bots`;
  const roundWon = s.won;
  let text = s.message ||
    (s.role === "dodger"
      ? `You gave the Bounty Hunters the slip with ${s.wallet.money} Ft left in your pocket.`
      : "The Dodger got away.");
  if (m.winner) {
    const won = m.winner === "you";
    $("result-emoji").textContent = won ? "🏆" : "💀";
    $("result-title").textContent = won ? "Match won!" : "Match lost!";
    $("result-text").textContent = `${text} Final score: ${score}.`;
    $("again").textContent = "New match (R)";
    $("again").dataset.action = "restart";
    $("to-menu").hidden = false;
  } else {
    $("result-emoji").textContent = roundWon ? "🎉" : "🚨";
    $("result-title").textContent = roundWon ? "Round won!" : "Round lost!";
    if (m.next_role && m.next_role !== s.role) {
      const why = m.round === m.regulation / 2 ? "Halftime! "
        : m.round === m.regulation ? "Overtime: win by two! " : "";
      text += ` ${why}You switch sides: next round you are the ${ROLE_LABEL[m.next_role]}.`;
    }
    $("result-text").textContent = `${text} ${score}.`;
    $("again").textContent = "Next round (Enter)";
    $("again").dataset.action = "next";
    $("to-menu").hidden = true;
  }
  $("result-stats").textContent = s.role === "dodger"
    ? `${s.clock} · ${s.stats.rides} rides · ${s.stats.tickets_bought} tickets · ` +
      `${s.stats.fines} fines · walked ${s.stats.walked_m} m`
    : `${s.clock} · ${s.stats.rides} rides · walked ${s.stats.walked_m} m`;
  $("history").innerHTML = m.history
    .map((r) => `<span class="dot ${r.won ? "won" : "lost"}" title="${ROLE_LABEL[r.role]}">` +
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

function sendKeys() { send({ type: "keys", ...keys }); }

window.addEventListener("keydown", (ev) => {
  if (ev.ctrlKey || ev.metaKey || ev.altKey) return;
  const dir = KEYMAP[ev.code];
  if (dir) {
    ev.preventDefault();
    if (!keys[dir]) { keys[dir] = true; sendKeys(); }
    return;
  }
  if (ev.repeat) return;
  if (!$("menu").hidden) return;
  if (ev.code === "Escape") {
    if (match && match.winner) quitToMenu();
    else if (confirm("Leave the match and go back to the main menu?")) quitToMenu();
    return;
  }
  if (ev.code === "Enter" || ev.code === "NumpadEnter") { ev.preventDefault(); nextRound(); }
  else if (ev.code === "KeyE" || ev.code === "Space") {
    ev.preventDefault(); send({ type: "use" });
  }
  else if (/^Digit[1-6]$/.test(ev.code)) send({ type: "wait", index: Number(ev.code.slice(5)) - 1 });
  else if (ev.code === "KeyT") send({ type: "ticket" });
  else if (ev.code === "KeyC") send({ type: "coffee" });
  else if (ev.code === "KeyF") setFollow(true);
  else if (ev.code === "Equal" || ev.code === "NumpadAdd") zoomBy(1);
  else if (ev.code === "Minus" || ev.code === "NumpadSubtract") zoomBy(-1);
  else if (ev.code === "KeyR") restart();
});

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
});
$("again").addEventListener("click", () => {
  if ($("again").dataset.action === "restart") restart();
  else nextRound();
});
$("follow").addEventListener("click", () => setFollow(true));
$("to-menu").addEventListener("click", quitToMenu);
$("ride-toggle").addEventListener("click", () => send({ type: "use" }));
for (const button of document.querySelectorAll("#menu [data-role]")) {
  button.addEventListener("click", () => startMatch(button.dataset.role));
}

// --- round timer and countdown ------------------------------------------------------------

function renderTimer(s, m) {
  $("topbar").hidden = false;
  const real = s.remaining_s / world.time_scale;
  $("round-timer").textContent = mmss(real);
  $("round-timer").parentElement.classList.toggle("low", real <= 30 && s.status === "running");
  $("round-label").textContent =
    m.overtime ? `Overtime ${m.round}` : `Round ${m.round}/${m.regulation}`;
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

function minimapBase() {
  // The district outline, lines, stations and cafés, drawn once per district.
  const size = 220 * mini.dpr;
  const pad = 14 * mini.dpr;
  const area = world.area;
  const ring = area.kind === "polygon" ? area.vertices : world.stations.map((st) => st.pos);
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
  if (area.kind === "polygon") {
    minimapPath(c, area.vertices);
    c.closePath();
    c.fillStyle = "#fffaf0";
    c.fill();
    c.lineWidth = 2 * mini.dpr;
    c.setLineDash([6 * mini.dpr, 4 * mini.dpr]);
    c.strokeStyle = INK;
    c.stroke();
    c.setLineDash([]);
  }
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
  if (s.role === "hunter") {
    if (s.ping) {
      // Where the Dodger was last reported: a circle that fades and grows with its age.
      const [x, y] = mini.project(s.ping.pos);
      const fresh = Math.max(0, 1 - s.ping.age_s / world.ping_every_s);
      const r = Math.max(6 * mini.dpr, (60 + s.ping.age_s * 1.4) * mini.perMetre);
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
    if (s.dodger) miniDot(c, s.dodger.pos, "#ffd43b", 5);
    miniDot(c, s.player.pos, "#fa5252", 5.5, "#fff");
  } else {
    // The Dodger's map shows where you are, never the Bounty Hunters.
    miniDot(c, s.player.pos, "#40c057", 5.5, "#fff");
  }
}

// --- arrow at the screen edge towards the latest report (Bounty Hunters) -------------------

function metresBetween([lat1, lon1], [lat2, lon2]) {
  const k = Math.cos((((lat1 + lat2) / 2) * Math.PI) / 180);
  return Math.hypot((lat2 - lat1) * 111320, (lon2 - lon1) * 111320 * k);
}

function renderPingArrow(s) {
  const el = $("ping-arrow");
  if (s.role !== "hunter" || !s.ping || s.status !== "running") { el.hidden = true; return; }
  const box = map.getContainer().getBoundingClientRect();
  const p = map.project(lngLat(s.ping.pos));
  // The frame the arrow runs along: clear of the top bar and the bottom controls.
  const left = 60;
  const right = box.width - 60;
  const top = 110;
  const bottom = box.height - 120;
  const inside = p.x > left && p.x < right && p.y > top && p.y < bottom;
  const cx = box.width / 2;
  const cy = box.height / 2;
  const dx = p.x - cx;
  const dy = p.y - cy;
  if (inside || !Number.isFinite(dx) || !Number.isFinite(dy) || (!dx && !dy)) {
    el.hidden = true;
    return;
  }
  const tx = dx > 0 ? (right - cx) / dx : dx < 0 ? (left - cx) / dx : Infinity;
  const ty = dy > 0 ? (bottom - cy) / dy : dy < 0 ? (top - cy) / dy : Infinity;
  const t = Math.min(tx, ty);
  el.style.left = `${cx + dx * t}px`;
  el.style.top = `${cy + dy * t}px`;
  $("ping-pointer").style.transform = `rotate(${Math.atan2(dy, dx)}rad)`;
  const m = metresBetween(s.player.pos, s.ping.pos);
  $("ping-dist").textContent = m >= 1000 ? `${(m / 1000).toFixed(1)} km` : `${Math.round(m)} m`;
  el.hidden = false;
}

init();

