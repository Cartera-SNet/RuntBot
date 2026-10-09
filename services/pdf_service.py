"""
Generación de PDF profesional con los resultados de las consultas RUNT.

Diseño: una página por vehículo. La captura de pantalla de "Resultado de la
Consulta" (desde el encabezado hasta justo antes de "Consultas Adicionales")
es la fuente principal de los datos principales + información general: es
exactamente lo que mostró el portal, así que repetir lo mismo en tablas de
texto solo duplica contenido y alarga el PDF sin agregar información. El
texto queda como respaldo únicamente para cuando no hay captura (por ejemplo,
si el navegador no pudo tomarla a tiempo).
"""
from __future__ import annotations

import base64
import datetime as dt
import io
from pathlib import Path
from typing import Any, Dict, List
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import (Image, KeepTogether, PageBreak, Paragraph, SimpleDocTemplate, Spacer,
                                Table, TableStyle)

NAVY = colors.HexColor("#0B2A6F")
MAGENTA = colors.HexColor("#D4145A")
INK = colors.HexColor("#1B2A4A")
MUTED = colors.HexColor("#6B7A99")
LINE = colors.HexColor("#DDE5F3")
SOFT = colors.HexColor("#EEF3FC")
HEAD = colors.HexColor("#DFEAFB")
OK = colors.HexColor("#15803D")
WARN = colors.HexColor("#B45309")
BAD = colors.HexColor("#B91C1C")

ESTADO_COLOR = {"Completado": OK, "Sin información": WARN, "Error": BAD, "Pendiente": MUTED}

PAGE_W, PAGE_H = A4
MARGIN = 16 * mm
CONTENT_W = PAGE_W - 2 * MARGIN

S = {
    "body": ParagraphStyle("body", fontName="Helvetica", fontSize=8.6, leading=11, textColor=INK, alignment=TA_LEFT),
    "label": ParagraphStyle("label", fontName="Helvetica-Bold", fontSize=7.6, leading=10, textColor=MUTED),
    "value": ParagraphStyle("value", fontName="Helvetica", fontSize=8.8, leading=11, textColor=INK),
    "h2": ParagraphStyle("h2", fontName="Helvetica-Bold", fontSize=11.5, leading=14, textColor=NAVY, spaceBefore=10, spaceAfter=4),
    "h3": ParagraphStyle("h3", fontName="Helvetica-Bold", fontSize=9.6, leading=12, textColor=NAVY, spaceBefore=6, spaceAfter=3),
    "small": ParagraphStyle("small", fontName="Helvetica", fontSize=8, leading=10.5, textColor=MUTED),
    "mono": ParagraphStyle("mono", fontName="Helvetica", fontSize=7.8, leading=10, textColor=INK),
    "th": ParagraphStyle("th", fontName="Helvetica-Bold", fontSize=8, leading=10, textColor=NAVY),
}


def _p(text, style="body"):
    return Paragraph(escape(str(text if text not in (None, "") else "—")).replace("\n", "<br/>"), S[style])


def _estado_p(estado: str, size=9):
    col = ESTADO_COLOR.get(estado, MUTED).hexval()[2:]
    return Paragraph(f'<font color="#{col}"><b>{escape(estado or "—")}</b></font>',
                     ParagraphStyle("e", parent=S["body"], fontSize=size, leading=size + 2))


def _captura_flowable(b64_png: str, max_height=None):
    """Imagen a ancho de página; limitada para caber en UNA hoja."""
    try:
        data = base64.b64decode(b64_png)
        from PIL import Image as PILImage
        with PILImage.open(io.BytesIO(data)) as im:
            w, h = im.size
        if not w:
            return None
        alto = CONTENT_W * (h / w)
        # Dejar espacio para encabezado de placa (~45mm)
        limite = max_height if max_height is not None else (PAGE_H - 2 * MARGIN - 50 * mm)
        if alto > limite:
            scale = limite / alto
            return Image(io.BytesIO(data), width=CONTENT_W * scale, height=limite)
        return Image(io.BytesIO(data), width=CONTENT_W, height=alto)
    except Exception:
        return None


def _kv_table(items: Dict[str, str], columns: int = 2):
    """Tabla etiqueta/valor en 1 o 2 pares por fila."""
    if not items:
        return _p("Sin datos disponibles.", "small")
    pares = list(items.items())
    if columns == 2:
        w = [CONTENT_W * 0.20, CONTENT_W * 0.30, CONTENT_W * 0.20, CONTENT_W * 0.30]
        rows = []
        for i in range(0, len(pares), 2):
            fila = []
            for k, v in pares[i:i + 2]:
                fila += [_p(k, "label"), _p(v, "value")]
            while len(fila) < 4:
                fila += ["", ""]
            rows.append(fila)
    else:
        w = [CONTENT_W * 0.34, CONTENT_W * 0.66]
        rows = [[_p(k, "label"), _p(v, "value")] for k, v in pares]
    t = Table(rows, colWidths=w)
    t.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LINEBELOW", (0, 0), (-1, -1), 0.4, LINE),
        ("TOPPADDING", (0, 0), (-1, -1), 3.5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3.5),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        *[("BACKGROUND", (c, 0), (c, -1), SOFT) for c in ((0, 2) if columns == 2 else (0,))],
    ]))
    return t


