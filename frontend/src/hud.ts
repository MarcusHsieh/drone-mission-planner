// Attitude indicator (artificial horizon) with heading, speed, altitude and
// climb, drawn on a canvas the way a ground station's HUD is. Telemetry comes
// at about 5 Hz; the drawing eases toward each new value so it doesn't step.

import type { Telemetry } from "./api";

interface Pose {
  roll: number;
  pitch: number;
  heading: number;
  speed: number;
  alt: number;
  climb: number;
}

const SKY_TOP = "#1d4f91";
const SKY = "#3a7bd5";
const GROUND = "#7a5230";
const GROUND_LOW = "#4b321d";
const INK = "#f8fafc";
const ACCENT = "#facc15";
const EASE_S = 0.12;

export class Hud {
  private readonly ctx: CanvasRenderingContext2D;
  private target: Pose | null = null;
  private pose: Pose | null = null;
  private mode = "";
  private armed = false;
  private stale = true;
  private last = 0;
  private queued = false;

  constructor(private readonly canvas: HTMLCanvasElement) {
    this.ctx = canvas.getContext("2d")!;
    new ResizeObserver(() => this.draw()).observe(canvas.parentElement!);
  }

  update(t: Telemetry): void {
    this.target = {
      roll: t.roll_deg ?? 0,
      pitch: t.pitch_deg ?? 0,
      heading: t.heading_deg,
      speed: t.groundspeed_ms,
      alt: t.alt_m,
      climb: t.climb_ms ?? 0,
    };
    this.pose ??= { ...this.target };
    this.mode = t.mode;
    this.armed = !!t.armed;
    this.stale = t.connected === false;
    this.kick();
  }

  setStale(stale: boolean): void {
    if (stale !== this.stale) {
      this.stale = stale;
      this.kick();
    }
  }

  private kick(): void {
    if (this.queued) return;
    this.queued = true;
    requestAnimationFrame((now) => this.frame(now));
  }

  private frame(now: number): void {
    this.queued = false;
    const dt = this.last ? Math.min(0.1, (now - this.last) / 1000) : 0.016;
    this.last = now;
    let moving = false;
    if (this.pose && this.target) {
      const k = 1 - Math.exp(-dt / EASE_S);
      for (const key of Object.keys(this.target) as (keyof Pose)[]) {
        let d = this.target[key] - this.pose[key];
        if (key === "heading") d = ((d + 540) % 360) - 180;
        if (Math.abs(d) > 0.01) {
          this.pose[key] += d * k;
          moving = true;
        } else this.pose[key] = this.target[key];
      }
      this.pose.heading = (this.pose.heading + 360) % 360;
    }
    this.draw();
    if (moving) this.kick();
    else this.last = 0;
  }

