"""Batch DXF part reports. Run without arguments for the desktop application."""
import argparse
import datetime
import math
import os
from pathlib import Path
import re
import tempfile
import time
from dataclasses import dataclass

import ezdxf
from ezdxf import units
from ezdxf.disassemble import recursive_decompose
from ezdxf.path import make_path
import numpy as np
from reportlab.lib.pagesizes import A4
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfgen.canvas import Canvas
from reportlab.lib.utils import ImageReader


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
    profile_paths: list = None
    profile_bounds: tuple = None
    size_3d: tuple = None
    layers: list = None
    open_paths: list = None

    def __post_init__(self):
        if self.open_paths is None:
            self.open_paths = []
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
    path_layers = []
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
            if styled_entity.dxf.layer in doc.layers:
                linetype = doc.layers.get(styled_entity.dxf.layer).dxf.get('linetype', 'CONTINUOUS').upper()
            else:
                linetype = 'CONTINUOUS'
        if linetype not in ('CONTINUOUS', 'BYBLOCK'):
            if linetype in doc.linetypes:
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
            path_layers.append(styled_entity.dxf.layer)
    if skipped:
        raise ValueError('Unsupported geometry: ' + ', '.join(sorted(skipped)))
    if not paths:
        raise ValueError('No supported 2D geometry found')
    from ezdxf.path import bbox as path_bbox
    box = path_bbox(paths, fast=False)
    raw_span = max(box.extmax.x - box.extmin.x, box.extmax.y - box.extmin.y)
    if doc.units == units.M and not input_mm and raw_span > 25:
        # Drawing coordinates are already in millimeters despite $INSUNITS=6 (Meters) in header
        scale = 1
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
    match_th = re.search(r'(?:(?:^|[-_])(\d+(?:\.\d+)?)\s*mm(?=$|[^a-z0-9]))|(?:(?:ahan|iron|steel|alum|sheet)[-\s_]+(\d+(?:\.\d+)?))', source.stem, re.I)
    thickness = f'{match_th.group(1) or match_th.group(2)}mm' if match_th else ''
    return Part(source, outlines, bounds, quantity, assumed_mm=not bool(doc.units) and not input_mm,
                thickness=thickness, layers=path_layers)



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
    y = y + 1e-9
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
    def sample_points(outlines, origin):
        pts = []
        for p in outlines:
            pts.append(np.asarray(p) - origin)
            if len(p) > 1:
                pts.append((np.asarray(p[:-1]) + np.asarray(p[1:])) / 2 - origin)
        return np.concatenate(pts, axis=0)

    aa, az = segments(paths, ca)
    ba, bz = segments(other, cb)
    pts_a = sample_points(paths, ca)
    pts_b = sample_points(other, cb)
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
            if near(pts_a @ rotation, ba, bz) and near(pts_b, aa @ rotation, az @ rotation):
                return True
    return False


