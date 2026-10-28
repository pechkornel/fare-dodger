// Fare Dodger live client: draws server snapshots on a Leaflet map and sends input.
"use strict";

const KEYMAP = {
  KeyW: "up", ArrowUp: "up", KeyS: "down", ArrowDown: "down",
  KeyA: "left", ArrowLeft: "left", KeyD: "right", ArrowRight: "right",
};
const MODE_COLORS = { metro: "#7048e8", tram: "#f2c200", trolleybus: "#e8590c", bus: "#1c7ed6" };

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s).replace(/[&<>"']/g, (c) => `&#${c.charCodeAt(0)};`);
const mmss = (s) => {
  s = Math.max(0, Math.round(s));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
};

let world = null;
let map = null;
let ws = null;
let follow = true;
let gameOver = false;
const keys = { up: false, down: false, left: false, right: false };

const markers = { vehicles: new Map(), inspectors: [], sight: [], player: null, ping: null };

async function init() {
  world = await (await fetch("api/world")).json();
  map = L.map("map", { keyboard: false, zoomControl: true, preferCanvas: true })
    .setView(world.center, 16);
  L.tileLayer(world.tiles.url, { maxZoom: 19, attribution: world.tiles.attribution }).addTo(map);
  drawArea(world.area);
  for (const line of world.lines) {
    L.polyline(line.path, {
      color: MODE_COLORS[line.mode] || "#888", weight: 3, opacity: 0.35, interactive: false,
    }).addTo(map);
  }
  for (const st of world.stations) {
    L.circleMarker(st.pos, {
      radius: 4, color: "#343a40", weight: 2, fillColor: "#fff", fillOpacity: 1,
    }).bindTooltip(esc(st.name), { direction: "top", offset: [0, -4] }).addTo(map);
  }
  map.on("dragstart", () => setFollow(false));
  connect();
}

function drawArea(area) {
  const style = { color: "#343a40", weight: 2, dashArray: "6 6", fill: false, interactive: false };
  if (area.kind === "polygon") {
    // Dim everything outside the play area: a world-sized ring with the area as a hole.
    const world = [[-89, -179], [-89, 179], [89, 179], [89, -179]];
    L.polygon([world, area.vertices], {
      stroke: false, fillColor: "#000", fillOpacity: 0.35, interactive: false,
    }).addTo(map);
    L.polygon(area.vertices, style).addTo(map);
  } else if (area.kind === "circle") {
    L.circle([area.center_lat, area.center_lon], { ...style, radius: area.radius_m }).addTo(map);
  } else if (area.kind === "bbox") {
    L.rectangle([[area.min_lat, area.min_lon], [area.max_lat, area.max_lon]], style).addTo(map);
  }
}

function connect() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  ws = new WebSocket(`${proto}://${location.host}/ws`);
  ws.onmessage = (ev) => render(JSON.parse(ev.data));
  ws.onclose = () => toast("end", "Disconnected from the server. Reload to play again.");
}

function send(msg) {
  if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(msg));
}

function setFollow(on) {
  follow = on;
  $("follow").hidden = on;
}

// --- rendering --------------------------------------------------------------------------

function divMarker(pos, html, className, size) {
  return L.marker(pos, {
    icon: L.divIcon({ html, className, iconSize: size, iconAnchor: [size[0] / 2, size[1] / 2] }),
    interactive: false, keyboard: false,
  }).addTo(map);
}

function render(s) {
  for (const e of s.events) toast(e.kind, e.text);
  renderVehicles(s.vehicles);
  renderInspectors(s.inspectors);
  renderPing(s.ping);
  renderPlayer(s.player);
  renderHud(s);
  if (follow) map.setView(s.player.pos, map.getZoom(), { animate: false });
  const over = s.status !== "running";
  if (over && !gameOver) showResult(s);
  if (!over) $("overlay").hidden = true;
  gameOver = over;
}

function renderVehicles(list) {
  const seen = new Set();
  for (const [trip, lat, lon, mode, route] of list) {
    seen.add(trip);
    let m = markers.vehicles.get(trip);
    if (!m) {
      const html = `<span class="pill ${mode}">${esc(route)}</span>`;
      m = divMarker([lat, lon], html, "vehicle", [40, 18]);
      markers.vehicles.set(trip, m);
    } else {
      m.setLatLng([lat, lon]);
    }
  }
  for (const [trip, m] of markers.vehicles) {
    if (!seen.has(trip)) { m.remove(); markers.vehicles.delete(trip); }
  }
}