def _grid_table(tabla: Dict[str, Any]):
    cols = tabla.get("columnas") or []
    filas = tabla.get("filas") or []
    if not cols or not filas:
        return None
    n = len(cols)
    w = [CONTENT_W / n] * n
    data = [[_p(c, "th") for c in cols]] + [[_p(c, "mono") for c in (f + [""] * n)[:n]] for f in filas]
    t = Table(data, colWidths=w, repeatRows=1)
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), HEAD),
        ("GRID", (0, 0), (-1, -1), 0.4, LINE),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]))
    return t


def _section_title(texto):
    t = Table([[Paragraph(escape(texto), ParagraphStyle("st", parent=S["h2"], textColor=colors.white,
                                                         spaceBefore=0, spaceAfter=0, fontSize=10))]],
              colWidths=[CONTENT_W])
    t.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), NAVY), ("LEFTPADDING", (0, 0), (-1, -1), 8),
                           ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4)]))
    return t


def _footer(canvas, doc):
    canvas.saveState()
    canvas.setStrokeColor(LINE)
    canvas.line(MARGIN, 13 * mm, PAGE_W - MARGIN, 13 * mm)
    canvas.setFont("Helvetica", 7.5)
    canvas.setFillColor(MUTED)
    canvas.drawString(MARGIN, 9 * mm, f"RUNT BOT  |  Generado el {doc._generado:%d/%m/%Y %H:%M}")
    canvas.drawRightString(PAGE_W - MARGIN, 9 * mm, f"Página {doc.page}")
    canvas.setFillColor(MAGENTA)
    canvas.rect(0, PAGE_H - 3 * mm, PAGE_W, 3 * mm, stroke=0, fill=1)
    canvas.restoreState()


def _portada(story, records, generado):
    band = Table([[Paragraph("RUNT BOT", ParagraphStyle("t", fontName="Helvetica-Bold", fontSize=22,
                                                         leading=26, textColor=colors.white)),
                   Paragraph("Informe de consultas vehiculares", ParagraphStyle("t2", fontName="Helvetica", fontSize=11,
                                                                                leading=14, textColor=colors.white, alignment=2))]],
                 colWidths=[CONTENT_W * 0.5, CONTENT_W * 0.5])
    band.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), NAVY), ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                              ("TOPPADDING", (0, 0), (-1, -1), 14), ("BOTTOMPADDING", (0, 0), (-1, -1), 14),
                              ("LEFTPADDING", (0, 0), (-1, -1), 12), ("RIGHTPADDING", (0, 0), (-1, -1), 12),
                              ("LINEBELOW", (0, 0), (-1, -1), 3, MAGENTA)]))
    story += [band, Spacer(1, 8)]

    c = {"Completado": 0, "Sin información": 0, "Error": 0}
    for r in records:
        if r.get("estado") in c:
            c[r["estado"]] += 1

    story.append(_kv_table({
        "Fecha y hora de generación": f"{generado:%d/%m/%Y %H:%M:%S}",
        "Vehículos procesados": str(len(records)),
    }, columns=1))
    story.append(Spacer(1, 10))

    cajas = [("Procesados", len(records), NAVY), ("Completadas", c["Completado"], OK),
             ("Sin información", c["Sin información"], WARN), ("Con error", c["Error"], BAD)]
    fila = [[Paragraph(f'<font size="20" color="#{col.hexval()[2:]}"><b>{n}</b></font><br/>'
                       f'<font size="8" color="#6B7A99">{escape(lbl)}</font>',
                       ParagraphStyle("c", parent=S["body"], leading=22, alignment=1)) for lbl, n, col in cajas]]
    t = Table(fila, colWidths=[CONTENT_W / 4] * 4)
    t.setStyle(TableStyle([("BOX", (0, 0), (-1, -1), 0.6, LINE), ("INNERGRID", (0, 0), (-1, -1), 0.6, LINE),
                           ("TOPPADDING", (0, 0), (-1, -1), 8), ("BOTTOMPADDING", (0, 0), (-1, -1), 8)]))
    story += [t, Spacer(1, 6), Paragraph("Resumen general", S["h2"])]

    head = [_p(x, "th") for x in ("#", "Placa", "Cédula", "Estado", "Vehículo", "Observación")]
    rows = [head]
    for i, r in enumerate(records, start=1):
        res = r.get("resultado") or {}
        g = res.get("general", {})
        veh = " ".join(str(g.get(k, "")) for k in ("marca", "linea", "modelo") if g.get(k)).strip()
        rows.append([_p(i, "mono"), _p(r.get("placa"), "mono"), _p(r.get("cedula"), "mono"),
                     _estado_p(r.get("estado"), 8), _p(veh, "mono"), _p((r.get("error") or "")[:120], "mono")])
    w = [CONTENT_W * x for x in (0.06, 0.12, 0.16, 0.14, 0.26, 0.26)]
    tabla = Table(rows, colWidths=w, repeatRows=1)
    tabla.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), HEAD), ("GRID", (0, 0), (-1, -1), 0.4, LINE),
                               ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                               ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3)]))
    story.append(tabla)