  draw(): void {
    const c = this.canvas;
    const box = c.parentElement!.getBoundingClientRect();
    if (box.width < 10 || box.height < 10) return; // hidden tab
    const dpr = window.devicePixelRatio || 1;
    const W = Math.round(box.width), H = Math.round(box.height);
    if (c.width !== W * dpr || c.height !== H * dpr) {
      c.width = W * dpr;
      c.height = H * dpr;
      c.style.width = `${W}px`;
      c.style.height = `${H}px`;
    }
    const g = this.ctx;
    g.setTransform(dpr, 0, 0, dpr, 0, 0);
    const p = this.pose ?? { roll: 0, pitch: 0, heading: 0, speed: 0, alt: 0, climb: 0 };
    const cx = W / 2, cy = H / 2 + 6;
    const R = Math.min(W, H);
    const pxPerDeg = R / 70; // about 70 degrees of pitch from top to bottom
    const font = (px: number, weight = 600) => `${weight} ${px}px ui-monospace, SFMono-Regular, Menlo, monospace`;
    const fs = Math.max(10, Math.min(15, R / 18));

    // ---- horizon, rotated by roll and shifted by pitch
    g.save();
    g.translate(cx, cy);
    g.rotate((-p.roll * Math.PI) / 180);
    g.translate(0, p.pitch * pxPerDeg);
    const big = Math.hypot(W, H) * 2;
    let grad = g.createLinearGradient(0, -big / 2, 0, 0);
    grad.addColorStop(0, SKY_TOP);
    grad.addColorStop(1, SKY);
    g.fillStyle = grad;
    g.fillRect(-big, -big, big * 2, big);
    grad = g.createLinearGradient(0, 0, 0, big / 2);
    grad.addColorStop(0, GROUND);
    grad.addColorStop(1, GROUND_LOW);
    g.fillStyle = grad;
    g.fillRect(-big, 0, big * 2, big);
    g.strokeStyle = INK;
    g.lineWidth = 1.5;
    g.beginPath();
    g.moveTo(-big, 0);
    g.lineTo(big, 0);
    g.stroke();
    // pitch ladder
    g.fillStyle = INK;
    g.font = font(fs * 0.75, 500);
    g.textBaseline = "middle";
    for (let a = -60; a <= 60; a += 5) {
      if (a === 0) continue;
      const y = -a * pxPerDeg;
      if (Math.abs(y - p.pitch * pxPerDeg) > H * 0.42) continue;
      const half = a % 10 === 0 ? R * 0.14 : R * 0.06;
      g.lineWidth = 1;
      g.beginPath();
      if (a < 0) g.setLineDash([4, 3]);
      g.moveTo(-half, y);
      g.lineTo(half, y);
      g.stroke();
      g.setLineDash([]);
      if (a % 10 === 0) {
        g.textAlign = "left";
        g.fillText(String(Math.abs(a)), half + 4, y);
        g.textAlign = "right";
        g.fillText(String(Math.abs(a)), -half - 4, y);
      }
    }
    g.restore();

    // ---- roll arc and pointer
    const arcR = R * 0.38;
    g.save();
    g.translate(cx, cy);
    g.strokeStyle = INK;
    g.lineWidth = 1.5;
    g.beginPath();
    g.arc(0, 0, arcR, (-150 * Math.PI) / 180, (-30 * Math.PI) / 180);
    g.stroke();
    for (const a of [-60, -45, -30, -20, -10, 0, 10, 20, 30, 45, 60]) {
      const r = ((a - 90) * Math.PI) / 180;
      const len = a % 30 === 0 ? 10 : 6;
      g.beginPath();
      g.moveTo(Math.cos(r) * arcR, Math.sin(r) * arcR);
      g.lineTo(Math.cos(r) * (arcR + len), Math.sin(r) * (arcR + len));
      g.stroke();
    }
    g.rotate((-p.roll * Math.PI) / 180);
    g.fillStyle = ACCENT;
    g.beginPath();
    g.moveTo(0, -arcR + 1);
    g.lineTo(-7, -arcR + 12);
    g.lineTo(7, -arcR + 12);
    g.closePath();
    g.fill();
    g.restore();

    // ---- fixed aircraft symbol
    g.save();
    g.translate(cx, cy);
    g.strokeStyle = "#0b1220";
    g.fillStyle = ACCENT;
    g.lineWidth = 1;
    const wing = R * 0.16;
    for (const s of [-1, 1]) {
      g.beginPath();
      g.rect(s > 0 ? R * 0.05 : -R * 0.05 - wing, -2.5, wing, 5);
      g.fill();
      g.stroke();
    }
    g.beginPath();
    g.arc(0, 0, 3.5, 0, Math.PI * 2);
    g.fill();
    g.stroke();
    g.restore();

    // ---- heading tape along the top
    const tapeH = fs * 1.9;
    g.fillStyle = "#0b1220b3";
    g.fillRect(0, 0, W, tapeH);
    const pxPerHdg = W / 90;
    g.strokeStyle = INK;
    g.fillStyle = INK;
    g.font = font(fs * 0.8, 500);
    g.textAlign = "center";
    g.textBaseline = "top";
    const names: Record<number, string> = { 0: "N", 45: "NE", 90: "E", 135: "SE", 180: "S", 225: "SW", 270: "W", 315: "NW" };
    for (let d = Math.floor((p.heading - 50) / 5) * 5; d <= p.heading + 50; d += 5) {
      const x = W / 2 + (d - p.heading) * pxPerHdg;
      const h = ((d % 360) + 360) % 360;
      g.lineWidth = 1;
      g.beginPath();
      g.moveTo(x, tapeH);
      g.lineTo(x, tapeH - (h % 10 === 0 ? 7 : 4));
      g.stroke();
      if (h % 15 === 0 && names[h] !== undefined) g.fillText(names[h], x, 3);
      else if (h % 30 === 0) g.fillText(String(h), x, 3);
    }
    // current heading box
    const hdgTxt = String(Math.round(p.heading) % 360).padStart(3, "0");
    g.font = font(fs);
    const bw = g.measureText("000").width + 12;
    g.fillStyle = "#0b1220";
    g.fillRect(W / 2 - bw / 2, tapeH - 1, bw, fs + 6);
    g.strokeStyle = ACCENT;
    g.strokeRect(W / 2 - bw / 2, tapeH - 1, bw, fs + 6);
    g.fillStyle = ACCENT;
    g.fillText(hdgTxt, W / 2, tapeH + 2);

    // ---- speed (left) and altitude (right) boxes
    const label = (x: number, align: CanvasTextAlign, top: string, value: string, unit: string) => {
      g.textAlign = align;
      g.textBaseline = "middle";
      g.font = font(fs * 0.7, 500);
      const w = Math.max(g.measureText(top).width, (g.font = font(fs * 1.25), g.measureText(value).width)) + 14;
      const bx = align === "left" ? x : x - w;
      g.fillStyle = "#0b1220cc";
      g.fillRect(bx, cy - fs * 1.4, w, fs * 2.8);
      g.strokeStyle = "#ffffff55";
      g.strokeRect(bx, cy - fs * 1.4, w, fs * 2.8);
      const tx = align === "left" ? bx + 7 : bx + w - 7;
      g.fillStyle = "#cbd5e1";
      g.font = font(fs * 0.7, 500);
      g.fillText(top, tx, cy - fs * 0.75);
      g.fillStyle = INK;
      g.font = font(fs * 1.25);
      g.fillText(value + unit, tx, cy + fs * 0.45);
    };
    label(6, "left", "GND SPD", p.speed.toFixed(1), "");
    label(W - 6, "right", "ALT (dock)", p.alt.toFixed(1), "");
    // climb under the altitude
    g.font = font(fs * 0.85);
    g.textAlign = "right";
    g.textBaseline = "top";
    const vs = p.climb;
    g.fillStyle = Math.abs(vs) < 0.2 ? "#cbd5e1" : vs > 0 ? "#86efac" : "#fca5a5";
    g.fillText(`${vs > 0.05 ? "▲" : vs < -0.05 ? "▼" : "•"} ${Math.abs(vs).toFixed(1)} m/s`, W - 8, cy + fs * 1.7);
    g.textAlign = "left";
    g.fillStyle = "#cbd5e1";
    g.fillText("m/s", 8, cy + fs * 1.7);

    // ---- mode and armed state along the bottom
    g.textBaseline = "bottom";
    g.font = font(fs * 0.95);
    g.textAlign = "left";
    g.fillStyle = "#0b1220b3";
    g.fillRect(0, H - fs * 1.8, W, fs * 1.8);
    g.fillStyle = INK;
    g.fillText(this.mode || "—", 8, H - 5);
    g.textAlign = "right";
    g.fillStyle = this.armed ? "#fca5a5" : "#86efac";
    g.fillText(this.armed ? "ARMED" : "DISARMED", W - 8, H - 5);
    g.textAlign = "center";
    g.fillStyle = "#cbd5e1";
    g.font = font(fs * 0.8, 500);
    g.fillText(`R ${p.roll.toFixed(0)}°  P ${p.pitch.toFixed(0)}°`, W / 2, H - 5);

    if (this.stale) {
      g.fillStyle = "#0b1220b0";
      g.fillRect(0, 0, W, H);
      g.fillStyle = "#fca5a5";
      g.font = font(fs * 1.1);
      g.textAlign = "center";
      g.textBaseline = "middle";
      g.fillText("NO TELEMETRY", W / 2, H / 2);
    }
  }
}
