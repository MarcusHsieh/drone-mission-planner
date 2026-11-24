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
let placing = false;

function setPlacing(on: boolean) {
  placing = on;
  $("#btn-place").classList.toggle("active", on);
  $("#btn-place").textContent = on ? "Placing… (click to stop)" : "Place on map";
  if (map) map.getCanvas().style.cursor = on ? "crosshair" : "";
}
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
  $("#btn-place").addEventListener("click", () => setPlacing(!placing));
  $("#btn-scatter").addEventListener("click", () => act(() => api.placed({ op: "scatter", count: 10, holes: [1] })));
  $("#btn-placed-clear").addEventListener("click", () => act(() => api.placed({ op: "clear" })));
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
  setTimeout(poll, q?.running || q?.field.scanning ? 500 : 2000);
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
  m.addSource("placed", { type: "geojson", data: { type: "FeatureCollection", features: [] } });
  m.addLayer({
    id: "placed", type: "circle", source: "placed",
    paint: {
      "circle-radius": ["interpolate", ["linear"], ["zoom"], 15, 5, 19, 11],
      "circle-color": "rgba(0,0,0,0)",
      "circle-stroke-color": ["get", "color"],
      "circle-stroke-width": 1.5,
      "circle-stroke-opacity": 0.9,
    },
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
  m.on("mouseleave", "divots", () => (m.getCanvas().style.cursor = placing ? "crosshair" : ""));
  if (q) drawMap(q);
}

/** True (and a popup shown) if the click hit a divot, so the map shouldn't toggle a cell. */
export function clickDivot(e: maplibregl.MapMouseEvent): boolean {
  if (!map || !map.getLayer("divots")) return false;
  if (placing) {
    const hit = map.queryRenderedFeatures(e.point, { layers: ["placed"] })[0];
    if (hit) act(() => api.placed({ op: "remove", ids: [String(hit.properties?.id)] }));
    else act(() => api.placed({ op: "add", points: [[e.lngLat.lng, e.lngLat.lat]] }));
    return true;
  }
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

let lastDock = "";
function render(s: QueueState) {
  q = s;
  window.dispatchEvent(new Event("queue-state"));
  // The dock charges on its own, without bumping the queue's version: redraw the panel for it.
  const dock = JSON.stringify([s.charge, s.field.scanning, s.field.waiting_for_charge, s.message]);
  if (s.version === lastVersion) {
    if (dock !== lastDock) {
      lastDock = dock;
      drawPanel(s);
    }
    return updateButtons();
  }
  lastVersion = s.version;
  lastDock = dock;
  drawMap(s);
  drawPanel(s);
  updateButtons();
}

/** One divot for the 3D view: tasks by status color, placed ones white until found. */
export interface DivotPoint {
  lon: number;
  lat: number;
  color: string;
  kind: "task" | "placed";
}
let divotsFn: (list: DivotPoint[]) => void = () => {};
/** Get every divot (tasks and placed) whenever they change, for the 3D view. */
export function onDivots(fn: (list: DivotPoint[]) => void): void {
  divotsFn = fn;
  if (q) fn(divotPoints(q));
}

function divotPoints(s: QueueState): DivotPoint[] {
  return [
    ...s.field.placed.filter((d) => !d.found_task).map((d) => ({ lon: d.lon, lat: d.lat, color: "#f8fafc", kind: "placed" as const })),
    ...s.tasks.map((t) => ({ lon: t.lon, lat: t.lat, color: STATUS_COLOR[t.status], kind: "task" as const })),
  ];
}

function drawMap(s: QueueState) {
  divotsFn(divotPoints(s));
  if (!map?.getSource("divots")) return;
  (map.getSource("divots") as GeoJSONSource).setData({
    type: "FeatureCollection",
    features: s.tasks.map((t) => ({
      type: "Feature",
      properties: { id: t.id, status: t.status, color: STATUS_COLOR[t.status] },
      geometry: { type: "Point", coordinates: [t.lon, t.lat] },
    })),
  });
  (map.getSource("placed") as GeoJSONSource).setData({
    type: "FeatureCollection",
    features: s.field.placed.map((d) => ({
      type: "Feature",
      // White: hidden, not found yet. Green: found.
      properties: { id: d.id, color: d.found_task ? "#4ade80" : "#f8fafc" },
      geometry: { type: "Point", coordinates: [d.lon, d.lat] },
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
    ${dockLine(s.charge)}
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

  const f = s.field;
  const ls = f.last_scan;
  $("#field-status").innerHTML = f.scanning
    ? f.waiting_for_charge ? `<b>Survey waiting for charge…</b>` : `<b>Surveying…</b> ${f.found} found so far.`
    : f.placed.length || f.scans
      ? `${f.placed.length} placed (hidden from the planner, white rings) · ${f.found} found by surveys${ls ? `. Last survey: ${ls.found} found, ${ls.not_covered} never under the camera.` : "."}${f.mean_error_m != null ? ` Found positions are ${f.mean_error_m} m off on average.` : ""}`
      : `Place divots on the map, preview a path over them (or ask the agent to plan a scan), then Fly survey. The camera sees a ${f.camera.footprint_m} m strip and finds every divot that passes under it.`;
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

/** The dock charges on its own whenever the drone sits on it. */
function dockLine(ch: QueueState["charge"]): string {
  if (!ch || ch.pct == null || ch.state === "idle") return "";
  const label = ch.state === "full" ? "On the dock, full"
    : ch.state === "settling" ? `On the dock at ${ch.pct.toFixed(0)}%, charging starts in a moment`
    : `Charging on the dock ${ch.pct.toFixed(0)}%${ch.left_s != null ? ` · ${fmtMin(ch.left_s)} to full (real time)` : ""}`;
  return `<div class="progress" title="Simulated: the dock charges whenever the drone sits on it"><div style="width:${ch.pct}%;background:#16a34a"></div><span>${label}</span></div>`;
}

function updateButtons() {
  const running = !!q?.running;
  const pending = (q?.counts.pending ?? 0) > 0;
  $<HTMLButtonElement>("#btn-queue-run").disabled = running || !pending;
  $<HTMLButtonElement>("#btn-queue-abort").disabled = !running;
  $<HTMLButtonElement>("#btn-queue-clear").disabled = running || !q?.tasks.length;
  const scanning = !!q?.field.scanning;
  $<HTMLButtonElement>("#btn-divots").disabled = running || scanning;
  $<HTMLButtonElement>("#btn-scatter").disabled = scanning;
  $<HTMLButtonElement>("#btn-placed-clear").disabled = scanning || !q?.field.placed.length;
  if (scanning) $<HTMLButtonElement>("#btn-queue-run").disabled = true;
}

export const queueRunning = () => !!q?.running || !!q?.field.scanning;
/** A survey is waiting on the dock for charge (abortable before takeoff). */
export const surveyWaiting = () => !!q?.field.waiting_for_charge;
export const refreshTaskButtons = () => updateButtons();

function boundsOf(pts: [number, number][]): [number, number, number, number] {
  const xs = pts.map((p) => p[0]), ys = pts.map((p) => p[1]);
  return [Math.min(...xs), Math.min(...ys), Math.max(...xs), Math.max(...ys)];
}
