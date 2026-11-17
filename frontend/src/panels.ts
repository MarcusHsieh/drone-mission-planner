// The flight-monitoring panels: battery, GPS, health, link, rangefinder and
// the message log, plus the status strip in the top bar. They show what a
// ground station like QGroundControl shows in flight, from the same telemetry
// WebSocket as the map. Setup (parameters, calibration, firmware) stays in QGC.

import { api, Level, LogMessage, Telemetry } from "./api";
import { Hud } from "./hud";

const $ = (id: string) => document.getElementById(id)!;
const esc = (s: string) => s.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]!);
const dash = "—";
const num = (v: number | null | undefined, digits = 1, unit = "") => (v === null || v === undefined || Number.isNaN(v) ? dash : `${v.toFixed(digits)}${unit}`);
const dot = (level: Level | "off") => `<span class="dot ${level}"></span>`;
const fmtDuration = (s: number) => {
  s = Math.max(0, Math.round(s));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
};

/** Telemetry older than this is shown as stale. */
const STALE_S = 2.5;
const MAX_LOG = 500;

/** Lithium-ion/LiPo per-cell levels for the battery colors (resting voltage under load is lower). */
const CELL_WARN_V = 3.6;
const CELL_BAD_V = 3.45;

let hud: Hud;
let lastFrame = 0;
let latest: Telemetry | null = null;
let estCells = 0;

export function setupPanels(): void {
  hud = new Hud($("hud") as HTMLCanvasElement);
  hud.draw();
  setupMessages();
  setInterval(checkStale, 1000);
}

export function updatePanels(t: Telemetry): void {
  lastFrame = performance.now();
  latest = t;
  document.body.classList.remove("telemetry-stale");
  hud.update(t);
  renderBattery(t);
  renderGps(t);
  renderHealth(t);
  renderLink(t);
  renderRangefinder(t);
  renderTopbar(t);
  if (t.messages) mergeMessages(t.messages);
}

function checkStale(): void {
  const age = lastFrame ? (performance.now() - lastFrame) / 1000 : Infinity;
  const stale = age > STALE_S;
  document.body.classList.toggle("telemetry-stale", stale);
  hud?.setStale(stale || latest?.connected === false);
  if (stale) renderLink(latest);
  if (stale && latest) renderTopbar(latest);
}

// ------------------------------------------------------------------ top bar
function renderTopbar(t: Telemetry): void {
  const stale = document.body.classList.contains("telemetry-stale");
  const b = t.battery;
  const pct = b?.remaining_pct ?? t.battery_pct;
  const battLevel: Level = pct > 40 ? "ok" : pct > 20 ? "warn" : "bad";
  const sats = t.gps?.satellites ?? t.satellites;
  const gpsLevel: Level = (t.gps?.fix_type ?? 3) >= 3 && sats >= 8 ? "ok" : (t.gps?.fix_type ?? 3) >= 2 ? "warn" : "bad";
  const linkLevel: Level = stale || t.connected === false ? "bad" : (t.link?.heartbeat_age_s ?? 0) > 2 ? "warn" : "ok";
  const health = worstLevel([t.ekf?.level, t.vibration?.level, t.prearm?.ok === false && !t.armed ? "warn" : undefined, (t.sensors ?? []).some((s) => s.enabled && !s.healthy) ? "bad" : undefined]);
  $("topbar-status").innerHTML = [
    `<span class="pill ${t.armed ? "armed" : "disarmed"}">${t.armed ? "ARMED" : "DISARMED"}</span>`,
    `<span class="pill mode">${esc(t.mode)}</span>`,
    `<span class="stat" title="Battery">${dot(battLevel)}${pct.toFixed(0)}% · ${(b?.voltage_v ?? t.voltage_v).toFixed(1)} V</span>`,
    `<span class="stat" title="GPS">${dot(gpsLevel)}${esc(t.gps?.fix ?? t.gps_fix)} · ${sats} sats</span>`,
    `<span class="stat" title="Arming checks, EKF, vibration and sensors">${dot(health)}Health</span>`,
    `<span class="stat" title="Telemetry link">${dot(linkLevel)}${stale ? "No telemetry" : t.connected === false ? "No autopilot" : "Link"}</span>`,
    t.source === "sim" ? `<span class="stat muted" title="The built-in simulator: health values are placeholders">sim</span>` : "",
  ].join("");
}

function worstLevel(levels: (Level | undefined)[]): Level {
  return levels.includes("bad") ? "bad" : levels.includes("warn") ? "warn" : "ok";
}

