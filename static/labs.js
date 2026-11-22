// EmergencySOS console - Deadlock Lab (OS) and Database (DBMS) tabs.
// Uses the helpers from app.js ($, api, esc, nice, table, toast, withButton, state).
"use strict";

const SHORT_RES = { ICU_BED: "ICU", VENTILATOR: "Vent", OPERATION_THEATRE: "OT", GENERAL_BED: "Gen", AMBULANCE: "Amb" };
const vec = (v, types) => types.map((t) => `<span class="vec-item"><i>${SHORT_RES[t]}</i>${v?.[t] ?? 0}</span>`).join("");

function fillSelect(sel, options, keep = true) {
  const old = sel.value;
  sel.innerHTML = options.map((o) => `<option value="${esc(o.value)}">${esc(o.label)}</option>`).join("");
  if (keep && [...sel.options].some((o) => o.value === old)) sel.value = old;
}

// ============================================================
// Banker's algorithm
// ============================================================
async function loadOsLab() {
  const [{ hospitals }, { queue }] = await Promise.all([api("/api/hospitals"), api("/api/queue")]);
  state.labHospitals = hospitals;
  fillSelect($("#bkHospital"), hospitals.map((h) => ({ value: h.hospital_id, label: h.name })));
  fillSelect($("#bkRequest"), [{ value: "", label: "nobody - check the current state" }].concat(
    queue.map((q) => ({ value: q.request_id, label: `#${q.request_id} ${q.patient_name} (${q.emergency_type.toLowerCase()}, needs ${SHORT_RES[q.required_resource] || q.required_resource})` }))));
  $("#bankersMode").innerHTML = state.meta.bankers
    ? '<span class="tag ok">checked on every dispatch</span>'
    : '<span class="tag warn">off for dispatch (BANKERS_CHECK=0)</span>';
  buildOverride();
}

function buildOverride() {
  const h = (state.labHospitals || []).find((x) => x.hospital_id === +$("#bkHospital").value);
  if (!h) return;
  const order = ["ICU_BED", "VENTILATOR", "OPERATION_THEATRE", "GENERAL_BED", "AMBULANCE"];
  const res = [...h.resources].sort((a, b) => order.indexOf(a.resource_type) - order.indexOf(b.resource_type));
  $("#bkOverride").innerHTML = res.map((r) => `<label class="inline-label">${esc(RESOURCE_NAMES[r.resource_type] || r.resource_type)}
    <input type="number" min="0" class="narrow" data-override="${r.resource_type}" placeholder="${r.available_units}" title="Real value: ${r.available_units} free"></label>`).join("")
    + '<span class="muted small">Leave empty = the real number.</span>';
}
$("#bkHospital").addEventListener("change", () => { buildOverride(); $("#bkResult").innerHTML = ""; });

$("#bkRun").addEventListener("click", (e) => withButton(e.target, async () => {
  const available = {};
  document.querySelectorAll("#bkOverride [data-override]").forEach((i) => { if (i.value !== "") available[i.dataset.override] = +i.value; });
  const res = await api("/api/bankers", { method: "POST", body: {
    hospital_id: +$("#bkHospital").value, request_id: $("#bkRequest").value || null, available } });
  renderBankers(res);
}));

