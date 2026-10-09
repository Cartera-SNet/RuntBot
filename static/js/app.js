/**
 * RUNT Downloader — Frontend
 * Ajuste SOLO visual: mismos endpoints, mismos payloads y la misma lógica de
 * decisión que el original (upload -> /api/start en bucle -> CAPTCHA manual
 * cuando el OCR no resuelve -> /api/submit_captcha -> /api/generate_pdf).
 * Lo único que cambia aquí es a qué elementos se pinta cada dato.
 */
(() => {
  "use strict";

  const $ = (sel) => document.querySelector(sel);

  // DOM refs (nuevo layout)
  const fileInput = $("#fileInput");
  const uploadZone = $("#uploadZone");
  const btnSelectFile = $("#btnSelectFile");
  const dropHint = $("#dropHint");
  const btnStart = $("#btnStart");

  const tabExcel = $("#tabExcel");
  const tabManual = $("#tabManual");
  const panelExcel = $("#panelExcel");
  const panelManual = $("#panelManual");
  const manualPlaca = $("#manualPlaca");
  const manualCedula = $("#manualCedula");
  const btnStartLabel = $("#btnStartLabel");

  const sectionCaptcha = $("#sectionCaptcha");
  const captchaImg = $("#captchaImg");
  const captchaInput = $("#captchaInput");
  const captchaPlaca = $("#captchaPlaca");
  const btnSubmitCaptcha = $("#btnSubmitCaptcha");
  const btnRefreshCaptcha = $("#btnRefreshCaptcha");
  const btnSkip = $("#btnSkip");

  const tRegistros = $("#tRegistros");
  const tExitosas = $("#tExitosas");
  const tPendientes = $("#tPendientes");
  const tError = $("#tError");
  const progressBar = $("#progressBar");
  const progressPct = $("#progressPct");
  const runStatus = $("#runStatus");

  const previewTitle = $("#previewTitle");
  const tbody = $("#tbody");

  const btnExportPdf = $("#btnExportPdf");
  const btnPdfLabel = $("#btnPdfLabel");

  const headerStatus = $("#headerStatus");
  const toast = $("#toast");
  const consoleEl = $("#console");

  let total = 0;
  let started = false;
  let lastRecords = [];

  // ---- Helpers de presentación ----
  function showToast(msg, type = "") {
    toast.textContent = msg;
    toast.className = "toast" + (type ? " " + type : "");
    toast.hidden = false;
    clearTimeout(showToast._t);
    showToast._t = setTimeout(() => { toast.hidden = true; }, 3500);
  }

  function setHeaderStatus(text) {
    headerStatus.textContent = text;
  }

  function esc(s) {
    return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }

  function logLine(tag, msg, level = "") {
    const t = new Date().toLocaleTimeString("es-CO", { hour12: false });
    const cls = level ? "m-" + level : "";
    const atBottom = consoleEl.scrollHeight - consoleEl.scrollTop - consoleEl.clientHeight < 40;
    consoleEl.insertAdjacentHTML("beforeend",
      `<div class="ln"><span class="t">[${t}]</span><span class="tag ${esc(tag)}">${esc(tag)}</span><span class="${cls}">${esc(msg)}</span></div>`);
    if (atBottom) consoleEl.scrollTop = consoleEl.scrollHeight;
  }

  const PILL_CLASS = {
    Procesando: "s-procesando", CAPTCHA: "s-captcha", Consultando: "s-consultando",
    Completado: "s-completado", "Sin información": "s-sin", Error: "s-error",
  };

  function renderTable(records) {
    lastRecords = records || [];
    if (!lastRecords.length) {
      tbody.innerHTML = '<tr class="empty"><td colspan="6">Aún no has cargado ningún archivo ni agregado registros.</td></tr>';
      return;
    }
    tbody.innerHTML = lastRecords.map((r) => {
      const estado = r.estado || "Pendiente";
      const activo = ["Procesando", "CAPTCHA", "Consultando"].includes(estado);
      const pill = PILL_CLASS[estado] || "";
      const msg = r.error ? `<span class="msg">${esc(r.error)}</span>` : "";
      const poliza = r.poliza
        ? `<span class="pill ${/vigente/i.test(r.poliza) ? "s-completado" : "s-sin"}">${esc(r.poliza)}</span>`
        : '<span class="muted">—</span>';
      const quitar = estado === "Pendiente"
        ? `<button type="button" class="row-remove" data-id="${r.id}" title="Quitar">&times;</button>` : "";
      const verPdf = ["Completado", "Sin información", "Error"].includes(estado)
        ? `<button type="button" class="btn-preview" data-preview-id="${r.id}" data-placa="${esc(r.placa)}" title="Ver PDF">Vista previa</button>`
        : "";
      return `<tr class="${activo ? "active" : ""}"><td>${r.id}</td><td class="mono">${esc(r.placa)}</td>` +
        `<td class="mono">${esc(r.cedula)}</td><td>${poliza}</td>` +
        `<td><span class="pill ${pill}">${esc(estado)}</span>${msg}</td>` +
        `<td class="c-actions">${verPdf}${quitar}</td></tr>`;
    }).join("");
  }

  tbody.addEventListener("click", async (e) => {
    const btn = e.target.closest(".row-remove");
    if (!btn) return;
    const id = Number(btn.dataset.id);
    btn.disabled = true;
    try {
      const res = await fetch("/api/remove_record", {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ id }),
      });
      const data = await res.json();
      if (data.ok) {
        total = data.total;
        btnStart.disabled = total === 0;
        await refreshStatus();
      } else {
        showToast(data.error || "No se pudo quitar", "error");
        btn.disabled = false;
      }
    } catch (err) {
      showToast("Error: " + err.message, "error");
      btn.disabled = false;
    }
  });

  function renderTiles({ total: t, completed, errors, pending }) {
    tRegistros.textContent = t || 0;
    tExitosas.textContent = completed || 0;
    tPendientes.textContent = pending || 0;
    tError.textContent = errors || 0;
    const terminados = (completed || 0) + (errors || 0);
    const pct = t ? Math.round((terminados / t) * 100) : 0;
    progressBar.style.width = pct + "%";
    progressPct.textContent = pct + "%";
  }

  function renderRunStatus({ total: t, completed, errors, pending, current, elapsed }) {
    if (!t) { runStatus.textContent = "Sin proceso en curso."; return; }
    const resumen = `Completadas ${completed || 0} · Pendientes ${pending || 0} · Con error ${errors || 0}`;
    if (current && current.placa) {
      const tiempo = elapsed != null ? ` · transcurrido ${fmtTime(elapsed)}` : "";
      runStatus.textContent = `${current.placa} · ${current.estado || ""}${tiempo}  —  ${resumen}`;
    } else {
      runStatus.textContent = resumen;
    }
  }

  function fmtTime(s) {
    const m = Math.floor(s / 60), r = Math.round(s % 60);
    return m ? `${m} min ${String(r).padStart(2, "0")} s` : `${r} s`;
  }

  // ---- Upload ----
  btnSelectFile.addEventListener("click", (e) => { e.stopPropagation(); fileInput.click(); });
  uploadZone.addEventListener("click", () => fileInput.click());
  uploadZone.addEventListener("dragover", (e) => { e.preventDefault(); uploadZone.classList.add("over"); });
  uploadZone.addEventListener("dragleave", () => uploadZone.classList.remove("over"));
  uploadZone.addEventListener("drop", (e) => {
    e.preventDefault();
    uploadZone.classList.remove("over");
    if (e.dataTransfer.files.length) { fileInput.files = e.dataTransfer.files; handleUpload(e.dataTransfer.files[0]); }
  });
  fileInput.addEventListener("change", () => { if (fileInput.files.length) handleUpload(fileInput.files[0]); });

  async function handleUpload(file) {
    const fd = new FormData();
    fd.append("file", file);
    setHeaderStatus("Cargando…");
    dropHint.textContent = "Leyendo archivo…";
    dropHint.classList.remove("ok");
    try {
      const res = await fetch("/api/upload", { method: "POST", body: fd });
      const data = await res.json();
      if (!data.ok) {
        showToast(data.error || "Error al cargar", "error");
        logLine("EXCEL", data.error || "Error al cargar el archivo.", "error");
        setHeaderStatus("Error");
        dropHint.textContent = "o arrastra tu archivo aquí";
        return;
      }
      total = data.total;
      previewTitle.textContent = "Vista previa del archivo";
      renderTable(data.preview || []);
      renderTiles({ total, completed: 0, errors: 0, pending: total });
      dropHint.textContent = `${file.name} · ${total} registros`;
      dropHint.classList.add("ok");
      btnStart.disabled = total === 0;
      showToast(`${total} registros cargados`, "success");
      logLine("EXCEL", `Archivo cargado correctamente. (${total} registros)`, "success");
      setHeaderStatus("Listo para consultar");
    } catch (err) {
      showToast("Error de red: " + err.message, "error");
      logLine("EXCEL", "Error de red: " + err.message, "error");
      setHeaderStatus("Error");
      dropHint.textContent = "o arrastra tu archivo aquí";
    }
  }

  // ---- Pestañas Excel / Manual ----
  function setModo(modo) {
    const esExcel = modo === "excel";
    tabExcel.classList.toggle("active", esExcel);
    tabManual.classList.toggle("active", !esExcel);
    tabExcel.setAttribute("aria-selected", String(esExcel));
    tabManual.setAttribute("aria-selected", String(!esExcel));
    panelExcel.hidden = !esExcel;
    panelManual.hidden = esExcel;
  }
  tabExcel.addEventListener("click", () => setModo("excel"));
  tabManual.addEventListener("click", () => setModo("manual"));

  // ---- Entrada manual (alternativa al Excel) ----
  panelManual.addEventListener("submit", async (e) => {
    e.preventDefault();
    const placa = manualPlaca.value.trim();
    const cedula = manualCedula.value.trim();
    if (!placa || !cedula) return;

    const btn = $("#btnManualAdd");
    btn.disabled = true;
    try {
      const res = await fetch("/api/add_manual", {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ placa, cedula }),
      });
      const data = await res.json();
      if (!data.ok) {
        showToast(data.error || "No se pudo agregar", "error");
        logLine("MANUAL", data.error || "No se pudo agregar el registro.", "error");
        return;
      }
      total = data.total;
      previewTitle.textContent = "Consultas";
      btnStart.disabled = total === 0;
      await refreshStatus();
      logLine("MANUAL", `Agregado: ${data.record.placa} / ${data.record.cedula}.`, "success");
      manualPlaca.value = "";
      manualCedula.value = "";
      manualPlaca.focus();
    } catch (err) {
      showToast("Error: " + err.message, "error");
    } finally {
      btn.disabled = false;
    }
  });
  manualPlaca.addEventListener("input", () => { manualPlaca.value = manualPlaca.value.toUpperCase(); });

  // ---- Start ----
  btnStart.addEventListener("click", async () => {
    btnStart.disabled = true;
    btnStartLabel.textContent = "Procesando…";
    started = true;
    setHeaderStatus("Procesando…");
    logLine("PROCESO", "Iniciando consultas.");
    await startNext();
  });

  async function startNext() {
    try {
      setHeaderStatus("Procesando…");
      const res = await fetch("/api/start", { method: "POST" });
      const data = await res.json();

      if (!data.ok && !data.done) {
        showToast(data.error || "Error al iniciar", "error");
        logLine("ERROR", data.error || "Error al iniciar.", "error");
        await refreshStatus();
        setTimeout(startNext, 800);
        return;
      }
      // OCR resolvió solo o consulta terminada
      if (data.status === "Completado" || data.status === "Sin información" || data.status === "Error") {
        showToast(data.message || data.status, data.status === "Completado" ? "success" : "");
        logLine("CONSULTA", `${data.placa || ""}: ${(data.status || "").toLowerCase()}.`, data.status === "Completado" ? "success" : "warn");
        await refreshStatus();
        if (data.done) { finish(); } else { setTimeout(startNext, 500); }
        return;
      }
      // Todos ya procesados (sin status de consulta en esta respuesta)
      if (data.done) {
        await refreshStatus();
        finish();
        return;
      }
      // Necesita intervención manual
      if (data.status === "waiting_captcha") {
        await refreshStatus();
        showCaptcha(data);
      }
    } catch (err) {
      showToast("Error: " + err.message, "error");
      logLine("ERROR", err.message, "error");
    }
  }

  function showCaptcha(data) {
    sectionCaptcha.hidden = false;
    captchaPlaca.textContent = data.placa || "";
    captchaImg.src = "data:image/png;base64," + data.captcha_image;
    if (data.ocr_suggestion) {
      captchaInput.value = data.ocr_suggestion;
      showToast("OCR sugirió: " + data.ocr_suggestion + " — verifica y confirma", "");
    } else {
      captchaInput.value = "";
    }
    logLine("CAPTCHA", `Esperando el código para ${data.placa || ""}.`);
    sectionCaptcha.scrollIntoView({ behavior: "smooth", block: "center" });
    captchaInput.focus();
    captchaInput.select();
    setHeaderStatus("CAPTCHA manual");
  }

  // ---- CAPTCHA submit ----
  btnSubmitCaptcha.addEventListener("click", submitCaptcha);
  captchaInput.addEventListener("keydown", (e) => { if (e.key === "Enter") submitCaptcha(); });

  async function submitCaptcha() {
    const code = captchaInput.value.trim();
    if (!code) { showToast("Ingresa el código del CAPTCHA", "error"); captchaInput.focus(); return; }
    btnSubmitCaptcha.disabled = true;
    setHeaderStatus("Consultando…");
    try {
      const res = await fetch("/api/submit_captcha", {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ code }),
      });
      const data = await res.json();
      btnSubmitCaptcha.disabled = false;

      if (!data.ok) {
        sectionCaptcha.hidden = true;
        showToast(data.error || "Error en consulta", "error");
        logLine("ERROR", data.error || "Error en consulta.", "error");
        await refreshStatus();
        return;
      }

      // CAPTCHA incorrecto → nueva imagen, pedir código otra vez
      if (data.status === "waiting_captcha") {
        showToast(data.message || "CAPTCHA incorrecto, intenta de nuevo", "error");
        logLine("CAPTCHA", data.message || "CAPTCHA incorrecto; nueva imagen.", "warn");
        showCaptcha(data);
        await refreshStatus();
        return;
      }

      sectionCaptcha.hidden = true;
      showToast(data.message || "Consulta completada", data.status === "Completado" ? "success" : "");
      logLine("CONSULTA", data.message || `${data.placa}: ${data.status}`, data.status === "Completado" ? "success" : "warn");
      await refreshStatus();
      if (data.done) { finish(); } else { setTimeout(startNext, 500); }
    } catch (err) {
      btnSubmitCaptcha.disabled = false;
      showToast("Error: " + err.message, "error");
      logLine("ERROR", err.message, "error");
    }
  }

  btnRefreshCaptcha.addEventListener("click", async () => {
    btnRefreshCaptcha.disabled = true;
    try {
      const res = await fetch("/api/refresh_captcha", { method: "POST" });
      const data = await res.json();
      if (data.ok) {
        captchaImg.src = "data:image/png;base64," + data.captcha_image;
        captchaInput.value = "";
        captchaInput.focus();
      } else {
        showToast(data.error || "No se pudo refrescar", "error");
      }
    } catch (err) {
      showToast("Error: " + err.message, "error");
    } finally {
      btnRefreshCaptcha.disabled = false;
    }
  });

  btnSkip.addEventListener("click", async () => {
    await fetch("/api/skip", { method: "POST" });
    sectionCaptcha.hidden = true;
    logLine("PROCESO", "Registro omitido por el usuario.", "warn");
    await refreshStatus();
    setTimeout(startNext, 400);
  });

  // ---- Estado (lectura puntual: el flujo avanza con las respuestas de
  // /api/start y /api/submit_captcha, igual que en el original; esta función
  // solo refresca lo que se ve en pantalla después de cada paso) ----
  async function refreshStatus() {
    try {
      const res = await fetch("/api/status");
      const data = await res.json();
      renderTiles(data);
      renderRunStatus(data);
      renderTable(data.records || []);  // siempre actualizar tabla (Completado visible)
      if ((data.completed || 0) + (data.errors || 0) > 0) btnExportPdf.disabled = false;
      return data;
    } catch (_) { /* se reintenta en el próximo paso del flujo */ }
  }

  function finish() {
    btnStartLabel.textContent = "Iniciar consultas";
    sectionCaptcha.hidden = true;
    setHeaderStatus("Generando ZIP…");
    logLine("PROCESO", "Proceso finalizado.", "success");
    btnExportPdf.disabled = false;
    refreshStatus();  // fuerza Completado en contadores y tabla
    // Antes había que pulsar "Exportar ZIP" a mano: si nadie lo hacía,
    // parecía que el bot se había quedado pegado, aunque ya hubiera
    // terminado hace rato. Ahora se genera solo en cuanto termina el
    // último registro.
    // OJO: esto ya se perdió varias veces en ediciones posteriores de este
    // archivo — si se vuelve a tocar finish(), conservar esta llamada.
    exportZip(true);
  }

  // ---- ZIP (siempre: consolidado + un PDF por placa, aunque sea 1 solo) ----
  async function exportZip(automatico) {
    btnExportPdf.disabled = true;
    btnPdfLabel.textContent = "Generando…";
    try {
      const res = await fetch("/api/generate_pdf", { method: "POST" });
      const data = await res.json();
      if (data.ok) {
        // Disparar la descarga directamente (sin mostrar un enlace aparte
        // que haya que volver a pulsar). El propio navegador decide si
        // pregunta dónde guardar o lo manda derecho a Descargas, según su
        // configuración ("Preguntar dónde guardar cada archivo").
        const a = document.createElement("a");
        a.href = data.url;
        a.download = data.filename;
        document.body.appendChild(a);
        a.click();
        a.remove();
        showToast(automatico
          ? `Proceso finalizado. ZIP listo (${data.total || ""} PDFs).`
          : `ZIP generado con ${data.total || ""} PDFs`, "success");
        logLine("PDF", `ZIP generado automáticamente: ${data.filename}`, "success");
        setHeaderStatus("Listo: ZIP generado");
        await refreshStatus();
      } else {
        showToast(data.error || "Error al generar el ZIP", "error");
        logLine("ERROR", data.error || "Error al generar el ZIP.", "error");
        setHeaderStatus("Finalizado (sin ZIP)");
      }
    } catch (err) {
      showToast("Error: " + err.message, "error");
      setHeaderStatus("Finalizado (sin ZIP)");
    }
    btnExportPdf.disabled = false;
    btnPdfLabel.textContent = "Exportar ZIP";
  }
  btnExportPdf.addEventListener("click", () => exportZip(false));

  // ---- Limpiar todo ----
  const btnLimpiar = $("#btnLimpiar");
  if (btnLimpiar) {
    btnLimpiar.addEventListener("click", async () => {
      if (!confirm("¿Limpiar todo? Se borrarán registros, PDFs y se cerrará el navegador del bot.")) return;
      try {
        const res = await fetch("/api/reset", { method: "POST" });
        const data = await res.json();
        if (data.ok) {
          total = 0;
          started = false;
          tbody.innerHTML = "";
          tRegistros.textContent = "0";
          tExitosas.textContent = "0";
          tPendientes.textContent = "0";
          tError.textContent = "0";
          progressBar.style.width = "0%";
          progressPct.textContent = "0%";
          runStatus.textContent = "Sin proceso en curso.";
          btnExportPdf.disabled = true;
          btnStart.disabled = true;
          sectionCaptcha.hidden = true;
          setHeaderStatus("Listo");
          showToast("Todo limpiado. Listo para empezar de cero.", "success");
          logLine("SISTEMA", "Limpieza completa. Estado en cero.", "success");
        } else {
          showToast("No se pudo limpiar", "error");
        }
      } catch (err) {
        showToast("Error: " + err.message, "error");
      }
    });
  }

  logLine("SISTEMA", "Aplicación iniciada.");


  // ── Modal PDF (elementos se buscan al abrir, por si el DOM cambia) ──
  function openPdfModal(id, placa) {
    const pdfModal = document.getElementById("pdfModal");
    const pdfModalFrame = document.getElementById("pdfModalFrame");
    const pdfModalTitle = document.getElementById("pdfModalTitle");
    const pdfModalDownload = document.getElementById("pdfModalDownload");
    const pdfModalLoading = document.getElementById("pdfModalLoading");
    const url = "/api/preview_pdf/" + id + "?t=" + Date.now();

    if (!pdfModal) {
      // Si no hay modal, abrir directo en pestaña
      window.open(url, "_blank");
      return;
    }

    if (pdfModalTitle) pdfModalTitle.textContent = "Vista previa — " + (placa || ("#" + id));
    if (pdfModalLoading) {
      pdfModalLoading.style.display = "block";
      pdfModalLoading.textContent = "Generando PDF…";
    }
    if (pdfModalFrame) {
      pdfModalFrame.style.display = "none";
      pdfModalFrame.src = "about:blank";
    }

    pdfModal.classList.remove("hidden");
    pdfModal.style.display = "flex";
    document.body.style.overflow = "hidden";

    fetch(url, { credentials: "same-origin" })
      .then(async (res) => {
        const ct = (res.headers.get("content-type") || "").toLowerCase();
        if (!res.ok || ct.indexOf("pdf") === -1) {
          let msg = "No se pudo generar la vista previa";
          try {
            const j = await res.json();
            if (j && j.error) msg = j.error;
          } catch (_) {}
          if (pdfModalLoading) pdfModalLoading.textContent = msg;
          if (typeof showToast === "function") showToast(msg, "error");
          // Abrir en pestaña como respaldo
          window.open(url, "_blank");
          return;
        }
        const blob = await res.blob();
        const blobUrl = URL.createObjectURL(new Blob([blob], { type: "application/pdf" }));
        if (pdfModalFrame) {
          pdfModalFrame.src = blobUrl;
          pdfModalFrame.style.display = "block";
        }
        if (pdfModalLoading) pdfModalLoading.style.display = "none";
        if (pdfModalDownload) {
          pdfModalDownload.href = blobUrl;
          pdfModalDownload.setAttribute("download", "RUNT_" + (placa || id) + ".pdf");
        }
      })
      .catch((err) => {
        const msg = "Error al cargar PDF: " + err.message;
        if (pdfModalLoading) pdfModalLoading.textContent = msg;
        if (typeof showToast === "function") showToast(msg, "error");
        window.open(url, "_blank");
      });
  }

  function closePdfModal() {
    const pdfModal = document.getElementById("pdfModal");
    const pdfModalFrame = document.getElementById("pdfModalFrame");
    if (!pdfModal) return;
    pdfModal.classList.add("hidden");
    pdfModal.style.display = "none";
    try {
      if (pdfModalFrame && pdfModalFrame.src && pdfModalFrame.src.indexOf("blob:") === 0) {
        URL.revokeObjectURL(pdfModalFrame.src);
      }
    } catch (_) {}
    if (pdfModalFrame) {
      pdfModalFrame.src = "about:blank";
      pdfModalFrame.style.display = "none";
    }
    document.body.style.overflow = "";
  }

  document.addEventListener("click", (e) => {
    if (e.target.closest("#pdfModalClose") || e.target.closest("#pdfModalBackdrop")) {
      closePdfModal();
      return;
    }
    const btn = e.target.closest("[data-preview-id]");
    if (!btn) return;
    e.preventDefault();
    e.stopPropagation();
    const id = Number(btn.getAttribute("data-preview-id"));
    const placa = btn.getAttribute("data-placa") || "";
    if (!id) {
      if (typeof showToast === "function") showToast("Registro inválido", "error");
      return;
    }
    openPdfModal(id, placa);
  });

  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") closePdfModal();
  });

})();
