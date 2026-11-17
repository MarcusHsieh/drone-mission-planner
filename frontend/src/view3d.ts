// 3D view of a flight on the planner's map: a quadcopter model at its real
// altitude and attitude, the planned route at survey altitude, the flown track
// in 3D, and the dock pad. Drawn with three.js in a MapLibre custom layer, so
// it shares the map's camera and sits on the satellite imagery. It is driven
// only by the telemetry WebSocket, so it works the same for the built-in sim
// and for ArduPilot SITL.
//
// Scene units are meters in the dock frame: x east, y north, z up, with the
// dock at the origin. Altitudes are relative to the dock (the ground is flat
// at the dock's height; no terrain model).

import maplibregl, { CustomLayerInterface, Map as MLMap } from "maplibre-gl";
import * as THREE from "three";
import { Line2 } from "three/examples/jsm/lines/Line2.js";
import { LineGeometry } from "three/examples/jsm/lines/LineGeometry.js";
import { LineMaterial } from "three/examples/jsm/lines/LineMaterial.js";
import type { Telemetry } from "./api";
import type { LocalFrame } from "./geo";
import { Photoreal } from "./photoreal";

/** The SITL quad is about 0.5 m motor to motor. */
const ARM_M = 0.25;
/** Smallest on-screen size we draw the drone at, in pixels across. */
const MIN_PX = 34;
/** Never enlarge the drone more than this; past it the altitude reads wrong. */
const MAX_ENLARGE = 20;
/** Pose smoothing time constant (telemetry arrives at about 5 Hz). */
const SMOOTH_S = 0.15;

interface Pose {
  x: number;
  y: number;
  z: number;
  yaw: number; // radians, clockwise from north
  roll: number; // radians, right side down positive
  pitch: number; // radians, nose up positive
}

export class Flight3D implements CustomLayerInterface {
  readonly id = "flight-3d";
  readonly type = "custom" as const;
  readonly renderingMode = "3d" as const;

  private map!: MLMap;
  private renderer!: THREE.WebGLRenderer;
  private readonly camera = new THREE.Camera();
  private readonly scene = new THREE.Scene();
  private readonly drone = new THREE.Group();
  private readonly rotors: THREE.Object3D[] = [];
  private readonly dock = new THREE.Group();
  private readonly shadow: THREE.Mesh;
  private readonly plumb: THREE.Line;
  private readonly route = new THREE.Group();
  private readonly lineMats: LineMaterial[] = [];
  private trackLine: Line2 | null = null;
  private trackCurtain: THREE.Mesh | null = null;
  private readonly track: number[] = []; // x, y, z triples

  private target: Pose | null = null;
  private pose: Pose | null = null;
  private armed = false;
  private lastFrame = performance.now();
  private origin: maplibregl.MercatorCoordinate;
  private metersToMerc: number;

  /** Set when the chase camera is on. */
  follow = false;
  /** Google photorealistic tiles, once enabled; drawn only while `photorealOn`. */
  photoreal: Photoreal | null = null;
  photorealOn = false;

  /** How many times real size the drone is drawn at right now. */
  enlarge = 1;
  /** Called with the drone's smoothed position each frame (for the chase camera). */
  onPose?: (lon: number, lat: number, altM: number, headingDeg: number) => void;

  constructor(private readonly frame: LocalFrame) {
    this.origin = maplibregl.MercatorCoordinate.fromLngLat([frame.lon0, frame.lat0], 0);
    this.metersToMerc = this.origin.meterInMercatorCoordinateUnits();

    this.scene.add(new THREE.HemisphereLight(0xffffff, 0x445533, 2.2));
    const sun = new THREE.DirectionalLight(0xffffff, 1.6);
    sun.position.set(-40, -60, 120);
    this.scene.add(sun);

    this.buildDrone();
    this.buildDock();

    this.shadow = new THREE.Mesh(
      new THREE.CircleGeometry(ARM_M * 1.6, 32),
      new THREE.MeshBasicMaterial({ color: 0x000000, transparent: true, opacity: 0.35, depthWrite: false }),
    );
    this.shadow.position.z = 0.02;
    this.plumb = new THREE.Line(
      new THREE.BufferGeometry().setFromPoints([new THREE.Vector3(0, 0, 0), new THREE.Vector3(0, 0, 1)]),
      new THREE.LineDashedMaterial({ color: 0xffffff, dashSize: 0.6, gapSize: 0.4, transparent: true, opacity: 0.8 }),
    );
    this.scene.add(this.route, this.dock, this.shadow, this.plumb, this.drone);
    this.setDroneVisible(false);
  }

