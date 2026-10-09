"""
Servicio de automatización RUNT usando Playwright.
Maneja navegación, captura de CAPTCHA, consulta y extracción de resultados.
El componente de CAPTCHA está aislado para facilitar futuras mejoras (OCR / manual).
"""

import asyncio
import base64
import re
import threading
import unicodedata
from typing import Optional, Dict, Any, List
from playwright.async_api import async_playwright, Page, Browser, BrowserContext

from services.captcha_ocr import recognize as ocr_recognize


# Portal oficial RUNT (consulta ciudadana)
URL = "https://portalpublico.runt.gov.co/#/consulta-vehiculo/consulta/consulta-ciudadana"

# Máximo de reintentos OCR automáticos por registro antes de pedir intervención manual
MAX_OCR_RETRIES = 5


# ---------------------------------------------------------------------------
# Etiquetas reales del portal (confirmadas por captura de pantalla) y escáner
# de texto plano. Vive aquí, fuera de la clase, porque es pura lectura del
# resultado: no toca el CAPTCHA ni cómo se llegó hasta esta página.
# ---------------------------------------------------------------------------
PRINCIPAL_LABELS = {
    "placa": ["PLACA DEL VEHÍCULO", "PLACA DEL VEHICULO", "PLACA"],
    "licencia_transito": ["NRO. DE LICENCIA DE TRÁNSITO", "NRO. DE LICENCIA DE TRANSITO"],
    "estado": ["ESTADO DEL VEHÍCULO", "ESTADO DEL VEHICULO"],
    "tipo_servicio": ["TIPO DE SERVICIO"],
    "clase_vehiculo": ["CLASE DE VEHÍCULO", "CLASE DE VEHICULO"],
}
GENERAL_LABELS = {
    "marca": ["MARCA"],
    "linea": ["LÍNEA", "LINEA"],
    "modelo": ["MODELO"],
    "color": ["COLOR"],
    "numero_serie": ["NÚMERO DE SERIE", "NUMERO DE SERIE"],
    "numero_chasis": ["NÚMERO DE CHASIS", "NUMERO DE CHASIS"],
    "vin": ["NÚMERO DE VIN", "NUMERO DE VIN"],
    "numero_motor": ["NÚMERO DE MOTOR", "NUMERO DE MOTOR"],
    "cilindraje": ["CILINDRAJE"],
    "tipo_carroceria": ["TIPO DE CARROCERÍA", "TIPO DE CARROCERIA"],
    "tipo_combustible": ["TIPO COMBUSTIBLE", "TIPO DE COMBUSTIBLE"],
    "fecha_matricula": ["FECHA DE MATRÍCULA INICIAL", "FECHA DE MATRICULA INICIAL"],
    "autoridad_transito": ["AUTORIDAD DE TRÁNSITO", "AUTORIDAD DE TRANSITO"],
    "gravamenes": ["GRAVÁMENES A LA PROPIEDAD", "GRAVAMENES A LA PROPIEDAD"],
    "clasico_antiguo": ["CLÁSICO O ANTIGUO", "CLASICO O ANTIGUO"],
    "repotenciado": ["REPOTENCIADO"],
    "regrabacion_motor": ["REGRABACIÓN MOTOR (SÍ/NO)", "REGRABACION MOTOR (SI/NO)", "REGRABACIÓN MOTOR", "REGRABACION MOTOR"],
    "nro_regrabacion_motor": ["NRO. REGRABACIÓN MOTOR", "NRO. REGRABACION MOTOR"],
    "regrabacion_chasis": ["REGRABACIÓN CHASIS (SÍ/NO)", "REGRABACION CHASIS (SI/NO)", "REGRABACIÓN CHASIS", "REGRABACION CHASIS"],
    "nro_regrabacion_chasis": ["NRO. REGRABACIÓN CHASIS", "NRO. REGRABACION CHASIS"],
    "regrabacion_serie": ["REGRABACIÓN SERIE (SÍ/NO)", "REGRABACION SERIE (SI/NO)", "REGRABACIÓN SERIE", "REGRABACION SERIE"],
    "nro_regrabacion_serie": ["NRO. REGRABACIÓN SERIE", "NRO. REGRABACION SERIE"],
    "regrabacion_vin": ["REGRABACIÓN VIN (SÍ/NO)", "REGRABACION VIN (SI/NO)", "REGRABACIÓN VIN", "REGRABACION VIN"],
    "nro_regrabacion_vin": ["NRO. REGRABACIÓN VIN", "NRO. REGRABACION VIN"],
    "vehiculo_ensenanza": ["VEHÍCULO ENSEÑANZA (SÍ/NO)", "VEHICULO ENSENANZA (SI/NO)", "VEHÍCULO ENSEÑANZA", "VEHICULO ENSENANZA"],
    "puertas": ["PUERTAS"],
    "pasajeros_total": ["PASAJEROS TOTAL"],
    "pasajeros_sentados": ["PASAJEROS SENTADOS"],
}
# Títulos que marcan dónde termina el valor de la última etiqueta encontrada
_STOP_HEADINGS = [
    "Información general del vehículo", "Informacion general del vehiculo", "Consultas Adicionales",
    "Datos Técnicos del Vehículo", "Póliza SOAT", "Pólizas de Responsabilidad Civil",
    "Certificado RTM", "Certificados de Revisión Ambiental o de Enseñanza", "Consulta realizada",
]


def _accent_rx(texto: str) -> str:
    """Regex tolerante a tildes: 'TRANSITO' también encuentra 'TRÁNSITO'."""
    variantes = {"a": "aá", "e": "eé", "i": "ií", "o": "oó", "u": "uúü", "n": "nñ"}
    out = []
    for ch in texto:
        base = ch.lower()
        if ch == " ":
            out.append(r"\s+")
        elif base in variantes:
            out.append("[" + variantes[base].upper() + variantes[base] + "]")
        else:
            out.append(re.escape(ch))
    return "".join(out)


