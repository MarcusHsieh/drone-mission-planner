// Must match backend/app/geo.py exactly: meters east/north of the dock,
// equirectangular around the dock, 3 m cells with the dock at a cell corner.
export const EARTH_RADIUS_M = 6378137.0;

export class LocalFrame {
  readonly kx: number;
  readonly ky: number;
  constructor(readonly lat0: number, readonly lon0: number, readonly cellM: number) {
    this.kx = (EARTH_RADIUS_M * Math.cos((lat0 * Math.PI) / 180) * Math.PI) / 180;
    this.ky = (EARTH_RADIUS_M * Math.PI) / 180;
  }
  toXY(lon: number, lat: number): [number, number] {
    return [(lon - this.lon0) * this.kx, (lat - this.lat0) * this.ky];
  }
  toLonLat(x: number, y: number): [number, number] {
    return [this.lon0 + x / this.kx, this.lat0 + y / this.ky];
  }
  cellOf(lon: number, lat: number): [number, number] {
    const [x, y] = this.toXY(lon, lat);
    return [Math.floor(x / this.cellM), Math.floor(y / this.cellM)];
  }
  cellRing(i: number, j: number): [number, number][] {
    const c = this.cellM;
    const a = this.toLonLat(i * c, j * c);
    const b = this.toLonLat((i + 1) * c, (j + 1) * c);
    return [a, [b[0], a[1]], b, [a[0], b[1]], a];
  }
}
