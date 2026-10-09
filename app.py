"""
RUNT BOT - Aplicación Flask principal.
Automatización de consultas vehiculares RUNT por Placa y Cédula.
"""

import os
import json
import re
import uuid
import time
from datetime import datetime
from pathlib import Path
from flask import (
    Flask, render_template, request, jsonify, send_file, session
)
from werkzeug.utils import secure_filename

from services.excel_service import load_excel, RE_PLACA, RE_CEDULA, _clean_placa, _clean_cedula
from services.runt_service import RuntService, run_async
from services.pdf_service import generate_pdf

# ---------------------------------------------------------------------------
# Configuración
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent
UPLOAD_FOLDER = BASE_DIR / "uploads"
RESULTADOS_FOLDER = BASE_DIR / "resultados"
ALLOWED_EXTENSIONS = {"xlsx", "xls"}

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "runt-bot-secret-key-change-me")
app.config["UPLOAD_FOLDER"] = str(UPLOAD_FOLDER)
app.config["MAX_CONTENT_LENGTH"] = 8 * 1024 * 1024  # 8 MB

UPLOAD_FOLDER.mkdir(exist_ok=True)
RESULTADOS_FOLDER.mkdir(exist_ok=True)

# ---------------------------------------------------------------------------
# Candado opcional (usuario/contraseña) para cuando esto quede en una URL
# pública (Railway). El bot consulta cédulas y datos personales reales; sin
# esto, cualquiera con el link podría usarlo. Se activa SOLO si se define
# ACCESS_PASSWORD como variable de entorno — en local, sin esa variable, la
# app funciona exactamente igual que antes (sin pedir nada).
# ---------------------------------------------------------------------------
ACCESS_USER = os.environ.get("ACCESS_USER", "admin")
ACCESS_PASSWORD = os.environ.get("ACCESS_PASSWORD", "")


@app.before_request
def _requerir_login():
    if not ACCESS_PASSWORD:
        return  # candado desactivado (uso local de siempre)
    auth = request.authorization
    if not auth or auth.username != ACCESS_USER or auth.password != ACCESS_PASSWORD:
        return (
            "Acceso restringido.",
            401,
            {"WWW-Authenticate": 'Basic realm="RUNT BOT"'},
        )


def _resumen_vehiculo(record):
    """Marca + línea + modelo en una sola cadena, para la columna 'Vehículo'
    de la vista previa (vacío si el registro aún no tiene resultado)."""
    general = ((record.get("resultado") or {}).get("general")) or {}
    partes = [general.get(k) for k in ("marca", "linea", "modelo") if general.get(k)]
    return " ".join(str(p) for p in partes).strip()


def _resumen_soat(record):
    """Estado de la Póliza SOAT (p. ej. "VIGENTE") para la columna 'Póliza'
    de la vista previa. Se calcula leyendo el texto de la página (no la
    estructura de una tabla), así que no importa si el portal arma esa
    sección con <table>, <mat-table> o simples <div>."""
    return (record.get("resultado") or {}).get("soat_estado") or ""


def _close_runt_browser():
    """Cierra Chromium cuando ya no quedan registros pendientes. Se llama
    desde los tres lugares que pueden completar el último registro del lote
    (OCR automático, sin información, y el envío manual del CAPTCHA)."""
    runt = work_state.get("runt")
    if runt is not None:
        try:
            run_async(runt.stop())
        except Exception:
            pass
        work_state["runt"] = None

# Estado global de la sesión de trabajo (simple, para uso local)
# En producción se usaría Redis/DB, aquí es suficiente para un usuario local.
work_state = {
    "records": [],
    "current_index": -1,
    "status": "idle",          # idle | loading | processing | waiting_captcha | done
    "message": "",
    "runt": None,              # Instancia de RuntService
    "started_at": None,
    "completed": 0,
    "errors": 0,
}