def _scan_labels(text: str, label_map: Dict[str, List[str]]) -> Dict[str, str]:
    """
    Busca, en una sola pasada, todas las etiquetas conocidas dentro del texto
    plano de la página y toma como valor lo que hay hasta la siguiente
    etiqueta encontrada (o hasta un título de sección).

    Funciona sin importar si el portal entrega "ETIQUETA: valor",
    "ETIQUETA\\nvalor" o "ETIQUETAvalor" pegados, que es justamente lo que no
    se puede saber de antemano sin ver el HTML real.

    IMPORTANTE — no quitar lo siguiente: el portal oficial no navega a otra
    página, el resultado aparece debajo del MISMO formulario, que sigue en
    el DOM. Si se escanean las etiquetas sobre todo el texto de la página,
    "Nro. Placa" o "Nro. Documento" (texto del formulario) se confunden con
    el alias corto "PLACA" y le ganan a la etiqueta real "PLACA DEL
    VEHÍCULO" de más abajo; y sin un límite inferior, el último campo
    encontrado se traga el botón "Realizar otra consulta" y el copyright.
    Esto ya pasó tres veces porque la protección vivía en el llamador
    (_extract_results) y se perdía al reescribir ese método; por eso ahora
    vive AQUÍ, dentro de _scan_labels, que es la función que de verdad
    importa proteger.
    """
    inicio = re.search(_accent_rx("PLACA DEL VEHÍCULO"), text, re.IGNORECASE)
    if inicio:
        text = text[inicio.start():]
    fin = re.search(r"realizar\s+otra\s+consulta|copyright\s+\d{4}\s+runt", text, re.IGNORECASE)
    if fin:
        text = text[:fin.start()]

    flat = [(key, lbl) for key, labels in label_map.items() for lbl in labels]
    flat.sort(key=lambda kv: len(kv[1]), reverse=True)  # evita que una etiqueta corta se coma a una larga
    alt = "|".join(_accent_rx(lbl) for _, lbl in flat)
    stop_rx = re.compile("|".join(_accent_rx(s) for s in _STOP_HEADINGS), re.IGNORECASE)
    letras = r"\wÁÉÍÓÚÑáéíóúñ"

    # 1.º con límites de palabra; si encuentra muy pocas, reintenta sin ellos
    # (texto con etiqueta y valor pegados, sin ningún separador real)
    for patron in (rf"(?<![{letras}])({alt})(?![{letras}])", rf"({alt})"):
        matches = list(re.finditer(patron, text, re.IGNORECASE))
        if len(matches) >= max(3, len(label_map) // 4):
            break

    por_label = {lbl.upper(): key for key, lbl in flat}
    out: Dict[str, str] = {}
    for i, m in enumerate(matches):
        found = m.group(1).upper()
        key = por_label.get(found)
        if not key or key in out:
            continue
        fin = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        seg = text[m.end():fin]
        corte = stop_rx.search(seg)
        if corte:
            seg = seg[:corte.start()]
        valor = re.sub(r"\s+", " ", seg).strip(" |:\t\n-–—")
        if valor and valor not in ("—", "-", "–", "N/A", "NO APLICA"):
            out[key] = valor
    return out


# ---------------------------------------------------------------------------
# Lectura de "Consultas Adicionales": acordeones a expandir y leer.
# Versión rápida y tolerante: comprueba aria-expanded antes de hacer clic
# (para no cerrar una sección que el portal ya abrió sola), usa esperas
# cortas en vez de timeouts largos, y extrae pares etiqueta/valor y tablas
# con una sola pasada de JavaScript en vez de volver a pedirle texto a
# Playwright por cada campo.
# ---------------------------------------------------------------------------
# Solo Póliza SOAT: es lo que se necesita en el PDF
SECCIONES_ADICIONALES = [
    "Póliza SOAT",
]

_COLLECT_JS = r"""
(root) => {
  const clean = s => (s || '').replace(/\s+/g, ' ').trim();
  const vis = e => !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length);
  const pairs = [], tables = [];
  root.querySelectorAll('table').forEach(t => {
    if (!vis(t)) return;
    const rows = [...t.rows];
    if (!rows.length) return;
    const hdr = (t.tHead && t.tHead.rows[0]) ||
      ((rows.length > 1 && [...rows[0].cells].every(c => c.tagName === 'TH')) ? rows[0] : null);
    if (hdr && hdr.cells.length > 2) {
      const cols = [...hdr.cells].map(c => clean(c.innerText));
      const body = rows.filter(r => r !== hdr && !(t.tHead && r.parentElement === t.tHead));
      const filas = body.map(r => [...r.cells].map(c => clean(c.innerText))).filter(f => f.some(x => x));
      tables.push({columnas: cols, filas});
      return;
    }
    rows.forEach(r => {
      const cells = [...r.cells].map(c => clean(c.innerText));
      if (cells.length === 2) pairs.push([cells[0], cells[1]]);
      else if (cells.length >= 4 && cells.length % 2 === 0)
        for (let i = 0; i < cells.length; i += 2) pairs.push([cells[i], cells[i + 1]]);
    });
  });
  root.querySelectorAll('div,li,p,span,label').forEach(el => {
    if (el.closest('table') || !vis(el)) return;
    const kids = [...el.children];
    if (kids.length === 2) {
      const a = kids[0], b = kids[1];
      const ta = clean(a.innerText), tb = clean(b.innerText);
      if (ta && tb && ta.length <= 70 && tb.length <= 300 && a.children.length === 0 && b.children.length === 0) {
        pairs.push([ta, tb]);
      }
    }
  });
  const texto = (root.innerText || '').trim();
  return {pairs, tables, texto};
}
"""


async def _async_find_regex(page, patron: str):
    """Primer elemento visible cuyo texto matchea el patrón (insensible a tildes/mayúsculas)."""
    loc = page.get_by_text(re.compile(patron, re.IGNORECASE))
    n = await loc.count()
    for i in range(min(n, 5)):
        el = loc.nth(i)
        if await el.is_visible():
            return el
    return None


async def _leer_seccion_adicional(page, nombre: str) -> Dict[str, Any]:
    """Expande (si hace falta) y lee una sección de Consultas Adicionales."""
    vacio = {"disponible": False, "mensaje": "No disponible para este vehículo.", "datos": {}, "tablas": [], "texto": ""}
    cab = await _async_find_regex(page, _accent_rx(nombre))
    if cab is None:
        return vacio
    try:
        await cab.scroll_into_view_if_needed()
        expandido = await cab.get_attribute("aria-expanded")
        if expandido != "true":
            await cab.click(timeout=2500)
            await page.wait_for_timeout(350)

        otros = [s for s in SECCIONES_ADICIONALES if s != nombre]
        # Subir desde la cabecera hasta encontrar un contenedor cuyo texto
        # crezca claramente respecto al solo título (ese es el bloque expandido).
        contenedor = await cab.evaluate_handle(
            """(h, others) => {
                const nrm = s => (s||'').normalize('NFD').replace(/[\\u0300-\\u036f]/g,'').toLowerCase();
                const baseLen = (h.innerText || '').length;
                let el = h, found = null;
                for (let i = 0; i < 6 && el.parentElement; i++) {
                    el = el.parentElement;
                    const t = nrm(el.innerText);
                    if (others.some(o => t.includes(nrm(o)))) break;
                    found = el;
                    if ((el.innerText || '').length > baseLen + 10) break;
                }
                return found || h;
            }""",
            otros,
        )
        datos_js = await page.evaluate(_COLLECT_JS, contenedor)
        texto = datos_js.get("texto", "")
        nn = _norm_simple(nombre)
        lineas = [l for l in texto.splitlines() if l.strip() and not _norm_simple(l).startswith(nn)]
        texto = "\n".join(lineas).strip()

        datos: Dict[str, str] = {}
        for k, v in datos_js.get("pairs", []):
            k = re.sub(r"\s+", " ", str(k or "")).strip().rstrip(":").strip()
            v = re.sub(r"\s+", " ", str(v or "")).strip()
            if k.isupper() and len(k) > 3:
                k = k[:1] + k[1:].lower()
            if k and v and _norm_simple(k) != _norm_simple(v) and k not in datos:
                datos[k] = v

        tablas = datos_js.get("tables", [])  # la función JS devuelve "tables" (inglés), no "tablas"
        if not datos and not tablas and len(texto) < 250:
            return {**vacio, "mensaje": texto or vacio["mensaje"], "texto": texto}
        if not datos and not tablas and not texto:
            return vacio
        return {"disponible": True, "mensaje": "", "datos": datos, "tablas": tablas, "texto": texto[:4000]}
    except Exception as e:
        return {**vacio, "mensaje": f"No se pudo leer: {str(e).splitlines()[0]}"}


def _norm_simple(s: str) -> str:
    s = unicodedata.normalize("NFD", str(s or ""))
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return re.sub(r"\s+", " ", s.lower()).strip(" :")


class CaptchaHandler:
    """
    CAPTCHA del portal oficial RUNT.
    Busca la imagen cerca del texto "Captcha" / "caracteres presentados"
    y el input asociado. OCR opcional + fallback manual.
    """

    def __init__(self):
        self.last_image_b64: Optional[str] = None
        self.last_captcha_id: Optional[str] = None

    async def _find_captcha_img(self, page: Page):
        """Localiza la imagen REAL del CAPTCHA (no logos RUNT)."""
        for sel in (
            "img[src*='captcha' i]",
            "img[alt*='captcha' i]",
            "img[id*='captcha' i]",
            "canvas[id*='captcha' i]",
        ):
            loc = page.locator(sel)
            try:
                if await loc.count() > 0 and await loc.first.is_visible():
                    return loc.first
            except Exception:
                pass

        imgs = page.locator("img:visible")
        n = await imgs.count()
        best = None
        best_score = -1
        for i in range(min(n, 25)):
            el = imgs.nth(i)
            try:
                box = await el.bounding_box()
                if not box:
                    continue
                w, h = box["width"], box["height"]
                if not (80 <= w <= 350 and 25 <= h <= 120):
                    continue
                src = (await el.get_attribute("src") or "").lower()
                alt = (await el.get_attribute("alt") or "").lower()
                if any(k in src or k in alt for k in ("logo", "runtpro", "historico", "banner", "icon")):
                    continue
                score = w * h
                if "captcha" in src or "captcha" in alt:
                    score += 50000
                if score > best_score:
                    best_score = score
                    best = el
            except Exception:
                continue
        return best

    async def capture(self, page: Page) -> Dict[str, Any]:
        """Captura SOLO la imagen del CAPTCHA (no logos)."""
        try:
            await page.wait_for_function(
                """() => {
                    const t = (document.body && document.body.innerText) || '';
                    return /captcha|caracteres presentados/i.test(t);
                }""",
                timeout=15000
            )
        except Exception:
            pass

        await page.wait_for_timeout(250)
        img = await self._find_captcha_img(page)

        try:
            if img is not None:
                src = await img.get_attribute("src") or ""
                if src.startswith("data:image"):
                    b64 = src.split(",", 1)[1] if "," in src else src
                else:
                    png = await img.screenshot(type="png")
                    b64 = base64.b64encode(png).decode()
            else:
                b64 = await self._capture_captcha_region(page)
                if not b64:
                    return {"success": False, "error": "No se encontró la imagen del CAPTCHA"}
            self.last_image_b64 = b64
            return {"image_b64": b64, "captcha_id": "", "success": True}
        except Exception as e:
            return {"success": False, "error": f"No se pudo capturar CAPTCHA: {e}"}

    async def _capture_captcha_region(self, page: Page) -> Optional[str]:
        """Screenshot de la zona del captcha por texto guía."""
        try:
            top = page.get_by_text(re.compile(r"Digite los caracteres|caracteres presentados", re.I)).first
            bottom = page.get_by_text(re.compile(r"Captcha", re.I)).first
            if await top.count() == 0:
                return None
            await top.scroll_into_view_if_needed()
            await page.wait_for_timeout(100)
            tb = await top.bounding_box()
            if not tb:
                return None
            bb = await bottom.bounding_box() if await bottom.count() > 0 else None
            top_y = tb["y"] + tb["height"]
            bot_y = bb["y"] if bb else top_y + 70
            alto = max(40, min(bot_y - top_y, 120))
            vp = page.viewport_size or {"width": 1280, "height": 900}
            x = max(0, (vp["width"] - 320) / 2)
            png = await page.screenshot(clip={"x": x, "y": top_y, "width": 320, "height": alto})
            return base64.b64encode(png).decode()
        except Exception:
            return None

    async def fill(self, page: Page, code: str) -> None:
        """Rellena el código del CAPTCHA."""
        code = code.strip()
        # Input por label / placeholder / name
        selectors = [
            page.get_by_label(re.compile(r"captcha", re.I)),
            page.get_by_placeholder(re.compile(r"caracteres|captcha|imagen", re.I)),
            page.locator("input[formcontrolname*='captcha' i], input[name*='captcha' i], input[id*='captcha' i]"),
            page.locator("input[type='text']").last,
        ]
        for loc in selectors:
            try:
                if await loc.count() > 0 and await loc.first.is_visible():
                    await loc.first.fill(code)
                    return
            except Exception:
                continue
        # Último recurso: el input vacío visible cerca del texto Captcha
        await page.locator("input:visible").last.fill(code)

    def try_ocr(self, image_b64: Optional[str] = None) -> Dict[str, Any]:
        b64 = image_b64 or self.last_image_b64
        if not b64:
            return {"text": None, "needs_manual": True, "confidence": 0.0, "available": False}
        result = ocr_recognize(b64)
        text = (result.get("text") or "").strip()
        basura = {"runt", "runtpro", "transporte", "consulta", "placa", "captcha", "pro"}
        if text.lower() in basura or len(text) < 3:
            result["text"] = None
            result["needs_manual"] = True
            result["confidence"] = 0.0
        return result


class RuntService:
    """Servicio principal de automatización de consultas RUNT."""

    def __init__(self, headless: bool = True):
        self.headless = headless
        self.browser: Optional[Browser] = None
        self.context: Optional[BrowserContext] = None
        self.page: Optional[Page] = None
        self.captcha = CaptchaHandler()
        self._playwright = None
        self.last_placa: str = ""
        self.last_documento: str = ""

    async def start(self) -> None:
        """Inicia el navegador Playwright."""
        self._playwright = await async_playwright().start()
        self.browser = await self._playwright.chromium.launch(
            headless=self.headless,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"]
        )
        self.context = await self.browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            locale="es-CO"
        )
        self.page = await self.context.new_page()

    async def stop(self) -> None:
        """Cierra el navegador."""
        if self.browser:
            await self.browser.close()
        if self._playwright:
            await self._playwright.stop()
        self.browser = None
        self.context = None
        self.page = None

    async def open_page(self) -> None:
        """
        Carga el formulario y ESPERA a que esté estable antes de seguir.
        No toca ningún desplegable.
        """
        if not self.page:
            raise RuntimeError("Navegador no iniciado. Llama a start() primero.")

        on_results = False
        try:
            body = await self.page.inner_text("body", timeout=2000)
            if re.search(r"PLACA\s+DEL\s+VEH|Realizar\s+otra\s+consulta", body, re.I):
                on_results = True
        except Exception:
            pass

        if on_results:
            clicked = False
            for loc in [
                self.page.get_by_role("button", name=re.compile(r"Realizar\s+otra\s+consulta", re.I)),
                self.page.get_by_text(re.compile(r"Realizar\s+otra\s+consulta", re.I)),
            ]:
                try:
                    if await loc.count() > 0 and await loc.first.is_visible():
                        await loc.first.click(timeout=4000)
                        clicked = True
                        break
                except Exception:
                    continue
            if not clicked:
                await self.page.goto(URL, wait_until="load", timeout=60000)
        else:
            await self.page.goto(URL, wait_until="load", timeout=60000)

        try:
            await self.page.keyboard.press("Escape")
        except Exception:
            pass

        # Esperar formulario REALMENTE listo: Nro. placa + captcha + inputs
        listo = False
        ultimo_error = ""
        for intento in range(4):
            try:
                await self.page.get_by_text(re.compile(r"Nro\.?\s*placa", re.I)).first.wait_for(
                    state="visible", timeout=10000
                )
                try:
                    await self.page.get_by_text(re.compile(r"Captcha|caracteres\s+presentados", re.I)).first.wait_for(
                        state="visible", timeout=8000
                    )
                except Exception:
                    pass
                inputs = self.page.locator(
                    "input[type='text']:visible, input:not([type]):visible, "
                    "input.mat-input-element:visible, input[type='tel']:visible"
                )
                if await inputs.count() >= 1:
                    await self.page.wait_for_timeout(1800)  # estabilizar Angular
                    await self.page.keyboard.press("Escape")
                    await self.page.wait_for_timeout(250)
                    listo = True
                    break
            except Exception as e:
                ultimo_error = str(e)
            try:
                await self.page.goto(URL, wait_until="load", timeout=60000)
                await self.page.wait_for_timeout(2000)
            except Exception as e:
                ultimo_error = str(e)

        if not listo:
            raise RuntimeError(
                "El formulario del portal RUNT no terminó de cargar. "
                f"Revisa la ventana del bot. Detalle: {ultimo_error[:120]}"
            )

    async def fill_formulario(self, placa: str, documento: str) -> None:
        """
        SOLO escribe en inputs de texto (placa y cédula).
        NUNCA hace clic en mat-select / Procedencia / Consultar Por.
        """
        page = self.page
        placa = placa.strip().upper()
        documento = str(documento).strip()

        await page.keyboard.press("Escape")
        await page.wait_for_timeout(100)
        await page.evaluate("() => window.scrollTo(0, 0)")
        await page.wait_for_timeout(80)

        text_inputs = page.locator(
            "input.mat-input-element:visible, "
            "input[type='text']:visible, "
            "input[type='tel']:visible, "
            "input:not([type]):visible"
        )

        n = await text_inputs.count()
        placa_el = None
        doc_el = None
        captcha_el = None

        for i in range(n):
            el = text_inputs.nth(i)
            try:
                if not await el.is_visible():
                    continue
                meta = await el.evaluate(
                    """(e) => {
                        const near = (e.closest('mat-form-field, .mat-form-field, .form-group, div') || e.parentElement);
                        const txt = (near && near.innerText) ? near.innerText : '';
                        return {
                            ph: e.placeholder || '',
                            name: e.name || '',
                            id: e.id || '',
                            fc: e.getAttribute('formcontrolname') || '',
                            aria: e.getAttribute('aria-label') || '',
                            near: txt.slice(0, 120)
                        };
                    }"""
                )
                blob = " ".join([
                    meta.get("ph", ""), meta.get("name", ""), meta.get("id", ""),
                    meta.get("fc", ""), meta.get("aria", ""), meta.get("near", "")
                ]).lower()

                if re.search(r"captcha|caracteres", blob) and captcha_el is None:
                    captcha_el = el
                elif re.search(r"placa", blob) and placa_el is None:
                    placa_el = el
                elif re.search(r"documento|propietario|cedula|cédula", blob) and doc_el is None:
                    doc_el = el
            except Exception:
                continue

        if placa_el is None and n >= 1:
            placa_el = text_inputs.nth(0)
        if doc_el is None and n >= 2:
            for i in range(n):
                el = text_inputs.nth(i)
                if placa_el is not None and i == 0:
                    # prefer another input
                    pass
                try:
                    if placa_el and await el.evaluate("(a,b) => a === b", placa_el):
                        continue
                except Exception:
                    pass
                if captcha_el is not None:
                    try:
                        if await el.evaluate("(a,b) => a === b", captcha_el):
                            continue
                    except Exception:
                        pass
                doc_el = el
                break

        if placa_el is None:
            raise RuntimeError("No se encontró el campo de placa (input de texto).")

        await placa_el.click()
        await placa_el.fill("")
        await placa_el.fill(placa)
        await page.wait_for_timeout(100)

        if doc_el is not None:
            await doc_el.click()
            await doc_el.fill("")
            await doc_el.fill(documento)
            await page.wait_for_timeout(100)
        else:
            await placa_el.focus()
            await page.keyboard.press("Tab")
            await page.wait_for_timeout(60)
            await page.keyboard.press("Tab")
            await page.wait_for_timeout(60)
            await page.keyboard.type(documento, delay=20)

        await page.mouse.wheel(0, 450)
        await page.wait_for_timeout(200)
        await page.keyboard.press("Escape")

    async def prepare_query(self, placa: str, documento: str, tipo_doc: str = "C") -> Dict[str, Any]:
        """Abre portal, llena formulario y captura CAPTCHA."""
        self.last_placa = placa.strip().upper()
        self.last_documento = str(documento).strip()
        await self.open_page()
        await self.fill_formulario(self.last_placa, self.last_documento)
        await self.page.wait_for_timeout(200)

        captcha_data = await self.captcha.capture(self.page)
        if not captcha_data.get("success"):
            return {
                "status": "Error",
                "placa": placa.upper().strip(),
                "documento": str(documento).strip(),
                "error": captcha_data.get("error", "No se pudo capturar CAPTCHA")
            }

        ocr = self.captcha.try_ocr(captcha_data["image_b64"])
        captcha_data["ocr"] = ocr

        return {
            "status": "CAPTCHA",
            "placa": placa.upper().strip(),
            "documento": str(documento).strip(),
            "captcha": captcha_data,
            "ocr_text": ocr.get("text"),
            "ocr_confidence": ocr.get("confidence", 0),
            "needs_manual": ocr.get("needs_manual", True)
        }

    async def try_auto_submit(self, max_retries: int = MAX_OCR_RETRIES) -> Dict[str, Any]:
        """
        Hasta max_retries (5) intentos. Si el captcha falla, REFRESCA la página
        completa y repite el flujo (formulario + captcha + envío).
        Detecta éxito (Completado) vs captcha inválido vs otros errores.
        """
        if not self.page:
            raise RuntimeError("Página no disponible")

        placa = self.last_placa
        documento = self.last_documento
        last_image_b64 = self.captcha.last_image_b64
        attempts_log = []

        for attempt in range(1, max_retries + 1):
            # Intentos 2+ → recargar página y volver a llenar el formulario
            if attempt > 1:
                try:
                    await self.open_page()  # goto o "Realizar otra consulta"
                    if placa and documento:
                        await self.fill_formulario(placa, documento)
                        await self.page.wait_for_timeout(200)
                except Exception as e:
                    attempts_log.append({"attempt": attempt, "error": f"recarga: {e}"})
                    continue

            captcha_data = await self.captcha.capture(self.page)
            if not captcha_data.get("success"):
                attempts_log.append({"attempt": attempt, "error": "captura fallida"})
                continue

            last_image_b64 = captcha_data["image_b64"]
            ocr = self.captcha.try_ocr(last_image_b64)
            text_ocr = ocr.get("text")
            conf = ocr.get("confidence", 0)
            attempts_log.append({
                "attempt": attempt,
                "text": text_ocr,
                "confidence": conf,
                "needs_manual": ocr.get("needs_manual"),
            })

            if not text_ocr or ocr.get("needs_manual"):
                # Sin lectura útil → recargar en el siguiente intento
                continue

            result = await self.submit_with_captcha(text_ocr)

            if result.get("status") == "Completado":
                result["ocr_attempts"] = attempts_log
                result["ocr_used"] = text_ocr
                result["attempt"] = attempt
                return result

            if result.get("status") == "Sin información":
                result["ocr_attempts"] = attempts_log
                result["ocr_used"] = text_ocr
                return result

            err = (result.get("error") or "").lower()
            if result.get("status") == "Error" and any(
                k in err for k in ("captcha", "código", "codigo", "verificación", "verificacion", "incorrect", "válido", "valido")
            ):
                # Captcha malo → siguiente intento con página limpia
                continue

            # Otro error (red, timeout, etc.): devolver
            result["ocr_attempts"] = attempts_log
            result["ocr_used"] = text_ocr
            return result

        # Agotados → pedir captcha manual con la última imagen
        # Asegurar imagen fresca
        try:
            await self.open_page()
            if placa and documento:
                await self.fill_formulario(placa, documento)
                await self.page.wait_for_timeout(200)
            captcha_data = await self.captcha.capture(self.page)
            if captcha_data.get("success"):
                last_image_b64 = captcha_data["image_b64"]
        except Exception:
            pass

        return {
            "status": "CAPTCHA",
            "needs_manual": True,
            "captcha": {
                "image_b64": last_image_b64,
                "success": bool(last_image_b64),
            },
            "ocr_attempts": attempts_log,
            "message": f"OCR no resolvió el CAPTCHA tras {max_retries} intentos (con recarga de página). Escribe el código manualmente.",
        }

    async def submit_with_captcha(self, captcha_code: str) -> Dict[str, Any]:
        """
        Rellena CAPTCHA, pulsa "Consultar Información" y espera el resultado
        en el portal oficial RUNT.
        """
        if not self.page:
            raise RuntimeError("Página no disponible")

        await self.captcha.fill(self.page, captcha_code)
        await self.page.wait_for_timeout(100)

        # Enviar con Enter (como indicaste); si no, clic en el botón
        submitted = False
        try:
            await self.page.keyboard.press("Enter")
            submitted = True
        except Exception:
            pass
        if not submitted:
            for loc in [
                self.page.get_by_role("button", name=re.compile(r"Consultar\s+Informaci", re.I)),
                self.page.get_by_text(re.compile(r"Consultar\s+Informaci", re.I)),
                self.page.locator("button:has-text('Consultar'), button[type='submit']"),
            ]:
                try:
                    if await loc.count() > 0 and await loc.first.is_visible():
                        await loc.first.click(timeout=4000)
                        submitted = True
                        break
                except Exception:
                    continue
        if not submitted:
            return {"status": "Error", "error": "No se pudo enviar el formulario (Enter / botón)"}

        # Esperar resultado o error.
        #
        # OJO - bug ya corregido una vez, no reintroducirlo: la condición
        # anterior aceptaba el resultado como "listo" con solo ver la
        # palabra "Aceptar", "error" o "mat-error" en CUALQUIER parte de la
        # página junto con "documento"/"placa"/"captcha"/"válido". El
        # problema es que esas palabras YA están en el propio formulario
        # (sus etiquetas) o en un aviso de cookies con botón "Aceptar", así
        # que esa condición se cumplía casi al instante, antes de que la
        # consulta real terminara. Eso producía un PDF "Completado" con una
        # captura del formulario y el spinner de carga todavía en pantalla,
        # sin ningún dato real. Por eso aquí solo se aceptan señales
        # concretas: el encabezado del resultado, el botón de "otra
        # consulta", o un mensaje de error/sin-resultados específico.
        try:
            await self.page.wait_for_function(
                """() => {
                    const t = (document.body && document.body.innerText) || '';
                    if (/PLACA\\s+DEL\\s+VEH[IÍ]CULO|Informaci[oó]n\\s+general\\s+del\\s+veh[ií]culo/i.test(t)) return true;
                    if (/Realizar\\s+otra\\s+consulta/i.test(t)) return true;
                    if (/captcha\\s+no\\s+es\\s+v[aá]lido|captcha\\s+incorrect|c[oó]digo\\s+incorrect|caracteres\\s+incorrect/i.test(t)) return true;
                    if (/no\\s+se\\s+encontr|sin\\s+resultados|no\\s+registra|no\\s+hay\\s+informaci/i.test(t)) return true;
                    return false;
                }""",
                timeout=45000
            )
        except Exception as e:
            try:
                body_text = await self.page.inner_text("body")
                if not re.search(r"PLACA\s+DEL\s+VEH|Informaci[oó]n\s+general", body_text, re.I):
                    return {
                        "status": "Error",
                        "error": f"Timeout esperando respuesta del portal: {str(e)}"
                    }
            except Exception:
                return {
                    "status": "Error",
                    "error": f"Timeout esperando respuesta del portal: {str(e)}"
                }

        # El sitio puede mostrar un spinner/overlay de carga un instante
        # más después de que el texto ya cambió (p. ej. mientras termina de
        # pintar los datos). Esperar a que desaparezca antes de leer o
        # capturar nada evita tomar la foto a mitad de la transición.
        await self._esperar_sin_overlay(self.page)

        body_text = ""
        try:
            body_text = await self.page.inner_text("body")
        except Exception:
            pass

        # Modal "El captcha no es valido" → cerrar y reportar
        if re.search(r"captcha\s+no\s+es\s+v[aá]lido|captcha\s+incorrect|c[oó]digo\s+incorrect|caracteres\s+incorrect", body_text, re.I):
            try:
                btn = self.page.get_by_role("button", name=re.compile(r"Aceptar", re.I))
                if await btn.count() > 0:
                    await btn.first.click(timeout=3000)
                else:
                    await self.page.get_by_text(re.compile(r"^Aceptar$", re.I)).first.click(timeout=3000)
                await self.page.wait_for_timeout(200)
            except Exception:
                try:
                    await self.page.keyboard.press("Escape")
                except Exception:
                    pass
            return {"status": "Error", "error": "CAPTCHA incorrecto"}
        if re.search(r"no\s+se\s+encontr|sin\s+resultados|no\s+registra|no\s+hay\s+informaci", body_text, re.I):
            if not re.search(r"PLACA\s+DEL\s+VEH", body_text, re.I):
                return {
                    "status": "Sin información",
                    "error": "No se encontró información para la placa y cédula proporcionadas"
                }

        # Verificación extra antes de dar por bueno el resultado: si no hay
        # un error conocido pero tampoco aparece todavía "Placa del
        # vehículo", es probable que la página siga cargando. Se espera un
        # poco más en vez de aceptar un "Completado" sin datos reales (que
        # es justo lo que pasaba antes de este arreglo).
        if not re.search(r"PLACA\s+DEL\s+VEH", body_text, re.I):
            encontrado = False
            for _ in range(20):
                await self.page.wait_for_timeout(500)
                try:
                    body_text = await self.page.inner_text("body")
                except Exception:
                    continue
                if re.search(r"PLACA\s+DEL\s+VEH", body_text, re.I):
                    encontrado = True
                    break
                if re.search(r"no\s+se\s+encontr|sin\s+resultados|no\s+registra|no\s+hay\s+informaci", body_text, re.I):
                    return {
                        "status": "Sin información",
                        "error": "No se encontró información para la placa y cédula proporcionadas"
                    }
                if re.search(r"captcha\s+no\s+es\s+v[aá]lido|captcha\s+incorrect|c[oó]digo\s+incorrect", body_text, re.I):
                    return {"status": "Error", "error": "CAPTCHA incorrecto"}
            if not encontrado:
                return {
                    "status": "Error",
                    "error": "El portal no terminó de cargar el resultado (no se encontró 'Placa del vehículo' tras esperar)."
                }

        data = await self._extract_results()
        principal = data.get("principal") or {}
        general = data.get("general") or {}
        if not principal and not general and len((data.get("raw_text") or "")) < 40:
            return {
                "status": "Error",
                "error": "Se obtuvo respuesta del portal pero no se pudieron extraer los datos"
            }
        data["status"] = "Completado"
        return data

    async def _extract_results(self) -> Dict[str, Any]:
        """
        Portal oficial: todo el resultado sale de una en la pantalla.
        Solo lee el texto visible y toma UNA captura del bloque completo.
        Sin abrir secciones ni buscar acordeones.
        """
        page = self.page
        result: Dict[str, Any] = {
            "principal": {},
            "general": {},
            "adicionales": {},
            "raw_text": "",
            "captura_adicionales_png": None,
        }

        await page.wait_for_timeout(120)

        raw = ""
        try:
            raw = await page.inner_text("body", timeout=3000)
        except Exception:
            pass
        result["raw_text"] = raw or ""

        combinados = {**PRINCIPAL_LABELS, **GENERAL_LABELS}
        encontrados = _scan_labels(raw, combinados)
        result["principal"] = {k: v for k, v in encontrados.items() if k in PRINCIPAL_LABELS}
        result["general"] = {k: v for k, v in encontrados.items() if k in GENERAL_LABELS}

        fecha_m = re.search(r"consulta realizada el\s+(.+?)(?:\s*[·|]|\n|$)", raw, re.IGNORECASE)
        if fecha_m:
            result["consulta_portal"] = fecha_m.group(1).strip()

        # Una sola captura de todo lo que aparece en pantalla (esto es lo que
        # ya funciona bien; se captura PRIMERO para no tocarlo con nada de
        # lo que sigue).
        result["captura_png"] = await self._capturar_resultado(page)

        # Póliza SOAT: vive en un acordeón aparte, más abajo en la misma
        # página («todo está ahí mismo», solo hay que abrirlo). Es
        # independiente de la captura de arriba — si falla, no afecta el
        # resultado principal, que ya quedó capturado.
        try:
            result["soat"] = await self._extraer_soat(page)
        except Exception as e:
            result["soat"] = {"disponible": False, "mensaje": f"No se pudo leer: {str(e).splitlines()[0]}"}

        # Estado del SOAT para la columna "Póliza" de la vista previa:
        # se busca por texto plano DESPUÉS de haber expandido la sección
        # (si estaba colapsada, su texto no aparecía todavía). Se busca por
        # texto, no por estructura de tabla, porque el portal puede armar
        # esto con <table>, <mat-table> o simples <div>.
        try:
            texto_tras_expandir = await page.inner_text("body", timeout=3000)
        except Exception:
            texto_tras_expandir = raw
        soat_m = re.search(_accent_rx("Póliza SOAT"), texto_tras_expandir, re.IGNORECASE)
        if soat_m:
            ventana = texto_tras_expandir[soat_m.end():soat_m.end() + 600]
            estado_m = re.search(r"\b(VIGENTE|VENCID[OA]|CANCELAD[OA]|SUSPENDID[OA]|NO\s+APLICA)\b", ventana, re.IGNORECASE)
            if estado_m:
                result["soat_estado"] = estado_m.group(1).upper()

        return result

    async def _extraer_soat(self, page: Page) -> Optional[Dict[str, Any]]:
        """Busca el texto 'Póliza SOAT' (como un Ctrl+F), hace clic en el '+'
        para expandirla y CAPTURA la sección como imagen — no intenta leer
        su estructura (Angular Material rara vez usa una <table> real, así
        que adivinar filas/columnas es frágil; una captura muestra lo mismo
        sin importar cómo esté armado el HTML por dentro)."""
        header = await _async_find_regex(page, _accent_rx("Póliza SOAT"))
        if header is None:
            return {"disponible": False, "mensaje": "No se encontró la sección 'Póliza SOAT' en la página."}

        await header.scroll_into_view_if_needed()
        await page.wait_for_timeout(150)
        await self._click_expandir_cerca(page, header)
        await page.wait_for_timeout(600)

        png = await self._capturar_seccion(page, header, [
            r"p[oó]lizas\s+de\s+responsabilidad\s+civil", r"p[oó]liza\s+andina",
            r"certificado", r"realizar\s+otra\s+consulta", r"copyright\s+\d{4}\s+runt",
        ])
        if not png:
            return {"disponible": False, "mensaje": "No se pudo capturar la sección 'Póliza SOAT'."}
        return {"disponible": True, "captura_png": png}

    async def _click_expandir_cerca(self, page: Page, header) -> None:
        """Hace clic en el '+' (o ícono de expandir) que está junto al
        encabezado, no en el texto del encabezado mismo — así es como
        realmente se abre el acordeón en el portal oficial. Si no encuentra
        ningún ícono cercano, hace clic en el propio encabezado como
        respaldo."""
        try:
            hbox = await header.bounding_box()
        except Exception:
            hbox = None
        if not hbox:
            return
        y_centro = hbox["y"] + hbox["height"] / 2

        objetivo = None
        try:
            candidatos = page.locator(
                "button, [role='button'], mat-icon, .mat-icon, "
                "[class*='expand' i], [class*='toggle' i], [class*='chevron' i], "
                "[class*='arrow' i], [class*='icon' i]"
            )
            mejor_dist = 999999
            for i in range(min(await candidatos.count(), 80)):
                el = candidatos.nth(i)
                try:
                    if not await el.is_visible():
                        continue
                    box = await el.bounding_box()
                except Exception:
                    continue
                if not box:
                    continue
                # Misma fila que el encabezado (tolerancia vertical) y a su derecha
                if abs((box["y"] + box["height"] / 2) - y_centro) < 18 and box["x"] >= hbox["x"] - 5:
                    dist = box["x"] - hbox["x"]
                    if 0 <= dist < mejor_dist:
                        mejor_dist = dist
                        objetivo = el
        except Exception:
            objetivo = None

        for candidato in (objetivo, header):
            if candidato is None:
                continue
            try:
                await candidato.click(timeout=2500)
                return
            except Exception:
                continue

    async def _capturar_seccion(self, page: Page, header, patrones_corte, max_alto: int = 900) -> Optional[str]:
        """Captura en imagen desde 'header' hasta el primer patrón de
        'patrones_corte' que aparezca más abajo (o hasta max_alto si no
        encuentra ninguno). Misma idea que _capturar_resultado, mismo
        patrón: solo cambia qué tan abajo se busca el límite."""
        try:
            await header.scroll_into_view_if_needed()
            await page.wait_for_timeout(100)
            top_box = await header.bounding_box()
            if not top_box:
                return None
            top_y = max(0, top_box["y"] - 6)

            vp = page.viewport_size or {"width": 1280, "height": 900}
            ancho = vp["width"]

            bottom_y = None
            for patron in patrones_corte:
                cand = await _async_find_regex(page, patron)
                if cand is None:
                    continue
                try:
                    cbox = await cand.bounding_box()
                except Exception:
                    continue
                if cbox and cbox["y"] > top_y + 10:
                    bottom_y = cbox["y"] if bottom_y is None else min(bottom_y, cbox["y"])

            alto = (bottom_y - top_y) if bottom_y else max_alto
            alto = max(80, min(alto, max_alto))

            if top_y + alto > vp["height"]:
                await page.set_viewport_size({"width": ancho, "height": int(top_y + alto + 100)})
                await page.wait_for_timeout(100)

            png = await page.screenshot(clip={"x": 0, "y": top_y, "width": ancho, "height": alto})
            return base64.b64encode(png).decode()
        except Exception:
            return None

    async def _esperar_sin_overlay(self, page: Page, intentos: int = 16) -> None:
        """Espera a que desaparezca cualquier spinner/overlay de carga visible
        (hasta ~intentos*300ms). Si no hay ninguno, retorna de inmediato."""
        selectores = [
            "mat-spinner", ".mat-spinner", ".spinner", "[class*='spinner' i]",
            "[class*='loading' i]", "[class*='overlay' i]", "[role='progressbar']",
            ".cdk-overlay-container",
        ]
        for _ in range(intentos):
            visible = False
            for sel in selectores:
                try:
                    loc = page.locator(sel)
                    if await loc.count() and await loc.first.is_visible():
                        visible = True
                        break
                except Exception:
                    continue
            if not visible:
                return
            await page.wait_for_timeout(200)

    async def _capturar_resultado(self, page: Page) -> Optional[str]:
        """Captura todo el bloque de resultado visible (una sola imagen rápida).

        OJO: a propósito NO hay un "screenshot ciego" de respaldo cuando no
        se encuentra "Placa del vehículo". Antes lo había, y producía PDFs
        que mostraban el formulario con el spinner de carga en vez del
        resultado real, marcados igual como "Completado". Es mejor devolver
        None (sin imagen) que una captura que parece un resultado y no lo
        es. Este método ahora solo se llama después de confirmar en
        submit_with_captcha que el resultado sí cargó, así que no encontrar
        el encabezado aquí sería inesperado.
        """
        try:
            # Ampliar viewport para que quepa el contenido
            vp = page.viewport_size or {"width": 1280, "height": 900}
            ancho = vp["width"]
            await page.set_viewport_size({"width": ancho, "height": 2200})
            await page.wait_for_timeout(100)

            # Ancla superior: PLACA DEL VEHÍCULO o título de la consulta
            heading = await _async_find_regex(page, _accent_rx("PLACA DEL VEHÍCULO"))
            if heading is None:
                heading = await _async_find_regex(page, _accent_rx("Consulta Automotores"))
            if heading is None:
                return None

            await heading.scroll_into_view_if_needed()
            await page.wait_for_timeout(80)
            top_box = await heading.bounding_box()
            if not top_box:
                return None

            top_y = max(0, top_box["y"] - 8)

            # Cortar justo antes de lo primero que venga después de
            # "Información general": el botón de otra consulta, el pie de
            # página, o el primer acordeón (Datos Técnicos, Póliza SOAT,
            # etc.). Así la captura no alcanza a mostrar el título de esas
            # secciones por encima (quedarían repetidos: una vez como texto
            # suelto al final de la imagen, y otra vez como su propia
            # sección aparte, como la de Póliza SOAT más abajo).
            patrones_corte = [
                r"realizar\s+otra\s+consulta", r"copyright\s+\d{4}\s+runt",
                r"datos\s+t[eé]cnicos\s+del\s+veh[ií]culo", r"p[oó]liza\s+soat",
                r"p[oó]lizas\s+de\s+responsabilidad\s+civil", r"certificado",
            ]
            bottom_y = None
            for patron in patrones_corte:
                cand = await _async_find_regex(page, patron)
                if cand is None:
                    continue
                cbox = await cand.bounding_box()
                if cbox and cbox["y"] > top_y:
                    bottom_y = cbox["y"] if bottom_y is None else min(bottom_y, cbox["y"])

            alto = (bottom_y - top_y) if bottom_y else 1500
            alto = max(200, min(alto, 1800))

            png = await page.screenshot(clip={"x": 0, "y": top_y, "width": ancho, "height": alto})
            return base64.b64encode(png).decode()
        except Exception:
            # Sin captura "ciega" de respaldo aquí tampoco, por la misma
            # razón de arriba: es preferible no mostrar imagen a mostrar
            # una que no corresponde al resultado real.
            return None

    def _find_value_near_label(self, text: str, labels: List[str]) -> Optional[str]:
        """Busca el valor asociado a una etiqueta en el texto plano del resultado."""
        for label in labels:
            # Patrones comunes: LABEL\nVALUE  o  LABEL | VALUE  o  LABEL VALUE
            patterns = [
                rf"{re.escape(label)}\s*[:\|\n]\s*([^\n\|]+)",
                rf"{re.escape(label)}\s+([A-Z0-9ÁÉÍÓÚÑ\-\.\/\s]{{2,60}}?)(?:\n|$|\|)",
            ]
            for pat in patterns:
                m = re.search(pat, text, re.IGNORECASE)
                if m:
                    val = m.group(1).strip()
                    # Limpiar basura
                    val = re.sub(r"\s{2,}", " ", val)
                    if val and val not in ("—", "-", "–", "N/A", "NO APLICA"):
                        return val
        return None

    async def refresh_captcha(self) -> Dict[str, Any]:
        """Fuerza una nueva imagen de CAPTCHA (recarga el formulario o clic en refresh)."""
        if not self.page:
            return {"success": False}
        # Intentar botón de recarga del captcha
        for sel in [
            "button[aria-label*='refresh' i], button[title*='refresh' i]",
            "button[aria-label*='actualiz' i], button[title*='actualiz' i]",
            "mat-icon:has-text('refresh'), .mat-icon:has-text('refresh')",
        ]:
            try:
                btn = self.page.locator(sel)
                if await btn.count() > 0 and await btn.first.is_visible():
                    await btn.first.click()
                    await self.page.wait_for_timeout(800)
                    return await self.captcha.capture(self.page)
            except Exception:
                continue
        # Fallback: recargar la página y rellenar de nuevo no es ideal aquí;
        # solo re-capturar la imagen actual
        await self.page.wait_for_timeout(300)
        return await self.captcha.capture(self.page)


