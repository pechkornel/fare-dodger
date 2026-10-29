// Admin page: server status, games in progress, and the "Update now" button.
"use strict";

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s).replace(/[&<>"']/g, (c) => `&#${c.charCodeAt(0)};`);
const dur = (s) => {
  s = Math.max(0, Math.round(s));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  return h ? `${h} h ${m} min` : m ? `${m} min ${s % 60} s` : `${s} s`;
};

let token = "";
try { token = sessionStorage.getItem("fare-dodger-admin") || ""; } catch { /* */ }
let lastCommit = null;

async function api(path, method = "GET") {
  const res = await fetch(path, { method, headers: { Authorization: `Bearer ${token}` } });
  if (res.status === 401 || res.status === 404) {
    const body = await res.json().catch(() => ({}));
    throw Object.assign(new Error(body.detail || res.statusText), { status: res.status });
  }
  if (!res.ok) throw new Error(res.statusText);
  return res.json();
}

function signedIn(ok, message = "") {
  $("login").hidden = ok;
  for (const id of ["dash", "updates", "rooms-panel"]) $(id).hidden = !ok;
  $("login-error").textContent = message;
  $("login-error").hidden = !message;
}

function render(status) {
  const info = status.info;
  const stats = [
    ["Version", `${info.version}${info.commit ? ` (${info.commit})` : ""}`],
    ["Up for", dur(status.uptime_s)],
    ["Memory", `${Math.round(status.memory_mib)} MiB`],
    ["Players", status.players],
    ["Games", `${status.rooms.length} / ${status.limits.max_matches}`],
    ["Timetable", `${info.feed} · ${info.service_date}`],
    ["Districts", info.districts],
    ["Per address", `${status.limits.max_per_ip} games`],
  ];
  $("stats").innerHTML = stats
    .map(([k, v]) => `<div class="stat"><small>${esc(k)}</small><b>${esc(v)}</b></div>`).join("");

  const u = status.update;
  const labels = {
    never: "No update run yet.", running: "Updating…", done: "Last update finished.",
    failed: "Last update failed.", unknown: "Unknown",
  };
  let state = labels[u.state] || u.state;
  if (u.pending && u.state !== "running") state = "Update requested…";
  $("update-state").textContent = state;
  $("update-state").className = `state ${u.state}`;
  const notes = [];
  if (u.finished) notes.push(`Finished ${new Date(u.finished).toLocaleString()}`);
  if (u.before && u.after) notes.push(u.before === u.after ? `already at ${u.after}` : `${u.before} → ${u.after}`);
  if (u.updater_missing) {
    notes.push("Nobody picked the request up: is the updater installed on the server " +
      "(deploy/install-updater.sh)?");
  }
  $("update-note").textContent = notes.join(" · ");
  $("update-log").hidden = !u.log;
  $("update-log").textContent = u.log || "";
  $("update-now").disabled = u.pending || u.state === "running";

  $("rooms").innerHTML = status.rooms.map((r) => {
    const players = r.players.map((p) => `${esc(p.name)} (${p.team})`).join(", ");
    const state = r.state === "lobby" ? "lobby"
      : `round ${r.round}, ${r.score[0]}–${r.score[1]}, ${esc(r.district)}${r.over ? " (over)" : ""}`;
    return `<tr><td><b>${esc(r.code)}</b></td><td>${r.size}v${r.size}</td><td>${players}</td>` +
      `<td>${state}</td><td>${dur(r.idle_s)}</td>` +
      `<td><button class="small danger" data-close="${esc(r.code)}">Close</button></td></tr>`;
  }).join("");
  $("no-rooms").hidden = status.rooms.length > 0;

  if (lastCommit && info.commit && info.commit !== lastCommit) {
    $("update-note").textContent = `Now running ${info.commit}. ` + $("update-note").textContent;
  }
  lastCommit = info.commit || lastCommit;
}

async function refresh() {
  if (!token) { signedIn(false); return; }
  try {
    render(await api("api/admin/status"));
    signedIn(true);
  } catch (err) {
    if (err.status === 401 || err.status === 404) {
      token = "";
      signedIn(false, err.message);
    } else if (!$("dash").hidden) {
      // The server goes away while it restarts after an update.
      $("update-state").textContent = "Restarting the game…";
    }
  }
}

$("sign-in").addEventListener("click", () => {
  token = $("token").value.trim();
  try { sessionStorage.setItem("fare-dodger-admin", token); } catch { /* */ }
  refresh();
});
$("token").addEventListener("keydown", (ev) => { if (ev.key === "Enter") $("sign-in").click(); });

$("update-now").addEventListener("click", async () => {
  const players = Number(($("stats").textContent.match(/Players(\d+)/) || [])[1] || 0);
  const warning = players
    ? `${players} player(s) are connected and will be cut off. Update now?`
    : "Update the game now?";
  if (!confirm(warning)) return;
  try {
    render((await api("api/admin/update", "POST")) && (await api("api/admin/status")));
  } catch (err) { alert(err.message); }
});

$("rooms").addEventListener("click", async (ev) => {
  const code = ev.target.dataset && ev.target.dataset.close;
  if (!code || !confirm(`Close game ${code}? Its players are sent back to the menu.`)) return;
  try { await api(`api/admin/rooms/${encodeURIComponent(code)}/close`, "POST"); refresh(); }
  catch (err) { alert(err.message); }
});

refresh();
setInterval(refresh, 3000);
