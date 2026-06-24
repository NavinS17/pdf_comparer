"""
highlight.py
------------
Annotates a copy of PDF 2 with coloured underlines (PyMuPDF) and generates
a ReportLab comparison report PDF.
"""

import io
import fitz
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib import colors
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import cm


ANNOTATE_TIME_BUDGET = 55   # seconds — annotate as many pages as possible


def build_highlighted_pdf(pdf2_bytes, highlights_per_page):
    import tempfile, os, time

    sorted_pages = sorted(highlights_per_page.keys())
    tmp_in_path = None

    try:
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
            f.write(pdf2_bytes)
            tmp_in_path = f.name

        doc = fitz.open(tmp_in_path)
        n_pages = len(doc)
        deadline = time.time() + ANNOTATE_TIME_BUDGET
        pages_done = 0

        for page_idx in sorted_pages:
            if time.time() >= deadline:
                break                    # time up — save what we have so far
            if page_idx >= n_pages:
                continue
            page = doc[page_idx]
            for rect, color in highlights_per_page[page_idx]:
                annot = page.add_underline_annot(rect)
                annot.set_colors(stroke=color)
                annot.update()
            pages_done += 1

        doc.saveIncr()   # append-only — much faster than full rewrite
        doc.close()

        with open(tmp_in_path, "rb") as f:
            return f.read(), pages_done, len(sorted_pages)

    except Exception:
        return pdf2_bytes, 0, len(sorted_pages)   # fallback: original PDF2
    finally:
        if tmp_in_path:
            try:
                os.unlink(tmp_in_path)
            except OSError:
                pass


def build_report_pdf(report_rows, pdf1_name, pdf2_name, summary):
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=landscape(A4),
        rightMargin=1.5 * cm,
        leftMargin=1.5 * cm,
        topMargin=2 * cm,
        bottomMargin=1.5 * cm,
    )

    styles = getSampleStyleSheet()
    title_sty = ParagraphStyle("title", parent=styles["Heading1"], fontSize=18, spaceAfter=4)
    sub_sty   = ParagraphStyle("sub",   parent=styles["Normal"],   fontSize=10,
                               textColor=colors.grey, spaceAfter=10)
    cell_sty  = ParagraphStyle("cell",  parent=styles["Normal"],   fontSize=8, leading=11)

    story = [
        Paragraph("PDF Comparison Report", title_sty),
        Paragraph(
            f"PDF 1 (original): <b>{pdf1_name}</b> &nbsp;|&nbsp; "
            f"PDF 2 (modified): <b>{pdf2_name}</b>", sub_sty,
        ),
        Paragraph(
            f"<font color='green'>Inserted: {summary['inserted']}</font> &nbsp; "
            f"<font color='orange'>Modified: {summary['modified']}</font> &nbsp; "
            f"<font color='red'>Deleted: {summary['deleted']}</font>", sub_sty,
        ),
        Spacer(1, 0.3 * cm),
    ]

    col_w = [1.8 * cm, 11.5 * cm, 11.5 * cm, 3.2 * cm]
    header = [
        Paragraph("<b>Page</b>", cell_sty),
        Paragraph(f"<b>PDF 1 — {pdf1_name[:45]}</b>", cell_sty),
        Paragraph(f"<b>PDF 2 — {pdf2_name[:45]}</b>", cell_sty),
        Paragraph("<b>Change Type</b>", cell_sty),
    ]
    table_data = [header]

    bg_map = {
        "Inserted": colors.Color(0.18, 0.66, 0.22, 0.15),
        "Deleted":  colors.Color(0.86, 0.20, 0.20, 0.15),
        "Modified": colors.Color(0.90, 0.72, 0.00, 0.15),
    }
    fg_map = {"Inserted": "#1c8c25", "Deleted": "#cc2222", "Modified": "#b37700"}
    row_bgs = []

    for i, row in enumerate(report_rows):
        ct = row["change_type"]
        table_data.append([
            Paragraph(row["page"], cell_sty),
            Paragraph(str(row["pdf1_text"])[:600], cell_sty),
            Paragraph(str(row["pdf2_text"])[:600], cell_sty),
            Paragraph(f'<font color="{fg_map[ct]}"><b>{ct}</b></font>', cell_sty),
        ])
        row_bgs.append((i + 1, bg_map[ct]))

    style_cmds = [
        ("BACKGROUND",    (0, 0), (-1, 0),  colors.HexColor("#2d2d2d")),
        ("TEXTCOLOR",     (0, 0), (-1, 0),  colors.white),
        ("FONTSIZE",      (0, 0), (-1, -1), 8),
        ("ROWBACKGROUNDS",(0, 1), (-1, -1), [colors.white, colors.HexColor("#F7F7F7")]),
        ("GRID",          (0, 0), (-1, -1), 0.4, colors.lightgrey),
        ("VALIGN",        (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING",   (0, 0), (-1, -1), 5),
        ("RIGHTPADDING",  (0, 0), (-1, -1), 5),
        ("TOPPADDING",    (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]
    for ri, bg in row_bgs:
        style_cmds.append(("BACKGROUND", (0, ri), (-1, ri), bg))

    table = Table(table_data, colWidths=col_w, repeatRows=1)
    table.setStyle(TableStyle(style_cmds))
    story.append(table)
    doc.build(story)
    return buf.getvalue()