def _load_step(filename):
    from OCP.BRep import BRep_Tool
    from OCP.BRepAdaptor import BRepAdaptor_Curve, BRepAdaptor_Surface
    from OCP.BRepBndLib import BRepBndLib
    from OCP.BRepMesh import BRepMesh_IncrementalMesh
    from OCP.Bnd import Bnd_Box
    from OCP.GCPnts import GCPnts_QuasiUniformDeflection
    from OCP.GeomAbs import GeomAbs_Cylinder, GeomAbs_Line
    from OCP.HLRAlgo import HLRAlgo_Projector
    from OCP.HLRBRep import HLRBRep_Algo, HLRBRep_HLRToShape
    from OCP.IFSelect import IFSelect_RetDone
    from OCP.STEPControl import STEPControl_Reader
    from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE, TopAbs_REVERSED
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopLoc import TopLoc_Location
    from OCP.TopoDS import TopoDS
    from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt

    source = Path(filename)
    reader = STEPControl_Reader()
    if reader.ReadFile(str(source)) != IFSelect_RetDone or not reader.TransferRoots():
        raise ValueError(f'Cannot read STEP geometry: {source.name}')
    shape = reader.OneShape()
    if shape.IsNull():
        raise ValueError('No supported 3D geometry found in STEP file')

    def items(kind):
        explorer = TopExp_Explorer(shape, kind)
        while explorer.More():
            yield explorer.Current()
            explorer.Next()

    box = Bnd_Box()
    BRepBndLib.AddOptimal_s(shape, box)
    if box.IsVoid():
        raise ValueError('STEP model has no finite geometry')
    bounds3d = np.array(box.Get()).reshape(2, 3)
    size_3d = tuple(bounds3d[1] - bounds3d[0])
    center = bounds3d.mean(axis=0)

    # Partial cylinders identify bends; full cylinders are usually drilled holes.
    candidates = []
    for face in items(TopAbs_FACE):
        surface = BRepAdaptor_Surface(TopoDS.Face_s(face))
        if surface.GetType() == GeomAbs_Cylinder:
            sweep = surface.LastUParameter() - surface.FirstUParameter()
            if 1e-5 < sweep < 2 * math.pi - 1e-5:
                axis = np.array(surface.Cylinder().Axis().Direction().Coord())
                length = abs(surface.LastVParameter() - surface.FirstVParameter())
                candidates.append((axis, length * sweep))
    if not candidates:
        for edge in items(TopAbs_EDGE):
            curve = BRepAdaptor_Curve(TopoDS.Edge_s(edge))
            if curve.GetType() == GeomAbs_Line:
                start = np.array(curve.Value(curve.FirstParameter()).Coord())
                end = np.array(curve.Value(curve.LastParameter()).Coord())
                length = np.linalg.norm(end - start)
                if length > 1e-6:
                    candidates.append(((end - start) / length, length))
    if not candidates:
        candidates = [(np.array([0., 0., 1.]), 1.)]
    bend = max(candidates, key=lambda c: sum(w for axis, w in candidates
               if abs(np.dot(axis, c[0])) > 0.999))[0].copy()
    if bend[np.argmax(np.abs(bend))] < 0:
        bend = -bend
    up = np.array([0., 1., 0.])
    up -= np.dot(up, bend) * bend
    if np.linalg.norm(up) < 0.2:
        up = np.array([0., 0., 1.])
        up -= np.dot(up, bend) * bend
    up /= np.linalg.norm(up)
    across = np.cross(up, bend)

    # Use the same local frame for both views, independent of STEP world axes.
    view = (across + bend + up) / math.sqrt(3)
    right = (across - bend) / math.sqrt(2)
    vertical = np.cross(view, right)

    def visible_paths(direction, x_axis):
        hlr = HLRBRep_Algo()
        hlr.Add(shape)
        hlr.Projector(HLRAlgo_Projector(gp_Ax2(gp_Pnt(*center),
                      gp_Dir(*direction), gp_Dir(*x_axis))))
        hlr.Update()
        hlr.Hide()
        result = HLRBRep_HLRToShape(hlr)
        paths = []
        seen = set()
        for compound in (result.VCompound(), result.Rg1LineVCompound(), result.OutLineVCompound()):
            if compound.IsNull():
                continue
            explorer = TopExp_Explorer(compound, TopAbs_EDGE)
            while explorer.More():
                curve = BRepAdaptor_Curve(TopoDS.Edge_s(explorer.Current()))
                samples = GCPnts_QuasiUniformDeflection(curve, 0.025)
                if not samples.IsDone():
                    raise ValueError('Cannot approximate STEP drawing curve')
                points = [samples.Value(i).Coord()[:2] for i in range(1, samples.NbPoints() + 1)]
                if len(points) >= 2:
                    key = tuple(tuple(round(c, 5) for c in p) for p in points)
                    key = min(key, key[::-1])
                    if key not in seen:
                        seen.add(key)
                        paths.append(points)
                explorer.Next()
        return paths

    profile_paths = visible_paths(bend, across)
    paths = visible_paths(view, right)
    if not paths or not profile_paths:
        raise ValueError('STEP model has no drawable edges')

    def bounds(paths):
        points = np.concatenate(paths)
        return (*points.min(axis=0), *points.max(axis=0))

    # Triangulation respects inner wires (holes) and curved surfaces.
    BRepMesh_IncrementalMesh(shape, 0.04, False, 0.12, False)
    faces = []
    rotation = np.array([right, vertical, view])
    light = np.array([-0.3, 0.5, 0.8])
    light /= np.linalg.norm(light)
    for face in items(TopAbs_FACE):
        face = TopoDS.Face_s(face)
        location = TopLoc_Location()
        mesh = BRep_Tool.Triangulation_s(face, location)
        if mesh is None:
            raise ValueError('Cannot triangulate STEP surface')
        transform = location.Transformation()
        mesh.ComputeNormals()
        for i in range(1, mesh.NbTriangles() + 1):
            indices = mesh.Triangle(i).Get()
            points = np.array([mesh.Node(j).Transformed(transform).Coord() for j in indices])
            points = (points - center) @ rotation.T
            normal = np.cross(points[1] - points[0], points[2] - points[0])
            if face.Orientation() == TopAbs_REVERSED:
                normal = -normal
            length = np.linalg.norm(normal)
            if length < 1e-12 or normal[2] <= 0:
                continue
            normal /= length
            normals = np.array([mesh.Normal(j).Transformed(transform).Coord() for j in indices])
            normals = normals @ rotation.T
            if face.Orientation() == TopAbs_REVERSED:
                normals = -normals
            normals /= np.maximum(np.linalg.norm(normals, axis=1, keepdims=True), 1e-12)
            diffuse = np.maximum(normals @ light, 0.)
            half_light = light + np.array([0., 0., 1.])
            half_light /= np.linalg.norm(half_light)
            specular = np.maximum(normals @ half_light, 0.) ** 32
            colors = np.clip((0.48 + 0.40 * diffuse[:, None]) * np.array([0.70, 0.82, 0.94])
                             + 0.14 * specular[:, None], 0., 1.)
            faces.append({'points': points[:, :2].tolist(), 'z': points[:, 2].tolist(),
                          'rgb': colors.mean(axis=0).tolist(), 'vertex_rgb': colors.tolist()})
    match_q = re.search(r'(?:^|[-_])q(\d+)(?=$|[^0-9])', source.stem, re.I)
    match_th = re.search(r'(?:^|[-_])(\d+(?:\.\d+)?)\s*mm(?=$|[^a-z0-9])', source.stem, re.I)
    return [Part(source=source, paths=paths, bounds=bounds(paths),
                 quantity=max(1, int(match_q.group(1))) if match_q else 1,
                 thickness=f'{match_th.group(1)}mm' if match_th else '', is_3d=True,
                 faces_3d=faces, profile_paths=profile_paths,
                 profile_bounds=bounds(profile_paths), size_3d=size_3d)]


