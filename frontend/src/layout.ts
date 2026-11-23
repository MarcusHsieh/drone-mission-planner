// The dashboard layout: every part of the app is a panel that can be dragged
// by its tab to another spot (or into another panel's tab strip), resized by
// the borders between panels, floated, closed and reopened from the Panels
// menu. The layout is saved in this browser's localStorage; Reset layout puts
// the default back.
//
// Panel contents live in #panel-store in index.html and are created once. A
// panel only borrows its element, and gives it back when it closes, so the
// map, the chat log and the message log keep their state when panels move or
// close, and code can keep finding elements by id.

import { createDockview, DockviewApi, IContentRenderer, themeAbyss } from "dockview-core";

export interface PanelDef {
  id: string;
  title: string;
  /** One line for the Panels menu. */
  hint: string;
}

export const PANELS: PanelDef[] = [
  { id: "map", title: "Map", hint: "Course, selection, route and the drone (2D, 3D, chase)" },
  { id: "agent", title: "Agent", hint: "Ask Claude to select and plan" },
  { id: "selection", title: "Selection", hint: "Selected groups and the route preview" },
  { id: "tasks", title: "Tasks", hint: "Divot task queue: legs, charge stops, task status" },
  { id: "flight", title: "Flight", hint: "Fly, abort, mode, mission progress" },
  { id: "hud", title: "Attitude", hint: "Artificial horizon, heading, speed, altitude, climb" },
  { id: "battery", title: "Battery", hint: "Voltage, current, mAh used, time left" },
  { id: "gps", title: "GPS", hint: "Fix, satellites, HDOP, distance to the dock" },
  { id: "health", title: "Health", hint: "Arming checks, EKF, vibration, sensors" },
  { id: "messages", title: "Messages", hint: "Autopilot messages (STATUSTEXT) and planner events" },
  { id: "link", title: "Link", hint: "Heartbeat, packet loss, RC signal" },
  { id: "rangefinder", title: "Rangefinder", hint: "Downward lidar (TFmini-S) distance" },
];

// Bump when PANELS or the default layout changes enough that old saved layouts should be dropped.
const STORAGE_KEY = "dmp.dashboard.layout.v2";

const store = () => document.getElementById("panel-store")!;
const bodyOf = (id: string) => document.querySelector<HTMLElement>(`[data-panel="${id}"]`);

class StoredPanel implements IContentRenderer {
  readonly element: HTMLElement;
  constructor(id: string) {
    this.element = bodyOf(id) ?? Object.assign(document.createElement("div"), { className: "panel-body pad muted", textContent: `Unknown panel "${id}"` });
  }
  init(): void {
    this.element.dataset.open = "true";
  }
  dispose(): void {
    // Keep the element (and its listeners and state) for when the panel is reopened.
    delete this.element.dataset.open;
    if (this.element.dataset.panel) store().appendChild(this.element);
  }
}

export class Dashboard {
  readonly api: DockviewApi;
  private saveTimer = 0;
  private loading = false;

  constructor(root: HTMLElement) {
    this.api = createDockview(root, {
      theme: themeAbyss,
      // Keep every open panel's content in the DOM, even in a hidden tab, so the
      // map and the HUD canvas aren't torn down and rebuilt on every tab switch.
      defaultRenderer: "always",
      createComponent: (opts) => new StoredPanel(opts.name),
    });
    this.loading = true;
    if (!this.restore()) this.defaultLayout();
    this.loading = false;
    this.api.onDidLayoutChange(() => this.scheduleSave());
    this.setupMenu();
  }

  isOpen(id: string): boolean {
    return !!this.api.getPanel(id);
  }

  /** Bring a panel to the front, reopening it (floating) if it was closed. */
  show(id: string): void {
    const p = this.api.getPanel(id);
    if (p) return p.api.setActive();
    const def = PANELS.find((d) => d.id === id)!;
    const w = this.api.width, h = this.api.height;
    this.api.addPanel({
      id,
      component: id,
      title: def.title,
      floating: { width: Math.min(420, w - 40), height: Math.min(320, h - 40), x: Math.max(20, w / 2 - 210), y: Math.max(20, h / 2 - 160) },
    });
  }

  close(id: string): void {
    this.api.getPanel(id)?.api.close();
  }

