import maplibregl, { GeoJSONSource, LngLatBoundsLike, Map as MLMap } from "maplibre-gl";
import "maplibre-gl/dist/maplibre-gl.css";
import "./style.css";
import { api, AgentReply, CourseInfo, Group, MissionState, Telemetry } from "./api";
import { LocalFrame } from "./geo";

// Distinct, readable on satellite imagery.
const PALETTE = ["#22d3ee", "#f472b6", "#facc15", "#a78bfa", "#fb923c", "#4ade80", "#60a5fa", "#f87171"];
const groupColor = (id: number) => PALETTE[(id - 1) % PALETTE.length];

const SUGGESTIONS = [
  "Survey the fairway on hole 4",
  "Add hole 5's fairway. What's the total area?",
  "Clear the selection",
];

const $ = <T extends HTMLElement = HTMLElement>(sel: string) => document.querySelector(sel) as T;
const fmtInt = (n: number) => Math.round(n).toLocaleString("en-US");
const fmtTime = (s: number) => {
  s = Math.round(s);
  const m = Math.floor(s / 60);
  return m ? `${m} min ${s % 60} s` : `${s} s`;
};
const esc = (s: string) => s.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]!);

let course: CourseInfo;
let frame: LocalFrame;
let map: MLMap;
let state: MissionState | null = null;
let flying = false;

// ---------------------------------------------------------------- map setup
function emptyFC(): GeoJSON.FeatureCollection {
  return { type: "FeatureCollection", features: [] };
}

function buildMap() {
  map = new maplibregl.Map({
    container: "map",
    center: [course.dock.lon, course.dock.lat],
    zoom: 16.3,
    maxZoom: 21,
    boxZoom: false,
    doubleClickZoom: false,
    style: {
      version: 8,
      sources: {
        satellite: {
          type: "raster",
          tiles: ["https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}"],
          tileSize: 256,
          maxzoom: 19,
          attribution: "Imagery © Esri, Maxar, Earthstar Geographics",
        },
        osm: {
          type: "raster",
          tiles: ["https://tile.openstreetmap.org/{z}/{x}/{y}.png"],
          tileSize: 256,
          maxzoom: 19,
          attribution: "© OpenStreetMap contributors",
        },
      },
      layers: [
        { id: "satellite", type: "raster", source: "satellite" },
        { id: "osm", type: "raster", source: "osm", layout: { visibility: "none" } },
      ],
    },
  });
  map.addControl(new maplibregl.NavigationControl({ visualizePitch: false }), "top-left");
  map.addControl(new maplibregl.ScaleControl({ unit: "metric" }), "bottom-left");
  map.on("load", () => {
    addCourseLayers();
    addDynamicLayers();
    addMarkers();
    map.fitBounds(courseBounds(), { padding: 40, duration: 0 });
    updateGrid();
    if (state) renderState(state);
  });
  map.on("moveend", updateGrid);
}

function courseBounds(): LngLatBoundsLike {
  const [i0, j0, i1, j1] = course.cell_bounds;
  const a = frame.toLonLat(i0 * frame.cellM, j0 * frame.cellM);
  const b = frame.toLonLat(i1 * frame.cellM, j1 * frame.cellM);
  return [a, b];
}

function addCourseLayers() {
  map.addSource("course", { type: "geojson", data: course.features });
  const kind = (k: string | string[]): maplibregl.FilterSpecification =>
    Array.isArray(k) ? ["in", ["get", "kind"], ["literal", k]] : ["==", ["get", "kind"], k];
  map.addLayer({ id: "fairway-fill", type: "fill", source: "course", filter: kind("fairway"), paint: { "fill-color": "#86efac", "fill-opacity": 0.12 } });
  map.addLayer({ id: "fairway-line", type: "line", source: "course", filter: kind("fairway"), paint: { "line-color": "#bbf7d0", "line-width": 1.2, "line-opacity": 0.8 } });
  map.addLayer({ id: "green-line", type: "line", source: "course", filter: kind("green"), paint: { "line-color": "#4ade80", "line-width": 1.5 } });
  map.addLayer({ id: "tee-line", type: "line", source: "course", filter: kind("tee"), paint: { "line-color": "#e2e8f0", "line-width": 1, "line-opacity": 0.7 } });
  map.addLayer({ id: "bunker-line", type: "line", source: "course", filter: kind("bunker"), paint: { "line-color": "#fde68a", "line-width": 1, "line-opacity": 0.8 } });
  map.addLayer({ id: "water-line", type: "line", source: "course", filter: kind(["water_hazard", "water"]), paint: { "line-color": "#38bdf8", "line-width": 1.2 } });
  map.addLayer({ id: "hole-line", type: "line", source: "course", filter: kind("hole"), paint: { "line-color": "#ffffff", "line-width": 1.2, "line-dasharray": [2, 3], "line-opacity": 0.7 } });
}

