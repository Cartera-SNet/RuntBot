"""
Módulo aislado de OCR para CAPTCHA con Tesseract.
- Captura solo el <img> del CAPTCHA (no pantalla completa)
- Preprocesa la imagen (grayscale, escala, contraste, umbral)
- Dos niveles de OCR (normal + procesado)
- Limpieza del texto reconocido
- Fallback a modo manual cuando la confianza es baja
"""

from __future__ import annotations

import base64
import io
import re
from typing import Optional, Dict, Any, Tuple

from PIL import Image, ImageOps, ImageFilter, ImageEnhance

try:
    import pytesseract
    TESSERACT_AVAILABLE = True
except ImportError:
    TESSERACT_AVAILABLE = False


# ---------------------------------------------------------------------------
# Configuración
# ---------------------------------------------------------------------------
# Caracteres típicos de este CAPTCHA (alfanumérico, sin confusiones extremas)
ALLOWED_CHARS = set("ABCDEFGHJKLMNPQRSTUVWXYZabcdefghjkmnpqrstuvwxyz0123456789")
# Confusiones frecuentes que Tesseract comete
CONFUSION_MAP = {
    "O": "0", "o": "0",
    "I": "1", "l": "1", "|": "1",
    "S": "5", "s": "5",
    "Z": "2", "z": "2",
    "B": "8",
    "G": "6",
    " ": "",
}

# Mínima longitud esperada del CAPTCHA (según el video ~4-6 caracteres)
MIN_LEN = 3
MAX_LEN = 8


def _b64_to_image(b64: str) -> Image.Image:
    """Convierte base64 (con o sin prefijo data:) a PIL Image."""
    if "," in b64:
        b64 = b64.split(",", 1)[1]
    raw = base64.b64decode(b64)
    return Image.open(io.BytesIO(raw)).convert("RGB")


def _preprocess(img: Image.Image, mode: str = "processed") -> Image.Image:
    """
    Preprocesa la imagen del CAPTCHA.

    mode:
      - "raw"       → casi original (solo escala)
      - "processed" → grayscale + contraste + umbral
      - "aggressive"→ más agresivo (filtro + umbral fuerte)
    """
    # Escalar ×3 para que Tesseract tenga más píxeles
    w, h = img.size
    img = img.resize((w * 3, h * 3), Image.Resampling.LANCZOS)

    if mode == "raw":
        return img

    # Escala de grises
    img = ImageOps.grayscale(img)

    if mode == "processed":
        # Aumentar contraste
        img = ImageEnhance.Contrast(img).enhance(2.0)
        # Ligero sharpen
        img = img.filter(ImageFilter.SHARPEN)
        # Umbral adaptativo simple
        img = img.point(lambda x: 0 if x < 140 else 255)
    elif mode == "aggressive":
        img = ImageEnhance.Contrast(img).enhance(2.5)
        img = img.filter(ImageFilter.MedianFilter(size=3))
        img = img.point(lambda x: 0 if x < 160 else 255)

    return img


def _clean_text(text: str) -> str:
    """Limpia el resultado de Tesseract."""
    if not text:
        return ""
    # Quitar espacios y saltos
    text = re.sub(r"\s+", "", text)
    # Solo caracteres alfanuméricos
    text = "".join(c for c in text if c.isalnum())
    # Corregir confusiones comunes solo si el resultado queda muy corto
    # (no forzar siempre, para no romper CAPTCHAs legítimos con O/0)
    return text.strip()


def _score_candidate(text: str) -> float:
    """
    Puntuación heurística de confianza (0-1).
    No es la confianza de Tesseract, sino reglas de plausibilidad.
    """
    if not text:
        return 0.0
    length_ok = MIN_LEN <= len(text) <= MAX_LEN
    alnum = sum(1 for c in text if c.isalnum()) / max(len(text), 1)
    allowed = sum(1 for c in text if c in ALLOWED_CHARS) / max(len(text), 1)
    score = 0.0
    if length_ok:
        score += 0.4
    score += 0.3 * alnum
    score += 0.3 * allowed
    return round(score, 3)


def recognize(image_b64: str) -> Dict[str, Any]:
    """
    Intenta reconocer el CAPTCHA con dos niveles de OCR.

    Returns:
        {
          "text": str | None,       # mejor candidato
          "confidence": float,      # 0-1 heurística
          "candidates": [...],      # lista de intentos
          "available": bool,        # Tesseract instalado
          "needs_manual": bool      # True si hay que pedir al usuario
        }
    """
    if not TESSERACT_AVAILABLE:
        return {
            "text": None,
            "confidence": 0.0,
            "candidates": [],
            "available": False,
            "needs_manual": True,
            "error": "pytesseract no está instalado"
        }

    try:
        img = _b64_to_image(image_b64)
    except Exception as e:
        return {
            "text": None,
            "confidence": 0.0,
            "candidates": [],
            "available": True,
            "needs_manual": True,
            "error": f"No se pudo decodificar la imagen: {e}"
        }

    configs = [
        ("raw",       "--psm 7 -c tessedit_char_whitelist=ABCDEFGHJKLMNPQRSTUVWXYZabcdefghjkmnpqrstuvwxyz0123456789"),
        ("processed", "--psm 7 -c tessedit_char_whitelist=ABCDEFGHJKLMNPQRSTUVWXYZabcdefghjkmnpqrstuvwxyz0123456789"),
        ("processed", "--psm 8 -c tessedit_char_whitelist=ABCDEFGHJKLMNPQRSTUVWXYZabcdefghjkmnpqrstuvwxyz0123456789"),
        ("aggressive","--psm 7 -c tessedit_char_whitelist=ABCDEFGHJKLMNPQRSTUVWXYZabcdefghjkmnpqrstuvwxyz0123456789"),
    ]

    candidates = []
    for mode, cfg in configs:
        try:
            processed = _preprocess(img, mode)
            raw_text = pytesseract.image_to_string(processed, config=cfg)
            cleaned = _clean_text(raw_text)
            score = _score_candidate(cleaned)
            candidates.append({
                "mode": mode,
                "config": cfg[:20],
                "raw": raw_text.strip(),
                "text": cleaned,
                "score": score
            })
        except Exception as e:
            candidates.append({
                "mode": mode,
                "text": "",
                "score": 0.0,
                "error": str(e)
            })

    # Ordenar por score
    candidates.sort(key=lambda c: c.get("score", 0), reverse=True)
    best = candidates[0] if candidates else None

    if best and best["score"] >= 0.55 and best["text"]:
        return {
            "text": best["text"],
            "confidence": best["score"],
            "candidates": candidates,
            "available": True,
            "needs_manual": False
        }

    return {
        "text": best["text"] if best else None,
        "confidence": best["score"] if best else 0.0,
        "candidates": candidates,
        "available": True,
        "needs_manual": True
    }
