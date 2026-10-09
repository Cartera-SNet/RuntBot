"""
Servicio de lectura y validación de archivos Excel con placas y cédulas.

Misma lógica de detección de columnas y limpieza que el bot de CAPTCHA manual:
- Busca los encabezados dentro de las primeras 15 filas (no exige que estén
  en la fila 1), por si hay un título o una fila en blanco antes.
- Reconoce varios nombres posibles de columna ("Cédula", "Documento",
  "Nro Documento", "Placa", "Matrícula", etc.), sin distinguir mayúsculas
  ni tildes.
- Corrige cédulas que Excel guardó como número (pierde ceros a la izquierda
  o las deja en notación científica tipo 1.048e9).
- Admite .xlsx/.xlsm (openpyxl) y .xls (xlrd).
No usa pandas, para evitar problemas de compilación en Windows.
"""
from __future__ import annotations

import datetime
import re
import unicodedata
from pathlib import Path
from typing import Any, Dict, List, Tuple


class ExcelError(Exception):
    """Error legible para mostrar en la interfaz."""


PLACA_ALIASES = {"placa", "placas", "placavehiculo", "placadelvehiculo", "matricula"}
CEDULA_ALIASES = {
    "cedula", "cedulas", "documento", "numerodocumento", "nrodocumento",
    "ndocumento", "identificacion", "numerodeidentificacion", "cc",
    "cedulaciudadania", "cedulapropietario",
}

RE_PLACA = re.compile(r"^[A-Z0-9]{5,7}$")
RE_CEDULA = re.compile(r"^\d{4,12}$")


def _norm(s) -> str:
    s = unicodedata.normalize("NFD", str(s or ""))
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return re.sub(r"[^a-z0-9]", "", s.lower())


def _cell_text(v) -> str:
    """Convierte cualquier celda a texto sin perder dígitos."""
    if v is None:
        return ""
    if isinstance(v, bool):
        return str(v)
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        return str(int(v)) if v.is_integer() else repr(v)
    if isinstance(v, (datetime.datetime, datetime.date)):
        return v.isoformat()
    return re.sub(r"\s+", " ", str(v)).strip()


def _read_rows(path: Path) -> List[List[str]]:
    ext = path.suffix.lower()
    rows: List[List[str]] = []
    try:
        if ext in (".xlsx", ".xlsm"):
            import openpyxl

            wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
            ws = wb.active
            for row in ws.iter_rows(values_only=True):
                rows.append([_cell_text(c) for c in row])
            wb.close()
        elif ext == ".xls":
            try:
                import xlrd
            except ImportError as e:
                raise ExcelError("Para leer .xls instala la dependencia xlrd (pip install xlrd).") from e
            book = xlrd.open_workbook(str(path))
            sheet = book.sheet_by_index(0)
            for i in range(sheet.nrows):
                rows.append([_cell_text(c) for c in sheet.row_values(i)])
        else:
            raise ExcelError("Formato no soportado. Usa un archivo .xlsx o .xls.")
    except ExcelError:
        raise
    except Exception as e:
        raise ExcelError(f"No se pudo abrir el archivo: {e}") from e
    return rows


def _clean_placa(v: str) -> str:
    return re.sub(r"[\s\-_.]", "", v or "").upper()


def _clean_cedula(v: str) -> str:
    v = (v or "").strip()
    v = re.sub(r"\.0+$", "", v)           # 1048206552.0 -> 1048206552
    return re.sub(r"[\s.,\-]", "", v)     # 1.048.206.552 -> 1048206552


def load_excel(file_path: str) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """
    Carga un Excel, detecta las columnas Placa/Cédula aunque no estén en la
    primera fila ni se llamen exactamente así, limpia y normaliza los datos.

    Returns:
        (lista de registros, meta información)
    """
    path = Path(file_path)
    if not path.exists():
        raise ExcelError(f"Archivo no encontrado: {file_path}")

    rows = _read_rows(path)
    if not any(any(c for c in r) for r in rows):
        raise ExcelError("El archivo está vacío.")

    header_idx = col_placa = col_cedula = None
    for i, r in enumerate(rows[:15]):
        norm_row = [_norm(c) for c in r]
        p = next((j for j, c in enumerate(norm_row) if c in PLACA_ALIASES), None)
        c = next((j for j, c in enumerate(norm_row) if c in CEDULA_ALIASES), None)
        if p is not None and c is not None:
            header_idx, col_placa, col_cedula = i, p, c
            break

    if header_idx is None:
        first = next((r for r in rows if any(r)), [])
        detectadas = ", ".join(c for c in first if c) or "ninguna"
        raise ExcelError(
            "No se encontraron las columnas requeridas: «Placa» y «Cedula». "
            f"Columnas detectadas: {detectadas}."
        )

    records: List[Dict[str, Any]] = []
    seen: Dict[Tuple[str, str], int] = {}
    for offset, r in enumerate(rows[header_idx + 1:], start=header_idx + 2):
        raw_p = r[col_placa] if col_placa < len(r) else ""
        raw_c = r[col_cedula] if col_cedula < len(r) else ""
        if not raw_p and not raw_c:
            continue  # fila vacía

        placa = _clean_placa(raw_p)
        cedula = _clean_cedula(raw_c)
        problema = ""
        if not placa:
            problema = "Falta la placa."
        elif not RE_PLACA.match(placa):
            problema = f"Placa «{placa}» no válida (se esperan 5 a 7 letras o números)."
        elif not cedula:
            problema = "Falta la cédula."
        elif not RE_CEDULA.match(cedula):
            if re.search(r"[eE]\+\d", raw_c):
                problema = ("La cédula quedó en notación científica. Da formato «Texto» "
                            "a la columna y vuelve a escribir el número.")
            else:
                problema = f"Cédula «{cedula}» no válida (solo dígitos, 4 a 12)."

        key = (placa, cedula)
        duplicado = not problema and key in seen
        seen.setdefault(key, offset)

        records.append({
            "id": len(records) + 1,
            "placa": placa,
            "cedula": cedula,
            "estado": "Error" if problema else "Pendiente",
            "resultado": None,
            "error": problema or ("Registro repetido (ver fila " + str(seen[key]) + ")." if duplicado else None),
        })

    if not records:
        raise ExcelError("El archivo tiene encabezados pero ningún registro.")

    meta = {
        "total": len(records),
        "preview": records[:5],
        "columnas": [c for c in (rows[header_idx] if header_idx < len(rows) else []) if c],
    }
    return records, meta


def build_template() -> bytes:
    """Plantilla .xlsx de ejemplo (columnas Placa y Cédula, con la cédula en
    formato Texto para que Excel no la dañe convirtiéndola en número)."""
    import openpyxl
    from openpyxl.styles import Font, PatternFill

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Consultas"
    ws.append(["Placa", "Cedula"])
    ws.append(["EVJ071", "1048206552"])
    ws.append(["ABC123", "1234567890"])
    for c in ws[1]:
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = PatternFill("solid", fgColor="0B2A6F")
    for row in ws.iter_rows(min_row=2, min_col=2, max_col=2):
        for c in row:
            c.number_format = "@"  # Texto: evita que Excel convierta la cédula a numero
    ws.column_dimensions["A"].width = 14
    ws.column_dimensions["B"].width = 18

    import io
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
