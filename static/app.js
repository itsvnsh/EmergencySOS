// EmergencySOS console - plain JavaScript, talks to the Flask REST API.
"use strict";

const state = { meta: null, tab: "dispatch", hospitals: [] };
const $ = (sel) => document.querySelector(sel);
const SEVERITY_WORDS = { 1: "mild", 2: "low", 3: "moderate", 4: "serious", 5: "critical" };
const PRIORITY_COLORS = { 1: "var(--p1)", 2: "var(--p2)", 3: "var(--p3)", 4: "var(--p4)", 5: "var(--p5)" };
const RESOURCE_NAMES = { ICU_BED: "ICU beds", VENTILATOR: "Ventilators", OPERATION_THEATRE: "Operation theatres", GENERAL_BED: "General beds" };

// ---------- helpers ----------
function esc(v) {
  return String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
const nice = (s) => esc(String(s ?? "").replace(/_/g, " ").toLowerCase().replace(/\b\w/g, (c) => c.toUpperCase()));
const pBadge = (p) => `<span class="badge p${p}">P${p}</span>`;
const fmtTime = (t) => (t ? esc(String(t).slice(11, 19)) : "");

async function api(path, options = {}) {
  const opts = { headers: { "Content-Type": "application/json" }, ...options };
  if (opts.body && typeof opts.body !== "string") opts.body = JSON.stringify(opts.body);
  let res, data;
  try {
    res = await fetch(path, opts);
    data = await res.json();
  } catch (e) {
    throw new Error("Server not reachable - is app.py running?");
  }
  if (!res.ok || data.ok === false) throw new Error(data.error || `Request failed (${res.status})`);
  return data;
}

let toastTimer;
function toast(msg, isError = false) {
  const t = $("#toast");
  t.textContent = msg;
  t.className = "toast show" + (isError ? " error" : "");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (t.className = "toast"), 3500);
}

async function withButton(btn, fn) {
  btn.disabled = true;
  try { await fn(); } catch (e) { toast(e.message, true); } finally { btn.disabled = false; }
}

function table(headers, rows, emptyText = "Nothing to show.") {
  if (!rows.length) return `<p class="empty">${esc(emptyText)}</p>`;
  const th = headers.map((h) => `<th class="${h.num ? "num" : ""}">${esc(h.label ?? h)}</th>`).join("");
  return `<div class="table-wrap"><table><thead><tr>${th}</tr></thead><tbody>${rows.join("")}</tbody></table></div>`;
}

// ---------- tabs ----------
document.querySelectorAll(".tab").forEach((b) =>
  b.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((x) => x.classList.toggle("active", x === b));
    document.querySelectorAll(".panel").forEach((p) => p.classList.toggle("active", p.id === "tab-" + b.dataset.tab));
    state.tab = b.dataset.tab;
    refreshActive();
  })
);

// ---------- meta / form setup ----------
async function loadMeta() {
  state.meta = await api("/api/meta");
  $("#typeSelect").innerHTML = state.meta.emergency_types.map((t) => `<option value="${t}">${nice(t)}</option>`).join("");
  const policyOpts = state.meta.policies.map((p) => `<option value="${p.id}">${esc(p.name)}</option>`).join("");
  $("#dispatchPolicy").innerHTML = policyOpts;
  $("#queuePolicy").innerHTML = policyOpts;
  $("#locSelect").innerHTML = state.meta.locations.map((l, i) => `<option value="${i}">${esc(l.name)}</option>`).join("")
    + `<option value="custom">Custom (type coordinates)</option>`;
  $("#agingInput").value = state.meta.aging_interval_min;
  setLocation(0);
}

function setLocation(i) {
  const l = state.meta.locations[i];
  if (!l) return;
  $("#lat").value = l.lat;
  $("#lon").value = l.lon;
}
$("#locSelect").addEventListener("change", (e) => { if (e.target.value !== "custom") setLocation(+e.target.value); });
$("#sevRange").addEventListener("input", (e) => ($("#sevLabel").textContent = `${e.target.value} · ${SEVERITY_WORDS[e.target.value]}`));
$("#geoBtn").addEventListener("click", () => {
  if (!navigator.geolocation) return toast("Geolocation is not available in this browser", true);
  navigator.geolocation.getCurrentPosition(
    (pos) => {
      $("#lat").value = pos.coords.latitude.toFixed(6);
      $("#lon").value = pos.coords.longitude.toFixed(6);
      $("#locSelect").value = "custom";
    },
    () => toast("Could not read your location", true)
  );
});