  // ------------------------------------------------------------ models
  private buildDrone() {
    const dark = new THREE.MeshStandardMaterial({ color: 0x1f2937, roughness: 0.6, metalness: 0.2 });
    const accent = new THREE.MeshStandardMaterial({ color: 0xf97316, roughness: 0.5 });
    const front = new THREE.MeshStandardMaterial({ color: 0x22d3ee, emissive: 0x0e7490, roughness: 0.4 });
    const rotorMat = new THREE.MeshStandardMaterial({ color: 0xe2e8f0, transparent: true, opacity: 0.55, side: THREE.DoubleSide });

    const body = new THREE.Mesh(new THREE.BoxGeometry(0.14, 0.2, 0.06), dark);
    body.position.z = 0.03;
    this.drone.add(body);
    // Nose marker so heading reads at a glance: cyan front, orange back.
    const nose = new THREE.Mesh(new THREE.ConeGeometry(0.04, 0.09, 12), front);
    nose.rotation.x = -Math.PI / 2;
    nose.position.set(0, 0.14, 0.03);
    this.drone.add(nose);

    // X layout: arms at 45 degrees, front pair cyan tipped.
    for (const [k, ang] of [45, 135, 225, 315].entries()) {
      const a = (ang * Math.PI) / 180;
      const dx = Math.sin(a) * ARM_M, dy = Math.cos(a) * ARM_M;
      const arm = new THREE.Mesh(new THREE.BoxGeometry(0.025, ARM_M, 0.02), dark);
      arm.position.set(dx / 2, dy / 2, 0.04);
      arm.rotation.z = -a;
      this.drone.add(arm);
      const motor = new THREE.Mesh(new THREE.CylinderGeometry(0.03, 0.03, 0.04, 16), k === 0 || k === 3 ? front : accent);
      motor.rotation.x = Math.PI / 2;
      motor.position.set(dx, dy, 0.05);
      this.drone.add(motor);
      const rotor = new THREE.Group();
      const disc = new THREE.Mesh(new THREE.CircleGeometry(0.12, 24), rotorMat);
      const blade = new THREE.Mesh(new THREE.BoxGeometry(0.24, 0.022, 0.004), dark);
      rotor.add(disc, blade);
      rotor.position.set(dx, dy, 0.075);
      rotor.userData.dir = k % 2 ? 1 : -1;
      this.rotors.push(rotor);
      this.drone.add(rotor);
    }
    // Landing skids.
    for (const sx of [-0.06, 0.06]) {
      const skid = new THREE.Mesh(new THREE.BoxGeometry(0.012, 0.22, 0.012), dark);
      skid.position.set(sx, 0, -0.03);
      this.drone.add(skid);
    }
  }

  /** The dock: a landing pad with the two centering bars that close on the drone's sides. */
  private buildDock() {
    const pad = new THREE.Mesh(
      new THREE.BoxGeometry(1.0, 1.0, 0.04),
      new THREE.MeshStandardMaterial({ color: 0x334155, roughness: 0.9 }),
    );
    pad.position.z = 0.02;
    const ring = new THREE.Mesh(
      new THREE.RingGeometry(0.32, 0.37, 40),
      new THREE.MeshBasicMaterial({ color: 0xfacc15 }),
    );
    ring.position.z = 0.045;
    const barMat = new THREE.MeshStandardMaterial({ color: 0xef4444, roughness: 0.5 });
    for (const sx of [-0.46, 0.46]) {
      const bar = new THREE.Mesh(new THREE.BoxGeometry(0.05, 0.9, 0.08), barMat);
      bar.position.set(sx, 0, 0.08);
      this.dock.add(bar);
    }
    this.dock.add(pad, ring);
  }

  // ------------------------------------------------------------ data in
  update(t: Telemetry) {
    const [x, y] = this.frame.toXY(t.lon, t.lat);
    const yaw = (t.heading_deg * Math.PI) / 180;
    const p: Pose = {
      x, y, z: Math.max(0, t.alt_m), yaw,
      roll: ((t.roll_deg ?? 0) * Math.PI) / 180,
      pitch: ((t.pitch_deg ?? 0) * Math.PI) / 180,
    };
    const flying = t.mode !== "DOCKED";
    if (flying && !this.armed) this.clearTrack();
    this.armed = flying;
    this.target = p;
    if (!this.pose || Math.hypot(p.x - this.pose.x, p.y - this.pose.y) > 60) this.pose = { ...p };
    this.setDroneVisible(true);
    if (flying) this.pushTrack(p);
    this.map?.triggerRepaint();
  }

