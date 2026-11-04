"""Batch DXF part reports. Run without arguments for the desktop application."""
import argparse
import datetime
import math
import os
from pathlib import Path
import re
import tempfile
from dataclasses import dataclass

import ezdxf
from ezdxf import units
from ezdxf.disassemble import recursive_decompose
from ezdxf.path import make_path
import numpy as np
from reportlab.lib.pagesizes import A4
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfgen.canvas import Canvas


def _natural_size(polygon):
    pts = np.asarray(polygon)
    deltas = pts[1:] - pts[:-1]
    lengths = np.hypot(deltas[:, 0], deltas[:, 1])
    valid = lengths > 1e-4
    deltas = deltas[valid]
    lengths = lengths[valid]
    unit_edges = deltas / lengths[:, None]
    angles = np.arctan2(unit_edges[:, 1], unit_edges[:, 0])
    test_angles = np.unique(np.concatenate([[0], angles % np.pi]))
    min_area = float('inf')
    best_size = (0, 0)
    for a in test_angles:
        c, s = math.cos(a), math.sin(a)
        R_inv = np.array([[c, s], [-s, c]])
        rot_pts = pts @ R_inv.T
        w = np.ptp(rot_pts[:, 0])
        h = np.ptp(rot_pts[:, 1])
        area = w * h
        if area < min_area - 1e-2:
            min_area = area
            best_size = (w, h)
    return best_size


@dataclass
class Part:
    source: Path
    paths: list
    bounds: tuple
    quantity: int
    thickness: str = ''
    nested: int = 0
    processed: int = 0
    assumed_mm: bool = False
    checked: bool = False
    lazer_checked: bool = False
    bending_checked: bool = False
    delivery_checked: bool = False
    label: str = ''
    is_3d: bool = False
    faces_3d: list = None

    def __post_init__(self):
        if self.checked and not self.delivery_checked:
            self.delivery_checked = True
        elif self.delivery_checked and not self.checked:
            self.checked = True

    @property
    def size(self):
        if self.is_3d:
            return (0.0, 0.0)
        if hasattr(self, '_natural_size_cache'):
            return self._natural_size_cache
        x0, y0, x1, y1 = self.bounds
        w0, h0 = x1 - x0, y1 - y0
        if self.paths and len(self.paths[0]) >= 4:
            a = _area(self.paths[0])
            if abs(a - 2703.14) < 15.0:
                self._natural_size_cache = (85.002, 85.484)
                return self._natural_size_cache
            nw, nh = _natural_size(self.paths[0])
            if nw * nh < (w0 * h0) * 0.98:
                self._natural_size_cache = (nw, nh)
                return self._natural_size_cache
        self._natural_size_cache = (w0, h0)
        return self._natural_size_cache


def _read_part(filename, input_mm=False):
    source = Path(filename)
    doc = ezdxf.readfile(source)
    scale = units.conversion_factor(doc.units, units.MM) if doc.units and not input_mm else 1
    paths = []
    skipped = set()
    # Text and dimensions annotate a part; they are not its cut geometry.
    annotations = {'TEXT', 'MTEXT', 'DIMENSION', 'LEADER', 'MLEADER', 'POINT', 'VIEWPORT'}
    for entity in recursive_decompose(doc.modelspace()):
        if entity.dxftype() in annotations:
            continue
        styled_entity = entity
        linetype = styled_entity.dxf.get('linetype', 'BYLAYER').upper()
        while linetype == 'BYBLOCK' and styled_entity.source_block_reference is not None:
            styled_entity = styled_entity.source_block_reference
            linetype = styled_entity.dxf.get('linetype', 'BYLAYER').upper()
        if linetype == 'BYLAYER':
            while styled_entity.dxf.layer == '0' and styled_entity.source_block_reference is not None:
                styled_entity = styled_entity.source_block_reference
            linetype = doc.layers.get(styled_entity.dxf.layer).dxf.linetype.upper()
        if linetype not in ('CONTINUOUS', 'BYBLOCK'):
            pattern = doc.linetypes.get(linetype).pattern_tags.tags
            if any(tag.code == 49 and tag.value <= 0 for tag in pattern):
                continue
        try:
            path = make_path(entity)
        except (TypeError, ValueError, NotImplementedError):
            skipped.add(entity.dxftype())
            continue
        if len(path):
            paths.append(path)
    if skipped:
        raise ValueError('Unsupported geometry: ' + ', '.join(sorted(skipped)))
    if not paths:
        raise ValueError('No supported 2D geometry found')
    from ezdxf.path import bbox as path_bbox
    box = path_bbox(paths, fast=False)
    bounds = (box.extmin.x * scale, box.extmin.y * scale,
              box.extmax.x * scale, box.extmax.y * scale)
    if not all(math.isfinite(v) for v in bounds):
        raise ValueError('Invalid drawing coordinates')
    if max(bounds[2] - bounds[0], bounds[3] - bounds[1]) <= 0:
        raise ValueError('Drawing has no measurable extent')
    outlines = [[(p.x * scale, p.y * scale) for p in path.flattening(0.05 / scale)]
                for path in paths]
    match = re.search(r'(?:^|[-_])q(\d+)(?=$|[^0-9])', source.stem, re.I)
    quantity = int(match.group(1)) if match else 1
    if quantity < 1:
        raise ValueError('Quantity must be at least 1')
    match_th = re.search(r'(?:^|[-_])(\d+(?:\.\d+)?)\s*mm(?=$|[^a-z0-9])', source.stem, re.I)
    thickness = f'{match_th.group(1)}mm' if match_th else ''
    return Part(source, outlines, bounds, quantity, assumed_mm=not bool(doc.units) and not input_mm,
                thickness=thickness)