// ---------- SOS ----------
$("#sosForm").addEventListener("submit", (e) => {
  e.preventDefault();
  const btn = e.target.querySelector("button[type=submit]");
  withButton(btn, async () => {
    const data = Object.fromEntries(new FormData(e.target));
    const res = await api("/api/sos", { method: "POST", body: data });
    const c = res.classification;
    const preview = res.preview.length
      ? res.preview.map((h) => `<li><b>${esc(h.name)}</b> &middot; score ${h.score} &middot; ${h.distance_km} km</li>`).join("")
      : "<li>No hospital currently has this unit free.</li>";
    $("#sosResult").innerHTML = `
      <div class="result ok">
        <h3>Request #${res.request.request_id} queued ${pBadge(c.priority)} ${esc(c.priority_label)}</h3>
        <dl class="kv">
          <dt>Needs</dt><dd>${nice(c.required_resource)} + ${nice(c.required_specialist)}</dd>
          <dt>Handling time</dt><dd>${c.est_handling_min} min (burst time for SJF)</dd>
          <dt>Nearest hospital</dt><dd>${esc(res.nearest?.name)} (${res.nearest?.distance_km} km)</dd>
        </dl>
        <p class="muted small" style="margin:10px 0 2px">Current top matches (final choice happens at dispatch):</p>
        <ol class="steps">${preview}</ol>
      </div>`;
    e.target.patient_name.value = "";
    e.target.description.value = "";
    toast(`SOS #${res.request.request_id} received`);
    refreshStats();
  });
});

// ---------- dispatch ----------
$("#dispatchBtn").addEventListener("click", (e) =>
  withButton(e.target, async () => {
    const res = await api("/api/dispatch", { method: "POST", body: { policy: $("#dispatchPolicy").value } });
    renderDispatch(res);
    loadAllocations();
    refreshStats();
  })
);

function renderDispatch(res) {
  const box = $("#dispatchResult");
  box.classList.remove("empty");
  if (res.status === "empty") { box.innerHTML = `<p class="empty">${esc(res.message)}</p>`; return; }
  const r = res.request;
  const attempts = (res.attempts || []).map((a) => {
    const tag = a.result === "allocated" ? `<span class="tag ok">committed</span>`
      : `<span class="tag bad">${a.result === "no_unit" ? "unit gone - rolled back" : esc(a.result)}</span>`;
    return `<li>#${a.rank ?? "-"} ${esc(a.hospital)} ${tag}</li>`;
  }).join("");
  const rankRows = (res.ranked || []).map((h) => `
    <tr class="${res.hospital && h.hospital_id === res.hospital.hospital_id ? "best" : ""}">
      <td>${h.rank}</td><td>${esc(h.name)}</td><td class="num">${h.distance_km}</td>
      <td class="num">${h.available_units}/${h.total_units}</td><td class="num">${h.parts.specialist}</td>
      <td class="num">${h.occupancy_pct}%</td><td class="num"><b>${h.score}</b></td></tr>`);
  const ok = res.status === "allocated";
  const headline = ok
    ? `Allocated to ${esc(res.hospital.name)}`
    : res.status === "no_capacity" ? "No capacity - request stays in queue" : esc(res.message || res.status);
  box.innerHTML = `
    <div class="result ${ok ? "ok" : "bad"}">
      <h3>${headline}</h3>
      <dl class="kv">
        <dt>Request</dt><dd>#${r.request_id} ${esc(r.patient_name)} &middot; ${nice(r.emergency_type)} ${pBadge(r.priority)}
          ${res.picked_from_position ? `<span class="muted">(head of ${esc(res.policy)} queue)</span>` : ""}</dd>
        <dt>Unit</dt><dd>${nice(r.required_resource)}</dd>
        ${ok ? `<dt>Doctor</dt><dd>${res.doctor ? `${esc(res.doctor.name)} (${nice(res.doctor.specialization)})` : "none free - unit only"}</dd>
        <dt>Distance</dt><dd>${res.hospital.distance_km} km &middot; score ${res.hospital.score}</dd>
        <dt>Fallbacks</dt><dd>${res.fallbacks}</dd>` : ""}
        <dt>Nearest hospital</dt><dd>${esc(res.nearest?.name)} (${res.nearest?.distance_km} km)</dd>
      </dl>
      ${attempts ? `<p class="muted small" style="margin:10px 0 0">Transaction attempts:</p><ol class="steps">${attempts}</ol>` : ""}
    </div>
    ${rankRows.length ? `<p class="muted small" style="margin:14px 0 4px">Ranking snapshot (40% distance, 25% free units, 20% specialist, 15% load):</p>
      ${table(["#", "Hospital", { label: "km", num: 1 }, { label: "Free", num: 1 }, { label: "Spec.", num: 1 }, { label: "Load", num: 1 }, { label: "Score", num: 1 }], rankRows)}` : ""}`;
}

