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
  fly: () => req<{ ok: boolean }>("POST", "/api/fly"),
  abort: () => req<{ ok: boolean }>("POST", "/api/abort"),
};
