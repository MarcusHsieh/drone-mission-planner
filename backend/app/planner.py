"""Grouping and path preview.

Groups: 4-connected components of the selected cells. Group numbers stay
stable across edits: a new component takes the number of the old group it
overlaps most, and brand-new components take the smallest free number.

Preview: coverage path planning per group. The group is split into
boustrophedon pieces (so passes never fly across gaps in a curved or dogleg
fairway), the sweep angle is searched, and pieces are ordered with
nearest-neighbor plus 2-opt. The route over groups uses the same tour
planner, allowing each group's path to be flown in either direction. M1 has
no sorties or battery math; the numbers are estimates for the stats panel.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import shapely
from shapely import affinity
from shapely.geometry import LineString, box
from shapely.geometry.base import BaseGeometry
from shapely.ops import unary_union

from .geo import CELL_M, Cell

# Planning assumptions (placeholders until the camera and airframe are chosen).
ALTITUDE_M = 15.0
SIDELAP = 0.70
CAMERA_HFOV_DEG = 80.0
CRUISE_SPEED_MS = 5.0
TURN_PENALTY_S = 3.0


def pass_spacing_m(altitude_m: float = ALTITUDE_M, sidelap: float = SIDELAP) -> float:
    footprint = 2 * altitude_m * math.tan(math.radians(CAMERA_HFOV_DEG / 2))
    return footprint * (1 - sidelap)


def components(cells: set[Cell]) -> list[set[Cell]]:
    seen: set[Cell] = set()
    out: list[set[Cell]] = []
    for start in cells:
        if start in seen:
            continue
        comp = {start}
        stack = [start]
        seen.add(start)
        while stack:
            i, j = stack.pop()
            for n in ((i + 1, j), (i - 1, j), (i, j + 1), (i, j - 1)):
                if n in cells and n not in seen:
                    seen.add(n)
                    comp.add(n)
                    stack.append(n)
        out.append(comp)
    return out


def regroup(cells: set[Cell], previous: dict[int, frozenset[Cell]]) -> dict[int, frozenset[Cell]]:
    comps = sorted(components(cells), key=len, reverse=True)
    result: dict[int, frozenset[Cell]] = {}
    claims = []
    for idx, comp in enumerate(comps):
        for gid, old in previous.items():
            overlap = len(comp & old)
            if overlap:
                claims.append((overlap, idx, gid))
    claims.sort(reverse=True)
    taken_ids: set[int] = set()
    assigned: dict[int, int] = {}
    for _, idx, gid in claims:
        if idx in assigned or gid in taken_ids:
            continue
        assigned[idx] = gid
        taken_ids.add(gid)
    for idx, comp in enumerate(comps):
        gid = assigned.get(idx)
        if gid is None:
            gid = 1
            while gid in taken_ids:
                gid += 1
            taken_ids.add(gid)
        result[gid] = frozenset(comp)
    return dict(sorted(result.items()))


def cells_polygon(cells: frozenset[Cell] | set[Cell]) -> BaseGeometry:
    return unary_union([box(i * CELL_M, j * CELL_M, (i + 1) * CELL_M, (j + 1) * CELL_M) for i, j in cells])


@dataclass
class Preview:
    waypoints: list[tuple[float, float]]  # local meters
    length_m: float
    passes: int  # straight passes flown
    heading_deg: float
    est_time_s: float
    pieces: int = 1  # sweep pieces the group was split into


Pt = tuple[float, float]
TURN_PENALTY_M = TURN_PENALTY_S * CRUISE_SPEED_MS
MAX_BRIDGE_M = 2 * CELL_M  # gaps this small are flown straight over
SWEEP_ANGLES = [float(a) for a in range(0, 180, 5)]
FINALISTS = 5  # angles planned in full after the quick ranking


def _long_axis_angle(poly: BaseGeometry) -> float:
    rect = poly.minimum_rotated_rectangle
    if rect.geom_type != "Polygon":
        return 0.0
    xs, ys = rect.exterior.coords.xy
    edges = [(xs[k + 1] - xs[k], ys[k + 1] - ys[k]) for k in range(2)]
    dx, dy = max(edges, key=lambda e: math.hypot(*e))
    return math.degrees(math.atan2(dy, dx)) % 180


def _dist(a: Pt, b: Pt) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def _path_len(pts: list[Pt]) -> float:
    return sum(_dist(pts[k], pts[k + 1]) for k in range(len(pts) - 1))


def _scan_rows(flat: BaseGeometry, spacing: float) -> list[tuple[float, list[tuple[float, float]]]]:
    """Pass rows through a polygon: [(y, [(x0, x1), ...]), ...].

    A row's center line can dip in and out of a ragged edge. Hits that belong
    to the same connected strip of the row's band (the ground within half a
    spacing of the line) become one pass, so the row only splits where the
    shape really has a gap.
    """
    minx, miny, maxx, maxy = flat.bounds
    n = max(int(math.ceil((maxy - miny) / spacing)), 1)
    y0 = miny + ((maxy - miny) - (n - 1) * spacing) / 2  # center the rows
    ys = [y0 + k * spacing for k in range(n)]
    half = spacing / 2
    bands = shapely.intersection(flat, [box(minx - 1, y - half, maxx + 1, y + half) for y in ys])
    lines = shapely.intersection(flat, [LineString([(minx - 1, y), (maxx + 1, y)]) for y in ys])
    rows = []
    for y, band, line in zip(ys, bands, lines):
        hit_x = [(g.bounds[0], g.bounds[2]) for g in shapely.get_parts(line) if g.geom_type == "LineString"]
        spans = []
        for g in shapely.get_parts(band):
            if g.geom_type != "Polygon" or g.area < 1.0:
                continue
            bx0, _, bx1, _ = g.bounds
            inside = [(a, b) for a, b in hit_x if a < bx1 and b > bx0]
            if inside:  # one pass per connected strip of the band
                spans.append((min(a for a, _ in inside), max(b for _, b in inside)))
        segs: list[tuple[float, float]] = []
        for a, b in sorted(spans):
            # Bridge small notches and holes rather than splitting the pass.
            if segs and a - segs[-1][1] <= MAX_BRIDGE_M:
                segs[-1] = (segs[-1][0], max(segs[-1][1], b))
            else:
                segs.append((a, b))
        if segs:
            rows.append((y, segs))
    return rows


def _decompose(rows: list[tuple[float, list[tuple[float, float]]]]) -> list[list[tuple[float, float, float]]]:
    """Boustrophedon cell decomposition on scanlines.

    A piece grows row by row while exactly one segment in the next row overlaps
    it and that segment overlaps nothing else. A split, merge, or gap (where a
    pass line would leave and re-enter the shape) closes the piece, so each
    piece is covered by back-and-forth passes that never cross empty ground.
    Returns pieces as lists of (y, x0, x1).
    """
    done: list[list[tuple[float, float, float]]] = []
    active: list[list[tuple[float, float, float]]] = []
    for y, segs in rows:
        links = [[s for s, (a, b) in enumerate(segs) if min(p[-1][2], b) - max(p[-1][1], a) > 0] for p in active]
        seg_hits = [0] * len(segs)
        for ls in links:
            for s in ls:
                seg_hits[s] += 1
        nxt: list[list[tuple[float, float, float]]] = []
        used = set()
        for p, ls in zip(active, links):
            if len(ls) == 1 and seg_hits[ls[0]] == 1:
                a, b = segs[ls[0]]
                p.append((y, a, b))
                nxt.append(p)
                used.add(ls[0])
            else:
                done.append(p)
        for s, (a, b) in enumerate(segs):
            if s not in used:
                nxt.append([(y, a, b)])
        active = nxt
    return done + active


def _piece_paths(piece: list[tuple[float, float, float]]) -> list[list[Pt]]:
    """The four ways to fly one piece: start left or right, from either end."""
    out = []
    for flip in (False, True):
        pts: list[Pt] = []
        for k, (y, a, b) in enumerate(piece):
            ltr = (k % 2 == 0) != flip
            pts.extend([(a, y), (b, y)] if ltr else [(b, y), (a, y)])
        out.append(pts)
        out.append(pts[::-1])
    return out


def _chain_cost(order: list[int], cands: list[list[list[Pt]]], home: Pt | None) -> tuple[float, list[int]]:
    """Best variant per item for a fixed order (dynamic programming)."""
    first = cands[order[0]]
    cost = [(_dist(home, c[0]) if home else 0.0) + _path_len(c) for c in first]
    back: list[list[int]] = []
    for prev_i, i in zip(order, order[1:]):
        prev, cur = cands[prev_i], cands[i]
        new, arg = [], []
        for c in cur:
            k = min(range(len(prev)), key=lambda k: cost[k] + _dist(prev[k][-1], c[0]))
            new.append(cost[k] + _dist(prev[k][-1], c[0]) + _path_len(c))
            arg.append(k)
        cost = new
        back.append(arg)
    last = cands[order[-1]]
    if home:
        cost = [cost[k] + _dist(last[k][-1], home) for k in range(len(last))]
    k = min(range(len(cost)), key=cost.__getitem__)
    best = cost[k]
    picks = [k]
    for arg in reversed(back):
        k = arg[k]
        picks.append(k)
    return best, picks[::-1]


def plan_tour(cands: list[list[list[Pt]]], home: Pt | None = None) -> tuple[list[int], list[int], float]:
    """Order items and pick one candidate path for each, to minimize length.

    Each item offers candidate paths (for example the same sweep flown from
    different corners). Nearest-neighbor seeds the order, 2-opt improves it,
    and the variant choice for an order is exact. Open path when home is None,
    otherwise home -> items -> home. Returns (order, variant per item, cost).
    """
    n = len(cands)
    if n == 0:
        return [], [], 0.0

    def nn(start: int | None) -> list[int]:
        left = set(range(n))
        here = home
        order = []
        if start is not None:
            order.append(start)
            left.discard(start)
            here = min(cands[start], key=_path_len)[-1]
        while left:
            i, c = min(((i, c) for i in left for c in cands[i]), key=lambda ic: _dist(here, ic[1][0]) if here else 0.0)
            order.append(i)
            left.discard(i)
            here = c[-1]
        return order

    seeds = [nn(None)] if home else [nn(s) for s in range(n)]
    best_cost, best_order = math.inf, seeds[0]
    for order in seeds:
        c, _ = _chain_cost(order, cands, home)
        if c < best_cost:
            best_cost, best_order = c, order
    order = best_order
    improved = n > 2 or (home is None and n > 1)
    rounds = 0
    while improved and rounds < 20:
        improved, rounds = False, rounds + 1
        for i in range(n - 1):
            for j in range(i + 1, n):
                cand = order[:i] + order[i : j + 1][::-1] + order[j + 1 :]
                c, _ = _chain_cost(cand, cands, home)
                if c < best_cost - 1e-6:
                    best_cost, order, improved = c, cand, True
    _, picks = _chain_cost(order, cands, home)
    return order, picks, best_cost


def _quick_cost(pieces: list[list[tuple[float, float, float]]]) -> float:
    """Cheap score for the angle search: pass length, turns, and NN hops."""
    paths = [_piece_paths(p) for p in pieces]
    total = sum(_path_len(c[0]) for c in paths)
    turns = sum(len(p) for p in pieces) - 1
    left = list(range(1, len(paths)))
    here = paths[0][0][-1]
    while left:
        i, c = min(((i, c) for i in left for c in paths[i]), key=lambda ic: _dist(here, ic[1][0]))
        total += _dist(here, c[0])
        here = c[-1]
        left.remove(i)
    return total + turns * TURN_PENALTY_M


def preview_path(cells: frozenset[Cell] | set[Cell], spacing_m: float | None = None) -> Preview:
    """Coverage path for one group.

    Tries sweep angles every 5 degrees plus the group's long axis, and splits
    the group into boustrophedon pieces at each angle. A quick score ranks the
    angles; the best few (always including the long axis, the old default)
    are planned in full with plan_tour, and the cheapest by length plus turns
    wins.
    """
    spacing = spacing_m or pass_spacing_m()
    poly = cells_polygon(cells)
    origin = poly.centroid
    long_axis = round(_long_axis_angle(poly), 2)
    scored = []
    for angle in sorted(set(SWEEP_ANGLES + [long_axis])):
        flat = affinity.rotate(poly, -angle, origin=origin)
        rows = _scan_rows(flat, spacing)
        if rows:
            pieces = _decompose(rows)
            scored.append((_quick_cost(pieces), angle, pieces))
    if not scored:  # tiny group: fly over its center
        return Preview([(round(origin.x, 2), round(origin.y, 2))], 0.0, 0, 0.0, 0.0, 0)
    # The quick score only ranks angles; plan the best few properly and keep the cheapest.
    best = None
    ranked = sorted(scored, key=lambda t: t[0])
    finalists = ranked[:FINALISTS] + [t for t in ranked[FINALISTS:] if t[1] == long_axis]
    for _, angle, pieces in finalists:
        cands = [_piece_paths(p) for p in pieces]
        order, picks, cost = plan_tour(cands)
        cost += (sum(len(p) for p in pieces) - 1) * TURN_PENALTY_M
        if best is None or cost < best[0] - 1e-6:
            best = (cost, angle, pieces, cands, order, picks)
    _, angle, pieces, cands, order, picks = best
    flat_path: list[Pt] = []
    for i, k in zip(order, picks):
        flat_path.extend(cands[i][k])
    passes = sum(len(p) for p in pieces)
    if len(flat_path) > 1:
        line = affinity.rotate(LineString(flat_path), angle, origin=origin)
        path = list(line.coords)
        length = line.length
    else:
        path, length = [(origin.x, origin.y)], 0.0
    return Preview(
        waypoints=[(round(x, 2), round(y, 2)) for x, y in path],
        length_m=length,
        passes=passes,
        heading_deg=(90 - angle) % 180,  # compass bearing of the passes
        est_time_s=length / CRUISE_SPEED_MS + max(passes - 1, 0) * TURN_PENALTY_S,
        pieces=len(pieces),
    )