// ------------------------------------------------------------------ battery
function renderBattery(t: Telemetry): void {
  const b = t.battery;
  const v = b?.voltage_v ?? t.voltage_v;
  const pct = b?.remaining_pct ?? t.battery_pct;
  // Cell count from the first sensible full-ish reading (LiPo is 3.5 to 4.35 V per cell).
  if (v > 2 && (!estCells || (!t.armed && Math.abs(v / estCells - 3.9) > 0.5))) estCells = Math.max(1, Math.round(v / 3.95));
  const cellV = estCells ? v / estCells : null;
  const level: Level = cellV === null ? "ok" : cellV < CELL_BAD_V || pct < 20 ? "bad" : cellV < CELL_WARN_V || pct < 40 ? "warn" : "ok";
  const used = b?.consumed_mah ?? null;
  const cap = b?.capacity_mah ?? null;
  const current = b?.current_a ?? null;
  const watts = current !== null ? v * current : null;
  let left = b?.time_remaining_s ?? null;
  // Estimate time left from the current draw if the autopilot doesn't.
  if (left === null && t.armed && current && current > 1 && cap && used !== null) left = ((cap - used) / 1000 / current) * 3600;
  $("battery-panel").innerHTML = `
    <div class="big-row">
      <div class="big ${level}">${pct.toFixed(0)}<small>%</small></div>
      <div class="big-side">
        <div><b>${v.toFixed(2)} V</b>${cellV ? ` <span class="muted">≈ ${cellV.toFixed(2)} V/cell (${estCells}S)</span>` : ""}</div>
        <div>${num(current, 1, " A")}${watts !== null ? ` <span class="muted">· ${watts.toFixed(0)} W</span>` : ""}</div>
      </div>
    </div>
    <div class="bar ${level}"><div style="width:${Math.max(0, Math.min(100, pct))}%"></div></div>
    <div class="kv">
      <div><span>Used</span>${used === null ? dash : `${used.toLocaleString("en-US")} mAh`}${cap ? ` <span class="muted">of ${cap.toLocaleString("en-US")}</span>` : ""}</div>
      <div><span>Time left</span>${left === null ? dash : `≈ ${fmtDuration(left)}`}</div>
    </div>
    ${t.source === "sim" ? `<div class="note">Simulated pack (6S, placeholder capacity).</div>` : ""}`;
}

// ------------------------------------------------------------------ GPS
function renderGps(t: Telemetry): void {
  const g = t.gps;
  const fixType = g?.fix_type ?? 3;
  const sats = g?.satellites ?? t.satellites;
  const fixLevel: Level = fixType >= 3 ? "ok" : fixType === 2 ? "warn" : "bad";
  const satLevel: Level = sats >= 10 ? "ok" : sats >= 6 ? "warn" : "bad";
  const hdop = g?.hdop ?? null;
  const hdopLevel: Level = hdop === null ? "warn" : hdop <= 1.2 ? "ok" : hdop <= 2 ? "warn" : "bad";
  $("gps-panel").innerHTML = `
    <div class="tiles">
      <div class="tile">${dot(fixLevel)}<span>Fix</span><b>${esc(g?.fix ?? t.gps_fix)}</b></div>
      <div class="tile">${dot(satLevel)}<span>Satellites</span><b>${sats}</b></div>
      <div class="tile">${dot(hdopLevel)}<span>HDOP</span><b>${num(hdop, 2)}</b></div>
      <div class="tile"><span>VDOP</span><b>${num(g?.vdop, 2)}</b></div>
    </div>
    <div class="kv">
      <div><span>H accuracy</span>${num(g?.h_acc_m, 2, " m")}</div>
      <div><span>Alt MSL</span>${num(t.alt_msl_m, 1, " m")}</div>
      <div><span>From dock</span>${num(t.dock_distance_m, 0, " m")}${t.dock_distance_m && t.dock_distance_m > 2 ? ` <span class="muted">dock at ${(((t.dock_bearing_deg ?? 0) + 180) % 360).toFixed(0)}°</span>` : ""}</div>
      <div><span>Position</span><span class="mono">${t.lat.toFixed(6)}, ${t.lon.toFixed(6)}</span></div>
    </div>`;
}

// ------------------------------------------------------------------ health
const EKF_NAMES: Record<string, string> = { velocity: "Velocity", pos_horiz: "Pos horiz", pos_vert: "Pos vert", compass: "Compass", terrain: "Terrain" };

function varianceBar(name: string, v: number): string {
  const level: Level = v >= 0.8 ? "bad" : v >= 0.5 ? "warn" : "ok";
  return `<div class="meter-row"><span>${name}</span><div class="meter ${level}"><div style="width:${Math.min(100, v * 100)}%"></div><i style="left:50%"></i><i style="left:80%"></i></div><b>${v.toFixed(2)}</b></div>`;
}

