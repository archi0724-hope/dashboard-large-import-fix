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
  export: "M12 3v12m0 0l-4-4m4 4l4-4M4 21h16", settings: "M12 15a3 3 0 100-6 3 3 0 000 6zM19.4 15a1.7 1.7 0 00.3 1.8l.1.1a2 2 0 11-2.8 2.8l-.1-.1a1.7 1.7 0 00-1.8-.3 1.7 1.7 0 00-1 1.5V21a2 2 0 11-4 0v-.1a1.7 1.7 0 00-1.1-1.5 1.7 1.7 0 00-1.8.3l-.1.1a2 2 0 11-2.8-2.8l.1-.1a1.7 1.7 0 00.3-1.8 1.7 1.7 0 00-1.5-1H3a2 2 0 110-4h.1a1.7 1.7 0 001.5-1.1 1.7 1.7 0 00-.3-1.8l-.1-.1a2 2 0 112.8-2.8l.1.1a1.7 1.7 0 001.8.3H9a1.7 1.7 0 001-1.5V3a2 2 0 114 0v.1a1.7 1.7 0 001 1.5 1.7 1.7 0 001.8-.3l.1-.1a2 2 0 112.8 2.8l-.1.1a1.7 1.7 0 00-.3 1.8V9a1.7 1.7 0 001.5 1H21a2 2 0 110 4h-.1a1.7 1.7 0 00-1.5 1z",
  logs: "M4 6h16M4 10h16M4 14h10M4 18h7",
};
const icon = k => html`<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="${ICON[k]}"/></svg>`;

// ---------------------------------------------------------------- state / routing
const S = { status: null, cfg: null, charts: [], poll: null, page: "overview", workspace: localStorage.getItem("workspace") || "organizer", review: { items: [], total: 0, cur: null, q: "", detail: null } };
const PAGES = [
  ["overview", "Dashboard"], ["source", "Import files"], ["preview", "Check & match"], ["review", "Review matches"],
  ["entities", "Companies"], ["quality", "Data quality"], ["export", "Export data"], ["settings", "Workspace settings"], ["logs", "Activity log"],
];
const VENDOR_PAGES = [["vendor", "Vendor dashboard"]];

function renderNav() {
  const pend = S.status?.totals?.pending_reviews || 0;
  const pages = S.workspace === "vendor" ? VENDOR_PAGES : PAGES;
  $("#nav").innerHTML = pages.map(([k, l]) => html`<a href="#/${k}" class="${S.page === k ? "on" : ""}" ${S.page === k ? raw('aria-current="page"') : ""}>${icon(k === "vendor" ? "overview" : k)}<span>${l}</span><span class="sp"></span>${k === "review" && pend ? html`<span class="badge-n">${pend}</span>` : ""}</a>`).map(x => x.__raw).join("");
  const p = S.status?.progress;
  const pill = $("#runpill");
  if (p?.running) { pill.className = "pill run"; pill.textContent = `Running ${p.percent}%`; }
  else if (p?.error) { pill.className = "pill err"; pill.textContent = "Stopped"; }
  else { pill.className = "pill idle"; pill.textContent = "Idle"; }
  const c = S.cfg;
  $("#srclabel").textContent = c ? (c.source_kind === "drive" ? "Source: Google Drive" : "Source: " + (c.local_input_dir ? c.local_input_dir.split("/").slice(-2).join("/") : "not set")) : "";
  $$('[data-workspace]').forEach(button => button.classList.toggle("on", button.dataset.workspace === S.workspace));
  document.body.classList.toggle("vendor-full", S.workspace === "vendor");
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
  const pages = S.workspace === "vendor" ? VENDOR_PAGES : PAGES;
  S.page = pages.some(p => p[0] === k) ? k : pages[0][0];
  killCharts();
  $("#title").textContent = (S.workspace === "vendor" ? "Vendor workspace · " : "Organizer workspace · ") + pages.find(p => p[0] === S.page)[1];
  $("#topactions").innerHTML = "";
  await guard(refreshStatus)();
  if (S.workspace === "vendor") {
    $("#view").innerHTML = `<iframe class="vendor-frame" title="Vendor dashboard" src="/vendor/"></iframe>`;
    $("#view").focus({ preventScroll: true });
    return;
  }
  $("#view").innerHTML = `<div class="empty">Loading...</div>`;
  await guard(PAGE[S.page])();
  $("#view").focus({ preventScroll: true });
}
window.addEventListener("hashchange", route);
$$('[data-workspace]').forEach(button => button.addEventListener("click", () => {
  S.workspace = button.dataset.workspace;
  localStorage.setItem("workspace", S.workspace);
  location.hash = S.workspace === "vendor" ? "#/vendor" : "#/overview";
  route();
}));

