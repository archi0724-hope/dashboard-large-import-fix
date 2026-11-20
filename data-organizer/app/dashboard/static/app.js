/* Data Organizer dashboard - vanilla JS, no build step, no external requests.
   Every dynamic value is escaped by the html`` helper: file contents can contain anything. */
"use strict";

// ---------------------------------------------------------------- helpers
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const raw = s => ({ __raw: String(s ?? "") });
const fmt = v => v == null || v === false ? "" : (typeof v === "object" && "__raw" in v) ? v.__raw : Array.isArray(v) ? v.map(fmt).join("") : esc(v);
const html = (s, ...v) => raw(s.reduce((o, x, i) => o + x + (i < v.length ? fmt(v[i]) : ""), ""));
const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
const num = n => n == null ? "-" : Number(n).toLocaleString();
const bytes = n => n == null ? "-" : n < 1024 ? n + " B" : n < 1048576 ? (n / 1024).toFixed(1) + " KB" : (n / 1048576).toFixed(1) + " MB";
const pct = (a, b) => b ? Math.round(100 * a / b) + "%" : "-";
const TOKEN_KEY = "organizer_token";

async function api(path, opts = {}) {
  const headers = { "Content-Type": "application/json", ...(opts.headers || {}) };
  const t = sessionStorage.getItem(TOKEN_KEY);
  if (t) headers["X-Token"] = t;
  if (opts.body instanceof FormData) delete headers["Content-Type"];
  const res = await fetch(path, { ...opts, headers, body: opts.body && !(opts.body instanceof FormData) ? JSON.stringify(opts.body) : opts.body });
  if (res.status === 401) {
    const tok = prompt("This dashboard needs its access token (DASHBOARD_TOKEN):");
    if (tok) { sessionStorage.setItem(TOKEN_KEY, tok); return api(path, opts); }
  }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail ? (typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail)) : res.statusText);
  return data;
}
const post = (p, body = {}) => api(p, { method: "POST", body });

function uploadWithProgress(formData, onProgress) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", "/api/upload");
    const token = sessionStorage.getItem(TOKEN_KEY);
    if (token) xhr.setRequestHeader("X-Token", token);
    xhr.upload.onprogress = event => { if (event.lengthComputable) onProgress(event.loaded, event.total); };
    xhr.onerror = () => reject(new Error("Upload connection failed"));
    xhr.onload = () => {
      let data = {};
      try { data = JSON.parse(xhr.responseText || "{}"); } catch { /* handled below */ }
      if (xhr.status >= 200 && xhr.status < 300) resolve(data);
      else reject(new Error(data.detail || xhr.statusText || "Upload failed"));
    };
    xhr.send(formData);
  });
}

function toast(msg, bad = false) {
  const d = document.createElement("div");
  d.className = "toast" + (bad ? " bad" : "");
  d.textContent = msg;
  $("#toasts").append(d);
  setTimeout(() => d.remove(), bad ? 7000 : 3500);
}
const guard = fn => async (...a) => { try { return await fn(...a); } catch (e) { toast(e.message || String(e), true); } };

function confirmBox(title, body, okLabel = "Confirm", danger = false) {
  return new Promise(res => {
    const m = $("#modal");
    m.hidden = false;
    m.innerHTML = html`<div role="dialog" aria-modal="true" aria-label="${title}"><h2>${title}</h2><p class="muted">${body}</p>
      <div class="row" style="justify-content:flex-end;margin-top:14px"><button data-r="0">Cancel</button><button data-r="1" class="${danger ? "danger" : "primary"}">${okLabel}</button></div></div>`.__raw;
    m.onclick = e => { const b = e.target.closest("[data-r]"); if (b || e.target === m) { m.hidden = true; res(b?.dataset.r === "1"); } };
    $("[data-r='1']", m).focus();
  });
}

const STATUS_COL = { auto_matched: "#1b7f4b", verified_mapping: "#0b7a75", user_accepted: "#3aa675", user_created: "#5ec6be", new_entity: "#5b6b86", needs_review: "#d8963a", uncertain_kept_separate: "#c2570c", unresolvable: "#c9c7bb" };
const BAND = { "Very High": "t-ok", "High": "t-acc", "Possible Match": "t-warn", "Manual Review": "t-warn", "Probably Different": "t-bad", "New entity": "t-new" };
const STATUS = { auto_matched: ["Auto-matched", "t-ok"], new_entity: ["New entity", "t-new"], needs_review: ["Needs review", "t-warn"], verified_mapping: ["Learned mapping", "t-acc"],
  user_accepted: ["Confirmed", "t-ok"], user_created: ["Created by user", "t-acc"], uncertain_kept_separate: ["Kept separate", "t-warn"], unresolvable: ["No entity name", "t-bad"] };
const bandTag = b => b ? html`<span class="tag ${BAND[b] || "t-new"}">${b}</span>` : "";
const statusTag = s => { const [l, c] = STATUS[s] || [s, "t-new"]; return html`<span class="tag ${c}">${l}</span>`; };
const fileTag = s => html`<span class="tag ${{ processed: "t-ok", deferred_ocr: "t-warn", failed: "t-bad", unsupported: "t-warn", duplicate_file: "t-info", discovered: "t-new", changed: "t-warn", processing: "t-acc", sampled: "t-info" }[s] || "t-new"}">${s}</span>`;
const sevTag = s => html`<span class="tag ${{ error: "t-bad", warning: "t-warn", info: "t-info" }[s] || "t-new"}">${s}</span>`;
const score = s => s == null ? html`<span class="faint">-</span>` : html`<strong>${Number(s).toFixed(0)}%</strong>`;
const loc = r => r.source_sheet || (r.source_page ? "page " + r.source_page : "") || "";

const ICON = {
  overview: "M3 13h7V3H3zM14 21h7V11h-7zM3 21h7v-6H3zM14 3v5h7V3z", source: "M3 7a2 2 0 012-2h4l2 2h8a2 2 0 012 2v9a2 2 0 01-2 2H5a2 2 0 01-2-2z",
  preview: "M1 12s4-7 11-7 11 7 11 7-4 7-11 7S1 12 1 12zM12 15a3 3 0 100-6 3 3 0 000 6z", review: "M9 12l2 2 4-4M21 12a9 9 0 11-18 0 9 9 0 0118 0z",
  entities: "M4 6h16M4 12h16M4 18h10", quality: "M12 9v4M12 17h.01M10.3 3.9L1.8 18a2 2 0 001.7 3h17a2 2 0 001.7-3L13.7 3.9a2 2 0 00-3.4 0z",
  catalogue: "M4 4h16v16H4zM8 8h8M8 12h8M8 16h5",
  export: "M12 3v12m0 0l-4-4m4 4l4-4M4 21h16", settings: "M12 15a3 3 0 100-6 3 3 0 000 6zM19.4 15a1.7 1.7 0 00.3 1.8l.1.1a2 2 0 11-2.8 2.8l-.1-.1a1.7 1.7 0 00-1.8-.3 1.7 1.7 0 00-1 1.5V21a2 2 0 11-4 0v-.1a1.7 1.7 0 00-1.1-1.5 1.7 1.7 0 00-1.8.3l-.1.1a2 2 0 11-2.8-2.8l.1-.1a1.7 1.7 0 00.3-1.8 1.7 1.7 0 00-1.5-1H3a2 2 0 110-4h.1a1.7 1.7 0 001.5-1.1 1.7 1.7 0 00-.3-1.8l-.1-.1a2 2 0 112.8-2.8l.1.1a1.7 1.7 0 001.8.3H9a1.7 1.7 0 001-1.5V3a2 2 0 114 0v.1a1.7 1.7 0 001 1.5 1.7 1.7 0 001.8-.3l.1-.1a2 2 0 112.8 2.8l-.1.1a1.7 1.7 0 00-.3 1.8V9a1.7 1.7 0 001.5 1H21a2 2 0 110 4h-.1a1.7 1.7 0 00-1.5 1z",
  logs: "M4 6h16M4 10h16M4 14h10M4 18h7",
};
const icon = k => html`<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="${ICON[k]}"/></svg>`;

// ---------------------------------------------------------------- state / routing
const S = { status: null, cfg: null, charts: [], poll: null, page: "overview", workspace: localStorage.getItem("workspace") || "organizer", review: { items: [], total: 0, cur: null, q: "", detail: null } };
const PAGES = [
  ["drivelibrary", "Google Drive library"],
  ["purchaseinvoices", "Purchase invoices"],
  ["vendorimports", "Upload vendor documents"],
  ["overview", "Dashboard"], ["source", "Import files"], ["preview", "Check & match"], ["review", "Review matches"], ["catalogue", "Catalogue approval"],
  ["entities", "Companies"], ["quality", "Data quality"], ["export", "Export data"], ["settings", "Workspace settings"], ["logs", "Activity log"],
];
const VENDOR_PAGES = [["vendor", "Vendor dashboard"]];
const workspacePages = () => S.workspace === "vendor" ? VENDOR_PAGES : PAGES;

function renderNav() {
  const pend = S.status?.totals?.pending_reviews || 0;
  const pages = workspacePages();
  $("#nav").innerHTML = pages.map(([k, l]) => html`<a href="#/${k}" class="${S.page === k ? "on" : ""}" ${S.page === k ? raw('aria-current="page"') : ""}>${icon(["vendor", "chatbot"].includes(k) ? "overview" : k)}<span>${l}</span><span class="sp"></span>${k === "review" && pend ? html`<span class="badge-n">${pend}</span>` : ""}</a>`).map(x => x.__raw).join("");
  const p = S.status?.progress;
  const pill = $("#runpill");
  if (p?.running) { pill.className = "pill run"; pill.textContent = `Running ${p.percent}%`; }
  else if (p?.error) { pill.className = "pill err"; pill.textContent = "Stopped"; }
  else { pill.className = "pill idle"; pill.textContent = "Idle"; }
  const c = S.cfg;
  $("#srclabel").textContent = c ? (c.source_kind === "drive" ? "Source: Google Drive" : "Source: " + (c.local_input_dir ? c.local_input_dir.split("/").slice(-2).join("/") : "not set")) : "";
  $$('[data-workspace]').forEach(button => button.classList.toggle("on", button.dataset.workspace === S.workspace));
  document.body.classList.toggle("vendor-full", S.workspace !== "organizer");
}

