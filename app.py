"""Batch DXF part reports. Run without arguments for the desktop application."""
import argparse
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
    nested: int = 0
    processed: int = 0
    assumed_mm: bool = False
    checked: bool = False

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
    values = (part.quantity, part.nested, part.processed)
    if any(type(value) is not int for value in values):
        raise ValueError('Counts must be whole numbers')
    if part.quantity < 1 or not 0 <= part.nested <= part.quantity or not 0 <= part.processed <= part.quantity:
        raise ValueError('Quantity must be positive; other counts must be between 0 and quantity')


def fitted_text(canvas, text, x, y, width, size=8):
    size = min(size, width / max(stringWidth(text, 'Helvetica', 1), 1))
    canvas.setFont('Helvetica', size)
    canvas.drawCentredString(x + width / 2, y, text)


def export_pdf(parts, destination):
    if not parts:
        raise ValueError('Add at least one DXF file')
    for part in parts:
        validate_counts(part)
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
        widths = [28, 124, 104, 91, 50, 50, 50, 50, 32]
        widths = [w * (page_w - 2 * margin) / sum(widths) for w in widths]
        xs = [margin]
        for width in widths:
            xs.append(xs[-1] + width)
        per_page = 7
        pages = math.ceil(len(parts) / per_page)
        headings = ['Order', 'Part Name', 'Thumbnail', 'Size (mm)', 'Parts Count', 'Nest Count', 'Remain Count', 'Processed', 'Check']
        for page in range(pages):
            canvas.setFont('Helvetica-Bold', 19)
            canvas.drawString(margin, page_h - 40, 'Part Info')
            top = page_h - 58
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
                values = [str(page * per_page + index + 1), part.source.stem, '',
                          f'{width:.2f} x {height:.2f}', str(part.quantity), str(part.nested),
                          str(part.quantity - part.nested), str(part.processed)]
                for col, value in enumerate(values):
                    fitted_text(canvas, value, xs[col] + 3, cy - 3, widths[col] - 6)
                canvas.acroForm.checkbox(
                    name=f'check_{page * per_page + index + 1}', tooltip=part.source.stem,
                    x=xs[8] + (widths[8] - 12) / 2, y=cy - 6, size=12,
                    checked=part.checked, buttonStyle='check', borderWidth=1,
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
            canvas.drawString(margin, 22, 'Remain Count = Parts Count - Nest Count')
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
    root.title('Noban - DXF to PDF')
    root.geometry('1050x560')
    parts = []
    toolbar = ttk.Frame(root, padding=10)
    toolbar.pack(fill='x')
    ttk.Label(root, text='Double-click a row to edit counts. Click Check (or press Space) to tick a part.', padding=10).pack(anchor='w')
    columns = ('name', 'size', 'quantity', 'nested', 'remaining', 'processed', 'checked')
    tree = ttk.Treeview(root, columns=columns, show='headings', selectmode='extended')
    for col, heading in zip(columns, ['Part Name', 'Size (mm)', 'Parts Count', 'Nest Count', 'Remain Count', 'Processed', 'Check']):
        tree.heading(col, text=heading)
        tree.column(col, width=250 if col == 'name' else 130, minwidth=70)
    tree.column('checked', width=70, stretch=False, anchor='center')
    tree.pack(fill='both', expand=True, padx=10, pady=5)
    status = tk.StringVar(value='Ready')
    ttk.Label(root, textvariable=status, padding=10).pack(anchor='w')

    def refresh():
        tree.delete(*tree.get_children())
        for i, part in enumerate(parts):
            w, h = part.size
            tree.insert('', 'end', iid=str(i), values=(part.source.name, f'{w:.2f} x {h:.2f}', part.quantity, part.nested, part.quantity - part.nested, part.processed, '\u2611' if part.checked else '\u2610'))
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
        if tree.identify_column(event.x) == '#7':
            return 'break'
        row = tree.identify_row(event.y)
        if not row:
            return
        part = parts[int(row)]
        dialog = tk.Toplevel(root)
        dialog.title('Edit counts')
        dialog.transient(root)
        dialog.grab_set()
        fields = []
        for i, (label, value) in enumerate(zip(['Parts Count', 'Nest Count', 'Processed'], [part.quantity, part.nested, part.processed])):
            ttk.Label(dialog, text=label, padding=8).grid(row=i, column=0)
            field = ttk.Entry(dialog)
            field.insert(0, str(value))
            field.grid(row=i, column=1, padx=8)
            fields.append(field)

        def save():
            try:
                q, n, p = (int(f.get()) for f in fields)
                candidate = Part(part.source, part.paths, part.bounds, q, n, p)
                validate_counts(candidate)
                part.quantity, part.nested, part.processed = q, n, p
            except ValueError as exc:
                messagebox.showerror('Invalid counts', str(exc), parent=dialog)
                return
            dialog.destroy()
            refresh()
        ttk.Button(dialog, text='Save', command=save).grid(row=3, column=1, pady=10)

    def toggle_checks(event):
        if event.keysym == 'space':
            rows = tree.selection()
        else:
            row = tree.identify_row(event.y)
            if not row or tree.identify_column(event.x) != '#7':
                return
            rows = (row,)
            tree.selection_set(row)
            tree.focus_set()
        for row in rows:
            part = parts[int(row)]
            part.checked = not part.checked
            tree.set(row, 'checked', '\u2611' if part.checked else '\u2610')
        return 'break'

    def export():
        if not parts:
            messagebox.showinfo('No parts', 'Add DXF files first.')
            return
        filename = filedialog.asksaveasfilename(defaultextension='.pdf', initialfile='Part Info.pdf', filetypes=[('PDF report', '*.pdf')])
        if filename:
            try:
                export_pdf(parts, filename)
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
    args = parser.parse_args()
    if args.files:
        export_pdf([load_part(path) for path in args.files], args.output)
    else:
        desktop()
