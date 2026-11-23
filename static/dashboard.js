// EmergencySOS console - Dashboard tab (Tier 2).
// Plain HTML/CSS charts, no chart library: bars are divs sized in %, so they work offline
// and on a phone. Every bar has a hover tooltip (data-tip) and the numbers stay in text.
// Uses the helpers from app.js ($, api, esc, toast, gantt, pBadge, state).
"use strict";

const fmtMin = (v) => (v == null ? "-" : `${(+v).toFixed(1)} min`);

// ---------- tooltip shared by every chart ----------
const tip = $("#chartTip");
document.addEventListener("pointerover", (e) => {
  const el = e.target.closest?.("[data-tip]");
  if (!tip) return;
  if (!el || !el.closest("#tab-dashboard")) { tip.classList.remove("show"); return; }
  tip.innerHTML = el.dataset.tip;
  tip.classList.add("show");
});
document.addEventListener("pointermove", (e) => {
  if (!tip || !tip.classList.contains("show")) return;
  const pad = 14, w = tip.offsetWidth, h = tip.offsetHeight;
  let x = e.clientX + pad, y = e.clientY + pad;
  if (x + w > innerWidth - 8) x = e.clientX - w - pad;
  if (y + h > innerHeight - 8) y = e.clientY - h - pad;
  tip.style.transform = `translate(${Math.max(8, x)}px, ${Math.max(8, y)}px)`;
});
document.addEventListener("scroll", () => tip?.classList.remove("show"), true);

// a "nice" axis maximum: 1, 2, 5 x 10^n
function niceMax(v) {
  if (!v || v <= 0) return 1;
  const p = 10 ** Math.floor(Math.log10(v)), f = v / p;
  return (f <= 1 ? 1 : f <= 2 ? 2 : f <= 5 ? 5 : 10) * p;
}

// ---------- KPI tiles ----------
function renderKpis(k, occ) {
  const tiles = [
    ["Waiting now", k.pending, k.pending ? "in the queue" : "queue empty"],
    ["Active cases", k.active, "unit + doctor booked"],
    ["SOS in 24 h", k.sos_24h, `${k.done_24h} completed`],
    ["Avg response", k.avg_response_min == null ? "-" : `${k.avg_response_min}<small> min</small>`, "SOS sent → unit booked, 24 h"],
    ["Bed occupancy", `${occ.pct}<small>%</small>`, `${occ.used} of ${occ.total} units in use`],
  ];
  $("#dbKpis").innerHTML = tiles.map(([label, value, note]) =>
    `<div class="kpi"><span class="kpi-label">${esc(label)}</span><b>${value}</b><span class="kpi-note">${esc(note)}</span></div>`).join("");
}

// ---------- grouped horizontal bars: avg wait vs critical wait per algorithm ----------
function renderScheduling(s) {
  if (!s.policies.length) {
    $("#dashSched").innerHTML = '<p class="empty">No patients in the system yet. Choose the sample workload.</p>';
    return;
  }
  const max = niceMax(Math.max(...s.policies.flatMap((p) => [p.avg_waiting, p.avg_critical_waiting])));
  const best = Math.min(...s.policies.map((p) => p.avg_critical_waiting));
  const hasCritical = s.policies[0].critical_count > 0;
  const bar = (v, cls, label, p) => `<div class="hbar-track"><div class="hbar ${cls}" style="width:${(100 * v) / max}%"
      data-tip="<b>${esc(p.name)}</b><br>${label}: ${v.toFixed(1)} min<br>Longest wait: ${p.max_waiting.toFixed(1)} min"></div>
      <span class="hbar-val">${v.toFixed(1)}</span></div>`;
  const rows = s.policies.map((p) => `<div class="hrow">
      <div class="hrow-name">${esc(p.name)}${hasCritical && p.avg_critical_waiting === best ? ' <span class="tag ok">best for critical</span>' : ""}</div>
      <div class="hrow-bars">${bar(p.avg_waiting, "s1", "Average wait", p)}${bar(p.avg_critical_waiting, "s2", "Critical (P1-P2) wait", p)}</div>
    </div>`).join("");
  $("#dashSched").innerHTML = `
    <div class="legend"><span><i class="sw s1"></i>Average wait</span><span><i class="sw s2"></i>Critical patients (P1-P2) wait</span>
      <span class="muted">minutes, ${s.patients} ${s.source === "real" ? "real patients" : "sample patients"}</span></div>
    <div class="hchart">${rows}</div>
    <p class="muted small">Same patients, six algorithms (simulated, non-preemptive). Shorter is better.
      ${hasCritical ? "" : "None of these patients is critical, so the orange bars are 0."}
      ${state.sysadmin ? 'Full table and all Gantt charts: <a href="#" data-goto="schedlab">Scheduler Lab</a>.' : ""}</p>`;
}