# Helper síncrono para uso desde Flask (evita problemas de event loop)
# ---------------------------------------------------------------------------
# Puente síncrono -> asíncrono para Flask.
#
# OJO: esto NO puede ser "crear un event loop nuevo por llamada"
# (asyncio.run(...) en cada una). El navegador, el contexto y la página de
# Playwright quedan guardados en la instancia de RuntService y se usan en
# llamadas sucesivas desde distintas rutas de Flask (prepare_query en
# /api/start, submit_with_captcha en /api/submit_captcha, etc.). La conexión
# interna de Playwright con el proceso del navegador queda atada al event
# loop en el que se creó (async_playwright().start()); si esa llamada se hizo
# dentro de un asyncio.run() que ya se cerró, esa conexión queda huérfana y la
# siguiente operación (p. ej. page.goto) revienta con errores como
# "'NoneType' object has no attribute 'send'".
#
# La solución es tener UN solo event loop, corriendo todo el tiempo en un
# hilo de fondo, y mandarle ahí todas las corrutinas con
# run_coroutine_threadsafe. Así el navegador vive en el mismo loop del
# principio al final, sin importar cuántas peticiones HTTP distintas lo usen.
# ---------------------------------------------------------------------------
_loop: Optional[asyncio.AbstractEventLoop] = None
_loop_thread: Optional[threading.Thread] = None
_loop_lock = threading.Lock()


def _get_loop() -> asyncio.AbstractEventLoop:
    global _loop, _loop_thread
    with _loop_lock:
        if _loop is None or _loop.is_closed():
            _loop = asyncio.new_event_loop()
            _loop_thread = threading.Thread(target=_loop.run_forever, daemon=True, name="runt-asyncio-loop")
            _loop_thread.start()
        return _loop


def run_async(coro):
    """Ejecuta una corrutina en el event loop persistente y espera el resultado."""
    loop = _get_loop()
    future = asyncio.run_coroutine_threadsafe(coro, loop)
    return future.result()
