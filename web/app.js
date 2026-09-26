/* Velloe Ops Intelligence - dashboard (vanilla JS, no build step).
   Part 1: helpers, API, shell (sidebar / top bar), modal, toasts, router. */
"use strict";

// ---------------------------------------------------------------- helpers
const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
const ESC = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
const h = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ESC[c]); // escape everything rendered
const view = () => $("#view");

async function api(path, opts = {}) {
  const res = await fetch(path, {
    method: opts.method || "GET",
    headers: opts.body ? { "Content-Type": "application/json" } : {},
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  let data = null;
  try { data = await res.json(); } catch (_) { /* non-JSON */ }
  if (!res.ok) throw new Error((data && data.error) || `HTTP ${res.status}`);
  return data;
}

function ago(iso) {
  if (!iso) return "–";
  const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (s < 60) return `${Math.floor(s)}s ago`;
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}
const clock = (iso) => (iso ? new Date(iso).toLocaleTimeString([], { hour12: false }) : "–");

const PRIORITY = { Critical: "b-err", High: "b-warn", Medium: "b-blue", Low: "" };
const prio = (p) => (p ? `<span class="badge ${PRIORITY[p] ?? ""}">${h(p).toUpperCase()}</span>` : `<span class="muted">–</span>`);
const STATUS = {
  Detected: "", Investigating: "b-blue", "Awaiting Approval": "b-warn", Approved: "b-ok", Completed: "b-ok",
  "Needs Review": "b-err", Stopped: "b-err", Error: "b-err",
};
const status = (s) => `<span class="badge ${STATUS[s] ?? ""}">${h(s)}</span>`;
const AUDIT_STATUS = { ok: "b-ok", warn: "b-warn", recovered: "b-blue", rejected: "b-warn", error: "b-err", stopped: "b-err" };
const auditStatus = (s) => `<span class="badge ${AUDIT_STATUS[s] ?? ""}">${h(s)}</span>`;
const refs = (list) => (list && list.length ? `<span class="refs">${list.map((r) => `<span class="ref">${h(r)}</span>`).join("")}</span>` : "");

function confBar(v) {
  if (v === null || v === undefined) return `<span class="muted">–</span>`;
  const cls = v >= 70 ? "ok" : v >= 40 ? "warn" : "err";
  return `<div style="display:flex;align-items:center;gap:8px"><div class="bar ${cls}" style="width:70px"><i style="width:${Math.max(3, v)}%"></i></div><span class="small">${h(v)}%</span></div>`;
}

const loading = (msg) => `<div class="card"><div class="state"><span class="spinner"></span> ${h(msg)}</div></div>`;
const empty = (title, msg, btn = "") => `<div class="state"><strong>${h(title)}</strong>${h(msg)}${btn ? `<div style="margin-top:12px">${btn}</div>` : ""}</div>`;
function errorState(msg, retryFn) {
  view().innerHTML = `<div class="card"><div class="state"><strong>Unable to load this page.</strong>${h(msg)}
    <div style="margin-top:12px"><button class="btn" id="retry-btn">Retry</button></div></div></div>`;
  $("#retry-btn").onclick = retryFn;
}

function toast(msg) {
  const el = document.createElement("div");
  el.className = "toast";
  el.textContent = msg;
  $("#toasts").appendChild(el);
  setTimeout(() => el.remove(), 4200);
}

function modal(inner, onMount) {
  const ov = document.createElement("div");
  ov.className = "overlay";
  ov.innerHTML = `<div class="modal" role="dialog" aria-modal="true">${inner}</div>`;
  const close = () => { ov.remove(); document.removeEventListener("keydown", esc); };
  const esc = (e) => { if (e.key === "Escape") close(); };
  ov.addEventListener("click", (e) => { if (e.target === ov) close(); });
  document.addEventListener("keydown", esc);
  document.body.appendChild(ov);
  onMount && onMount(ov, close);
  const first = $("textarea, input, button", ov);
  first && first.focus();
  return close;
}

// ---------------------------------------------------------------- shell
const ICONS = {
  overview: '<path d="M3 13h8V3H3zM13 21h8V11h-8zM3 21h8v-6H3zM13 3v6h8V3z"/>',
  investigations: '<circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/>',
  board: '<rect x="3" y="4" width="5" height="16" rx="1"/><rect x="10" y="4" width="5" height="10" rx="1"/><rect x="17" y="4" width="4" height="13" rx="1"/>',
  incidents: '<path d="M12 3 2 20h20L12 3z"/><path d="M12 10v4M12 17h.01"/>',
  agents: '<circle cx="9" cy="8" r="3"/><circle cx="17" cy="9" r="2.5"/><path d="M3 20c0-3.3 2.7-6 6-6s6 2.7 6 6M15 20c0-2 1-4 3-4.5 2 .5 3 2.5 3 4.5"/>',
  audit: '<path d="M8 4h11v16H5V7z"/><path d="M8 4v3H5M9 12h7M9 16h5"/>',
  analytics: '<path d="M4 20V10M10 20V4M16 20v-7M22 20H2"/>',
  settings: '<circle cx="12" cy="12" r="3"/><path d="M19 12a7 7 0 0 0-.1-1.2l2-1.6-2-3.4-2.4 1a7 7 0 0 0-2-1.2L14 3h-4l-.5 2.6a7 7 0 0 0-2 1.2l-2.4-1-2 3.4 2 1.6A7 7 0 0 0 5 12c0 .4 0 .8.1 1.2l-2 1.6 2 3.4 2.4-1c.6.5 1.3.9 2 1.2L10 21h4l.5-2.6c.7-.3 1.4-.7 2-1.2l2.4 1 2-3.4-2-1.6c.1-.4.1-.8.1-1.2z"/>',
};
const NAV = [
  ["overview", "Overview"], ["investigations", "Investigations"], ["board", "Action Board"], ["incidents", "Incidents"],
  ["agents", "Agents"], ["audit", "Audit Trail"], ["analytics", "Analytics"], ["settings", "Settings"],
];
const state = { overview: null, route: "overview", timer: null };

function renderNav(active) {
  const pending = state.overview ? state.overview.kpis.pending_approval : 0;
  $("#nav").innerHTML = NAV.map(([k, label]) => `<a href="#/${k}" class="${k === active ? "active" : ""}" ${k === active ? 'aria-current="page"' : ""}>
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${ICONS[k]}</svg>
    ${label}${k === "board" && pending ? `<span class="count" title="Awaiting approval">${pending}</span>` : ""}</a>`).join("");
}

function dotFor(s) { return s === "Operational" || s === "Healthy" ? "" : s === "Running" ? "live" : s === "Recovered" ? "warn" : s === "Idle" ? "idle" : "err"; }

function renderShell() {
  const o = state.overview;
  if (!o) return;
  const overall = o.health.overall;
  $("#side-status").innerHTML = `<div class="muted small">System Status</div>
    <div style="display:flex;align-items:center;gap:6px;margin-top:4px;font-weight:600"><span class="dot ${dotFor(overall)}"></span>${h(overall)}</div>
    <div class="muted small" style="margin-top:6px">LLM: ${h(o.provider)}</div>`;
  $("#top-status").innerHTML = `<span class="dot ${dotFor(overall)}"></span> System Status: <b>${h(overall)}</b>`;
  const pct = o.budget && o.budget.budget_used_pct != null ? o.budget.budget_used_pct : null;
  $("#top-budget").innerHTML = `Budget <div class="meter"><i style="width:${pct ?? 0}%"></i></div> <span>${pct == null ? "–" : pct + "%"}</span>`;
  const running = o.running_executions || o.investigations.filter((i) => i.running);
  $("#top-activity").innerHTML = running.length
    ? `<span class="dot live"></span> ${running.length} execution${running.length > 1 ? "s" : ""} running`
    : `<span class="dot idle"></span> Agents idle`;
  renderNav(state.route);
}

async function refreshShell() {
  try { state.overview = await api("/api/overview"); renderShell(); } catch (_) {
    $("#top-status").innerHTML = `<span class="dot err"></span> Server unreachable`;
  }
}

// ---------------------------------------------------------------- new investigation (guided use-case picker)
// Each use case composes a question the Strategist can route to the right domain (keywords matter), plus the
// options relevant to that use case. Everything resolves to the existing /api/investigations pipeline.
const USE_CASES = [
  { id: "infra", icon: "🌡️", title: "Infrastructure Incident", domain: "thermal",
    desc: "Correlate cooling, HVAC and server alerts to find a likely cause.",
    q: (s) => `Why did temperature increase at ${s}? Investigate the cooling and HVAC anomaly.`,
    options: ["site"] },
  { id: "power", icon: "⚡", title: "Power Anomaly", domain: "power",
    desc: "Find recurring power patterns and the affected equipment.",
    q: (s) => `Why are repeated power fluctuations happening at ${s}, and what should we do?`,
    options: ["site"] },
  { id: "it", icon: "🖥️", title: "IT / Network Incident", domain: "network",
    desc: "Correlate server events, network alerts and timestamps.",
    q: (s) => `Why are servers and the network repeatedly becoming unavailable at ${s}? Investigate the network outages.`,
    options: ["site"] },
  { id: "cooling", icon: "❄️", title: "Cooling Optimization", domain: "thermal",
    desc: "Analyze temperature and cooling events for abnormal patterns.",
    q: (s) => `Why is cooling performance dropping at ${s}? Analyze the temperature and thermal events.`,
    options: ["site"] },
  { id: "maint", icon: "🔧", title: "Maintenance Intelligence", domain: "change",
    desc: "Analyze incident and maintenance history to prioritize attention.",
    q: (s) => `Which equipment needs attention at ${s}? Review the maintenance and change history for recurring issues.`,
    options: ["site"] },
  { id: "report", icon: "📋", title: "Incident Report", domain: "general",
    desc: "Collect events, verify evidence and produce an executive summary.",
    q: (s) => `Create an incident report for ${s}: collect recent incidents, analyze them and summarize.`,
    options: ["site"] },
  { id: "warning", icon: "🚨", title: "Early Warning Scan", domain: "general",
    desc: "Scan available events for anything unusual and open tasks.",
    q: (s) => `Is anything unusual happening at ${s}? Scan recent incidents and telemetry for anomalies.`,
    options: ["site"] },
  { id: "risk", icon: "📊", title: "Operational Risk", domain: "general",
    desc: "Find repeated patterns across incidents and highlight risks.",
    q: (s, areas) => `Are there recurring operational risks at ${s}${areas}? Analyze recent incidents for repeated patterns.`,
    options: ["site", "areas"] },
  { id: "opportunity", icon: "💼", title: "Business Opportunity", domain: "general",
    desc: "Find recurring manual work that could be automated.",
    q: (s, areas) => `What operational problems at ${s}${areas} could we automate? Find recurring issues and automation opportunities.`,
    options: ["site", "areas"] },
];
// Scan areas map to phrases the engine already understands (domain keywords / incident categories).
const SCAN_AREAS = [
  ["power", "Power & UPS"], ["cooling", "Cooling & thermal"], ["network", "Network"],
  ["maintenance", "Maintenance history"], ["reliability", "Tool / data reliability"],
];

function newInvestigation(prefillOrCase = "") {
  const failures = (state.overview && state.overview.failures) || { normal: "Normal" };
  const sites = [{ code: "Site A", label: "Site A" }, { code: "Site B", label: "Site B" }];
  // Allow deep-linking a specific use case by id (e.g. from a "Find automation opportunities" button).
  const preset = USE_CASES.find((u) => u.id === prefillOrCase);
  const prefillText = preset ? "" : prefillOrCase;
  const state_ = { selected: preset || null };

  const failureBlock = `<label>Simulation mode <span class="muted small" style="font-weight:400">(developer / demo control)</span></label>
      <div class="opts">${Object.entries(failures).map(([k, v], i) => `<label class="opt"><input type="radio" name="failure" value="${h(k)}" ${i === 0 ? "checked" : ""}> ${h(v)}</label>`).join("")}</div>
      <p class="muted small" style="margin:8px 0 0">Failures are injected into the first data source of the plan. The Scout retries, then switches to the fallback source.</p>`;

  const casesGrid = `<div class="uc-grid">${USE_CASES.map((u) => `<button type="button" class="uc-card" data-uc="${u.id}">
      <span class="uc-ico" aria-hidden="true">${u.icon}</span><span class="uc-t">${h(u.title)}</span><span class="uc-d">${h(u.desc)}</span></button>`).join("")}</div>`;

  const optionsForm = (uc) => {
    const siteOpt = uc.options.includes("site") ? `<label for="uc-site">Which site?</label>
      <select id="uc-site" class="input">${sites.map((s) => `<option value="${h(s.code)}">${h(s.label)}</option>`).join("")}<option value="all">All sites</option></select>` : "";
    const areasOpt = uc.options.includes("areas") ? `<label>Which areas do you need? <span class="muted small" style="font-weight:400">(pick the data areas to scan)</span></label>
      <div class="opts">${SCAN_AREAS.map((a, i) => `<label class="opt"><input type="checkbox" name="area" value="${h(a[0])}" ${i < 2 ? "checked" : ""}> ${h(a[1])}</label>`).join("")}</div>` : "";
    return `<div class="uc-chosen"><span class="uc-ico" aria-hidden="true">${uc.icon}</span><div><div class="uc-t">${h(uc.title)}</div><div class="uc-d">${h(uc.desc)}</div></div>
        <button type="button" class="btn sm" id="uc-back">Change</button></div>
      ${siteOpt}${areasOpt}
      <label for="uc-preview">Question sent to the agents</label>
      <textarea id="uc-preview" maxlength="500"></textarea>
      <div class="muted small">Edit this freely if you want. It routes to the <b>${h(uc.domain)}</b> analysis path.</div>
      ${failureBlock}`;
  };

  const render = (ov, close) => {
    const body = $("#uc-body", ov);
    const foot = $("#uc-foot", ov);
    if (!state_.selected) {
      body.innerHTML = `<p class="muted small" style="margin:0 0 12px">Pick a use case, choose a few options, and Kiro runs Strategist → Scout → Intelligence → Guardian for you.</p>
        ${casesGrid}
        ${prefillText ? `<label for="uc-preview" style="margin-top:14px">Or type your own</label><textarea id="uc-preview" maxlength="500">${h(prefillText)}</textarea>${failureBlock}` : ""}`;
      foot.innerHTML = `<button class="btn" data-close>Cancel</button>${prefillText ? '<button class="btn primary" id="uc-start">Start investigation</button>' : ""}`;
      $$("[data-uc]", ov).forEach((b) => (b.onclick = () => { state_.selected = USE_CASES.find((u) => u.id === b.dataset.uc); render(ov, close); }));
    } else {
      const uc = state_.selected;
      body.innerHTML = optionsForm(uc);
      foot.innerHTML = `<button class="btn" data-close>Cancel</button><button class="btn primary" id="uc-start">Start investigation</button>`;
      const sync = () => { $("#uc-preview", ov).value = compose(ov, uc); };
      const back = $("#uc-back", ov); if (back) back.onclick = () => { state_.selected = null; render(ov, close); };
      $$("#uc-site, input[name=area]", ov).forEach((el) => (el.onchange = sync));
      sync();
    }
    $("[data-close]", ov).onclick = close;
    const start = $("#uc-start", ov);
    if (start) start.onclick = () => submit(ov, close);
  };

  const compose = (ov, uc) => {
    const siteEl = $("#uc-site", ov);
    const site = !siteEl || siteEl.value === "all" ? "our sites" : siteEl.value;
    let areas = "";
    if (uc.options.includes("areas")) {
      const picked = $$("input[name=area]:checked", ov).map((c) => c.value);
      const labels = picked.map((p) => (SCAN_AREAS.find((a) => a[0] === p) || [p, p])[1].toLowerCase());
      if (labels.length) areas = ` (focus on ${labels.join(", ")})`;
    }
    return uc.q(site, areas).slice(0, 500);
  };

  const submit = async (ov, close) => {
    const question = ($("#uc-preview", ov) ? $("#uc-preview", ov).value.trim() : "");
    const failEl = $("input[name=failure]:checked", ov);
    const failure = failEl ? failEl.value : "normal";
    if (!question) return toast("Pick a use case or type a question first.");
    $("#uc-start", ov).disabled = true;
    try {
      const r = await api("/api/investigations", { method: "POST", body: { question, failure } });
      close();
      toast(`${r.code} started`);
      location.hash = `#/investigation/${r.id}`;
    } catch (e) { toast(e.message); $("#uc-start", ov).disabled = false; }
  };

  modal(`<div class="card-b"><h2>New investigation</h2><div id="uc-body" style="margin-top:6px"></div></div>
    <div class="modal-f" id="uc-foot"></div>`, (ov, close) => render(ov, close));
}

// ---------------------------------------------------------------- approval
function approveDialog(a, after) {
  modal(`<div class="card-b">
      <div class="muted small">${h(a.code)} · requires human approval</div>
      <h2 style="margin-top:4px">${a.action_type === "maintenance_ticket" ? "Create Maintenance Ticket" : a.action_type === "change_review" ? "Open Change Review" : "Approve Action"}</h2>
      <dl class="kv" style="margin-top:14px">
        <dt>Action</dt><dd>${h(a.recommended)}</dd>
        <dt>Why recommended?</dt><dd>${h(a.reason)}</dd>
        <dt>Expected impact</dt><dd>${h(a.expected_impact || "Impact remains to be confirmed.")}</dd>
        <dt>Risk</dt><dd>${h(a.risk || "Risk is limited to acting on incomplete evidence.")}</dd>
        <dt>Missing evidence</dt><dd>${(a.missing_evidence || []).length ? list(a.missing_evidence) : '<span class="muted">None identified</span>'}</dd>
        <dt>Why approval?</dt><dd>${h(a.approval_reason || "Human approval is required before tracked operational work is created.")}</dd>
        <dt>Evidence</dt><dd>${(a.evidence || []).length} linked records ${refs(a.evidence)}</dd>
        <dt>Priority</dt><dd>${prio(a.priority)}</dd>
        <dt>Owner</dt><dd>${h(a.owner)} · effort ${h(a.effort)}</dd>
        <dt>Confidence</dt><dd>${confBar(a.confidence)}</dd>
      </dl>
      <label for="note">Decision rationale <span class="muted small">(required when rejecting)</span></label><textarea id="note" maxlength="300" style="min-height:52px" placeholder="Record why this decision is appropriate."></textarea>
      <p class="muted small" style="margin:8px 0 0">Prototype: approval creates a <b>simulated</b> ticket in the local database. No external system is changed.</p>
    </div>
    <div class="modal-f"><button class="btn danger" id="rej">Reject</button><button class="btn primary" id="app">Approve Action</button></div>`,
  (ov, close) => {
    const go = async (verb) => {
      const rationale = $("#note", ov).value.trim();
      if (verb === "reject" && !rationale) return toast("Enter a rejection rationale.");
      $$("button", ov).forEach((b) => (b.disabled = true));
      try {
        const r = await api(`/api/actions/${a.id}/${verb}`, { method: "POST", body: { note: rationale } });
        close();
        const ticketLabel = a.action_type === "maintenance_ticket" ? "maintenance ticket" : a.action_type === "change_review" ? "change review" : "action ticket";
        toast(verb === "approve" ? `✓ Simulated ${ticketLabel} ${r.ticket_ref} created` : "Action rejected and sent back to Investigating");
        after && after(r);
      } catch (e) { toast(e.message); $$("button", ov).forEach((b) => (b.disabled = false)); }
    };
    $("#app", ov).onclick = () => go("approve");
    $("#rej", ov).onclick = () => go("reject");
  });
}

async function completeAction(id, after) {
  try { await api(`/api/actions/${id}/complete`, { method: "POST", body: {} }); toast("Action marked completed"); after && after(); } catch (e) { toast(e.message); }
}

// ---------------------------------------------------------------- router
const ROUTES = {};
function stopPolling() { if (state.timer) { clearInterval(state.timer); state.timer = null; } }
// `nav` is the navigation the caller belongs to: a handler that resumes after the user already moved on
// must not start (or keep) a timer that redraws the old page over the new one.
function poll(fn, ms, nav = state.nav) {
  if (nav !== state.nav) return;
  stopPolling();
  const timer = setInterval(async () => {
    if (nav !== state.nav) { clearInterval(timer); return; }
    try { await fn(); } catch (_) { clearInterval(timer); if (state.timer === timer) state.timer = null; }
  }, ms);
  state.timer = timer;
}
const isCurrent = (nav) => nav === state.nav;

async function route() {
  stopPolling();
  state.nav = (state.nav || 0) + 1;
  let [, page, arg] = location.hash.replace(/^#\/?/, "#/").split("/");
  if (!page || !Object.hasOwn(ROUTES, page)) page = "overview";  // '#/', unknown pages, '#/toString'
  state.route = page === "investigation" ? "investigations" : page === "agent" || page === "execution" ? "agents" : page;
  renderNav(state.route);
  $("#sidebar").classList.remove("open");
  const fn = ROUTES[page];
  const nav = state.nav;
  view().innerHTML = loading("Loading…");
  window.scrollTo(0, 0);
  try { await fn(arg); } catch (e) { if (isCurrent(nav)) errorState(e.message, route); }
}

window.addEventListener("hashchange", route);
document.addEventListener("DOMContentLoaded", () => {
  $("#menu-btn").onclick = () => $("#sidebar").classList.toggle("open");
  $("#search-form").onsubmit = (e) => {
    e.preventDefault();
    location.hash = `#/investigations/${encodeURIComponent($("#search").value.trim())}`;
  };
  const saved = localStorage.getItem("velloe-theme");
  if (saved) document.documentElement.dataset.theme = saved;
  refreshShell().then(route);
  setInterval(refreshShell, 3000);
});

/* Part 2: overview, investigations list, investigation page. */

// ---------------------------------------------------------------- shared widgets
function activityFeed(rows, withCode = false) {
  if (!rows.length) return empty("No agent activity yet.", " Start an investigation to see agents work.");
  return `<ul class="feed">${rows.map((a) => `<li>
      <div class="time">${h(clock(a.ts))}</div>
      <div><div class="who">${h(a.agent)} ${a.status !== "ok" ? auditStatus(a.status) : ""} ${withCode && a.code ? `<span class="muted small">${h(a.code)}</span>` : ""}</div>
      <div class="what">${h(describe(a))}</div>${a.reason ? `<div class="muted small" style="margin-top:2px"><b>Why:</b> ${h(a.reason)}</div>` : ""}</div></li>`).join("")}</ul>`;
}

function describe(a) {
  const map = {
    detect: "Opened investigation", create_plan: "Created investigation plan", simulation: `Failure simulation: ${a.output}`,
    tool_call: a.retry ? `${a.input} → recovered` : `${a.input}`, tool_failure: `${a.input} failed → ${a.output}`,
    fallback: `Fallback activated: ${a.input}`, degraded: `Fallback failed: ${a.input}; continuing without it`,
    detect_patterns: "Detected patterns and correlations", rank_hypotheses: `Ranked hypotheses: ${a.reason}`,
    recommend_actions: "Generated recommended actions", verify: a.status === "ok" ? "Result approved" : `Result rejected: ${a.reason}`,
    delegate_more_evidence: `Delegated extra evidence collection: ${a.output}`, revision: `Revision ${a.input} started`,
    complete: `Investigation ${a.output}`, approve_action: `Approved action → ${a.output}`, reject_action: "Rejected action",
    complete_action: "Marked action completed", stop: `Stopped safely: ${a.output}`, error: `Error: ${a.output}`,
  };
  return map[a.action] || `${a.action}: ${a.output}`;
}

function healthPanel(hl) {
  const color = (v) => v === "Healthy" ? "ok" : v === "Running" ? "accent" : v === "Recovered" ? "warn" : v === "Idle" ? "muted" : "err";
  return `<ul>${Object.entries(hl.components).map(([k, v]) => `<li><span class="dot ${dotFor(v)}"></span>${h(k)}
    <span class="st" style="color:var(--${color(v)})" title="${h(hl.notes[k] || "")}">● ${h(v)}</span></li>`).join("")}</ul>
    <div class="muted small" style="margin-top:6px">Based on ${h(hl.basis)}.</div>`;
}

function invTable(rows) {
  return `<div class="table-wrap"><table><thead><tr><th>Incident</th><th>Site</th><th>Priority</th><th>Status</th><th>Confidence</th><th>Last update</th></tr></thead>
    <tbody>${rows.map((i) => `<tr class="click" data-go="#/investigation/${i.id}">
      <td><div style="font-weight:600">${h(i.title)}</div><div class="muted small mono">${h(i.code)}${i.faults && i.faults !== "none" ? ` · <span title="${h(i.faults)}">simulated failure</span>` : ""}</div></td>
      <td style="white-space:nowrap">${h(i.site || "–")}</td><td>${prio(i.priority)}</td>
      <td>${i.running ? `<span class="badge b-blue"><span class="spinner" style="width:10px;height:10px;border-width:2px"></span> Running</span>` : status(i.status)}</td>
      <td>${confBar(i.confidence)}</td><td class="muted">${h(ago(i.updated_at))}</td></tr>`).join("")}</tbody></table></div>`;
}

function bindRows(root = view()) { $$("[data-go]", root).forEach((r) => (r.onclick = () => (location.hash = r.dataset.go))); }

const newBtn = `<button class="btn primary" data-new>+ New Investigation</button>`;
function bindNew(root = view()) { $$("[data-new]", root).forEach((b) => (b.onclick = () => newInvestigation())); }

// ---------------------------------------------------------------- overview
ROUTES.overview = async () => {
  if (!state.overview) await refreshShell();
  const draw = () => {
    const o = state.overview;
    if (!o) return;
    const k = o.kpis;
    const active = o.investigations;
    view().innerHTML = `
      <div class="page-head"><div><h1>Overview</h1><p>Operational intelligence across monitored sites · <span class="demo-pill">Demo / Simulated Data</span></p></div>
        <div class="actions"><a class="btn" href="#/agents" title="Run one agent for a focused task (Individual Agent Mode)">Run Individual Agent</a>${newBtn}</div></div>
      <div class="grid g-kpi" style="margin-bottom:16px">
        <div class="card kpi"><div class="label">Active Investigations</div><div class="value">${k.active}</div><div class="sub">Detected, investigating or awaiting approval</div></div>
        <div class="card kpi"><div class="label">Resolved</div><div class="value">${k.resolved}</div><div class="sub">Approved or completed</div></div>
        <div class="card kpi"><div class="label">Pending Approval</div><div class="value">${k.pending_approval}</div><div class="sub">Actions waiting for a human</div></div>
        <div class="card kpi"><div class="label">Recovery Events</div><div class="value">${k.recovery_events}</div><div class="sub">Fallbacks activated · ${k.tool_failures} failed tool attempts</div></div>
      </div>
      <div class="grid g-2">
        <div class="card"><div class="card-h"><h2>Investigations</h2><span class="right muted small">${k.total} stored run${k.total === 1 ? "" : "s"}</span></div>
          ${active.length ? invTable(active) : empty("No active investigations.", " Start a new investigation to begin.", newBtn)}</div>
        <div class="stack">
          <div class="card"><div class="card-h"><h2>Agent Activity</h2>${active.some((i) => i.running) ? '<span class="right badge b-blue"><span class="dot live"></span> Live</span>' : ""}</div>
            ${activityFeed(o.activity, true)}</div>
          <div class="card health"><div class="card-h"><h2>System Health</h2></div><div class="card-b">${healthPanel(o.health)}</div></div>
        </div>
      </div>`;
    bindRows(); bindNew();
  };
  draw();
  // state.overview is refreshed by the global 3 s shell poll; fetching it here too doubled the requests
  poll(() => draw(), 2500);
};

// ---------------------------------------------------------------- investigations list
ROUTES.investigations = async (q) => {
  let query = q || "";
  try { query = decodeURIComponent(query); } catch (_) { /* malformed %-escape: search the raw text */ }
  query = query.toLowerCase();
  const rows = await api("/api/investigations");
  const list = query ? rows.filter((i) => [i.title, i.code, i.site, i.question, i.status].join(" ").toLowerCase().includes(query)) : rows;
  view().innerHTML = `<div class="page-head"><div><h1>Investigations</h1><p>${query ? `Search results for “${h(query)}”` : "All investigations, newest first"}</p></div>
      <div class="actions">${query ? '<a class="btn" href="#/investigations">Clear search</a>' : ""}${newBtn}</div></div>
    <div class="card">${list.length ? invTable(list) : query ? empty("No matches.", " Try a site, code or keyword.") :
      empty("No investigations yet.", " Start a new investigation to begin.", newBtn)}</div>`;
  bindRows(); bindNew();
};

// ---------------------------------------------------------------- investigation page
const citeHtml = (text) => h(text).replace(/\[([A-Z]{2,4}-[A-Za-z0-9\-]+)\]/g, '<span class="ref">$1</span>');
const STRENGTH = { strong: ["Strong evidence", "b-ok"], moderate: ["Moderate evidence", "b-blue"], missing: ["Missing evidence", ""] };
const strength = (s) => `<span class="badge ${STRENGTH[s][1]}">${STRENGTH[s][0]}</span>`;

function stages(inv) {
  const log = inv.audit;
  const by = (r) => log.filter((a) => a.role === r);
  const running = inv.running;
  const last = log.length ? log[log.length - 1].role : null;
  const plan = inv.plan;
  const scout = by("scout");
  const fails = scout.filter((a) => a.action === "tool_failure").length;
  const falls = scout.filter((a) => a.action === "fallback").length;
  const deg = scout.filter((a) => a.action === "degraded").length;
  const verdicts = by("guardian").filter((a) => a.action === "verify");
  const rejected = verdicts.filter((a) => a.status === "rejected").length;
  const lastVerdict = verdicts.length ? verdicts[verdicts.length - 1] : null;
  const acts = inv.actions.filter((a) => a.requires_approval);
  const pending = acts.filter((a) => a.status === "Awaiting Approval").length;
  const st = (role, has) => (running && last === role ? "active" : has ? "done" : "");
  return [
    { n: "1 · Plan", name: "Velloe Strategist", cls: st("strategist", by("strategist").length),
      d: plan ? `${plan.tasks.length} tasks · ${plan.domain} · ${plan.planned_by}` : "Waiting" },
    { n: "2 · Collect", name: "Velloe Scout", cls: deg ? "err" : fails ? (running && last === "scout" ? "active" : "warn") : st("scout", scout.length),
      d: scout.length ? `${inv.tool_calls.length} calls · ${fails} failures · ${falls} fallback${falls === 1 ? "" : "s"}` : "Waiting" },
    { n: "3 · Analyze", name: "Velloe Intelligence", cls: st("intelligence", by("intelligence").length),
      d: inv.result ? `${inv.result.hypotheses.length} hypotheses · ${inv.revisions || 0} revision${inv.revisions === 1 ? "" : "s"}` : by("intelligence").length ? "Analyzing…" : "Waiting" },
    { n: "4 · Verify", name: "Velloe Guardian", cls: lastVerdict ? (lastVerdict.status === "ok" ? (rejected ? "warn" : "done") : running ? "active" : "err") : st("guardian", 0),
      d: lastVerdict ? (rejected ? `Rejected ${rejected}× → ${lastVerdict.output}` : lastVerdict.output) : "Waiting" },
    { n: "5 · Approve", name: "Human", cls: acts.length ? (pending ? "active" : "done") : "",
      d: acts.length ? (pending ? `${pending} awaiting approval` : acts.map((a) => a.status).join(", ")) : inv.status === "Needs Review" ? "Needs human review" : "No gated action" },
  ];
}

function recoveryBanner(inv) {
  const ev = inv.audit.filter((a) => a.role === "scout" && ["tool_failure", "fallback", "degraded"].includes(a.action));
  if (!ev.length) return "";
  const groups = {};
  // Group by the audited tool ("Velloe Ops API /telemetry", "snapshot export /telemetry" -> "telemetry");
  // `input` now holds the request JSON, which split one failure into two banners with JSON as the title.
  const toolOf = (a) => {
    if (a.tool) return a.tool.split("/").pop().trim();
    try { return JSON.parse(a.input).tool || a.input; } catch (_) { return (a.input.split("/")[1] || a.input).trim(); }
  };
  ev.forEach((a) => { const tool = toolOf(a); (groups[tool] = groups[tool] || []).push(a); });
  return Object.entries(groups).map(([tool, rows]) => {
    const bad = rows.some((r) => r.action === "degraded");
    const recovered = rows.some((r) => r.action === "fallback");
    const firstErr = rows.find((r) => r.action === "tool_failure");
    const retries = rows.filter((r) => r.action === "tool_failure" && r.retry > 0);
    const steps = [`⚠ Scout encountered API failure on <b>${h(tool)}</b> (${h(firstErr ? firstErr.output.split(" (")[0] : "unavailable")})`]
      .concat(retries.map((r) => `Retry ${r.retry} failed (${h(r.output.split(" (")[0])})`));
    if (recovered) steps.push("Fallback activated", `<b style="color:var(--ok)">✓ Investigation continued</b>`);
    else if (bad) steps.push("Fallback unavailable", `<b>Continued without ${h(tool)} data (gap reported, nothing fabricated)</b>`);
    else if (!inv.running) steps.push(`<b style="color:var(--ok)">✓ Recovered on retry</b>`);
    else steps.push('<span class="spinner"></span>');
    return `<div class="recovery ${bad ? "bad" : ""}" style="margin-bottom:16px" role="status">
      <h3>${bad ? "Tool unavailable - graceful degradation" : recovered ? "Tool unavailable - fallback source activated" : "Transient tool failure"}</h3>
      <div class="rec-steps">${steps.map((s) => `<span>${s}</span>`).join('<span class="sep">→</span>')}</div>
      <details style="margin-top:8px"><summary class="small">Why did the system do this?</summary><div class="small" style="margin-top:4px"><b>Retry:</b> the evidence was required and the failure could be transient. <b>Fallback:</b> the retry limit was reached, so Scout selected the configured fallback. <b>${bad ? "Stop using this source" : "Continue"}:</b> ${bad ? "both sources failed; the gap is disclosed instead of fabricated." : "validated evidence remains available for a degraded analysis."}</div></details></div>`;
  }).join("");
}

function hypothesisCard(hy, i) {
  const li = (cls, icon, e) => `<li><span class="i ${cls}">${icon}</span><span>${h(e.statement)} ${refs(e.refs)}
    <span class="muted small">· ${h(e.strength)}</span></span></li>`;
  return `<div class="hyp ${i === 0 ? "top" : ""}">
    <div class="hyp-h"><span class="muted small mono">Hypothesis ${String.fromCharCode(65 + i)}</span><h3>${h(hy.title)}</h3>
      <div class="conf">${i === 0 ? '<span class="badge b-accent">Leading</span>' : ""}<span class="badge ${hy.label === "High" ? "b-ok" : hy.label === "Medium" ? "b-blue" : ""}">${h(hy.label)}</span>${confBar(hy.confidence)}</div></div>
    <ul class="ev">
      ${hy.supporting.length ? hy.supporting.map((e) => li("sup", "✓", e)).join("") : '<li><span class="i mis">–</span><span class="muted">No supporting evidence found</span></li>'}
      ${hy.contradicting.map((e) => li("con", "⚠", e)).join("")}
      ${hy.missing.map((m) => `<li><span class="i mis">?</span><span class="muted">Missing: ${h(m.what)}</span></li>`).join("")}
    </ul></div>`;
}

// A re-run replaces the analysis and actions, so it is not offered once a human decided an action
// (the server refuses it too) or while a run is in progress.
const decided = (inv) => inv.actions.some((a) => ["approved", "rejected"].includes(a.approval_state) || a.status === "Completed");
const moreDisabled = (inv) => (inv.running ? "disabled" : decided(inv)
  ? 'disabled title="Not available after a human decision - start a new investigation instead"' : "");

function nextActionCard(inv) {
  const r = inv.result;
  const a = inv.actions[0];
  if (!a) return "";
  const gated = a.requires_approval;
  const ticketType = a.action_type === "maintenance_ticket" ? "Maintenance ticket" : a.action_type === "change_review" ? "Change review" : "Action ticket";
  return `<div class="next-action"><div class="card-h"><h2>Recommended Next Action</h2><span class="right">${status(a.status)}</span></div>
    <div class="card-b">
      <div style="font-size:15px;font-weight:600">${h(a.recommended)}</div>
      <div class="na-grid">
        <div><div class="k">Priority</div><div class="v">${prio(a.priority)}</div></div>
        <div><div class="k">Owner</div><div class="v">${h(a.owner)}</div></div>
        <div><div class="k">Estimated effort</div><div class="v">${h(a.effort)}</div></div>
      </div>
      <div class="small"><b>Why this recommendation?</b> ${h(a.reason)} ${refs(a.evidence)}</div>
      <div class="small" style="margin-top:6px"><b>Why this priority?</b> ${h(a.priority_reason || "Priority reflects impact and confidence.")}</div>
      <div class="small" style="margin-top:6px"><b>Why human approval?</b> ${h(a.approval_reason || (gated ? "Tracked operational work requires explicit approval." : "This is a non-disruptive task."))}</div>
      ${a.opportunity ? `<div class="small" style="margin-top:8px"><b>Business opportunity:</b> ${h(a.opportunity)}</div>` : ""}
      ${a.ticket_ref ? `<div class="recovery" style="margin-top:12px;border-color:var(--ok);background:var(--ok-soft)"><b style="color:var(--ok)">✓ ${h(ticketType)} ${h(a.ticket_ref)} created</b> <span class="muted small">(simulated, local store)</span></div>` : ""}
      <div style="display:flex;gap:8px;margin-top:14px;flex-wrap:wrap">
        <button class="btn" data-more ${moreDisabled(inv)}>Request More Evidence</button>
        ${gated && a.status === "Awaiting Approval" ? `<button class="btn primary" data-approve="${a.id}">Approve Action</button>` : ""}
        ${a.status === "Approved" ? `<button class="btn" data-complete="${a.id}">Mark completed</button>` : ""}
      </div>
      ${!gated ? '<div class="muted small" style="margin-top:8px">No external change - no approval required.</div>' : ""}
      ${r && r.confidence < 60 ? '<div class="muted small" style="margin-top:8px">Confidence is low: collect more evidence before acting.</div>' : ""}
    </div></div>`;
}

// ---------------------------------------------------------------- plain-language investigation views
// Short, simple sentences with the key facts highlighted. Detailed technical cards stay available below.
const mark = (text, tone = "") => `<mark class="hl ${tone}">${h(text)}</mark>`;
const plural = (n, word) => `${n} ${n === 1 ? word : word.endsWith("copy") ? word.slice(0, -1) + "ies" : word + "s"}`;
const took = (ms) => (!ms ? "" : ms < 1000 ? `${ms} ms` : `${(ms / 1000).toFixed(1)} s`);
const TOOL_WORD = { incidents: "incident records", telemetry: "temperature sensor readings",
  maintenance: "maintenance records", weather: "outside weather" };
const SOURCE_WORD = { ok: ["Worked", "b-ok"], degraded_fallback: ["Used backup copy", "b-warn"], failed: ["Not available", "b-err"] };
const toolWord = (t) => TOOL_WORD[String(t).split(":")[0]] || t;

function keyFacts(inv) {
  const r = inv.result, g = inv.guardian;
  const records = r ? Object.values(r.sources || {}).reduce((n, s) => n + (s.records || 0), 0) : 0;
  const waiting = inv.actions.filter((a) => a.status === "Awaiting Approval").length;
  const fact = (k, v, tone = "") => `<div class="key-fact ${tone}"><div class="k">${k}</div><div class="v">${v}</div></div>`;
  return `<div class="key-facts">
    ${fact("Status", h(inv.running ? "Working…" : inv.status))}
    ${fact("How sure we are", inv.confidence != null && r ? `${h(inv.confidence)}%` : "–",
      !r || inv.confidence == null ? "" : g && g.verdict !== "APPROVED" ? "" : inv.confidence >= 70 ? "ok" : inv.confidence >= 40 ? "" : "err")}
    ${fact("Priority", h(inv.priority || "–"))}
    ${fact("Records checked", h(records || "–"))}
    ${fact("Guardian", g ? (g.verdict === "APPROVED" ? "Approved" : "Not approved") : "Waiting", g ? (g.verdict === "APPROVED" ? "ok" : "err") : "")}
    ${fact("Needs your approval", h(waiting), waiting ? "warn" : "")}
  </div>`;
}

function simpleSummary(inv) {
  const r = inv.result, g = inv.guardian;
  if (!r) return "";
  const top = r.hypotheses[0];
  const main = inv.actions[0];
  const waiting = inv.actions.filter((a) => a.status === "Awaiting Approval").length;
  const row = (k, body) => `<div class="ps-row"><div class="ps-k">${k}</div><p>${body}</p></div>`;
  const approved = g && g.verdict === "APPROVED";
  return `<div class="card" style="margin-bottom:16px"><div class="card-h"><h2>In simple words</h2><span class="right muted small">Quick summary of this investigation</span></div>
    <div class="card-b plain-text">
      ${row("What happened", h(r.problem.statement))}
      ${row("Most likely reason", top
        ? `The most likely reason is ${mark(top.title)}. We are ${mark(`${top.confidence}% sure`, top.confidence >= 70 ? "ok" : "")}. This is based on ${plural(top.supporting.length, "fact")} that support it and ${plural(top.contradicting.length, "fact")} against it.`
          + (r.hypotheses.length > 1 ? ` We also checked ${plural(r.hypotheses.length - 1, "other possible reason")}.` : "")
        : "We could not find a clear reason in the data we have.")}
      ${row("Why it matters", h(r.impact.statement))}
      ${row("What we suggest", main ? `<b>${h(main.recommended)}</b> The owner is ${mark(main.owner)} and the priority is ${mark(main.priority, main.priority === "High" || main.priority === "Critical" ? "err" : "")}.` : "No action is suggested.")}
      ${row("Checked by Guardian", g ? (approved
        ? `Guardian checked the result and ${mark("approved it", "ok")}. The facts support the answer.`
        : `Guardian ${mark("did not approve", "err")} this result. Reason: ${h(g.reason)}`) : "Guardian has not checked this yet.")}
      ${row("Your next step", !approved ? "Nothing to approve yet. You can ask for more evidence and run it again."
        : waiting ? `${mark(`${plural(waiting, "action")} need${waiting === 1 ? "s" : ""} your approval`, "warn")}. See <b>Your decision</b> below.`
        : "Nothing is waiting for you. All decisions are done.")}
    </div></div>`;
}

function agentBoxes(inv) {
  const r = inv.result, g = inv.guardian, p = inv.plan;
  const runs = inv.agent_runs || [];
  const last = inv.audit.length ? inv.audit[inv.audit.length - 1].role : null;
  const time = (role) => took(runs.filter((x) => x.agent === role).reduce((n, x) => n + (x.duration_ms || 0), 0));
  const badgeFor = (role, done) => {
    if (inv.running && last === role) return '<span class="badge b-blue"><span class="dot live"></span> Working</span>';
    if (runs.some((x) => x.agent === role && ["error", "timeout"].includes(x.status))) return '<span class="badge b-err">Problem</span>';
    return done ? '<span class="badge b-ok">Done</span>' : '<span class="badge">Waiting</span>';
  };
  const box = (n, role, name, job, badge, text, items = []) => `<div class="agent-box">
      <div class="ab-h"><span class="ab-step">${n}</span><div style="flex:1"><h3>${h(name)}</h3><div class="ab-role">${h(job)}</div></div>${badge}</div>
      <p>${text}</p>${items.length ? `<ul>${items.map((i) => `<li>${i}</li>`).join("")}</ul>` : ""}
      ${time(role) ? `<div class="ab-foot">Time taken: ${h(time(role))}</div>` : ""}</div>`;

  const calls = inv.tool_calls || [];
  const failed = calls.filter((c) => !["ok", "fallback_ok"].includes(c.outcome)).length;
  const backups = calls.filter((c) => c.outcome === "fallback_ok").length;
  const sources = r ? Object.entries(r.sources || {}) : [];
  const verdicts = inv.audit.filter((a) => a.role === "guardian" && a.action === "verify");
  const rejections = verdicts.filter((a) => a.status === "rejected").length;
  const checks = (g && g.checks) || [];
  const passed = checks.filter((c) => c.passed).length;

  return `<div class="card" style="margin-bottom:16px"><div class="card-h"><h2>What each agent did</h2><span class="right muted small">Step by step</span></div>
    <div class="card-b"><div class="agent-grid">
      ${box(1, "strategist", "Velloe Strategist", "Makes the plan", badgeFor("strategist", !!p),
        p ? `Made a plan with ${mark(plural(p.tasks.length, "step"))} for ${h((p.site_labels || []).join(", ") || "all sites")}. The topic is ${mark(p.domain)}.`
          : "Waiting to make a plan.",
        p ? p.tasks.map((t) => `Look at ${h(toolWord(t.tool))}`) : [])}
      ${box(2, "scout", "Velloe Scout", "Collects the data", badgeFor("scout", sources.length || calls.length),
        calls.length ? `Made ${mark(plural(calls.length, "data request"))}. ${failed ? `${mark(plural(failed, "request") + " failed", "err")}, ` : "No request failed. "}${backups ? `and ${mark(plural(backups, "backup copy") + " used", "warn")}. ` : ""}No data was made up.`
          : "Waiting to collect data.",
        sources.map(([t, s]) => `${h(toolWord(t))}: <span class="badge ${(SOURCE_WORD[s.status] || ["", ""])[1]}">${h((SOURCE_WORD[s.status] || [s.status])[0])}</span> · ${h(s.records)} records`))}
      ${box(3, "intelligence", "Velloe Intelligence", "Finds the reason", badgeFor("intelligence", !!r),
        r ? `Found ${mark(plural(r.patterns.length, "pattern"))} and ${mark(plural(r.hypotheses.length, "possible reason"))}. ${r.hypotheses[0] ? `The top reason is <b>${h(r.hypotheses[0].title)}</b> (${h(r.hypotheses[0].confidence)}% sure).` : ""}`
          + (inv.revisions ? ` It improved its answer ${mark(plural(inv.revisions, "time"))} after Guardian feedback.` : "")
          : "Waiting for data.",
        r ? [`Suggested ${plural(inv.actions.length, "action")}`, `Missing evidence: ${h((r.hypotheses[0]?.missing || []).length || "none")}`] : [])}
      ${box(4, "guardian", "Velloe Guardian", "Checks the answer", g ? (g.verdict === "APPROVED" ? '<span class="badge b-ok">Approved</span>' : '<span class="badge b-err">Not approved</span>') : badgeFor("guardian", false),
        g ? `Checked ${mark(plural(checks.length, "rule"))}: ${mark(`${passed} passed`, "ok")}${checks.length - passed ? `, ${mark(`${checks.length - passed} failed`, "err")}` : ""}. ${rejections ? `It sent the answer back ${plural(rejections, "time")} before the final result. ` : ""}Final result: ${mark(g.verdict === "APPROVED" ? "Approved" : "Not approved", g.verdict === "APPROVED" ? "ok" : "err")}.`
          : "Waiting to check the answer.",
        g ? [h(g.reason)] : [])}
    </div></div></div>`;
}

function humanDecision(inv) {
  const g = inv.guardian;
  if (inv.running || !g) return "";
  const head = `<div class="card-h"><h2>Your decision</h2><span class="right muted small">Human approval · step 5</span></div>`;
  if (g.verdict !== "APPROVED") {
    return `<div class="card locked" style="margin-bottom:16px">${head}<div class="card-b plain-text">
      <p style="margin:0">🔒 ${mark("Human approval is locked.", "err")} Guardian did not approve this result, so no action can be approved yet.</p>
      <p style="margin:8px 0 0">You can collect more evidence and run the check again.</p>
      <div style="margin-top:12px"><button class="btn" data-more ${moreDisabled(inv)}>Request More Evidence</button></div></div></div>`;
  }
  const byAction = {};
  (inv.approvals || []).forEach((p) => (byAction[p.action_id] = p));
  const gated = inv.actions.filter((a) => a.requires_approval);
  const free = [...new Set(inv.actions.filter((a) => !a.requires_approval).map((a) => a.recommended))];
  const item = (a) => {
    const p = byAction[a.id];
    let body;
    if (a.status === "Awaiting Approval") {
      body = `<label for="dn-${a.id}" class="small">Note <span class="muted">(needed if you reject)</span></label>
        <textarea id="dn-${a.id}" maxlength="300" style="min-height:44px" placeholder="Why do you approve or reject this?"></textarea>
        <div class="di-actions"><button class="btn primary" data-decide="approve" data-id="${a.id}">✓ Approve</button>
          <button class="btn danger" data-decide="reject" data-id="${a.id}">✗ Reject</button>
          <button class="btn" data-approve="${a.id}">See full details</button></div>`;
    } else if (a.approval_state === "approved") {
      body = `<div class="done-note">✓ Approved${p ? ` by ${h(p.actor)}` : ""}. Ticket ${h(a.ticket_ref)} was created (simulated).</div>
        ${p && p.note ? `<div class="small muted">Note: ${h(p.note)}</div>` : ""}
        ${a.status === "Approved" ? `<div class="di-actions"><button class="btn" data-complete="${a.id}">Mark as completed</button></div>` : '<div class="small muted">Marked as completed.</div>'}`;
    } else if (a.approval_state === "rejected") {
      body = `<div class="rej-note">✗ Rejected${p ? ` by ${h(p.actor)}` : ""}.</div>${p && p.note ? `<div class="small muted">Reason: ${h(p.note)}</div>` : ""}`;
    } else {
      body = `<div class="small muted">Status: ${h(a.status)}</div>`;
    }
    return `<div class="decision-item"><div style="display:flex;gap:8px;align-items:flex-start">
        <div style="flex:1"><div style="font-weight:600">${h(a.recommended)}</div>
        <div class="small" style="margin-top:4px">${h(a.reason)}</div>
        <div class="small muted" style="margin-top:4px">Priority ${h(a.priority)} · Owner ${h(a.owner)} · Effort ${h(a.effort)}${a.confidence != null ? ` · ${h(a.confidence)}% sure` : ""}</div></div>
        ${status(a.status)}</div>${body}</div>`;
  };
  return `<div class="card decision" style="margin-bottom:16px">${head}<div class="card-b plain-text">
    <p style="margin:0">✓ ${mark("Guardian approved this result.", "ok")} ${!gated.length ? "No action needs your approval."
      : gated.some((a) => a.status === "Awaiting Approval") ? "Now a person must decide. Nothing changes until you approve."
      : "The human decision is done. You can see it below."}</p>
    ${gated.map(item).join("")}
    ${free.length ? `<div class="small muted" style="margin-top:12px"><b>Safe tasks (no approval needed):</b> ${free.map((t) => h(t)).join(" · ")}</div>` : ""}
    <p class="small muted" style="margin:12px 0 0">Demo only: approving creates a simulated ticket in the local database. No real system is changed.</p>
  </div></div>`;
}

const DETAILS_KEY = "velloe-inv-details-open";
const detailsOpen = () => localStorage.getItem(DETAILS_KEY) === "1";
// When a human decision is pending, the decision panel goes right under the header so it is never missed.
const awaiting = (inv) => !inv.running && inv.guardian?.verdict === "APPROVED" &&
  inv.actions.some((a) => a.status === "Awaiting Approval");

ROUTES.investigation = async (id) => {
  const nav = state.nav;
  const draw = async () => {
    const inv = await api(`/api/investigations/${encodeURIComponent(id)}`);
    if (!isCurrent(nav)) return false;  // user navigated away while this request was in flight
    const r = inv.result;
    const g = inv.guardian;
    // Only a live thread means "running": a row left 'Investigating' by a dead server would poll forever.
    const live = inv.running;
    view().innerHTML = `
      <div class="inv-head"><div style="flex:1;min-width:260px">
          <div class="code">INVESTIGATION #${h(inv.code)} <span class="demo-pill" style="margin-left:6px">Demo / Simulated Data</span></div>
          <h1>${h(inv.title)}${inv.site && !String(inv.title).includes(inv.site) ? ` <span class="muted" style="font-weight:500">· ${h(inv.site)}</span>` : ""}</h1>
          <div class="meta">${inv.priority ? prio(inv.priority) + " PRIORITY" : ""} <span class="muted">Status:</span> ${live ? '<span class="badge b-blue"><span class="spinner" style="width:10px;height:10px;border-width:2px"></span> Investigating</span>' : status(inv.status)}
            ${inv.confidence != null && r ? `<span class="muted">· Confidence ${h(inv.confidence)}%</span>` : ""}</div>
          <div class="muted small" style="margin-top:6px">“${h(inv.question)}”</div></div>
        <div style="display:flex;gap:8px"><a class="btn" href="#/audit/${inv.id}">Audit trail</a><button class="btn" data-more ${moreDisabled(inv)}>Request More Evidence</button></div></div>
      <div class="card" style="margin-bottom:16px"><div class="pipeline">${stages(inv).map((s) => `<div class="stage ${s.cls}">
        <div class="n">${h(s.n)}</div><div class="s">${s.cls === "active" ? '<span class="spinner" style="width:12px;height:12px"></span>' : ""}${h(s.name)}</div><div class="d">${h(s.d)}</div></div>`).join("")}</div></div>
      ${awaiting(inv) ? `<div class="approval-callout" role="alert"><b>⏳ Your approval is needed.</b> Guardian verified this result; ${plural(inv.actions.filter((a) => a.status === "Awaiting Approval").length, "action")} wait for a human decision.</div>${humanDecision(inv)}` : ""}
      ${keyFacts(inv)}
      ${recoveryBanner(inv)}
      ${inv.status === "Error" || inv.status === "Stopped" ? `<div class="recovery bad" style="margin-bottom:16px"><h3>Run ${h(inv.status.toLowerCase())}</h3>${h(inv.error)}</div>` : ""}
      ${!r ? `${agentBoxes(inv)}<div class="card"><div class="card-h"><h2>Live agent activity</h2><span class="right badge b-blue"><span class="dot live"></span> ${live ? "Running" : "Finished"}</span></div>
          ${activityFeed(inv.audit.slice().reverse())}</div>` : `
      ${simpleSummary(inv)}
      ${agentBoxes(inv)}
      ${awaiting(inv) ? "" : humanDecision(inv)}
      <button type="button" class="details-toggle" id="details-toggle" aria-expanded="${detailsOpen()}" aria-controls="full-details">
        <span class="chev" aria-hidden="true">▸</span><span>${detailsOpen() ? "Hide" : "Show"} all technical details</span>
        <span class="muted small">plan, timeline, evidence, hypotheses, Guardian checks, data sources</span></button>
      <div class="full-details" id="full-details" ${detailsOpen() ? "" : "hidden"}>
      ${inv.plan ? `<details class="card why-panel" style="margin-bottom:16px"><summary class="card-h"><b>Why this investigation plan?</b><span class="muted small">Domain, scope, tools, and delegation rationale</span></summary><div class="card-b"><div class="small"><b>Objective:</b> ${h(inv.plan.objective_text)}</div><div class="table-wrap" style="margin-top:8px"><table><thead><tr><th>Task</th><th>Tool</th><th>What</th><th>Why</th></tr></thead><tbody>${inv.plan.tasks.map((t) => `<tr><td class="mono small">${h(t.id)}</td><td>${h(t.tool)}</td><td>${h(t.agent)} collects evidence</td><td class="small">${h(t.reason)}</td></tr>`).join("")}</tbody></table></div></div></details>` : ""}

      <div class="grid g-2">
        <div class="stack">
          <div class="card"><div class="card-h"><h2>Investigation Summary</h2>${g ? `<span class="right badge ${g.verdict === "APPROVED" ? "b-ok" : "b-err"}">Guardian: ${h(g.verdict)}</span>` : ""}</div>
            <div class="card-b"><p style="margin:0 0 12px">${citeHtml(r.summary)}</p>
              <dl class="kv"><dt>Problem</dt><dd>${h(r.problem.statement)} ${refs(r.problem.refs)}</dd>
                <dt>Impact</dt><dd>${h(r.impact.statement)}</dd>
                <dt>Evidence status</dt><dd><span class="badge ${r.evidence_assessment?.status === "INSUFFICIENT EVIDENCE" ? "b-err" : "b-ok"}">${h(r.evidence_assessment?.status || "Available")}</span>${r.evidence_assessment?.status === "INSUFFICIENT EVIDENCE" ? `<div class="small" style="margin-top:4px"><b>Known:</b> ${h((r.evidence_assessment.known || []).join("; ") || "No matching operational fact.")}<br><b>Uncertain:</b> ${h((r.evidence_assessment.uncertain || []).join("; "))}<br><b>Next evidence:</b> ${h((r.evidence_assessment.recommended_next_evidence || []).join("; ") || "Manual investigation required.")}</div>` : ""}</dd>
                <dt>Patterns</dt><dd>${r.patterns.map((p) => `<div>${h(p.statement)} ${refs(p.refs.slice(0, 4))}${p.refs.length > 4 ? ` <span class="muted small">+${p.refs.length - 4}</span>` : ""}</div>`).join("") || "–"}</dd></dl></div></div>
          <div class="grid g-2e">
            <div class="card"><div class="card-h"><h2>Event Timeline</h2><span class="right muted small">IST</span></div><div class="card-b">
              ${r.timeline.length ? `<ol class="tl">${r.timeline.map((t) => `<li><span class="t">${h(t.time)}</span><span class="m k-${h(t.kind)}"></span><span>${h(t.label)} ${refs(t.refs)}</span></li>`).join("")}</ol>` : empty("No timeline.", " No correlated events in the collected data.")}</div></div>
            <div class="card"><div class="card-h"><h2>Evidence Panel</h2></div><div class="card-b">
              ${r.evidence_chain.length ? `<div class="chain">${r.evidence_chain.map((c, i) => `${i ? '<div class="arrow">↓</div>' : ""}<div class="step"><span class="mono small muted">${h(c.time)}</span><span>${h(c.step)}</span>${strength(c.strength)}</div>`).join("")}
                ${(r.hypotheses[0]?.missing || []).map((m) => `<div class="arrow">·</div><div class="step missing"><span class="muted">${h(m.what)}</span>${strength("missing")}</div>`).join("")}</div>` : empty("No evidence chain.", " The leading hypothesis has no time-ordered evidence.")}</div></div>
          </div>
          <div class="card"><div class="card-h"><h2>Root-cause Hypotheses</h2><span class="right muted small">Confidence = evidence-weight score, not a probability</span></div>
            <div class="card-b">${r.hypotheses.length ? r.hypotheses.map(hypothesisCard).join("") : empty("No hypotheses.", " Not enough data to form one.")}</div></div>
        </div>
        <div class="stack">
          ${nextActionCard(inv)}
          <div class="card"><div class="card-h"><h2>Action & Opportunity Engine</h2><a class="right small" href="#/board">Action Board →</a></div>
            <div class="table-wrap"><table><tbody>${inv.actions.map((a) => `<tr><td><div style="font-weight:600;font-size:13px">${h(a.issue)}</div>
              <div class="small" style="margin-top:3px"><b>Why:</b> ${h(a.reason)}</div><div class="muted small"><b>Priority:</b> ${h(a.priority_reason || "Set from impact and confidence.")} · ${h(a.owner)} · effort ${h(a.effort)}</div></td><td>${prio(a.priority)}</td><td>${status(a.status)}${a.status === "Awaiting Approval" && a !== inv.actions[0] ? `<div style="margin-top:6px"><button class="btn sm primary" data-approve="${a.id}">Review</button></div>` : ""}</td></tr>`).join("")}</tbody></table></div>
            ${r.opportunities.length ? `<div class="card-b" style="border-top:1px solid var(--border)"><div class="muted small" style="margin-bottom:4px;font-weight:600">BUSINESS OPPORTUNITIES</div>${r.opportunities.map((o) => `<div class="small" style="padding:3px 0">◆ ${h(o)}</div>`).join("")}</div>` : ""}</div>
          <div class="card"><div class="card-h"><h2>Guardian Verification</h2><span class="right muted small">${h(inv.revisions)} revision${inv.revisions === 1 ? "" : "s"}</span></div>
            <div class="card-b">${g ? `<dl class="kv" style="margin-bottom:10px"><dt>Why ${h(g.verdict)}</dt><dd>${h(g.reason)}</dd><dt>Risk</dt><dd>${h(g.risk || "Low")}</dd><dt>Missing evidence</dt><dd>${list(g.missing_evidence)}</dd>${g.verdict === "REJECTED" ? `<dt>Required revision</dt><dd>${list(g.required_revision)}</dd>` : ""}</dl>` : ""}<ul class="checks">${(g?.checks || []).map((c) => `<li><span style="color:var(--${c.passed ? "ok" : "err"});font-weight:700">${c.passed ? "✓" : "✗"}</span><span><b>${h(c.check.replace(/_/g, " "))}</b> <span class="muted">${h(c.detail)}</span></span></li>`).join("")}</ul></div></div>
          <div class="card"><div class="card-h"><h2>Data Sources</h2></div><div class="table-wrap"><table><tbody>
            ${Object.entries(r.sources).map(([t, s]) => `<tr><td style="font-weight:600">${h(t)}</td><td class="small">${h(s.source || "unavailable")}<div class="muted" style="margin-top:3px"><b>Why selected:</b> ${h(s.selection_reason || "Selected by the investigation plan.")}</div></td>
              <td><span class="badge ${s.status === "ok" ? "b-ok" : s.status === "failed" ? "b-err" : "b-warn"}">${s.status === "degraded_fallback" ? "fallback" : h(s.status)}</span></td><td class="small muted">${h(s.records)} rec</td></tr>`).join("")}</tbody></table></div></div>
          <div class="card"><div class="card-h"><h2>Unknowns & Risks</h2></div><div class="card-b small">
            ${r.unknowns.map((u) => `<div style="padding:3px 0">? ${h(u.what)}</div>`).join("") || '<div class="muted">No open unknowns.</div>'}
            ${r.risks.map((x) => `<div style="padding:3px 0">⚠ ${h(x.risk)} ${refs(x.refs)}</div>`).join("")}</div></div>
        </div>
      </div></div>`}`;
    // Plain button + hidden panel (not <details>/<summary>): the click can only toggle, never navigate.
    const dt = $("#details-toggle");
    if (dt) dt.onclick = (e) => {
      e.preventDefault(); e.stopPropagation();
      const open = !detailsOpen();
      localStorage.setItem(DETAILS_KEY, open ? "1" : "0");
      $("#full-details").hidden = !open;
      dt.setAttribute("aria-expanded", String(open));
      dt.children[1].textContent = `${open ? "Hide" : "Show"} all technical details`;
    };
    $$("[data-decide]").forEach((b) => (b.onclick = async () => {
      const verb = b.dataset.decide;
      const note = ($(`#dn-${b.dataset.id}`) || { value: "" }).value.trim();
      if (verb === "reject" && !note) return toast("Please write a short reason before rejecting.");
      $$("[data-decide]").forEach((x) => (x.disabled = true));  // no double submit
      try {
        const res = await api(`/api/actions/${b.dataset.id}/${verb}`, { method: "POST", body: { note } });
        toast(verb === "approve" ? `✓ Approved · simulated ticket ${res.ticket_ref} created` : "Action rejected");
      } catch (e) { toast(e.message); }
      draw();
    }));
    $$("[data-more]").forEach((b) => (b.onclick = async () => {
      $$("[data-more]").forEach((x) => (x.disabled = true));  // no double submit
      try { await api(`/api/investigations/${inv.id}/more-evidence`, { method: "POST", body: {} }); toast("Collecting all available evidence and re-running analysis…"); await draw(); poll(tick, 1000, nav); } catch (e) { toast(e.message); draw(); }
    }));
    $$("[data-approve]").forEach((b) => (b.onclick = () => approveDialog(inv.actions.find((a) => String(a.id) === b.dataset.approve), draw)));
    $$("[data-complete]").forEach((b) => (b.onclick = () => completeAction(b.dataset.complete, draw)));
    return live;
  };
  const tick = async () => { if (!(await draw())) stopPolling(); };
  if (await draw()) poll(tick, 1000, nav);
};

/* Part 3: action board, incidents, agents, audit trail, analytics, settings. */

// ---------------------------------------------------------------- action board
const COLUMNS = ["Detected", "Investigating", "Awaiting Approval", "Approved", "Completed"];
const approvalText = (a) => !a.requires_approval ? "Not required" : a.approval_state === "approved" ? "Approved" : a.approval_state === "rejected" ? "Rejected" : a.approval_state === "not_verified" ? "Blocked - not verified" : "Pending review";
const actionTarget = (a) => `#/${a.investigation_mode === "INDIVIDUAL_AGENT" ? "execution" : "investigation"}/${a.investigation_id}`;

function actionControls(a) {
  return `${a.status === "Awaiting Approval" ? `<button class="btn sm primary" data-approve="${a.id}">Review</button>` : ""}
    ${a.status === "Approved" || a.status === "Detected" ? `<button class="btn sm" data-complete="${a.id}">Complete</button>` : ""}
    <a class="btn sm" href="${actionTarget(a)}">${h(a.investigation_code)}</a>`;
}

function actionCard(a, flashId) {
  return `<article class="acard ${String(a.id) === String(flashId) ? "flash" : ""}">
    <div style="display:flex;gap:6px;align-items:center">${prio(a.priority)}<span class="muted small mono" style="margin-left:auto">${h(a.code)}</span></div>
    <div class="t">${h(a.issue)}</div><div class="r">${h(a.recommended)}</div>
    <details style="margin-top:8px"><summary class="small">Why this decision?</summary><div class="small" style="margin-top:4px">${h(a.reason)}</div><div class="muted small" style="margin-top:3px">${h(a.priority_reason || "Priority reflects impact and confidence.")}</div></details>
    <div class="f"><span>${h(a.site || "No site")}</span><span>· ${h(a.owner)}</span><span>· effort ${h(a.effort)}</span>${a.confidence != null ? `<span>· ${h(a.confidence)}%</span>` : ""}
      ${a.requires_approval ? `<span class="badge ${a.approval_state === "approved" ? "b-ok" : a.approval_state === "rejected" ? "b-err" : "b-warn"}">${h(approvalText(a))}</span>` : '<span class="badge">No approval</span>'}
      ${a.ticket_ref ? `<span class="badge b-ok">✓ ${h(a.ticket_ref)}</span>` : ""}</div>
    ${a.opportunity ? `<details style="margin-top:8px"><summary class="small">Automation opportunity</summary><div class="small muted" style="margin-top:4px">${h(a.opportunity)}</div></details>` : ""}
    <div class="btns">${actionControls(a)}</div></article>`;
}

function actionTable(acts) {
  return `<div class="card"><div class="table-wrap"><table class="action-table"><thead><tr><th>Issue / Site</th><th>Priority</th><th>Owner</th><th>Recommendation</th><th>Approval</th><th>Status</th><th>Action</th></tr></thead><tbody>
    ${acts.map((a) => `<tr><td><div style="font-weight:600">${h(a.issue)}</div><div class="muted small">${h(a.site || "No site")} · ${h(a.code)} · ${h(a.action_type)}</div></td>
      <td>${prio(a.priority)}</td><td><div>${h(a.owner)}</div><div class="muted small">Effort ${h(a.effort)}</div></td>
      <td style="min-width:280px"><div class="small">${h(a.recommended)}</div><div class="small" style="margin-top:4px"><b>Why:</b> ${h(a.reason)}</div><div class="muted small">${h(a.priority_reason || "Priority reflects impact and confidence.")}</div>${a.opportunity ? `<div class="muted small" style="margin-top:4px">◆ ${h(a.opportunity)}</div>` : ""}<div class="muted small">${(a.evidence || []).length} evidence link(s)</div></td>
      <td><span class="badge ${a.approval_state === "approved" ? "b-ok" : a.approval_state === "rejected" ? "b-err" : a.requires_approval ? "b-warn" : ""}">${h(approvalText(a))}</span></td>
      <td>${status(a.status)}${a.ticket_ref ? `<div class="small mono" style="margin-top:4px">${h(a.ticket_ref)}</div>` : ""}</td>
      <td><div style="display:flex;gap:6px;flex-wrap:wrap">${actionControls(a)}</div></td></tr>`).join("")}</tbody></table></div></div>`;
}

ROUTES.board = async () => {
  let boardView = localStorage.getItem("velloe-board-view") === "table" ? "table" : "kanban";
  const draw = async (flashId) => {
    const acts = await api("/api/actions");
    const switcher = `<div class="chips" role="group" aria-label="Action Board view"><button class="chip" data-board-view="kanban" aria-pressed="${boardView === "kanban"}">Kanban</button><button class="chip" data-board-view="table" aria-pressed="${boardView === "table"}">Table</button></div>`;
    const kanban = `<div class="board">${COLUMNS.map((c) => {
      const cards = acts.filter((a) => a.status === c);
      return `<section class="col" aria-label="${h(c)}"><div class="col-h"><span class="dot ${c === "Awaiting Approval" ? "warn" : c === "Approved" || c === "Completed" ? "" : "idle"}"></span>${h(c)}<span class="n">${cards.length}</span></div>
        <div class="col-b">${cards.map((a) => actionCard(a, flashId)).join("") || '<div class="muted small" style="padding:8px">Nothing here.</div>'}</div></section>`;
    }).join("")}</div>`;
    view().innerHTML = `<div class="page-head"><div><h1>Action Board</h1><p>Data → insight → decision → action. Operational changes require human approval.</p></div>
        <div class="actions">${switcher}<span class="demo-pill">Demo / Simulated Data</span></div></div>
      ${acts.length ? (boardView === "kanban" ? kanban : actionTable(acts)) : `<div class="card">${empty("No actions yet.", " Actions appear after the Guardian approves an investigation.", newBtn)}</div>`}`;
    bindNew();
    $$('[data-board-view]').forEach((b) => (b.onclick = () => { boardView = b.dataset.boardView; localStorage.setItem("velloe-board-view", boardView); draw(); }));
    $$('[data-approve]').forEach((b) => (b.onclick = () => approveDialog(acts.find((a) => String(a.id) === b.dataset.approve), (r) => draw(r.id))));
    $$('[data-complete]').forEach((b) => (b.onclick = () => completeAction(b.dataset.complete, () => draw(b.dataset.complete))));
  };
  await draw();
};

// ---------------------------------------------------------------- incidents
ROUTES.incidents = async () => {
  const rows = await api("/api/incidents");
  view().innerHTML = `<div class="page-head"><div><h1>Incidents</h1><p>Incident catalog stored in SQLite · <span class="demo-pill">Demo / Simulated Data</span></p></div>
      <div class="actions">${newBtn}</div></div>
    <div class="card"><div class="table-wrap"><table><thead><tr><th>ID</th><th>Opened (IST)</th><th>Site</th><th>Severity</th><th>Category</th><th>Summary</th><th>Investigations</th><th></th></tr></thead>
      <tbody>${rows.map((r) => `<tr><td class="mono">${h(r.id)}</td><td class="small">${h(String(r.opened || "").replace("T", " ").slice(0, 16))}</td>
        <td>${h(r.site_label)} <span class="muted small">${h(r.site)}</span></td>
        <td><span class="badge ${r.severity === "P1" ? "b-err" : r.severity === "P2" ? "b-warn" : ""}">${h(r.severity)}</span></td><td>${h(r.category)}</td><td>${h(r.summary)}</td>
        <td class="small">${(r.investigations || []).length ? r.investigations.slice(-3).map((i) => `<a href="#/${i.mode === "INDIVIDUAL_AGENT" ? "execution" : "investigation"}/${h(i.id)}">${h(i.code)}</a>`).join(", ") + (r.investigations.length > 3 ? ` +${r.investigations.length - 3}` : "") : '<span class="muted">none yet</span>'}</td>
        <td><button class="btn sm" data-inv="${h(r.id)}">Investigate</button></td></tr>`).join("")}</tbody></table></div></div>`;
  bindNew();
  $$("[data-inv]").forEach((b) => (b.onclick = () => {
    const r = rows.find((x) => x.id === b.dataset.inv);
    newInvestigation(`Investigate ${r.category} incident ${r.id} at ${r.site_label}: ${r.summary}.`);
  }));
};

// ---------------------------------------------------------------- agents
const AGENT_LETTER = { strategist: "S", scout: "Sc", intelligence: "I", guardian: "G" };
const AGENT_META = {
  strategist: { name: "Velloe Strategist", role: "Planning & Delegation", verb: "Generate Plan" },
  scout: { name: "Velloe Scout", role: "Data & Tool Intelligence", verb: "Fetch Data" },
  intelligence: { name: "Velloe Intelligence", role: "Analysis & Recommendations", verb: "Analyze" },
  guardian: { name: "Velloe Guardian", role: "Verification & Quality Control", verb: "Verify" },
};
const fmtMs = (v) => (v == null ? "–" : v >= 1000 ? (v / 1000).toFixed(1) + " s" : v < 1 ? "&lt;1 ms" : h(v) + " ms");

ROUTES.agents = async () => {
  const [rows, recent] = await Promise.all([api("/api/agents"), api("/api/executions")]);
  view().innerHTML = `<div class="page-head"><div><h1>Agents</h1><p>Four specialized agents coordinated by an orchestration layer. Metrics are measured from stored runs (demo dataset).</p></div>
      <div class="actions"><span class="demo-pill">Demo dataset</span></div></div>
    <div class="grid" style="grid-template-columns:repeat(auto-fit,minmax(260px,1fr))">${rows.map((a) => `<div class="card agent-card"><div class="card-b">
      <div style="display:flex;gap:10px;align-items:center"><div class="agent-icon">${AGENT_LETTER[a.role]}</div>
        <div><div style="font-weight:650">${h(a.name)}</div><div class="muted small">${h(AGENT_META[a.role].role)}</div></div>
        <span class="badge ${a.status === "Healthy" ? "b-ok" : a.status === "Running" ? "b-blue" : a.status === "Idle" ? "" : "b-err"}" style="margin-left:auto">● ${h(a.status)}</span></div>
      <div class="small" style="margin-top:10px"><span class="muted">Current task:</span> ${a.current_task ? h(a.current_task) : '<span class="muted">none</span>'}</div>
      <div class="stats">
        <div><div class="k">Tasks completed</div><div class="v">${h(a.tasks)}</div></div>
        <div><div class="k">Success rate</div><div class="v">${a.success_rate == null ? "–" : h(a.success_rate) + "%"}</div></div>
        <div><div class="k">Avg execution time</div><div class="v">${fmtMs(a.avg_ms)}</div></div>
        <div><div class="k">${h(a.handled_label)}</div><div class="v">${h(a.handled)}</div></div>
      </div>
      <div class="muted small" style="margin-top:10px">Runs: ${h(a.by_mode.workflow)} in workflows · ${h(a.by_mode.individual)} individual${a.timeouts ? ` · ${h(a.timeouts)} timeout(s)` : ""}<br>
        Tools: ${h((a.policy.tools || []).join(", ") || "none")} · ${a.policy.llm ? "LLM optional, deterministic fallback" : "deterministic by design"}</div>
      <div style="margin-top:12px"><a class="btn sm" href="#/agent/${a.role}">Run Agent</a></div>
    </div></div>`).join("")}</div>
    <div class="grid g-2" style="margin-top:16px">
      <div class="card"><div class="card-h"><h2>Recent individual executions</h2><span class="right muted small">Individual Agent Mode</span></div>
        ${recent.length ? execTable(recent) : empty("No individual executions yet.", " Choose an agent and click Run Agent.")}</div>
      <div class="card"><div class="card-h"><h2>Orchestration layer (not an AI agent)</h2></div><div class="card-b small">
        Every execution - full workflow or single agent - passes through the orchestrator: validation, tool permissions, retries and fallback, timeouts, step / time / LLM-call budget, audit logging and human approval.
        <div style="margin-top:10px"><b>Full Investigation:</b> <span class="mono">Strategist → Scout → Intelligence → Guardian → Human → Action</span></div>
        <div style="margin-top:4px"><b>Individual Agent:</b> <span class="mono">Orchestrator → selected agent → result</span>. Other agents run only when you choose a handoff.</div></div></div>
    </div>`;
  bindRows();
};

function execTable(rows) {
  return `<div class="table-wrap"><table><thead><tr><th>Execution</th><th>Agent</th><th>Status</th><th>Updated</th></tr></thead><tbody>
    ${rows.slice(0, 12).map((e) => `<tr class="click" data-go="#/execution/${e.id}"><td><div class="mono small">${h(e.code)}${e.parent_id ? ' <span class="muted">· handoff</span>' : ""}</div>
      <div class="small">${h(String(e.question).slice(0, 70))}</div></td><td class="small" style="white-space:nowrap">${h(AGENT_META[e.agent]?.name || e.agent)}</td>
      <td>${execStatus(e)}</td><td class="muted small">${h(ago(e.updated_at))}</td></tr>`).join("")}</tbody></table></div>`;
}
const runningBadge = `<span class="badge b-blue"><span class="spinner" style="width:10px;height:10px;border-width:2px"></span> Running</span>`;

// ---------------------------------------------------------------- individual agent workspace
const QUICK = {
  strategist: [
    ["Plan infrastructure investigation", { task: "Create an investigation plan for repeated network outages at Site A." }],
    ["Plan power anomaly investigation", { task: "Why are repeated power fluctuations happening at Site A, and what should we do?" }],
    ["Plan cooling incident investigation", { task: "Create an investigation plan for the cooling incident and high inlet temperature at Site A." }],
  ],
  scout: [
    ["Fetch latest incidents", { task: "Fetch the latest incident data for Site A.", site: "NOI-DC1", source: "incidents" }],
    ["Retrieve operational data", { task: "Retrieve telemetry and maintenance data for Site A.", site: "NOI-DC1", source: "" }],
    ["Check external source", { task: "Check outdoor weather for Site B.", site: "GGN-EDGE2", source: "weather" }],
  ],
  intelligence: [
    ["Analyze incident timeline", { objective: "Analyze this incident timeline.", records: "10:05 Power fluctuation\n10:08 UPS warning\n10:12 Server warning\n\n10:45 Power fluctuation\n10:48 UPS warning\n10:51 Server warning" }],
    ["Find recurring patterns", { objective: "Find recurring incident patterns.", records: "09:00 CRAC-2 fault alarm\n09:10 Row B inlet temperature high\n09:25 Switch CSW-1 port flap\n\n13:00 CRAC-2 fault alarm\n13:12 Row B inlet temperature high\n13:24 Switch CSW-1 port flap\n\n17:00 CRAC-2 fault alarm\n17:09 Row B inlet temperature high\n17:22 Switch CSW-1 port flap" }],
    ["Identify operational risks", { objective: "Identify operational risks.", records: "08:10 Grid voltage dip\n08:11 UPS on battery\n08:40 Badge reader offline\n11:30 Grid voltage dip\n11:31 UPS on battery\n11:35 Server warning" }],
    ["Find automation opportunities", { objective: "Find automation opportunities.", records: "02:00 Disk latency alert\n02:05 Storage controller warning\n02:20 VM restart\n\n02:00 Disk latency alert\n02:04 Storage controller warning\n02:19 VM restart" }],
  ],
  guardian: [
    ["Verify investigation", { report: "The UPS is definitely defective because UPS warnings happened after two power fluctuations.", evidence: "" }],
    ["Review recommendation", { report: "CSW-1 is faulty and caused the outages. Replace CSW-1 tonight.", evidence: "11 Jun 15:05 CSW-1 port flaps\n13 Jun 14:55 CSW-1 port flaps" }],
    ["Check evidence quality", { report: "Repeated UPS warnings following power fluctuations suggest a UPS-related or upstream power-quality issue may be contributing. UPS diagnostic logs and input voltage measurements are required to confirm the cause.", evidence: "10:05 Power fluctuation\n10:08 UPS warning\n10:45 Power fluctuation\n10:48 UPS warning" }],
  ],
};

async function workspaceForm(agent) {
  const meta = await api("/api/sites");
  const fails = meta.failures[agent] || { normal: "Normal" };
  const sim = Object.keys(fails).length > 1 ? `<label>Simulation mode <span class="muted small" style="font-weight:400">(demo control)</span></label>
    <div class="opts">${Object.entries(fails).map(([k, v], i) => `<label class="opt"><input type="radio" name="failure" value="${h(k)}" ${i === 0 ? "checked" : ""}> ${h(v)}</label>`).join("")}</div>` : "";
  const fields = {
    strategist: `<label for="f-task">Task</label><textarea id="f-task" maxlength="4000" placeholder="e.g. Create an investigation plan for repeated power fluctuations at Site A."></textarea>`,
    scout: `<label for="f-task">Data request</label><textarea id="f-task" maxlength="4000" style="min-height:56px" placeholder="e.g. Fetch the latest incident data for Site A."></textarea>
      <div class="grid g-2e" style="gap:10px">
        <div><label for="f-site">Site</label><select id="f-site" class="input"><option value="">All sites</option>${meta.sites.map((s) => `<option value="${h(s.code)}">${h(s.label)} · ${h(s.code)}</option>`).join("")}</select></div>
        <div><label for="f-source">Preferred source (optional)</label><select id="f-source" class="input"><option value="">Auto (from request)</option>${meta.tools.map((t) => `<option value="${h(t)}">${h(t)}</option>`).join("")}</select></div>
        <div><label for="f-start">From</label><input id="f-start" class="input" type="date" value="${h(meta.window.start)}"></div>
        <div><label for="f-end">To</label><input id="f-end" class="input" type="date" value="${h(meta.window.end)}"></div>
      </div>`,
    intelligence: `<label for="f-records">Operational data <span class="muted small" style="font-weight:400">one timestamped record per line (HH:MM or YYYY-MM-DD HH:MM), or a JSON list</span></label>
      <textarea id="f-records" class="mono" maxlength="20000" style="min-height:170px" placeholder="10:05 Power fluctuation&#10;10:08 UPS warning&#10;10:12 Server warning"></textarea>
      <label for="f-objective">Analysis objective</label><input id="f-objective" class="input" maxlength="300" placeholder="e.g. Find recurring incident patterns">`,
    guardian: `<label for="f-report">Report / conclusion</label><textarea id="f-report" maxlength="4000" placeholder="Paste the conclusion, hypothesis or recommendation to verify."></textarea>
      <label for="f-evidence">Evidence <span class="muted small" style="font-weight:400">(optional; records, measurements or logs it is based on)</span></label>
      <textarea id="f-evidence" class="mono" maxlength="15000" style="min-height:90px"></textarea>`,
  }[agent];
  return `<div class="card"><div class="card-h"><h2>Input / Task</h2></div><div class="card-b ws-form">
      <div class="muted small" style="font-weight:600;margin-bottom:6px">QUICK ACTIONS</div>
      <div class="chips">${QUICK[agent].map(([l], i) => `<button type="button" class="chip" data-quick="${i}">${h(l)}</button>`).join("")}</div>
      ${fields}${sim}
      <div style="display:flex;gap:8px;align-items:center;margin-top:16px">
        <button class="btn primary" id="run-agent">${h(AGENT_META[agent].verb)}</button>
        <span class="muted small">Runs through the orchestrator: validation, tool policy, timeout, budget and audit.</span></div>
    </div></div>`;
}

function readForm(agent) {
  const v = (id) => ($(id) ? $(id).value.trim() : "");
  const failure = $("input[name=failure]:checked") ? $("input[name=failure]:checked").value : "normal";
  if (agent === "strategist") return { task: v("#f-task"), context: {}, failure };
  if (agent === "scout") return { task: v("#f-task"), failure, context: { site: v("#f-site"), preferred_source: v("#f-source"), start: v("#f-start"), end: v("#f-end") } };
  if (agent === "intelligence") return { task: v("#f-objective") || "Analyze operational records", failure, context: { records: v("#f-records"), objective: v("#f-objective") } };
  return { task: "Verify submitted conclusion", failure, context: { report: v("#f-report"), evidence: v("#f-evidence") } };
}

function fillQuick(agent, q) {
  const set = (id, val) => { if ($(id) && val !== undefined) $(id).value = val; };
  set("#f-task", q.task); set("#f-site", q.site); set("#f-source", q.source);
  set("#f-records", q.records); set("#f-objective", q.objective); set("#f-report", q.report); set("#f-evidence", q.evidence);
}

function agentHead(agent, statusHtml, extra = "") {
  const m = AGENT_META[agent];
  return `<div class="inv-head"><div style="flex:1;min-width:260px">
      <div class="code">INDIVIDUAL AGENT MODE <span class="demo-pill" style="margin-left:6px">Demo / Simulated Data</span></div>
      <h1><span class="agent-icon" style="display:inline-grid;width:30px;height:30px;vertical-align:-6px;margin-right:8px">${AGENT_LETTER[agent]}</span>${h(m.name)}</h1>
      <div class="meta"><span class="muted">${h(m.role)}</span> · ${statusHtml}</div>${extra}</div>
    <div style="display:flex;gap:8px"><a class="btn" href="#/agents">All agents</a></div></div>`;
}

ROUTES.agent = async (agent) => {
  if (!Object.hasOwn(AGENT_META, agent || "")) { location.hash = "#/agents"; return; }
  const [form, recent] = await Promise.all([workspaceForm(agent), api(`/api/executions?agent=${agent}`)]);
  view().innerHTML = `${agentHead(agent, '<span class="badge">Idle</span>')}
    <div class="grid ws-grid">${form}
      <div class="stack"><div class="card"><div class="card-h"><h2>Results</h2></div>${empty("No result yet.", ` Enter a task and click ${AGENT_META[agent].verb}.`)}</div>
        <div class="card"><div class="card-h"><h2>Recent ${h(AGENT_META[agent].name)} executions</h2></div>${recent.length ? execTable(recent) : empty("None yet.", "")}</div></div></div>`;
  bindRows();
  $$("[data-quick]").forEach((b) => (b.onclick = () => fillQuick(agent, QUICK[agent][+b.dataset.quick][1])));
  $("#run-agent").onclick = async () => {
    $("#run-agent").disabled = true;
    try {
      const r = await api(`/api/agents/${agent}/run`, { method: "POST", body: readForm(agent) });
      location.hash = `#/execution/${r.id}`;
    } catch (e) { toast(e.message); $("#run-agent").disabled = false; }
  };
};

// ---------------------------------------------------------------- execution result
const EXEC_STATE = {
  Running: ["b-blue", "Agent running"], Completed: ["b-ok", "Agent completed"], Degraded: ["b-warn", "Completed with degraded data"],
  "Insufficient Data": ["b-warn", "Insufficient reliable data"], Failed: ["b-err", "Agent failed"], Timeout: ["b-err", "Timeout reached"],
  "Budget Reached": ["b-err", "Budget reached"], "Validation Failed": ["b-err", "Validation failed"],
};
const execStatus = (ex) => (ex.running ? runningBadge : `<span class="badge ${(EXEC_STATE[ex.status] || [""])[0]}">${h((EXEC_STATE[ex.status] || [0, ex.status])[1])}</span>`);
const list = (items, cls = "") => (items && items.length ? `<ul class="plain ${cls}">${items.map((x) => `<li>${h(x)}</li>`).join("")}</ul>` : '<span class="muted">None</span>');

function renderPlan(p) {
  return `<div class="card-b"><dl class="kv">
      <dt>Objective</dt><dd><b>${h(p.objective_text)}</b></dd>
      <dt>Scope</dt><dd>${h(p.site_labels.join(", "))} · ${h(p.window.start)} → ${h(p.window.end)} · domain <span class="mono">${h(p.domain)}</span> · planned by ${h(p.planned_by)}</dd>
      <dt>Steps</dt><dd><ol style="margin:0;padding-left:18px">${p.steps.map((s) => `<li>${h(s)}</li>`).join("")}</ol></dd>
      <dt>Required data</dt><dd>${list(p.required_data)}</dd>
      <dt>Potential tools</dt><dd>${list(p.potential_tools)}</dd>
      <dt>Risks / unknowns</dt><dd>${list(p.risks)}</dd></dl>
      <div class="muted small" style="margin-top:10px">Plan only: no data was collected and no other agent ran.</div></div>
    <div class="table-wrap" style="border-top:1px solid var(--border)"><table><thead><tr><th>Task</th><th>Delegated to</th><th>Tool</th><th>Why</th></tr></thead><tbody>
      ${p.tasks.map((t) => `<tr><td class="mono small">${h(t.id)}</td><td>Velloe Scout</td><td class="mono small">${h(t.tool)}</td><td class="small">${h(t.reason)}</td></tr>`).join("")}</tbody></table></div>`;
}

function renderData(o) {
  return `${o.message ? `<div class="recovery bad" style="margin:12px 16px 0"><h3>${h(o.message)}</h3>No operational records were returned. Nothing was fabricated.</div>` : ""}
    <div class="card-b stack" style="gap:10px">${o.sources.map((s) => `<div class="hyp">
      <div class="hyp-h"><h3>${h(s.tool_label)}</h3><div class="conf"><span class="badge ${s.status === "ok" ? "b-ok" : s.status === "failed" ? "b-err" : "b-warn"}">${s.status === "degraded_fallback" ? "fallback" : h(s.status)}</span></div></div>
      <dl class="kv" style="margin-top:8px"><dt>Source</dt><dd>${h(s.source)}</dd><dt>Tool</dt><dd class="mono small">${h(s.tool)}</dd>
        <dt>Why selected</dt><dd class="small">${h(s.selection_reason || "Selected from the task and allowed Scout tools.")}</dd>
        <dt>Records</dt><dd>${h(s.records)}</dd><dt>Retries</dt><dd>${h(s.retries)}</dd><dt>Fallback used</dt><dd>${s.fallback_used ? "Yes" : "No"}</dd>
        <dt>Missing fields</dt><dd>${s.missing_fields.length ? s.missing_fields.map((f) => `<span class="ref">${h(f)}</span>`).join(" ") : '<span class="muted">none</span>'}</dd>
        <dt>Quality notes</dt><dd class="small">${h(s.notes)}</dd>
        ${s.errors.length ? `<dt>Errors</dt><dd class="small mono">${s.errors.map(h).join("<br>")}</dd>` : ""}</dl>
      ${(o.preview?.[s.tool] || []).length ? `<div class="table-wrap" style="margin-top:8px"><table><tbody>${o.preview[s.tool].map((r) => `<tr><td class="mono small">${h(r.id)}</td><td class="small">${h(r.time || r.start || r.date || "")}</td><td class="small">${h(r.summary || r.description || (r.value != null ? r.value + " " + (r.metric || "") : r.tmax != null ? `max ${r.tmax} °C / min ${r.tmin} °C` : ""))}</td></tr>`).join("")}</tbody></table>
        ${s.records > o.preview[s.tool].length ? `<div class="muted small" style="padding:4px 0">Showing ${o.preview[s.tool].length} of ${h(s.records)} records.</div>` : ""}</div>` : ""}
    </div>`).join("")}</div>`;
}

function renderAnalysis(o) {
  const r = o.result;
  return `<div class="card-b"><p style="margin:0 0 10px">${citeHtml(o.summary)}</p>
      <div class="muted small">Basis: ${h(o.basis)} · Confidence: ${h(r.confidence_method || "evidence-weight score (heuristic), not a probability")}${o.revision_of ? " · revised after Guardian feedback" : ""}</div></div>
    <div class="card-b" style="border-top:1px solid var(--border)"><div class="muted small" style="font-weight:600;margin-bottom:6px">EVIDENCE ASSESSMENT</div><span class="badge ${r.evidence_assessment?.status === "INSUFFICIENT EVIDENCE" ? "b-err" : "b-ok"}">${h(r.evidence_assessment?.status || "Available")}</span>${r.evidence_assessment?.status === "INSUFFICIENT EVIDENCE" ? `<div class="small" style="margin-top:6px"><b>Known:</b> ${h((r.evidence_assessment.known || []).join("; ") || "No matching fact.")}<br><b>Uncertain:</b> ${h((r.evidence_assessment.uncertain || []).join("; "))}<br><b>Next evidence:</b> ${h((r.evidence_assessment.recommended_next_evidence || []).join("; "))}</div>` : ""}</div>
    <div class="card-b" style="border-top:1px solid var(--border)"><div class="muted small" style="font-weight:600;margin-bottom:6px">OBSERVATIONS</div>
      ${r.patterns.map((p) => `<div class="small" style="padding:3px 0"><span class="badge">${h(p.kind)}</span> ${h(p.statement)} ${refs(p.refs.slice(0, 5))}</div>`).join("") || '<div class="muted small">No pattern found.</div>'}
      ${r.records ? `<details style="margin-top:8px"><summary class="small">Parsed records (${r.records.length})</summary><div class="table-wrap"><table><tbody>${r.records.map((x) => `<tr><td class="mono small">${h(x.id)}</td><td class="mono small">${h(x.time)}</td><td class="small">${h(x.label)}</td><td class="small muted">${h(x.component)}</td></tr>`).join("")}</tbody></table></div></details>` : ""}</div>
    <div class="card-b" style="border-top:1px solid var(--border)"><div class="muted small" style="font-weight:600;margin-bottom:6px">POSSIBLE HYPOTHESES</div>${r.hypotheses.map(hypothesisCard).join("")}</div>
    <div class="table-wrap" style="border-top:1px solid var(--border)"><table><thead><tr><th>Recommended action</th><th>Priority</th><th>Owner</th><th>Effort</th><th>Approval</th></tr></thead><tbody>
      ${r.actions.map((a) => `<tr><td><div style="font-weight:600;font-size:13px">${h(a.recommended)}</div><div class="small"><b>Why:</b> ${h(a.reason)}</div><div class="muted small"><b>Priority:</b> ${h(a.priority_reason || "Based on impact and confidence.")}</div>${a.opportunity ? `<div class="small muted">◆ ${h(a.opportunity)}</div>` : ""}</td>
        <td>${prio(a.priority)}</td><td class="small">${h(a.owner)}</td><td class="small">${h(a.effort)}</td><td class="small">${a.requires_approval ? "Human approval required" : "Not required"}</td></tr>`).join("")}</tbody></table></div>
    <div class="card-b small" style="border-top:1px solid var(--border)"><b>Missing evidence / unknowns</b>${r.unknowns.map((u) => `<div style="padding:2px 0">? ${h(u.what)}</div>`).join("")}
      ${r.risks.map((x) => `<div style="padding:2px 0">⚠ ${h(x.risk)}</div>`).join("")}
      <div class="muted" style="margin-top:6px">Recommendations are proposals. Nothing is created until Guardian verifies and a human approves.</div></div>`;
}

function renderVerdict(o) {
  const v = o.verdict, ok = v.verdict === "APPROVED";
  const sec = (t, body) => `<dt>${t}</dt><dd>${body}</dd>`;
  return `<div class="card-b"><div class="verdict ${ok ? "ok" : "bad"}">VERDICT: ${h(v.verdict)}</div>
      <div class="muted small" style="margin:6px 0 12px">Reviewed: ${o.reviewed.kind === "text" ? "submitted conclusion" : `Intelligence result <a href="#/execution/${o.reviewed.execution}">${h(o.reviewed.code)}</a>`} · verdict returned to the user, no automatic revision</div>
      ${o.reviewed.kind === "text" ? `<blockquote class="quote">${h(o.reviewed.report)}</blockquote>` : ""}
      <dl class="kv">${sec("Reason", h(v.reason))}
        ${!ok ? sec("Unsupported claims", list(v.unsupported_claims)) : ""}
        ${sec("Missing evidence", list(v.missing_evidence))}
        ${sec("Risk", h(v.risk))}
        ${!ok ? sec("Required revision", list(v.required_revision)) : ""}</dl></div>
    <div class="card-b" style="border-top:1px solid var(--border)"><ul class="checks">${v.checks.map((c) => `<li><span style="color:var(--${c.passed ? "ok" : "err"});font-weight:700">${c.passed ? "✓" : "✗"}</span><span><b>${h(c.check.replace(/_/g, " "))}</b> <span class="muted">${h(c.detail)}</span></span></li>`).join("")}</ul></div>`;
}

function stateBanner(ex) {
  const msg = {
    Timeout: ["Timeout reached", "The orchestrator stopped waiting for the agent. Partial results were discarded; nothing was fabricated."],
    "Budget Reached": ["Budget reached", "The execution hit a step, time or LLM-call limit and stopped safely."],
    "Validation Failed": ["Validation failed", "The input was rejected before the agent ran."],
    Failed: ["Agent failed", "An unexpected error occurred. It is recorded in the audit trail."],
  }[ex.status];
  return msg ? `<div class="recovery bad" style="margin-bottom:16px" role="alert"><h3>${msg[0]}</h3>${h(ex.error || msg[1])}</div>` : "";
}

ROUTES.execution = async (id) => {
  const nav = state.nav;
  const draw = async () => {
    const ex = await api(`/api/executions/${encodeURIComponent(id)}`);
    if (!isCurrent(nav)) return false;
    const o = ex.output;
    const body = !o ? (ex.running ? `<div class="card-b"><span class="spinner"></span> ${h(AGENT_META[ex.agent].name)} is running…</div>` : empty("No result.", " See the audit events below."))
      : { plan: () => renderPlan(o.plan), data: () => renderData(o), analysis: () => renderAnalysis(o), verdict: () => renderVerdict(o) }[o.type]();
    const b = ex.budget || {};
    view().innerHTML = `${agentHead(ex.agent, execStatus(ex), `<div class="muted small" style="margin-top:6px"><span class="mono">${h(ex.code)}</span> · “${h(String(ex.question).slice(0, 160))}”
        ${ex.parent ? ` · handoff from <a href="#/execution/${ex.parent.id}">${h(ex.parent.code)}</a>` : ""}</div>`)}
      ${stateBanner(ex)}${recoveryBanner(ex)}
      <div class="grid g-2">
        <div class="stack">
          <div class="card"><div class="card-h"><h2>Results</h2>${o && o.type === "verdict" ? "" : ""}<span class="right">${execStatus(ex)}</span></div>${body}</div>
          ${ex.handoffs.length ? `<div class="card"><div class="card-h"><h2>Optional handoff</h2><span class="right muted small">user-controlled · audited</span></div><div class="card-b">
            <div style="display:flex;gap:8px;flex-wrap:wrap">${ex.handoffs.map((x) => `<button class="btn ${x.target === "action" || x.target === "guardian" ? "primary" : ""}" data-handoff="${h(x.target)}">${h(x.label)}</button>`).join("")}</div>
            <div class="muted small" style="margin-top:8px">Individual Agent Mode never chains agents automatically. Each handoff starts one new execution through the orchestrator.</div></div></div>` : ""}
          ${ex.actions.length ? `<div class="card"><div class="card-h"><h2>Recommended actions</h2><a class="right small" href="#/board">Action Board →</a></div><div class="table-wrap"><table><tbody>
            ${ex.actions.map((a) => `<tr><td><div style="font-weight:600;font-size:13px">${h(a.recommended)}</div><div class="small"><b>Why:</b> ${h(a.reason)}</div><div class="muted small"><b>Priority:</b> ${h(a.priority_reason || "Based on impact and confidence.")} · ${h(a.code)} · ${h(a.owner)} · effort ${h(a.effort)}</div></td><td>${prio(a.priority)}</td>
              <td>${status(a.status)}${a.ticket_ref ? `<div class="small" style="color:var(--ok);margin-top:4px">✓ ${h(a.ticket_ref)} (simulated)</div>` : ""}</td>
              <td>${a.status === "Awaiting Approval" ? `<button class="btn sm primary" data-approve="${a.id}">Approve Action</button>` : ""}</td></tr>`).join("")}</tbody></table></div></div>` : ""}
        </div>
        <div class="stack">
          <div class="card"><div class="card-h"><h2>Execution details</h2></div><div class="card-b"><dl class="kv">
            <dt>Execution ID</dt><dd class="mono">${h(ex.code)}</dd><dt>Mode</dt><dd class="mono">INDIVIDUAL_AGENT</dd>
            <dt>Agent</dt><dd>${h(AGENT_META[ex.agent].name)}</dd><dt>Status</dt><dd>${execStatus(ex)}</dd>
            <dt>Simulation</dt><dd>${h(ex.faults || "none")}</dd><dt>Duration</dt><dd>${b.elapsed_seconds != null ? h(b.elapsed_seconds) + " s" : "–"}</dd>
            <dt>Tool calls</dt><dd>${h(ex.tool_calls.length)} (${h(ex.tool_calls.filter((t) => t.outcome !== "ok" && t.outcome !== "fallback_ok").length)} failed attempts)</dd>
            <dt>LLM calls</dt><dd>${h(b.llm_calls ?? 0)}</dd><dt>Budget used</dt><dd>${b.budget_used_pct != null ? h(b.budget_used_pct) + "%" : "–"}</dd></dl>
            ${ex.children.length ? `<div class="small" style="margin-top:10px"><b>Handoffs from here:</b> ${ex.children.map((c) => `<a href="#/execution/${c.id}">${h(c.code)}</a> (${h(AGENT_META[c.agent]?.name || c.agent)})`).join(", ")}</div>` : ""}
            <div style="margin-top:12px;display:flex;gap:8px"><a class="btn sm" href="#/agent/${ex.agent}">Run again</a><a class="btn sm" href="#/audit/${ex.id}">Open in Audit Trail</a></div></div></div>
          <div class="card"><div class="card-h"><h2>Audit events</h2>${ex.running ? '<span class="right badge b-blue"><span class="dot live"></span> Live</span>' : ""}</div>${activityFeed(ex.audit.slice().reverse())}</div>
        </div>
      </div>`;
    $$("[data-handoff]").forEach((btn) => (btn.onclick = async () => {
      $$("[data-handoff]").forEach((x) => (x.disabled = true));
      try {
        const r = await api(`/api/executions/${ex.id}/handoff`, { method: "POST", body: { target: btn.dataset.handoff } });
        if (r.actions) { toast(`${r.actions.length} action(s) created · operational actions await human approval`); draw(); } else location.hash = `#/execution/${r.id}`;
      } catch (e) { toast(e.message); draw(); }
    }));
    $$("[data-approve]").forEach((btn) => (btn.onclick = () => approveDialog(ex.actions.find((a) => String(a.id) === btn.dataset.approve), draw)));
    return ex.running;
  };
  const tick = async () => { if (!(await draw())) stopPolling(); };
  if (await draw()) poll(tick, 800, nav);
};

// ---------------------------------------------------------------- audit trail
const AUDIT_AGENTS = [["", "All agents"], ["orchestrator", "Orchestrator"], ["strategist", "Strategist"], ["scout", "Scout"], ["intelligence", "Intelligence"], ["guardian", "Guardian"], ["human", "Human"]];
const modeBadge = (m) => (m === "INDIVIDUAL_AGENT" ? '<span class="badge b-accent">INDIVIDUAL</span>' : '<span class="badge">WORKFLOW</span>');
const optionList = (items, selected) => items.map(([v, l]) => `<option value="${h(v)}" ${v === selected ? "selected" : ""}>${h(l)}</option>`).join("");
const fullStamp = (iso) => iso ? new Date(iso).toLocaleString([], { year: "numeric", month: "short", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit" }) : "–";
function prettyJson(value) {
  if (!value) return "–";
  try { return JSON.stringify(JSON.parse(value), null, 2); } catch (_) { return String(value); }
}

ROUTES.audit = async (invId) => {
  const filters = { search: "", agent: "", mode: "", status: "", event: "", investigation: invId || "", execution: "", from: "", to: "" };
  const draw = async () => {
    const query = new URLSearchParams(Object.entries(filters).filter(([, value]) => value));
    const rows = await api(`/api/audit?${query.toString()}`);
    view().innerHTML = `<div class="page-head"><div><h1>Audit Trail</h1><p>Search and filter every agent decision, tool failure, recovery, execution, and approval.</p></div>
        <div class="actions">${invId ? '<a class="btn" href="#/audit">All events</a>' : ""}<button class="btn" id="audit-refresh">Refresh</button></div></div>
      <div class="card" style="margin-bottom:12px"><form class="card-b filter-grid" id="audit-filters">
        <div class="filter-search"><label for="af-search">Search</label><input class="input" id="af-search" value="${h(filters.search)}" placeholder="Agent, action, reason, code…"></div>
        <div><label for="af-agent">Agent</label><select class="input" id="af-agent">${optionList(AUDIT_AGENTS, filters.agent)}</select></div>
        <div><label for="af-mode">Mode</label><select class="input" id="af-mode">${optionList([["", "All modes"], ["FULL_WORKFLOW", "Full investigation"], ["INDIVIDUAL_AGENT", "Individual agent"]], filters.mode)}</select></div>
        <div><label for="af-status">Status</label><select class="input" id="af-status">${optionList([["", "All statuses"], ["ok", "OK"], ["warn", "Warning"], ["recovered", "Recovered"], ["rejected", "Rejected"], ["error", "Error"], ["stopped", "Stopped"]], filters.status)}</select></div>
        <div><label for="af-event">Event</label><select class="input" id="af-event">${optionList([["", "All events"], ["failure", "Failure"], ["recovery", "Recovery"]], filters.event)}</select></div>
        <div><label for="af-investigation">Investigation ID</label><input class="input" id="af-investigation" inputmode="numeric" value="${h(filters.investigation)}" placeholder="e.g. 12"></div>
        <div><label for="af-execution">Execution ID</label><input class="input" id="af-execution" inputmode="numeric" value="${h(filters.execution)}" placeholder="e.g. 18"></div>
        <div><label for="af-from">From date</label><input class="input" id="af-from" type="date" value="${h(filters.from)}"></div>
        <div><label for="af-to">To date</label><input class="input" id="af-to" type="date" value="${h(filters.to)}"></div>
        <div class="filter-actions"><button class="btn primary" type="submit">Apply filters</button><button class="btn" type="button" id="audit-clear">Clear</button></div>
      </form></div>
      <div class="card">${rows.length ? `<div class="table-wrap"><table><thead><tr><th>Timestamp</th><th>Mode</th><th>Agent</th><th>Action</th><th>Status</th><th>Duration</th><th>Retry / Revision</th></tr></thead>
        <tbody>${rows.map((a, i) => `<tr class="click" data-row="${i}" aria-expanded="false"><td class="small"><div>${h(fullStamp(a.timestamp || a.ts))}</div><div class="muted mono">${h(a.code)}</div></td>
          <td>${modeBadge(a.mode)}${a.fallback_used ? '<div class="small" style="color:var(--warn)">fallback</div>' : ""}</td>
          <td style="font-weight:600;white-space:nowrap">${h(a.agent)}</td><td><div class="mono small">${h(a.action)}</div><div class="muted small">${h(String(a.reason || "").slice(0, 90))}</div></td>
          <td>${auditStatus(a.status)}</td><td class="small">${a.duration_ms != null ? h(a.duration_ms) + " ms" : "–"}</td><td class="small">${h(a.retry_count || 0)} / ${h(a.revision_count || 0)}</td></tr>
          <tr class="detail" data-detail="${i}" hidden><td colspan="7"><div class="small muted">execution ${h(a.execution_id || a.investigation_id)} · investigation ${h(a.investigation_id)} · mode ${h(a.mode)} · tool ${h(a.tool || "–")} · retry ${h(a.retry_count || 0)} · revision ${h(a.revision_count || 0)} · fallback ${a.fallback_used ? "yes" : "no"} · budget ${a.budget_pct != null ? h(a.budget_pct) + "%" : "–"}
            · <a href="#/${a.mode === "INDIVIDUAL_AGENT" ? "execution" : "investigation"}/${a.investigation_id}">open execution</a></div>
            <div class="audit-detail-grid"><div><b>Reason</b><pre>${h(a.reason || "–")}</pre><b>Input</b><pre>${h(a.input)}</pre><b>Output</b><pre>${h(a.output)}</pre></div>
            <div><b>Request</b><pre>${h(prettyJson(a.request_json))}</pre><b>Response status</b><pre>${h(a.response_status || "–")}</pre><b>Validation</b><pre>${h(a.validation || "–")}</pre><b>Normalized result</b><pre>${h(prettyJson(a.normalized_result_json))}</pre></div></div></td></tr>`).join("")}</tbody></table></div>`
        : empty("No audit events match these filters.", " Change or clear filters, then try again.")}</div>`;
    const value = (id) => $(id).value.trim();
    $("#audit-filters").onsubmit = (event) => { event.preventDefault(); Object.assign(filters, { search: value("#af-search"), agent: value("#af-agent"), mode: value("#af-mode"), status: value("#af-status"), event: value("#af-event"), investigation: value("#af-investigation"), execution: value("#af-execution"), from: value("#af-from"), to: value("#af-to") }); draw(); };
    $("#audit-clear").onclick = () => { Object.keys(filters).forEach((key) => (filters[key] = "")); draw(); };
    $$('[data-row]').forEach((row) => (row.onclick = () => { const detail = $(`[data-detail="${row.dataset.row}"]`); detail.hidden = !detail.hidden; row.setAttribute("aria-expanded", String(!detail.hidden)); }));
    $("#audit-refresh").onclick = draw;
  };
  await draw();
};

// ---------------------------------------------------------------- analytics
function hbars(obj, total, color = "") {
  const entries = Object.entries(obj);
  if (!entries.length) return '<div class="muted small">No data yet.</div>';
  const max = total || Math.max(...entries.map(([, v]) => v), 1);
  return entries.map(([k, v]) => `<div class="hbar"><span>${h(k)}</span><div class="bar ${color}"><i style="width:${(v / max) * 100}%"></i></div><span class="small" style="text-align:right">${h(v)}</span></div>`).join("");
}

ROUTES.analytics = async () => {
  const a = await api("/api/analytics");
  const tools = Object.entries(a.tools);
  const scenarios = a.benchmark.latest?.results || [];
  view().innerHTML = `<div class="page-head"><div><h1>Analytics</h1><p>Calculated from stored runs in the local database. Benchmark runs are excluded and shown separately.</p></div>
      <div class="actions"><span class="demo-pill">Demo dataset</span></div></div>
    <div class="grid g-kpi" style="margin-bottom:16px">
      <div class="card kpi"><div class="label">Investigation completion rate</div><div class="value">${a.completion.rate == null ? "–" : a.completion.rate + "%"}</div><div class="sub">${a.completion.completed} of ${a.completion.finished} finished runs reached a verdict</div></div>
      <div class="card kpi"><div class="label">Guardian rejection rate</div><div class="value">${a.guardian.rate == null ? "–" : a.guardian.rate + "%"}</div><div class="sub">${a.guardian.rejected} of ${a.guardian.reviews} reviews rejected</div></div>
      <div class="card kpi"><div class="label">Tool failures</div><div class="value">${h(a.tool_failures)}</div><div class="sub">Failed or unrecovered tool attempts</div></div>
      <div class="card kpi"><div class="label">Recovery count</div><div class="value">${h(a.recovery_count)}</div><div class="sub">Fallbacks activated successfully</div></div>
      <div class="card kpi"><div class="label">Simulated benchmark</div><div class="value">${a.benchmark.latest ? h(a.benchmark.latest.completion_rate) + "%" : "–"}</div><div class="sub">${a.benchmark.latest ? `latest benchmark #${h(a.benchmark.latest.id)}: ${h(a.benchmark.latest.completed_runs)}/${h(a.benchmark.latest.n_runs)} expectations passed (${h(a.benchmark.invocations)} stored)` : "Run python benchmark.py"}</div></div>
    </div>
    <div class="grid g-2e">
      <div class="card"><div class="card-h"><h2>Investigations by status</h2></div><div class="card-b">${hbars(a.investigations_by_status)}</div></div>
      <div class="card"><div class="card-h"><h2>Incident priority distribution</h2><span class="right muted small">unique incidents collected</span></div><div class="card-b">${hbars(a.incident_priority)}</div></div>
      <div class="card"><div class="card-h"><h2>Agent execution time</h2><span class="right muted small">avg ms per task</span></div><div class="card-b">${hbars(a.agent_ms)}</div></div>
      <div class="card"><div class="card-h"><h2>Tool failure / recovery</h2></div>
        ${tools.length ? `<div class="table-wrap"><table><thead><tr><th>Tool</th><th>OK</th><th>Failed attempts</th><th>Recovered</th><th>Unrecovered</th></tr></thead><tbody>
        ${tools.map(([t, v]) => `<tr><td style="font-weight:600">${h(t)}</td><td>${v.ok}</td><td>${v.failures}</td><td>${v.recovered}</td><td>${v.failed}</td></tr>`).join("")}</tbody></table></div>` : empty("No tool calls yet.", "")}</div>
    </div>
    <div class="grid g-2e" style="margin-top:16px">
      <div class="card"><div class="card-h"><h2>Top Operational Risks</h2><span class="right muted small">rolled up from action risk fields</span></div>
        <div class="card-b">${(a.operational_risks || []).length ? (a.operational_risks || []).map((r) => `<div style="padding:6px 0;border-bottom:1px dashed var(--border)">
          <div style="display:flex;gap:8px;align-items:flex-start"><span style="color:var(--warn);font-weight:700">⚠</span><div style="flex:1"><div class="small">${h(r.risk)}</div>
          <div class="muted small" style="margin-top:3px">${prio(r.top_priority)} · flagged in ${h(r.count)} action${r.count === 1 ? "" : "s"} · ${r.investigations.map((c) => `<span class="ref">${h(c)}</span>`).join(" ")}</div></div></div></div>`).join("")
          : empty("No operational risks yet.", " Risks are generated when an investigation produces recommendations.")}</div></div>
      <div class="card"><div class="card-h"><h2>Business Opportunities</h2><span class="right muted small">automation & integration openings</span></div>
        <div class="card-b">${(a.business_opportunities || []).length ? (a.business_opportunities || []).map((o) => `<div style="padding:6px 0;border-bottom:1px dashed var(--border)">
          <div style="display:flex;gap:8px;align-items:flex-start"><span style="color:var(--accent);font-weight:700">◆</span><div style="flex:1"><div class="small">${h(o.opportunity)}</div>
          <div class="muted small" style="margin-top:3px">Identified in ${h(o.count)} action${o.count === 1 ? "" : "s"} · ${o.investigations.map((c) => `<span class="ref">${h(c)}</span>`).join(" ")}</div></div></div></div>`).join("")
          : empty("No business opportunities yet.", " Opportunities are generated alongside recommended actions.")}</div></div>
    </div>
    <div class="card" style="margin-top:16px"><div class="card-h"><h2>Benchmark methodology</h2><span class="right demo-pill">Simulated benchmark</span></div><div class="card-b small">${h(a.benchmark.methodology)}</div>
      ${scenarios.length ? `<div class="table-wrap"><table><thead><tr><th>Scenario</th><th>Expected</th><th>Status</th><th>Recovery</th><th>Retries</th><th>Rejections</th><th>Time</th><th>Result</th></tr></thead><tbody>${scenarios.map((s) => `<tr><td>${h(s.scenario || s.run)}</td><td>${h(s.expectation || "completion")}</td><td>${status(s.status)}</td><td>${s.recovered ? "Yes" : "No"}</td><td>${h(s.retries ?? "–")}</td><td>${h(s.rejections ?? "–")}</td><td>${h(s.seconds)} s</td><td><span class="badge ${s.passed === false ? "b-err" : "b-ok"}">${s.passed === false ? "Failed" : "Passed"}</span></td></tr>`).join("")}</tbody></table></div>` : ""}</div>`;
};

// ---------------------------------------------------------------- settings
// Plain-language labels for every runtime limit; unknown keys still render with their raw name.
const LIMIT_INFO = {
  max_workflow_steps: ["Workflow", "Max workflow steps", "Audit entries allowed before the run stops safely"],
  max_steps: ["Workflow", "Max steps", "Step budget per run"],
  execution_timeout: ["Workflow", "Execution timeout", "Wall-clock limit for one investigation", "s"],
  max_seconds: ["Workflow", "Max run time", "Hard wall-clock budget", "s"],
  max_llm_calls: ["Workflow", "Max LLM calls", "Real model calls allowed per run"],
  max_guardian_revisions: ["Verification", "Guardian revisions", "Bounded re-analysis cycles after a rejection"],
  max_revisions: ["Verification", "Max revisions", "Revision cycles before Needs Review"],
  max_tool_retries: ["Scout reliability", "Tool retries", "Retries before switching to the fallback source"],
  scout_attempts: ["Scout reliability", "Attempts per source", "First try + retries"],
  scout_timeout_s: ["Scout reliability", "Tool timeout", "Deadline for one data-source call", "s"],
  thermal_threshold_c: ["Analysis", "Thermal threshold", "Inlet temperature treated as an anomaly", "°C"],
  demo_step_delay_s: ["Demo", "Step delay", "Pause between audit steps so viewers can follow", "s"],
};

function limitRows(limits) {
  const groups = {};
  Object.entries(limits || {}).forEach(([k, v]) => {
    const [g, label, desc, unit] = LIMIT_INFO[k] || ["Other", k.replace(/_/g, " "), "", ""];
    (groups[g] = groups[g] || []).push(`<div class="set-row"><div class="k">${h(label)}</div><div class="v">${h(v)}${unit ? " " + h(unit) : ""}</div><div class="d">${h(desc)}</div></div>`);
  });
  return Object.entries(groups).map(([g, rows]) => `<div class="set-group">${h(g)}</div>${rows.join("")}`).join("");
}

ROUTES.settings = async () => {
  const s = await api("/api/settings");
  const theme = document.documentElement.dataset.theme || "light";
  const live = s.provider !== "mock";
  const chain = s.chain || [];
  const db = s.database || {};
  const primary = chain[0];
  view().innerHTML = `<div class="page-head"><div><h1>Settings</h1><p>Runtime configuration. Values come from <span class="mono">.env</span>; API keys are never shown here.</p></div></div>
    <div class="set-strip">
      <div class="card kpi"><div class="label">AI mode</div><div class="value">${live ? '<span class="badge b-ok">● Live models</span>' : '<span class="badge">Deterministic (mock)</span>'}</div><div class="sub">${live ? `${h(chain.length)} provider${chain.length === 1 ? "" : "s"} in chain` : "No model calls are made"}</div></div>
      <div class="card kpi"><div class="label">Primary model</div><div class="value mono">${h(primary ? primary.label : "–")}</div><div class="sub">${primary && primary.cooling_down_s ? `Skipped for ${h(primary.cooling_down_s)} s after a failure` : "Tried first"}</div></div>
      <div class="card kpi"><div class="label">Database</div><div class="value mono">${h(db.path || "–")}</div><div class="sub">${db.rows ? `${h(db.rows.investigations)} runs · ${h(db.rows.audit_logs)} audit entries` : h(db.error || "")}</div></div>
      <div class="card kpi"><div class="label">Server</div><div class="value mono">${h(s.bind)}</div><div class="sub">Local only · no authentication</div></div>
    </div>
    <div class="set-grid">
      <div class="stack">
        <div class="card"><div class="card-h"><h2>AI models</h2><span class="right muted small">first success wins</span></div><div class="card-b">
          ${chain.length ? chain.map((c, i) => `<div class="chain-item"><span class="n">${i + 1}</span><div><div class="lbl">${h(c.label)}</div><div class="muted small">${h(c.role === "primary" ? "Primary" : "Fallback")}</div></div>
            <span class="badge ${c.cooling_down_s ? "b-warn" : "b-ok"}">${c.cooling_down_s ? `cooling down ${h(c.cooling_down_s)} s` : "ready"}</span></div>`).join("")
            : empty("No model configured.", " Set LLM_BASE_URL / LLM_MODEL (and optional LLM_FALLBACK_*) in .env.")}
          <div class="chain-item" style="margin-top:8px"><span class="n">${chain.length + 1}</span><div><div class="lbl">deterministic fallback</div><div class="muted small">Built-in answers if every model fails</div></div><span class="badge">always</span></div>
          <div style="display:flex;gap:8px;align-items:center;margin-top:14px;flex-wrap:wrap">
            <button class="btn primary" id="llm-test" ${live ? "" : "disabled"}>Test connection</button>
            <span class="muted small">Sends one tiny request through the chain.</span></div>
          <div class="test-result" id="llm-result" aria-live="polite"></div>
        </div></div>
        <div class="card"><div class="card-h"><h2>Safety & execution limits</h2><span class="right muted small">read-only</span></div><div class="card-b">${limitRows(s.limits)}</div></div>
      </div>
      <div class="stack">
        <div class="card"><div class="card-h"><h2>Appearance</h2></div><div class="card-b">
          <div class="seg" role="group" aria-label="Theme"><button data-theme="light" aria-pressed="${theme === "light"}">☀ Light</button><button data-theme="dark" aria-pressed="${theme === "dark"}">☾ Dark</button></div>
          <div class="muted small" style="margin-top:8px">Saved in this browser.</div></div></div>
        <div class="card"><div class="card-h"><h2>Human approval</h2></div><div class="card-b small">
          <div class="set-row"><div class="k">Approval gate</div><div class="v">always on</div><div class="d">Operational actions wait for a person</div></div>
          <div class="set-row"><div class="k">Tickets</div><div class="v">simulated</div><div class="d">Approval creates a local MT-xxxx record only</div></div>
          <div class="set-row"><div class="k">Reject reason</div><div class="v">required</div><div class="d">Every rejection is audited with its rationale</div></div></div></div>
        <div class="card"><div class="card-h"><h2>Database</h2></div><div class="card-b small">
          ${db.rows ? Object.entries(db.rows).map(([k, v]) => `<div class="set-row"><div class="k">${h(k.replace(/_/g, " "))}</div><div class="v">${h(v)}</div><div class="d"></div></div>`).join("")
            : `<div class="muted">${h(db.error || "Unavailable")}</div>`}
          ${db.journal_mode ? `<div class="muted" style="margin-top:8px">SQLite · ${h(db.journal_mode)} · schema v${h(db.schema_version)}</div>` : ""}</div></div>
        <div class="card danger-zone"><div class="card-h"><h2>Danger zone</h2></div><div class="card-b small">
          <p style="margin-top:0">Deletes every stored investigation, action, approval and audit entry in <span class="mono">${h(db.path || "the local database")}</span>. Cannot be undone.</p>
          <button class="btn danger" id="reset-btn">Delete all stored runs</button></div></div>
      </div>
    </div>`;
  $$("[data-theme]").forEach((b) => (b.onclick = () => {
    document.documentElement.dataset.theme = b.dataset.theme;
    localStorage.setItem("velloe-theme", b.dataset.theme);
    $$("[data-theme]").forEach((x) => x.setAttribute("aria-pressed", String(x === b)));
  }));
  const test = $("#llm-test");
  if (test) test.onclick = async () => {
    test.disabled = true;
    $("#llm-result").innerHTML = '<span class="spinner"></span> Testing…';
    try {
      const r = await api("/api/llm/test", { method: "POST", body: {} });
      $("#llm-result").innerHTML = r.live
        ? `<span class="badge b-ok">✓ Working</span> Answered by <b class="mono">${h(r.provider)}</b> in ${h(r.seconds)} s.${r.skipped.length ? `<div class="muted small" style="margin-top:4px">Skipped: ${r.skipped.map(h).join("; ")}</div>` : ""}`
        : `<span class="badge b-err">✗ No model answered</span> ${h(r.error || "Using the deterministic fallback.")}`;
    } catch (e) { $("#llm-result").innerHTML = `<span class="badge b-err">Error</span> ${h(e.message)}`; }
    test.disabled = false;
  };
  $("#reset-btn").onclick = async () => {
    if (!confirm("Delete all stored investigations, actions and audit logs from the local database?")) return;
    try { await api("/api/reset", { method: "POST", body: { confirm: "RESET" } }); toast("Local demo data cleared"); refreshShell(); ROUTES.settings(); } catch (e) { toast(e.message); }
  };
};