// ---------- occupancy: one bar per hospital ----------
function renderOccupancy(o) {
  $("#dashOccTotal").textContent = `all hospitals: ${o.pct}% (${o.used}/${o.total})`;
  const order = ["ICU_BED", "VENTILATOR", "OPERATION_THEATRE", "GENERAL_BED"];
  const rows = o.hospitals.map((h) => {
    const detail = order.filter((t) => h.types[t]).map((t) =>
      `${esc(RESOURCE_NAMES[t])}: ${h.types[t].used}/${h.types[t].total}`).join("<br>");
    const level = h.pct >= 90 ? "high" : h.pct >= 70 ? "mid" : "";
    return `<div class="occ-row">
      <div class="occ-name" title="${esc(h.name)}">${esc(h.name)}</div>
      <div class="hbar-track"><div class="hbar s1 ${level}" style="width:${h.pct}%"
        data-tip="<b>${esc(h.name)}</b><br>${h.used} of ${h.total} units in use (${h.pct}%)<br>${detail}<br>Ambulances out: ${h.ambulances_out}/${h.ambulances_total}"></div></div>
      <span class="occ-val">${h.pct}%</span>
      <div class="occ-amb muted small" title="Ambulances on the road / fleet">amb. ${h.ambulances_out}/${h.ambulances_total}</div>
    </div>`;
  }).join("");
  $("#dashOcc").innerHTML = `<div class="hchart">${rows}</div>
    <p class="muted small">Beds, ICU, ventilators and theatres together; hover for each type. Darker = 70% full or more,
      darkest = 90% or more.</p>`;
}

// ---------- stacked columns: SOS per hour ----------
function renderHourly(hours) {
  const total = hours.reduce((s, h) => s + h.total, 0);
  const max = niceMax(Math.max(...hours.map((h) => h.total)));
  const cols = hours.map((h, i) => `<div class="col" data-tip="<b>${esc(h.label)} - ${esc(hours[(i + 1) % 24].label)}</b><br>
        ${h.total} SOS (${h.critical} critical)">
      <div class="col-stack">
        <div class="col-seg s1" style="height:${(100 * h.other) / max}%"></div>
        <div class="col-seg s2" style="height:${(100 * h.critical) / max}%"></div>
      </div>
      <span class="col-label">${i % 3 === 0 ? esc(h.label.slice(0, 2)) : ""}</span></div>`).join("");
  const peak = hours.reduce((a, b) => (b.total > a.total ? b : a), hours[0]);
  $("#dashHourly").innerHTML = `
    <div class="legend"><span><i class="sw s1"></i>Other</span><span><i class="sw s2"></i>Critical (P1-P2)</span>
      <span class="muted">${total} in total${total ? `, busiest hour ${esc(peak.label)} (${peak.total})` : ""}</span></div>
    <div class="cols-wrap"><div class="cols-axis"><span>${max}</span><span>${max / 2}</span><span>0</span></div>
      <div class="cols">${cols}</div></div>
    <p class="muted small">Hour of the day (server time). Each column is one hour; hover for the numbers.</p>`;
}