// ---------------------------------------------------------------- overview
const STEPS = [["source", "Choose source"], ["source", "Scan files"], ["preview", "Preview"], ["overview", "Run"], ["review", "Review"], ["export", "Export"]];

const PAGE = {};
PAGE.overview = async () => {
  const st = S.status, t = st.totals, p = st.progress;
  const sum = await api("/api/summary");
  const srcSet = st.config.source_kind === "drive" ? !!st.config.drive_folder : !!st.config.local_input_dir;
  const done = [srcSet, t.files > 0, t.records > 0, t.records > 0, t.records > 0 && t.pending_reviews === 0, false];
  const next = done.findIndex(d => !d);
  const steps = STEPS.map(([pg, l], i) => html`<a class="step ${done[i] ? "done" : i === next ? "next" : ""}" href="#/${pg}"><i>${done[i] ? "✓" : i + 1}</i>${l}</a>`);
  const auto = (sum.status.find(s => s.label === "auto_matched")?.n || 0) + (sum.status.find(s => s.label === "verified_mapping")?.n || 0);
  const matched = sum.status.filter(s => ["auto_matched", "verified_mapping", "needs_review", "user_accepted"].includes(s.label)).reduce((a, b) => a + b.n, 0);
  const kpi = (v, l, cls = "") => html`<div class="card kpi ${cls}"><div class="v">${v}</div><div class="l">${l}</div></div>`;
  $("#view").innerHTML = html`
    <div class="steps" aria-label="Workflow">${steps}</div>
    <div class="grid g6" style="margin-bottom:16px">
      ${kpi(num(t.files_processed) + " / " + num(t.files), "Files processed")}${kpi(num(t.records), "Records extracted")}${kpi(num(t.entities), "Master entities")}
      ${kpi(pct(auto, matched), "Matched automatically")}${kpi(num(t.pending_reviews), "Waiting for review", t.pending_reviews ? "warn" : "")}${kpi(num(t.errors), "Processing errors", t.errors ? "bad" : "")}
    </div>
    <div class="card"><div class="h"><h2>Pipeline</h2><div class="row">
      <button class="primary" id="btnRun" ${p.running ? "disabled" : ""}>${t.records ? "Resume / process new files" : "Start processing"}</button>
      <button id="btnStop" ${p.running ? "" : "disabled"}>Stop</button><button class="danger" id="btnReset" ${p.running ? "disabled" : ""}>Reset dashboard data</button></div></div>
      <div id="stages">${raw(stagesHtml(p))}</div></div>
    ${t.records ? html`<div class="grid g2">
      <div class="card"><h2>Match confidence</h2><div class="chart"><canvas id="c1" aria-label="Records by confidence band"></canvas></div></div>
      <div class="card"><h2>Outcome per record</h2><div class="chart"><canvas id="c2" aria-label="Records by outcome"></canvas></div></div>
      <div class="card"><h2>Master entities by state</h2><div class="chart"><canvas id="c3" aria-label="Entities by state"></canvas></div></div>
      <div class="card"><h2>Most common data-quality findings</h2>${sum.quality.length ? html`<table><tbody>${sum.quality.map(q => html`<tr><td>${q.label.replace(/_/g, " ")}</td><td>${sevTag(q.severity)}</td><td class="num">${num(q.n)}</td></tr>`)}</tbody></table>` : html`<p class="muted">Nothing flagged.</p>`}</div>
    </div>` : html`<div class="card empty"><h3>No data processed yet</h3><p>Point the app at a folder (or Google Drive), scan it, preview a sample, then start processing.<br>New here? Try the built-in synthetic demo files first.</p>
      <div class="row" style="justify-content:center"><button class="primary" id="btnDemo">Use demo data</button><a class="btn" href="#/source">Choose my own source</a></div></div>`}`.__raw;
  $("#btnRun")?.addEventListener("click", guard(async event => {
    // Disable immediately: status polling has not yet observed the new run,
    // so leaving this enabled allowed repeat clicks to show a confusing
    // "run already in progress" error.
    event.currentTarget.disabled = true;
    event.currentTarget.textContent = "Starting…";
    await post("/api/run");
    toast("Started");
    // Re-render right away with the running state instead of waiting for the
    // first polling cycle, which keeps the cards, Stop button, and stages in sync.
    await route();
  }));
  $("#btnStop")?.addEventListener("click", guard(async () => { await post("/api/stop"); toast("Stopping after the current batch - progress is saved"); }));
  $("#btnReset")?.addEventListener("click", guard(async () => {
    if (await confirmBox("Reset dashboard data", "This clears all imported dashboard data, generated matches, review items, and saved decisions. Your original files are not deleted.", "Reset dashboard", true)) {
      await post("/api/reset", { scope: "all" });
      toast("Dashboard data reset");
      route();
    }
  }));
  $("#btnDemo")?.addEventListener("click", guard(async () => { await post("/api/demo/prepare"); toast("Demo files created and selected"); await post("/api/run"); startPolling(); route(); }));
  if (t.records) {
    const col = { "Very High": "#1b7f4b", High: "#0b7a75", "Possible Match": "#d8963a", "Manual Review": "#c2570c", "Probably Different": "#b42318", "n/a": "#8b93a3" };
    S.charts.push(new Chart($("#c1"), { type: "bar", data: { labels: sum.bands.map(b => b.label), datasets: [{ data: sum.bands.map(b => b.n), backgroundColor: sum.bands.map(b => col[b.label] || "#4d5b73"), borderRadius: 4 }] }, options: chartOpts() }));
    S.charts.push(new Chart($("#c2"), { type: "doughnut", data: { labels: sum.status.map(s => (STATUS[s.label] || [s.label])[0]), datasets: [{ data: sum.status.map(s => s.n), backgroundColor: sum.status.map(s => STATUS_COL[s.label] || "#8b93a3") }] }, options: { ...chartOpts(false), cutout: "58%", plugins: { legend: { position: "right", labels: { boxWidth: 12, font: { family: "IBM Plex Sans" } } } } } }));
    S.charts.push(new Chart($("#c3"), { type: "bar", data: { labels: sum.by_state.map(b => b.label), datasets: [{ data: sum.by_state.map(b => b.n), backgroundColor: "#0b7a75", borderRadius: 4 }] }, options: { ...chartOpts(), indexAxis: "y" } }));
  }
  if (p.running) startPolling();
};
function chartOpts(scales = true) {
  Chart.defaults.font.family = "'IBM Plex Sans', system-ui, sans-serif";
  const o = { responsive: true, maintainAspectRatio: false, plugins: { legend: { display: false } } };
  if (scales) o.scales = { x: { grid: { display: false }, ticks: { precision: 0 } }, y: { beginAtZero: true, ticks: { precision: 0 }, grid: { color: "#ecebe3" } } };
  return o;
}
function stagesHtml(p) {
  if (!p.stages.length) return `<p class="muted">Idle. The pipeline is resumable: stopping (or a crash) never loses finished work, and new files are picked up on the next run.</p>`;
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
  const tab = S.srcTab || (mode === "drive" ? "drive" : "local");
  $("#view").innerHTML = html`
    <div class="card"><div class="h"><h2>Where are your files?</h2>
      <div class="seg" role="tablist">${[["local", "Local folder"], ["drive", "Google Drive"], ["upload", "Upload files"], ["demo", "Demo data"]].map(([k, l]) => html`<button role="tab" data-tab="${k}" class="${tab === k ? "on" : ""}">${l}</button>`)}</div></div>
      <div id="srcbody">${tab === "local" ? html`<div class="stack"><label class="f">Input folder (read-only - sub-folders are included)
        <input id="inpath" value="${cfg.source_kind === "local" ? cfg.local_input_dir : ""}" placeholder="/path/to/your/files" style="width:100%"></label>
        <div class="row"><button class="primary" id="saveSrc">Use this folder</button><span class="muted small">Your files are never modified. Results go to the output folder (${cfg.output_dir || "data/output"}).</span></div></div>`
      : tab === "drive" ? html`<div class="stack"><label class="f">Google Drive folder URL or ID<input id="drvurl" value="${cfg.drive_folder}" placeholder="https://drive.google.com/drive/folders/..." style="width:100%"></label>
        <div class="note ${ds.ready_to_connect ? "" : "warn"}">${ds.ready_to_connect ? "Credentials found - the app can read this folder (read-only scope)." : "Not authorised yet. Run  python -m app drive-auth  once in a terminal (needs secrets/client_secret.json), or configure a service account. Full steps are in the README."}</div>
        <div class="row"><button class="primary" id="saveDrv">Use this Drive folder</button></div></div>`
      : tab === "upload" ? html`<div class="stack"><input type="file" id="upfiles" multiple><div class="row"><button class="primary" id="doUp">Upload &amp; use</button><span class="muted small">Upload one vendor ZIP or files. ZIP company folders are kept together. Max 10 GB each.</span></div></div>`
      : html`<div class="stack"><p class="muted">Creates ~11 synthetic files (Excel, legacy .xls, CSV, PDF, Word, text, a scanned image, a corrupt file, a duplicate…) with deliberately messy names such as <em>SMS HOSPITAL JAIPUR</em> / <em>S.M.S. Hospital</em> / <em>Sawai Man Singh Hospital</em>.</p><div class="row"><button class="primary" id="mkDemo">Create demo files &amp; use them</button></div></div>`}</div></div>
    <div class="card"><div class="h"><h2>Files</h2><div class="row"><button id="btnScan">Scan folder</button><button id="btnInspect">Inspect structure</button><button class="primary" id="btnOrganize">Organize into company folders</button></div></div>
      <h3>Processable files <span class="muted small">${num(processable.length)}</span></h3>
      ${fileTable(processable, "No processable files listed yet. Choose a source above, then press Scan folder.")}
      ${others.length ? html`<details style="margin-top:18px"><summary><strong>Others</strong> <span class="muted small">${num(others.length)} unsupported files</span></summary>${fileTable(others, "No unsupported files.")}</details>` : ""}</div>
    <div id="inspectbox"></div>`.__raw;
  $$("[data-tab]").forEach(b => b.addEventListener("click", () => { S.srcTab = b.dataset.tab; PAGE.source(); }));
  $("#saveSrc")?.addEventListener("click", guard(async () => { await api("/api/config", { method: "PUT", body: { source_kind: "local", local_input_dir: $("#inpath").value.trim() } }); toast("Source saved"); route(); }));
  $("#saveDrv")?.addEventListener("click", guard(async () => { await api("/api/config", { method: "PUT", body: { source_kind: "drive", drive_folder: $("#drvurl").value.trim() } }); toast("Drive folder saved"); route(); }));
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
  $("#btnOrganize").addEventListener("click", guard(async e => { e.target.disabled = true; e.target.textContent = "Organizing…"; try { const r = await post("/api/organize-files"); toast(`${r.files_copied} files copied into ${r.company_folders} company folders; ${r.work_related} saved as Work Related Data; ${r.skipped} unavailable`); } finally { e.target.disabled = false; e.target.textContent = "Organize into company folders"; } }));
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
  $("#ren").onclick = guard(async () => { const n = prompt("Standard name:", e.standard_name); if (n && n.trim()) { await post(`/api/entities/${id}/rename`, { standard_name: n.trim() }); toast("Renamed"); loadEntities(); openEntity(id); } });
  $("#mrg").onclick = guard(async () => {
    const q = prompt("Search for the entity to merge INTO (name or id):"); if (!q) return;
    const r = await api(`/api/entities?search=${encodeURIComponent(q)}&limit=8`);
    const opts = r.items.filter(x => x.master_entity_id !== id); if (!opts.length) return toast("No other entity matches", true);
    const pick = prompt("Merge into which one? Type the number:\n" + opts.map((x, i) => `${i + 1}. ${x.standard_name} (${x.city || "-"}) ${x.master_entity_id}`).join("\n"));
    const t = opts[(+pick || 0) - 1]; if (!t) return;
    if (await confirmBox("Merge entities", `Merge “${e.standard_name}” into “${t.standard_name}”? All its names and records move to the target.`, "Merge", true)) {
      await post("/api/entities/merge", { source_id: id, target_id: t.master_entity_id }); toast("Merged"); dr.hidden = true; loadEntities();
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
