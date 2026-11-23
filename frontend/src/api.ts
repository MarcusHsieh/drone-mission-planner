export type LonLat = [number, number];

export interface HoleSummary {
  hole: number;
  par: number;
  length_m: number;
  has_fairway: boolean;
  fairway_area_m2: number;
  fairway_cells: number;
}

export interface CourseInfo {
  name: string;
  dock: { lat: number; lon: number; label: string };
  frame: { lat0: number; lon0: number; cell_m: number };
  cell_bounds: [number, number, number, number];
  holes: HoleSummary[];
  features: GeoJSON.FeatureCollection;
  agent: { model: string; configured: boolean };
  w3w_configured: boolean;
  /** Google Map Tiles API key for photorealistic 3D, or null. */
  google_3d_key: string | null;
}

export interface Group {
  id: number;
  label: string;
  cell_count: number;
  area_m2: number;
  size_m: [number, number];
  holes: number[];
  center: LonLat;
  bounds: [number, number, number, number];
  farthest_from_dock_m: number;
  preview: { length_m: number; passes: number; pass_heading_deg: number; est_time_s: number } | null;
  cells: [number, number][];
  path: LonLat[] | null;
}

export interface MissionState {
  version: number;
  total_cells: number;
  total_area_m2: number;
  groups: Group[];
  route: { order: number[]; total_m: number; survey_m: number; transit_m: number; est_time_s: number; path: LonLat[] } | null;
  assumptions: { altitude_m: number; sidelap: number; pass_spacing_m: number; speed_ms: number };
}

export interface AgentReply {
  reply: string;
  actions: ({ type: "focus"; bounds: [number, number, number, number] } | { type: "preview"; group_ids: number[] })[];
  tool_calls: { name: string; input: Record<string, unknown>; ok: boolean; error?: string | null }[];
  usage: { input_tokens: number; output_tokens: number; cache_read_input_tokens: number; calls: number };
  model: string;
  state: MissionState;
}

export interface Telemetry {
  lat: number;
  lon: number;
  alt_m: number;
  heading_deg: number;
  /** Attitude in degrees: roll right side down positive, pitch nose up positive. */
  roll_deg?: number;
  pitch_deg?: number;
  groundspeed_ms: number;
  battery_pct: number;
  voltage_v: number;
  mode: string;
  gps_fix: string;
  satellites: number;
  distance_flown_m: number;
  route_m: number;
  progress_pct: number;
  elapsed_s: number;
  speedup: number;
  message: string;
  /** "sim" for the built-in sim, "sitl" for ArduPilot SITL over MAVLink. */
  source?: "sim" | "sitl";
  waypoints_reached?: number;
  waypoints_total?: number;
  armed?: boolean;
  autopilot_mode?: string;
  connected?: boolean;
  // ---- dashboard panels (both the sim and SITL send these; the sim's health values are placeholders)
  climb_ms?: number;
  airspeed_ms?: number;
  throttle_pct?: number;
  alt_msl_m?: number | null;
  system_status?: string | null;
  dock_distance_m?: number;
  dock_bearing_deg?: number;
  battery?: {
    voltage_v: number;
    current_a: number | null;
    remaining_pct: number;
    consumed_mah: number | null;
    capacity_mah: number | null;
    time_remaining_s: number | null;
  };
  gps?: { fix: string; fix_type: number; satellites: number; hdop: number | null; vdop: number | null; h_acc_m: number | null };
  ekf?: {
    flags_ok: boolean;
    missing: string[];
    const_pos_mode: boolean;
    gps_glitching: boolean;
    variances: Record<string, number>;
    level: Level;
  } | null;
  vibration?: { x: number; y: number; z: number; clipping: [number, number, number]; level: Level } | null;
  rangefinder?: { distance_m: number; min_m: number; max_m: number; in_range: boolean; age_s: number } | null;
  sensors?: { name: string; enabled: boolean; healthy: boolean }[];
  prearm?: { ok: boolean | null; reasons: string[] };
  link?: {
    connected: boolean;
    endpoint: string;
    heartbeat_age_s: number | null;
    packet_loss_pct: number | null;
    vehicle_drop_pct: number | null;
    rc_rssi_pct: number | null;
  };
  nav?: { wp_dist_m: number | null; xtrack_m: number | null; mission_seq: number | null };
  /** The last few log entries; GET /api/messages has the whole log. */
  messages?: LogMessage[];
}

export type Level = "ok" | "warn" | "bad";

export interface LogMessage {
  id: number;
  /** Unix seconds. */
  t: number;
  /** MAVLink MAV_SEVERITY: 0 emergency ... 4 warning, 5 notice, 6 info, 7 debug. */
  severity: number;
  level: string;
  source: "vehicle" | "planner";
  text: string;
}

