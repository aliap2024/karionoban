import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

import ezdxf
from app import load_part, load_parts, export_pdf, validate_counts, compare_nesting, CompareResult, CompareItem


class ReportTests(unittest.TestCase):
    def test_geometry_units_counts_and_pages(self):
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            drawing = ezdxf.new()
            drawing.units = ezdxf.units.IN
            drawing.modelspace().add_lwpolyline([(0, 0), (2, 0), (2, 1), (0, 1)], close=True)
            drawing.modelspace().add_circle((1, .5), .1)
            drawing.modelspace().add_text('Annotation', dxfattribs={'insert': (100, 100)})
            source = folder / 'plate-q4.DXF'
            drawing.saveas(source)
            part = load_part(source)
            self.assertEqual(part.quantity, 4)
            self.assertAlmostEqual(part.size[0], 50.8)
            self.assertAlmostEqual(part.size[1], 25.4)
            self.assertEqual(len(part.paths), 2)
            mm_part = load_part(source, input_mm=True)
            self.assertEqual(mm_part.size, (2, 1))
            self.assertEqual(mm_part.quantity, 4)
            output = folder / 'report.pdf'
            export_pdf([part] * 10, output)
            self.assertTrue(output.read_bytes().startswith(b'%PDF'))
            self.assertIn(b'/Count 2', output.read_bytes())
            part.quantity = 0
            with self.assertRaises(ValueError):
                export_pdf([part], output)
            self.assertTrue(output.read_bytes().startswith(b'%PDF'))

    def test_invalid_and_unitless_drawings(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / 'part.dxf'
            drawing = ezdxf.new()
            drawing.units = 0
            drawing.saveas(source)
            with self.assertRaises(ValueError):
                load_part(source)
            drawing.modelspace().add_circle((0, 0), 10)
            drawing.saveas(source)
            part = load_part(source)
            self.assertTrue(part.assumed_mm)
            self.assertAlmostEqual(part.size[0], 20)
            part.quantity = 0
            with self.assertRaises(ValueError):
                validate_counts(part)

    def test_sheet_and_scattered_rotated_parts(self):
        import math
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / 'sheet-q2.dxf'
            for sheet in (False, True):
                drawing = ezdxf.new()
                drawing.units = ezdxf.units.MM
                space = drawing.modelspace()
                if sheet:
                    space.add_lwpolyline([(-20, -20), (500, -20), (500, 500), (-20, 500)], close=True)
                for x, y, angle in [(0, 0, 0), (100, 100, .73), (200, 50, math.pi)]:
                    def transform(p):
                        a, b = p
                        return (x + a * math.cos(angle) - b * math.sin(angle),
                                y + a * math.sin(angle) + b * math.cos(angle))
                    corners = list(map(transform, [(0, 0), (30, 0), (30, 20), (0, 20)]))
                    if x == 0:
                        # Mixed directions and split entities still form one outline.
                        for a, b in zip(corners, corners[1:] + corners[:1]):
                            space.add_line(b, a)
                    else:
                        space.add_lwpolyline(corners, close=True)
                    space.add_circle(transform((5, 7)), 2)
                # Same outer dimensions, a different hole position: a distinct type.
                space.add_lwpolyline([(300, 0), (330, 0), (330, 20), (300, 20)], close=True)
                space.add_circle((310, 10), 2)
                drawing.saveas(source)
                parts = load_parts(source)
                self.assertEqual(sorted(p.quantity for p in parts), [2, 6])
                self.assertTrue(all(len(p.paths) == 2 for p in parts))
                self.assertEqual(len({p.label for p in parts}), 2)
                with self.assertRaises(ValueError):
                    load_part(source)
                export_pdf(parts, Path(folder) / 'sheet.pdf')

    def test_plate_with_two_holes_and_bend_line(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / 'plate.dxf'
            drawing = ezdxf.new()
            drawing.units = ezdxf.units.MM
            space = drawing.modelspace()
            space.add_lwpolyline([(0, 0), (200, 0), (200, 100), (0, 100)], close=True)
            for x in (20, 180):
                space.add_circle((x, 50), 3)
            space.add_line((100, 10), (100, 90))
            drawing.saveas(source)
            part = load_part(source)
            self.assertEqual(part.quantity, 1)
            self.assertEqual(part.size, (200, 100))
            self.assertEqual(len(part.paths), 3)

    def test_dashed_geometry_is_excluded_with_layer_and_block_styles(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / 'bends.dxf'
            drawing = ezdxf.new(setup=True)
            drawing.units = ezdxf.units.MM
            drawing.linetypes.new('CUSTOM_BEND', dxfattribs={'pattern': [5, 3, -2]})
            drawing.layers.new('FOLD', dxfattribs={'linetype': 'CUSTOM_BEND'})
            space = drawing.modelspace()
            space.add_lwpolyline([(0, 0), (100, 0), (100, 50), (0, 50)], close=True)
            space.add_circle((10, 10), 2)
            # A dashed closed loop is construction geometry, not another hole/part.
            space.add_circle((20, 20), 4, dxfattribs={'linetype': 'DASHED'})
            space.add_line((500, 500), (600, 500), dxfattribs={'layer': 'FOLD'})
            block = drawing.blocks.new('FOLD_MARKS')
            block.add_line((0, 0), (10, 0), dxfattribs={'linetype': 'BYBLOCK'})
            block.add_circle((0, 10), 3)  # BYLAYER on block layer 0 inherits INSERT layer.
            space.add_blockref('FOLD_MARKS', (1000, 1000), dxfattribs={'layer': 'FOLD'})
            drawing.saveas(source)
            part = load_part(source)
            self.assertEqual(part.quantity, 1)
            self.assertEqual(part.size, (100, 50))
            self.assertEqual(len(part.paths), 2)

    def test_open_bends_do_not_split_identical_parts_or_remove_split_boundaries(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / 'split.dxf'
            drawing = ezdxf.new()
            drawing.units = ezdxf.units.MM
            space = drawing.modelspace()
            # The bend is deliberately before the boundary's split LINE entities.
            space.add_line((0, 10), (40, 10))
            for x in (0, 100):
                corners = [(x, 0), (x + 40, 0), (x + 40, 20), (x, 20)]
                for a, b in zip(corners, corners[1:] + corners[:1]):
                    space.add_line(a, b)
                space.add_circle((x + 5, 5), 2)
            drawing.saveas(source)
            part = load_part(source)
            self.assertEqual(part.quantity, 2)
            self.assertEqual(part.size, (40, 20))
            self.assertEqual(len(part.paths), 2)

    def test_nested_island_and_open_outline(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / 'nested.dxf'
            drawing = ezdxf.new()
            drawing.units = ezdxf.units.MM
            space = drawing.modelspace()
            for radius in (30, 20, 5):
                space.add_circle((0, 0), radius)
            drawing.saveas(source)
            parts = load_parts(source)
            self.assertEqual(len(parts), 2)
            self.assertEqual(sorted(len(p.paths) for p in parts), [1, 2])
            space.add_line((100, 100), (110, 100))
            drawing.saveas(source)
            with self.assertRaisesRegex(ValueError, 'Open cut outline'):
                load_parts(source)

    def test_symmetric_boundary_with_asymmetric_holes_and_input_mm(self):
        from app import _same_shape
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / 'symmetric.dxf'
            drawing = ezdxf.new()
            drawing.units = ezdxf.units.IN  # Header says IN, but geometry is drawn in mm
            space = drawing.modelspace()
            # Two identical rectangular parts with 4 equidistant corners and an asymmetric hole
            for offset_x in (0, 300):
                space.add_lwpolyline([(offset_x - 100, -50), (offset_x + 100, -50),
                                      (offset_x + 100, 50), (offset_x - 100, 50)], close=True)
                # Asymmetric hole (only on the left side)
                space.add_circle((offset_x - 50, 0), 5)
            drawing.saveas(source)
            parts = load_parts(source, input_mm=True)
            self.assertEqual(len(parts), 1)
            self.assertEqual(parts[0].quantity, 2)
            self.assertAlmostEqual(parts[0].size[0], 200.0)
            self.assertAlmostEqual(parts[0].size[1], 100.0)
            p1 = parts[0].paths
            self.assertTrue(_same_shape(p1, p1, 0.1))

    def test_checkboxes_follow_parts_across_pages(self):
        from app import Part
        from reportlab.pdfbase.acroform import AcroForm
        with tempfile.TemporaryDirectory() as folder:
            parts = [
                Part(
                    Path(f'part-{i}.dxf'), [], (0, 0, 10, 20), 1,
                    lazer_checked=(i % 3 == 0),
                    bending_checked=(i % 3 == 1),
                    delivery_checked=(i % 3 == 2)
                ) for i in range(10)
            ]
            original = AcroForm.checkbox
            calls = []
            def record(form, *args, **kwargs):
                calls.append((kwargs['name'], kwargs['checked']))
                return original(form, *args, **kwargs)
            with patch.object(AcroForm, 'checkbox', record):
                export_pdf(parts, Path(folder) / 'checked.pdf')
            expected = [(f'delivery_{i + 1}', i % 3 == 2) for i in range(10)]
            self.assertEqual(calls, expected)

    def test_header_and_columns_structure(self):
        import base64
        import re
        import zlib
        from app import Part
        with tempfile.TemporaryDirectory() as folder:
            part = Part(Path('test-part.dxf'), [], (0, 0, 50, 50), 2)
            out_pdf = Path(folder) / 'header_test.pdf'
            export_pdf([part], out_pdf, name='Ali Custom', date='2026-11-02', day='Monday')
            pdf_bytes = out_pdf.read_bytes()
            self.assertTrue(pdf_bytes.startswith(b'%PDF'))

            # Extract and decompress page stream
            stream_match = re.search(rb'/Contents (\d+ 0 R)', pdf_bytes)
            self.assertIsNotNone(stream_match)
            stream_obj = stream_match.group(1).split()[0]
            obj_match = re.search(rb'%s 0 obj\s*<<.*?>>\s*stream\s*(.*?)\s*endstream' % stream_obj, pdf_bytes, re.DOTALL)
            self.assertIsNotNone(obj_match)
            decompressed = zlib.decompress(base64.a85decode(obj_match.group(1), adobe=True))

            # Verify header fields
            self.assertIn(b'Name:', decompressed)
            self.assertIn(b'Ali Custom', decompressed)
            self.assertIn(b'Date:', decompressed)
            self.assertIn(b'2026-11-02', decompressed)
            self.assertIn(b'Day:', decompressed)
            self.assertIn(b'Monday', decompressed)

            # Verify new column headers
            self.assertIn(b'Order', decompressed)
            self.assertIn(b'Part Name', decompressed)
            self.assertIn(b'Shape', decompressed)
            self.assertIn(b'Size \\(mm\\)', decompressed)
            self.assertIn(b'Thickness', decompressed)
            self.assertIn(b'Parts Count', decompressed)
            self.assertIn(b'Lazer Check', decompressed)
            self.assertIn(b'Bending Check', decompressed)
            self.assertIn(b'Delivery', decompressed)

            # Verify renamed and removed column headers are not present
            self.assertNotIn(b'Thumbnail', decompressed)
            self.assertNotIn(b'Nest Count', decompressed)
            self.assertNotIn(b'Remain Count', decompressed)
            self.assertNotIn(b'Processed', decompressed)

    def test_thickness_and_unchecked_rendering(self):
        import base64
        import re
        import zlib
        from app import Part
        from reportlab.pdfbase.acroform import AcroForm
        with tempfile.TemporaryDirectory() as folder:
            # 1. Test empty thickness and ensure NO checkboxes are created for lazer or bending
            part_empty = Part(Path('part-blank.dxf'), [], (0, 0, 40, 20), 3)
            out_pdf1 = Path(folder) / 'blank_test.pdf'
            calls = []
            original = AcroForm.checkbox
            def record(form, *args, **kwargs):
                calls.append(kwargs['name'])
                return original(form, *args, **kwargs)
            with patch.object(AcroForm, 'checkbox', record):
                export_pdf([part_empty], out_pdf1)
            # Only delivery checkbox exists, none for lazer or bending
            self.assertEqual(calls, ['delivery_1'])
            self.assertEqual(part_empty.thickness, '')

            # 2. Test populated thickness
            part_thick = Part(Path('part-thick.dxf'), [], (0, 0, 40, 20), 5, thickness='2.5mm')
            out_pdf2 = Path(folder) / 'thick_test.pdf'
            export_pdf([part_thick], out_pdf2)
            pdf_bytes = out_pdf2.read_bytes()
            stream_match = re.search(rb'/Contents (\d+ 0 R)', pdf_bytes)
            self.assertIsNotNone(stream_match)
            stream_obj = stream_match.group(1).split()[0]
            obj_match = re.search(rb'%s 0 obj\s*<<.*?>>\s*stream\s*(.*?)\s*endstream' % stream_obj, pdf_bytes, re.DOTALL)
            decompressed = zlib.decompress(base64.a85decode(obj_match.group(1), adobe=True))
            self.assertIn(b'2.5mm', decompressed)

    def test_installers_use_the_same_private_runtime_and_current_payload(self):
        import base64
        import re
        root = Path(__file__).resolve().parent
        template = (root / 'windows-install.ps1').read_text()
        # The base interpreter stays read-only; packages target the private venv.
        self.assertIn('PythonCore\\3.12\\InstallPath', template)
        self.assertIn('-m venv --without-pip $runtime', template)
        self.assertIn("$appPythonw = Join-Path $runtime 'Scripts\\pythonw.exe'", template)
        self.assertIn('$shortcut.TargetPath = $appPythonw', template)
        self.assertIn('Start-Process -FilePath $appPythonw', template)
        self.assertIn('-m pip --isolated --python $appPython install --disable-pip-version-check --no-index', template)
        nsis = (root / 'offline-installer.nsi').read_text()
        self.assertLess(template.index('Remove-OldAppFiles $desktopFolders $shell $true'),
                        template.index("$python = Find-BasePython"))
        self.assertLess(template.index('Application runtime check failed.'),
                        template.index("WriteAllBytes((Join-Path $installDir 'app.py')"))
        self.assertIn('foreach ($folder in @($desktop, $programs))', template)
        self.assertIn('NOBAN_SETUP_EXE', nsis)
        self.assertNotIn('CreateShortCut', nsis)
        self.assertIn('1.0.6.0', nsis)
        self.assertIn('Sysnative', nsis)
        for name in ('Install-Noban.cmd', 'Install-Noban-Offline.cmd', 'offline-install.ps1'):
            script = (root / name).read_text()
            for filename in ('app.py', 'requirements.txt', 'app.ico', 'app.png'):
                target = r'\$req' if filename == 'requirements.txt' else r"\(Join-Path \$installDir '" + re.escape(filename) + r"'\)"
                match = re.search(r"WriteAllBytes\(" + target +
                                  r", \[Convert\]::FromBase64String\('([^']+)'\)", script)
                self.assertIsNotNone(match, (name, filename))
                source = root / filename if filename.endswith(('.py', '.txt')) else root / 'assets' / filename
                self.assertEqual(base64.b64decode(match.group(1)), source.read_bytes())
            self.assertNotRegex(script, r'__[A-Z_]+__')
            self.assertIn('-m venv --without-pip', script)
            self.assertIn('$shortcut.TargetPath = $appPythonw', script)

    def test_branding_shortcut_and_icons(self):
        import base64
        root = Path(__file__).resolve().parent

        # 1. Verify asset files exist and have content
        ico_file = root / 'assets' / 'app.ico'
        png_file = root / 'assets' / 'app.png'
        self.assertTrue(ico_file.exists(), 'assets/app.ico does not exist')
        self.assertTrue(png_file.exists(), 'assets/app.png does not exist')
        self.assertGreater(ico_file.stat().st_size, 1000)
        self.assertGreater(png_file.stat().st_size, 1000)

        # 2. Verify window title in app.py
        app_code = (root / 'app.py').read_text(encoding='utf-8')
        self.assertIn("root.title('لیزر مرکزی - DXF to PDF')", app_code)
        self.assertIn("root.iconphoto", app_code)
        self.assertIn("root.iconbitmap", app_code)

        # 3. Verify Windows installer shortcut configuration
        ps1_code = (root / 'windows-install.ps1').read_text(encoding='utf-8')
        b64_title = '2YTbjNiy2LEg2YXYsdqp2LLbjA=='
        self.assertIn(b64_title, ps1_code)
        decoded = base64.b64decode(b64_title).decode('utf-8')
        self.assertEqual(decoded, 'لیزر مرکزی')
        self.assertIn("($shortcutTitle + '.lnk')", ps1_code)
        self.assertIn("app.ico", ps1_code)
        self.assertIn("$tempShortcutPath = Join-Path $env:TEMP", ps1_code)
        self.assertIn("Move-Item -LiteralPath $tempShortcutPath -Destination $shortcutPath -Force", ps1_code)
        self.assertIn("Description = 'Laser Markazi - DXF to PDF'", ps1_code)

        # 4. Verify make-installer.py placeholder coverage
        make_code = (root / 'make-installer.py').read_text(encoding='utf-8')
        self.assertIn('__APP_ICO_BASE64__', make_code)
        self.assertIn('__APP_PNG_BASE64__', make_code)

        # 5. Verify offline installer NSIS configuration
        nsi_code = (root / 'offline-installer.nsi').read_text(encoding='utf-8')
        self.assertIn('Name "لیزر مرکزی"', nsi_code)
        self.assertIn('OutFile "LaserMarkazi-Setup-1.0.6.exe"', nsi_code)

    def test_step_3d_file_loading_and_pdf_export(self):
        import subprocess
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            # 1. Test synthetic STEP file
            step_file = folder / 'bracket-3mm-q5.step'
            from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
            from OCP.IFSelect import IFSelect_RetDone
            from OCP.STEPControl import STEPControl_AsIs, STEPControl_Writer
            writer = STEPControl_Writer()
            writer.Transfer(BRepPrimAPI_MakeBox(100, 50, 10).Shape(), STEPControl_AsIs)
            self.assertEqual(writer.Write(str(step_file)), IFSelect_RetDone)
            parts = load_parts(step_file)
            self.assertEqual(len(parts), 1)
            part = parts[0]
            self.assertTrue(part.is_3d)
            self.assertEqual(part.quantity, 5)
            self.assertEqual(part.thickness, '3mm')
            self.assertGreaterEqual(len(part.paths), 9)
            self.assertEqual(part.size_3d, (100, 50, 10))

            # Export PDF and verify output
            pdf_out = folder / 'output.pdf'
            export_pdf(parts, pdf_out)
            self.assertTrue(pdf_out.exists())

            # 2. Test user's real STEP file if present
            sample_step = Path('/home/aliap/Downloads/18-gal-2mm-q2.STEP')
            if sample_step.exists():
                sample_parts = load_parts(sample_step)
                self.assertEqual(len(sample_parts), 1)
                sp = sample_parts[0]
                self.assertTrue(sp.is_3d)
                self.assertEqual(sp.quantity, 2)
                self.assertEqual(sp.thickness, '2mm')
                self.assertGreater(len(sp.paths), 0)
                self.assertEqual(sp.size, (0.0, 0.0))

                pdf_sample = folder / 'step_sample.pdf'
                export_pdf(sample_parts, pdf_sample)
                self.assertTrue(pdf_sample.exists())
                txt = subprocess.check_output(['pdftotext', str(pdf_sample), '-']).decode('utf-8')
                self.assertIn('18-gal-2mm-q2', txt)
                self.assertIn('2mm', txt)
                self.assertIn('-', txt)

    def test_step_dual_view_bend_profile_and_isometric(self):
        sample_01 = Path('/home/aliap/Downloads/noban/1405.08.12/01-gal-2mm-q2.STEP')
        sample_02 = Path('/home/aliap/Downloads/noban/1405.08.12/02-gal-2mm-q4.STEP')
        if sample_01.exists() and sample_02.exists():
            with tempfile.TemporaryDirectory() as folder:
                folder = Path(folder)
                parts_01 = load_parts(sample_01)
                self.assertEqual(len(parts_01), 1)
                p1 = parts_01[0]
                self.assertTrue(p1.is_3d)
                self.assertIsNotNone(p1.profile_paths)
                self.assertIsNotNone(p1.profile_bounds)
                self.assertIsNotNone(p1.faces_3d)
                # Profile span for U-bracket (approx 194 x 110)
                span_x1 = p1.profile_bounds[2] - p1.profile_bounds[0]
                span_y1 = p1.profile_bounds[3] - p1.profile_bounds[1]
                self.assertAlmostEqual(span_x1, 194.0, delta=1.0)
                self.assertAlmostEqual(span_y1, 110.0, delta=1.0)

                parts_02 = load_parts(sample_02)
                p2 = parts_02[0]
                self.assertTrue(p2.is_3d)
                self.assertIsNotNone(p2.profile_paths)
                self.assertIsNotNone(p2.profile_bounds)
                span_x2 = p2.profile_bounds[2] - p2.profile_bounds[0]
                span_y2 = p2.profile_bounds[3] - p2.profile_bounds[1]
                self.assertAlmostEqual(span_x2, 15.0, delta=1.0)
                self.assertAlmostEqual(span_y2, 25.6, delta=1.0)

                # Export multi-part dual-view PDF
                pdf_out = folder / 'dual_view_report.pdf'
                export_pdf([p1, p2], pdf_out)
                self.assertTrue(pdf_out.exists())
                self.assertGreater(pdf_out.stat().st_size, 5000)

    def test_step_shading_uses_pixel_depth_and_smooth_colors(self):
        import numpy as np
        from app import _shade_step
        points = [[0, 0], [1, 0], [0, 1]]
        front = {'points': points, 'z': [-1, 1, -1],
                 'vertex_rgb': [[1, 0, 0]] * 3}
        back = {'points': points, 'z': [0, 0, 0],
                'vertex_rgb': [[0, 0, 1]] * 3}
        first = np.asarray(_shade_step([front, back], (0, 0, 1, 1)))
        second = np.asarray(_shade_step([back, front], (0, 0, 1, 1)))
        np.testing.assert_array_equal(first, second)
        np.testing.assert_array_equal(first[899, 100], [0, 0, 255])
        np.testing.assert_array_equal(first[899, 800], [255, 0, 0])
        np.testing.assert_array_equal(first[100, 900], [255, 255, 255])
        gradient = dict(back, vertex_rgb=[[1, 0, 0], [0, 1, 0], [0, 0, 1]])
        rgb = np.asarray(_shade_step([gradient], (0, 0, 1, 1)))[666, 333]
        self.assertTrue(np.all((rgb >= 80) & (rgb <= 90)), rgb)

    def test_step_holes_hidden_edges_units_and_invalid_geometry(self):
        import numpy as np
        from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut
        from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox, BRepPrimAPI_MakeCylinder
        from OCP.IFSelect import IFSelect_RetDone
        from OCP.STEPControl import STEPControl_AsIs, STEPControl_Writer
        from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / 'plate-q2.step'
            box = BRepPrimAPI_MakeBox(40, 30, 2).Shape()
            hole = BRepPrimAPI_MakeCylinder(
                gp_Ax2(gp_Pnt(20, 15, -1), gp_Dir(0, 0, 1)), 4, 4).Shape()
            shape = BRepAlgoAPI_Cut(box, hole).Shape()
            writer = STEPControl_Writer()
            writer.Transfer(shape, STEPControl_AsIs)
            self.assertEqual(writer.Write(str(source)), IFSelect_RetDone)
            part = load_parts(source)[0]
            np.testing.assert_allclose(part.size_3d, (40, 30, 2), atol=1e-6)
            self.assertTrue(part.faces_3d)
            self.assertTrue(any(len(path) > 10 for path in part.paths))
            # Only three of the six box surfaces face the camera; hidden edges stay hidden.
            self.assertGreaterEqual(len(part.paths), 9)
            self.assertLess(len(part.paths), 20)
            self.assertEqual(len({tuple(tuple(p) for p in path) for path in part.profile_paths}),
                             len(part.profile_paths))
            self.assertTrue(all(np.isfinite(f['points']).all() for f in part.faces_3d))
            # A ray through the hole center must hit no shaded triangle.
            for face in part.faces_3d:
                tri = np.asarray(face['points'])
                edges = np.roll(tri, -1, axis=0) - tri
                sides = edges[:, 0] * -tri[:, 1] - edges[:, 1] * -tri[:, 0]
                self.assertFalse(np.all(sides > 1e-8) or np.all(sides < -1e-8))
            # STEP units must be converted by the reader, not guessed from the name.
            text = source.read_text()
            self.assertIn('SI_UNIT(.MILLI.,.METRE.)', text)
            source.write_text(text.replace('SI_UNIT(.MILLI.,.METRE.)', 'SI_UNIT($,.METRE.)'))
            scaled = load_parts(source)[0]
            np.testing.assert_allclose(scaled.size_3d, (40000, 30000, 2000), atol=1e-3)
            source.write_text('ISO-10303-21; invalid geometry')
            with self.assertRaises(ValueError):
                load_parts(source)

    def test_dense_sheet_nesting_multi_level_and_bend_lines(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / 'nested-4mm-q3.dxf'
            drawing = ezdxf.new()
            drawing.units = ezdxf.units.MM
            space = drawing.modelspace()

            # Sheet boundary: 1000 x 500 mm (area = 500,000)
            space.add_lwpolyline([(0, 0), (1000, 0), (1000, 500), (0, 500)], close=True)

            # Part 1 (Level 0): 800 x 400 mm (area = 320,000 -> 64% of sheet, > 50%!)
            space.add_lwpolyline([(50, 50), (850, 50), (850, 450), (50, 450)], close=True)
            # Open bend line inside Part 1
            space.add_line((100, 50), (100, 450))

            # Cutout in Part 1 (Level 1): 400 x 200 mm at (200, 150)
            space.add_lwpolyline([(200, 150), (600, 150), (600, 350), (200, 350)], close=True)

            # Part 2 (Level 2): Nested inside Cutout: 300 x 150 mm at (250, 180)
            space.add_lwpolyline([(250, 180), (550, 180), (550, 330), (250, 330)], close=True)
            # Open bend line inside Part 2
            space.add_line((300, 180), (300, 330))

            # Cutout in Part 2 (Level 3): 100 x 80 mm at (350, 220)
            space.add_lwpolyline([(350, 220), (450, 220), (450, 300), (350, 300)], close=True)

            # Part 3 (Level 4): Nested inside Cutout 2: 60 x 50 mm at (370, 235)
            space.add_lwpolyline([(370, 235), (430, 235), (430, 285), (370, 285)], close=True)
            # Circular hole in Part 3 (Level 5): r=5 at (400, 260)
            space.add_circle((400, 260), 5)

            drawing.saveas(source)
            parts = load_parts(source)
            self.assertEqual(len(parts), 3)
            self.assertTrue(all(p.quantity == 3 for p in parts))
            self.assertTrue(all(p.thickness == '4mm' for p in parts))

            # Verify sizes and hole counts
            sizes = sorted([p.size for p in parts], key=lambda s: s[0] * s[1])
            self.assertAlmostEqual(sizes[0][0], 60.0)
            self.assertAlmostEqual(sizes[0][1], 50.0)
            self.assertAlmostEqual(sizes[1][0], 300.0)
            self.assertAlmostEqual(sizes[1][1], 150.0)
            self.assertAlmostEqual(sizes[2][0], 800.0)
            self.assertAlmostEqual(sizes[2][1], 400.0)

            # Export report PDF
            pdf_out = Path(folder) / 'nested_report.pdf'
            export_pdf(parts, pdf_out)
            self.assertTrue(pdf_out.exists())

            # Test real Iranpour nested DXF if available
            real_dxf = Path('/home/aliap/Downloads/iranpour-05-07-05-4mm.dxf')
            if real_dxf.exists():
                iranpour_parts = load_parts(real_dxf, input_mm=True)
                self.assertEqual(len(iranpour_parts), 43)
                self.assertEqual(sum(p.quantity for p in iranpour_parts), 377)
                self.assertTrue(all(p.thickness == '4mm' for p in iranpour_parts))
                export_pdf(iranpour_parts, Path(folder) / 'iranpour.pdf')

    def test_clear_all_button_and_windows_export_resilience(self):
        root = Path(__file__).resolve().parent
        app_code = (root / 'app.py').read_text(encoding='utf-8')
        self.assertIn("ttk.Button(toolbar, text='Clear all', command=clear)", app_code)
        self.assertIn("parts.clear()", app_code)
        self.assertIn("tree.bind('<Delete>', lambda e: remove())", app_code)
        self.assertIn("destination.parent.mkdir(parents=True, exist_ok=True)", app_code)

        # Test saving to a non-existent sub-directory automatically creates it
        with tempfile.TemporaryDirectory() as folder:
            from app import Part
            part = Part(Path('test.dxf'), [[(0, 0), (10, 0), (10, 10), (0, 10), (0, 0)]], (0, 0, 10, 10), 1)
            nested_dest = Path(folder) / 'sub1' / 'sub2' / 'nested_export.pdf'
            export_pdf([part], nested_dest)
            self.assertTrue(nested_dest.exists())

    def test_remnant_sheet_nesting_and_meter_unit_scaling(self):
        with tempfile.TemporaryDirectory() as folder:
            # 1. Remnant L-shaped stock containing multiple parts with cutouts
            source = Path(folder) / 'remnant-ahan 1.5.dxf'
            drawing = ezdxf.new()
            drawing.units = ezdxf.units.MM
            space = drawing.modelspace()
            # L-shaped remnant sheet: 1000x800 with cutout corner
            space.add_lwpolyline([(0, 0), (1000, 0), (1000, 400), (500, 400), (500, 800), (0, 800)], close=True)
            # Part 1: 300 x 200 mm with hole at (100, 100)
            space.add_lwpolyline([(50, 50), (350, 50), (350, 250), (50, 250)], close=True)
            space.add_circle((150, 150), 10)
            # Part 2: 350 x 250 mm with hole at (150, 550)
            space.add_lwpolyline([(50, 450), (400, 450), (400, 700), (50, 700)], close=True)
            space.add_circle((150, 550), 15)
            drawing.saveas(source)

            parts = load_parts(source)
            self.assertEqual(len(parts), 2)
            self.assertEqual(parts[0].thickness, '1.5mm')
            self.assertEqual(parts[1].thickness, '1.5mm')
            sizes = sorted([p.size for p in parts], key=lambda s: s[0] * s[1])
            self.assertAlmostEqual(sizes[0][0], 300.0)
            self.assertAlmostEqual(sizes[0][1], 200.0)
            self.assertAlmostEqual(sizes[1][0], 350.0)
            self.assertAlmostEqual(sizes[1][1], 250.0)

            # 2. Test auto-recovery from misconfigured $INSUNITS=6 (Meters) on mm coordinates
            m_source = Path(folder) / 'plate-steel 2.5-q4.dxf'
            m_drawing = ezdxf.new()
            m_drawing.units = ezdxf.units.M  # Header says meters
            m_space = m_drawing.modelspace()
            m_space.add_lwpolyline([(0, 0), (400, 0), (400, 250), (0, 250)], close=True)
            m_space.add_circle((100, 100), 10)
            m_drawing.saveas(m_source)

            m_parts = load_parts(m_source)
            self.assertEqual(len(m_parts), 1)
            self.assertEqual(m_parts[0].quantity, 4)
            self.assertEqual(m_parts[0].thickness, '2.5mm')
            self.assertAlmostEqual(m_parts[0].size[0], 400.0)
            self.assertAlmostEqual(m_parts[0].size[1], 250.0)

            # 3. Test real nested DXF yarmahmoodi if available in Downloads or workspace
            for candidate in (Path('/home/aliap/Downloads/yarmahmoodi-ahan 1.5 full.dxf'),
                              Path(__file__).resolve().parent / 'yarmahmoodi-ahan 1.5 full.dxf'):
                if candidate.exists():
                    y_parts = load_parts(candidate)
                    self.assertEqual(len(y_parts), 25)
                    self.assertEqual(sum(p.quantity for p in y_parts), 358)
                    self.assertEqual(y_parts[0].thickness, '1.5mm')
                    out_pdf = Path(folder) / 'yarmahmoodi.pdf'
                    export_pdf(y_parts, out_pdf)
                    self.assertTrue(out_pdf.exists())
                    break

    def test_plate_with_multiple_nonround_cutouts_not_detected_as_sheet(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / 'plate_with_slots-3mm.dxf'
            drawing = ezdxf.new()
            drawing.units = ezdxf.units.MM
            space = drawing.modelspace()
            # 200 x 100 mm plate
            space.add_lwpolyline([(0, 0), (200, 0), (200, 100), (0, 100)], close=True)
            # 4 non-round rectangular cutouts (e.g. slots/connectors)
            for x, y in [(20, 20), (20, 60), (120, 20), (120, 60)]:
                space.add_lwpolyline([(x, y), (x + 40, y), (x + 40, y + 20), (x, y + 20)], close=True)
            drawing.saveas(source)

            parts = load_parts(source)
            self.assertEqual(len(parts), 1)
            self.assertAlmostEqual(parts[0].size[0], 200.0)
            self.assertAlmostEqual(parts[0].size[1], 100.0)
            self.assertEqual(len(parts[0].paths), 5)  # 1 outer + 4 cutouts

    def test_concentric_countersunk_hole_not_treated_as_part(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / 'countersink_plate.dxf'
            drawing = ezdxf.new()
            drawing.units = ezdxf.units.MM
            space = drawing.modelspace()
            # 150 x 150 mm plate
            space.add_lwpolyline([(0, 0), (150, 0), (150, 150), (0, 150)], close=True)
            # Hole of radius 8 and concentric countersunk circle of radius 6 at center (75, 75)
            space.add_circle((75, 75), 8)
            space.add_circle((75, 75), 6)
            drawing.saveas(source)

            parts = load_parts(source)
            # Must remain 1 part, not extract a phantom 6mm coin or washer
            self.assertEqual(len(parts), 1)
            self.assertAlmostEqual(parts[0].size[0], 150.0)
            self.assertAlmostEqual(parts[0].size[1], 150.0)
            self.assertEqual(len(parts[0].paths), 3)

    def test_letter_cutout_with_inner_island_retained_in_part(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / 'sign_plate.dxf'
            drawing = ezdxf.new()
            drawing.units = ezdxf.units.MM
            space = drawing.modelspace()
            # 250 x 80 mm plate
            space.add_lwpolyline([(0, 0), (250, 0), (250, 80), (0, 80)], close=True)
            # Letter 'O' cutout: outer loop (r=8) and inner counter loop (r=1.5)
            space.add_circle((50, 40), 8)
            space.add_circle((50, 40), 1.5)
            # Two regular mounting slots
            space.add_lwpolyline([(120, 30), (160, 30), (160, 50), (120, 50)], close=True)
            space.add_lwpolyline([(180, 30), (220, 30), (220, 50), (180, 50)], close=True)
            drawing.saveas(source)

            parts = load_parts(source)
            self.assertEqual(len(parts), 1)
            self.assertAlmostEqual(parts[0].size[0], 250.0)
            self.assertAlmostEqual(parts[0].size[1], 80.0)
            self.assertEqual(len(parts[0].paths), 5)

    def test_unlisted_entity_layer_does_not_crash(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / 'unlisted_layer.dxf'
            drawing = ezdxf.new()
            drawing.units = ezdxf.units.MM
            space = drawing.modelspace()
            # Add entity with layer that does not exist in drawing.layers table
            line = space.add_lwpolyline([(0, 0), (100, 0), (100, 50), (0, 50)], close=True)
            line.dxf.layer = 'UNDECLARED_CUSTOM_LAYER'
            drawing.saveas(source)

            parts = load_parts(source)
            self.assertEqual(len(parts), 1)
            self.assertAlmostEqual(parts[0].size[0], 100.0)
            self.assertAlmostEqual(parts[0].size[1], 50.0)

    def test_real_sample_boresh_lezer_and_three_sheets_if_present(self):
        boresh = Path('/home/aliap/boresh_lezer_markazi.dxf')
        if boresh.exists():
            parts = load_parts(boresh)
            self.assertEqual(len(parts), 1)
            self.assertAlmostEqual(parts[0].size[0], 300.0)
            self.assertAlmostEqual(parts[0].size[1], 65.0)
            self.assertEqual(len(parts[0].paths), 31)

        three_sheets = Path('/home/aliap/kargah/samples/three-ready-sheets-with-parts.dxf')
        if three_sheets.exists():
            parts = load_parts(three_sheets)
            self.assertEqual(len(parts), 3)

    def test_compare_nesting_exact_match(self):
        from app import Part
        # Expected individual parts: A (qty 4) and B (qty 6)
        part_a = Part(Path('partA.dxf'), [[(0, 0), (100, 0), (100, 50), (0, 50), (0, 0)]], (0, 0, 100, 50), 4)
        part_b = Part(Path('partB.dxf'), [[(0, 0), (80, 0), (80, 80), (0, 80), (0, 0)]], (0, 0, 80, 80), 6)

        # Nested parts: identical shapes and quantities (even rotated)
        nes_a = Part(Path('nested_sheet.dxf'), [[(0, 0), (0, 100), (-50, 100), (-50, 0), (0, 0)]], (-50, 0, 0, 100), 4)
        nes_b = Part(Path('nested_sheet.dxf'), [[(10, 10), (90, 10), (90, 90), (10, 90), (10, 10)]], (10, 10, 90, 90), 6)

        result = compare_nesting([nes_a, nes_b], [part_a, part_b])
        self.assertTrue(result.is_match)
        self.assertEqual(result.total_required, 10)
        self.assertEqual(result.total_nested, 10)
        self.assertEqual(result.matched_count, 2)
        self.assertEqual(result.mismatched_count, 0)
        self.assertEqual(result.missing_count, 0)
        self.assertEqual(result.extra_count, 0)
        self.assertTrue(all(it.status == 'MATCH' and it.diff == 0 for it in result.items))

    def test_compare_nesting_mismatch_missing_and_extra(self):
        from app import Part
        # Expected parts:
        # Part A (req: 10), Part B (req: 5)
        part_a = Part(Path('partA.dxf'), [[(0, 0), (100, 0), (100, 50), (0, 50), (0, 0)]], (0, 0, 100, 50), 10)
        part_b = Part(Path('partB.dxf'), [[(0, 0), (80, 0), (80, 80), (0, 80), (0, 0)]], (0, 0, 80, 80), 5)

        # Nested layout:
        # Part A (nested only 8 -> shortage -2)
        # Part C (nested 3 -> unexpected extra)
        # Part B is missing completely!
        nes_a = Part(Path('sheet.dxf'), [[(0, 0), (100, 0), (100, 50), (0, 50), (0, 0)]], (0, 0, 100, 50), 8)
        nes_c = Part(Path('sheet.dxf'), [[(0, 0), (30, 0), (30, 30), (0, 30), (0, 0)]], (0, 0, 30, 30), 3)

        result = compare_nesting([nes_a, nes_c], [part_a, part_b])
        self.assertFalse(result.is_match)
        self.assertEqual(result.matched_count, 0)
        self.assertEqual(result.mismatched_count, 1)  # Part A
        self.assertEqual(result.missing_count, 1)     # Part B
        self.assertEqual(result.extra_count, 1)       # Part C

        item_map = {it.name: it for it in result.items}
        self.assertEqual(item_map['partA.dxf'].status, 'QUANTITY_MISMATCH')
        self.assertEqual(item_map['partA.dxf'].required_qty, 10)
        self.assertEqual(item_map['partA.dxf'].nested_qty, 8)
        self.assertEqual(item_map['partA.dxf'].diff, -2)

        self.assertEqual(item_map['partB.dxf'].status, 'MISSING_IN_NEST')
        self.assertEqual(item_map['partB.dxf'].required_qty, 5)
        self.assertEqual(item_map['partB.dxf'].nested_qty, 0)
        self.assertEqual(item_map['partB.dxf'].diff, -5)

        self.assertEqual(item_map['sheet.dxf'].status, 'EXTRA_IN_NEST')
        self.assertEqual(item_map['sheet.dxf'].required_qty, 0)
        self.assertEqual(item_map['sheet.dxf'].nested_qty, 3)
        self.assertEqual(item_map['sheet.dxf'].diff, 3)

    def test_compare_nesting_open_bend_lines_toggle(self):
        from app import Part
        # Individual part has bend line at x=30
        part_with_bend = Part(Path('bracket.dxf'),
                              [[(0, 0), (100, 0), (100, 50), (0, 50), (0, 0)]],
                              (0, 0, 100, 50), 2,
                              open_paths=[[(30, 0), (30, 50)]])

        # Nested part on sheet has the same 100x50 outline, but bend line at x=40
        nes_diff_bend = Part(Path('nest_sheet.dxf'),
                             [[(0, 0), (100, 0), (100, 50), (0, 50), (0, 0)]],
                             (0, 0, 100, 50), 2,
                             open_paths=[[(40, 0), (40, 50)]])

        # Nested part with EXACT same bend line (at x=30)
        nes_exact_bend = Part(Path('nest_sheet.dxf'),
                              [[(0, 0), (100, 0), (100, 50), (0, 50), (0, 0)]],
                              (0, 0, 100, 50), 2,
                              open_paths=[[(30, 0), (30, 50)]])

        # 1. When include_open_lines=False (checkbox OFF):
        # Bend lines are ignored, so closed cut boundaries match!
        res_ignore_bends = compare_nesting([nes_diff_bend], [part_with_bend], include_open_lines=False)
        self.assertTrue(res_ignore_bends.is_match)
        self.assertEqual(res_ignore_bends.matched_count, 1)

        # 2. When include_open_lines=True (checkbox ON):
        # Bend lines MUST match! With bend at 40 vs 30, it fails to match!
        res_check_bends_diff = compare_nesting([nes_diff_bend], [part_with_bend], include_open_lines=True)
        self.assertFalse(res_check_bends_diff.is_match)
        self.assertEqual(res_check_bends_diff.missing_count, 1)
        self.assertEqual(res_check_bends_diff.extra_count, 1)

        # 3. When include_open_lines=True with EXACT same bend line:
        res_check_bends_match = compare_nesting([nes_exact_bend], [part_with_bend], include_open_lines=True)
        self.assertTrue(res_check_bends_match.is_match)
        self.assertEqual(res_check_bends_match.matched_count, 1)

    def test_compare_nesting_from_dxf_files(self):
        with tempfile.TemporaryDirectory() as folder:
            f = Path(folder)
            # Create nested layout with 2 parts on a sheet: 5x Part A, 3x Part B
            nest_file = f / 'nest_sheet.dxf'
            dwg = ezdxf.new()
            dwg.units = ezdxf.units.MM
            sp = dwg.modelspace()
            # Sheet border
            sp.add_lwpolyline([(0, 0), (1000, 0), (1000, 500), (0, 500)], close=True, dxfattribs={'layer': 'SHEET_1'})
            # 5 copies of 100x50 part
            for i in range(5):
                sp.add_lwpolyline([(50 + i * 120, 50), (150 + i * 120, 50), (150 + i * 120, 100), (50 + i * 120, 100)], close=True)
            # 3 copies of 80x80 part
            for i in range(3):
                sp.add_lwpolyline([(50 + i * 100, 200), (130 + i * 100, 200), (130 + i * 100, 280), (50 + i * 100, 280)], close=True)
            dwg.saveas(nest_file)

            # Create individual part files:
            # partA-q5.dxf
            part_a_file = f / 'partA-q5.dxf'
            dwg_a = ezdxf.new()
            dwg_a.units = ezdxf.units.MM
            dwg_a.modelspace().add_lwpolyline([(0, 0), (100, 0), (100, 50), (0, 50)], close=True)
            dwg_a.saveas(part_a_file)

            # partB-q3.dxf
            part_b_file = f / 'partB-q3.dxf'
            dwg_b = ezdxf.new()
            dwg_b.units = ezdxf.units.MM
            dwg_b.modelspace().add_lwpolyline([(0, 0), (80, 0), (80, 80), (0, 80)], close=True)
            dwg_b.saveas(part_b_file)

            # Run comparison directly using file paths
            result = compare_nesting([nest_file], [part_a_file, part_b_file])
            self.assertTrue(result.is_match)
            self.assertEqual(result.total_required, 8)
            self.assertEqual(result.total_nested, 8)
            self.assertEqual(result.matched_count, 2)


if __name__ == '__main__':
    unittest.main()