async function refreshStatus() {
  S.status = await api("/api/status");
  S.cfg = S.status.config;
  renderNav();
  return S.status;
}

function killCharts() { S.charts.forEach(c => c.destroy()); S.charts = []; }

async function route() {
  const k = (location.hash.replace(/^#\//, "") || "overview").split("?")[0];
  S.workspace = ["vendor", "chatbot"].includes(k) ? "vendor" : "organizer";
  localStorage.setItem("workspace", S.workspace);
  const pages = workspacePages();
  S.page = pages.some(p => p[0] === k) ? k : pages[0][0];
  killCharts();
  $("#title").textContent = (S.workspace === "vendor" ? "Vendor workspace · " : "Organizer workspace · ") + pages.find(p => p[0] === S.page)[1];
  $("#topactions").innerHTML = "";
  if (S.workspace !== "organizer") {
    renderNav();
    const src = k === "chatbot" ? "/vendor/?assistant=1" : "/vendor/";
    const title = "Vendor dashboard";
    if ($("#view .vendor-frame")?.getAttribute("src") !== src) {
      $("#view").innerHTML = `<iframe class="vendor-frame" title="${title}" src="${src}"></iframe>`;
    }
    $("#view").focus({ preventScroll: true });
    return;
  }
  await guard(refreshStatus)();
  $("#view").innerHTML = `<div class="empty">Loading...</div>`;
  await guard(PAGE[S.page])();
  $("#view").focus({ preventScroll: true });
}
window.addEventListener("hashchange", route);
$$('[data-workspace]').forEach(button => button.addEventListener("click", () => {
  S.workspace = button.dataset.workspace;
  localStorage.setItem("workspace", S.workspace);
  const target = S.workspace === "vendor" ? "#/vendor" : "#/overview";
  if (location.hash === target) route();
  else location.hash = target;
}));

// ---------------------------------------------------------------- overview
const STEPS = [["source", "Choose source"], ["source", "Scan files"], ["preview", "Preview"], ["overview", "Run"], ["review", "Review"], ["export", "Export"]];

const PAGE = {};
PAGE.drivelibrary = async () => {
  $('#view').innerHTML = '<p><a class="btn primary" href="/drive-library" target="_blank" rel="noopener">Open shared Drive library</a></p><iframe title="Shared Drive library" src="/drive-library" style="width:100%;height:calc(100vh - 220px);border:0"></iframe>';
};
PAGE.purchaseinvoices = async () => {
  $('#view').innerHTML = '<p><a class="btn primary" href="/purchase-invoices" target="_blank" rel="noopener">Open purchase invoices</a></p><iframe title="Purchase invoices" src="/purchase-invoices" style="width:100%;height:calc(100vh - 220px);border:0"></iframe>';
};
PAGE.vendorimports = async () => {
  $('#view').innerHTML = '<p><a class="btn primary" href="/vendor-upload" target="_blank" rel="noopener">Open shared vendor import</a></p><iframe title="Shared vendor import" src="/vendor-upload" style="width:100%;height:calc(100vh - 220px);border:0"></iframe>';
};
PAGE.overview = async () => {
  const st = S.status, t = st.totals, p = st.progress;
  const sourceSet = st.config.source_kind === "drive" ? !!st.config.drive_folder : !!st.config.local_input_dir;
  const ocrReady = st.config.ocr_enabled && st.tools.tesseract;
  const kpi = (value, label) => html`<div class="card kpi"><div class="v">${value}</div><div class="l">${label}</div></div>`;
  $("#view").innerHTML = html`
    ${!sourceSet ? html`<div class="card row sb"><div><h2>Choose your source files</h2><p class="muted">Reconnect files already uploaded, or choose a folder under Import files.</p></div><button class="primary" id="useSavedUploads">Use uploaded files</button></div>` : ""}
    <div class="card organizer-intro"><div><h2>Your files, organized by company</h2><p class="muted">Read company details from Excel, PDF, JPG, PNG, CSV, Word and text. Original files keep their format and contents.</p></div><a class="btn primary" href="#/source">Import files</a></div>
    <div class="grid g4" style="margin-bottom:16px">
      ${kpi(num(t.files_processed) + " / " + num(t.files), "Files read")}${kpi(num(t.entities), "Companies")}${kpi(num(t.records), "Company records")}${kpi(num(t.pending_reviews), "Matches to check")}
    </div>
    <div class="grid g3 organizer-workflow">
      <div class="card"><span class="workflow-number">1</span><h2>Import</h2><p class="muted">Choose a folder or upload files. ZIP folders are supported too.</p><a class="btn" href="#/source">Choose files</a></div>
      <div class="card"><span class="workflow-number">2</span><h2>Read files</h2><p class="muted">Find company names and details across all supported file types.</p><button class="primary" id="btnRun" ${p.running ? "disabled" : ""}>${p.running ? "Reading files…" : t.records ? "Read new files / resume" : "Read files"}</button></div>
      <div class="card"><span class="workflow-number">3</span><h2>Company folders</h2><p class="muted">Copy original documents into each company's folder and sync the originals and catalogue rows into Vendor.</p><button id="btnOrganizeDashboard" ${p.running || !t.files ? "disabled" : ""}>Create folders &amp; sync to Vendor</button></div>
    </div>
    <div class="card"><div class="h"><h2>${p.running ? "Reading progress" : "File reading"}</h2>${p.running ? html`<button id="btnStop">Pause</button>` : html`<a href="#/source">View all files</a>`}</div><div id="stages">${raw(stagesHtml(p))}</div>
      <p class="small ${ocrReady ? "muted" : "ocr-warning"}">${ocrReady ? "Image reading is ready: JPG, PNG and scanned PDFs use OCR." : "JPG, PNG and scanned PDFs need OCR. Enable OCR in Workspace settings and install Tesseract to read their contents."}</p>
      ${t.files_failed ? html`<p class="small ocr-warning">${num(t.files_failed)} file(s) could not be read. <a href="#/source">Check files and retry</a>.</p>` : ""}
    </div>
    <div class="card"><div class="h"><h2>Products &amp; prices in Excel</h2><button class="primary" id="btnProductExcel" ${p.running ? "disabled" : ""}>Extract products &amp; download Excel</button></div>
      <p class="muted">One product per row, sorted by company, product name and SKU. Includes Products &amp; Prices, Company Contacts and Needs Review. The synced catalogue is also exportable from Vendor → Catalogue &amp; prices. Originals stay unchanged.</p>
      <div id="productExcelStatus" role="status" aria-live="polite"></div></div>
    <div class="card"><div class="h"><h2>Companies</h2><div class="row"><a class="btn" href="#/entities">View all companies</a><button id="btnCompanyZip" ${p.running || !t.files ? "disabled" : ""}>Download company folders</button></div></div>
      <label class="f" for="companyQuickSearch">Search companies<input id="companyQuickSearch" placeholder="Company name or phone" type="search"></label>
      <div id="overviewCompanies" style="margin-top:14px"></div>
      <p class="small muted">A document containing several companies is copied unchanged to each matching folder. Files without an identified company are kept separately for review.</p>
      <div id="companyFolderResult" role="status" aria-live="polite"></div>
    </div>
    ${t.pending_reviews ? html`<div class="card row sb"><div><h2>Check uncertain matches</h2><p class="muted">Confirm which company these records belong to.</p></div><a class="btn" href="#/review">Review ${num(t.pending_reviews)} matches</a></div>` : ""}`.__raw;
  $("#btnRun").addEventListener("click", guard(async event => {
    if (!sourceSet) { location.hash = "#/source"; return; }
    const button = event.currentTarget;
    button.disabled = true;
    button.textContent = "Starting…";
    try { await post("/api/scan"); await post("/api/run"); toast("Reading files started"); await route(); }
    finally { if (button.isConnected) { button.disabled = false; button.textContent = "Read files"; } }
  }));
  $("#useSavedUploads")?.addEventListener("click", guard(async event => {
    const button = event.currentTarget; button.disabled = true;
    try { await post("/api/source/uploads"); toast("Your uploaded files are selected"); await route(); }
    finally { if (button.isConnected) button.disabled = false; }
  }));
  $("#btnStop")?.addEventListener("click", guard(async () => { await post("/api/stop"); toast("Pausing after the current batch. Progress is saved."); }));
  $("#btnOrganizeDashboard").addEventListener("click", guard(async event => {
    const button = event.currentTarget; button.disabled = true; button.textContent = "Creating folders…";
    try {
      const result = await post("/api/organize-files");
      $("#companyFolderResult").innerHTML = html`<p><strong>${num(result.company_folders)} company folders</strong> · ${num(result.files_copied)} original files copied.</p><p class="small mono">${result.folder}</p>${result.skipped ? html`<p class="ocr-warning">${num(result.skipped)} file(s) were unavailable. Check the source folder.</p>` : ""}<p class="small" id="vendorSyncStatus">Syncing originals and catalogue rows into Vendor…</p>`.__raw;
      watchVendorSync(result.vendor_sync, $("#vendorSyncStatus"));
      toast("Company folders created; Vendor sync started");
    } finally { button.disabled = false; button.textContent = "Create folders & sync to Vendor"; }
  }));
  $("#btnCompanyZip").addEventListener("click", guard(async event => {
    const button = event.currentTarget; button.disabled = true; button.textContent = "Preparing download…";
    try {
      const result = await post("/api/export/structured-zip");
      const url = new URL("/api/exports/download", location.origin); url.searchParams.set("file", result.file);
      const token = sessionStorage.getItem(TOKEN_KEY); if (token) url.searchParams.set("token", token);
      const link = document.createElement("a"); link.href = url; link.download = result.file; link.click();
      if (result.vendor_sync?.running) watchVendorSync(result.vendor_sync);
      toast(`${num(result.files)} original files packaged`);
    } finally { button.disabled = false; button.textContent = "Download company folders"; }
  }));
  let timer;
  $("#btnProductExcel").addEventListener("click", guard(async event => {
    if (!sourceSet) { location.hash = "#/source"; toast("Choose your source files first"); return; }
    await showProductExport(await post("/api/export/products"), true);
  }));
  $("#companyQuickSearch").addEventListener("input", () => { clearTimeout(timer); timer = setTimeout(guard(loadOverviewCompanies), 250); });
  await loadOverviewCompanies();
  const existingExport = await api("/api/export/products/status");
  if (existingExport.running || existingExport.result) guard(() => showProductExport(existingExport))();
  if (p.running) startPolling();
};
async function showProductExport(job, downloadWhenReady = false) {
  const button = $("#btnProductExcel"), statusBox = $("#productExcelStatus");
  if (!button || !statusBox) return;
  button.disabled = true;
  try {
    while (job.running && statusBox.isConnected) {
      statusBox.textContent = `Reading ${num(job.done)} / ${num(job.total)} source files: ${job.message}`;
      await new Promise(resolve => setTimeout(resolve, 1500));
      job = await api("/api/export/products/status");
    }

    if (!statusBox.isConnected) return;
    if (job.error) throw new Error(job.error);
    if (!job.result) return;
    const result = job.result;
    const url = new URL("/api/exports/download", location.origin); url.searchParams.set("file", result.file);
    const token = sessionStorage.getItem(TOKEN_KEY); if (token) url.searchParams.set("token", token);
    statusBox.innerHTML = html`<p>${num(result.products)} product rows, ${num(result.contacts)} contacts, ${num(result.review_items)} review items. All ${num(result.files)} source files accounted for.</p><a class="btn primary" href="${url.href}" download>Download company-wise Excel</a>`.__raw;
    if (downloadWhenReady) $("a", statusBox).click();
  } finally { if (button.isConnected) button.disabled = false; }
}
async function watchVendorSync(job, statusTarget = null) {
  if (!job) return;
  const wait = milliseconds => new Promise(resolve => setTimeout(resolve, milliseconds));
  while (job.running) {
    if (statusTarget?.isConnected) statusTarget.textContent = `Syncing ${num(job.done)} / ${num(job.total)}: ${job.message}`;
    await wait(1500);
    job = await api("/api/vendor/sync/status");
  }
  if (job.error) {
    if (statusTarget?.isConnected) statusTarget.textContent = `Vendor sync failed: ${job.error}`;
    else toast(`Vendor sync failed: ${job.error}`);
    return;
  }
  const result = job.result || {};
  const summary = `${num(result.documents_added)} original documents added, ${num(result.duplicate_documents)} already synced, ${num(result.products_added)} catalogue rows added, ${num(result.unassigned_documents_added)} unassigned.`;
  if (statusTarget?.isConnected) {
    statusTarget.innerHTML = html`<span>${summary}</span> <a href="/vendor/">Open Vendor dashboard</a>${result.skipped_count ? html`<p class="ocr-warning">${num(result.skipped_count)} file(s) could not be synced. First issue: ${result.skipped[0].file} — ${result.skipped[0].reason}</p>` : ""}`.__raw;
  } else {
    toast(`Vendor sync complete: ${summary}`);
  }
}
let overviewSearchRequest = 0;
async function loadOverviewCompanies() {
  const input = $("#companyQuickSearch"), box = $("#overviewCompanies");
  if (!input || !box) return;
  const request = ++overviewSearchRequest;
  const result = await api(`/api/entities?search=${encodeURIComponent(input.value)}&limit=20`);
  if (request !== overviewSearchRequest || !box.isConnected) return;
  box.innerHTML = result.items.length ? html`<p class="small muted">Showing ${num(result.items.length)} of ${num(result.total)} companies</p><div class="tw"><table><thead><tr><th>Company</th><th>City</th><th>Phone</th><th class="num">Records</th><th class="num">Source files</th></tr></thead><tbody>${result.items.map(e => html`<tr><td><button class="company-link" data-company="${e.master_entity_id}">${e.standard_name}</button></td><td>${e.city || "—"}</td><td>${e.phone || "—"}</td><td class="num">${num(e.record_count)}</td><td class="num">${num(e.source_file_count)}</td></tr>`)}</tbody></table></div>`.__raw : html`<div class="empty">${input.value ? "No matching companies." : "Import and read your files to see companies here."}</div>`.__raw;
  $$('[data-company]', box).forEach(button => button.addEventListener("click", guard(() => openEntity(button.dataset.company))));
}
function chartOpts(scales = true) {
  Chart.defaults.font.family = "'IBM Plex Sans', system-ui, sans-serif";
  const o = { responsive: true, maintainAspectRatio: false, plugins: { legend: { display: false } } };
  if (scales) o.scales = { x: { grid: { display: false }, ticks: { precision: 0 } }, y: { beginAtZero: true, ticks: { precision: 0 }, grid: { color: "#ecebe3" } } };
  return o;
}
function stagesHtml(p) {
  if (!p.stages.length) return `<p class="muted">Ready. Choose your files, then click Read files. You can pause and resume without losing completed work.</p>`;
  return p.stages.map(s => html`<div class="stage"><span class="n">${s.name}</span><div class="bar"><i style="width:${s.status === "done" || s.status === "skipped" ? 100 : s.total ? Math.min(100, 100 * s.done / s.total) : 0}%"></i></div>
    <span class="small muted" style="text-align:right">${s.status}${s.total ? " " + num(s.done) + "/" + num(s.total) : ""}</span><small>${s.message}</small></div>`).map(x => x.__raw).join("") +
    (p.error ? `<div class="note ${p.error.startsWith("Stopped") ? "warn" : "bad"}" style="margin-top:8px">${esc(p.error)}</div>` : "");
}
function startPolling() {
  clearInterval(S.poll);
  S.poll = setInterval(async () => {
    try {
      const p = await api("/api/progress");
      if (S.status) S.status.progress = p;
      renderNav();
      const box = $("#stages");
      if (box) box.innerHTML = stagesHtml(p);
      if (!p.running) { clearInterval(S.poll); await refreshStatus(); if (S.page === "overview") route(); else if (S.page === "source") route(); }
    } catch { clearInterval(S.poll); }
  }, 1000);
}

// ---------------------------------------------------------------- source & files
PAGE.source = async () => {
  const cfg = S.cfg, inv = (await api("/api/inventory")).files, ds = await api("/api/drive/status");
  const processable = inv.filter(f => f.status !== "unsupported");
  const others = inv.filter(f => f.status === "unsupported");
  const fileRows = files => files.map(f => html`<tr><td>${f.source_path}</td><td>${f.ext}</td><td class="num">${bytes(f.size_bytes)}</td><td>${fileTag(f.status)}</td><td class="num">${num(f.records_extracted)}</td><td class="small muted">${f.error || f.notes || ""}</td></tr>`).map(x => x.__raw).join("");
  const fileTable = (files, empty) => files.length ? html`<div class="tw"><table><thead><tr><th>File</th><th>Type</th><th class="num">Size</th><th>Status</th><th class="num">Records</th><th>Notes</th></tr></thead><tbody>${raw(fileRows(files))}</tbody></table></div>` : html`<div class="empty">${empty}</div>`;
  const mode = cfg.source_kind;
  const sourceReady = mode === "drive" ? !!cfg.drive_folder : !!cfg.local_input_dir;
  const tab = S.srcTab || (mode === "drive" ? "drive" : "local");
  $("#view").innerHTML = html`
    <div class="card"><div class="h"><h2>Where are your files?</h2>
      <div class="seg" role="tablist">${[["local", "Local folder"], ["drive", "Google Drive"], ["upload", "Upload files"], ["demo", "Demo data"]].map(([k, l]) => html`<button role="tab" data-tab="${k}" class="${tab === k ? "on" : ""}">${l}</button>`)}</div></div>
      <div class="row" style="margin-bottom:12px"><button id="useSavedUploadsSource">Use uploaded files</button><span class="small muted">Reconnect the documents already saved in this workspace.</span></div>
      <div id="srcbody">${tab === "local" ? html`<div class="stack"><label class="f">Input folder (read-only - sub-folders are included)
        <input id="inpath" value="${cfg.source_kind === "local" ? cfg.local_input_dir : ""}" placeholder="/path/to/your/files" style="width:100%"></label>
        <div class="row"><button class="primary" id="saveSrc">Use this folder</button><span class="muted small">Your files are never modified. Results go to the output folder (${cfg.output_dir || "data/output"}).</span></div></div>`
      : tab === "drive" ? html`<div class="stack"><label class="f">Google Drive folder URL or ID<input id="drvurl" value="${cfg.drive_folder}" placeholder="https://drive.google.com/drive/folders/..." style="width:100%"></label>
        <div class="note">${ds.help} Excel and other supported documents are downloaded during Scan. ZIP archives can be selected below.</div>
        <div class="row"><button class="primary" id="saveDrv">Use this Drive folder</button><button id="listDriveFiles">Show files in connected folder</button></div><div id="driveFiles"></div></div>`
      : tab === "upload" ? html`<div class="stack"><a class="btn primary" href="#/vendorimports">Open shared vendor upload / Google Drive import</a><p class="small muted">For large collections, keep originals in Drive and index shared records. Local ZIP uploads depend on available server space.</p><input type="file" id="upfiles" multiple><div class="row"><button class="primary" id="doUp">Upload &amp; use in Organizer</button><span class="muted small">These files use Organizer working storage. Server capacity and processing safeguards apply.</span></div></div>`
      : html`<div class="stack"><p class="muted">Creates ~11 synthetic files (Excel, legacy .xls, CSV, PDF, Word, text, a scanned image, a corrupt file, a duplicate…) with deliberately messy names such as <em>SMS HOSPITAL JAIPUR</em> / <em>S.M.S. Hospital</em> / <em>Sawai Man Singh Hospital</em>.</p><div class="row"><button class="primary" id="mkDemo">Create demo files &amp; use them</button></div></div>`}</div></div>
    <div class="card"><div class="h"><h2>Files</h2><div class="row"><button id="btnScan" ${sourceReady ? "" : "disabled"}>Scan folder</button><button id="btnInspect" ${sourceReady ? "" : "disabled"}>Inspect structure</button><button class="primary" id="btnOrganize" ${inv.length ? "" : "disabled"}>Organize into company folders</button></div></div>
      <h3>Processable files <span class="muted small">${num(processable.length)}</span></h3>
      ${fileTable(processable, "No processable files listed yet. Choose a source above, then press Scan folder.")}
      ${others.length ? html`<details style="margin-top:18px"><summary><strong>Others</strong> <span class="muted small">${num(others.length)} unsupported files</span></summary>${fileTable(others, "No unsupported files.")}</details>` : ""}</div>
    <div id="inspectbox"></div>`.__raw;
  $$("[data-tab]").forEach(b => b.addEventListener("click", () => { S.srcTab = b.dataset.tab; PAGE.source(); }));
  $("#useSavedUploadsSource").addEventListener("click", guard(async event => {
    const button = event.currentTarget; button.disabled = true;
    try { await post("/api/source/uploads"); toast("Your uploaded files are selected"); S.srcTab = "local"; await route(); }
    finally { if (button.isConnected) button.disabled = false; }
  }));
  $("#saveSrc")?.addEventListener("click", guard(async () => {
    const path = $("#inpath").value.trim();
    if (!path) { toast("Enter a folder path or click Use uploaded files", true); return; }
    await api("/api/config", { method: "PUT", body: { source_kind: "local", local_input_dir: path } }); toast("Source saved"); route();
  }));
  $("#saveDrv")?.addEventListener("click", guard(async () => { await api("/api/config", { method: "PUT", body: { source_kind: "drive", drive_folder: $("#drvurl").value.trim() } }); toast("Drive folder saved"); route(); }));
    $("#listDriveFiles")?.addEventListener("click", guard(async event => {
      const button = event.currentTarget; button.disabled = true;
      try {
        const result = await api('/api/drive/files');
        $('#driveFiles').innerHTML = html`<p>${num(result.files.length)} files found. For ZIPs over 2 GB, including 10+ GB, <a href="/drive-library">open the shared Drive library</a> to index without downloading the whole archive.</p><p>To copy a smaller ZIP into Organizer instead, select it below. After reading, use Create folders &amp; sync to Vendor.</p>${result.files.map(file => html`<div class="row"><span>${file.path}</span>${file.name.toLowerCase().endsWith('.zip') ? html`<button data-drive-zip="${file.id}">Copy ZIP into Organizer (up to 2 GB)</button>` : ''}</div>`)}<p class="small muted">Copy limits: 2 GiB download, 5 GiB expanded, 5,000 entries, 128 MB per document.</p>`.__raw;
        $$('[data-drive-zip]').forEach(control => control.addEventListener('click', guard(async () => {
          control.disabled = true; control.textContent = 'Downloading and extracting…';
          try { const imported = await post('/api/drive/import-zip', {file_id: control.dataset.driveZip}); toast(`${imported.saved} imported. Read files, then sync to Vendor.${imported.skipped_entries ? ` ${imported.skipped_entries} unsafe entries skipped.` : ''}`); S.srcTab = 'drive'; await refreshStatus(); route(); }
          finally { control.disabled = false; control.textContent = 'Copy ZIP into Organizer (up to 2 GB)'; }
        })));
      } finally { button.disabled = false; }
    }));
  $("#mkDemo")?.addEventListener("click", guard(async () => { await post("/api/demo/prepare"); S.srcTab = "local"; toast("Demo files ready"); route(); }));
  $("#doUp")?.addEventListener("click", guard(async () => {
    const fd = new FormData(); [...$("#upfiles").files].forEach(f => fd.append("files", f));
    if (![...fd.keys()].length) return toast("Choose at least one file", true);
    const button = $("#doUp");
    button.disabled = true;
    button.textContent = "Uploading…";
    try {
      const r = await uploadWithProgress(fd, (loaded, total) => {
        const percent = Math.round(loaded / total * 100);
        button.textContent = `Uploading ${percent}% (${bytes(loaded)} / ${bytes(total)})`;
      });
      button.textContent = "Scanning…";
      await post("/api/scan");
      toast(`${r.saved.length} file(s) uploaded and scanned${r.skipped_entries ? `; ${r.skipped_entries} unreadable ZIP entr${r.skipped_entries === 1 ? "y" : "ies"} skipped` : ""}`); S.srcTab = "local"; route();
    } finally {
      if (button.isConnected) { button.disabled = false; button.textContent = "Upload & use"; }
    }
  }));
  $("#btnScan").addEventListener("click", guard(async e => { e.target.disabled = true; try { const r = await post("/api/scan"); toast(`${r.count} files found`); route(); } finally { e.target.disabled = false; } }));
  $("#btnOrganize").addEventListener("click", guard(async e => { e.target.disabled = true; e.target.textContent = "Organizing…"; try { const r = await post("/api/organize-files"); toast(`${r.files_copied} originals copied into ${r.company_folders} company folders; Vendor sync started`); watchVendorSync(r.vendor_sync); } finally { e.target.disabled = false; e.target.textContent = "Organize into company folders"; } }));
  $("#btnInspect").addEventListener("click", guard(async e => { e.target.disabled = true; e.target.textContent = "Inspecting…"; try { renderInspection((await post("/api/inspect", { max_files: 60 })).files); } finally { e.target.disabled = false; e.target.textContent = "Inspect structure"; } }));
};

function renderInspection(files) {
  const fields = ["name", "address", "city", "district", "state", "pincode", "phone", "email", "website", "registration_id", "category", "ignore"];
  $("#inspectbox").innerHTML = html`<div class="card"><div class="h"><h2>File inspection</h2><span class="muted small">How each file was understood. Change a mapping if a column was guessed wrong.</span></div>
    ${files.map(f => html`<details class="file" ${f.status === "error" ? raw("open") : ""}><summary><strong>${f.path}</strong>${fileTag(f.status === "ok" ? "processed" : f.status === "error" ? "failed" : f.status)}<span class="muted small">${f.records != null ? num(f.records) + " records" : ""}${f.sheets ? " · " + f.sheets + " sheet(s)" : ""}${f.pages ? " · " + f.pages + " page(s)" : ""}</span></summary><div class="in">
      ${(f.issues || []).map(i => html`<div class="note ${i.severity === "error" ? "bad" : i.severity === "warning" ? "warn" : ""}" style="margin-bottom:6px">${i.location !== "file" ? i.location + ": " : ""}${i.message}</div>`)}
      ${(f.blocks || []).map(b => html`<h3 style="margin:10px 0 6px">${b.sheet || (b.page ? "Page " + b.page : "Content")} <span class="muted small">${b.method}${b.header_row ? " · header on row " + b.header_row + " (" + b.header_source.replace(/_/g, " ") + ")" : ""}${b.mode ? " · " + b.mode + " mode" : ""} · ${num(b.records)} records${b.blank_rows ? " · " + b.blank_rows + " blank skipped" : ""}${b.repeated_headers ? " · " + b.repeated_headers + " repeated header rows skipped" : ""}</span></h3>
        ${(b.preamble || []).length ? html`<p class="small muted">Rows above the header (kept as context): ${b.preamble.join(" ‖ ")}</p>` : ""}
        ${(b.columns || []).length ? html`<table><thead><tr><th>Original column</th><th>Mapped to</th><th>How</th></tr></thead><tbody>${b.columns.map(c => html`<tr><td>${c.original}</td><td><select data-h="${c.original}" data-f="${f.file}" aria-label="Map ${c.original}">
          <option value="">${c.field ? "(auto) " + c.field : "(kept, not mapped)"}</option>${fields.map(x => html`<option value="${x}">${x === "ignore" ? "ignore column" : x}</option>`)}</select></td><td class="small muted">${c.method}${c.confidence ? " " + Math.round(c.confidence * 100) + "%" : ""}</td></tr>`)}</tbody></table>` : ""}
        ${(b.samples || []).length ? html`<details style="margin-top:6px"><summary class="small muted" style="cursor:pointer">Sample rows</summary><pre class="mono" style="white-space:pre-wrap">${JSON.stringify(b.samples.slice(0, 3), null, 1)}</pre></details>` : ""}`)}
    </div></details>`)}<div class="row" style="margin-top:8px"><button class="primary" id="saveMap">Save column overrides</button><span class="small muted">Overrides apply to files extracted after saving. Already-processed files need Settings → Clear everything and a re-run.</span></div></div>`.__raw;
  $("#saveMap").addEventListener("click", guard(async () => {
    const ov = { ...(S.cfg.column_overrides || {}) };
    $$("#inspectbox select").forEach(s => { if (s.value) ov[`${s.dataset.f}::${s.dataset.h}`] = s.value; });
    await api("/api/config", { method: "PUT", body: { column_overrides: ov } }); toast(`${Object.keys(ov).length} override(s) saved`);
  }));
}

// ---------------------------------------------------------------- preview (dry run)
PAGE.preview = async () => {
  $("#view").innerHTML = html`<div class="card"><div class="h"><h2>Dry run on a sample</h2><div class="row"><label class="row small">Sample size <input id="ssz" type="number" min="10" max="5000" value="${S.cfg.dry_run_sample_size}" style="width:90px"></label><button class="primary" id="go">Preview standardisation</button></div></div>
    <p class="muted">Runs the real extraction, cleaning and matching on a few records per file in a throw-away database - <strong>nothing is saved</strong>. Use it to check the column detection and the name standardisation before processing everything.</p></div><div id="res"></div>`.__raw;
  $("#go").addEventListener("click", guard(async e => {
    e.target.disabled = true; e.target.textContent = "Running…";
    try {
      const r = await post("/api/dry-run", { sample: +$("#ssz").value });
      $("#res").innerHTML = html`<div class="card"><h2>${num(r.count)} sample records</h2><div class="tw"><table><thead><tr><th>File</th><th>Where</th><th>Original name</th><th>Normalized</th><th>Suggested standard name</th><th class="num">Conf.</th><th>Outcome</th><th>Why</th></tr></thead><tbody>
        ${r.rows.map(x => html`<tr><td class="small">${x.file}</td><td class="small muted">${x.location || ""}${x.row ? " r" + x.row : ""}</td><td>${x.original_name || html`<span class="faint">(no entity name)</span>`}</td><td class="mono">${x.normalized_name}</td><td><strong>${x.suggested_standard_name || ""}</strong></td><td class="num">${score(x.confidence)}</td><td>${statusTag(x.status)}</td><td class="small muted">${x.reason || ""}</td></tr>`)}</tbody></table></div></div>`.__raw;
    } finally { e.target.disabled = false; e.target.textContent = "Preview standardisation"; }
  }));
};

// ---------------------------------------------------------------- manual review
PAGE.review = async () => {
  $("#view").innerHTML = html`<div class="split"><div><div class="card" style="padding:12px"><div class="row"><input id="rq" placeholder="Search name or city" value="${S.review.q}" style="flex:1"><button class="sm" id="bulk" title="Accept every listed suggestion at or above a score">Bulk…</button></div>
    <div class="small muted" id="rcount" style="margin:8px 0 4px"></div><div class="queue" id="queue"></div></div></div><div id="rdetail"></div></div>`.__raw;
  $("#rq").addEventListener("input", debounce(() => { S.review.q = $("#rq").value; loadQueue(); }, 250));
  $("#bulk").addEventListener("click", guard(bulkAccept));
  await loadQueue();
  document.onkeydown = e => {
    if (S.page !== "review" || /INPUT|TEXTAREA|SELECT/.test(e.target.tagName)) return;
    if (e.key === "a") decide("accept"); else if (e.key === "r") decide("reject"); else if (e.key === "n") decide("new_entity");
    else if (e.key === "j") stepQueue(1); else if (e.key === "k") stepQueue(-1);
  };
};
const debounce = (fn, ms) => { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; };
async function loadQueue(keep = true) {
  const r = await api(`/api/reviews?limit=100&search=${encodeURIComponent(S.review.q)}`);
  S.review.items = r.items; S.review.total = r.total;
  if (!keep || !r.items.some(i => i.review_id === S.review.cur)) S.review.cur = r.items[0]?.review_id ?? null;
  $("#rcount").textContent = `${num(r.total)} waiting`;
  $("#queue").innerHTML = r.items.length ? r.items.map(i => html`<button class="qi ${i.review_id === S.review.cur ? "on" : ""}" data-id="${i.review_id}"><div class="a">${i.original_name}</div>
    <div class="b">${i.context_city || "no city"} → ${i.candidate?.standard_name || "?"}</div><div class="row sb" style="margin-top:4px">${bandTag(i.match_band)}<span class="small">${score(i.match_score)}${i.record_count > 1 ? " · " + i.record_count + " records" : ""}</span></div></button>`).map(x => x.__raw).join("")
    : `<div class="empty small">Nothing to review.</div>`;
  $$(".qi").forEach(b => b.addEventListener("click", () => { S.review.cur = +b.dataset.id; $$(".qi").forEach(x => x.classList.toggle("on", x === b)); showDetail(); }));
  await showDetail();
}
function stepQueue(d) { const ids = S.review.items.map(i => i.review_id), i = ids.indexOf(S.review.cur); const n = ids[Math.max(0, Math.min(ids.length - 1, i + d))]; if (n != null) { S.review.cur = n; $$(".qi").forEach(x => x.classList.toggle("on", +x.dataset.id === n)); showDetail(); } }
const kvRows = (o, keys) => keys.filter(([k]) => o && o[k]).map(([k, l]) => html`<dt>${l}</dt><dd>${o[k]}</dd>`);
async function showDetail() {
  const box = $("#rdetail");
  if (!S.review.cur) { box.innerHTML = html`<div class="card empty"><h3>All caught up</h3><p>No uncertain matches are waiting. Every decision you make is remembered and applied to future files.</p><a class="btn primary" href="#/export">Go to export</a></div>`.__raw; return; }
  const d = S.review.detail = await api(`/api/reviews/${S.review.cur}`);
  const s = d.sample || {}, c = d.candidate || {}, ev = d.evidence || {};
  box.innerHTML = html`<div class="card"><h2 style="display:block">Is <em>“${d.original_name}”</em> the same as <em>“${c.standard_name}”</em>?</h2>
    <div class="row" style="margin:6px 0 12px">${bandTag(d.match_band)}<strong>${Number(d.match_score).toFixed(0)}% confidence</strong><span class="muted">${d.record_count} record(s) with this name</span></div>
    <div class="cmp"><div><h3>Found in your data</h3><dl class="kv"><dt>Name</dt><dd><strong>${d.original_name}</strong></dd>${kvRows(s, [["original_address", "Address"], ["original_city", "City"], ["original_state", "State"], ["original_phone", "Phone"], ["original_email", "Email"], ["original_registration_id", "Reg. ID"]])}
        <dt>Source</dt><dd class="small">${s.source_path || s.source_file}${s.source_sheet ? " › " + s.source_sheet : ""}${s.source_page ? " › page " + s.source_page : ""}${s.source_row ? " › row " + s.source_row : ""}</dd></dl></div>
      <div><h3>Suggested master entity <span class="mono">${c.master_entity_id}</span></h3><dl class="kv"><dt>Name</dt><dd><strong>${c.standard_name}</strong></dd>${kvRows(c, [["address", "Address"], ["city", "City"], ["state", "State"], ["phone", "Phone"], ["registration_id", "Reg. ID"]])}
        <dt>Also known as</dt><dd>${(c.aliases || []).slice(0, 6).join("; ") || "-"}</dd><dt>Records</dt><dd>${c.record_count}</dd></dl></div></div>
    <h3 style="margin:14px 0 6px">Evidence</h3><div class="chips">${(ev.checklist || []).map(e => html`<span class="chip ${e.status}" title="${e.detail}">${{ match: "✓", conflict: "✗", partial: "≈", missing: "–" }[e.status]} ${e.key.replace("_", " ")}${e.status !== "missing" ? ": " + String(e.detail).slice(0, 44) : ""}</span>`)}</div>
    <p class="muted" style="margin:8px 0 0">${d.reason}</p>
    ${(ev.alternatives || []).length ? html`<h3 style="margin:14px 0 6px">Other possible matches</h3>${ev.alternatives.map(a => html`<div class="row sb" style="padding:4px 0"><span>${a.name} <span class="mono muted">${a.entity_id}</span> · ${Math.round(a.score)}%</span><button class="sm" data-alt="${a.entity_id}">It is this one</button></div>`)}` : ""}
    ${d.records?.length > 1 ? html`<details style="margin-top:10px"><summary class="small muted" style="cursor:pointer">All ${d.records.length} affected records</summary><table class="small">${d.records.map(r => html`<tr><td>${r.source_file}</td><td>${loc(r)}</td><td>row ${r.source_row}</td><td>${r.original_name}</td></tr>`)}</table></details>` : ""}</div>
    <div class="actions"><button class="primary" id="dAccept">Same entity <kbd>A</kbd></button><button id="dReject">Not the same <kbd>R</kbd></button>
      <span class="row"><input id="newname" placeholder="Standard name (optional)" style="width:210px" value="${d.original_name}"><button id="dNew">New entity <kbd>N</kbd></button></span>
      <label class="row small"><input type="checkbox" id="scope"> apply to this name in every city</label></div>`.__raw;
  $("#dAccept").onclick = () => decide("accept"); $("#dReject").onclick = () => decide("reject"); $("#dNew").onclick = () => decide("new_entity");
  $$("[data-alt]").forEach(b => b.onclick = () => decide("accept", b.dataset.alt));
}
const decide = guard(async (action, target) => {
  const id = S.review.cur; if (!id) return;
  const body = { action, scope: $("#scope")?.checked ? "alias" : "city" };
  if (target) body.target_id = target;
  if (action === "new_entity") body.standard_name = $("#newname")?.value;
  const r = await post(`/api/reviews/${id}/decide`, body);
  toast(`${r.decided.length} decision(s) saved - remembered for future files`);
  const ids = S.review.items.map(i => i.review_id), i = ids.indexOf(id);
  S.review.cur = ids[i + 1] ?? ids[i - 1] ?? null;
  await refreshStatus(); await loadQueue();
});
async function bulkAccept() {
  const min = +prompt("Accept every listed suggestion with confidence at or above (0-100):", "80");
  if (!min) return;
  const ids = S.review.items.filter(i => i.match_score >= min).map(i => i.review_id);
  if (!ids.length) return toast("Nothing at or above that score", true);
  if (!await confirmBox("Bulk accept", `Merge ${ids.length} suggestion(s) with confidence ≥ ${min}%? You can still rename or split entities later.`, "Accept all")) return;
  const r = await post("/api/reviews/bulk", { ids, action: "accept" });
  toast(`${r.decided.length} accepted${r.failed.length ? ", " + r.failed.length + " failed" : ""}`); await refreshStatus(); await loadQueue(false);
}

// ---------------------------------------------------------------- entities
const E = { q: "", status: "", offset: 0, sort: "standard_name", dir: "asc" };
PAGE.entities = async () => {
  $("#view").innerHTML = html`<div class="card"><div class="row"><input id="eq" placeholder="Search name, alias, registration id, phone…" value="${E.q}" style="flex:1;min-width:220px">
    <select id="est" aria-label="Status"><option value="">All statuses</option>${[["auto", "Auto-matched"], ["verified", "Verified by user"], ["provisional", "Pending review"]].map(([v, l]) => html`<option value="${v}" ${E.status === v ? raw("selected") : ""}>${l}</option>`)}</select></div></div><div id="etable"></div>`.__raw;
  $("#eq").addEventListener("input", debounce(() => { E.q = $("#eq").value; E.offset = 0; loadEntities(); }, 250));
  $("#est").addEventListener("change", () => { E.status = $("#est").value; E.offset = 0; loadEntities(); });
  await loadEntities();
};
async function loadEntities() {
  const r = await api(`/api/entities?search=${encodeURIComponent(E.q)}&status=${E.status}&sort=${E.sort}&direction=${E.dir}&limit=50&offset=${E.offset}`);
  const th = (k, l, cls = "") => html`<th class="${cls}"><a href="#" data-sort="${k}" style="color:inherit;text-decoration:none">${l}${E.sort === k ? (E.dir === "asc" ? " ↑" : " ↓") : ""}</a></th>`;
  $("#etable").innerHTML = html`<div class="card"><div class="h"><h2>${num(r.total)} master entities</h2><a class="btn sm" href="#/export">Export</a></div><div class="tw"><table><thead><tr>${th("master_entity_id", "ID")}${th("standard_name", "Standard name")}<th>Also found as</th>${th("city", "City")}${th("state", "State")}${th("record_count", "Records", "num")}${th("confidence", "Conf.", "num")}<th>Status</th></tr></thead><tbody>
    ${r.items.map(e => html`<tr class="click" data-id="${e.master_entity_id}" tabindex="0"><td class="mono">${e.master_entity_id}</td><td><strong>${e.standard_name}</strong>${e.data_conflicts ? html` <span class="tag t-warn" title="${e.data_conflicts}">conflict</span>` : ""}</td>
      <td class="small muted">${(e.aliases || []).filter(a => a !== e.standard_name).slice(0, 3).join("; ")}${e.aliases.length > 4 ? " +" + (e.aliases.length - 4) : ""}</td><td>${e.city}</td><td>${e.state}</td><td class="num">${e.record_count}</td><td class="num">${score(e.confidence)}</td>
      <td>${e.verification_status === "verified" ? html`<span class="tag t-acc">verified</span>` : e.verification_status === "provisional" ? html`<span class="tag t-warn">pending review</span>` : html`<span class="tag t-ok">auto</span>`}</td></tr>`)}
    </tbody></table></div><div class="row sb" style="margin-top:10px"><span class="muted small">${r.total ? E.offset + 1 : 0}-${Math.min(E.offset + 50, r.total)} of ${num(r.total)}</span><span class="row"><button class="sm" id="prev" ${E.offset ? "" : "disabled"}>Previous</button><button class="sm" id="next" ${E.offset + 50 < r.total ? "" : "disabled"}>Next</button></span></div></div>`.__raw;
  $$("[data-sort]").forEach(a => a.addEventListener("click", e => { e.preventDefault(); const k = a.dataset.sort; E.dir = E.sort === k && E.dir === "asc" ? "desc" : "asc"; E.sort = k; loadEntities(); }));
  $$("#etable tr.click").forEach(tr => { tr.addEventListener("click", () => openEntity(tr.dataset.id)); tr.addEventListener("keydown", e => { if (e.key === "Enter") openEntity(tr.dataset.id); }); });
  $("#prev")?.addEventListener("click", () => { E.offset = Math.max(0, E.offset - 50); loadEntities(); });
  $("#next")?.addEventListener("click", () => { E.offset += 50; loadEntities(); });
}
const openEntity = guard(async id => {
  const e = await api(`/api/entities/${id}`), dr = $("#drawer");
  dr.hidden = false;
  dr.innerHTML = html`<div class="row sb"><h2>${e.standard_name}</h2><button id="dclose" aria-label="Close">✕</button></div><p class="mono muted">${e.master_entity_id} · ${e.verification_status} · confidence ${e.confidence}%</p>
    <div class="row" style="margin:8px 0"><button id="ren">Rename</button><button id="mrg">Merge into another entity…</button></div>
    ${Object.keys(e.data_conflicts || {}).length ? html`<div class="note warn"><strong>Records disagree:</strong> ${Object.entries(e.data_conflicts).map(([k, v]) => k + ": " + v.join(" / ")).join(" · ")}</div>` : ""}
    <dl class="kv" style="margin:12px 0">${kvRows(e, [["city", "City"], ["district", "District"], ["state", "State"], ["address", "Address"], ["pincode", "PIN"], ["phone", "Phone"], ["email", "Email"], ["website", "Website"], ["registration_id", "Reg. ID"], ["entity_type", "Type"], ["category", "Category"]])}<dt>Records</dt><dd>${e.record_count} from ${e.source_file_count} file(s)</dd></dl>
    <h3>Names found for this entity (${e.alias_details.length})</h3><table class="small"><thead><tr><th>As written</th><th class="num">Times</th><th>How it was matched</th><th>First seen</th></tr></thead><tbody>${e.alias_details.map(a => html`<tr><td>${a.alias}${a.verified ? html` <span class="tag t-acc">verified</span>` : ""}</td><td class="num">${a.occurrences}</td><td>${a.match_method}</td><td class="muted">${a.original_source}</td></tr>`)}</tbody></table>
    <h3 style="margin-top:14px">Source records (traceability)</h3><div class="tw"><table class="small"><thead><tr><th>File</th><th>Where</th><th>Original name</th><th>Outcome</th><th class="num">Conf.</th></tr></thead><tbody>${e.records.map(r => html`<tr><td>${r.source_file}</td><td>${loc(r)} r${r.source_row}</td><td>${r.original_name}</td><td>${statusTag(r.match_status)}</td><td class="num">${score(r.match_score)}</td></tr>`)}</tbody></table></div>`.__raw;
  $("#dclose").onclick = () => { dr.hidden = true; };
  $("#ren").onclick = guard(async () => { const n = prompt("Standard name:", e.standard_name); if (n && n.trim()) { await post(`/api/entities/${id}/rename`, { standard_name: n.trim() }); toast("Renamed"); (S.page === "overview" ? loadOverviewCompanies() : loadEntities()); openEntity(id); } });
  $("#mrg").onclick = guard(async () => {
    const q = prompt("Search for the entity to merge INTO (name or id):"); if (!q) return;
    const r = await api(`/api/entities?search=${encodeURIComponent(q)}&limit=8`);
    const opts = r.items.filter(x => x.master_entity_id !== id); if (!opts.length) return toast("No other entity matches", true);
    const pick = prompt("Merge into which one? Type the number:\n" + opts.map((x, i) => `${i + 1}. ${x.standard_name} (${x.city || "-"}) ${x.master_entity_id}`).join("\n"));
    const t = opts[(+pick || 0) - 1]; if (!t) return;
    if (await confirmBox("Merge entities", `Merge “${e.standard_name}” into “${t.standard_name}”? All its names and records move to the target.`, "Merge", true)) {
      await post("/api/entities/merge", { source_id: id, target_id: t.master_entity_id }); toast("Merged"); dr.hidden = true;
      if (S.page === "overview") await route(); else await loadEntities();
    }
  });
});
document.addEventListener("keydown", e => { if (e.key === "Escape") { $("#drawer").hidden = true; $("#modal").hidden = true; } });

// ---------------------------------------------------------------- quality & duplicates
PAGE.quality = async () => {
  const tab = S.qTab || "dups";
  $("#view").innerHTML = html`<div class="seg" style="margin-bottom:14px">${[["dups", "Duplicates"], ["issues", "Data-quality issues"], ["errors", "Processing errors"]].map(([k, l]) => html`<button data-t="${k}" class="${tab === k ? "on" : ""}">${l}</button>`)}</div><div id="qbody"></div>`.__raw;
  $$("[data-t]").forEach(b => b.addEventListener("click", () => { S.qTab = b.dataset.t; PAGE.quality(); }));
  const box = $("#qbody");
  if (tab === "dups") {
    const r = await api("/api/duplicates?limit=100");
    box.innerHTML = r.total ? html`<p class="muted">Nothing is deleted or merged automatically - these are groups to look at. The suggested primary has the strongest evidence.</p>${r.groups.map(g => html`<div class="card"><div class="h"><h3>${g.type.replace(/_/g, " ")}</h3><span class="mono muted small">${g.group_id}</span></div><table class="small"><tbody>${g.members.map(m => html`<tr><td>${m.is_suggested_primary ? html`<span class="tag t-ok">primary</span>` : ""}</td><td>${m.original_name || m.standard_name}</td><td class="muted">${m.source_file || ""}${m.source_row ? " r" + m.source_row : ""}</td><td class="mono muted">${m.master_entity_id || ""}</td></tr>`)}</tbody></table></div>`)}`.__raw
      : `<div class="card empty"><h3>No duplicates found</h3>Duplicate detection runs at the end of every run (Settings can switch it off).</div>`;
  } else if (tab === "issues") {
    const sev = S.qSev || "", r = await api(`/api/quality?limit=200&severity=${sev}`);
    box.innerHTML = html`<div class="card"><div class="row" style="margin-bottom:10px">${["", "error", "warning", "info"].map(s => html`<button class="sm ${sev === s ? "primary" : ""}" data-sev="${s}">${s || "all"}</button>`)}<span class="muted small">${num(r.total)} findings</span></div><div class="tw"><table><thead><tr><th>Severity</th><th>Issue</th><th>Value</th><th>What is wrong</th><th>Where</th></tr></thead><tbody>
      ${r.items.map(i => html`<tr><td>${sevTag(i.severity)}</td><td>${i.issue_type.replace(/_/g, " ")}</td><td class="mono">${String(i.value ?? "").slice(0, 60)}</td><td class="small">${i.message}</td><td class="small muted">${i.source_file || i.master_entity_id || ""} ${loc(i)} ${i.source_row ? "r" + i.source_row : ""}</td></tr>`)}</tbody></table></div></div>`.__raw;
    $$("[data-sev]").forEach(b => b.addEventListener("click", () => { S.qSev = b.dataset.sev; PAGE.quality(); }));
  } else {
    const r = await api("/api/errors?limit=300");
    box.innerHTML = html`<div class="card"><p class="muted">Every failure names the file and the sheet / page / row, so nothing fails silently. One bad file never stops the run.</p><div class="tw"><table><thead><tr><th>When</th><th>File</th><th>Where</th><th>Stage</th><th>Severity</th><th>Message</th></tr></thead><tbody>
      ${r.items.map(i => html`<tr><td class="small">${String(i.occurred_at).replace("T", " ")}</td><td>${i.file_name}</td><td class="small">${i.location}</td><td>${i.stage}</td><td>${sevTag(i.severity)}</td><td class="small">${i.message}</td></tr>`)}</tbody></table></div></div>`.__raw;
  }
};

// ---------------------------------------------------------------- export
PAGE.export = async () => {
  const cfg = S.cfg, files = (await api("/api/exports")).files;
  const groups = [["state", "State"], ["district", "District"], ["city", "City"], ["entity_type", "Entity type"], ["category", "Category / industry"], ["source", "Source file"], ["year", "Source year"]];
  $("#view").innerHTML = html`<div class="card"><h2>Create output files</h2><div class="grid g2" style="margin-top:8px"><div class="stack"><strong>Formats</strong><div class="chips">${["xlsx", "csv", "docx", "pdf"].map(f => html`<label class="chip"><input type="checkbox" name="fmt" value="${f}" ${cfg.export_formats.includes(f) ? raw("checked") : ""}> ${f}</label>`)}</div></div>
    <div class="stack"><strong>Also organise copies into folders by</strong><div class="chips">${groups.map(([k, l]) => html`<label class="chip"><input type="checkbox" name="grp" value="${k}" ${cfg.group_by.includes(k) ? raw("checked") : ""}> ${l}</label>`)}</div></div></div>
    <div class="row" style="margin-top:14px"><button class="primary" id="doExp">Generate output files</button><button id="doZip">Create company-folder ZIP</button><button class="sm" id="resetExp">Reset export options</button><span class="muted small">One ZIP with a folder for every company; unmatched files go to Work Related Data.</span></div></div>
    <div class="card"><div class="h"><h2>Output folder</h2><span class="mono muted small">${cfg.output_dir || "data/output"}</span></div>${files.length ? html`<table><thead><tr><th>File</th><th class="num">Size</th><th></th></tr></thead><tbody>${files.map(f => html`<tr><td>${f.file}</td><td class="num">${bytes(f.bytes)}</td><td><a class="btn sm" href="/api/exports/download?file=${encodeURIComponent(f.file)}${sessionStorage.getItem(TOKEN_KEY) ? "&token=" + encodeURIComponent(sessionStorage.getItem(TOKEN_KEY)) : ""}">Download</a></td></tr>`)}</tbody></table>` : `<div class="empty">No files yet.</div>`}</div>`.__raw;
  $("#doExp").addEventListener("click", guard(async e => {
    const formats = $$("[name=fmt]:checked").map(x => x.value), group_by = $$("[name=grp]:checked").map(x => x.value);
    if (!formats.length) return toast("Choose at least one format", true);
    e.target.disabled = true; e.target.textContent = "Writing…";
    try { await api("/api/config", { method: "PUT", body: { export_formats: formats, group_by } }); const r = await post("/api/export"); toast(`${r.files.length} files written`); route(); }
    finally { e.target.disabled = false; e.target.textContent = "Generate output files"; }
  }));
  $("#doZip").addEventListener("click", guard(async e => {
    e.target.disabled = true; e.target.textContent = "Creating ZIP…";
    try { const r = await post("/api/export/structured-zip"); toast(`${r.files.toLocaleString()} files packaged`); route(); }
    finally { e.target.disabled = false; e.target.textContent = "Create company-folder ZIP"; }
  }));
  $("#resetExp").addEventListener("click", guard(async () => {
    const defaults = ["xlsx", "csv", "docx", "pdf"];
    $$('[name=fmt]').forEach(input => { input.checked = defaults.includes(input.value); });
    $$('[name=grp]').forEach(input => { input.checked = false; });
    S.cfg = await api("/api/config", { method: "PUT", body: { export_formats: defaults, group_by: [] } });
    toast("Export options reset");
  }));
};

// ------------------------------------------------------- catalogue approval
PAGE.catalogue = async () => {
  const [vendors, imports, metrics, history] = await Promise.all([
    api("/api/catalogue/vendors"), api("/api/catalogue/imports"), api("/api/catalogue/metrics"),
    api("/api/catalogue/price-history?limit=25"),
  ]);
  const productQuery = S.catalogueProductQuery || "";
  const productVendor = S.catalogueProductVendor || "";
  const productOffset = S.catalogueProductOffset || 0;
  const productParams = new URLSearchParams({ query: productQuery, vendor_id: productVendor, limit: "100", offset: String(productOffset) });
  const products = await api(`/api/catalogue/products?${productParams}`);
  const selectedId = S.catalogueImportId || imports.items[0]?.id;
  const selected = imports.items.find(item => item.id === selectedId);
  const offset = S.catalogueOffset || 0;
  const rows = selected
    ? (await api(`/api/catalogue/imports/${selected.id}/rows?status=pending&limit=200&offset=${offset}`)).items
    : [];
  const card = (value, label, tone = "") => html`<div class="card kpi ${tone}"><div class="v">${num(value)}</div><div class="l">${label}</div></div>`;
  const token = sessionStorage.getItem(TOKEN_KEY);
  const exportUrl = selected ? `/api/catalogue/imports/${selected.id}/export.csv?status=all${token ? "&token=" + encodeURIComponent(token) : ""}` : "";
  const productExportParams = new URLSearchParams({ query: productQuery, vendor_id: productVendor });
  if (token) productExportParams.set("token", token);
  const productExportUrl = `/api/catalogue/products/export.xlsx?${productExportParams}`;
  $("#view").innerHTML = html`
    <div class="grid g6" style="margin-bottom:16px">
      ${card(metrics.imports, "Catalogue uploads")}${card(metrics.extracted, "Extracted rows")}
      ${card(metrics.pending, "Awaiting approval", metrics.pending ? "warn" : "")}${card(metrics.approved, "Approved")}
      ${card(metrics.duplicates, "Duplicate rows")}${card(metrics.price_changes, "Price changes recorded")}
    </div>
    <div class="card"><h2>Upload a catalogue or price list</h2>
      <p class="muted">Rows stay out of product search until explicitly approved. Original files are retained for traceability.</p>
      <p class="muted small">Approved records become verified reference data for Vendor search and the assistant. Unapproved uploads are never training data; automatic AI model retraining is not enabled.</p>
      ${vendors.length ? html`<div class="row">
        <label class="f">Verified vendor<select id="catalogueVendor">${vendors.map(v => html`<option value="${v.vendor_id}">${v.company_name} · ${v.vendor_id}</option>`)}</select></label>
        <label class="f">Excel, CSV, WhatsApp TXT or ZIP<input id="catalogueFile" type="file" accept=".xlsx,.xls,.csv,.txt,.zip"></label>
        <button class="primary" id="catalogueUpload">Upload for review</button>
      </div><p class="muted small">Common product columns are detected automatically. WhatsApp TXT/ZIP imports only pair a product and price from the same message; review every extracted row. You can correct each row before approval. Maximum file size: 100 MB.</p>` :
      html`<div class="note warn">Add a vendor in the Vendor dashboard before importing catalogue rows. A vendor must be selected by its existing ID; it is never guessed from the file.</div>`}
    </div>
    <div class="card"><div class="h"><h2>Import batches</h2>${selected ? html`<a class="btn sm" href="${exportUrl}">Download cleaned CSV</a>` : ""}</div>
      ${imports.items.length ? html`<div class="tw"><table><thead><tr><th>File</th><th>Vendor</th><th>Uploaded</th><th>Status</th><th>Pending</th><th>Approved</th><th>Duplicates</th><th></th></tr></thead><tbody>
        ${imports.items.map(item => html`<tr><td>${item.filename}</td><td>${item.company_name} <span class="muted small">${item.company_key}</span></td>
          <td class="small">${item.uploaded_at}</td><td>${item.status}</td><td>${num(item.pending)}</td><td>${num(item.approved)}</td><td>${num(item.duplicates)}</td>
          <td><button class="sm ${selectedId === item.id ? "primary" : ""}" data-review-import="${item.id}">Review rows</button></td></tr>`)}
      </tbody></table></div>` : html`<div class="empty">No catalogue imports yet.</div>`}
    </div>
    <div class="card"><div class="h"><h2>Approved catalogue · available in Vendor search</h2><a class="btn sm" href="${productExportUrl}">Download Excel</a></div>
      <form id="catalogueProductFilter" class="row">
        <label class="f">Search product, SKU, vendor, brand or category<input id="catalogueProductQuery" value="${productQuery}"></label>
        <label class="f">Vendor<select id="catalogueProductVendor"><option value="">All vendors</option>
          ${vendors.map(v => html`<option value="${v.vendor_id}" ${productVendor === v.vendor_id ? "selected" : ""}>${v.company_name}</option>`)}
        </select></label>
        <button class="primary" type="submit">Search approved records</button>
      </form>
      ${products.items.length ? html`<div class="tw"><table><thead><tr><th>Vendor</th><th>Product</th><th>SKU</th><th>Category</th><th>Price</th><th>Unit / pack</th><th>Source / date</th><th>Updated</th></tr></thead><tbody>
        ${products.items.map(item => html`<tr><td>${item.company_name}</td><td>${item.product_name}</td><td>${item.sku || "—"}</td>
          <td>${item.product_category || "—"}</td><td>${item.price == null ? "No verified price" : `${item.currency || ""} ${item.price}`}</td>
          <td>${[item.unit, item.pack_size].filter(Boolean).join(" / ") || "—"}</td>
          <td>${item.source_file || "—"}${item.source_page ? ` · ${item.source_page}` : ""}${item.source_row ? ` · row ${item.source_row}` : ""}</td>
          <td>${item.extracted_at || "—"}</td></tr>`)}
      </tbody></table></div>
      <div class="row"><span class="muted small">Showing ${productOffset + 1}–${productOffset + products.items.length} of ${num(products.total)} sorted by vendor, product and price.</span>
        ${productOffset > 0 ? html`<button class="sm" id="catalogueProductsPrevious">Previous</button>` : ""}
        ${productOffset + products.items.length < products.total ? html`<button class="sm" id="catalogueProductsNext">Next 100</button>` : ""}
      </div>` : html`<div class="empty">No approved catalogue records match this search. Approve imported rows to publish them to Vendor search.</div>`}
    </div>
    ${selected ? html`<div class="card"><div class="h"><h2>Review ${selected.filename} · ${selected.company_name}</h2><span class="muted small">${num(selected.pending)} rows pending</span></div>
      <p class="muted small">Approval publishes a row to product search. Any changed price is recorded in history. Missing prices are not inferred.</p>
      ${rows.length ? html`<div class="tw"><table><thead><tr><th>Source</th><th>Product</th><th>SKU</th><th>Price</th><th>Pack</th><th>Unit</th><th>Category</th><th>Match / price check</th><th>Action</th></tr></thead><tbody>
        ${rows.map(row => {
          const record = row.record, match = row.existing_match;
          const conflict = match && !match.ambiguous && match.price !== record.price;
          const matchView = match?.ambiguous ? html`<span class="tag t-bad">Multiple matches</span>` :
            match ? html`<span class="small">${match.product_name} · existing ${match.currency || ""} ${match.price ?? "No price"}${conflict ? " → proposed " + (record.price ?? "No price") : " (same price)"}</span>` :
            html`<span class="muted small">New catalogue row</span>`;
          return html`<tr data-catalogue-row="${row.id}">
            <td>${row.source_sheet || row.source_page || "—"}<br><span class="muted small">row ${row.source_row}</span></td>
            <td><input data-field="product_name" value="${record.product_name || ""}" aria-label="Product name"></td>
            <td><input data-field="sku" value="${record.sku || ""}" aria-label="SKU"></td>
            <td><input data-field="price" type="number" min="0" step="any" value="${record.price ?? ""}" aria-label="Price"></td>
            <td><input data-field="pack_size" value="${record.pack_size || ""}" aria-label="Pack size"></td>
            <td><input data-field="unit" value="${record.unit || ""}" aria-label="Unit"></td>
            <td><input data-field="product_category" value="${record.product_category || ""}" aria-label="Category"></td>
            <td>${matchView}</td><td class="row">
              <button class="sm primary" data-catalogue-approve="${row.id}" ${match?.ambiguous ? "disabled" : ""}>Approve & publish</button>
              <button class="sm danger" data-catalogue-reject="${row.id}">Reject</button>
            </td></tr>`;
        })}
      </tbody></table></div>
      ${selected.pending > offset + rows.length ? html`<button class="sm" id="catalogueMore">Load next 200 pending rows</button>` : ""}
      ` : html`<div class="empty">No pending rows in this batch. Approved, rejected, and duplicate rows are available in the CSV export.</div>`}
    </div>` : ""}
    <div class="card"><h2>Recent approved price history</h2>
      ${history.items.length ? html`<div class="tw"><table><thead><tr><th>Changed</th><th>Vendor</th><th>Product</th><th>SKU</th><th>Previous</th><th>New</th><th>Source</th><th>Row</th></tr></thead><tbody>
        ${history.items.map(item => html`<tr><td>${item.changed_at}</td><td>${item.company_name}</td><td>${item.product_name}</td><td>${item.sku}</td>
          <td>${item.currency} ${item.old_price ?? "—"}</td><td>${item.currency} ${item.new_price ?? "—"}</td><td>${item.source_file}</td><td>${item.source_row}</td></tr>`)}
      </tbody></table></div>` : html`<div class="empty">No approved price changes recorded yet.</div>`}
    </div>`.__raw;

  $("#catalogueUpload")?.addEventListener("click", guard(async event => {
    const file = $("#catalogueFile").files[0], vendorId = $("#catalogueVendor").value;
    if (!file) return toast("Choose an Excel or CSV file first", true);
    event.target.disabled = true;
    const form = new FormData();
    form.append("vendor_id", vendorId);
    form.append("file", file);
    const result = await api("/api/catalogue/imports", { method: "POST", body: form });
    S.catalogueImportId = result.import_id;
    S.catalogueOffset = 0;
    toast(result.duplicate_upload ? "This file was already imported for that vendor" :
      `${result.records_extracted} rows staged for review${result.messages_needing_review ? ` · ${result.messages_needing_review} ambiguous chat messages skipped` : ""}`);
    await PAGE.catalogue();
  }));
  $("#catalogueProductFilter")?.addEventListener("submit", guard(async event => {
    event.preventDefault();
    S.catalogueProductQuery = $("#catalogueProductQuery").value.trim();
    S.catalogueProductVendor = $("#catalogueProductVendor").value;
    S.catalogueProductOffset = 0;
    await PAGE.catalogue();
  }));
  $("#catalogueProductsPrevious")?.addEventListener("click", guard(async () => {
    S.catalogueProductOffset = Math.max(0, productOffset - 100);
    await PAGE.catalogue();
  }));
  $("#catalogueProductsNext")?.addEventListener("click", guard(async () => {
    S.catalogueProductOffset = productOffset + 100;
    await PAGE.catalogue();
  }));
  $$("[data-review-import]").forEach(button => button.addEventListener("click", () => {
    S.catalogueImportId = Number(button.dataset.reviewImport);
    S.catalogueOffset = 0;
    PAGE.catalogue();
  }));
  $("#catalogueMore")?.addEventListener("click", guard(async () => {
    S.catalogueOffset = (S.catalogueOffset || 0) + 200;
    await PAGE.catalogue();
  }));
  $$("[data-catalogue-approve]").forEach(button => button.addEventListener("click", guard(async () => {
    const row = button.closest("[data-catalogue-row]"), rowId = Number(row.dataset.catalogueRow);
    const initial = rows.find(item => item.id === rowId), changes = {};
    for (const field of ["product_name", "sku", "price", "pack_size", "unit", "product_category"]) {
      const value = $(`[data-field="${field}"]`, row).value.trim();
      const previous = initial.record[field] == null ? "" : String(initial.record[field]);
      if (value !== previous) changes[field] = field === "price" && value === "" ? null : value;
    }
    if (Object.keys(changes).length) await api(`/api/catalogue/import-rows/${rowId}`, { method: "PATCH", body: { changes } });
    const result = await post(`/api/catalogue/import-rows/${rowId}/decide`, { action: "approve" });
    toast(result.decision);
    await PAGE.catalogue();
  })));
  $$("[data-catalogue-reject]").forEach(button => button.addEventListener("click", guard(async () => {
    const result = await post(`/api/catalogue/import-rows/${button.dataset.catalogueReject}/decide`, { action: "reject" });
    toast(result.decision);
    await PAGE.catalogue();
  })));
};

// ---------------------------------------------------------------- settings
PAGE.settings = async () => {
  const c = S.cfg, t = c.thresholds, tools = S.status.tools, st = S.status.settings;
  const chk = (id, label, val, hint = "") => html`<label class="row"><input type="checkbox" id="${id}" ${val ? raw("checked") : ""}><span><strong>${label}</strong>${hint ? html`<br><span class="muted small">${hint}</span>` : ""}</span></label>`;
  $("#view").innerHTML = html`<div class="grid g2"><div class="card stack"><h2>Matching</h2>
      <label class="f">Entity type<select id="etype">${["hospital", "company", "person", "generic"].map(x => html`<option ${c.entity_type === x ? raw("selected") : ""}>${x}</option>`)}</select><small>Controls which words count as “generic” (hospital, limited…) versus distinctive.</small></label>
      <div class="grid g2"><label class="f">Very high ≥<input type="number" id="t_vh" value="${t.very_high}" min="0" max="100"></label><label class="f">High ≥<input type="number" id="t_h" value="${t.high}" min="0" max="100"></label>
      <label class="f">Possible ≥<input type="number" id="t_p" value="${t.possible}" min="0" max="100"></label><label class="f">Manual review ≥<input type="number" id="t_r" value="${t.review}" min="0" max="100"></label></div>
      <label class="f">Auto-merge minimum score<input type="number" id="amin" value="${c.auto_match_min_score}" min="0" max="100"><small>A pair is merged automatically only at or above this score AND with supporting evidence. Name similarity alone never merges.</small></label>
      ${chk("automatic", "Automatic matching", c.automatic_matching, "Off = only mappings you confirmed are applied automatically; everything else goes to review.")}
      ${chk("manualrev", "Send uncertain matches to manual review", c.manual_review_required, "Off = uncertain pairs stay separate and are only noted in the reason column.")}
      ${chk("dups", "Duplicate detection", c.duplicate_detection)}</div>
    <div class="stack"><div class="card stack"><h2>Reading files</h2>${chk("ocr", "OCR for scanned PDFs and images", c.ocr_enabled, tools.tesseract ? "Tesseract found." : "Tesseract is NOT installed - scanned files will be reported, not read.")}
      ${chk("hidden", "Read hidden Excel sheets", c.include_hidden_sheets)}<label class="f">Dry-run sample size<input type="number" id="dsz" value="${c.dry_run_sample_size}" min="10" max="5000"></label>
      <div class="small muted">Legacy .doc needs LibreOffice: ${tools.libreoffice ? "found" : "not found (save as .docx instead)"}.</div></div>
      <div class="card stack"><h2>Reset</h2><div class="row"><button class="danger" id="rs1">Clear matching results</button><button class="danger" id="rs2">Clear everything</button></div><small class="muted">“Matching results” keeps the extracted raw data and your decisions. “Everything” starts from scratch (source files are never touched).</small></div></div></div>
    <div class="row"><button class="primary" id="saveCfg">Save settings</button></div>`.__raw;
  $("#saveCfg").addEventListener("click", guard(async () => {
    await api("/api/config", { method: "PUT", body: { entity_type: $("#etype").value, thresholds: { very_high: +$("#t_vh").value, high: +$("#t_h").value, possible: +$("#t_p").value, review: +$("#t_r").value },
      auto_match_min_score: +$("#amin").value, automatic_matching: $("#automatic").checked, manual_review_required: $("#manualrev").checked, duplicate_detection: $("#dups").checked,
      ocr_enabled: $("#ocr").checked, include_hidden_sheets: $("#hidden").checked, dry_run_sample_size: +$("#dsz").value } });
    toast("Settings saved - they apply to the next run"); await refreshStatus();
  }));
  $("#rs1").addEventListener("click", guard(async () => { if (await confirmBox("Clear matching results", "Master entities, matches and review items will be rebuilt on the next run. Raw data and your saved decisions are kept.", "Clear", true)) { await post("/api/reset", { scope: "derived" }); toast("Cleared"); route(); } }));
  $("#rs2").addEventListener("click", guard(async () => { if (await confirmBox("Clear everything", "All extracted data, matches and saved decisions are removed. Your original files are not touched.", "Clear everything", true)) { await post("/api/reset", { scope: "all" }); toast("Cleared"); route(); } }));
};

// ---------------------------------------------------------------- logs
PAGE.logs = async () => {
  const name = S.logName || "application";
  $("#view").innerHTML = html`<div class="card"><div class="row" style="margin-bottom:10px"><div class="seg">${["application", "extraction", "matching", "errors", "audit"].map(n => html`<button data-l="${n}" class="${n === name ? "on" : ""}">${n}</button>`)}</div><button class="sm" id="lr">Refresh</button></div><pre class="log" id="logbox">Loading…</pre></div>`.__raw;
  $$("[data-l]").forEach(b => b.addEventListener("click", () => { S.logName = b.dataset.l; PAGE.logs(); }));
  const load = guard(async () => { const r = await api(`/api/logs/${name}?lines=400`); const b = $("#logbox"); b.textContent = r.lines.join("\n") || "(empty)"; b.scrollTop = b.scrollHeight; });
  $("#lr").addEventListener("click", load); await load();
};

route();