function addDynamicLayers() {
  map.addSource("grid", { type: "geojson", data: emptyFC() });
  map.addLayer({ id: "grid", type: "line", source: "grid", paint: { "line-color": "#ffffff", "line-opacity": 0.22, "line-width": 0.6 } });

  map.addSource("cells", { type: "geojson", data: emptyFC() });
  map.addLayer({ id: "cells-fill", type: "fill", source: "cells", paint: { "fill-color": ["get", "color"], "fill-opacity": 0.45 } });
  map.addLayer({ id: "cells-outline", type: "line", source: "cells", minzoom: 18, paint: { "line-color": ["get", "color"], "line-width": 0.6, "line-opacity": 0.9 } });

  map.addSource("route", { type: "geojson", data: emptyFC() });
  map.addLayer({ id: "route-transit", type: "line", source: "route", filter: ["==", ["get", "kind"], "route"], paint: { "line-color": "#ffffff", "line-width": 1.5, "line-dasharray": [1.5, 2], "line-opacity": 0.85 } });
  map.addLayer({ id: "path-casing", type: "line", source: "route", filter: ["==", ["get", "kind"], "path"], layout: { "line-join": "round", "line-cap": "round" }, paint: { "line-color": "#0f172a", "line-width": 4.5, "line-opacity": 0.7 } });
  map.addLayer({ id: "path", type: "line", source: "route", filter: ["==", ["get", "kind"], "path"], layout: { "line-join": "round", "line-cap": "round" }, paint: { "line-color": ["get", "color"], "line-width": 2.5 } });

  map.addSource("trail", { type: "geojson", data: emptyFC() });
  map.addLayer({ id: "trail", type: "line", source: "trail", paint: { "line-color": "#f8fafc", "line-width": 2, "line-opacity": 0.9 } });

  map.addSource("box", { type: "geojson", data: emptyFC() });
  map.addLayer({ id: "box-fill", type: "fill", source: "box", paint: { "fill-color": ["get", "color"], "fill-opacity": 0.2 } });
  map.addLayer({ id: "box-line", type: "line", source: "box", paint: { "line-color": ["get", "color"], "line-width": 1.5 } });
}

let droneMarker: maplibregl.Marker;
function addMarkers() {
  const dock = document.createElement("div");
  dock.className = "dock-marker";
  dock.title = course.dock.label;
  dock.innerHTML = "<span>DOCK</span>";
  // Marker elements sit inside the map container, so their clicks would also
  // reach the map and toggle the grid cell under the dock. Stop that, and show
  // the dock's details instead.
  for (const ev of ["mousedown", "mouseup", "click", "dblclick", "contextmenu", "touchstart", "touchend"]) {
    dock.addEventListener(ev, (e) => e.stopPropagation());
  }
  dock.addEventListener("click", () => {
    new maplibregl.Popup({ offset: 14, maxWidth: "240px" })
      .setLngLat([course.dock.lon, course.dock.lat])
      .setHTML(`<b>${esc(course.dock.label)}</b><div>${course.dock.lat.toFixed(6)}, ${course.dock.lon.toFixed(6)}</div><div class="muted">Grid origin: cell (0, 0) starts here.</div>`)
      .addTo(map);
  });
  new maplibregl.Marker({ element: dock }).setLngLat([course.dock.lon, course.dock.lat]).addTo(map);

  for (const f of course.features.features) {
    if (f.properties?.kind !== "hole_label") continue;
    const el = document.createElement("div");
    el.className = "hole-marker";
    el.textContent = String(f.properties.hole);
    el.title = `Hole ${f.properties.hole} tee`;
    new maplibregl.Marker({ element: el }).setLngLat((f.geometry as GeoJSON.Point).coordinates as [number, number]).addTo(map);
  }

  const drone = document.createElement("div");
  drone.className = "drone-marker";
  drone.innerHTML = `<svg viewBox="0 0 24 24" width="26" height="26"><path d="M12 2 L19 20 L12 16 L5 20 Z" fill="#f8fafc" stroke="#0f172a" stroke-width="1.5"/></svg>`;
  droneMarker = new maplibregl.Marker({ element: drone, rotationAlignment: "map" }).setLngLat([course.dock.lon, course.dock.lat]);
}