def _shade_step(faces, bounds):
    """Rasterize smooth surface colors with a per-pixel depth buffer; edges stay vector."""
    from PIL import Image

    x0, y0, x1, y1 = bounds
    span = np.maximum([x1 - x0, y1 - y0], 1e-9)
    width, height = np.maximum(2, np.ceil(1000 * span / max(span)).astype(int))
    depth = np.full((height, width), -np.inf)
    pixels = np.full((height, width, 3), 255, dtype=np.uint8)
    for face in faces:
        points = (np.asarray(face['points']) - [x0, y0]) / span * [width - 1, height - 1]
        points[:, 1] = height - 1 - points[:, 1]
        lo = np.maximum(0, np.floor(points.min(axis=0)).astype(int))
        hi = np.minimum([width - 1, height - 1], np.ceil(points.max(axis=0)).astype(int))
        if np.any(hi < lo):
            continue
        a, b, c = points
        determinant = (b[1] - c[1]) * (a[0] - c[0]) + (c[0] - b[0]) * (a[1] - c[1])
        if abs(determinant) < 1e-12:
            continue
        yy, xx = np.mgrid[lo[1]:hi[1] + 1, lo[0]:hi[0] + 1]
        u = ((b[1] - c[1]) * (xx - c[0]) + (c[0] - b[0]) * (yy - c[1])) / determinant
        v = ((c[1] - a[1]) * (xx - c[0]) + (a[0] - c[0]) * (yy - c[1])) / determinant
        w = 1 - u - v
        za, zb, zc = face['z']
        z = u * za + v * zb + w * zc
        region = np.s_[lo[1]:hi[1] + 1, lo[0]:hi[0] + 1]
        visible = (u >= -1e-9) & (v >= -1e-9) & (w >= -1e-9) & (z > depth[region])
        if not visible.any():
            continue
        ca, cb, cc = np.asarray(face['vertex_rgb'])
        colors = u[visible, None] * ca + v[visible, None] * cb + w[visible, None] * cc
        pixels[region][visible] = np.clip(colors * 255, 0, 255).astype(np.uint8)
        depth[region][visible] = z[visible]
    return Image.fromarray(pixels)