  /** Planned route (dock -> passes -> dock) at survey altitude, colored per group. */
  setRoute(transit: [number, number][] | null, groups: { color: string; path: [number, number][] }[], altM: number) {
    for (const c of [...this.route.children]) {
      this.route.remove(c);
      disposeLine(c as Line2);
    }
    this.lineMats.length = 0;
    if (this.trackLine) this.lineMats.push(this.trackLine.material);
    if (transit && transit.length > 1) {
      const pts = transit.map(([lon, lat]) => this.frame.toXY(lon, lat));
      // Straight up from the dock, the route, straight down to the dock.
      const xyz = [pts[0][0], pts[0][1], 0, ...pts.flatMap(([x, y]) => [x, y, altM]), pts.at(-1)![0], pts.at(-1)![1], 0];
      this.route.add(this.makeLine(xyz, 0xffffff, 1.5, 0.55, true));
    }
    for (const g of groups) {
      if (g.path.length < 2) continue;
      const xyz = g.path.flatMap(([lon, lat]) => [...this.frame.toXY(lon, lat), altM]);
      this.route.add(this.makeLine(xyz, new THREE.Color(g.color).getHex(), 3, 0.95));
    }
    this.map?.triggerRepaint();
  }

  private makeLine(xyz: number[], color: number, widthPx: number, opacity: number, dashed = false): Line2 {
    const geom = new LineGeometry();
    geom.setPositions(xyz);
    const mat = new LineMaterial({
      color, linewidth: widthPx, transparent: opacity < 1, opacity, dashed,
      dashSize: 3, gapSize: 3, worldUnits: false,
    });
    if (this.map) mat.resolution.set(this.map.getCanvas().width, this.map.getCanvas().height);
    this.lineMats.push(mat);
    const line = new Line2(geom, mat);
    if (dashed) line.computeLineDistances();
    return line;
  }

  private clearTrack() {
    this.track.length = 0;
    if (this.trackLine) {
      this.scene.remove(this.trackLine);
      disposeLine(this.trackLine);
      this.lineMats.splice(this.lineMats.indexOf(this.trackLine.material), 1);
      this.trackLine = null;
    }
    if (this.trackCurtain) {
      this.scene.remove(this.trackCurtain);
      this.trackCurtain.geometry.dispose();
      this.trackCurtain = null;
    }
  }

  private pushTrack(p: Pose) {
    const n = this.track.length;
    if (n && Math.hypot(p.x - this.track[n - 3], p.y - this.track[n - 2], p.z - this.track[n - 1]) < 0.4) return;
    this.track.push(p.x, p.y, p.z);
    if (this.track.length < 6) return;
    if (!this.trackLine) {
      this.trackLine = this.makeLine(this.track, 0xffffff, 3.5, 1);
      this.scene.add(this.trackLine);
    } else {
      // LineGeometry can't grow in place; a few thousand points rebuild in well under a millisecond.
      this.trackLine.geometry.dispose();
      const g = new LineGeometry();
      g.setPositions(this.track);
      this.trackLine.geometry = g;
    }
    // A faint curtain from the track down to the ground makes the altitude readable.
    if (this.trackCurtain) {
      this.scene.remove(this.trackCurtain);
      this.trackCurtain.geometry.dispose();
    }
    this.trackCurtain = curtain(this.track);
    this.scene.add(this.trackCurtain);
  }

  private setDroneVisible(v: boolean) {
    this.drone.visible = this.shadow.visible = this.plumb.visible = v;
  }

  /** Start Google 3D tiles (opens one session). Call once, the first time a 3D view is shown. */
  enablePhotoreal(apiKey: string): Photoreal {
    if (!this.photoreal) {
      this.photoreal = new Photoreal(apiKey, this.frame.lat0, this.frame.lon0);
      this.photoreal.tiles.addEventListener("needs-update", () => this.map?.triggerRepaint());
      this.photoreal.tiles.addEventListener("load-model", () => this.map?.triggerRepaint());
      this.scene.add(this.photoreal.group);
    }
    return this.photoreal;
  }

  // ------------------------------------------------------------ MapLibre hooks
  onAdd(map: MLMap, gl: WebGLRenderingContext | WebGL2RenderingContext) {
    this.map = map;
    this.renderer = new THREE.WebGLRenderer({ canvas: map.getCanvas(), context: gl, antialias: true });
    this.renderer.autoClear = false;
  }

  onRemove() {
    this.renderer.dispose();
  }

