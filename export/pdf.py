"""WeasyPrint renderer of the timetable layout: one A4 landscape page."""

from html import escape

from weasyprint import HTML

from .layout import HEADER_BG, TITLE_BG

ROW_MM = 5.2
STRIP_MM = 1.4

CSS = f"""
@page {{ size: A4 landscape; margin: 8mm; }}
body {{ margin: 0; font-family: Calibri, Carlito, Helvetica, Arial, sans-serif; }}
.title {{ background: {TITLE_BG}; color: #000; font-weight: bold; font-size: 14pt;
  text-align: center; line-height: 1.15; padding: 1mm 0; }}
table {{ width: 100%; border-collapse: collapse; table-layout: fixed; }}
th, td {{ border: 0.5pt solid #000; padding: 0; font-size: 5.8pt; text-align: center;
  height: {ROW_MM}mm; line-height: {ROW_MM}mm; overflow: hidden; white-space: nowrap; }}
th {{ background: {HEADER_BG}; color: #fff; font-weight: bold; }}
td {{ font-weight: bold; }}
.col-day {{ width: 7mm; }}
.col-hour {{ width: 11mm; }}
td.hour {{ font-weight: normal; }}
td.day {{ vertical-align: top; color: #fff; font-style: italic; font-weight: normal;
  font-size: 8pt;
  line-height: 1; }}
td.day div {{ height: 6mm; line-height: 6mm; transform-origin: 0 0;
  text-align: center; }}
tr.strip td {{ background: #d9d9d9; height: {STRIP_MM}mm; line-height: {STRIP_MM}mm;
  padding: 0; font-size: 1pt; }}
"""


def _html(layout):
    out = [f"<style>{CSS}</style>"]
    out.append(
        '<div class="title">'
        + "<br>".join(escape(line) for line in layout.title_lines)
        + "</div><table><colgroup><col class='col-day'><col class='col-hour'>"
    )
    out.append("<col>" * len(layout.classes) + "</colgroup><thead><tr>")
    out.append("".join(f"<th>{escape(name)}</th>" for name in layout.header))
    out.append("</tr></thead><tbody>")
    width = 2 + len(layout.classes)
    for number, block in enumerate(layout.days):
        if number:
            out.append(f'<tr class="strip"><td colspan="{width}"></td></tr>')
        for index, row in enumerate(block.rows):
            out.append("<tr>")
            if index == 0:
                out.append(
                    f'<td class="day" rowspan="{len(block.rows)}" '
                    f'style="background:{block.background}">'
                    f'<div style="width:{len(block.rows) * ROW_MM}mm;'
                    f"transform:translateY({len(block.rows) * ROW_MM}mm) "
                    f'rotate(-90deg)">{escape(block.label)}</div></td>'
                )
            out.append(f'<td class="hour">{escape(row.label)}</td>')
            for cell in row.cells:
                weight = "" if cell.bold else "font-weight:normal;"
                out.append(
                    f'<td style="background:{cell.background};{weight}">'
                    f"{escape(cell.text)}</td>"
                )
            out.append("</tr>")
    out.append("</tbody></table>")
    return "<!doctype html><meta charset='utf-8'>" + "".join(out)


def render_pdf(layout):
    """The PDF bytes of `layout`; fixed row heights keep it on one page."""
    return HTML(string=_html(layout)).write_pdf()
