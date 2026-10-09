# RUNT Downloader — Consulta Vehicular Automatizada

Bot web local en Python que automatiza consultas de vehículos en **runtporplacas.com.co** (Consulta RUNT por Placa y Cédula).

## Características

- Carga de Excel con columnas **Placa** y **Cedula**
- Automatización real con **Playwright** (navegador Chromium)
- Captura y visualización del CAPTCHA en la interfaz
- Extracción de datos principales, información general y consultas adicionales
- Procesamiento secuencial de múltiples registros
- Generación de PDF profesional con los resultados
- Interfaz web moderna (Flask + HTML/CSS/JS)

## Requisitos

- Windows 10/11 (o Linux/macOS con Python)
- Python 3.10 o superior
- Conexión a Internet

## Instalación y uso (Windows)

1. Descomprime `RUNT_BOT.zip`
2. Haz **doble clic** en `iniciar.bat`
3. Espera a que se cree el entorno virtual, se instalen las dependencias y se prepare Playwright
4. Se abrirá automáticamente el navegador en `http://127.0.0.1:5050`

## Uso

1. Prepara un Excel con las columnas:

   | Placa  | Cedula     |
   |--------|------------|
   | EVJ071 | 1048206552 |
   | ABC123 | 1234567890 |

2. Arrastra o selecciona el archivo en la interfaz
3. Revisa la vista previa y pulsa **Iniciar consultas**
4. Cuando aparezca el CAPTCHA, escribe el código y pulsa **Consultar**
5. El bot continúa automáticamente con el siguiente registro
6. Al finalizar, genera el PDF con todos los resultados

## Estructura del proyecto

```
RUNT_BOT/
├── app.py                 # Servidor Flask
├── requirements.txt
├── iniciar.bat            # Arranque en un clic (Windows)
├── README.md
├── templates/
│   └── index.html
├── static/
│   ├── css/style.css
│   └── js/app.js
├── services/
│   ├── runt_service.py    # Automatización Playwright + CAPTCHA
│   ├── excel_service.py   # Lectura y validación de Excel
│   └── pdf_service.py     # Generación de PDF
├── uploads/               # Archivos Excel subidos
└── resultados/            # PDFs generados
```


## CAPTCHA y OCR (Tesseract)

El bot intenta resolver el CAPTCHA automáticamente con **Tesseract OCR**:

1. Playwright captura **solo** el elemento `<img id="rqp_captcha_img">` (no captura de pantalla completa).
2. Preprocesa la imagen (escala ×3, escala de grises, contraste, umbralización).
3. Ejecuta varios intentos de OCR (raw / processed / aggressive).
4. Limpia el texto (espacios, caracteres no alfanuméricos).
5. Si la confianza es suficiente → rellena y envía automáticamente.
6. Si el sitio responde “CAPTCHA incorrecto” → refresca imagen y reintenta (máx. 3 veces).
7. Si OCR no consigue una lectura confiable → muestra la imagen en la interfaz para entrada **manual** (con sugerencia de OCR si la hay).

### Instalar Tesseract en Windows

1. Descarga el instalador: https://github.com/UB-Mannheim/tesseract/wiki
2. Instala (recomendado: `C:\Program Files\Tesseract-OCR`)
3. Asegúrate de que `tesseract.exe` esté en el PATH
4. Reinicia `iniciar.bat`

Sin Tesseract el bot funciona igual; simplemente todos los CAPTCHA se resuelven a mano.

> **Nota:** El CAPTCHA es un mecanismo anti-automatización. El OCR es un experimento técnico con fallback manual. No se garantiza un 100 % de aciertos.

## Notas importantes

- El CAPTCHA debe resolverse **manualmente** (primera implementación). El componente está aislado para poder integrar un solucionador automático más adelante sin tocar el flujo principal.
- El bot interactúa realmente con el sitio; no es una simulación.
- Cada registro conserva su resultado de forma independiente.
- Estados posibles: Pendiente · Procesando · CAPTCHA · Consultando · Completado · Sin información · Error

## Licencia y responsabilidad

Herramienta de uso personal/local. No es un producto oficial del RUNT ni de la Concesión RUNT. El usuario es responsable del uso conforme a la normativa aplicable.

## Desplegar en Railway (Docker)

El proyecto incluye `Dockerfile` y `railway.json`, listos para subir tal cual.

### Pasos

1. Sube esta carpeta a un repositorio de Git (GitHub, por ejemplo) y conéctalo
   en Railway como un nuevo proyecto — Railway detecta el `Dockerfile` solo.
2. En **Variables** del servicio en Railway, define (todas opcionales salvo
   que quieras protegerlo):
   - `ACCESS_USER` y `ACCESS_PASSWORD` — si defines `ACCESS_PASSWORD`, la
     página pide usuario/contraseña antes de dejar entrar a nadie. **Muy
     recomendado**: este bot consulta cédulas y datos personales reales, y
     Railway le da una URL pública por defecto. Sin esto, cualquiera con el
     link podría usarlo.
   - `SECRET_KEY` — una cadena cualquiera, para las sesiones de Flask.
3. Dale clic a "Deploy". La primera build tarda varios minutos (instala
   Chromium y Tesseract); los siguientes despliegues son más rápidos por el
   caché de Docker.

### Qué configuración ya viene lista

- **Puerto**: Railway inyecta `$PORT`; la app ya lo usa automáticamente
  (`app.py` y el `Dockerfile` lo leen solos, no hay que tocar nada).
- **Navegador oculto**: `RUNT_HEADLESS=1` ya viene fijo en el `Dockerfile`
  (en la nube no hay pantalla, así que *tiene* que correr oculto).
- **Se duerme solo sin uso**: `railway.json` trae `"sleepApplication": true`
  — Railway apaga el contenedor tras 10 minutos sin tráfico entrante y lo
  vuelve a prender solo con la siguiente visita (con unos segundos de
  arranque en frío). Para desactivarlo, cambia ese valor a `false` y vuelve
  a desplegar.
- **Chromium se cierra solo** al terminar de procesar todos los registros
  del lote (no se queda una pestaña fantasma abierta consumiendo memoria
  entre lote y lote).

### Limitaciones a tener en cuenta

- **Un usuario/lote a la vez.** El estado del bot (qué se está consultando,
  resultados listos) vive en memoria del proceso, no en una base de datos
  — por diseño, para que sea simple. Si dos personas lo usan al mismo
  tiempo desde el mismo despliegue, van a pisarse los datos entre sí (una
  vería los resultados de la otra). Si varias personas necesitan usarlo a
  la vez, hace falta rediseñar esa parte (sesiones por usuario o una base
  de datos) — avisa si quieres que lo haga.
- **Los archivos no persisten entre despliegues.** Los PDF/ZIP generados
  viven en el disco del contenedor; si Railway reinicia el servicio
  (redeploy, o lo duerme y lo prende de nuevo en un contenedor distinto),
  se pierden. Descarga el ZIP apenas se genere, no lo dejes "para después".
- **Arranque en frío.** Si `sleepApplication` está activo y nadie lo usó en
  10+ minutos, la primera visita tarda unos segundos extra en responder
  mientras el contenedor despierta.

### Probarlo en Docker local (sin Railway)

```bat
docker build -t runt-bot .
docker run -p 5050:5050 -e RUNT_HEADLESS=1 runt-bot
```

Abre `http://localhost:5050`.