// ---------------------------------------------------------------- grid
function updateGrid() {
  const src = map.getSource("grid") as GeoJSONSource | undefined;
  if (!src) return;
  if (map.getZoom() < 17.3) {
    src.setData(emptyFC());
    return;
  }
  const b = map.getBounds();
  const [ci0, cj0, ci1, cj1] = course.cell_bounds;
  const [vi0, vj0] = frame.cellOf(b.getWest(), b.getSouth());
  const [vi1, vj1] = frame.cellOf(b.getEast(), b.getNorth());
  const i0 = Math.max(ci0, vi0), i1 = Math.min(ci1 + 1, vi1 + 1);
  const j0 = Math.max(cj0, vj0), j1 = Math.min(cj1 + 1, vj1 + 1);
  if (i0 > i1 || j0 > j1) return src.setData(emptyFC());
  const c = frame.cellM;
  const lines: GeoJSON.Position[][] = [];
  for (let i = i0; i <= i1; i++) lines.push([frame.toLonLat(i * c, j0 * c), frame.toLonLat(i * c, j1 * c)]);
  for (let j = j0; j <= j1; j++) lines.push([frame.toLonLat(i0 * c, j * c), frame.toLonLat(i1 * c, j * c)]);
  src.setData({ type: "FeatureCollection", features: [{ type: "Feature", properties: {}, geometry: { type: "MultiLineString", coordinates: lines } }] });
}

// ---------------------------------------------------------------- state rendering
function renderState(s: MissionState) {
  state = s;
  if (map?.getSource("cells")) {
    const cellFeatures: GeoJSON.Feature[] = [];
    const routeFeatures: GeoJSON.Feature[] = [];
    for (const g of s.groups) {
      const color = groupColor(g.id);
      cellFeatures.push({
        type: "Feature",
        properties: { group: g.id, color },
        geometry: { type: "MultiPolygon", coordinates: g.cells.map(([i, j]) => [frame.cellRing(i, j)]) },
      });
      if (g.path && g.path.length > 1) {
        routeFeatures.push({ type: "Feature", properties: { kind: "path", color }, geometry: { type: "LineString", coordinates: g.path } });
      }
    }
    if (s.route) routeFeatures.unshift({ type: "Feature", properties: { kind: "route" }, geometry: { type: "LineString", coordinates: s.route.path } });
    (map.getSource("cells") as GeoJSONSource).setData({ type: "FeatureCollection", features: cellFeatures });
    (map.getSource("route") as GeoJSONSource).setData({ type: "FeatureCollection", features: routeFeatures });
  }
  renderGroups(s);
}

function renderGroups(s: MissionState) {
  $("#totals").textContent = s.total_cells
    ? `${fmtInt(s.total_cells)} cells · ${fmtInt(s.total_area_m2)} m² · ${s.groups.length} group${s.groups.length === 1 ? "" : "s"}`
    : "";
  const box = $("#groups");
  if (!s.groups.length) {
    box.innerHTML = `<div class="empty">No cells selected. Click cells on the map, shift-drag a box, or ask the agent.</div>`;
  } else {
    box.innerHTML = s.groups.map(groupCard).join("");
    box.querySelectorAll<HTMLElement>("[data-focus]").forEach((el) =>
      el.addEventListener("click", () => {
        const g = s.groups.find((x) => x.id === Number(el.dataset.focus));
        if (g) focus(g.bounds);
      }),
    );
  }
  const r = s.route;
  $("#route").innerHTML = r
    ? `<b>Route preview:</b> ${fmtInt(r.total_m)} m total (${fmtInt(r.survey_m)} m of passes, ${fmtInt(r.transit_m)} m to and from the dock), about ${fmtTime(r.est_time_s)} at ${s.assumptions.speed_ms} m/s. Passes ${s.assumptions.pass_spacing_m} m apart (${s.assumptions.altitude_m} m altitude, ${Math.round(s.assumptions.sidelap * 100)}% sidelap). Estimates; no battery or charge stops in M1.`
    : s.groups.length
      ? `Pass spacing ${s.assumptions.pass_spacing_m} m at ${s.assumptions.altitude_m} m altitude. Click Preview path to draw the passes.`
      : "";
  updateButtons();
}