  render(_gl: WebGLRenderingContext | WebGL2RenderingContext, args: maplibregl.CustomRenderMethodInput) {
    const now = performance.now();
    const dt = Math.min(0.1, (now - this.lastFrame) / 1000);
    this.lastFrame = now;
    let moving = false;

    // Enlarge the drone and dock so they stay visible from a golf-hole view.
    const mPerPx = 40075016.686 * Math.cos((this.frame.lat0 * Math.PI) / 180) / (512 * 2 ** this.map.getZoom());
    const enlarge = Math.min(MAX_ENLARGE, Math.max(1, (MIN_PX * mPerPx) / (2 * ARM_M)));
    this.drone.scale.setScalar(enlarge);
    this.enlarge = enlarge;
    this.dock.scale.setScalar(Math.max(1, enlarge * 0.6));

    if (this.target && this.pose) {
      const a = 1 - Math.exp(-dt / SMOOTH_S);
      const p = this.pose, t = this.target;
      p.x += (t.x - p.x) * a;
      p.y += (t.y - p.y) * a;
      p.z += (t.z - p.z) * a;
      p.yaw += wrapPi(t.yaw - p.yaw) * a;
      p.roll += (t.roll - p.roll) * a;
      p.pitch += (t.pitch - p.pitch) * a;
      moving = Math.abs(t.x - p.x) + Math.abs(t.y - p.y) + Math.abs(t.z - p.z) > 0.02;

      // Body frame: x right, y forward, z up. Yaw about z (clockwise from
      // north, so negative), then pitch about the body's x, then roll about y.
      this.drone.position.set(p.x, p.y, p.z + 0.03 * enlarge);
      this.drone.rotation.set(p.pitch, p.roll, -p.yaw, "ZXY");
      this.shadow.position.set(p.x, p.y, 0.02);
      this.shadow.scale.setScalar(enlarge * Math.max(0.4, 1 - p.z / 60));
      this.plumb.position.set(p.x, p.y, 0);
      this.plumb.scale.set(1, 1, Math.max(0.01, p.z));
      this.plumb.computeLineDistances();
      this.plumb.visible = this.drone.visible && p.z > 0.5;
      for (const r of this.rotors) if (this.armed) r.rotation.z += r.userData.dir * dt * 60;
      if (this.follow && this.onPose) {
        const [lon, lat] = this.frame.toLonLat(p.x, p.y);
        this.onPose(lon, lat, p.z, (p.yaw * 180) / Math.PI);
      }
    }

    const canvas = this.map.getCanvas();
    for (const m of this.lineMats) m.resolution.set(canvas.width, canvas.height);

    const s = this.metersToMerc;
    const model = new THREE.Matrix4()
      .makeTranslation(this.origin.x, this.origin.y, this.origin.z ?? 0)
      .scale(new THREE.Vector3(s, -s, s));
    const main = new THREE.Matrix4().fromArray(args.defaultProjectionData.mainMatrix as unknown as number[]);
    this.camera.projectionMatrix = main.clone().multiply(model);

    const pr = this.photoreal;
    if (pr) {
      pr.group.visible = this.photorealOn && pr.status !== "failed";
      if (pr.group.visible) {
        // Local meters -> MapLibre's view space: P^-1 * (P * V) * model.
        const proj = new THREE.Matrix4().fromArray(args.projectionMatrix as unknown as number[]);
        const localView = proj.invert().multiply(this.camera.projectionMatrix);
        pr.update(localView, args.fov, canvas.width, canvas.height);
      }
    }
    this.renderer.resetState();
    this.renderer.render(this.scene, this.camera);
    if (this.armed || moving) this.map.triggerRepaint();
  }
}

function wrapPi(a: number) {
  return Math.atan2(Math.sin(a), Math.cos(a));
}

function disposeLine(l: Line2) {
  l.geometry.dispose();
  l.material.dispose();
}

/** Vertical strip from each track point down to the ground. */
function curtain(xyz: number[]): THREE.Mesh {
  const n = xyz.length / 3;
  const pos = new Float32Array(n * 2 * 3);
  for (let i = 0; i < n; i++) {
    const [x, y, z] = [xyz[3 * i], xyz[3 * i + 1], xyz[3 * i + 2]];
    pos.set([x, y, z, x, y, 0], 6 * i);
  }
  const idx: number[] = [];
  for (let i = 0; i < n - 1; i++) {
    const a = 2 * i, b = a + 1, c = a + 2, d = a + 3;
    idx.push(a, b, c, b, d, c);
  }
  const g = new THREE.BufferGeometry();
  g.setAttribute("position", new THREE.BufferAttribute(pos, 3));
  g.setIndex(idx);
  return new THREE.Mesh(g, CURTAIN_MAT);
}

const CURTAIN_MAT = new THREE.MeshBasicMaterial({ color: 0xffffff, transparent: true, opacity: 0.12, side: THREE.DoubleSide, depthWrite: false });