// ---------- allocations ----------
async function loadAllocations() {
  const { allocations } = await api("/api/allocations?status=ACTIVE");
  const rows = allocations.map((a) => `<tr>
    <td>#${a.allocation_id}</td><td>${esc(a.patient_name)} ${pBadge(a.priority)}</td><td>${nice(a.emergency_type)}</td>
    <td>${esc(a.hospital_name)}</td><td>${nice(a.required_resource)}</td>
    <td>${a.doctor_name ? esc(a.doctor_name) : '<span class="muted">-</span>'}</td>
    <td class="num">${a.distance_km ?? ""}</td><td>${fmtTime(a.allocated_at)}</td>
    <td><button class="ghost small" data-release="${a.allocation_id}">Release</button></td></tr>`);
  $("#allocTable").innerHTML = table(["ID", "Patient", "Type", "Hospital", "Unit", "Doctor", { label: "km", num: 1 }, "Since", ""],
    rows, "No active allocations. Send an SOS and dispatch it.");
}
$("#allocTable").addEventListener("click", (e) => {
  const id = e.target.dataset.release;
  if (!id) return;
  withButton(e.target, async () => {
    await api(`/api/allocations/${id}/release`, { method: "POST" });
    toast(`Allocation #${id} released - unit returned`);
    loadAllocations();
    refreshStats();
  });
});
$("#refreshAlloc").addEventListener("click", () => loadAllocations().catch((e) => toast(e.message, true)));

// ---------- queue ----------
async function loadQueue() {
  const policy = $("#queuePolicy").value || "PRIORITY";
  const { queue, policy_name } = await api(`/api/queue?policy=${policy}`);
  $("#queueNote").textContent = `Ordered by ${policy_name}. Aging: every ${state.meta.aging_interval_min} min of waiting improves the effective priority by 1. `
    + "MLQ levels: 1 = critical (P1-P2), 2 = urgent (P3), 3 = routine (P4-P5).";
  const rows = queue.map((q) => `<tr>
    <td class="num">${q.position}</td><td>#${q.request_id}</td><td>${esc(q.patient_name)}</td><td>${nice(q.emergency_type)}</td>
    <td class="num">${q.severity}</td><td>${pBadge(q.priority)}</td>
    <td>${q.effective_priority < q.priority ? `${pBadge(q.effective_priority)} <span class="tag warn">aged</span>` : pBadge(q.effective_priority)}</td>
    <td class="num">${q.mlq_level}</td><td class="num">${q.est_handling_min}</td><td class="num">${q.wait_min}</td>
    <td><button class="ghost small" data-dispatch="${q.request_id}">Dispatch</button></td></tr>`);
  $("#queueTable").innerHTML = table(
    [{ label: "Pos", num: 1 }, "ID", "Patient", "Type", { label: "Sev", num: 1 }, "Priority", "Effective", { label: "MLQ", num: 1 }, { label: "Burst (min)", num: 1 }, { label: "Waiting (min)", num: 1 }, ""],
    rows, "The queue is empty. Send an SOS or click 'Add demo requests'.");
}
$("#queuePolicy").addEventListener("change", () => loadQueue().catch((e) => toast(e.message, true)));
$("#queueTable").addEventListener("click", (e) => {
  const id = e.target.dataset.dispatch;
  if (!id) return;
  withButton(e.target, async () => {
    const res = await api("/api/dispatch", { method: "POST", body: { request_id: +id, policy: $("#queuePolicy").value } });
    renderDispatch(res);
    toast(res.status === "allocated" ? `#${id} allocated to ${res.hospital.name}` : res.message || res.status, res.status !== "allocated");
    loadQueue(); loadRecent(); refreshStats();
  });
});
$("#seedBtn").addEventListener("click", (e) =>
  withButton(e.target, async () => {
    await api("/api/demo/seed", { method: "POST" });
    toast("6 demo requests added (arrived 1-45 min ago)");
    loadQueue(); loadRecent(); refreshStats();
  })
);