function groupCard(g: Group): string {
  const p = g.preview;
  return `<div class="group" data-focus="${g.id}" title="Click to zoom to ${g.label}">
    <div class="group-head"><span class="swatch" style="background:${groupColor(g.id)}"></span><b>${g.label}</b>
      <span class="muted">${g.holes.length ? "hole " + g.holes.join(", ") : "off-fairway"}</span></div>
    <div class="stats">
      <div><span>Cells</span>${fmtInt(g.cell_count)}</div>
      <div><span>Area</span>${fmtInt(g.area_m2)} m²</div>
      <div><span>Size</span>${g.size_m[0]} × ${g.size_m[1]} m</div>
      <div><span>From dock</span>≤ ${fmtInt(g.farthest_from_dock_m)} m</div>
      ${p ? `<div><span>Passes</span>${p.passes} · ${fmtInt(p.length_m)} m</div><div><span>Survey time</span>${fmtTime(p.est_time_s)}</div>` : ""}
    </div>
  </div>`;
}

function focus(bounds: [number, number, number, number]) {
  map.fitBounds([[bounds[0], bounds[1]], [bounds[2], bounds[3]]], { padding: 70, duration: 1400, maxZoom: 19 });
}

// ---------------------------------------------------------------- selection by hand
async function edit(op: string, cells: [number, number][] = [], box?: [number, number, number, number]) {
  try {
    renderState(await api.select(op, cells, box));
  } catch (e) {
    toast(String((e as Error).message));
  }
}

function inGrid([i, j]: [number, number]) {
  const [i0, j0, i1, j1] = course.cell_bounds;
  return i >= i0 && i <= i1 && j >= j0 && j <= j1;
}

function setupSelection() {
  let start: { cell: [number, number]; remove: boolean } | null = null;
  const boxSrc = () => map.getSource("box") as GeoJSONSource;

  map.on("mousedown", (e) => {
    const oe = e.originalEvent;
    if (!(oe.shiftKey || oe.altKey) || oe.button !== 0) return;
    e.preventDefault();
    map.dragPan.disable();
    start = { cell: frame.cellOf(e.lngLat.lng, e.lngLat.lat), remove: oe.altKey };
  });
  map.on("mousemove", (e) => {
    if (!start) return;
    const end = frame.cellOf(e.lngLat.lng, e.lngLat.lat);
    const c = frame.cellM;
    const [i0, i1] = [Math.min(start.cell[0], end[0]), Math.max(start.cell[0], end[0]) + 1];
    const [j0, j1] = [Math.min(start.cell[1], end[1]), Math.max(start.cell[1], end[1]) + 1];
    const a = frame.toLonLat(i0 * c, j0 * c), b = frame.toLonLat(i1 * c, j1 * c);
    boxSrc().setData({ type: "FeatureCollection", features: [{ type: "Feature", properties: { color: start.remove ? "#f87171" : "#22d3ee" }, geometry: { type: "Polygon", coordinates: [[a, [b[0], a[1]], b, [a[0], b[1]], a]] } }] });
  });
  const finish = (e: maplibregl.MapMouseEvent) => {
    if (!start) return;
    const end = frame.cellOf(e.lngLat.lng, e.lngLat.lat);
    const s = start;
    start = null;
    map.dragPan.enable();
    boxSrc().setData(emptyFC());
    edit(s.remove ? "remove" : "add", [], [s.cell[0], s.cell[1], end[0], end[1]]);
  };
  map.on("mouseup", finish);

  map.on("click", (e) => {
    const oe = e.originalEvent;
    if (oe.shiftKey || oe.altKey) return;
    const cell = frame.cellOf(e.lngLat.lng, e.lngLat.lat);
    if (!inGrid(cell)) return toast("That spot is outside the front-nine grid.");
    edit("toggle", [cell]);
  });

  map.on("contextmenu", (e) => {
    const cell = frame.cellOf(e.lngLat.lng, e.lngLat.lat);
    if (!inGrid(cell)) return;
    showCellPopup(cell);
  });
}

const offset = (m: number, pos: string, neg: string) => `${Math.abs(m).toFixed(1)} m ${m >= 0 ? pos : neg}`;