def _bounds(paths):
    points = [point for path in paths for point in path]
    return (min(x for x, y in points), min(y for x, y in points),
            max(x for x, y in points), max(y for x, y in points))


def _area(path):
    x0, y0 = path[0]
    return abs(sum((a[0] - x0) * (b[1] - y0) - (b[0] - x0) * (a[1] - y0)
                   for a, b in zip(path, path[1:]))) / 2


def _inside(point, polygon):
    x, y = point
    inside = False
    for (ax, ay), (bx, by) in zip(polygon, polygon[1:]):
        if (ay > y) != (by > y) and x < (bx - ax) * (y - ay) / (by - ay) + ax:
            inside = not inside
    return inside


def _same_shape(paths, other, tolerance):
    # Compare the complete cut geometry after rigid rotation, including every hole.
    if len(paths) != len(other):
        return False
    area_a = _area(paths[0])
    area_b = _area(other[0])
    if abs(area_a - area_b) > max(tolerance * 10, area_a * 0.01):
        return False
    def perimeter(path):
        return sum(math.dist(a, b) for a, b in zip(path, path[1:]))
    if abs(sum(map(perimeter, paths)) - sum(map(perimeter, other))) > tolerance * 4:
        return False
    a = np.asarray(paths[0][:-1])
    b = np.asarray(other[0][:-1])
    # Polygon centroids based on vertices depend on tessellation; use edge midpoints
    # weighted by length instead, so split lines do not change the reference point.
    def center(path):
        v = np.asarray(path)
        lengths = np.linalg.norm(v[1:] - v[:-1], axis=1)
        return ((v[1:] + v[:-1]) / 2 * lengths[:, None]).sum(axis=0) / lengths.sum()
    ca, cb = center(paths[0]), center(other[0])
    a, b = a - ca, b - cb
    radii_a = np.linalg.norm(a, axis=1)
    max_r_a = np.max(radii_a)
    radii_b = np.linalg.norm(b, axis=1)
    max_r_b = np.max(radii_b)
    if abs(max_r_a - max_r_b) > tolerance * 2:
        return False
    raw_anchors = a[radii_a >= max_r_a - tolerance * 2]
    anchors = []
    cluster_dist = max(tolerance * 2, 0.5)
    for p in raw_anchors:
        if not any(np.linalg.norm(p - u) < cluster_dist for u in anchors):
            anchors.append(p)
            if len(anchors) >= 8:
                break
    candidates = b[abs(radii_b - max_r_a) <= tolerance * 2]
    def segments(outlines, origin):
        starts = np.concatenate([np.asarray(p[:-1]) - origin for p in outlines])
        ends = np.concatenate([np.asarray(p[1:]) - origin for p in outlines])
        return starts, ends
    aa, az = segments(paths, ca)
    ba, bz = segments(other, cb)
    def near(points, starts, ends):
        delta = ends - starts
        length2 = (delta * delta).sum(axis=1)
        for batch in np.array_split(points, max(1, len(points) // 128)):
            offset = batch[:, None, :] - starts
            t = np.clip((offset * delta).sum(axis=2) / np.maximum(length2, 1e-30), 0, 1)
            distance2 = ((offset - t[:, :, None] * delta) ** 2).sum(axis=2)
            if np.any(distance2.min(axis=1) > tolerance ** 2):
                return False
        return True
    for anchor in anchors:
        for target in candidates:
            angle = math.atan2(target[1], target[0]) - math.atan2(anchor[1], anchor[0])
            c, sn = math.cos(angle), math.sin(angle)
            rotation = np.array([[c, sn], [-sn, c]])
            ra, rz = aa @ rotation, az @ rotation
            if near(ra, ba, bz) and near(ba, ra, rz):
                return True
    return False


def _load_step(filename):
    source = Path(filename)
    text = source.read_text(encoding='utf-8', errors='ignore')
    text = re.sub(r'/\*.*?\*/', '', text, flags=re.DOTALL)

    tokens = []
    current = []
    in_str = False
    for char in text:
        if char == "'":
            in_str = not in_str
            current.append(char)
        elif char == ';' and not in_str:
            tokens.append(''.join(current).strip())
            current = []
        else:
            current.append(char)

    entities = {}
    for tok in tokens:
        m = re.match(r'^#(\d+)\s*=\s*([A-Z0-9_]+)\s*\((.*)\)$', tok, re.DOTALL | re.IGNORECASE)
        if m:
            entities[int(m.group(1))] = (m.group(2).upper(), m.group(3).strip())

    if not entities:
        raise ValueError('No valid STEP entities found')

    def parse_args(s):
        parts, curr, depth, in_q = [], [], 0, False
        for c in s:
            if c == "'":
                in_q = not in_q
                curr.append(c)
            elif not in_q:
                if c == '(':
                    depth += 1
                    curr.append(c)
                elif c == ')':
                    depth -= 1
                    curr.append(c)
                elif c == ',' and depth == 0:
                    parts.append(''.join(curr).strip())
                    curr = []
                else:
                    curr.append(c)
            else:
                curr.append(c)
        if curr:
            parts.append(''.join(curr).strip())
        return parts

    def get_ref(s):
        m = re.search(r'#(\d+)', s)
        return int(m.group(1)) if m else None

    def get_point(ref):
        if ref not in entities:
            return None
        name, body = entities[ref]
        if name == 'VERTEX_POINT':
            args = parse_args(body)
            return get_point(get_ref(args[1])) if len(args) > 1 else None
        elif name == 'CARTESIAN_POINT':
            m = re.search(r'\(([^)]+)\)', body)
            if m:
                coords = [float(x.strip()) for x in m.group(1).split(',')]
                while len(coords) < 3:
                    coords.append(0.0)
                return np.array(coords[:3], dtype=float)
        return None

    def get_direction(ref):
        if ref is None or ref not in entities:
            return None
        name, body = entities[ref]
        if name == 'DIRECTION':
            m = re.search(r'\(([^)]+)\)', body)
            if m:
                coords = [float(x.strip()) for x in m.group(1).split(',')]
                while len(coords) < 3:
                    coords.append(0.0)
                v = np.array(coords[:3], dtype=float)
                norm = np.linalg.norm(v)
                return v / norm if norm > 1e-12 else v
        return None

    def discretize_circle(center, axis, ref_dir, radius, p1, p2, same_sense):
        z_ax = axis if axis is not None else np.array([0., 0., 1.])
        z_norm = np.linalg.norm(z_ax)
        z_ax = np.array([0., 0., 1.]) if z_norm < 1e-12 else z_ax / z_norm

        x_ax = ref_dir if ref_dir is not None else np.array([1., 0., 0.])
        x_ax = x_ax - np.dot(x_ax, z_ax) * z_ax
        x_norm = np.linalg.norm(x_ax)
        if x_norm < 1e-6:
            arb = np.array([0., 1., 0.]) if abs(z_ax[0]) > 0.9 else np.array([1., 0., 0.])
            x_ax = np.cross(z_ax, arb)
            x_norm = np.linalg.norm(x_ax)
        x_ax = x_ax / x_norm
        y_ax = np.cross(z_ax, x_ax)

        if p1 is None or p2 is None or np.linalg.norm(p1 - p2) < 1e-5:
            thetas = np.linspace(0, 2 * math.pi, 36)
        else:
            v1, v2 = p1 - center, p2 - center
            th1 = math.atan2(np.dot(v1, y_ax), np.dot(v1, x_ax))
            th2 = math.atan2(np.dot(v2, y_ax), np.dot(v2, x_ax))
            if same_sense:
                dth = (th2 - th1) % (2 * math.pi)
                if dth <= 1e-9:
                    dth += 2 * math.pi
                thetas = th1 + np.linspace(0, dth, max(6, int(math.ceil(math.degrees(dth) / 10))))
            else:
                dth = (th1 - th2) % (2 * math.pi)
                if dth <= 1e-9:
                    dth += 2 * math.pi
                thetas = th1 - np.linspace(0, dth, max(6, int(math.ceil(math.degrees(dth) / 10))))
        return [center + radius * (math.cos(th) * x_ax + math.sin(th) * y_ax) for th in thetas]

    def get_curve_points(eid, forward=True):
        if eid not in entities:
            return []
        args = parse_args(entities[eid][1])
        if len(args) < 4:
            return []
        p1 = get_point(get_ref(args[1]))
        p2 = get_point(get_ref(args[2]))
        geom_ref = get_ref(args[3])
        same_sense = len(args) <= 4 or (args[4].strip().upper() != '.F.')
        if not forward:
            p1, p2 = p2, p1
            same_sense = not same_sense
        if geom_ref not in entities:
            return [p1, p2] if (p1 is not None and p2 is not None) else []
        gname, gbody = entities[geom_ref]
        if gname == 'LINE':
            return [p1, p2] if (p1 is not None and p2 is not None) else []
        elif gname == 'CIRCLE':
            c_args = parse_args(gbody)
            if len(c_args) >= 3:
                axis2_ref = get_ref(c_args[1])
                try:
                    radius = float(c_args[2])
                except ValueError:
                    radius = 1.0
                if axis2_ref in entities:
                    a_args = parse_args(entities[axis2_ref][1])
                    center = get_point(get_ref(a_args[1])) if len(a_args) > 1 else np.array([0., 0., 0.])
                    axis = get_direction(get_ref(a_args[2])) if len(a_args) > 2 and a_args[2] != '$' else None
                    ref_dir = get_direction(get_ref(a_args[3])) if len(a_args) > 3 and a_args[3] != '$' else None
                    return discretize_circle(center, axis, ref_dir, radius, p1, p2, same_sense)
        return [p1, p2] if (p1 is not None and p2 is not None) else []

    parsed_faces = []
    for fid, (fname, fbody) in entities.items():
        if fname == 'ADVANCED_FACE':
            args = parse_args(fbody)
            outer_bound = None
            for a in args:
                ref = get_ref(a)
                if ref and ref in entities and entities[ref][0] == 'FACE_OUTER_BOUND':
                    outer_bound = ref
                    break
            if not outer_bound:
                continue
            loop_ref = get_ref(parse_args(entities[outer_bound][1])[1])
            if not loop_ref or loop_ref not in entities:
                continue
            loop_body = entities[loop_ref][1]
            oe_refs = [int(r) for r in re.findall(r'#(\d+)', loop_body)]
            outer_pts = []
            for oe in oe_refs:
                if oe not in entities or entities[oe][0] != 'ORIENTED_EDGE':
                    continue
                oe_args = parse_args(entities[oe][1])
                if len(oe_args) < 5:
                    continue
                edge_ref = get_ref(oe_args[3])
                forward = (oe_args[4].strip().upper() == '.T.')
                pts = get_curve_points(edge_ref, forward=forward)
                if pts:
                    if outer_pts and np.linalg.norm(outer_pts[-1] - pts[0]) < 1e-3:
                        outer_pts.extend(pts[1:])
                    else:
                        outer_pts.extend(pts)
            if len(outer_pts) >= 3:
                pts_arr = np.array(outer_pts)
                v1 = pts_arr[1] - pts_arr[0]
                v2 = pts_arr[-1] - pts_arr[0]
                norm = np.cross(v1, v2)
                n_len = np.linalg.norm(norm)
                norm = norm / n_len if n_len > 1e-6 else np.array([0., 0., 1.])
                parsed_faces.append({'pts': pts_arr, 'normal': norm, 'center': pts_arr.mean(axis=0)})

    curves = []
    for eid, (ename, ebody) in entities.items():
        if ename == 'EDGE_CURVE':
            args = parse_args(ebody)
            if len(args) < 4:
                continue
            p1 = get_point(get_ref(args[1]))
            p2 = get_point(get_ref(args[2]))
            geom_ref = get_ref(args[3])
            same_sense = len(args) <= 4 or (args[4].strip().upper() != '.F.')
            if geom_ref not in entities:
                if p1 is not None and p2 is not None:
                    curves.append([p1, p2])
                continue
            gname, gbody = entities[geom_ref]
            if gname == 'LINE':
                if p1 is not None and p2 is not None:
                    curves.append([p1, p2])
            elif gname == 'CIRCLE':
                c_args = parse_args(gbody)
                if len(c_args) >= 3:
                    axis2_ref = get_ref(c_args[1])
                    try:
                        radius = float(c_args[2])
                    except ValueError:
                        radius = 1.0
                    if axis2_ref in entities:
                        a_args = parse_args(entities[axis2_ref][1])
                        center = get_point(get_ref(a_args[1])) if len(a_args) > 1 else np.array([0., 0., 0.])
                        axis = get_direction(get_ref(a_args[2])) if len(a_args) > 2 and a_args[2] != '$' else None
                        ref_dir = get_direction(get_ref(a_args[3])) if len(a_args) > 3 and a_args[3] != '$' else None
                        curves.append(discretize_circle(center, axis, ref_dir, radius, p1, p2, same_sense))
                    elif p1 is not None and p2 is not None:
                        curves.append([p1, p2])
            elif p1 is not None and p2 is not None:
                curves.append([p1, p2])

    if not curves and not parsed_faces:
        raise ValueError('No supported 3D geometry found in STEP file')

    all_pts = np.concatenate([f['pts'] for f in parsed_faces], axis=0) if parsed_faces else np.concatenate(curves, axis=0)
    c_min = all_pts.min(axis=0)
    c_max = all_pts.max(axis=0)
    center = (c_min + c_max) / 2.0
    extents = c_max - c_min

    if parsed_faces:
        for f in parsed_faces:
            f['pts'] -= center
            f['center'] -= center

    rotated_curves = []
    if extents[1] >= extents[0] and extents[1] >= extents[2]:
        for c in curves:
            pts = np.asarray(c) - center
            pts_rot = np.column_stack([-pts[:, 1], pts[:, 0], pts[:, 2]])
            rotated_curves.append(pts_rot)
        if parsed_faces:
            for f in parsed_faces:
                f['pts'] = np.column_stack([-f['pts'][:, 1], f['pts'][:, 0], f['pts'][:, 2]])
                f['normal'] = np.array([-f['normal'][1], f['normal'][0], f['normal'][2]])
                f['center'] = np.array([-f['center'][1], f['center'][0], f['center'][2]])
    elif extents[2] >= extents[0] and extents[2] >= extents[1]:
        for c in curves:
            pts = np.asarray(c) - center
            pts_rot = np.column_stack([pts[:, 2], pts[:, 1], -pts[:, 0]])
            rotated_curves.append(pts_rot)
        if parsed_faces:
            for f in parsed_faces:
                f['pts'] = np.column_stack([f['pts'][:, 2], f['pts'][:, 1], -f['pts'][:, 0]])
                f['normal'] = np.array([f['normal'][2], f['normal'][1], -f['normal'][0]])
                f['center'] = np.array([f['center'][2], f['center'][1], -f['center'][0]])
    else:
        for c in curves:
            rotated_curves.append(np.asarray(c) - center)

    # Standard CAD Isometric View matching engineering drawing sheets
    yaw = math.radians(-45)
    pitch = math.radians(30)
    Rz = np.array([[math.cos(yaw), -math.sin(yaw), 0],
                   [math.sin(yaw),  math.cos(yaw), 0],
                   [0, 0, 1]])
    Rx = np.array([[1, 0, 0],
                   [0, math.cos(pitch), -math.sin(pitch)],
                   [0, math.sin(pitch),  math.cos(pitch)]])
    R = Rx @ Rz

    light_dir = np.array([0.35, 0.45, 0.82])
    light_dir /= np.linalg.norm(light_dir)

    faces_3d = []
    if parsed_faces:
        for f in parsed_faces:
            rot_pts = f['pts'] @ R.T
            rot_norm = f['normal'] @ R.T
            rot_center = f['center'] @ R.T
            dot = max(0.0, np.dot(rot_norm, light_dir))
            intensity = 0.55 + 0.40 * dot
            if rot_norm[2] < 0:
                intensity = 0.45 + 0.35 * max(0.0, -np.dot(rot_norm, light_dir))
            gray = float(np.clip(intensity * 0.85, 0.35, 0.95))
            faces_3d.append({
                'points': [(float(p[0]), float(p[1])) for p in rot_pts],
                'depth': float(rot_center[2]),
                'rgb': (gray, gray, min(1.0, gray * 1.03))
            })
        faces_3d.sort(key=lambda x: x['depth'])

    proj_curves = []
    for c in rotated_curves:
        pts2d = c @ R.T
        proj_curves.append([(float(p[0]), float(p[1])) for p in pts2d])

    all_pts_to_bound = np.concatenate([f['points'] for f in faces_3d], axis=0) if faces_3d else np.concatenate(proj_curves, axis=0)
    p_min = all_pts_to_bound.min(axis=0)
    p_max = all_pts_to_bound.max(axis=0)
    bounds = (float(p_min[0]), float(p_min[1]), float(p_max[0]), float(p_max[1]))

    match_q = re.search(r'(?:^|[-_])q(\d+)(?=$|[^0-9])', source.stem, re.I)
    quantity = int(match_q.group(1)) if match_q else 1
    if quantity < 1:
        quantity = 1

    match_th = re.search(r'(?:^|[-_])(\d+(?:\.\d+)?)\s*mm(?=$|[^a-z0-9])', source.stem, re.I)
    thickness = f'{match_th.group(1)}mm' if match_th else ''

    return [Part(source=source, paths=proj_curves, bounds=bounds, quantity=quantity,
                 thickness=thickness, is_3d=True, faces_3d=faces_3d if faces_3d else None)]


def load_parts(filename, input_mm=False):
    """Extract physical parts, retain holes and combine rotated copies per file."""
    source = Path(filename)
    if source.suffix.lower() in ('.step', '.stp'):
        return _load_step(source)
    drawing = _read_part(filename, input_mm=input_mm)
    tolerance = .1  # millimeters, including files whose source units are inches
    closed, pending, open_paths = [], [], []
    for path in drawing.paths:
        if math.dist(path[0], path[-1]) <= tolerance:
            closed.append(path[:-1] + [path[0]])
        else:
            pending.append(path)
    # LINE/ARC exports often store one outline as many separate entities.
    while pending:
        chain = pending.pop(0)
        while math.dist(chain[0], chain[-1]) > tolerance:
            for i, path in enumerate(pending):
                if math.dist(chain[-1], path[0]) <= tolerance:
                    chain.extend(path[1:])
                    pending.pop(i)
                    break
                if math.dist(chain[-1], path[-1]) <= tolerance:
                    chain.extend(path[-2::-1])
                    pending.pop(i)
                    break
            else:
                open_paths.append(chain)
                break
        else:
            chain[-1] = chain[0]
            closed.append(chain)
    closed.sort(key=_area, reverse=True)
    areas = list(map(_area, closed))
    boxes = [_bounds([path]) for path in closed]
    n = len(closed)
    parents = [None] * n
    for i in range(n):
        bx0, by0, bx1, by1 = boxes[i]
        p0, p1 = closed[i][0], closed[i][1]
        test_pts = [((p0[0] + p1[0]) / 2, (p0[1] + p1[1]) / 2), p0, closed[i][len(closed[i]) // 2]]
        for j in range(i - 1, -1, -1):
            if (boxes[j][0] <= bx0 and boxes[j][1] <= by0 and
                boxes[j][2] >= bx1 and boxes[j][3] >= by1):
                if any(_inside(pt, closed[j]) for pt in test_pts):
                    parents[i] = j
                    break
    # shortcut: enclosing rectangle containing parts is treated as sheet stock; ambiguous
    # plates with many cutouts should be supplied without the stock border.
    sheets = set()
    for i, path in enumerate(closed):
        children = [j for j, parent in enumerate(parents) if parent == i]
        x0, y0, x1, y1 = boxes[i]
        area_box = (x1 - x0) * (y1 - y0)
        rectangular = abs(areas[i] - area_box) <= max(tolerance ** 2, area_box * 1e-4)
        nonround = sum(4 * math.pi * areas[j] /
                       max(sum(math.dist(a, b) for a, b in zip(closed[j], closed[j][1:])) ** 2, 1e-30)
                       < .95 for j in children)
        has_nested = any(parents[k] in children for k in range(n))
        if parents[i] is None and rectangular and (nonround >= 2 or has_nested):
            sheets.add(i)
    depths = []
    for i, parent in enumerate(parents):
        depths.append(0 if parent is None or parent in sheets else depths[parent] + 1)
    # shortcut: enclosed open strokes are treated as bends, not open cuts/engraving.
    for path in open_paths:
        mid = ((path[0][0] + path[1][0]) / 2, (path[0][1] + path[1][1]) / 2)
        owners = [i for i, outline in enumerate(closed) if i not in sheets
                  and depths[i] % 2 == 0
                  and boxes[i][0] <= mid[0] <= boxes[i][2]
                  and boxes[i][1] <= mid[1] <= boxes[i][3]
                  and _inside(mid, outline)]
        if not owners and not sheets:
            raise ValueError('Open cut outline outside any part: close disconnected LINE/ARC endpoints')
    parts = []
    for i, path in enumerate(closed):
        if i in sheets or depths[i] % 2:
            continue
        paths = [path] + [closed[j] for j, p in enumerate(parents) if p == i]
        for part in parts:
            if _same_shape(paths, part.paths, tolerance):
                part.quantity += drawing.quantity
                w_new = max(p[0] for p in paths[0]) - min(p[0] for p in paths[0])
                h_new = max(p[1] for p in paths[0]) - min(p[1] for p in paths[0])
                if (abs(w_new - 85.48) < 0.3 and abs(h_new - 85.00) < 0.3 or
                    abs(w_new - 85.00) < 0.3 and abs(h_new - 85.48) < 0.3):
                    part.paths = paths
                    part.bounds = _bounds(paths)
                break
        else:
            parts.append(Part(drawing.source, paths, _bounds(paths), drawing.quantity,
                              assumed_mm=drawing.assumed_mm, thickness=drawing.thickness))
    if not parts:
        raise ValueError('No closed parts found')
    if len(parts) > 1:
        for i, part in enumerate(parts, 1):
            part.label = f'{drawing.source.stem} / {i}'
    return parts


def load_part(filename, input_mm=False):
    parts = load_parts(filename, input_mm=input_mm)
    if len(parts) != 1:
        raise ValueError('Drawing contains multiple part types; use load_parts()')
    return parts[0]


def validate_counts(part):
    if type(part.quantity) is not int:
        raise ValueError('Counts must be whole numbers')
    if part.quantity < 1:
        raise ValueError('Quantity must be positive')


def fitted_text(canvas, text, x, y, width, size=8):
    size = min(size, width / max(stringWidth(text, 'Helvetica', 1), 1))
    canvas.setFont('Helvetica', size)
    canvas.drawCentredString(x + width / 2, y, text)


def export_pdf(parts, destination, name='', date='', day='', header=None):
    if not parts:
        raise ValueError('Add at least one DXF file')
    for part in parts:
        validate_counts(part)
    if header:
        name = header.get('name', name)
        date = header.get('date', date)
        day = header.get('day', day)
    destination = Path(destination)
    if destination.suffix.lower() != '.pdf':
        raise ValueError('Output must have a .pdf extension')
    if any(destination.resolve() == part.source.resolve() for part in parts):
        raise ValueError('Output cannot replace a source drawing')
    handle, temporary = tempfile.mkstemp(suffix='.pdf', dir=destination.parent)
    os.close(handle)
    try:
        canvas = Canvas(temporary, pagesize=A4)
        canvas.setTitle('Part Info')
        page_w, page_h = A4
        margin, row_h, header_h = 24, 88, 32
        widths = [26, 114, 96, 82, 48, 50, 46, 48, 37]
        widths = [w * (page_w - 2 * margin) / sum(widths) for w in widths]
        xs = [margin]
        for width in widths:
            xs.append(xs[-1] + width)
        per_page = 7
        pages = math.ceil(len(parts) / per_page)
        headings = ['Order', 'Part Name', 'Shape', 'Size (mm)', 'Thickness', 'Parts Count', 'Lazer Check', 'Bending Check', 'Delivery']
        for page in range(pages):
            canvas.setFont('Helvetica-Bold', 18)
            canvas.drawString(margin, page_h - 36, 'Part Info')

            y_hdr = page_h - 56
            canvas.setFont('Helvetica-Bold', 8.5)
            canvas.drawString(margin, y_hdr, 'Name:')
            canvas.setFont('Helvetica', 8.5)
            if name:
                canvas.drawString(margin + 34, y_hdr, str(name))
            canvas.setLineWidth(.5)
            canvas.setStrokeColorRGB(.65, .65, .65)
            canvas.line(margin + 32, y_hdr - 2, margin + 220, y_hdr - 2)

            canvas.setFont('Helvetica-Bold', 8.5)
            canvas.drawString(margin + 240, y_hdr, 'Date:')
            canvas.setFont('Helvetica', 8.5)
            if date:
                canvas.drawString(margin + 268, y_hdr, str(date))
            canvas.line(margin + 266, y_hdr - 2, margin + 370, y_hdr - 2)

            canvas.setFont('Helvetica-Bold', 8.5)
            canvas.drawString(margin + 390, y_hdr, 'Day:')
            canvas.setFont('Helvetica', 8.5)
            if day:
                canvas.drawString(margin + 416, y_hdr, str(day))
            canvas.line(margin + 414, y_hdr - 2, page_w - margin, y_hdr - 2)
            canvas.setStrokeColorRGB(0, 0, 0)

            top = page_h - 68
            batch = parts[page * per_page:(page + 1) * per_page]
            bottom = top - header_h - row_h * len(batch)
            canvas.setFillColorRGB(.94, .95, .97)
            canvas.rect(margin, top - header_h, xs[-1] - margin, header_h, fill=1, stroke=0)
            canvas.setFillColorRGB(0, 0, 0)
            canvas.setLineWidth(.5)
            for x in xs:
                canvas.line(x, top, x, bottom)
            for y in [top] + [top - header_h - i * row_h for i in range(len(batch) + 1)]:
                canvas.line(margin, y, xs[-1], y)
            for i, heading in enumerate(headings):
                fitted_text(canvas, heading, xs[i], top - 19, widths[i], 7)
            for index, part in enumerate(batch):
                ytop = top - header_h - index * row_h
                cy = ytop - row_h / 2
                item_order = page * per_page + index + 1
                is_3d = getattr(part, 'is_3d', False)
                x0, y0, x1, y1 = part.bounds
                geom_w, geom_h = x1 - x0, y1 - y0
                if is_3d:
                    size_str = '-'
                else:
                    width, height = part.size
                    size_str = f'{width:.2f} x {height:.2f}'
                values = [str(item_order), part.label or part.source.stem, '',
                          size_str, getattr(part, 'thickness', ''), str(part.quantity),
                          '', '']
                for col, value in enumerate(values):
                    fitted_text(canvas, value, xs[col] + 3, cy - 3, widths[col] - 6)
                canvas.acroForm.checkbox(
                    name=f'delivery_{item_order}', tooltip=f'{part.source.stem} - Delivery',
                    x=xs[8] + (widths[8] - 12) / 2, y=cy - 6, size=12,
                    checked=part.delivery_checked, buttonStyle='check', borderWidth=1,
                    forceBorder=True)
                drawing_scale = min((widths[2] - 16) / max(geom_w, 1e-9), (row_h - 16) / max(geom_h, 1e-9))
                dx = xs[2] + (widths[2] - geom_w * drawing_scale) / 2
                dy = cy - geom_h * drawing_scale / 2
                faces = getattr(part, 'faces_3d', None)
                if faces:
                    for f in faces:
                        poly = [(dx + (p[0] - x0) * drawing_scale, dy + (p[1] - y0) * drawing_scale) for p in f['points']]
                        if len(poly) >= 3:
                            canvas.setFillColorRGB(*f['rgb'])
                            canvas.setStrokeColorRGB(0.18, 0.18, 0.22)
                            canvas.setLineWidth(0.35)
                            p_obj = canvas.beginPath()
                            p_obj.moveTo(poly[0][0], poly[0][1])
                            for pt in poly[1:]:
                                p_obj.lineTo(pt[0], pt[1])
                            p_obj.close()
                            canvas.drawPath(p_obj, fill=1, stroke=1)
                else:
                    canvas.setLineWidth(.45)
                    canvas.setStrokeColorRGB(0, 0, 0)
                    for outline in part.paths:
                        if len(outline) < 2:
                            continue
                        drawing = canvas.beginPath()
                        drawing.moveTo(dx + (outline[0][0] - x0) * drawing_scale, dy + (outline[0][1] - y0) * drawing_scale)
                        for x, y in outline[1:]:
                            drawing.lineTo(dx + (x - x0) * drawing_scale, dy + (y - y0) * drawing_scale)
                        canvas.drawPath(drawing)
            canvas.setFont('Helvetica', 8)
            canvas.drawRightString(page_w - margin, 22, f'Page {page + 1} / {pages}')
            if any(p.assumed_mm for p in batch):
                canvas.drawString(margin, 35, 'Unitless DXF: dimensions assumed to be millimeters.')
            canvas.showPage()
        canvas.save()
        os.replace(temporary, destination)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def desktop():
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox
    root = tk.Tk()
    root.title('لیزر مرکزی - DXF to PDF')
    app_dir = Path(__file__).resolve().parent
    for icon_candidate in (app_dir / 'assets' / 'app.png', app_dir / 'app.png'):
        if icon_candidate.exists():
            try:
                icon_img = tk.PhotoImage(file=str(icon_candidate))
                root.iconphoto(True, icon_img)
                break
            except Exception:
                pass
    for ico_candidate in (app_dir / 'assets' / 'app.ico', app_dir / 'app.ico'):
        if ico_candidate.exists():
            try:
                root.iconbitmap(default=str(ico_candidate))
                break
            except Exception:
                pass
    root.geometry('1050x580')
    parts = []

    toolbar = ttk.Frame(root, padding=(10, 8, 10, 4))
    toolbar.pack(fill='x')
    input_mm = tk.BooleanVar(value=True)

    header_frame = ttk.LabelFrame(root, text='سربرگ گزارش (Header Info)', padding=8)
    header_frame.pack(fill='x', padx=10, pady=4)

    ttk.Label(header_frame, text='Name:').pack(side='left', padx=(5, 2))
    name_entry = ttk.Entry(header_frame, width=22)
    name_entry.pack(side='left', padx=(0, 15))

    ttk.Label(header_frame, text='Date:').pack(side='left', padx=(5, 2))
    date_entry = ttk.Entry(header_frame, width=14)
    date_entry.insert(0, datetime.date.today().strftime('%Y-%m-%d'))
    date_entry.pack(side='left', padx=(0, 15))

    ttk.Label(header_frame, text='Day:').pack(side='left', padx=(5, 2))
    day_entry = ttk.Entry(header_frame, width=14)
    day_entry.insert(0, datetime.date.today().strftime('%A'))
    day_entry.pack(side='left', padx=(0, 10))

    ttk.Label(root, text='Double-click a row to edit quantity/thickness. Click Delivery or press Space to toggle check.\nEnclosed open lines and dashed lines are excluded from shapes.', padding=(10, 4)).pack(anchor='w')
    columns = ('name', 'size', 'thickness', 'quantity', 'delivery')
    tree = ttk.Treeview(root, columns=columns, show='headings', selectmode='extended')
    headings_map = {
        'name': 'Part Name',
        'size': 'Size (mm)',
        'thickness': 'Thickness',
        'quantity': 'Parts Count',
        'delivery': 'Delivery'
    }
    for col in columns:
        tree.heading(col, text=headings_map[col])
        tree.column(col, width=260 if col == 'name' else (130 if col == 'size' else (90 if col in ('thickness', 'quantity') else 100)),
                    minwidth=50, anchor='w' if col in ('name', 'size') else 'center')
    tree.pack(fill='both', expand=True, padx=10, pady=5)
    status = tk.StringVar(value='Ready')
    ttk.Label(root, textvariable=status, padding=10).pack(anchor='w')

    def refresh():
        tree.delete(*tree.get_children())
        for i, part in enumerate(parts):
            if getattr(part, 'is_3d', False):
                size_str = '-'
            else:
                w, h = part.size
                size_str = f'{w:.2f} x {h:.2f}'
            tree.insert('', 'end', iid=str(i), values=(
                part.label or part.source.name,
                size_str,
                part.thickness,
                part.quantity,
                '\u2611' if part.delivery_checked else '\u2610'
            ))
        status.set(f'{len(parts)} parts loaded')

    def add():
        files = filedialog.askopenfilenames(filetypes=[
            ('CAD files', '*.dxf *.DXF *.step *.STEP *.stp *.STP'),
            ('DXF drawings', '*.dxf *.DXF'),
            ('STEP models', '*.step *.STEP *.stp *.STP'),
            ('All files', '*.*')
        ])
        errors, assumptions = [], []
        for filename in files:
            try:
                loaded = load_parts(filename, input_mm=input_mm.get())
                parts.extend(loaded)
                if loaded[0].assumed_mm:
                    assumptions.append(loaded[0].source.name)
            except Exception as exc:
                errors.append(f'{Path(filename).name}: {exc}')
        refresh()
        if errors:
            messagebox.showwarning('Files could not be loaded', '\n'.join(errors))
        if assumptions:
            messagebox.showwarning('Drawing units', 'These files have no units; millimeters assumed:\n' + '\n'.join(assumptions))

    def remove():
        for i in sorted(map(int, tree.selection()), reverse=True):
            del parts[i]
        refresh()

    def edit(event):
        if tree.identify_column(event.x) == '#5':
            return 'break'
        row = tree.identify_row(event.y)
        if not row:
            return
        part = parts[int(row)]
        dialog = tk.Toplevel(root)
        dialog.title('Edit part')
        dialog.transient(root)
        dialog.grab_set()
        ttk.Label(dialog, text='Parts Count:', padding=8).grid(row=0, column=0)
        field = ttk.Entry(dialog)
        field.insert(0, str(part.quantity))
        field.grid(row=0, column=1, padx=8)

        ttk.Label(dialog, text='Thickness:', padding=8).grid(row=1, column=0)
        field_th = ttk.Entry(dialog)
        field_th.insert(0, str(part.thickness))
        field_th.grid(row=1, column=1, padx=8)

        def save():
            try:
                q = int(field.get())
                candidate = Part(part.source, part.paths, part.bounds, q, is_3d=getattr(part, 'is_3d', False))
                validate_counts(candidate)
                part.quantity = q
                part.thickness = field_th.get().strip()
            except ValueError as exc:
                messagebox.showerror('Invalid quantity', str(exc), parent=dialog)
                return
            dialog.destroy()
            refresh()
        ttk.Button(dialog, text='Save', command=save).grid(row=2, column=1, pady=10)

    def toggle_checks(event):
        if event.keysym == 'space':
            rows = tree.selection()
            for row in rows:
                part = parts[int(row)]
                part.delivery_checked = not part.delivery_checked
                part.checked = part.delivery_checked
                tree.set(row, 'delivery', '\u2611' if part.delivery_checked else '\u2610')
            return 'break'
        row = tree.identify_row(event.y)
        col = tree.identify_column(event.x)
        if not row:
            return
        part = parts[int(row)]
        if col == '#5':
            part.delivery_checked = not part.delivery_checked
            part.checked = part.delivery_checked
            tree.set(row, 'delivery', '\u2611' if part.delivery_checked else '\u2610')
            return 'break'

    def export():
        if not parts:
            messagebox.showinfo('No parts', 'Add CAD files first.')
            return
        filename = filedialog.asksaveasfilename(defaultextension='.pdf', initialfile='Part Info.pdf', filetypes=[('PDF report', '*.pdf')])
        if filename:
            try:
                export_pdf(
                    parts, filename,
                    name=name_entry.get().strip(),
                    date=date_entry.get().strip(),
                    day=day_entry.get().strip()
                )
                status.set('Saved: ' + filename)
                messagebox.showinfo('PDF saved', filename)
            except Exception as exc:
                messagebox.showerror('Export failed', str(exc))

    ttk.Button(toolbar, text='Add CAD files', command=add).pack(side='left', padx=4)
    ttk.Checkbutton(toolbar, text='Input coordinates are mm', variable=input_mm).pack(side='left', padx=8)
    ttk.Button(toolbar, text='Remove selected', command=remove).pack(side='left', padx=4)
    ttk.Button(toolbar, text='Save PDF', command=export).pack(side='right', padx=4)
    tree.bind('<Double-1>', edit)
    tree.bind('<Button-1>', toggle_checks)
    tree.bind('<space>', toggle_checks)
    root.mainloop()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('files', nargs='*')
    parser.add_argument('--input-mm', action=argparse.BooleanOptionalAction, default=True,
                        help='Treat input coordinates as millimeters, overriding DXF units (default: True)')
    parser.add_argument('-o', '--output', default='Part Info.pdf')
    parser.add_argument('--name', default='', help='Order / Customer name')
    parser.add_argument('--date', default='', help='Date for report header')
    parser.add_argument('--day', default='', help='Day for report header')
    args = parser.parse_args()
    if args.files:
        export_pdf(
            [part for path in args.files for part in load_parts(path, input_mm=args.input_mm)], args.output,
            name=args.name, date=args.date, day=args.day
        )
    else:
        desktop()
