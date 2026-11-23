// The Tasks panel and its map layers: fake divots as a task queue, the legs
// the planner split them into (one battery each, charge stops between), and
// each task's status as the drone works through them.
//
// The backend owns the queue and the loop (backend/app/task_queue.py); this
// file only draws GET /api/queue, polling faster while the queue runs.

import maplibregl, { GeoJSONSource, Map as MLMap } from "maplibre-gl";
import { api, QueueLeg, QueueState, QueueTask, TaskStatus } from "./api";

const $ = <T extends HTMLElement = HTMLElement>(sel: string) => document.querySelector(sel) as T;
const esc = (s: string) => s.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]!);
const fmtInt = (n: number) => Math.round(n).toLocaleString("en-US");
const fmtMin = (s: number) => (s >= 3600 ? `${Math.floor(s / 3600)} h ${Math.round((s % 3600) / 60)} min` : `${(s / 60).toFixed(1)} min`);

export const STATUS_COLOR: Record<TaskStatus, string> = {
  pending: "#facc15",
  active: "#22d3ee",
  done: "#4ade80",
  failed: "#f87171",
};
// Leg colors, distinct from the selection palette's first entries.
const LEG_COLORS = ["#f472b6", "#a78bfa", "#fb923c", "#60a5fa", "#34d399", "#e879f9", "#fbbf24", "#94a3b8"];
const legColor = (i: number) => LEG_COLORS[(i - 1) % LEG_COLORS.length];

const LEG_STATUS: Record<QueueLeg["status"], string> = {
  planned: "Planned",
  flying: "Flying",
  done: "Done",
  cut_short: "Cut short",
  aborted: "Aborted",
  failed: "Failed",
};

let map: MLMap | null = null;
let q: QueueState | null = null;
let lastVersion = -1;
let focusFn: (b: [number, number, number, number]) => void = () => {};
let toastFn: (m: string) => void = () => {};

export function setupTasks(focus: (b: [number, number, number, number]) => void, toast: (m: string) => void): void {
  focusFn = focus;
  toastFn = toast;
  $("#btn-divots").addEventListener("click", async () => {
    const count = Number($<HTMLInputElement>("#divot-count").value) || 20;
    const raw = $<HTMLInputElement>("#divot-holes").value.trim();
    const holes = raw ? raw.split(/[\s,]+/).map(Number).filter((n) => Number.isInteger(n) && n > 0) : null;
    await act(() => api.divots(count, holes && holes.length ? holes : null, null), true);
  });
  $("#btn-queue-run").addEventListener("click", () => act(api.queueRun));
  $("#btn-queue-abort").addEventListener("click", () => act(api.queueAbort));
  $("#btn-queue-clear").addEventListener("click", () => act(api.queueClear));
  poll();
}

async function act(fn: () => Promise<QueueState>, zoom = false) {
  try {
    render(await fn());
    if (zoom && q?.tasks.length) focusFn(boundsOf(q.tasks.map((t) => [t.lon, t.lat])));
  } catch (e) {
    toastFn((e as Error).message);
  }
}

async function poll() {
  try {
    render(await api.queue());
  } catch {
    /* backend restarting: try again */
  }
  setTimeout(poll, q?.running ? 500 : 2000);
}

/** Call once the map style has loaded. */
export function addTaskLayers(m: MLMap): void {
  map = m;
  m.addSource("legs", { type: "geojson", data: { type: "FeatureCollection", features: [] } });
  m.addLayer({
    id: "legs-planned", type: "line", source: "legs", filter: ["==", ["get", "planned"], true],
    layout: { "line-join": "round", "line-cap": "round" },
    paint: { "line-color": ["get", "color"], "line-width": 2, "line-dasharray": [2, 2], "line-opacity": 0.9 },
  });
  m.addLayer({
    id: "legs-flown", type: "line", source: "legs", filter: ["==", ["get", "planned"], false],
    layout: { "line-join": "round", "line-cap": "round" },
    paint: { "line-color": ["get", "color"], "line-width": 1.5, "line-opacity": 0.45 },
  });
  m.addSource("divots", { type: "geojson", data: { type: "FeatureCollection", features: [] } });
  m.addLayer({
    id: "divots", type: "circle", source: "divots",
    paint: {
      "circle-radius": ["interpolate", ["linear"], ["zoom"], 15, 4, 19, 8],
      "circle-color": ["get", "color"],
      "circle-stroke-color": "#0f172a",
      "circle-stroke-width": ["case", ["==", ["get", "status"], "active"], 3, 1.5],
    },
  });
  m.on("mouseenter", "divots", () => (m.getCanvas().style.cursor = "pointer"));
  m.on("mouseleave", "divots", () => (m.getCanvas().style.cursor = ""));
  if (q) drawMap(q);
}

