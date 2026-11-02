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
            part.nested = 5
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
            parts = [Part(Path(f'part-{i}.dxf'), [], (0, 0, 10, 20), 1, checked=(i % 2 == 0)) for i in range(10)]
            original = AcroForm.checkbox
            calls = []
            def record(form, *args, **kwargs):
                calls.append((kwargs['name'], kwargs['checked']))
                return original(form, *args, **kwargs)
            with patch.object(AcroForm, 'checkbox', record):
                export_pdf(parts, Path(folder) / 'checked.pdf')
            self.assertEqual(calls, [(f'check_{i + 1}', i % 2 == 0) for i in range(10)])


if __name__ == '__main__':
    unittest.main()