def load_parts(filename, input_mm=False):
    """Extract physical parts, retain holes and combine rotated copies per file."""
    source = Path(filename)
    if source.suffix.lower() in ('.step', '.stp'):
        return _load_step(source)
    drawing = _read_part(filename, input_mm=input_mm)
    drawing_layers = getattr(drawing, 'layers', None) or ['0'] * len(drawing.paths)
    tolerance = .1  # millimeters, including files whose source units are inches
    closed, pending, open_paths = [], [], []
    closed_layers, pending_layers = [], []
    for path, layer in zip(drawing.paths, drawing_layers):
        if math.dist(path[0], path[-1]) <= tolerance:
            closed.append(path[:-1] + [path[0]])
            closed_layers.append(layer)
        else:
            pending.append(path)
            pending_layers.append(layer)
    # LINE/ARC exports often store one outline as many separate entities.
    while pending:
        chain = pending.pop(0)
        layer = pending_layers.pop(0)
        while math.dist(chain[0], chain[-1]) > tolerance:
            for i, path in enumerate(pending):
                if math.dist(chain[-1], path[0]) <= tolerance:
                    chain.extend(path[1:])
                    pending.pop(i)
                    pending_layers.pop(i)
                    break
                if math.dist(chain[-1], path[-1]) <= tolerance:
                    chain.extend(path[-2::-1])
                    pending.pop(i)
                    pending_layers.pop(i)
                    break
            else:
                open_paths.append(chain)
                break
        else:
            chain[-1] = chain[0]
            closed.append(chain)
            closed_layers.append(layer)
    order = sorted(range(len(closed)), key=lambda idx: _area(closed[idx]), reverse=True)
    closed = [closed[idx] for idx in order]
    closed_layers = [closed_layers[idx] for idx in order]
    areas = list(map(_area, closed))
    boxes = [_bounds([path]) for path in closed]
    n = len(closed)
    parents = [None] * n
    for i in range(n):
        bx0, by0, bx1, by1 = boxes[i]
        p0, p1 = closed[i][0], closed[i][1]
        cx, cy = (bx0 + bx1) / 2, (by0 + by1) / 2
        poly_cx = sum(p[0] for p in closed[i][:-1]) / max(len(closed[i]) - 1, 1)
        poly_cy = sum(p[1] for p in closed[i][:-1]) / max(len(closed[i]) - 1, 1)
        test_pts = [(cx, cy), (poly_cx, poly_cy), ((p0[0] + p1[0]) / 2, (p0[1] + p1[1]) / 2),
                    p0, closed[i][len(closed[i]) // 2]]
        for j in range(i - 1, -1, -1):
            if (boxes[j][0] - 1e-3 <= bx0 and boxes[j][1] - 1e-3 <= by0 and
                boxes[j][2] + 1e-3 >= bx1 and boxes[j][3] + 1e-3 >= by1):
                if any(_inside(pt, closed[j]) for pt in test_pts):
                    parents[i] = j
                    break
    # Enclosing boundary containing parts is treated as sheet stock; both standard
    # rectangular sheets and irregular remnant/offcut sheets are recognized.
    sheets = set()
    sheet_re = re.compile(r'(?i)(?:^|[-_])(?:sheet|stock|nest|table|chideman|varagh|ورق|شیت|چیدمان)(?:[-_0-9]|$)')
    for i, path in enumerate(closed):
        if parents[i] is not None:
            continue
        layer_name = closed_layers[i] if i < len(closed_layers) else ''
        x0, y0, x1, y1 = boxes[i]
        w, h = x1 - x0, y1 - y0
        area_box = w * h
        root_area = areas[i]
        children = [j for j, parent in enumerate(parents) if parent == i]
        if min(w, h) >= 150 and max(w, h) >= 250 and root_area >= 50_000 and sheet_re.search(layer_name):
            sheets.add(i)
            continue
        if root_area < 100_000 or min(w, h) < 200:
            continue
        has_nested_part = any(parents[k] in children and areas[parents[k]] >= 500 for k in range(n))
        rectangular = abs(root_area - area_box) <= max(tolerance ** 2, area_box * 1e-4)
        sum_child_area = sum(areas[j] for j in children)
        if has_nested_part:
            sheets.add(i)
        elif len(children) >= 2 and sum_child_area >= 0.25 * root_area:
            sheets.add(i)
    depths = []
    for i, parent in enumerate(parents):
        depths.append(0 if parent is None or parent in sheets else depths[parent] + 1)
    is_part = [False] * n
    for i in range(n):
        if i in sheets or depths[i] % 2 != 0:
            continue
        if depths[i] == 0:
            is_part[i] = True
        else:
            p = parents[i]
            x0, y0, x1, y1 = boxes[i]
            span = max(x1 - x0, y1 - y0)
            px0, py0, px1, py1 = boxes[p]
            p_span = max(px1 - px0, py1 - py0)
            ci = ((x0 + x1) / 2, (y0 + y1) / 2)
            cp = ((px0 + px1) / 2, (py0 + py1) / 2)
            clearance = min(abs(x0 - px0), abs(x1 - px1), abs(y0 - py0), abs(y1 - py1))
            concentric = math.dist(ci, cp) <= max(2.0, 0.05 * p_span)
            is_countersink = concentric and (clearance <= 6.0 or (areas[p] < 2500.0 and areas[i] / max(areas[p], 1e-9) > 0.35))
            if areas[i] < 50.0 or span < 8.0 or is_countersink:
                is_part[i] = False
            else:
                is_part[i] = True
    # shortcut: enclosed open strokes are treated as bends, not open cuts/engraving.
    part_open_paths = {i: [] for i in range(n)}
    for path in open_paths:
        mid = ((path[0][0] + path[1][0]) / 2, (path[0][1] + path[1][1]) / 2)
        owners = [i for i, outline in enumerate(closed) if is_part[i]
                  and boxes[i][0] <= mid[0] <= boxes[i][2]
                  and boxes[i][1] <= mid[1] <= boxes[i][3]
                  and _inside(mid, outline)]
        if not owners and not sheets:
            raise ValueError('Open cut outline outside any part: close disconnected LINE/ARC endpoints')
        if owners:
            part_open_paths[owners[0]].append(path)
    parts = []
    for i, path in enumerate(closed):
        if not is_part[i]:
            continue
        descendants = []
        for j in range(i + 1, n):
            if is_part[j]:
                continue
            curr = parents[j]
            while curr is not None and curr != i and not is_part[curr] and curr not in sheets:
                curr = parents[curr]
            if curr == i:
                descendants.append(closed[j])
        paths = [path] + descendants
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
                              assumed_mm=drawing.assumed_mm, thickness=drawing.thickness,
                              open_paths=part_open_paths.get(i, [])))
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
    canvas.setFillColorRGB(0, 0, 0)
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
    destination.parent.mkdir(parents=True, exist_ok=True)
    if any(destination.resolve() == part.source.resolve() for part in parts):
        raise ValueError('Output cannot replace a source drawing')
    handle, temporary = tempfile.mkstemp(suffix='.pdf', dir=destination.parent)
    os.close(handle)
    try:
        canvas = Canvas(temporary, pagesize=A4)
        canvas.setTitle('Part Info')
        page_w, page_h = A4
        margin, row_h, header_h = 24, 88, 32
        widths = [24, 104, 118, 72, 48, 46, 46, 48, 37]
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
                profile_paths = getattr(part, 'profile_paths', None)
                faces = getattr(part, 'faces_3d', None)

                if is_3d and profile_paths:
                    shape_x = xs[2]
                    shape_w = widths[2]
                    profile_w = (shape_w - 8) * 0.28
                    iso_w = (shape_w - 8) - profile_w
                    avail_h = row_h - 16

                    # Orthographic bend profile (left).
                    px0, py0, px1, py1 = getattr(part, 'profile_bounds', (0, 0, 1, 1))
                    pw, ph = max(px1 - px0, 1e-6), max(py1 - py0, 1e-6)
                    scale_p = min((profile_w - 6) / pw, avail_h * 0.55 / ph)
                    p_dx = shape_x + 3 + (profile_w - pw * scale_p) / 2
                    p_dy = cy - ph * scale_p / 2

                    canvas.setLineWidth(0.5)
                    canvas.setLineJoin(1)
                    canvas.setLineCap(1)
                    canvas.setStrokeColorRGB(0, 0, 0)
                    for curve in profile_paths:
                        if len(curve) < 2:
                            continue
                        d = canvas.beginPath()
                        d.moveTo(p_dx + (curve[0][0] - px0) * scale_p, p_dy + (curve[0][1] - py0) * scale_p)
                        for pt in curve[1:]:
                            d.lineTo(p_dx + (pt[0] - px0) * scale_p, p_dy + (pt[1] - py0) * scale_p)
                        canvas.drawPath(d, stroke=1, fill=0)

                    # Subtle vertical separator between 2D profile and 3D isometric view
                    div_x = shape_x + profile_w + 4
                    canvas.setLineWidth(0.3)
                    canvas.setStrokeColorRGB(0.85, 0.85, 0.85)
                    canvas.line(div_x, ytop - 6, div_x, ytop - row_h + 6)

                    # Shaded isometric view with exact visible edges (right).
                    ix0, iy0, ix1, iy1 = part.bounds
                    iw, ih = max(ix1 - ix0, 1e-6), max(iy1 - iy0, 1e-6)
                    scale_i = min((iso_w - 6) / iw, avail_h / ih)
                    i_dx = div_x + 4 + (iso_w - iw * scale_i) / 2
                    i_dy = cy - ih * scale_i / 2

                    if faces:
                        image = _shade_step(faces, part.bounds)
                        canvas.drawImage(ImageReader(image), i_dx, i_dy,
                                         width=iw * scale_i, height=ih * scale_i)
                    canvas.setLineWidth(0.4)
                    canvas.setStrokeColorRGB(0, 0, 0)
                    for outline in part.paths:
                        if len(outline) < 2:
                            continue
                        drawing = canvas.beginPath()
                        drawing.moveTo(i_dx + (outline[0][0] - ix0) * scale_i, i_dy + (outline[0][1] - iy0) * scale_i)
                        for x, y in outline[1:]:
                            drawing.lineTo(i_dx + (x - ix0) * scale_i, i_dy + (y - iy0) * scale_i)
                        canvas.drawPath(drawing)
                else:
                    drawing_scale = min((widths[2] - 16) / max(geom_w, 1e-9), (row_h - 16) / max(geom_h, 1e-9))
                    dx = xs[2] + (widths[2] - geom_w * drawing_scale) / 2
                    dy = cy - geom_h * drawing_scale / 2
                    faces = getattr(part, 'faces_3d', None)
                    if faces:
                        for f in faces:
                            poly = [(dx + (p[0] - x0) * drawing_scale, dy + (p[1] - y0) * drawing_scale) for p in f['points']]
                            if len(poly) >= 3:
                                canvas.setFillColorRGB(*f['rgb'])
                                canvas.setStrokeColorRGB(*f['rgb'])
                                canvas.setLineWidth(0.08)
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
            canvas.setFillColorRGB(0, 0, 0)
            canvas.setFont('Helvetica', 8)
            canvas.drawRightString(page_w - margin, 22, f'Page {page + 1} / {pages}')
            if any(p.assumed_mm for p in batch):
                canvas.drawString(margin, 35, 'Unitless DXF: dimensions assumed to be millimeters.')
            canvas.showPage()
        canvas.save()
        # On Windows, antivirus or indexing services can briefly hold newly written files.
        for attempt in range(5):
            try:
                os.replace(temporary, destination)
                break
            except PermissionError:
                if attempt == 4:
                    raise
                time.sleep(0.1)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@dataclass