/** True (and a popup shown) if the click hit a divot, so the map shouldn't toggle a cell. */
export function clickDivot(e: maplibregl.MapMouseEvent): boolean {
  if (!map || !map.getLayer("divots")) return false;
  const f = map.queryRenderedFeatures(e.point, { layers: ["divots"] })[0];
  if (!f) return false;
  const t = q?.tasks.find((x) => x.id === f.properties?.id);
  if (t) showTask(t);
  return true;
}

function showTask(t: QueueTask) {
  if (!map) return;
  new maplibregl.Popup({ offset: 10, maxWidth: "260px" })
    .setLngLat([t.lon, t.lat])
    .setHTML(
      `<b>${esc(t.id)}</b> · divot${t.hole ? ` on hole ${t.hole}` : ""}
       <div><span class="task-status ${t.status}">${t.status}</span>${t.leg ? ` · leg ${t.leg}` : ""}${t.attempts ? ` · ${t.attempts} ${t.attempts === 1 ? "try" : "tries"}` : ""}</div>
       <div class="muted">${t.x.toFixed(1)} m E, ${t.y.toFixed(1)} m N of the dock · ${t.service_s} s on station</div>
       ${t.note ? `<div class="muted">${esc(t.note)}</div>` : ""}`,
    )
    .addTo(map);
}

function render(s: QueueState) {
  q = s;
  if (s.version === lastVersion) return updateButtons();
  lastVersion = s.version;
  drawMap(s);
  drawPanel(s);
  updateButtons();
}

function drawMap(s: QueueState) {
  if (!map?.getSource("divots")) return;
  (map.getSource("divots") as GeoJSONSource).setData({
    type: "FeatureCollection",
    features: s.tasks.map((t) => ({
      type: "Feature",
      properties: { id: t.id, status: t.status, color: STATUS_COLOR[t.status] },
      geometry: { type: "Point", coordinates: [t.lon, t.lat] },
    })),
  });
  (map.getSource("legs") as GeoJSONSource).setData({
    type: "FeatureCollection",
    features: s.legs.map((l) => ({
      type: "Feature",
      properties: { index: l.index, color: legColor(l.index), planned: l.status === "planned" || l.status === "flying" },
      geometry: { type: "LineString", coordinates: l.path },
    })),
  });
}