function showCellPopup([i, j]: [number, number]) {
  const c = frame.cellM;
  const center = frame.toLonLat((i + 0.5) * c, (j + 0.5) * c);
  const group = state?.groups.find((g) => g.cells.some(([a, b]) => a === i && b === j));
  const el = document.createElement("div");
  el.className = "cell-popup";
  el.innerHTML = `<b>Cell (${i}, ${j})</b>
    <div class="muted">${offset((i + 0.5) * c, "E", "W")} · ${offset((j + 0.5) * c, "N", "S")} of the dock</div>
    <div>${center[1].toFixed(6)}, ${center[0].toFixed(6)}</div>
    <div>${group ? group.label : "Not selected"}</div>
    <div class="w3w">${course.w3w_configured ? `<button class="small">Get what3words</button>` : `<span class="muted">what3words not configured</span>`}</div>`;
  const popup = new maplibregl.Popup({ closeButton: true, maxWidth: "260px" }).setLngLat(center).setDOMContent(el).addTo(map);
  el.querySelector("button")?.addEventListener("click", async () => {
    const out = el.querySelector(".w3w")!;
    out.textContent = "Looking up…";
    try {
      const r = await api.w3w(i, j);
      out.innerHTML = r.words ? `<span class="w3w-words">///${esc(r.words)}</span> <span class="muted">(square at cell center${r.cached ? ", cached" : ""})</span>` : `<span class="err">${esc(r.error ?? "No result")}</span>`;
    } catch (e) {
      out.innerHTML = `<span class="err">${esc((e as Error).message)}</span>`;
    }
    popup.setDOMContent(el);
  });
}

// ---------------------------------------------------------------- chat
function addChat(role: "user" | "agent" | "error", html: string) {
  const log = $("#chat-log");
  const el = document.createElement("div");
  el.className = `msg ${role}`;
  el.innerHTML = html;
  log.appendChild(el);
  log.scrollTop = log.scrollHeight;
  return el;
}

function describeTool(t: AgentReply["tool_calls"][number]): string {
  const a = t.input as Record<string, unknown>;
  const args = Object.entries(a).map(([k, v]) => `${k}=${Array.isArray(v) ? v.join(",") : v}`).join(" ");
  return `<span class="tool ${t.ok ? "" : "bad"}" title="${esc(t.error ?? "")}">${esc(t.name)}${args ? " " + esc(args) : ""}</span>`;
}

async function sendChat(text: string) {
  text = text.trim();
  if (!text) return;
  addChat("user", esc(text));
  const input = $<HTMLInputElement>("#chat-input");
  input.value = "";
  const send = $<HTMLButtonElement>("#chat-send");
  send.disabled = true;
  const pending = addChat("agent", `<span class="typing">Thinking…</span>`);
  try {
    const r = await api.chat(text);
    for (const a of r.actions) if (a.type === "focus") focus(a.bounds);
    renderState(r.state);
    const cost = r.usage.input_tokens + r.usage.output_tokens;
    pending.innerHTML = `${esc(r.reply)}${r.tool_calls.length ? `<div class="tools">${r.tool_calls.map(describeTool).join("")}</div>` : ""}<div class="usage">${esc(r.model)} · ${r.usage.calls} call${r.usage.calls === 1 ? "" : "s"} · ${fmtInt(cost)} tokens</div>`;
  } catch (e) {
    pending.className = "msg error";
    pending.textContent = (e as Error).message;
  } finally {
    send.disabled = false;
    input.focus();
  }
}

function setupChat() {
  $("#chat-form").addEventListener("submit", (e) => {
    e.preventDefault();
    sendChat($<HTMLInputElement>("#chat-input").value);
  });
  const sug = $("#suggestions");
  for (const s of SUGGESTIONS) {
    const b = document.createElement("button");
    b.className = "suggestion";
    b.textContent = s;
    b.addEventListener("click", () => sendChat(s));
    sug.appendChild(b);
  }
  const reset = document.createElement("button");
  reset.className = "suggestion ghost";
  reset.textContent = "New conversation";
  reset.addEventListener("click", async () => {
    await api.chatReset();
    $("#chat-log").innerHTML = "";
    addChat("agent", "New conversation. The selection on the map is unchanged.");
  });
  sug.appendChild(reset);
  if (!course.agent.configured) {
    addChat("error", "The agent is off: add ANTHROPIC_API_KEY to backend/.env and restart the backend. Manual selection still works.");
  } else {
    addChat("agent", `Hi! Ask me to survey part of holes 1–9, for example “survey the fairway on hole 4”.`);
  }
}

// ---------------------------------------------------------------- drone
const trail: [number, number][] = [];