function renderBankers(r) {
  const types = r.types;
  const procs = r.processes.concat(r.new_process ? [r.new_process] : []);
  const need = (p) => Object.fromEntries(types.map((t) => [t, Math.max(0, (p.max[t] || 0) - (p.allocation[t] || 0))]));
  const head = (label) => `<th colspan="${types.length}" class="grp">${label}</th>`;
  const sub = types.map((t) => `<th class="num">${SHORT_RES[t]}</th>`).join("");
  const cells = (v) => types.map((t) => `<td class="num">${v[t] || 0}</td>`).join("");
  const blocked = new Set(r.blocked);
  const rows = procs.map((p) => `<tr class="${p.is_new ? "new-proc" : ""} ${blocked.has(p.id) ? "blocked" : ""}">
      <td><b>${esc(p.label)}</b><div class="muted small">${nice(p.emergency_type)}</div></td>
      ${cells(p.allocation)}${cells(p.max)}${cells(need(p))}</tr>`).join("");
  const matrix = procs.length
    ? `<div class="table-wrap"><table class="matrix"><thead><tr><th rowspan="2">Process (case)</th>${head("Allocation")}${head("Max")}${head("Need = Max - Allocation")}</tr>
       <tr>${sub}${sub}${sub}</tr></thead><tbody>${rows}</tbody></table></div>`
    : '<p class="empty">No active cases at this hospital: with nobody to wait for, any state is safe.</p>';

  const verdict = {
    safe: ["ok", r.new_process ? "SAFE - the patient can be admitted" : "SAFE state"],
    unsafe: ["bad", r.new_process ? "UNSAFE - admission refused, the dispatcher tries the next hospital" : "UNSAFE state"],
    must_wait: ["bad", "Must wait - not enough free units right now (Request > Available)"],
  }[r.verdict];
  const seq = r.sequence_labels.length
    ? `<div class="chips">${r.sequence_labels.map((l) => `<span class="chip">${esc(l)}</span>`).join('<span class="arrow">&rarr;</span>')}</div>` : "";
  const steps = r.steps.map((s, i) => `<tr><td class="num">${i + 1}</td><td>${esc(s.label)}</td>
      <td class="vec">${vec(s.need, types)}</td><td class="vec">${vec(s.work_before, types)}</td><td class="vec">${vec(s.work_after, types)}</td></tr>`);
  const avail = r.available_after
    ? `<dt>Available now</dt><dd class="vec">${vec(r.available, types)}</dd><dt>After admitting</dt><dd class="vec">${vec(r.available_after, types)}</dd>`
    : `<dt>Available</dt><dd class="vec">${vec(r.available, types)}</dd>`;

  $("#bkResult").innerHTML = `
    <div class="result ${verdict[0]}">
      <h3>${verdict[1]}</h3>
      <dl class="kv">${avail}
        ${r.overridden.length ? `<dt>Pretend</dt><dd><span class="tag warn">what-if</span> ${r.overridden.map((t) => esc(SHORT_RES[t])).join(", ")} changed by you</dd>` : ""}
        ${r.sequence_labels.length ? `<dt>${r.safe ? "Safe sequence" : "Can still finish"}</dt><dd>${seq}</dd>` : ""}
        ${r.blocked_labels.length ? `<dt>Can never finish</dt><dd>${r.blocked_labels.map((l) => `<span class="tag bad">${esc(l)}</span>`).join(" ")}</dd>` : ""}
      </dl>
    </div>
    <h3 class="sub-title">Matrices</h3>${matrix}
    ${steps.length ? `<h3 class="sub-title">Safety algorithm, step by step</h3>
      <p class="muted small">Pick a case whose Need fits in Work, let it finish, add what it held back to Work. Repeat.</p>
      ${table([{ label: "Step", num: 1 }, "Finishes", "Its Need", "Work before", "Work after"], steps)}` : ""}`;
}

// ============================================================
// Deadlock detection
// ============================================================
const DL_KIND = { cycle: "bad", victim: "bad", wait: "warn", commit: "ok", lock: "", begin: "", timeout: "bad" };
function runDeadlock(btn, ordered) {
  withButton(btn, async () => {
    $("#dlResult").innerHTML = '<p class="muted">Running two real transactions on MySQL...</p>';
    const r = await api("/api/labs/deadlock", { method: "POST", body: { ordered } });
    const rows = r.events.map((e) => `<tr><td class="num">${e.t.toFixed(1)}</td>
      <td><b>${esc(e.tx)}</b></td><td><span class="tag ${DL_KIND[e.kind] ?? ""}">${esc(e.kind)}</span> ${esc(e.text)}</td></tr>`);
    const outcome = Object.entries(r.outcome).map(([tx, o]) => `${esc(tx)}: ${esc(o)}`).join(" &middot; ");
    $("#dlResult").innerHTML = `
      <div class="result ${r.deadlock ? "bad" : "ok"}"><h3>${esc(r.label)}</h3><p style="margin:0">${esc(r.verdict)}</p>
        <p class="muted small" style="margin:4px 0 0">Outcome: ${outcome}</p></div>
      <div class="dl-grid">
        <div><h3 class="sub-title">Wait-for graph</h3>${waitForGraph(r)}
          <p class="muted small">${esc(r.rows.A)}<br>${esc(r.rows.B)}<br>T1 needs ${r.plan.T1.join(" then ")}, T2 needs ${r.plan.T2.join(" then ")}.</p></div>
        <div><h3 class="sub-title">Timeline (ms)</h3>${table([{ label: "t", num: 1 }, "Who", "What"], rows)}</div>
      </div>`;
  });
}
$("#dlBad").addEventListener("click", (e) => runDeadlock(e.target, false));
$("#dlGood").addEventListener("click", (e) => runDeadlock(e.target, true));

