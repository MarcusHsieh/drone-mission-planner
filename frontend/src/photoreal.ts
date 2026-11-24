// Google Photorealistic 3D Tiles under the flight, in the same three.js scene
// as the drone (so trees and houses hide the drone and the route correctly).
//
// Cost: Google bills a session (one root tileset request, good for 3 hours of
// tile requests), not each tile. We open one session per page load, and only
// the first time the 3D or Chase view is opened; 2D never contacts Google.
// Google's terms allow only the browser's own HTTP cache, so there is no
// other caching here.
//
// The tiles are in ECEF (earth-centered) meters. We place them in the dock
// frame (x east, y north, z up) with the dock's ground at z = 0, by measuring
// the mesh's ground height under the dock once tiles have loaded.

import * as THREE from "three";
import { DRACOLoader } from "three/examples/jsm/loaders/DRACOLoader.js";
import { TilesRenderer, WGS84_ELLIPSOID } from "3d-tiles-renderer";
import { GLTFExtensionsPlugin, GoogleCloudAuthPlugin, TileCompressionPlugin, TilesFadePlugin } from "3d-tiles-renderer/plugins";

export type PhotorealStatus = "loading" | "ready" | "failed";

/** Ground at the dock, meters above the WGS84 ellipsoid: 140 m above sea level, geoid about -32 m here. Refined from the mesh. */
const DOCK_ELLIPSOID_HEIGHT_M = 108;
const DEG = Math.PI / 180;

export class Photoreal {
  readonly tiles: TilesRenderer;
  readonly group: THREE.Group;
  status: PhotorealStatus = "loading";
  error = "";
  onStatus?: (s: PhotorealStatus) => void;

  private groundH = DOCK_ELLIPSOID_HEIGHT_M;
  private groundFixes = 0;
  private readonly lodCamera = new THREE.PerspectiveCamera();
  private readonly raycaster = new THREE.Raycaster();

  constructor(apiKey: string, private readonly lat0: number, private readonly lon0: number) {
    const tiles = new TilesRenderer();
    tiles.registerPlugin(new GoogleCloudAuthPlugin({ apiToken: apiKey, autoRefreshToken: true }));
    tiles.registerPlugin(new TileCompressionPlugin());
    tiles.registerPlugin(new TilesFadePlugin());
    const draco = new DRACOLoader().setDecoderPath("/draco/");
    tiles.registerPlugin(new GLTFExtensionsPlugin({ dracoLoader: draco }));
    // Keep the download set small: the demo stays around one golf course.
    tiles.lruCache.maxSize = 600;
    this.tiles = tiles;
    this.group = tiles.group as unknown as THREE.Group;
    this.group.matrixAutoUpdate = false;
    this.placeGround();

    tiles.addEventListener("load-error", (e: { tile: unknown; error: Error }) => {
      // A missing child tile is normal; only a failed root (bad key, quota, no network) is fatal.
      if (e.tile && this.status !== "loading") return;
      if (this.status === "ready") return;
      this.fail(e.error?.message || String(e.error));
    });
    tiles.addEventListener("tiles-load-end", () => {
      if (this.status === "failed") return;
      this.fixGround();
      if (this.status === "loading" && tiles.visibleTiles.size > 0) this.setStatus("ready");
    });
  }

  private fail(msg: string) {
    this.error = /error code 40[03]|403|PERMISSION|API key|denied/i.test(msg)
      ? "Google rejected the API key (check that the Map Tiles API is enabled and the key allows this address)."
      : /429|quota|RESOURCE_EXHAUSTED/i.test(msg)
        ? "Google's daily quota for 3D tiles is used up."
        : `Couldn't load Google 3D tiles: ${msg}`;
    this.group.visible = false;
    this.setStatus("failed");
  }

  private setStatus(s: PhotorealStatus) {
    this.status = s;
    this.onStatus?.(s);
  }

  /** ECEF -> dock frame, with the dock's ground (height groundH on the ellipsoid) at z = 0. */
  private placeGround() {
    const enu = WGS84_ELLIPSOID.getEastNorthUpFrame(this.lat0 * DEG, this.lon0 * DEG, this.groundH, new THREE.Matrix4());
    this.group.matrix.copy(enu.invert());
    this.group.updateMatrixWorld(true);
  }

  /** Measure where the mesh's ground is under the dock and move it to z = 0 (a few times, as finer tiles arrive). */
  private fixGround() {
    if (this.groundFixes >= 6) return;
    this.raycaster.set(new THREE.Vector3(0, 0, 300), new THREE.Vector3(0, 0, -1));
    const hit = this.raycaster.intersectObject(this.group, true)[0];
    if (!hit) return;
    const dz = hit.point.z;
    if (Math.abs(dz) > 80) return; // not the ground near the dock
    this.groundFixes++;
    if (Math.abs(dz) > 0.05) {
      this.groundH += dz;
      this.placeGround();
    }
  }

  /**
   * Choose and load tiles for the map's current camera. MapLibre gives one
   * combined matrix; the tiles renderer needs a real camera in meters for
   * level of detail, so rebuild one from the same view.
   */
  update(localView: THREE.Matrix4, fovRad: number, width: number, height: number) {
    if (this.status === "failed") return;
    const cam = lodCamera(localView, fovRad, width, height, this.lodCamera);
    this.tiles.setCamera(cam);
    this.tiles.setResolution(cam, width, height);
    this.tiles.update();
  }

  /** Height of the mesh (ground, roof or tree top) at x, y in the dock frame, or null where nothing has loaded. */
  groundAt(x: number, y: number): number | null {
    if (this.status !== "ready") return null;
    this.raycaster.set(new THREE.Vector3(x, y, 300), new THREE.Vector3(0, 0, -1));
    const hit = this.raycaster.intersectObject(this.group, true)[0];
    return hit && Math.abs(hit.point.z) < 80 ? hit.point.z : null;
  }

  /** Attribution text Google requires for the tiles on screen. */
  attribution(): string {
    const out: { type: string; value: string }[] = [];
    this.tiles.getAttributions(out);
    return out.filter((a) => a.type === "string").map((a) => a.value).join("; ");
  }

  dispose() {
    this.tiles.dispose();
  }
}

/** A perspective camera in dock-frame meters that sees what MapLibre's camera sees (for level of detail and culling). */
export function lodCamera(localView: THREE.Matrix4, fovRad: number, width: number, height: number, cam = new THREE.PerspectiveCamera()) {
    const inv = localView.clone().invert();
    const pos = new THREE.Vector3(0, 0, 0).applyMatrix4(inv);
    const ahead = new THREE.Vector3(0, 0, -1).applyMatrix4(inv);
    const up = new THREE.Vector3(0, 1, 0).applyMatrix4(inv).sub(pos).normalize();
    cam.position.copy(pos);
    cam.up.copy(up);
    cam.lookAt(ahead);
    cam.fov = fovRad / DEG;
    cam.aspect = width / height;
    cam.near = 1;
    cam.far = 60000;
    cam.updateProjectionMatrix();
    cam.updateMatrixWorld(true);
  return cam;
}