function renderInspectors(list) {
  list.forEach((insp, i) => {
    const cls = `inspector-dot${insp.riding ? " riding" : ""}${insp.chasing ? " chasing" : ""}`;
    if (!markers.inspectors[i]) {
      markers.inspectors[i] = divMarker(insp.pos, `<div class="${cls}"></div>`, "", [24, 24]);
      markers.sight[i] = L.circle(insp.pos, {
        radius: world.sight_radius_m, color: "#e03131", weight: 1, opacity: 0.5,
        fillOpacity: 0.06, interactive: false,
      }).addTo(map);
    }
    markers.inspectors[i].setLatLng(insp.pos);
    markers.inspectors[i].getElement().firstChild.className = cls;
    markers.sight[i].setLatLng(insp.pos);
    markers.sight[i].setStyle({ opacity: insp.riding ? 0 : 0.5, fillOpacity: insp.riding ? 0 : 0.06 });
  });
}

function renderPing(ping) {
  if (!ping) { if (markers.ping) { markers.ping.remove(); markers.ping = null; } return; }
  if (!markers.ping) {
    markers.ping = L.circle(ping.pos, {
      radius: 60, color: "#e03131", weight: 2, dashArray: "4 4", fillOpacity: 0.15,
      interactive: false,
    }).addTo(map);
  }
  const fade = Math.max(0.15, 1 - ping.age_s / world.ping_every_s);
  markers.ping.setLatLng(ping.pos);
  markers.ping.setStyle({ opacity: fade, fillOpacity: 0.15 * fade });
}

function renderPlayer(p) {
  const cls = `player-dot${p.state === "ride" ? " ride" : ""}`;
  if (!markers.player) {
    markers.player = divMarker(p.pos, `<div class="${cls}"></div>`, "", [26, 26]);
    markers.player.setZIndexOffset(1000);
  }
  markers.player.setLatLng(p.pos);
  markers.player.getElement().firstChild.className = cls;
}

function renderHud(s) {
  $("clock").textContent = s.clock;
  $("remaining").textContent = `${mmss(s.remaining_s / world.time_scale)} left`;
  const frac = 1 - s.next_ping_s / world.ping_every_s;
  $("ping-bar").style.width = `${Math.min(100, Math.max(0, frac * 100))}%`;
  $("ping-in").textContent = mmss(s.next_ping_s / world.time_scale);
  const near = s.nearest_inspector_m;
  $("nearest").textContent = `Nearest Inspector ${near >= 1000 ? (near / 1000).toFixed(1) + " km" : near + " m"}`;
  $("nearest").className = near <= world.sight_radius_m * 2 ? "danger" : "muted";

  let status;
  if (s.ride) {
    const r = s.ride;
    status = `Riding <span class="pill ${r.mode}">${esc(r.route)}</span> → ${esc(r.headsign)}<br>` +
      `<span class="muted">Next: ${esc(r.next_stop)}` +
      (r.alight_at ? ` · getting off at ${esc(r.alight_at)}` : " · E to get off") + "</span>";
  } else if (s.waiting) {
    status = `Waiting for ${esc(s.waiting.route)} → ${esc(s.waiting.headsign)} ` +
      `<span class="muted">(${mmss(s.waiting.in_s / world.time_scale)}, E to cancel)</span>`;
  } else {
    status = "On foot" + (s.stop ? "" : ' <span class="muted">· walk to a stop to ride</span>');
  }
  $("status").innerHTML = status;

  if (!s.stop) { $("stop").innerHTML = ""; return; }
  const rows = s.stop.departures.map((d, i) =>
    `<div class="dep${d.waiting ? " waiting" : ""}"><span class="key">${i + 1}</span>` +
    `<span class="pill ${d.mode}">${esc(d.route)}</span>` +
    `<span class="headsign">${esc(d.headsign)}</span>` +
    `<span class="in">${d.in_s <= 0 ? "now" : mmss(d.in_s / world.time_scale)}</span></div>`);
  $("stop").innerHTML = `<h3>${esc(s.stop.name)}</h3>` +
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

function showResult(s) {
  const won = s.status === "escaped";
  $("result-title").textContent = won ? "You escaped!" : "Caught!";
  $("result-text").textContent = won
    ? "You stayed hidden until the Inspectors gave up."
    : (s.message || "An Inspector caught you.");
  $("result-stats").textContent =
    `Time ${s.clock} · walked ${s.stats.walked_m} m · ${s.stats.rides} rides`;
  $("overlay").hidden = false;
}

function restart() {
  for (const m of markers.vehicles.values()) m.remove();
  markers.vehicles.clear();
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
  if (ev.code === "KeyE" || ev.code === "Space") { ev.preventDefault(); send({ type: "use" }); }
  else if (/^Digit[1-6]$/.test(ev.code)) send({ type: "wait", index: Number(ev.code.slice(5)) - 1 });
  else if (ev.code === "KeyF") setFollow(true);
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

$("again").addEventListener("click", restart);
$("follow").addEventListener("click", () => setFollow(true));

init();