  reset(): void {
    this.loading = true;
    this.api.clear();
    this.defaultLayout();
    this.loading = false;
    try {
      localStorage.removeItem(STORAGE_KEY);
    } catch {
      /* storage blocked: nothing saved anyway */
    }
  }

  private defaultLayout(): void {
    const add = (id: string, position?: Parameters<DockviewApi["addPanel"]>[0]["position"], size?: { initialWidth?: number; initialHeight?: number }) =>
      this.api.addPanel({ id, component: id, title: PANELS.find((d) => d.id === id)!.title, position, ...size });
    // Map on the left; planning (agent and selection as tabs) and the flight
    // controls on the right; flight instruments in a strip along the bottom,
    // the way a ground station lays them out.
    add("map");
    add("agent", { referencePanel: "map", direction: "right" }, { initialWidth: 410 });
    add("selection", { referencePanel: "agent", direction: "within" });
    add("tasks", { referencePanel: "agent", direction: "within" });
    add("flight", { referencePanel: "agent", direction: "below" });
    // The instrument strip spans the full width so each panel gets room.
    add("hud", { direction: "below" }, { initialHeight: 270 });
    add("battery", { referencePanel: "hud", direction: "right" });
    add("gps", { referencePanel: "battery", direction: "right" });
    add("rangefinder", { referencePanel: "gps", direction: "within" });
    add("health", { referencePanel: "gps", direction: "right" });
    add("link", { referencePanel: "health", direction: "within" });
    add("messages", { referencePanel: "health", direction: "right" });
    // Show the first tab of each tabbed group.
    this.api.getPanel("gps")?.api.setActive();
    this.api.getPanel("health")?.api.setActive();
    this.api.getPanel("agent")?.api.setActive();
    this.api.getPanel("map")?.api.setActive();
    // Give the message log a bit more room than the number panels.
    const w = this.api.width;
    if (w > 1100) this.api.getPanel("messages")?.group.api.setSize({ width: Math.round(w * 0.27) });
  }

  private restore(): boolean {
    let raw: string | null = null;
    try {
      raw = localStorage.getItem(STORAGE_KEY);
    } catch {
      return false;
    }
    if (!raw) return false;
    try {
      const data = JSON.parse(raw);
      // Drop panels this version doesn't have.
      const known = new Set(PANELS.map((p) => p.id));
      if (data?.panels) for (const id of Object.keys(data.panels)) if (!known.has(id)) throw new Error(`unknown panel ${id}`);
      this.api.fromJSON(data);
      return this.api.panels.length > 0;
    } catch (e) {
      console.warn("Saved dashboard layout ignored:", e);
      this.api.clear();
      return false;
    }
  }

  private scheduleSave(): void {
    if (this.loading) return;
    this.refreshMenu();
    clearTimeout(this.saveTimer);
    this.saveTimer = window.setTimeout(() => {
      try {
        localStorage.setItem(STORAGE_KEY, JSON.stringify(this.api.toJSON()));
      } catch {
        /* storage full or blocked: the layout just won't persist */
      }
    }, 400);
  }

  // ------------------------------------------------------------ Panels menu
  private setupMenu(): void {
    const btn = document.getElementById("btn-panels")!;
    const menu = document.getElementById("panels-menu")!;
    const setOpen = (open: boolean) => {
      menu.hidden = !open;
      btn.setAttribute("aria-expanded", String(open));
      if (open) this.refreshMenu();
    };
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      setOpen(menu.hidden);
    });
    document.addEventListener("click", (e) => {
      if (!menu.hidden && !menu.contains(e.target as Node)) setOpen(false);
    });
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape") setOpen(false);
    });
    menu.addEventListener("change", (e) => {
      const box = e.target as HTMLInputElement;
      if (box.checked) this.show(box.value);
      else this.close(box.value);
    });
    document.getElementById("btn-reset-layout")!.addEventListener("click", () => this.reset());
  }

  private refreshMenu(): void {
    const menu = document.getElementById("panels-menu")!;
    if (menu.hidden) return;
    menu.innerHTML =
      PANELS.map(
        (p) => `<label title="${p.hint}"><input type="checkbox" value="${p.id}" ${this.isOpen(p.id) ? "checked" : ""}/> <b>${p.title}</b><span class="muted">${p.hint}</span></label>`,
      ).join("") + `<div class="menu-note muted">Drag a tab to move a panel or stack it with others. Drag a border to resize. Saved in this browser.</div>`;
  }
}