function vibeBar(axis: string, v: number): string {
  const level: Level = v > 60 ? "bad" : v > 30 ? "warn" : "ok";
  return `<div class="meter-row"><span>${axis}</span><div class="meter ${level}"><div style="width:${Math.min(100, (v / 90) * 100)}%"></div><i style="left:33.3%"></i><i style="left:66.6%"></i></div><b>${v.toFixed(0)}</b></div>`;
}

function renderHealth(t: Telemetry): void {
  const pre = t.prearm;
  const preLevel: Level = pre?.ok === false ? (t.armed ? "ok" : "bad") : "ok";
  const preText = t.armed ? "Armed" : pre?.ok === null || pre === undefined ? "Not reported" : pre.ok ? "Ready to arm" : "Not ready to arm";
  const reasons = !t.armed && pre?.reasons?.length ? `<ul class="reasons">${pre.reasons.slice(-4).map((r) => `<li>${esc(r)}</li>`).join("")}</ul>` : "";
  const e = t.ekf;
  const ekf = e
    ? `<h4>${dot(e.level)}EKF ${e.level === "ok" ? "healthy" : e.missing.length ? `missing: ${e.missing.join(", ")}` : "variance high"}${e.gps_glitching ? " · GPS glitch" : ""}${e.const_pos_mode ? " · no position" : ""}</h4>
       ${Object.entries(e.variances).filter(([k]) => k !== "terrain" || e.variances[k] > 0).map(([k, v]) => varianceBar(EKF_NAMES[k] ?? k, v)).join("")}`
    : `<h4>${dot("off")}EKF not reported</h4>`;
  const vb = t.vibration;
  const clip = vb ? vb.clipping.reduce((a, b) => a + b, 0) : 0;
  const vibe = vb
    ? `<h4>${dot(vb.level)}Vibration (m/s²)${clip ? ` · <span class="bad-text">${clip} clipping</span>` : ""}</h4>${vibeBar("X", vb.x)}${vibeBar("Y", vb.y)}${vibeBar("Z", vb.z)}`
    : `<h4>${dot("off")}Vibration not reported</h4>`;
  const sensors = (t.sensors ?? [])
    .map((s) => `<span class="sensor ${!s.enabled ? "off" : s.healthy ? "ok" : "bad"}" title="${s.enabled ? (s.healthy ? "Healthy" : "Unhealthy") : "Not enabled"}">${esc(s.name)}</span>`)
    .join("");
  $("health-panel").innerHTML = `
    <h4>${dot(preLevel)}${preText} <span class="muted">· ${esc(t.system_status ?? dash)}</span></h4>${reasons}
    ${ekf}
    ${vibe}
    <h4>Sensors</h4><div class="sensors">${sensors || `<span class="muted">Not reported</span>`}</div>
    ${t.source === "sim" ? `<div class="note">The built-in sim has no health model; these are placeholders. Run SITL for real values.</div>` : ""}`;
}

// ------------------------------------------------------------------ link
function renderLink(t: Telemetry | null): void {
  const age = lastFrame ? (performance.now() - lastFrame) / 1000 : null;
  const wsLevel: Level = age === null || age > STALE_S ? "bad" : "ok";
  const l = t?.link;
  const hb = l?.heartbeat_age_s ?? null;
  const hbLevel: Level = !l?.connected ? "bad" : hb === null || hb > 5 ? "bad" : hb > 2 ? "warn" : "ok";
  const loss = l?.packet_loss_pct ?? null;
  const lossLevel: Level = loss === null ? "ok" : loss > 10 ? "bad" : loss > 2 ? "warn" : "ok";
  const rssi = l?.rc_rssi_pct ?? null;
  $("link-panel").innerHTML = `
    <div class="kv one">
      <div>${dot(wsLevel)}<span>Planner backend</span>${wsLevel === "ok" ? "streaming" : age === null ? "connecting…" : `no data for ${age.toFixed(0)} s`}</div>
      <div>${dot(hbLevel)}<span>Autopilot</span>${!l ? dash : l.connected ? `heartbeat ${num(hb, 1, " s")} ago` : "not connected"}</div>
      <div><span>Endpoint</span><span class="mono">${esc(l?.endpoint ?? dash)}</span></div>
      <div>${dot(lossLevel)}<span>Packet loss</span>${num(loss, 1, " %")} <span class="muted">(planner side)</span></div>
      <div><span>Vehicle drops</span>${num(l?.vehicle_drop_pct, 1, " %")}</div>
      <div><span>RC signal</span>${rssi === null ? `${dash} <span class="muted">(no RSSI reported)</span>` : `${rssi}%`}</div>
    </div>
    <div class="note">On the airframe, RC is ExpressLRS. Set RSSI_TYPE so ArduPilot reports link quality here; telemetry to the planner needs its own path (ELRS MAVLink mode or a telemetry radio).</div>`;
}