def _vehiculo(story, r, n, total):
    res = r.get("resultado") or {}
    estado = r.get("estado") or "—"

    band = Table([[Paragraph(f'<font color="#C9D6F2" size="8">Vehículo {n} de {total}</font><br/>'
                             f'<font color="white" size="20"><b>{escape(r.get("placa") or "—")}</b></font>',
                             ParagraphStyle("v", parent=S["body"], leading=24)),
                   Paragraph(f'<font color="#C9D6F2" size="8">Estado de la consulta</font><br/>'
                             f'<font color="white" size="12"><b>{escape(estado)}</b></font>',
                             ParagraphStyle("v2", parent=S["body"], leading=16, alignment=2))]],
                 colWidths=[CONTENT_W * 0.55, CONTENT_W * 0.45])
    band.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), NAVY), ("LINEBELOW", (0, 0), (-1, -1), 3, MAGENTA),
                              ("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("LEFTPADDING", (0, 0), (-1, -1), 12),
                              ("RIGHTPADDING", (0, 0), (-1, -1), 12), ("TOPPADDING", (0, 0), (-1, -1), 10),
                              ("BOTTOMPADDING", (0, 0), (-1, -1), 10)]))
    story += [band, Spacer(1, 4)]
    portal = res.get("consulta_portal")
    story.append(_p(f"Cédula consultada: {r.get('cedula') or '—'}   |   Consulta del portal: {portal or '—'}", "small"))

    if estado != "Completado":
        story += [Spacer(1, 8), _p(r.get("error") or "Sin detalle adicional.", "body")]
        return

    # Portal oficial: todo sale en una sola pantalla → una sola captura
    captura = _captura_flowable(res["captura_png"]) if res.get("captura_png") else None
    if captura is not None:
        story += [Spacer(1, 6), _section_title("Resultado de la consulta"), Spacer(1, 3), captura]
    else:
        story += [Spacer(1, 6), _section_title("Datos principales"), Spacer(1, 2),
                  _kv_table(res.get("principal", {}), columns=1)]
        story += [Spacer(1, 8), _section_title("Información general del vehículo"), Spacer(1, 2),
                  _kv_table(res.get("general", {}), columns=2)]

    # Póliza SOAT: sección aparte (su propia captura), independiente de que
    # la de arriba haya salido bien o no.
    soat = res.get("soat") or {}
    story += [Spacer(1, 8), _section_title("Póliza SOAT")]
    captura_soat = _captura_flowable(soat["captura_png"]) if soat.get("captura_png") else None
    if captura_soat is not None:
        story += [Spacer(1, 3), captura_soat]
    else:
        story += [Spacer(1, 2), _p(soat.get("mensaje") or "No disponible para este vehículo.", "small")]


def _build(records: List[Dict[str, Any]], output_path: str) -> str:
    """PDF compacto: solo el resultado de cada vehículo (sin portada)."""
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    generado = dt.datetime.now()
    doc = SimpleDocTemplate(output_path, pagesize=A4, leftMargin=MARGIN, rightMargin=MARGIN, topMargin=12 * mm,
                            bottomMargin=14 * mm, title="RUNT BOT - Consultas vehiculares", author="RUNT BOT")
    doc._generado = generado

    story: list = []
    for n, r in enumerate(records, start=1):
        if n > 1:
            story.append(PageBreak())
        _vehiculo(story, r, n, len(records))
    if not story:
        story.append(_p("Sin resultados.", "body"))
    doc.build(story, onFirstPage=_footer, onLaterPages=_footer)
    return output_path


def generate_pdf(records: List[Dict[str, Any]], output_path: str) -> str:
    """Genera un PDF con todos los resultados (uno por vehículo, una página cada uno)."""
    return _build(records, output_path)


def generate_individual_pdf(record: Dict[str, Any], output_path: str) -> str:
    """Genera un PDF para un solo vehículo."""
    return _build([record], output_path)
