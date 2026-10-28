// Fare Dodger live client: draws server snapshots on a Leaflet map and sends input.
"use strict";

const KEYMAP = {
  KeyW: "up", ArrowUp: "up", KeyS: "down", ArrowDown: "down",
  KeyA: "left", ArrowLeft: "left", KeyD: "right", ArrowRight: "right",
};
const MODE_COLORS = { metro: "#7950f2", tram: "#fcc419", trolleybus: "#fd7e14", bus: "#339af0" };
const MODE_ICONS = { metro: "🚇", tram: "🚋", trolleybus: "🚎", bus: "🚌" };
const INK = "#2b2118";

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s).replace(/[&<>"']/g, (c) => `&#${c.charCodeAt(0)};`);
const mmss = (s) => {
  s = Math.max(0, Math.round(s));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
};
const pill = (mode, route) =>
  `<span class="pill ${mode}">${MODE_ICONS[mode] || ""}${esc(route)}</span>`;
const ROLE_LABEL = { dodger: "🏃 Dodger", hunter: "🤠 Bounty Hunter" };

let world = null;        // static data of the district being played
let district = -1;       // its index, or -1 before the first one is loaded
let loading = false;
let map = null;
let staticLayer = null;
let ws = null;
let follow = true;
let shownRound = null;   // round whose result overlay is showing
let burstTimer = null;
let match = null;
const keys = { up: false, down: false, left: false, right: false };
const last = { money: null, tickets: null };

const markers = {
  vehicles: new Map(), hunters: [], sight: [], player: null, dodger: null, ping: null,
};

function init() {
  map = L.map("map", { keyboard: false, zoomControl: true, preferCanvas: true })
    .setView([47.4979, 19.0402], 14);
  staticLayer = L.layerGroup().addTo(map);
  map.on("dragstart", () => setFollow(false));
  connect();
}

async function loadDistrict(index) {
  loading = true;
  try {
    const next = await (await fetch(`api/world/${index}`)).json();
    if (!world) {
      L.tileLayer(next.tiles.url, { maxZoom: 19, attribution: next.tiles.attribution })
        .addTo(map);
    }
    world = next;
    district = index;
    clearDynamic();
    staticLayer.clearLayers();
    drawArea(world.area);
    for (const line of world.lines) {
      L.polyline(line.path, {
        color: INK, weight: 8, opacity: 0.18, lineCap: "round", lineJoin: "round",
        interactive: false,
      }).addTo(staticLayer);
      L.polyline(line.path, {
        color: MODE_COLORS[line.mode] || "#888", weight: 5, opacity: 0.7, lineCap: "round",
        lineJoin: "round", interactive: false,
      }).addTo(staticLayer);
    }
    for (const st of world.stations) {
      L.circleMarker(st.pos, {
        radius: 5, color: INK, weight: 3, fillColor: "#fff", fillOpacity: 1,
      }).bindTooltip(esc(st.name), { direction: "top", offset: [0, -6] }).addTo(staticLayer);
    }
    map.setView(world.center, 16, { animate: false });
    setFollow(true);
  } finally {
    loading = false;
  }
}

function drawArea(area) {
  const style = { color: INK, weight: 3, dashArray: "10 8", fill: false, interactive: false };
  if (area.kind === "polygon") {
    // Dim everything outside the play area: a world-sized ring with the area as a hole.
    const outside = [[-89, -179], [-89, 179], [89, 179], [89, -179]];
    L.polygon([outside, area.vertices], {
      stroke: false, fillColor: INK, fillOpacity: 0.3, interactive: false,
    }).addTo(staticLayer);
    L.polygon(area.vertices, style).addTo(staticLayer);
  } else if (area.kind === "circle") {
    L.circle([area.center_lat, area.center_lon], { ...style, radius: area.radius_m })
      .addTo(staticLayer);
  } else if (area.kind === "bbox") {
    L.rectangle([[area.min_lat, area.min_lon], [area.max_lat, area.max_lon]], style)
      .addTo(staticLayer);
  }
}

function clearDynamic() {
  for (const m of markers.vehicles.values()) m.remove();
  markers.vehicles.clear();
  for (const m of [...markers.hunters, ...markers.sight]) m.remove();
  markers.hunters = [];
  markers.sight = [];
  for (const key of ["player", "dodger", "ping"]) {
    if (markers[key]) { markers[key].remove(); markers[key] = null; }
  }
  last.money = last.tickets = null;
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
  renderPing(s.ping);
  renderPlayer(s.player, s.role);
  renderScore(match);
  renderHud(s);
  if (follow) map.setView(s.player.pos, map.getZoom(), { animate: false });
  if (match.round_over && shownRound !== match.round) showResult(s, match);
  if (!match.round_over) { $("overlay").hidden = true; shownRound = null; }
}

function renderVehicles(list, myTrip) {
  const seen = new Set();
  for (const [trip, lat, lon, mode, route] of list) {
    seen.add(trip);
    let m = markers.vehicles.get(trip);
    if (!m) {
      m = divMarker([lat, lon], pill(mode, route), "vehicle", [60, 22]);
      markers.vehicles.set(trip, m);
    } else {
      m.setLatLng([lat, lon]);
    }
    const el = m.getElement();
    if (el) el.classList.toggle("mine", trip === myTrip);
    m.setZIndexOffset(trip === myTrip ? 900 : 0);
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
      markers.hunters[i] = divMarker(hunter.pos, `<div class="${cls}">🤠</div>`, "", [34, 34]);
      markers.hunters[i].setZIndexOffset(800);
      markers.sight[i] = L.circle(hunter.pos, {
        radius: world.sight_radius_m, color: "#fa5252", weight: 2, dashArray: "6 6",
        opacity: 0.6, fillOpacity: 0.08, interactive: false,
      }).addTo(map);
    }
    markers.hunters[i].setLatLng(hunter.pos);
    markers.hunters[i].getElement().firstChild.className = cls;
    markers.sight[i].setLatLng(hunter.pos);
    markers.sight[i].setStyle({
      opacity: hunter.riding ? 0 : 0.6, fillOpacity: hunter.riding ? 0 : 0.08,
    });
  });
}