// ---------- real wait by priority ----------
function renderResponse(rows) {
  const max = niceMax(Math.max(0, ...rows.map((r) => r.avg_min || 0)));
  $("#dashResp").innerHTML = `<div class="hchart">${rows.map((r) => `<div class="occ-row">
      <div class="occ-name">${pBadge(r.priority)} <span class="muted small">${r.cases} case${r.cases === 1 ? "" : "s"}</span></div>
      <div class="hbar-track">${r.avg_min == null ? '<span class="hbar-val muted">no cases yet</span>'
        : `<div class="hbar s1" style="width:${(100 * r.avg_min) / max}%"
            data-tip="<b>Priority P${r.priority}</b><br>${r.cases} cases<br>Average wait: ${fmtMin(r.avg_min)}<br>Longest: ${fmtMin(r.max_min)}"></div>
`}</div>
      <span class="occ-val">${r.avg_min == null ? "" : (+r.avg_min).toFixed(1)}</span><div class="occ-amb muted small">${r.avg_min == null ? "" : "min"}</div></div>`).join("")}</div>
    <p class="muted small">Measured from the database, not simulated: how long each patient really waited between
      sending the SOS and getting a unit. With priority scheduling and aging, P1 should wait least.</p>`;
}

// ---------- Gantt (Automatic policy) ----------
function renderGantt(s) {
  if (!s.gantt) { $("#dashGantt").innerHTML = '<p class="empty">No patients to schedule.</p>'; $("#dashGanttNote").textContent = ""; return; }
  const g = s.gantt, makespan = g.makespan || 1;
  $("#dashGanttNote").textContent = `${g.policy_name}, ${s.source === "real" ? "real" : "sample"} patients`;
  const ticks = [0, 0.25, 0.5, 0.75, 1].map((f) => `<span>${Math.round(f * makespan)}</span>`).join("");
  $("#dashGantt").innerHTML = `<div class="gantt">${gantt({ policy_name: g.policy_name, gantt: g.segments }, makespan)}</div>
    <div class="axis"><span></span><div>${ticks}</div></div>
    <p class="muted small">Minutes from the first arrival. Colour = priority (red P1 … grey P5), striped = dispatcher idle.
      Order: ${g.segments.filter((x) => x.id).map((x) => esc(x.label)).join(" → ")}</p>`;
}

// ---------- connection pool ----------
function renderPool(p) {
  if (!p.enabled) {
    $("#dashPool").innerHTML = '<p class="muted">Pool switched off (DB_POOL=0): every query opens a new connection.</p>';
    return;
  }
  const items = [
    ["Pool size", p.size, "semaphore permits"], ["In use now", p.in_use, `peak ${p.peak_in_use}`],
    ["Idle", p.idle, "ready to reuse"], ["Connections opened", p.created, `for ${p.borrowed} borrows`],
    ["Reused", `${p.reuse_pct}%`, `${p.reused} times`], ["Waited", p.waited, `${p.wait_ms} ms in total`],
  ];
  $("#dashPool").innerHTML = `<div class="pool-row">${items.map(([a, b, c]) =>
    `<div><b>${esc(b)}</b><span>${esc(a)}</span><small>${esc(c)}</small></div>`).join("")}</div>
    <p class="muted small">Opening a MySQL connection costs a TCP handshake and a login; the pool keeps them open and lends them out.
      When all ${p.size} are out, the next request blocks on the semaphore until one comes back.
      Load test before/after: <code>python load_test.py --compare-pool</code>.</p>`;
}

// ---------- load ----------
let dashLoading = null, dashLast = 0;
async function loadDashboard(force = false) {
  if (dashLoading) return dashLoading;
  if (!force && Date.now() - dashLast < 1500) return;     // live updates can arrive in bursts
  dashLoading = (async () => {
    const d = await api(`/api/dashboard?source=${encodeURIComponent($("#dashSource").value)}`);
    renderKpis(d.kpis, d.occupancy);
    renderScheduling(d.scheduling);
    renderOccupancy(d.occupancy);
    renderHourly(d.cases_per_hour);
    renderResponse(d.response_by_priority);
    renderGantt(d.scheduling);
    renderPool(d.pool);
    dashLast = Date.now();
  })();
  try { await dashLoading; } finally { dashLoading = null; }
}
$("#dashSource").addEventListener("change", () => loadDashboard(true).catch((e) => toast(e.message, true)));
$("#tab-dashboard").addEventListener("click", (e) => {
  const a = e.target.closest("[data-goto]");
  if (a) { e.preventDefault(); showTab(a.dataset.goto); }
});