function renderTelemetry(t: Telemetry) {
  const wasFlying = flying;
  flying = !["DOCKED"].includes(t.mode);
  const battColor = t.battery_pct > 50 ? "#4ade80" : t.battery_pct > 25 ? "#facc15" : "#f87171";
  $("#drone").innerHTML = `
    <div class="drone-status"><span class="mode mode-${t.mode.toLowerCase()}">${t.mode}</span> ${esc(t.message)}</div>
    <div class="battery"><div style="width:${Math.max(0, t.battery_pct)}%;background:${battColor}"></div><span>${t.battery_pct.toFixed(0)}% · ${t.voltage_v.toFixed(1)} V</span></div>
    <div class="stats">
      <div><span>Altitude</span>${t.alt_m.toFixed(1)} m</div>
      <div><span>Speed</span>${t.groundspeed_ms.toFixed(1)} m/s</div>
      <div><span>Heading</span>${t.heading_deg}°</div>
      <div><span>GPS</span>${t.gps_fix} · ${t.satellites} sats</div>
      <div><span>Flown</span>${fmtInt(t.distance_flown_m)} / ${fmtInt(t.route_m)} m</div>
      <div><span>Flight time</span>${fmtTime(t.elapsed_s)}${t.speedup !== 1 ? ` <em class="muted">(${t.speedup}× sim)</em>` : ""}</div>
    </div>`;
  if (!map || !droneMarker) return;
  if (flying) {
    if (!wasFlying) trail.length = 0;
    droneMarker.setLngLat([t.lon, t.lat]).setRotation(t.heading_deg).addTo(map);
    trail.push([t.lon, t.lat]);
    (map.getSource("trail") as GeoJSONSource | undefined)?.setData({ type: "Feature", properties: {}, geometry: { type: "LineString", coordinates: trail } });
  } else {
    droneMarker.remove();
  }
  if (wasFlying !== flying) updateButtons();
}

function connectTelemetry() {
  const ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws/telemetry`);
  ws.onmessage = (m) => renderTelemetry(JSON.parse(m.data));
  ws.onclose = () => setTimeout(connectTelemetry, 1500);
}

function updateButtons() {
  const hasGroups = !!state?.groups.length;
  $<HTMLButtonElement>("#btn-preview").disabled = !hasGroups;
  $<HTMLButtonElement>("#btn-clear").disabled = !hasGroups;
  $<HTMLButtonElement>("#btn-fly").disabled = flying || !state?.route;
  $<HTMLButtonElement>("#btn-abort").disabled = !flying;
}

function setupButtons() {
  $("#btn-preview").addEventListener("click", async () => {
    try {
      renderState(await api.preview());
    } catch (e) {
      toast((e as Error).message);
    }
  });
  $("#btn-clear").addEventListener("click", () => edit("clear"));
  $("#btn-fly").addEventListener("click", async () => {
    try {
      await api.fly();
      if (state?.route) focus(boundsOf(state.route.path));
    } catch (e) {
      toast((e as Error).message);
    }
  });
  $("#btn-abort").addEventListener("click", () => api.abort().catch((e) => toast(e.message)));
  document.querySelectorAll<HTMLButtonElement>("#basemap-toggle button").forEach((b) =>
    b.addEventListener("click", () => {
      const sat = b.dataset.basemap === "satellite";
      map.setLayoutProperty("satellite", "visibility", sat ? "visible" : "none");
      map.setLayoutProperty("osm", "visibility", sat ? "none" : "visible");
      document.querySelectorAll("#basemap-toggle button").forEach((x) => x.classList.toggle("active", x === b));
    }),
  );
}

function boundsOf(pts: [number, number][]): [number, number, number, number] {
  const xs = pts.map((p) => p[0]), ys = pts.map((p) => p[1]);
  return [Math.min(...xs), Math.min(...ys), Math.max(...xs), Math.max(...ys)];
}

function toast(msg: string) {
  const el = document.createElement("div");
  el.className = "toast";
  el.textContent = msg;
  document.body.appendChild(el);
  setTimeout(() => el.remove(), 4000);
}

// ---------------------------------------------------------------- boot
async function boot() {
  try {
    course = await api.course();
  } catch (e) {
    $("#course-name").textContent = `Backend not reachable: ${(e as Error).message}`;
    return;
  }
  frame = new LocalFrame(course.frame.lat0, course.frame.lon0, course.frame.cell_m);
  $("#course-name").textContent = `${course.name} · dock at hole 1 tee`;
  $("#model-chip").textContent = course.agent.model;
  buildMap();
  setupSelection();
  setupChat();
  setupButtons();
  renderState(await api.state());
  connectTelemetry();
}

boot();