function waitForGraph(r) {
  const pos = { T1: 70, T2: 270 };
  const edge = (e) => {
    const top = e.from === "T1";                               // T1 -> T2 above, T2 -> T1 below
    const x1 = pos[e.from] + (top ? 32 : -32), x2 = pos[e.to] + (top ? -38 : 38), y = top ? 58 : 102;
    const cy = top ? 10 : 150;
    const cls = r.cycle ? "wfg-edge cyc" : "wfg-edge";
    return `<path class="${cls}" d="M${x1},${y} Q${(x1 + x2) / 2},${cy} ${x2},${y}" marker-end="url(#${r.cycle ? "arrRed" : "arrBlue"})"/>
      <text class="wfg-label" x="${(x1 + x2) / 2}" y="${top ? 28 : 140}">waits for ${esc(e.row[0])}</text>`;
  };
  const node = (n) => `<circle class="wfg-node ${r.victim === n ? "victim" : ""}" cx="${pos[n]}" cy="80" r="30"/>
    <text class="wfg-name" x="${pos[n]}" y="85">${n}</text>`;
  return `<svg class="wfg" viewBox="0 0 340 160" role="img" aria-label="Wait-for graph">
    <defs>
      <marker id="arrBlue" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" class="arr-blue"/></marker>
      <marker id="arrRed" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" class="arr-red"/></marker>
    </defs>
    ${r.graph.edges.map(edge).join("")}${node("T1")}${node("T2")}
    ${r.cycle ? `<text class="wfg-cycle" x="170" y="86">cycle</text>` : ""}
  </svg>`;
}

// ============================================================
// Race condition
// ============================================================
document.querySelectorAll("[data-race]").forEach((btn) => btn.addEventListener("click", () => withButton(btn, async () => {
  const kind = btn.dataset.race;
  const r = await api("/api/labs/race", { method: "POST", body: {
    clients: 6, delay_ms: 50, locked: kind !== "unlocked", engine: kind === "procedure" ? "procedure" : "python" } });
  const rows = r.clients_detail.map((c) => `<tr><td>Client ${c.client}</td><td>#${c.request_id}</td>
    <td><span class="tag ${c.result === "allocated" ? "ok" : c.result.startsWith("error") ? "bad" : ""}">${esc(c.result.replace("_", " "))}</span></td>
    <td class="num">${c.started_ms}</td><td class="num">${c.finished_ms}</td></tr>`);
  $("#raceResult").innerHTML = `
    <div class="result ${r.consistent ? "ok" : "bad"}"><h3>${esc(r.mode_label)}</h3>
      <dl class="kv"><dt>Bookings</dt><dd><b>${r.bookings}</b> for ${r.beds} bed</dd>
        <dt>Rolled back</dt><dd>${r.rolled_back}</dd>
        <dt>Counter after</dt><dd>${r.final_available} free</dd>
        <dt>Consistent</dt><dd>${r.consistent ? '<span class="tag ok">YES</span>' : '<span class="tag bad">NO - lost update, bed overbooked</span>'}</dd></dl></div>
    ${table(["Client", "Request", "Result", { label: "Started (ms)", num: 1 }, { label: "Finished (ms)", num: 1 }], rows)}`;
})));

