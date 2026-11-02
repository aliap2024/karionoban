import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

import ezdxf
from app import load_part, export_pdf, validate_counts


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
            expected = []
            for i in range(10):
                expected.extend([
                    (f'lazer_{i + 1}', i % 3 == 0),
                    (f'bending_{i + 1}', i % 3 == 1),
                    (f'delivery_{i + 1}', i % 3 == 2)
                ])
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
            self.assertIn(b'Thumbnail', decompressed)
            self.assertIn(b'Size \\(mm\\)', decompressed)
            self.assertIn(b'Parts Count', decompressed)
            self.assertIn(b'Lazer Check', decompressed)
            self.assertIn(b'Bending Check', decompressed)
            self.assertIn(b'Delivery', decompressed)

            # Verify removed column headers are not present
            self.assertNotIn(b'Nest Count', decompressed)
            self.assertNotIn(b'Remain Count', decompressed)
            self.assertNotIn(b'Processed', decompressed)

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


if __name__ == '__main__':
    unittest.main()