function drawPanel(s: QueueState) {
  const c = s.counts;
  const n = s.tasks.length;
  const phase = s.running ? s.phase : n ? s.phase : "idle";
  $("#queue-status").innerHTML = `
    <div class="drone-status"><span class="mode queue-${phase}">${phase.toUpperCase()}</span> ${esc(s.message)}</div>
    ${s.charge ? `<div class="progress" title="Simulated charge"><div style="width:${s.charge.pct}%;background:#16a34a"></div><span>Charging ${s.charge.pct.toFixed(0)}% · ${fmtMin(s.charge.left_s)} left (real time)</span></div>` : ""}
    ${n ? `<div class="task-counts">${(["pending", "active", "done", "failed"] as TaskStatus[]).map((k) => `<span class="task-status ${k}">${c[k]} ${k}</span>`).join("")}</div>` : ""}
    ${s.remaining.legs ? `<div class="muted small">Left to fly: ${s.remaining.legs} leg${s.remaining.legs === 1 ? "" : "s"}, ${s.remaining.charge_stops} charge stop${s.remaining.charge_stops === 1 ? "" : "s"}, about ${fmtMin(s.remaining.time_s)} including charging, ${fmtInt(s.remaining.mah)} mAh.</div>` : ""}`;

  $("#queue-legs").innerHTML = s.legs.length
    ? `<table class="legs">
        <thead><tr><th>Leg</th><th>Tasks</th><th>km</th><th>min</th><th title="Predicted energy (mAh), % of the pack">mAh</th><th title="Predicted battery left on landing">Lands</th><th title="Minutes on the dock charging afterwards (real time)">Charge</th><th>Status</th><th title="Measured by the vehicle: mAh, and times the prediction">Actual</th></tr></thead>
        <tbody>${s.legs.map(legRow).join("")}</tbody>
      </table>`
    : n
      ? ""
      : `<div class="empty">No tasks. Generate divots to fill the queue: the planner splits them into legs that each fit on one battery, with a charge stop on the dock between legs.</div>`;
  $("#queue-legs").querySelectorAll<HTMLElement>("[data-leg]").forEach((el) =>
    el.addEventListener("click", () => {
      const l = q?.legs.find((x) => x.index === Number(el.dataset.leg));
      if (l) focusFn(boundsOf(l.path));
    }),
  );

  $("#queue-tasks").innerHTML = n
    ? `<div class="task-chips">${s.tasks.map((t) => `<button class="task-chip ${t.status}" data-task="${esc(t.id)}" title="${esc(`${t.id}: ${t.status}${t.hole ? `, hole ${t.hole}` : ""}${t.leg ? `, leg ${t.leg}` : ""}${t.note ? `. ${t.note}` : ""}`)}">${esc(t.id)}</button>`).join("")}</div>`
    : "";
  $("#queue-tasks").querySelectorAll<HTMLElement>("[data-task]").forEach((el) =>
    el.addEventListener("click", () => {
      const t = q?.tasks.find((x) => x.id === el.dataset.task);
      if (!t) return;
      map?.easeTo({ center: [t.lon, t.lat], zoom: Math.max(map.getZoom(), 18.5) });
      showTask(t);
    }),
  );

  const e = s.energy;
  $("#queue-model").innerHTML = `<b>Energy model${e.calibrated ? "" : " (uncalibrated placeholders)"}:</b> ${e.cells}S ${fmtInt(e.pack_mah)} mAh, land with ≥ ${e.reserve_pct}% left
    (${fmtInt(e.usable_mah)} mAh per leg), ${e.hover_a} A hover, ${e.cruise_a} A cruise, ×${e.margin} margin: about ${e.hover_minutes_to_reserve} min of hover per leg.
    Tasks: descend to ${s.service.alt_m} m, hold, climb back. Charging at ${e.charge_c}C, simulated ${e.charge_speedup}× faster.
    A leg returns early if the vehicle's battery drops under ${s.service.guard_pct}%. Not real flight times: calibrate with logged flights first.`;
}

function legRow(l: QueueLeg): string {
  const a = l.actual;
  const done = a.tasks_done ?? (q ? l.task_ids.filter((id) => q!.tasks.find((t) => t.id === id)?.status === "done").length : 0);
  const actual = a.mah != null
    ? `${fmtInt(a.mah)}${a.mah_vs_plan ? ` <span class="muted">${a.mah_vs_plan}×</span>` : ""}`
    : l.status === "flying" ? "…" : "";
  return `<tr data-leg="${l.index}" class="leg-${l.status}" title="Leg ${l.index}: ${l.task_ids.join(", ")}. Click to zoom.">
    <td><span class="swatch" style="background:${legColor(l.index)}"></span>${l.index}</td>
    <td>${l.status === "planned" ? "" : `${done}/`}${l.task_ids.length}</td>
    <td>${(l.distance_m / 1000).toFixed(1)}</td>
    <td>${(l.time_s / 60).toFixed(1)}</td>
    <td>${fmtInt(l.mah)} <span class="muted">${Math.round(l.pct_of_pack)}%</span></td>
    <td>${l.end_pct.toFixed(0)}%</td>
    <td>${l.charge_after_s ? Math.round(l.charge_after_s / 60) : "—"}</td>
    <td>${LEG_STATUS[l.status]}</td>
    <td>${actual}</td>
  </tr>`;
}

function updateButtons() {
  const running = !!q?.running;
  const pending = (q?.counts.pending ?? 0) > 0;
  $<HTMLButtonElement>("#btn-queue-run").disabled = running || !pending;
  $<HTMLButtonElement>("#btn-queue-abort").disabled = !running;
  $<HTMLButtonElement>("#btn-queue-clear").disabled = running || !q?.tasks.length;
  $<HTMLButtonElement>("#btn-divots").disabled = running;
}

export const queueRunning = () => !!q?.running;

function boundsOf(pts: [number, number][]): [number, number, number, number] {
  const xs = pts.map((p) => p[0]), ys = pts.map((p) => p[1]);
  return [Math.min(...xs), Math.min(...ys), Math.max(...xs), Math.max(...ys)];
}