// ============================================================
// Database tab: views, triggers, procedures, history
// ============================================================
function objTable(rows, columns, emptyText = "No rows.") {
  if (!rows.length) return `<p class="empty">${esc(emptyText)}</p>`;
  const keys = columns?.length ? columns : Object.keys(rows[0]);
  const fmt = (v) => (v === null || v === undefined ? '<span class="muted">-</span>' : esc(v));
  return table(keys.map((k) => ({ label: k })), rows.map((r) => `<tr>${keys.map((k) => `<td>${fmt(r[k])}</td>`).join("")}</tr>`));
}

function objList(objs, withRows) {
  return objs.map((o) => `<div class="obj">
    <div class="obj-head"><code>${esc(o.name)}</code>
      ${o.installed ? "" : '<span class="tag bad">not installed</span>'}
      ${withRows && o.installed ? `<button class="ghost small" data-view="${esc(o.name)}">Show rows</button>` : ""}</div>
    <div class="muted small">${esc(o.about)}</div>
    <details><summary>SQL</summary><pre class="sql">${esc(o.sql)}</pre></details></div>`).join("");
}

async function loadDatabase() {
  const [cat, { hospitals }] = await Promise.all([api("/api/db/objects"), api("/api/hospitals")]);
  $("#dbBanner").innerHTML = cat.triggers_installed ? "" : `<div class="result bad" style="margin:0 0 16px">
    <h3>Database triggers are not installed</h3><p style="margin:0">${esc(cat.help)}</p></div>`;
  $("#dbViews").innerHTML = objList(cat.views, true);
  $("#dbTriggers").innerHTML = objList(cat.triggers, false);
  $("#dbProcedures").innerHTML = objList(cat.procedures, false);
  $("#trgDemo").disabled = !cat.triggers_installed;
  fillSelect($("#spHospital"), hospitals.map((h) => ({ value: h.hospital_id, label: h.name })));
}

$("#dbViews").addEventListener("click", (e) => {
  const name = e.target.dataset.view;
  if (!name) return;
  withButton(e.target, async () => {
    const { rows, columns } = await api(`/api/db/view/${encodeURIComponent(name)}`);
    $("#dbViewRows").innerHTML = `<h3 class="sub-title">SELECT * FROM ${esc(name)} <span class="muted small">(${rows.length} rows, max 50)</span></h3>${objTable(rows, columns, "The view is empty right now.")}`;
  });
});

$("#trgDemo").addEventListener("click", (e) => withButton(e.target, async () => {
  const r = await api("/api/db/trigger-demo", { method: "POST" });
  const steps = r.steps.map((s) => `<li><code>${esc(s.sql)}</code><br>
    ${s.ok ? '<span class="tag ok">accepted</span>' : `<span class="tag bad">refused by trigger</span> ${esc(s.message)}`}</li>`).join("");
  $("#trgDemoResult").innerHTML = `<div class="result"><h3>A test request (#${r.request_id}) walks through its states</h3>
    <ol class="steps">${steps}</ol>
    <h3 class="sub-title">request_status_history (written by the triggers)</h3>${objTable(r.history, r.history_columns)}
    <p class="muted small">The test request was deleted afterwards.</p></div>`;
}));

$("#spRun").addEventListener("click", (e) => withButton(e.target, async () => {
  const r = await api(`/api/db/report/${$("#spHospital").value}`);
  $("#spResult").innerHTML = `<div class="result"><h3>CALL sp_hospital_report(${esc($("#spHospital").value)})</h3>
    <p class="muted small">One CALL, two result sets.</p>
    <h3 class="sub-title">Result set 1: units</h3>${objTable(r.units, r.units_columns)}
    <h3 class="sub-title">Result set 2: doctors</h3>${objTable(r.doctors, r.doctors_columns)}</div>`;
}));

$("#histForm").addEventListener("submit", (e) => {
  e.preventDefault();
  withButton(e.target.querySelector("button"), async () => {
    const r = await api(`/api/db/history/${+$("#histId").value}`);
    $("#histResult").innerHTML = `<p>#${r.request.request_id} ${esc(r.request.patient_name)}, now <span class="tag">${esc(r.request.status)}</span></p>
      ${objTable(r.history, r.columns, r.triggers_installed ? "No history rows." : "No history: the triggers are not installed.")}`;
  });
});
