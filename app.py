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
from reportlab.lib.pagesizes import A4
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfgen.canvas import Canvas


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

    def __post_init__(self):
        if self.checked and not self.delivery_checked:
            self.delivery_checked = True
        elif self.delivery_checked and not self.checked:
            self.checked = True

    @property
    def size(self):
        x0, y0, x1, y1 = self.bounds
        return x1 - x0, y1 - y0


def load_part(filename):
    source = Path(filename)
    doc = ezdxf.readfile(source)
    scale = units.conversion_factor(doc.units, units.MM) if doc.units else 1
    paths = []
    skipped = set()
    # Text and dimensions annotate a part; they are not its cut geometry.
    annotations = {'TEXT', 'MTEXT', 'DIMENSION', 'LEADER', 'MLEADER', 'POINT', 'VIEWPORT'}
    for entity in recursive_decompose(doc.modelspace()):
        if entity.dxftype() in annotations:
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
    return Part(source, outlines, bounds, quantity, assumed_mm=not bool(doc.units))


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
                width, height = part.size
                item_order = page * per_page + index + 1
                values = [str(item_order), part.source.stem, '',
                          f'{width:.2f} x {height:.2f}', getattr(part, 'thickness', ''), str(part.quantity)]
                for col, value in enumerate(values):
                    fitted_text(canvas, value, xs[col] + 3, cy - 3, widths[col] - 6)
                canvas.acroForm.checkbox(
                    name=f'lazer_{item_order}', tooltip=f'{part.source.stem} - Lazer',
                    x=xs[6] + (widths[6] - 12) / 2, y=cy - 6, size=12,
                    checked=part.lazer_checked, buttonStyle='check', borderWidth=1,
                    forceBorder=True)
                canvas.acroForm.checkbox(
                    name=f'bending_{item_order}', tooltip=f'{part.source.stem} - Bending',
                    x=xs[7] + (widths[7] - 12) / 2, y=cy - 6, size=12,
                    checked=part.bending_checked, buttonStyle='check', borderWidth=1,
                    forceBorder=True)
                canvas.acroForm.checkbox(
                    name=f'delivery_{item_order}', tooltip=f'{part.source.stem} - Delivery',
                    x=xs[8] + (widths[8] - 12) / 2, y=cy - 6, size=12,
                    checked=part.delivery_checked, buttonStyle='check', borderWidth=1,
                    forceBorder=True)
                drawing_scale = min((widths[2] - 16) / max(width, 1e-9), (row_h - 16) / max(height, 1e-9))
                x0, y0, _, _ = part.bounds
                dx = xs[2] + (widths[2] - width * drawing_scale) / 2
                dy = cy - height * drawing_scale / 2
                canvas.setLineWidth(.45)
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

    ttk.Label(root, text='Double-click a row to edit quantity/thickness. Click Lazer, Bending, or Delivery to toggle checks.', padding=(10, 4)).pack(anchor='w')
    columns = ('name', 'size', 'thickness', 'quantity', 'lazer', 'bending', 'delivery')
    tree = ttk.Treeview(root, columns=columns, show='headings', selectmode='extended')
    headings_map = {
        'name': 'Part Name',
        'size': 'Size (mm)',
        'thickness': 'Thickness',
        'quantity': 'Parts Count',
        'lazer': 'Lazer Check',
        'bending': 'Bending Check',
        'delivery': 'Delivery'
    }
    for col in columns:
        tree.heading(col, text=headings_map[col])
        tree.column(col, width=240 if col == 'name' else (120 if col == 'size' else (75 if col in ('thickness', 'quantity') else 95)),
                    minwidth=50, anchor='w' if col in ('name', 'size') else 'center')
    tree.pack(fill='both', expand=True, padx=10, pady=5)
    status = tk.StringVar(value='Ready')
    ttk.Label(root, textvariable=status, padding=10).pack(anchor='w')

    def refresh():
        tree.delete(*tree.get_children())
        for i, part in enumerate(parts):
            w, h = part.size
            tree.insert('', 'end', iid=str(i), values=(
                part.source.name,
                f'{w:.2f} x {h:.2f}',
                part.thickness,
                part.quantity,
                '\u2611' if part.lazer_checked else '\u2610',
                '\u2611' if part.bending_checked else '\u2610',
                '\u2611' if part.delivery_checked else '\u2610'
            ))
        status.set(f'{len(parts)} parts loaded')

    def add():
        files = filedialog.askopenfilenames(filetypes=[('DXF drawings', '*.dxf *.DXF')])
        errors, assumptions = [], []
        for filename in files:
            try:
                part = load_part(filename)
                parts.append(part)
                if part.assumed_mm:
                    assumptions.append(part.source.name)
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
        if tree.identify_column(event.x) in ('#5', '#6', '#7'):
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
                candidate = Part(part.source, part.paths, part.bounds, q)
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
            part.lazer_checked = not part.lazer_checked
            tree.set(row, 'lazer', '\u2611' if part.lazer_checked else '\u2610')
            return 'break'
        elif col == '#6':
            part.bending_checked = not part.bending_checked
            tree.set(row, 'bending', '\u2611' if part.bending_checked else '\u2610')
            return 'break'
        elif col == '#7':
            part.delivery_checked = not part.delivery_checked
            part.checked = part.delivery_checked
            tree.set(row, 'delivery', '\u2611' if part.delivery_checked else '\u2610')
            return 'break'

    def export():
        if not parts:
            messagebox.showinfo('No parts', 'Add DXF files first.')
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

    ttk.Button(toolbar, text='Add DXF files', command=add).pack(side='left', padx=4)
    ttk.Button(toolbar, text='Remove selected', command=remove).pack(side='left', padx=4)
    ttk.Button(toolbar, text='Save PDF', command=export).pack(side='right', padx=4)
    tree.bind('<Double-1>', edit)
    tree.bind('<Button-1>', toggle_checks)
    tree.bind('<space>', toggle_checks)
    root.mainloop()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('files', nargs='*')
    parser.add_argument('-o', '--output', default='Part Info.pdf')
    parser.add_argument('--name', default='', help='Order / Customer name')
    parser.add_argument('--date', default='', help='Date for report header')
    parser.add_argument('--day', default='', help='Day for report header')
    args = parser.parse_args()
    if args.files:
        export_pdf(
            [load_part(path) for path in args.files], args.output,
            name=args.name, date=args.date, day=args.day
        )
    else:
        desktop()