def allowed_file(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


# ---------------------------------------------------------------------------
# Rutas
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/upload", methods=["POST"])
def upload_excel():
    """Carga y valida el archivo Excel."""
    if "file" not in request.files:
        return jsonify({"ok": False, "error": "No se envió ningún archivo"}), 400

    file = request.files["file"]
    if file.filename == "":
        return jsonify({"ok": False, "error": "Nombre de archivo vacío"}), 400

    if not allowed_file(file.filename):
        return jsonify({"ok": False, "error": "Solo se permiten archivos .xlsx o .xls"}), 400

    filename = secure_filename(file.filename)
    save_path = UPLOAD_FOLDER / f"{uuid.uuid4().hex}_{filename}"
    file.save(str(save_path))

    try:
        records, meta = load_excel(str(save_path))
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400

    # Resetear estado
    work_state["records"] = records
    work_state["current_index"] = -1
    work_state["status"] = "idle"
    work_state["message"] = f"{meta['total']} registros cargados"
    work_state["completed"] = 0
    work_state["errors"] = 0
    work_state["started_at"] = None

    # Cerrar navegador anterior si existe
    if work_state["runt"]:
        try:
            run_async(work_state["runt"].stop())
        except Exception:
            pass
        work_state["runt"] = None

    return jsonify({
        "ok": True,
        "total": meta["total"],
        "preview": meta["preview"],
        "message": work_state["message"]
    })


@app.route("/api/add_manual", methods=["POST"])
def add_manual():
    """
    Agrega UN registro escrito a mano (placa + cédula), sin necesidad de
    Excel. Se puede usar solo, o para completar lo ya cargado desde un
    archivo: se agrega al mismo listado, con la misma validación y limpieza
    que usa el Excel (mayúsculas, sin puntos/espacios, formato).
    """
    data = request.get_json(silent=True) or {}
    placa_raw = str(data.get("placa") or "")
    cedula_raw = str(data.get("cedula") or "")

    placa = _clean_placa(placa_raw)
    cedula = _clean_cedula(cedula_raw)

    if not placa or not RE_PLACA.match(placa):
        return jsonify({"ok": False, "error": f"Placa «{placa or placa_raw}» no válida (5 a 7 letras o números)."}), 400
    if not cedula or not RE_CEDULA.match(cedula):
        return jsonify({"ok": False, "error": f"Cédula «{cedula or cedula_raw}» no válida (solo dígitos, 4 a 12)."}), 400

    records = work_state["records"]
    if any(r["placa"] == placa and r["cedula"] == cedula for r in records):
        return jsonify({"ok": False, "error": f"{placa} / {cedula} ya está en la lista."}), 400

    record = {
        "id": (max((r["id"] for r in records), default=0) + 1),
        "placa": placa,
        "cedula": cedula,
        "estado": "Pendiente",
        "resultado": None,
        "error": None,
    }
    records.append(record)
    work_state["message"] = f"{len(records)} registros en la lista"

    return jsonify({
        "ok": True,
        "record": {"id": record["id"], "placa": record["placa"], "cedula": record["cedula"], "estado": record["estado"]},
        "total": len(records),
    })


@app.route("/api/remove_record", methods=["POST"])
def remove_record():
    """Quita un registro de la lista (solo si todavía está Pendiente)."""
    data = request.get_json(silent=True) or {}
    rid = data.get("id")
    records = work_state["records"]
    objetivo = next((r for r in records if r["id"] == rid), None)
    if objetivo is None:
        return jsonify({"ok": False, "error": "Registro no encontrado."}), 404
    if objetivo["estado"] != "Pendiente":
        return jsonify({"ok": False, "error": "Solo se pueden quitar registros Pendientes."}), 400
    records.remove(objetivo)
    return jsonify({"ok": True, "total": len(records)})


@app.route("/api/status")
def get_status():
    """Devuelve el estado actual del procesamiento."""
    records = work_state["records"]
    total = len(records)
    completed = sum(1 for r in records if r["estado"] == "Completado")
    errors = sum(1 for r in records if r["estado"] in ("Error", "Sin información"))
    pending = total - completed - errors
    current = None
    if 0 <= work_state["current_index"] < total:
        current = records[work_state["current_index"]]

    elapsed = None
    if work_state["started_at"]:
        elapsed = round(time.time() - work_state["started_at"], 1)

    return jsonify({
        "status": work_state["status"],
        "message": work_state["message"],
        "total": total,
        "completed": completed,
        "errors": errors,
        "pending": pending,
        "current_index": work_state["current_index"],
        "current": current,
        "elapsed": elapsed,
        "records": [
            {
                "id": r["id"],
                "placa": r["placa"],
                "cedula": r["cedula"],
                "estado": r["estado"],
                "error": r.get("error"),
                "vehiculo": _resumen_vehiculo(r),
                "poliza": _resumen_soat(r),
                "estado_vehiculo": ((r.get("resultado") or {}).get("principal") or {}).get("estado"),
            }
            for r in records
        ]
    })


@app.route("/api/start", methods=["POST"])
def start_processing():
    """
    Inicia el procesamiento del siguiente registro pendiente.
    1) Prepara formulario + captura CAPTCHA
    2) Intenta OCR automático (hasta MAX_OCR_RETRIES)
    3) Si OCR falla → pide intervención manual (muestra imagen)
    """
    records = work_state["records"]
    if not records:
        return jsonify({"ok": False, "error": "No hay registros cargados"}), 400

    next_idx = None
    for i, r in enumerate(records):
        if r["estado"] == "Pendiente":
            next_idx = i
            break

    if next_idx is None:
        work_state["status"] = "done"
        return jsonify({"ok": True, "message": "Todos los registros ya fueron procesados", "done": True})

    work_state["current_index"] = next_idx
    work_state["status"] = "processing"
    work_state["message"] = f"Preparando consulta {next_idx + 1} de {len(records)}"
    if work_state["started_at"] is None:
        work_state["started_at"] = time.time()

    record = records[next_idx]
    record["estado"] = "Procesando"

    try:
        if work_state["runt"] is None:
            # Visible por defecto para poder ver qué hace el bot. Si en algún
            # momento se prefiere ocultarlo, definir RUNT_HEADLESS=1 antes de
            # iniciar (set RUNT_HEADLESS=1 && python app.py).
            headless = os.environ.get("RUNT_HEADLESS", "0") == "1"
            runt = RuntService(headless=headless)
            run_async(runt.start())
            work_state["runt"] = runt
        else:
            runt = work_state["runt"]

        # 1) Preparar formulario (placa + cédula + capturar imagen)
        prep = run_async(runt.prepare_query(record["placa"], record["cedula"]))

        if prep.get("status") == "Error":
            record["estado"] = "Error"
            record["error"] = prep.get("error", "Error al preparar consulta")
            work_state["errors"] += 1
            work_state["status"] = "idle"
            return jsonify({"ok": False, "error": record["error"]})

        # 2) Intentar OCR automático + envío
        record["estado"] = "CAPTCHA"
        work_state["message"] = f"Intentando OCR para {record['placa']}…"
        auto = run_async(runt.try_auto_submit())

        if auto.get("status") == "Completado":
            record["estado"] = "Completado"
            record["resultado"] = {
                "principal": auto.get("principal", {}),
                "general": auto.get("general", {}),
                "adicionales": auto.get("adicionales", {}),
                "captura_png": auto.get("captura_png"),
                "captura_adicionales_png": auto.get("captura_adicionales_png"),
                "soat": auto.get("soat"),
                "soat_estado": auto.get("soat_estado"),
                "consulta_portal": auto.get("consulta_portal"),
            }
            work_state["completed"] += 1
            work_state["message"] = f"{record['placa']} consultado (OCR)"
            pending = any(r["estado"] == "Pendiente" for r in records)
            work_state["status"] = "idle" if pending else "done"
            if not pending:
                _close_runt_browser()
            return jsonify({
                "ok": True,
                "status": "Completado",
                "placa": record["placa"],
                "estado": "Completado",
                "ocr_used": auto.get("ocr_used"),
                "ocr_attempts": auto.get("ocr_attempts"),
                "resultado": record["resultado"],
                "done": not pending,
                "message": work_state["message"]
            })

        if auto.get("status") == "Sin información":
            record["estado"] = "Sin información"
            record["error"] = auto.get("error", "Sin información")
            work_state["errors"] += 1
            pending = any(r["estado"] == "Pendiente" for r in records)
            work_state["status"] = "idle" if pending else "done"
            if not pending:
                _close_runt_browser()
            return jsonify({
                "ok": True,
                "status": "Sin información",
                "placa": record["placa"],
                "error": record["error"],
                "done": not pending,
                "message": f"{record['placa']}: sin información"
            })

        # Error no-CAPTCHA (ej. timeout de red)
        if auto.get("status") == "Error" and not auto.get("needs_manual"):
            record["estado"] = "Error"
            record["error"] = auto.get("error", "Error desconocido")
            work_state["errors"] += 1
            work_state["status"] = "idle"
            return jsonify({"ok": False, "error": record["error"]})

        # 3) OCR no pudo → pedir manual
        img_b64 = (auto.get("captcha") or {}).get("image_b64") or (prep.get("captcha") or {}).get("image_b64")
        record["estado"] = "CAPTCHA"
        work_state["status"] = "waiting_captcha"
        work_state["message"] = f"OCR falló — introduce el CAPTCHA para {record['placa']}"
        return jsonify({
            "ok": True,
            "status": "waiting_captcha",
            "placa": record["placa"],
            "cedula": record["cedula"],
            "index": next_idx,
            "captcha_image": img_b64,
            "ocr_attempts": auto.get("ocr_attempts"),
            "ocr_suggestion": (auto.get("ocr_attempts") or [{}])[-1].get("text") if auto.get("ocr_attempts") else None,
            "message": work_state["message"]
        })

    except Exception as e:
        record["estado"] = "Error"
        record["error"] = str(e)
        work_state["errors"] += 1
        work_state["status"] = "idle"
        return jsonify({"ok": False, "error": str(e)})


@app.route("/api/submit_captcha", methods=["POST"])
def submit_captcha():
    """Recibe el código del CAPTCHA, envía la consulta y extrae resultados."""
    data = request.get_json() or {}
    code = (data.get("code") or "").strip()
    if not code:
        return jsonify({"ok": False, "error": "Código CAPTCHA vacío"}), 400

    idx = work_state["current_index"]
    records = work_state["records"]
    if idx < 0 or idx >= len(records):
        return jsonify({"ok": False, "error": "No hay registro activo"}), 400

    record = records[idx]
    record["estado"] = "Consultando"
    work_state["status"] = "processing"
    work_state["message"] = f"Consultando {record['placa']}…"

    runt = work_state["runt"]
    if not runt:
        return jsonify({"ok": False, "error": "Sesión de navegador no disponible"}), 500

    try:
        result = run_async(runt.submit_with_captcha(code))

        if result.get("status") == "Completado":
            record["estado"] = "Completado"
            record["resultado"] = {
                "principal": result.get("principal", {}),
                "general": result.get("general", {}),
                "adicionales": result.get("adicionales", {}),
                "captura_png": result.get("captura_png"),
                "captura_adicionales_png": result.get("captura_adicionales_png"),
                "soat": result.get("soat"),
                "soat_estado": result.get("soat_estado"),
                "consulta_portal": result.get("consulta_portal"),
            }
            work_state["completed"] += 1
            work_state["message"] = f"{record['placa']} consultado correctamente"
        elif result.get("status") == "Sin información":
            record["estado"] = "Sin información"
            record["error"] = result.get("error", "Sin información")
            work_state["errors"] += 1
            work_state["message"] = f"{record['placa']}: sin información"
        else:
            err = (result.get("error") or "").lower()
            # Captcha incorrecto → recargar página y pedir otro código (no marcar Error aún)
            if any(k in err for k in ("captcha", "código", "codigo", "válido", "valido", "incorrect")):
                try:
                    prep = run_async(runt.prepare_query(record["placa"], record["cedula"]))
                    img = (prep.get("captcha") or {}).get("image_b64") or ""
                    record["estado"] = "CAPTCHA"
                    work_state["status"] = "waiting_captcha"
                    work_state["message"] = "CAPTCHA incorrecto. Escribe el nuevo código."
                    return jsonify({
                        "ok": True,
                        "status": "waiting_captcha",
                        "placa": record["placa"],
                        "captcha_image": img,
                        "message": "CAPTCHA incorrecto. Se recargó la página; escribe el nuevo código.",
                    })
                except Exception as e:
                    record["estado"] = "Error"
                    record["error"] = f"CAPTCHA incorrecto y no se pudo recargar: {e}"
                    work_state["errors"] += 1
                    work_state["message"] = record["error"]
            else:
                record["estado"] = "Error"
                record["error"] = result.get("error", "Error desconocido")
                work_state["errors"] += 1
                work_state["message"] = f"Error en {record['placa']}: {record['error']}"

        # ¿Quedan pendientes?
        pending = any(r["estado"] == "Pendiente" for r in records)
        work_state["status"] = "idle" if pending else "done"
        if not pending:
            _close_runt_browser()

        return jsonify({
            "ok": True,
            "status": record["estado"],
            "estado": record["estado"],
            "placa": record["placa"],
            "resultado": record.get("resultado"),
            "error": record.get("error"),
            "done": not pending,
            "message": work_state["message"]
        })

    except Exception as e:
        record["estado"] = "Error"
        record["error"] = str(e)
        work_state["errors"] += 1
        work_state["status"] = "idle"
        return jsonify({"ok": False, "error": str(e)})


@app.route("/api/refresh_captcha", methods=["POST"])
def refresh_captcha():
    """Solicita una nueva imagen de CAPTCHA."""
    runt = work_state["runt"]
    if not runt:
        return jsonify({"ok": False, "error": "Sesión no disponible"}), 500
    try:
        data = run_async(runt.refresh_captcha())
        if data.get("success"):
            return jsonify({"ok": True, "captcha_image": data["image_b64"]})
        return jsonify({"ok": False, "error": "No se pudo refrescar CAPTCHA"})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


@app.route("/api/skip", methods=["POST"])
def skip_current():
    """Marca el registro actual como error y permite continuar."""
    idx = work_state["current_index"]
    records = work_state["records"]
    if 0 <= idx < len(records):
        records[idx]["estado"] = "Error"
        records[idx]["error"] = "Omitido por el usuario"
        work_state["errors"] += 1
    work_state["status"] = "idle"
    work_state["message"] = "Registro omitido"
    return jsonify({"ok": True})


@app.route("/api/generate_pdf", methods=["POST"])
def api_generate_pdf():
    """
    Genera siempre un .zip. Con 2+ registros trae el consolidado (todos los
    vehículos en un solo archivo) más un PDF individual por placa. Con un
    solo registro, el consolidado y el individual son el mismo contenido
    repetido dos veces dentro del zip, así que en ese caso se mete un único
    PDF, sin duplicar.
    """
    import zipfile
    from services.pdf_service import generate_pdf, generate_individual_pdf

    records = [r for r in work_state["records"] if r.get("estado") in ("Completado", "Sin información", "Error")]
    if not records:
        records = work_state["records"]
    if not records:
        return jsonify({"ok": False, "error": "No hay registros"}), 400

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    try:
        zip_name = f"RUNT_Resultados_{timestamp}.zip"
        zip_path = RESULTADOS_FOLDER / zip_name
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            if len(records) == 1:
                r = records[0]
                placa = re.sub(r"[^A-Za-z0-9]", "", str(r.get("placa") or "SINPLACA"))
                nombre = f"RUNT_{placa}_{timestamp}.pdf"
                ruta = RESULTADOS_FOLDER / nombre
                generate_individual_pdf(r, str(ruta))
                zf.write(str(ruta), nombre)
            else:
                consolidado = f"RUNT_Consolidado_{timestamp}.pdf"
                cons_path = RESULTADOS_FOLDER / consolidado
                generate_pdf(records, str(cons_path))
                zf.write(str(cons_path), consolidado)

                for r in records:
                    placa = re.sub(r"[^A-Za-z0-9]", "", str(r.get("placa") or "SINPLACA"))
                    ind_name = f"RUNT_{placa}_{timestamp}.pdf"
                    ind_path = RESULTADOS_FOLDER / ind_name
                    generate_individual_pdf(r, str(ind_path))
                    zf.write(str(ind_path), ind_name)

        return jsonify({
            "ok": True,
            "filename": zip_name,
            "url": f"/download/{zip_name}",
            "tipo": "zip",
            "total": len(records)
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/download/<filename>")
def download_file(filename):
    path = RESULTADOS_FOLDER / secure_filename(filename)
    if not path.exists():
        return "Archivo no encontrado", 404
    return send_file(str(path), as_attachment=True, download_name=filename)


@app.route("/api/reset", methods=["POST"])
def reset():
    """Limpia absolutamente todo: estado, navegador, uploads y resultados."""
    if work_state.get("runt"):
        try:
            run_async(work_state["runt"].stop())
        except Exception:
            pass

    for folder in (UPLOAD_FOLDER, RESULTADOS_FOLDER):
        try:
            for f in folder.iterdir():
                if f.is_file():
                    try:
                        f.unlink()
                    except OSError:
                        pass
        except Exception:
            pass

    work_state["records"] = []
    work_state["current_index"] = -1
    work_state["status"] = "idle"
    work_state["message"] = ""
    work_state["runt"] = None
    work_state["started_at"] = None
    work_state["completed"] = 0
    work_state["errors"] = 0
    return jsonify({"ok": True, "message": "Todo limpiado. Listo para empezar de cero."})


# ---------------------------------------------------------------------------
# Arranque
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    print("=" * 60)
    print("  RUNT BOT  —  Consulta Vehicular Automatizada")
    print("  Abre tu navegador en: http://127.0.0.1:5050")
    print("=" * 60)
    # Railway (y la mayoría de plataformas en la nube) inyectan el puerto
    # real en la variable de entorno PORT y esperan que el servidor escuche
    # en 0.0.0.0, no en 127.0.0.1. Local sigue funcionando igual (PORT no
    # definida -> usa 5050 como siempre).
    port = int(os.environ.get("PORT", 5050))
    host = os.environ.get("HOST", "127.0.0.1" if "PORT" not in os.environ else "0.0.0.0")
    app.run(host=host, port=port, debug=False, threaded=True)
