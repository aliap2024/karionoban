import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

import ezdxf
from app import load_part, load_parts, export_pdf, validate_counts


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
        self.assertIn('1.0.4.0', nsis)
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

        # 4. Verify make-installer.py placeholder coverage
        make_code = (root / 'make-installer.py').read_text(encoding='utf-8')
        self.assertIn('__APP_ICO_BASE64__', make_code)
        self.assertIn('__APP_PNG_BASE64__', make_code)

        # 5. Verify offline installer NSIS configuration
        nsi_code = (root / 'offline-installer.nsi').read_text(encoding='utf-8')
        self.assertIn('Name "لیزر مرکزی"', nsi_code)
        self.assertIn('OutFile "LaserMarkazi-Setup-1.0.4.exe"', nsi_code)

    def test_step_3d_file_loading_and_pdf_export(self):
        import subprocess
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            # 1. Test synthetic STEP file
            step_file = folder / 'bracket-3mm-q5.step'
            step_content = """ISO-10303-21;
HEADER;
FILE_DESCRIPTION((''),'2;1');
FILE_NAME('test.step','2026',(''),(''),'','','');
FILE_SCHEMA(('CONFIG_CONTROL_DESIGN'));
ENDSEC;
DATA;
#1 = CARTESIAN_POINT('', (0.0, 0.0, 0.0));
#2 = CARTESIAN_POINT('', (100.0, 0.0, 0.0));
#3 = CARTESIAN_POINT('', (100.0, 50.0, 10.0));
#4 = CARTESIAN_POINT('', (0.0, 50.0, 10.0));
#11 = VERTEX_POINT('', #1);
#12 = VERTEX_POINT('', #2);
#13 = VERTEX_POINT('', #3);
#14 = VERTEX_POINT('', #4);
#21 = LINE('', #1, #100);
#22 = LINE('', #2, #100);
#23 = LINE('', #3, #100);
#24 = LINE('', #4, #100);
#31 = EDGE_CURVE('', #11, #12, #21, .T.);
#32 = EDGE_CURVE('', #12, #13, #22, .T.);
#33 = EDGE_CURVE('', #13, #14, #23, .T.);
#34 = EDGE_CURVE('', #14, #11, #24, .T.);
ENDSEC;
END-ISO-10303-21;
"""
            step_file.write_text(step_content)
            parts = load_parts(step_file)
            self.assertEqual(len(parts), 1)
            part = parts[0]
            self.assertTrue(part.is_3d)
            self.assertEqual(part.quantity, 5)
            self.assertEqual(part.thickness, '3mm')
            self.assertEqual(len(part.paths), 4)

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
                self.assertEqual(len(sp.paths), 82)
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


if __name__ == '__main__':
    unittest.main()