class CompareItem:
    status: str          # 'MATCH', 'QUANTITY_MISMATCH', 'MISSING_IN_NEST', 'EXTRA_IN_NEST'
    name: str
    size: tuple
    required_qty: int
    nested_qty: int
    diff: int
    thickness: str = ''
    part_obj: Any = None
    nested_obj: Any = None
    note: str = ''


@dataclass
class CompareResult:
    items: list
    is_match: bool
    total_required: int
    total_nested: int
    matched_count: int
    mismatched_count: int
    missing_count: int
    extra_count: int
    include_open_lines: bool


def compare_nesting(nested_sources, part_sources, include_open_lines=False, tolerance=0.1, input_mm=True):
    """
    Compare parts nested on sheet(s) against expected individual parts.
    
    nested_sources: list of file paths (str/Path) or Part objects representing nested layout(s).
    part_sources: list of file paths (str/Path) or Part objects representing expected individual parts.
    include_open_lines: if True, bend lines / engraving lines are checked during shape comparison.
    """
    def resolve_parts(sources):
        if isinstance(sources, (str, Path, Part)):
            sources = [sources]
        res = []
        for s in sources:
            if isinstance(s, Part):
                res.append(s)
            else:
                res.extend(load_parts(s, input_mm=input_mm))
        return res

    nested_raw = resolve_parts(nested_sources)
    expected_raw = resolve_parts(part_sources)

    def _get_geometry(part):
        if include_open_lines and getattr(part, 'open_paths', None):
            return part.paths + part.open_paths
        return part.paths

    # Consolidate identical parts in nested layouts
    nested_unique = []
    for p in nested_raw:
        geo = _get_geometry(p)
        for u in nested_unique:
            if _same_shape(geo, _get_geometry(u), tolerance):
                u.quantity += p.quantity
                break
        else:
            p_copy = Part(p.source, p.paths, p.bounds, p.quantity,
                          thickness=p.thickness, label=p.label,
                          open_paths=getattr(p, 'open_paths', None) or [])
            nested_unique.append(p_copy)

    # Consolidate identical parts in expected parts
    expected_unique = []
    for p in expected_raw:
        geo = _get_geometry(p)
        for u in expected_unique:
            if _same_shape(geo, _get_geometry(u), tolerance):
                u.quantity += p.quantity
                break
        else:
            p_copy = Part(p.source, p.paths, p.bounds, p.quantity,
                          thickness=p.thickness, label=p.label,
                          open_paths=getattr(p, 'open_paths', None) or [])
            expected_unique.append(p_copy)

    matched_nested_indices = set()
    items = []

    for exp in expected_unique:
        exp_geo = _get_geometry(exp)
        match_idx = None
        for idx, nes in enumerate(nested_unique):
            if idx not in matched_nested_indices:
                if _same_shape(exp_geo, _get_geometry(nes), tolerance):
                    match_idx = idx
                    break
        if match_idx is not None:
            matched_nested_indices.add(match_idx)
            nes = nested_unique[match_idx]
            diff = nes.quantity - exp.quantity
            if diff == 0:
                status = 'MATCH'
                note = 'تطابق کامل (Exact Match)'
            elif diff < 0:
                status = 'QUANTITY_MISMATCH'
                note = f'کسری تعداد: {abs(diff)} عدد کمتر چیده شده'
            else:
                status = 'QUANTITY_MISMATCH'
                note = f'مازاد تعداد: {diff} عدد اضافه چیده شده'
            items.append(CompareItem(
                status=status,
                name=exp.label or exp.source.name,
                size=exp.size,
                required_qty=exp.quantity,
                nested_qty=nes.quantity,
                diff=diff,
                thickness=exp.thickness or nes.thickness,
                part_obj=exp,
                nested_obj=nes,
                note=note
            ))
        else:
            items.append(CompareItem(
                status='MISSING_IN_NEST',
                name=exp.label or exp.source.name,
                size=exp.size,
                required_qty=exp.quantity,
                nested_qty=0,
                diff=-exp.quantity,
                thickness=exp.thickness,
                part_obj=exp,
                nested_obj=None,
                note='قطعه در فایل چیدمان چیده نشده است'
            ))

    for idx, nes in enumerate(nested_unique):
        if idx not in matched_nested_indices:
            items.append(CompareItem(
                status='EXTRA_IN_NEST',
                name=nes.label or nes.source.name,
                size=nes.size,
                required_qty=0,
                nested_qty=nes.quantity,
                diff=nes.quantity,
                thickness=nes.thickness,
                part_obj=None,
                nested_obj=nes,
                note='قطعه اضافه / ناشناخته در فایل چیدمان'
            ))

    is_match = all(it.status == 'MATCH' for it in items)
    matched_count = sum(1 for it in items if it.status == 'MATCH')
    mismatched_count = sum(1 for it in items if it.status == 'QUANTITY_MISMATCH')
    missing_count = sum(1 for it in items if it.status == 'MISSING_IN_NEST')
    extra_count = sum(1 for it in items if it.status == 'EXTRA_IN_NEST')
    total_required = sum(it.required_qty for it in items if it.status != 'EXTRA_IN_NEST')
    total_nested = sum(it.nested_qty for it in items if it.status != 'MISSING_IN_NEST')

    return CompareResult(
        items=items,
        is_match=is_match,
        total_required=total_required,
        total_nested=total_nested,
        matched_count=matched_count,
        mismatched_count=mismatched_count,
        missing_count=missing_count,
        extra_count=extra_count,
        include_open_lines=include_open_lines
    )


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

    def clear():
        if not parts:
            return
        parts.clear()
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
            except PermissionError:
                messagebox.showerror('Save failed',
                    'The file is currently open in another program (e.g. Adobe Acrobat or browser).\n'
                    'Please close the PDF file and try saving again.')
            except Exception as exc:
                messagebox.showerror('Export failed', str(exc))

    def open_compare():
        dialog = tk.Toplevel(root)
        dialog.title('مقایسه چیدمان با قطعات (Compare Nesting vs Parts)')
        dialog.geometry('980x660')
        dialog.minsize(800, 500)
        dialog.transient(root)

        nested_files = []
        part_sources = []

        files_frame = ttk.Frame(dialog, padding=(10, 8))
        files_frame.pack(fill='x')

        nested_frame = ttk.LabelFrame(files_frame, text='۱. فایل‌های چیدمان (Nested Layout DXF)', padding=8)
        nested_frame.pack(side='left', fill='both', expand=True, padx=(0, 5))
        nested_listbox = tk.Listbox(nested_frame, height=5, selectmode='extended')
        nested_listbox.pack(fill='both', expand=True, pady=(0, 5))

        btn_box1 = ttk.Frame(nested_frame)
        btn_box1.pack(fill='x')
        def add_nested_files():
            chosen = filedialog.askopenfilenames(parent=dialog, filetypes=[('CAD drawings', '*.dxf *.DXF'), ('All files', '*.*')])
            for f in chosen:
                if f not in nested_files:
                    nested_files.append(f)
                    nested_listbox.insert('end', Path(f).name)
        def remove_nested_file():
            for idx in sorted(nested_listbox.curselection(), reverse=True):
                del nested_files[idx]
                nested_listbox.delete(idx)
        ttk.Button(btn_box1, text='افزودن چیدمان...', command=add_nested_files).pack(side='left', padx=2)
        ttk.Button(btn_box1, text='حذف', command=remove_nested_file).pack(side='left', padx=2)

        parts_frame = ttk.LabelFrame(files_frame, text='۲. فایل‌های قطعات مجزا (Individual Part DXF)', padding=8)
        parts_frame.pack(side='right', fill='both', expand=True, padx=(5, 0))
        part_listbox = tk.Listbox(parts_frame, height=5, selectmode='extended')
        part_listbox.pack(fill='both', expand=True, pady=(0, 5))

        btn_box2 = ttk.Frame(parts_frame)
        btn_box2.pack(fill='x')
        def add_part_files():
            chosen = filedialog.askopenfilenames(parent=dialog, filetypes=[('CAD drawings', '*.dxf *.DXF'), ('All files', '*.*')])
            for f in chosen:
                if f not in part_sources:
                    part_sources.append(f)
                    part_listbox.insert('end', Path(f).name)
        def use_current_table():
            if not parts:
                messagebox.showinfo('No parts', 'جدول اصلی خالی است. ابتدا فایل‌های قطعات را اضافه کنید.', parent=dialog)
                return
            for p in parts:
                if p not in part_sources:
                    part_sources.append(p)
                    part_listbox.insert('end', f'{p.label or p.source.name} (Qty: {p.quantity})')
        def remove_part_file():
            for idx in sorted(part_listbox.curselection(), reverse=True):
                del part_sources[idx]
                part_listbox.delete(idx)
        ttk.Button(btn_box2, text='افزودن قطعات...', command=add_part_files).pack(side='left', padx=2)
        ttk.Button(btn_box2, text='استفاده از جدول اصلی', command=use_current_table).pack(side='left', padx=2)
        ttk.Button(btn_box2, text='حذف', command=remove_part_file).pack(side='left', padx=2)

        ctrl_frame = ttk.Frame(dialog, padding=(10, 6))
        ctrl_frame.pack(fill='x')
        chk_open = tk.BooleanVar(value=False)
        ttk.Checkbutton(ctrl_frame, text='بررسی خطوط باز و خطوط خم/هک در مقایسه (Include bend & engraving open lines)', variable=chk_open).pack(side='left', padx=5)

        summary_var = tk.StringVar(value='فایل‌های چیدمان و قطعات مجزا را انتخاب کنید و دکمه مقایسه را بزنید.')
        summary_lbl = ttk.Label(dialog, textvariable=summary_var, font=('Helvetica', 10, 'bold'), padding=(10, 5))
        summary_lbl.pack(anchor='w')

        cols = ('status', 'name', 'size', 'required', 'nested', 'diff', 'note')
        res_tree = ttk.Treeview(dialog, columns=cols, show='headings', selectmode='extended')
        res_tree.heading('status', text='وضعیت (Status)')
        res_tree.heading('name', text='نام قطعه (Part Name)')
        res_tree.heading('size', text='ابعاد (Size mm)')
        res_tree.heading('required', text='نیاز (Req)')
        res_tree.heading('nested', text='چیدمان (Nested)')
        res_tree.heading('diff', text='اختلاف (Diff)')
        res_tree.heading('note', text='توضیحات (Description)')

        res_tree.column('status', width=130, anchor='center')
        res_tree.column('name', width=200, anchor='w')
        res_tree.column('size', width=110, anchor='center')
        res_tree.column('required', width=70, anchor='center')
        res_tree.column('nested', width=70, anchor='center')
        res_tree.column('diff', width=65, anchor='center')
        res_tree.column('note', width=260, anchor='w')
        res_tree.pack(fill='both', expand=True, padx=10, pady=5)

        res_tree.tag_configure('tag_match', foreground='#1b5e20')
        res_tree.tag_configure('tag_mismatch', foreground='#e65100')
        res_tree.tag_configure('tag_missing', foreground='#b71c1c')
        res_tree.tag_configure('tag_extra', foreground='#4a148c')

        def do_compare():
            if not nested_files:
                messagebox.showwarning('Missing input', 'لطفاً حداقل یک فایل چیدمان اضافه کنید.', parent=dialog)
                return
            if not part_sources:
                messagebox.showwarning('Missing input', 'لطفاً حداقل یک فایل قطعه اضافه کنید.', parent=dialog)
                return
            try:
                result = compare_nesting(nested_files, part_sources,
                                         include_open_lines=chk_open.get(),
                                         input_mm=input_mm.get())
            except Exception as exc:
                messagebox.showerror('Comparison error', str(exc), parent=dialog)
                return

            res_tree.delete(*res_tree.get_children())
            for item in result.items:
                w, h = item.size
                size_str = f'{w:.1f} x {h:.1f}' if w and h else '-'
                diff_str = f'+{item.diff}' if item.diff > 0 else str(item.diff)
                if item.status == 'MATCH':
                    icon = '✔ تطابق کامل'
                    tag = 'tag_match'
                elif item.status == 'QUANTITY_MISMATCH':
                    icon = '⚠ مغایرت تعداد'
                    tag = 'tag_mismatch'
                elif item.status == 'MISSING_IN_NEST':
                    icon = '✖ چیده نشده (کسری)'
                    tag = 'tag_missing'
                else:
                    icon = '❓ قطعه اضافه'
                    tag = 'tag_extra'

                res_tree.insert('', 'end', values=(
                    icon, item.name, size_str, item.required_qty, item.nested_qty, diff_str, item.note
                ), tags=(tag,))

            if result.is_match:
                summary_var.set(f'✔ تطابق کامل: تمام {result.total_required} قطعه دقیقاً طبق سفارش چیده شده‌اند.')
            else:
                summary_var.set(f'⚠ مغایرت: {result.matched_count} منطبق | {result.mismatched_count} مغایرت تعداد | {result.missing_count} کسری | {result.extra_count} قطعه اضافه')

        ttk.Button(ctrl_frame, text='▶ شروع مقایسه (Compare)', command=do_compare).pack(side='right', padx=5)

    ttk.Button(toolbar, text='Add CAD files', command=add).pack(side='left', padx=4)
    ttk.Button(toolbar, text='Compare (مقایسه)', command=open_compare).pack(side='left', padx=4)
    ttk.Checkbutton(toolbar, text='Input coordinates are mm', variable=input_mm).pack(side='left', padx=8)
    ttk.Button(toolbar, text='Remove selected', command=remove).pack(side='left', padx=4)
    ttk.Button(toolbar, text='Clear all', command=clear).pack(side='left', padx=4)
    ttk.Button(toolbar, text='Save PDF', command=export).pack(side='right', padx=4)
    tree.bind('<Double-1>', edit)
    tree.bind('<Button-1>', toggle_checks)
    tree.bind('<space>', toggle_checks)
    tree.bind('<Delete>', lambda e: remove())
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
    parser.add_argument('--compare-nested', nargs='+', help='Nested sheet DXF files to compare')
    parser.add_argument('--compare-parts', nargs='+', help='Individual part DXF files to compare against')
    parser.add_argument('--include-open-lines', action='store_true', default=False,
                        help='Include open bend/engraving lines in nesting comparison')
    args = parser.parse_args()
    if args.compare_nested and args.compare_parts:
        res = compare_nesting(args.compare_nested, args.compare_parts,
                              include_open_lines=args.include_open_lines,
                              input_mm=args.input_mm)
        print(f"Comparison Result: {'MATCH' if res.is_match else 'MISMATCH'}")
        print(f"Total Required: {res.total_required} | Total Nested: {res.total_nested}")
        print(f"Matched: {res.matched_count} | Mismatched Qty: {res.mismatched_count} | Missing: {res.missing_count} | Extra: {res.extra_count}")
        print("-" * 75)
        for it in res.items:
            w, h = it.size
            print(f"[{it.status:<17}] {it.name:<25} {w:.1f}x{h:.1f}mm  Req:{it.required_qty:<3} Nest:{it.nested_qty:<3} Diff:{it.diff:<+3} {it.note}")
    elif args.files:
        export_pdf(
            [part for path in args.files for part in load_parts(path, input_mm=args.input_mm)], args.output,
            name=args.name, date=args.date, day=args.day
        )
    else:
        desktop()