async function loadRecent() {
  const { requests } = await api("/api/requests");
  const statusTag = { PENDING: "warn", ALLOCATED: "ok", COMPLETED: "", CANCELLED: "bad" };
  const rows = requests.slice(0, 25).map((r) => `<tr>
    <td>#${r.request_id}</td><td>${esc(r.patient_name)}</td><td>${nice(r.emergency_type)}</td><td>${pBadge(r.priority)}</td>
    <td><span class="tag ${statusTag[r.status]}">${esc(r.status)}</span></td><td>${esc(r.hospital_name || "")}</td>
    <td>${fmtTime(r.created_at)}</td></tr>`);
  $("#recentTable").innerHTML = table(["ID", "Patient", "Type", "Priority", "Status", "Hospital", "Received"], rows, "No requests yet.");
}

// ---------- hospitals / admin ----------
function meterClass(pct) { return pct >= 85 ? "high" : pct >= 60 ? "mid" : ""; }

async function loadHospitals() {
  const { hospitals } = await api("/api/hospitals");
  state.hospitals = hospitals;
  $("#hospitalGrid").innerHTML = hospitals.map((h) => {
    const res = h.resources.map((r) => {
      const used = r.total_units ? Math.round(100 * (r.total_units - r.available_units) / r.total_units) : 0;
      return `<div class="res-row">
        <div>${RESOURCE_NAMES[r.resource_type]}<div class="meter"><i class="${meterClass(used)}" style="width:${used}%"></i></div></div>
        <div class="edit">
          <input type="number" min="0" value="${r.available_units}" data-free title="Free units"> /
          <input type="number" min="0" value="${r.total_units}" data-total title="Total units">
        </div>
        <button class="ghost small" data-save="${h.hospital_id}|${r.resource_type}">Save</button>
      </div>`;
    }).join("");
    const docs = h.doctors.map((d) => `<div class="doc">
        <span class="${d.on_shift ? "" : "off"}">${esc(d.name)} &middot; ${nice(d.specialization)}</span>
        <span>${d.current_cases}/${d.max_cases}
          <button class="ghost switch" data-shift="${d.doctor_id}" data-on="${d.on_shift ? 0 : 1}">${d.on_shift ? "On shift" : "Off"}</button></span>
      </div>`).join("");
    return `<div class="card hosp">
      <h3>${esc(h.name)}</h3>
      <div class="sub">${esc(h.area)} &middot; ${h.occupancy_pct}% occupied</div>
      ${res}
      <div class="doc-list">${docs}</div>
    </div>`;
  }).join("");
}

$("#hospitalGrid").addEventListener("click", (e) => {
  const t = e.target;
  if (t.dataset.save) {
    const [hid, rtype] = t.dataset.save.split("|");
    const row = t.closest(".res-row");
    withButton(t, async () => {
      await api("/api/admin/resource", {
        method: "POST",
        body: { hospital_id: +hid, resource_type: rtype, total_units: row.querySelector("[data-total]").value, available_units: row.querySelector("[data-free]").value },
      });
      toast("Capacity updated");
      loadHospitals(); refreshStats();
    });
  } else if (t.dataset.shift) {
    withButton(t, async () => {
      await api(`/api/admin/doctor/${t.dataset.shift}/shift`, { method: "POST", body: { on_shift: t.dataset.on === "1" } });
      loadHospitals();
    });
  }
});

$("#resetBtn").addEventListener("click", (e) => {
  if (!confirm("Delete all requests and allocations and reload the simulated hospital data?")) return;
  withButton(e.target, async () => {
    await api("/api/admin/reset", { method: "POST" });
    toast("Simulated data reloaded");
    refreshActive(); refreshStats();
  });
});

// ---------- scheduler lab ----------
async function loadWorkload() {
  const { workload } = await api("/api/labs/scheduler/workload");
  const rows = workload.map((w) => `<tr><td>${esc(w.label)}</td><td>${nice(w.type)}</td><td class="num">${w.severity}</td>
    <td>${pBadge(w.priority)}</td><td class="num">${w.arrival}</td><td class="num">${w.burst}</td></tr>`);
  $("#workloadTable").innerHTML = table(["Req", "Type", { label: "Sev", num: 1 }, "Priority", { label: "Arrival (min)", num: 1 }, { label: "Burst (min)", num: 1 }], rows);
}

function gantt(res, makespan) {
  const segs = res.gantt.map((g) => {
    const left = (100 * g.start) / makespan, width = (100 * (g.end - g.start)) / makespan;
    const style = `left:${left}%;width:${width}%;${g.id ? `background:${PRIORITY_COLORS[g.priority]}` : ""}`;
    return `<div class="seg ${g.id ? "" : "idle"}" style="${style}" title="${esc(g.label)}: ${g.start}-${g.end} min">${g.id ? esc(g.label) : ""}</div>`;
  }).join("");
  return `<div class="gantt-row"><div class="name">${esc(res.policy_name)}</div><div class="bar">${segs}</div></div>`;
}

