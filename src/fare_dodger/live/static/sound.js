// Game sounds, made with WebAudio (no sound files): a heartbeat when a Bounty Hunter is
// close, a whoosh when you board, alarms for inspections and catches. M mutes, remembered.

let ctx = null;
let master = null;
let muted = false;
try { muted = localStorage.getItem("fare-dodger-muted") === "1"; } catch { /* */ }

// Browsers only start audio after a click or a key press.
export function unlock() {
  if (!ctx) {
    const Ctx = window.AudioContext || window.webkitAudioContext;
    if (!Ctx) return;
    ctx = new Ctx();
    master = ctx.createGain();
    master.gain.value = muted ? 0 : 0.5;
    master.connect(ctx.destination);
  }
  if (ctx.state === "suspended") ctx.resume();
}

export function toggleMute() {
  muted = !muted;
  try { localStorage.setItem("fare-dodger-muted", muted ? "1" : "0"); } catch { /* */ }
  if (master) master.gain.value = muted ? 0 : 0.5;
  return muted;
}

const ready = () => ctx && !muted && ctx.state === "running";

// One note: a wave sliding from f0 to f1 Hz, with a quick attack and a decay.
function tone(f0, f1, dur, { type = "sine", gain = 0.4, at = 0 } = {}) {
  const t = ctx.currentTime + at;
  const osc = ctx.createOscillator();
  const env = ctx.createGain();
  osc.type = type;
  osc.frequency.setValueAtTime(f0, t);
  osc.frequency.exponentialRampToValueAtTime(Math.max(f1, 1), t + dur);
  env.gain.setValueAtTime(0.0001, t);
  env.gain.exponentialRampToValueAtTime(gain, t + 0.01);
  env.gain.exponentialRampToValueAtTime(0.0001, t + dur);
  osc.connect(env).connect(master);
  osc.start(t);
  osc.stop(t + dur + 0.02);
}

// Filtered noise sweeping from f0 to f1 Hz: wind, doors, a vehicle pulling away.
function noise(f0, f1, dur, { gain = 0.3, at = 0 } = {}) {
  const t = ctx.currentTime + at;
  const buffer = ctx.createBuffer(1, Math.ceil(ctx.sampleRate * dur), ctx.sampleRate);
  const data = buffer.getChannelData(0);
  for (let i = 0; i < data.length; i++) data[i] = Math.random() * 2 - 1;
  const src = ctx.createBufferSource();
  src.buffer = buffer;
  const filter = ctx.createBiquadFilter();
  filter.type = "bandpass";
  filter.Q.value = 1.2;
  filter.frequency.setValueAtTime(f0, t);
  filter.frequency.exponentialRampToValueAtTime(f1, t + dur);
  const env = ctx.createGain();
  env.gain.setValueAtTime(0.0001, t);
  env.gain.exponentialRampToValueAtTime(gain, t + dur * 0.3);
  env.gain.exponentialRampToValueAtTime(0.0001, t + dur);
  src.connect(filter).connect(env).connect(master);
  src.start(t);
}

const SOUNDS = {
  heartbeat: () => { tone(70, 40, 0.18, { gain: 0.7 }); tone(65, 38, 0.16, { gain: 0.5, at: 0.22 }); },
  board: () => { tone(880, 880, 0.25, { gain: 0.15 }); tone(660, 660, 0.4, { gain: 0.15, at: 0.25 });
    noise(300, 2500, 0.9, { gain: 0.2, at: 0.3 }); },
  alight: () => noise(2000, 300, 0.5, { gain: 0.15 }),
  inspection: () => { for (let i = 0; i < 3; i++) {
    tone(950, 950, 0.18, { type: "square", gain: 0.12, at: i * 0.4 });
    tone(700, 700, 0.18, { type: "square", gain: 0.12, at: i * 0.4 + 0.2 });
  } },
  ok: () => { tone(660, 660, 0.12, { gain: 0.2 }); tone(990, 990, 0.25, { gain: 0.2, at: 0.12 }); },
  caught: () => { tone(220, 55, 0.9, { type: "sawtooth", gain: 0.35 }); noise(800, 80, 0.8, { gain: 0.4 }); },
  ping: () => tone(1400, 1100, 0.3, { gain: 0.12 }),
  coffee: () => { tone(520, 1040, 0.3, { type: "triangle", gain: 0.2 }); },
  money: () => { tone(1320, 1320, 0.08, { gain: 0.15 }); tone(1760, 1760, 0.2, { gain: 0.15, at: 0.08 }); },
  escape: () => [523, 659, 784, 1047].forEach((f, i) => tone(f, f, 0.3, { type: "triangle", gain: 0.2, at: i * 0.12 })),
  sprint: () => noise(400, 900, 0.25, { gain: 0.08 }),
};

export function play(name) {
  if (ready() && SOUNDS[name]) SOUNDS[name]();
}
