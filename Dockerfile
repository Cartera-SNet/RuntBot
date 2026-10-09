# RUNT BOT — imagen para Railway (o cualquier Docker).
#
# Incluye, además de Python y las dependencias de requirements.txt:
#   - Chromium + sus librerías de sistema (Playwright las instala con
#     --with-deps, así no hay que listarlas una por una a mano).
#   - Tesseract OCR (el binario real; pytesseract solo es el envoltorio
#     en Python, necesita este paquete del sistema para funcionar).
#
# No usa una imagen base "slim" al estilo playwright/python oficial porque
# esa imagen no siempre trae la misma versión de Python que se probó en
# desarrollo; en cambio, se parte de python:3.12-slim y se deja que
# "playwright install --with-deps" resuelva las librerías de Chromium,
# que es el camino recomendado por Playwright para Debian/Ubuntu.

FROM python:3.12-slim

# Evita prompts interactivos de apt y mantiene la imagen más liviana.
ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Tesseract OCR (binario del sistema) + librerías básicas que Playwright
# necesita para poder instalar luego las suyas con --with-deps.
RUN apt-get update && apt-get install -y --no-install-recommends \
    tesseract-ocr \
    tesseract-ocr-spa \
    fonts-liberation \
    wget \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Chromium + todas sus dependencias de sistema (fuentes, librerías gráficas,
# etc.). --with-deps hace "apt-get install" de lo que Chromium necesite,
# evitando mantener esa lista a mano y que se desactualice con el tiempo.
RUN playwright install --with-deps chromium

COPY . .

RUN mkdir -p uploads resultados

# En la nube no hay pantalla: el navegador SIEMPRE debe correr oculto
# (headless). Si en algún momento se corre este mismo Dockerfile con una
# pantalla virtual (Xvfb) y se quiere ver el navegador, sobrescribir esta
# variable en el entorno de ese despliegue puntual.
ENV RUNT_HEADLESS=1

# Railway inyecta el puerto real en $PORT; 5050 es solo el valor por
# defecto para correr la imagen fuera de Railway (docker run -p 5050:5050).
EXPOSE 5050

# --workers 1: el estado del bot (qué se está consultando, resultados ya
# listos) vive en una variable compartida en memoria, no en una base de
# datos. Con más de un worker, cada uno tendría su propia copia separada y
# las peticiones empezarían a "perder" datos entre sí. --timeout alto
# porque una sola consulta (con reintentos de OCR y espera del portal)
# puede tardar más de un minuto; el valor por defecto de gunicorn (30s)
# cortaría la petición a la mitad.
CMD gunicorn --bind 0.0.0.0:${PORT:-5050} --workers 1 --threads 4 --timeout 300 app:app