$("#runSchedBtn").addEventListener("click", (e) =>
  withButton(e.target, async () => {
    const { results } = await api("/api/labs/scheduler", { method: "POST", body: { aging_interval: +$("#agingInput").value } });
    const keys = ["avg_waiting", "avg_turnaround", "avg_critical_waiting", "max_waiting"];
    const best = Object.fromEntries(keys.map((k) => [k, Math.min(...results.map((r) => r.metrics[k]))]));
    const rows = results.map((r) => `<tr><td><b>${esc(r.policy_name)}</b></td>${keys
      .map((k) => `<td class="num ${r.metrics[k] === best[k] ? "best" : ""}">${r.metrics[k].toFixed(2)}</td>`).join("")}
      <td>${r.gantt.filter((g) => g.id).map((g) => esc(g.label)).join(" → ")}</td></tr>`);
    const makespan = Math.max(...results.map((r) => r.metrics.makespan));
    const ticks = [0, 0.25, 0.5, 0.75, 1].map((f) => `<span>${Math.round(f * makespan)}</span>`).join("");
    const pr = results.find((r) => r.policy === "PRIORITY"), fc = results.find((r) => r.policy === "FCFS"),
      na = results.find((r) => r.policy === "PRIORITY_NO_AGING");
    $("#schedResults").innerHTML = `
      <div class="card">
        <h2>Results (minutes)</h2>
        <p class="muted">Priority with aging cuts the average wait of critical patients to <b>${pr.metrics.avg_critical_waiting}</b> min
          against <b>${fc.metrics.avg_critical_waiting}</b> min under FCFS. Aging lowers the longest wait from
          <b>${na.metrics.max_waiting}</b> to <b>${pr.metrics.max_waiting}</b> min (no starvation), at some cost to critical waits
          compared with pure priority. Green = best in column.</p>
        <div class="table-wrap"><table class="metrics-table"><thead><tr><th>Policy</th><th class="num">Avg wait</th><th class="num">Avg turnaround</th>
          <th class="num">Critical wait</th><th class="num">Max wait</th><th>Execution order</th></tr></thead><tbody>${rows.join("")}</tbody></table></div>
      </div>
      <div class="card">
        <h2>Gantt charts</h2>
        <div class="gantt">${results.map((r) => gantt(r, makespan)).join("")}</div>
        <div class="axis"><span></span><div>${ticks}</div></div>
        <p class="muted small">Colour = priority (red P1 … grey P5). Striped = dispatcher idle.</p>
      </div>`;
  })
);

// ---------- top stats + polling ----------
async function refreshStats() {
  try {
    const [{ hospitals }, { queue }, { allocations }] = await Promise.all([
      api("/api/hospitals"), api("/api/queue?policy=PRIORITY"), api("/api/allocations?status=ACTIVE"),
    ]);
    const free = (type) => hospitals.reduce((s, h) => s + (h.resources.find((r) => r.resource_type === type)?.available_units || 0), 0);
    $("#topStats").innerHTML = [
      ["Pending", queue.length], ["Active", allocations.length], ["Free ICU", free("ICU_BED")], ["Free vent.", free("VENTILATOR")],
    ].map(([k, v]) => `<div class="stat"><b>${v}</b><span>${k}</span></div>`).join("");
  } catch (e) {
    $("#topStats").innerHTML = `<span class="tag bad">${esc(e.message)}</span>`;
  }
}

function refreshActive() {
  const loaders = {
    dispatch: [loadAllocations], queue: [loadQueue, loadRecent], hospitals: [loadHospitals],
    schedlab: [loadWorkload],
  }[state.tab] || [];
  loaders.forEach((fn) => fn().catch((e) => toast(e.message, true)));
}

// polling keeps the console live (replacing this with push updates is a pending task)
setInterval(() => {
  refreshStats();
  if (state.tab === "queue") loadQueue().catch(() => {});
  if (state.tab === "dispatch") loadAllocations().catch(() => {});
  // skip the hospital grid while an admin is typing into it
  if (state.tab === "hospitals" && !document.activeElement.closest?.("#hospitalGrid")) loadHospitals().catch(() => {});
}, 5000);

loadMeta()
  .then(() => { refreshActive(); refreshStats(); })
  .catch((e) => toast(e.message, true));