// ------------------------------------------------------------------ rangefinder
function renderRangefinder(t: Telemetry): void {
  const r = t.rangefinder;
  if (!r) {
    $("rangefinder-panel").innerHTML = `<div class="muted">No downward rangefinder reported. On the airframe this is the TFmini-S (RNGFND1_TYPE 20).</div>`;
    return;
  }
  const below = r.distance_m < r.min_m;
  const status = r.in_range ? "In range" : below ? "Below minimum (on the ground?)" : `No return (beyond ${r.max_m} m)`;
  const level: Level = r.in_range || below ? "ok" : "warn";
  const pct = Math.min(100, (r.distance_m / r.max_m) * 100);
  const diff = r.in_range ? t.alt_m - r.distance_m : null;
  $("rangefinder-panel").innerHTML = `
    <div class="big-row">
      <div class="big ${level}">${r.in_range ? r.distance_m.toFixed(2) : dash}<small>m</small></div>
      <div class="big-side"><div>${dot(level)}${status}</div><div class="muted">Range ${r.min_m}–${r.max_m} m</div></div>
    </div>
    <div class="bar ${level}"><div style="width:${r.in_range ? pct : below ? 0 : 100}%"></div></div>
    <div class="kv">
      <div><span>Altitude</span>${t.alt_m.toFixed(1)} m <span class="muted">(above dock)</span></div>
      <div><span>Alt − lidar</span>${diff === null ? dash : `${diff >= 0 ? "+" : ""}${diff.toFixed(1)} m`}</div>
    </div>
    ${t.alt_m > r.max_m ? `<div class="note">Survey altitude is above the lidar's ${r.max_m} m range, so it only reads during takeoff and landing.</div>` : ""}`;
}

// ------------------------------------------------------------------ messages
let log: LogMessage[] = [];
let lastId = 0;
let hiddenUpTo = 0;

function setupMessages(): void {
  $("msg-only-warn").addEventListener("change", renderMessages);
  $("btn-msg-clear").addEventListener("click", () => {
    hiddenUpTo = lastId;
    renderMessages();
  });
  reloadMessages();
}

async function reloadMessages(): Promise<void> {
  try {
    const r = await api.messages();
    log = r.messages.slice(-MAX_LOG);
    lastId = log.length ? log[log.length - 1].id : 0;
    hiddenUpTo = Math.min(hiddenUpTo, lastId);
    renderMessages();
  } catch {
    /* backend down; the WebSocket tail fills in when it's back */
  }
}

function mergeMessages(tail: LogMessage[]): void {
  if (!tail.length) return;
  const newest = tail[tail.length - 1].id;
  if (newest < lastId) {
    // The backend restarted and its ids started over.
    lastId = 0;
    hiddenUpTo = 0;
    reloadMessages();
    return;
  }
  const fresh = tail.filter((m) => m.id > lastId);
  if (!fresh.length) return;
  // A gap means we missed some (tab asleep, reconnect): fetch the whole log.
  if (lastId && fresh[0].id > lastId + 1) {
    reloadMessages();
    return;
  }
  log.push(...fresh);
  if (log.length > MAX_LOG) log = log.slice(-MAX_LOG);
  lastId = newest;
  renderMessages();
}

function renderMessages(): void {
  const box = $("messages-log");
  const onlyWarn = ($("msg-only-warn") as HTMLInputElement).checked;
  const shown = log.filter((m) => m.id > hiddenUpTo && (!onlyWarn || m.severity <= 4));
  const atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 30;
  box.innerHTML = shown.length
    ? shown
        .map((m) => {
          const time = new Date(m.t * 1000).toLocaleTimeString([], { hour12: false });
          const cls = m.severity <= 3 ? "bad" : m.severity === 4 ? "warn" : m.severity === 5 ? "notice" : "info";
          return `<div class="logline ${cls}"><span class="time">${time}</span><span class="src ${m.source}">${m.source === "vehicle" ? "AP" : "Planner"}</span><span class="text">${esc(m.text)}</span></div>`;
        })
        .join("")
    : `<div class="muted empty">No messages${onlyWarn ? " at warning level or worse" : ""} yet.</div>`;
  if (atBottom) box.scrollTop = box.scrollHeight;
}