export type TaskStatus = "pending" | "active" | "done" | "failed";

export interface QueueTask {
  id: string;
  kind: string;
  x: number;
  y: number;
  lon: number;
  lat: number;
  hole: number | null;
  service_s: number;
  status: TaskStatus;
  attempts: number;
  leg: number | null;
  note: string;
}

export interface QueueLeg {
  index: number;
  task_ids: string[];
  distance_m: number;
  time_s: number;
  mah: number;
  pct_of_pack: number;
  end_pct: number;
  charge_after_s: number;
  status: "planned" | "flying" | "done" | "cut_short" | "aborted" | "failed";
  actual: { tasks_done?: number; flight_s?: number | null; mah?: number | null; mah_vs_plan?: number; landed_from_dock_m?: number | null };
  breakdown_s: Record<string, number>;
  path: LonLat[];
}

export interface QueueState {
  version: number;
  phase: "idle" | "flying" | "charging" | "done" | "aborted" | "failed";
  running: boolean;
  message: string;
  counts: Record<TaskStatus, number>;
  tasks: QueueTask[];
  legs: QueueLeg[];
  remaining: { legs: number; charge_stops: number; time_s: number; mah: number };
  charge: { real_s: number; wall_s: number; left_s: number; pct: number } | null;
  energy: {
    pack_mah: number;
    cells: number;
    reserve_pct: number;
    margin: number;
    hover_a: number;
    cruise_a: number;
    usable_mah: number;
    hover_minutes_to_reserve: number;
    charge_c: number;
    charge_speedup: number;
    calibrated: boolean;
  };
  service: { alt_m: number; guard_pct: number };
  field: DivotField;
}

export interface PlacedDivot {
  id: string;
  x: number;
  y: number;
  lon: number;
  lat: number;
  hole: number | null;
  found_task: string | null;
  found_error_m: number | null;
  seen: boolean;
}

export interface DivotField {
  placed: PlacedDivot[];
  scanning: boolean;
  scans: number;
  last_scan: { index: number; found: number; not_covered: number } | null;
  found: number;
  not_found: number;
  mean_error_m: number | null;
  camera: { footprint_m: number; pos_err_m: number };
}

async function req<T>(method: string, url: string, body?: unknown): Promise<T> {
  const r = await fetch(url, {
    method,
    headers: body === undefined ? undefined : { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!r.ok) {
    let detail = `${r.status} ${r.statusText}`;
    try {
      const j = await r.json();
      if (j.detail) detail = typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail);
    } catch {
      /* not JSON */
    }
    throw new Error(detail);
  }
  return r.json() as Promise<T>;
}

export const api = {
  course: () => req<CourseInfo>("GET", "/api/course"),
  state: () => req<MissionState>("GET", "/api/state"),
  select: (op: string, cells: [number, number][] = [], box?: [number, number, number, number]) =>
    req<MissionState>("POST", "/api/selection", { op, cells, box }),
  preview: () => req<MissionState>("POST", "/api/preview", {}),
  chat: (message: string) => req<AgentReply>("POST", "/api/agent", { message }),
  chatReset: () => req<{ ok: boolean }>("POST", "/api/agent/reset"),
  w3w: (i: number, j: number) =>
    req<{ words: string | null; error?: string; lat: number; lon: number; cached?: boolean }>("GET", `/api/w3w?i=${i}&j=${j}`),
  buildings: () => req<GeoJSON.FeatureCollection & { source: string; error: string | null }>("GET", "/api/buildings"),
  fly: () => req<{ ok: boolean }>("POST", "/api/fly"),
  abort: () => req<{ ok: boolean }>("POST", "/api/abort"),
  messages: () => req<{ messages: LogMessage[] }>("GET", "/api/messages"),
  queue: () => req<QueueState>("GET", "/api/queue"),
  divots: (count: number, holes: number[] | null, seed: number | null) =>
    req<QueueState>("POST", "/api/queue/divots", { count, holes, seed }),
  queueRun: () => req<QueueState>("POST", "/api/queue/run"),
  queueAbort: () => req<QueueState>("POST", "/api/queue/abort"),
  queueClear: () => req<QueueState>("POST", "/api/queue/clear"),
  placed: (body: { op: "add" | "remove" | "clear" | "scatter"; points?: LonLat[]; ids?: string[]; count?: number; holes?: number[] | null }) =>
    req<QueueState>("POST", "/api/field/divots", body),
  scan: () => req<QueueState>("POST", "/api/scan"),
};