function renderDodger(dodger) {
  if (!dodger) {
    if (markers.dodger) { markers.dodger.remove(); markers.dodger = null; }
    return;
  }
  const cls = `toon wanted${dodger.held ? " held" : ""}`;
  if (!markers.dodger) {
    markers.dodger = divMarker(dodger.pos, `<div class="${cls}">🏃</div>`, "", [34, 34]);
    markers.dodger.setZIndexOffset(950);
  }
  markers.dodger.setLatLng(dodger.pos);
  markers.dodger.getElement().firstChild.className = cls;
}

function renderPing(ping) {
  if (!ping) { if (markers.ping) { markers.ping.remove(); markers.ping = null; } return; }
  if (!markers.ping) {
    markers.ping = L.circle(ping.pos, {
      radius: 60, color: "#e03131", weight: 3, dashArray: "4 6", fillOpacity: 0.15,
      interactive: false,
    }).addTo(map);
  }
  const fade = Math.max(0.15, 1 - ping.age_s / world.ping_every_s);
  markers.ping.setLatLng(ping.pos);
  markers.ping.setStyle({ opacity: fade, fillOpacity: 0.15 * fade });
}

function renderPlayer(p, role) {
  const moving = keys.up || keys.down || keys.left || keys.right;
  const state = p.state === "walk" && !moving ? "idle" : p.state;
  let face = { held: "😬", wait: "🧍", idle: "🧍" }[state] || "🏃";
  if (role === "hunter") face = "🤠";
  const cls = `toon player ${role} ${state}`;
  if (!markers.player) {
    markers.player = divMarker(p.pos, `<div class="${cls}">${face}</div>`, "", [34, 34]);
    markers.player.setZIndexOffset(1000);
  }
  markers.player.setLatLng(p.pos);
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
  } else if (s.waiting) {
    status = `Waiting for ${esc(s.waiting.route)} → ${esc(s.waiting.headsign)} ` +
      `<span class="muted">(${mmss(s.waiting.in_s / world.time_scale)}, E to cancel)</span>` +
      (s.wallet && !s.wallet.tickets ? '<br><span class="danger">No ticket: risky!</span>' : "");
  } else {
    status = "On foot" + (s.stop ? "" : ' <span class="muted">· walk to a stop to ride</span>');
  }
  $("status").innerHTML = status;
  $("status").className = held ? "status held" : "status";

  if (!s.stop) { $("stop").innerHTML = ""; return; }
  const rows = s.stop.departures.map((d, i) =>
    `<div class="dep${d.waiting ? " waiting" : ""}"><span class="key">${i + 1}</span>` +
    pill(d.mode, d.route) +
    `<span class="headsign">${esc(d.headsign)}</span>` +
    `<span class="in">${d.in_s <= 0 ? "now" : mmss(d.in_s / world.time_scale)}</span></div>`);
  const buy = s.wallet
    ? `<span class="chip" id="buy">T · 🎫 ${world.ticket_price_ft} Ft</span>` : "";
  $("stop").innerHTML = `<h3><span>🚏 ${esc(s.stop.name)}</span>${buy}</h3>` +
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
  if (ev.code === "Enter" || ev.code === "NumpadEnter") { ev.preventDefault(); nextRound(); }
  else if (ev.code === "KeyE" || ev.code === "Space") {
    ev.preventDefault(); send({ type: "use" });
  }
  else if (/^Digit[1-6]$/.test(ev.code)) send({ type: "wait", index: Number(ev.code.slice(5)) - 1 });
  else if (ev.code === "KeyT") send({ type: "ticket" });
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

// The stop panel is redrawn every snapshot, so a click (down and up on one element) can miss.
$("stop").addEventListener("pointerdown", (ev) => {
  if (ev.target.closest("#buy")) send({ type: "ticket" });
});
$("again").addEventListener("click", () => {
  if ($("again").dataset.action === "restart") restart();
  else nextRound();
});
$("follow").addEventListener("click", () => setFollow(true));

init();
