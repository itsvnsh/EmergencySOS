// EmergencySOS console - plain JavaScript, talks to the Flask REST API.
"use strict";

const state = { meta: null, tab: null, hospitals: [], user: null, sysadmin: false, map: null, mapLayer: null };
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
const srcTag = (src) => (src === "ROAD" ? '<span class="tag ok" title="Road distance from the routing server">road</span>'
  : src ? '<span class="tag warn" title="Routing server not reachable: straight line x 1.35">estimate</span>' : "");
// time left until a case auto-completes, using the server clock offset from /api/allocations
function endsIn(t) {
  if (!t) return '<span class="muted">-</span>';
  const ms = new Date(String(t).replace(" ", "T")) - (Date.now() + (state.clockSkew || 0));
  if (ms <= 0) return '<span class="tag warn">due now</span>';
  const m = Math.floor(ms / 60000), sec = Math.floor((ms % 60000) / 1000);
  return `in ${m}:${String(sec).padStart(2, "0")}`;
}

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
  if (res.status === 401) { location.href = "/login"; throw new Error("Please log in"); }
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
function showTab(name) {
  document.querySelectorAll(".tab").forEach((x) => x.classList.toggle("active", x.dataset.tab === name));
  document.querySelectorAll(".panel").forEach((p) => p.classList.toggle("active", p.id === "tab-" + name));
  state.tab = name;
  refreshActive();
}
document.querySelectorAll(".tab").forEach((b) => b.addEventListener("click", () => showTab(b.dataset.tab)));

// ---------- who is logged in ----------
const isDispatcher = () => state.user?.role === "DISPATCHER";
const isAdmin = () => state.user?.role === "HOSPITAL_ADMIN";
// a hospital admin tied to one hospital may only edit that one; the system admin may edit all
const canEditHospital = (hid) => isAdmin() && (state.user.hospital_id == null || state.user.hospital_id === hid);

async function loadMe() {
  const me = await api("/api/me");
  if (!me.user) { location.href = "/login"; throw new Error("Please log in"); }
  if (me.user.role === "PATIENT") { location.href = "/sos"; throw new Error("Patient portal"); }
  state.user = me.user;
  state.sysadmin = me.system_admin;
  document.body.classList.toggle("is-dispatcher", isDispatcher());
  document.body.classList.toggle("is-admin", isAdmin());
  document.body.classList.toggle("is-sysadmin", state.sysadmin);
  const where = me.user.hospital_name ? ` · ${me.user.hospital_name}` : state.sysadmin ? " · all hospitals" : "";
  $("#userName").innerHTML = `<b>${esc(me.user.full_name)}</b><small>${esc(me.user.role_name)}${esc(where)}</small>`;
}
$("#logoutBtn").addEventListener("click", async () => {
  try { await api("/api/logout", { method: "POST" }); } finally { location.href = "/login"; }
});

// ---------- meta / form setup ----------
async function loadMeta() {
  state.meta = await api("/api/meta");
  $("#typeSelect").innerHTML = state.meta.emergency_types.map((t) => `<option value="${t}">${nice(t)}</option>`).join("");
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

// hospital contact number shown under a hospital name (tap to call on a phone)
function phoneLine(phone) {
  if (!phone) return "";
  return `<a class="phone" href="tel:${esc(phone.replace(/[^0-9+]/g, ""))}">&#9742; ${esc(phone)}</a>`;
}

// ---------- SOS ----------
$("#sosForm").addEventListener("submit", (e) => {
  e.preventDefault();
  const btn = e.target.querySelector("button[type=submit]");
  withButton(btn, async () => {
    const data = Object.fromEntries(new FormData(e.target));
    const res = await api("/api/sos", { method: "POST", body: data });
    const c = res.classification;
    const preview = res.preview.length
      ? res.preview.map((h) => `<li><b>${esc(h.name)}</b> &middot; score ${h.score} &middot; ${h.distance_km} km ${phoneLine(h.phone)}</li>`).join("")
      : "<li>No hospital currently has this unit free.</li>";
    $("#sosResult").innerHTML = `
      <div class="result ok">
        <h3>Request #${res.request.request_id} queued ${pBadge(c.priority)} ${esc(c.priority_label)}</h3>
        <dl class="kv">
          <dt>Needs</dt><dd>${nice(c.required_resource)} + ${nice(c.required_specialist)}</dd>
          <dt>Handling time</dt><dd>${c.est_handling_min} min (burst time for SJF)</dd>
          <dt>Nearest hospital</dt><dd>${esc(res.nearest?.name)} (${res.nearest?.distance_km} km) ${phoneLine(res.nearest?.phone)}</dd>
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
    const res = await api("/api/dispatch", { method: "POST", body: { policy: "AUTO" } });
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
      <td>${h.rank}</td><td>${esc(h.name)}</td><td class="num">${h.distance_km}<div class="muted small">${h.travel_min} min</div></td>
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
</dd>
        ${res.decision ? `<dt>Chosen by</dt><dd>${queueTag(res.decision)} &rarr; <b>${esc(res.decision.algorithm)}</b>
          <div class="muted small">${esc(res.decision.reason)}</div></dd>` : ""}
        <dt>Unit</dt><dd>${nice(r.required_resource)}</dd>
        ${ok ? `<dt>Hospital phone</dt><dd>${phoneLine(res.hospital.phone) || '<span class="muted">-</span>'}</dd>
        <dt>Doctor</dt><dd>${res.doctor ? `${esc(res.doctor.name)} (${nice(res.doctor.specialization)})` : "none free - unit only"}</dd>
        <dt>Distance</dt><dd>${res.hospital.distance_km} km by road &middot; ~${res.hospital.travel_min} min ${srcTag(res.hospital.distance_source)} &middot; score ${res.hospital.score}</dd>
        <dt>Fallbacks</dt><dd>${res.fallbacks}</dd>` : ""}
        <dt>Nearest hospital</dt><dd>${esc(res.nearest?.name)} (${res.nearest?.distance_km} km) ${phoneLine(res.nearest?.phone)}</dd>
      </dl>
      ${attempts ? `<p class="muted small" style="margin:10px 0 0">Transaction attempts:</p><ol class="steps">${attempts}</ol>` : ""}
    </div>
    ${rankRows.length ? `<p class="muted small" style="margin:14px 0 4px">Ranking snapshot (40% road distance, 25% free units, 20% specialist, 15% load):</p>
      ${table(["#", "Hospital", { label: "Road km", num: 1 }, { label: "Free", num: 1 }, { label: "Spec.", num: 1 }, { label: "Load", num: 1 }, { label: "Score", num: 1 }], rankRows)}` : ""}`;
}

// ---------- allocations ----------
async function loadAllocations() {
  const { allocations, server_time } = await api("/api/allocations?status=ACTIVE");
  if (server_time) state.clockSkew = new Date(server_time.replace(" ", "T")) - Date.now();
  const rows = allocations.map((a) => `<tr>
    <td>#${a.allocation_id}</td><td>${esc(a.patient_name)} ${pBadge(a.priority)}</td><td>${nice(a.emergency_type)}</td>
    <td>${esc(a.hospital_name)}${a.hospital_phone ? `<br>${phoneLine(a.hospital_phone)}` : ""}</td><td>${nice(a.required_resource)}</td>
    <td>${a.doctor_name ? esc(a.doctor_name) : '<span class="muted">-</span>'}</td>
    <td class="num">${a.distance_km ?? ""}${a.travel_min != null ? `<div class="muted small">${a.travel_min} min ${srcTag(a.distance_source)}</div>` : ""}</td>
    <td>${fmtTime(a.allocated_at)}</td><td data-ends="${esc(a.expected_end_at || "")}">${endsIn(a.expected_end_at)}</td>
    <td>${isDispatcher() ? `<button class="ghost small" data-release="${a.allocation_id}">Release</button>` : ""}</td></tr>`);
  $("#allocTable").innerHTML = table(["ID", "Patient", "Type", "Hospital", "Unit", "Doctor", { label: "Road km", num: 1 }, "Since", "Auto-completes", ""],
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
const QUEUE_CLASS = { 1: "bad", 2: "info", 3: "ok" };
function queueTag(d) {
  return `<span class="tag ${QUEUE_CLASS[d.level]}">${esc(d.queue)} queue</span>`;
}

async function loadQueue() {
  const { queue } = await api("/api/queue");
  $("#queueNote").textContent = "This is the order the dispatcher will follow (Automatic mode). "
    + `Aging: every ${state.meta.aging_interval_min} min of waiting raises a patient's priority by one level.`;
  const rows = queue.map((q) => `<tr>
    <td class="num">${q.position}</td><td>#${q.request_id}</td><td>${esc(q.patient_name)}</td><td>${nice(q.emergency_type)}</td>
    <td class="num">${q.severity}</td><td>${pBadge(q.priority)}</td>
    <td>${q.effective_priority < q.priority ? `${pBadge(q.effective_priority)} <span class="tag warn">aged</span>` : pBadge(q.effective_priority)}</td>
    <td>${queueTag(q.auto)}</td><td title="${esc(q.auto.reason)}">${esc(q.auto.algorithm)}</td>
    <td class="num">${q.est_handling_min}</td><td class="num">${q.wait_min}</td></tr>`);
  $("#queueTable").innerHTML = table(
    [{ label: "Pos", num: 1 }, "ID", "Patient", "Type", { label: "Sev", num: 1 }, "Priority", "Effective", "Queue", "Algorithm used", { label: "Burst (min)", num: 1 }, { label: "Waiting (min)", num: 1 }],
    rows, "The queue is empty. Send an SOS or click 'Add demo requests'.");
}
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
    <td>${fmtTime(r.created_at)}</td>
    <td class="actions"><button class="ghost small" data-edit-req="${r.request_id}">Edit</button>
      ${r.status === "PENDING" && isDispatcher() ? `<button class="ghost small warn-btn" data-cancel-req="${r.request_id}">Cancel</button>` : ""}</td></tr>`);
  state.recent = requests;
  $("#recentTable").innerHTML = table(["ID", "Patient", "Type", "Priority", "Status", "Hospital", "Received", ""], rows, "No requests yet.");
}
$("#recentTable").addEventListener("click", (e) => {
  const t = e.target;
  if (t.dataset.editReq) {
    const r = state.recent.find((x) => x.request_id === +t.dataset.editReq);
    openDialog(`Patient details · request #${r.request_id}`, [
      { name: "patient_name", label: "Patient name", value: r.patient_name, required: true, maxlength: 80 },
      { name: "phone", label: "Phone", value: r.phone, maxlength: 20 },
      { name: "description", label: "Description", value: r.description, maxlength: 255 },
    ], async (data) => {
      await api(`/api/requests/${r.request_id}`, { method: "PUT", body: data });
      toast("Patient details saved");
      loadRecent();
    });
  } else if (t.dataset.cancelReq) {
    if (!confirm(`Cancel request #${t.dataset.cancelReq}? It will be removed from the queue.`)) return;
    withButton(t, async () => {
      await api(`/api/requests/${t.dataset.cancelReq}/cancel`, { method: "POST" });
      toast("Request cancelled");
      loadQueue(); loadRecent(); refreshStats();
    });
  }
});

// ---------- hospitals / admin ----------
function meterClass(pct) { return pct >= 85 ? "high" : pct >= 60 ? "mid" : ""; }

async function loadHospitals() {
  const { hospitals } = await api("/api/hospitals");
  state.hospitals = hospitals;
  $("#adminScope").textContent = isAdmin()
    ? (state.sysadmin ? "You can edit every hospital, add hospitals and manage user accounts."
      : `You can edit ${state.user.hospital_name} only. Other hospitals are shown read-only.`)
    : "Read-only view. Hospital admins change these numbers.";
  // editable hospitals first
  const sorted = [...hospitals].sort((a, b) => canEditHospital(b.hospital_id) - canEditHospital(a.hospital_id));
  $("#hospitalGrid").innerHTML = sorted.map((h) => {
    const edit = canEditHospital(h.hospital_id);
    const res = h.resources.map((r) => {
      const used = r.total_units ? Math.round(100 * (r.total_units - r.available_units) / r.total_units) : 0;
      return `<div class="res-row ${edit ? "" : "ro"}">
        <div>${RESOURCE_NAMES[r.resource_type]}<div class="meter"><i class="${meterClass(used)}" style="width:${used}%"></i></div></div>
        ${edit ? `<div class="edit">
          <input type="number" min="0" value="${r.available_units}" data-free title="Free units" aria-label="Free units"> /
          <input type="number" min="0" value="${r.total_units}" data-total title="Total units" aria-label="Total units">
        </div>
        <button class="ghost small" data-save="${h.hospital_id}|${r.resource_type}">Save</button>`
        : `<div class="ro-count"><b>${r.available_units}</b> / ${r.total_units} free</div>`}
      </div>`;
    }).join("");
    const docs = h.doctors.map((d) => `<div class="doc">
        <span class="${d.on_shift ? "" : "off"}">${esc(d.name)} &middot; ${nice(d.specialization)}</span>
        <span class="doc-act">${d.current_cases}/${d.max_cases}
          ${edit ? `<button class="ghost switch" data-shift="${d.doctor_id}" data-on="${d.on_shift ? 0 : 1}">${d.on_shift ? "On shift" : "Off"}</button>
          <button class="icon-btn" data-edit-doc="${d.doctor_id}" title="Edit doctor" aria-label="Edit ${esc(d.name)}">&#9998;</button>
          <button class="icon-btn danger-text" data-del-doc="${d.doctor_id}" title="Remove doctor" aria-label="Remove ${esc(d.name)}">&times;</button>`
          : `<span class="tag ${d.on_shift ? "ok" : ""}">${d.on_shift ? "on shift" : "off"}</span>`}</span>
      </div>`).join("");
    return `<div class="card hosp ${edit ? "editable" : ""}">
      <div class="hosp-head"><h3>${esc(h.name)}</h3>
        ${edit ? `<span class="inline"><button class="ghost small" data-edit-hosp="${h.hospital_id}">Edit details</button>
          ${state.sysadmin ? `<button class="ghost small warn-btn" data-del-hosp="${h.hospital_id}">Delete</button>` : ""}</span>` : ""}</div>
      ${phoneLine(h.phone)}
      <div class="sub">${esc(h.area)} &middot; ${h.occupancy_pct}% occupied</div>
      ${res}
      <div class="doc-list">${docs}
        ${edit ? `<button class="ghost small add-doc" data-add-doc="${h.hospital_id}">+ Add doctor</button>` : ""}</div>
    </div>`;
  }).join("");
}

const findDoctor = (id) => state.hospitals.flatMap((h) => h.doctors).find((d) => d.doctor_id === id);
const specOptions = () => state.meta.specializations.map((s) => ({ value: s, label: nice(s).replace(/&amp;/g, "&") }));
const doctorFields = (d = {}) => [
  { name: "name", label: "Doctor name", value: d.name ?? "Dr. ", required: true, maxlength: 80 },
  { name: "specialization", label: "Specialization", type: "select", value: d.specialization, options: specOptions() },
  { name: "max_cases", label: "Max cases at once", type: "number", value: d.max_cases ?? 3, min: 1, max: 50 },
  { name: "on_shift", label: "On shift", type: "checkbox", value: d.on_shift ?? 1 },
];
const hospitalFields = (h = {}) => [
  { name: "name", label: "Hospital name", value: h.name, required: true, maxlength: 120 },
  { name: "area", label: "Area", value: h.area, required: true, maxlength: 80 },
  { name: "phone", label: "Phone", value: h.phone, maxlength: 20 },
  { name: "latitude", label: "Latitude", type: "number", step: "any", value: h.latitude, required: true },
  { name: "longitude", label: "Longitude", type: "number", step: "any", value: h.longitude, required: true },
];

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
  } else if (t.dataset.addDoc) {
    const h = state.hospitals.find((x) => x.hospital_id === +t.dataset.addDoc);
    openDialog(`Add doctor · ${h.name}`, doctorFields(), async (data) => {
      await api(`/api/admin/hospitals/${h.hospital_id}/doctors`, { method: "POST", body: data });
      toast("Doctor added"); loadHospitals();
    });
  } else if (t.dataset.editDoc) {
    const d = findDoctor(+t.dataset.editDoc);
    openDialog(`Edit doctor · ${d.name}`, doctorFields(d), async (data) => {
      await api(`/api/admin/doctors/${d.doctor_id}`, { method: "PUT", body: data });
      toast("Doctor updated"); loadHospitals();
    });
  } else if (t.dataset.delDoc) {
    const d = findDoctor(+t.dataset.delDoc);
    if (!confirm(`Remove ${d.name}?`)) return;
    withButton(t, async () => {
      await api(`/api/admin/doctors/${d.doctor_id}`, { method: "DELETE" });
      toast("Doctor removed"); loadHospitals();
    });
  } else if (t.dataset.editHosp) {
    const h = state.hospitals.find((x) => x.hospital_id === +t.dataset.editHosp);
    openDialog(`Edit hospital · ${h.name}`, hospitalFields(h), async (data) => {
      await api(`/api/admin/hospitals/${h.hospital_id}`, { method: "PUT", body: data });
      toast("Hospital details saved"); loadHospitals();
    });
  } else if (t.dataset.delHosp) {
    const h = state.hospitals.find((x) => x.hospital_id === +t.dataset.delHosp);
    if (!confirm(`Delete ${h.name} with all its beds and doctors?`)) return;
    withButton(t, async () => {
      await api(`/api/admin/hospitals/${h.hospital_id}`, { method: "DELETE" });
      toast("Hospital deleted"); loadHospitals(); refreshStats();
    });
  }
});

$("#addHospitalBtn").addEventListener("click", () => {
  const unitFields = state.meta.resource_types.map((r) => (
    { name: `units_${r}`, label: `${RESOURCE_NAMES[r]} (total)`, type: "number", value: 0, min: 0, max: 5000 }));
  openDialog("Add hospital", [...hospitalFields({ latitude: 30.3165, longitude: 78.0322 }), ...unitFields], async (data) => {
    const units = {};
    state.meta.resource_types.forEach((r) => { units[r] = { total: +data[`units_${r}`] || 0 }; delete data[`units_${r}`]; });
    await api("/api/admin/hospitals", { method: "POST", body: { ...data, units } });
    toast("Hospital added"); loadHospitals(); refreshStats();
  });
});

$("#resetBtn").addEventListener("click", (e) => {
  if (!confirm("Delete all requests and allocations and reload the simulated hospital data?")) return;
  withButton(e.target, async () => {
    await api("/api/admin/reset", { method: "POST" });
    alert("Simulated data reloaded. Please log in again.");
    location.href = "/login";
  });
});

// ---------- scheduler lab ----------
const LAB_INTRO = {
  real: "These are the patients really in your database: everyone holding an active allocation plus anyone still waiting. "
    + "The lab asks: if these same patients had to be handled again by ONE dispatcher, one at a time, how long would each "
    + "algorithm make them wait? Arrival = minutes after the first of them sent their SOS. Burst = handling time. "
    + "This is only a simulation: nothing in the database changes.",
  sample: "12 example requests arrive over a 46-minute shift; one dispatcher handles one case at a time. "
    + "Burst time = estimated handling time from the classifier.",
};

async function loadWorkload() {
  const source = $("#labSource").value;
  $("#labIntro").textContent = LAB_INTRO[source] + ' "Critical" = priority 1-2.';
  $("#schedResults").innerHTML = "";
  const { workload } = await api(`/api/labs/scheduler/workload?source=${source}`);
  if (source === "real") {
    const rows = workload.map((w) => `<tr><td>${esc(w.label)}</td><td>${esc(w.patient_name)}</td><td>${nice(w.type)}</td>
      <td class="num">${w.severity}</td><td>${pBadge(w.priority)}</td>
      <td><span class="tag ${w.status === "WAITING" ? "warn" : "ok"}">${esc(w.status.toLowerCase())}</span></td>
      <td class="num">${w.arrival}</td><td class="num">${w.burst}</td></tr>`);
    $("#workloadTable").innerHTML = table(["Req", "Patient", "Type", { label: "Sev", num: 1 }, "Priority", "Now",
      { label: "Arrival (min)", num: 1 }, { label: "Burst (min)", num: 1 }], rows,
      "No patients in the system yet. Send a few SOS requests and dispatch them, or choose 'Sample workload'.");
  } else {
    const rows = workload.map((w) => `<tr><td>${esc(w.label)}</td><td>${nice(w.type)}</td><td class="num">${w.severity}</td>
      <td>${pBadge(w.priority)}</td><td class="num">${w.arrival}</td><td class="num">${w.burst}</td></tr>`);
    $("#workloadTable").innerHTML = table(["Req", "Type", { label: "Sev", num: 1 }, "Priority", { label: "Arrival (min)", num: 1 }, { label: "Burst (min)", num: 1 }], rows);
  }
}
$("#labSource").addEventListener("change", () => loadWorkload().catch((e) => toast(e.message, true)));

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
    const { results } = await api("/api/labs/scheduler", { method: "POST", body: { aging_interval: +$("#agingInput").value, source: $("#labSource").value } });
    const keys = ["avg_waiting", "avg_turnaround", "avg_critical_waiting", "max_waiting"];
    const best = Object.fromEntries(keys.map((k) => [k, Math.min(...results.map((r) => r.metrics[k]))]));
    const rows = results.map((r) => `<tr><td><b>${esc(r.policy_name)}</b></td>${keys
      .map((k) => `<td class="num ${r.metrics[k] === best[k] ? "best" : ""}">${r.metrics[k].toFixed(2)}</td>`).join("")}
      <td>${r.gantt.filter((g) => g.id).map((g) => esc(g.label)).join(" → ")}</td></tr>`);
    const makespan = Math.max(...results.map((r) => r.metrics.makespan));
    const ticks = [0, 0.25, 0.5, 0.75, 1].map((f) => `<span>${Math.round(f * makespan)}</span>`).join("");
    const au = results.find((r) => r.policy === "AUTO"), fc = results.find((r) => r.policy === "FCFS");
    const bestBy = (k) => results.filter((r) => r.metrics[k] === best[k]).map((r) => r.policy_name).join(", ");
    const summary = au.metrics.critical_count
      ? `Critical patients (P1-P2) wait <b>${au.metrics.avg_critical_waiting}</b> min on average with <b>Automatic</b>
         (what your dispatcher uses) against <b>${fc.metrics.avg_critical_waiting}</b> min with First Come First Served.`
      : "None of these patients is critical (P1-P2), so the Critical wait column is 0 for every algorithm.";
    $("#schedResults").innerHTML = `
      <div class="card">
        <h2>Results (minutes)</h2>
        <p class="muted">${summary} Lowest average wait: <b>${esc(bestBy("avg_waiting"))}</b>.
          Lowest critical wait: <b>${esc(bestBy("avg_critical_waiting"))}</b>.
          Shortest longest-wait (fairest): <b>${esc(bestBy("max_waiting"))}</b>. Green = best in column.</p>
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
      api("/api/hospitals"), api("/api/queue"), api("/api/allocations?status=ACTIVE"),
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
    schedlab: [loadWorkload], map: [loadMap], audit: [loadAudit], users: [loadUsers],
  }[state.tab] || [];
  loaders.forEach((fn) => fn().catch((e) => toast(e.message, true)));
}

// ---------- live updates ----------
// The server pushes an event (Server-Sent Events) whenever anything changes: an SOS, a dispatch,
// a release, an automatic completion, an admin edit - even changes made from the terminal.
// We then reload only the tab you are looking at. No more polling every 5 s.
function refreshLive() {
  refreshStats();
  if (state.tab === "queue") { loadQueue().catch(() => {}); loadRecent().catch(() => {}); }
  if (state.tab === "dispatch") loadAllocations().catch(() => {});
  if (state.tab === "map") loadMap().catch(() => {});
  if (state.tab === "audit") loadAudit().catch(() => {});
  // skip the hospital grid while an admin is typing into it or a dialog is open
  if (state.tab === "hospitals" && !document.activeElement.closest?.("#hospitalGrid") && !$("#editDialog").open)
    loadHospitals().catch(() => {});
}
let liveTimer, fallbackTimer;
function setLive(on) {
  const dot = $("#liveDot");
  dot.textContent = on ? "live" : "reconnecting";
  dot.classList.toggle("on", on);
  // if the stream is down, fall back to polling every 10 s until it comes back
  clearInterval(fallbackTimer);
  if (!on) fallbackTimer = setInterval(refreshLive, 10000);
}
function startLive() {
  if (!window.EventSource) return setLive(false);
  const es = new EventSource("/api/stream");
  es.addEventListener("open", () => setLive(true));
  es.addEventListener("error", () => setLive(false));        // the browser reconnects by itself
  es.addEventListener("change", () => {
    setLive(true);
    clearTimeout(liveTimer);
    liveTimer = setTimeout(refreshLive, 250);                 // group bursts of changes
  });
}
// the "auto-completes in m:ss" countdown ticks every second without asking the server
setInterval(() => {
  if (state.tab === "dispatch") loadAllocationsClock();
}, 1000);
function loadAllocationsClock() {
  document.querySelectorAll("#allocTable [data-ends]").forEach((el) => (el.innerHTML = endsIn(el.dataset.ends)));
}

loadMe()
  .then(loadMeta)
  .then(() => {
    showTab(isDispatcher() ? "dispatch" : "hospitals");
    refreshStats();
    startLive();
  })
  .catch((e) => { if (!/log in|Patient portal/.test(e.message)) toast(e.message, true); });


// ---------- generic edit dialog ----------
// fields: [{name, label, type, value, options, required, min, max, maxlength, step}]
function openDialog(title, fields, onSave) {
  const dlg = $("#editDialog");
  $("#editTitle").textContent = title;
  $("#editError").textContent = "";
  $("#editFields").innerHTML = fields.map((f) => {
    const attrs = ["required", "min", "max", "maxlength", "step"].filter((a) => f[a] != null && f[a] !== false)
      .map((a) => (f[a] === true ? a : `${a}="${esc(f[a])}"`)).join(" ");
    if (f.type === "select")
      return `<label>${esc(f.label)}<select name="${f.name}">${f.options.map((o) =>
        `<option value="${esc(o.value)}" ${o.value === f.value ? "selected" : ""}>${esc(o.label)}</option>`).join("")}</select></label>`;
    if (f.type === "checkbox")
      return `<label class="check"><input type="checkbox" name="${f.name}" ${f.value ? "checked" : ""}> ${esc(f.label)}</label>`;
    return `<label>${esc(f.label)}<input name="${f.name}" type="${f.type || "text"}" value="${esc(f.value ?? "")}" ${attrs}></label>`;
  }).join("");
  const form = $("#editForm");
  form.onsubmit = async (e) => {
    if (e.submitter?.value !== "save") return;           // Cancel just closes
    e.preventDefault();
    if (!form.reportValidity()) return;
    const data = {};
    fields.forEach((f) => {
      const el = form.elements[f.name];
      data[f.name] = f.type === "checkbox" ? el.checked : el.value;
    });
    $("#editSave").disabled = true;
    try { await onSave(data); dlg.close(); } catch (ex) { $("#editError").textContent = ex.message; }
    finally { $("#editSave").disabled = false; }
  };
  dlg.showModal();
  form.querySelector("input, select")?.focus();
}

// ---------- map ----------
const P_COLORS = { 1: "#b91c1c", 2: "#ef4444", 3: "#1565c0", 4: "#16a34a", 5: "#94a3b8" };
function ensureMap() {
  if (state.map) return true;
  if (!window.L) {
    $("#map").innerHTML = '<p class="empty map-off">The map library could not be loaded (no internet?). The tables below still work.</p>';
    return false;
  }
  state.map = L.map("map").setView([30.30, 78.04], 11);
  L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 18, attribution: "&copy; OpenStreetMap contributors",
  }).addTo(state.map);
  state.mapLayer = L.layerGroup().addTo(state.map);
  state.mapFitted = false;
  return true;
}

async function loadMap() {
  const data = await api("/api/map");
  const r = data.routing;
  $("#routingNote").innerHTML = r.mode === "ROAD" ? `Road routing on (${esc(r.server)})`
    : `<span class="tag warn">estimate</span> ${esc(r.reason)}`;
  const rows = data.active.map((a) => `<tr><td>#${a.allocation_id}</td><td>${esc(a.patient_name)} ${pBadge(a.priority)}</td>
    <td>${esc(a.hospital_name)}</td><td class="num">${a.route.km}</td><td class="num">${a.route.minutes}</td>
    <td>${srcTag(a.route.source)}</td></tr>`);
  $("#mapCases").innerHTML = `<h3 class="sub-title">Active cases</h3>` + table(["ID", "Patient", "Hospital",
    { label: "Road km", num: 1 }, { label: "Drive min", num: 1 }, "Distance"], rows, "No active cases.");
  if (!ensureMap()) return;
  setTimeout(() => state.map.invalidateSize(), 0);          // the tab was hidden while the map was created
  const layer = state.mapLayer;
  layer.clearLayers();
  const bounds = [];
  data.hospitals.forEach((h) => {
    const icon = L.divIcon({ className: "", html: `<div class="hosp-pin ${h.free_icu > 0 ? "" : "full"}">${h.free_icu}</div>`, iconSize: [28, 28], iconAnchor: [14, 14] });
    L.marker([h.latitude, h.longitude], { icon, title: h.name }).addTo(layer)
      .bindPopup(`<b>${esc(h.name)}</b><br>${esc(h.area)}<br>Free ICU: ${h.free_icu} &middot; Ventilators: ${h.free_vent}
        <br>All units: ${h.free_units}/${h.total_units}${h.phone ? `<br>&#9742; ${esc(h.phone)}` : ""}`);
    bounds.push([h.latitude, h.longitude]);
  });
  data.waiting.forEach((w) => {
    L.circleMarker([w.latitude, w.longitude], { radius: 8, color: "#fff", weight: 2, fillColor: P_COLORS[w.priority], fillOpacity: 0.95 })
      .addTo(layer).bindPopup(`<b>Waiting:</b> #${w.request_id} ${esc(w.patient_name)}<br>${nice(w.emergency_type)} &middot; P${w.priority}`);
    bounds.push([w.latitude, w.longitude]);
  });
  data.active.forEach((a) => {
    L.polyline(a.route.path, { color: P_COLORS[a.priority], weight: 4, opacity: 0.8, dashArray: a.route.source === "ROAD" ? null : "6 6" })
      .addTo(layer).bindPopup(`#${a.allocation_id} ${esc(a.patient_name)} &rarr; ${esc(a.hospital_name)}<br>${a.route.km} km, ~${a.route.minutes} min (${a.route.source === "ROAD" ? "road" : "estimate"})`);
    L.circleMarker([a.latitude, a.longitude], { radius: 6, color: P_COLORS[a.priority], weight: 2, fillColor: "#fff", fillOpacity: 1 }).addTo(layer);
  });
  if (!state.mapFitted && bounds.length) { state.map.fitBounds(bounds, { padding: [30, 30] }); state.mapFitted = true; }
}

// ---------- audit log ----------
const EVENT_CLASS = { ALLOCATED: "ok", RELEASED: "", AUTO_COMPLETED: "ok", ROLLBACK: "bad", NO_CAPACITY: "bad", CANCELLED: "bad", LOGIN: "info" };
async function loadAudit() {
  const { log } = await api("/api/audit?limit=150");
  const rows = log.map((l) => `<tr><td>${esc(String(l.created_at).slice(0, 19))}</td>
    <td><span class="tag ${EVENT_CLASS[l.event_type] ?? "warn"}">${esc(l.event_type)}</span></td>
    <td>${esc(l.details)}</td><td>${esc(l.username || "")}</td></tr>`);
  $("#auditTable").innerHTML = table(["Time", "Event", "Details", "By"], rows, "No events yet.");
}

// ---------- users (system admin) ----------
async function loadUsers() {
  if (!state.sysadmin) return;
  const [{ users }, { hospitals }] = await Promise.all([api("/api/admin/users"), api("/api/hospitals")]);
  $("#userHospital").innerHTML = '<option value="">None (all hospitals)</option>'
    + hospitals.map((h) => `<option value="${h.hospital_id}">${esc(h.name)}</option>`).join("");
  const rows = users.map((u) => `<tr class="${u.is_active ? "" : "muted"}"><td><b>${esc(u.username)}</b><div class="muted small">${esc(u.full_name)}</div></td>
    <td>${nice(u.role)}</td><td>${esc(u.hospital_name || (u.role === "HOSPITAL_ADMIN" ? "all (system admin)" : ""))}</td>
    <td>${u.last_login_at ? esc(String(u.last_login_at).slice(0, 16)) : '<span class="muted">never</span>'}</td>
    <td>${u.user_id === state.user.user_id ? '<span class="muted small">you</span>'
      : `<button class="ghost small" data-user-active="${u.user_id}" data-to="${u.is_active ? 0 : 1}">${u.is_active ? "Disable" : "Enable"}</button>`}</td></tr>`);
  $("#usersTable").innerHTML = table(["User", "Role", "Hospital", "Last login", ""], rows);
  $("#userHospital").disabled = $("#userRole").value !== "HOSPITAL_ADMIN";
}
$("#usersTable").addEventListener("click", (e) => {
  const t = e.target;
  if (!t.dataset.userActive) return;
  withButton(t, async () => {
    await api(`/api/admin/users/${t.dataset.userActive}/active`, { method: "POST", body: { active: t.dataset.to === "1" } });
    loadUsers();
  });
});
$("#userForm").addEventListener("submit", (e) => {
  e.preventDefault();
  const btn = e.target.querySelector("button[type=submit]");
  withButton(btn, async () => {
    await api("/api/admin/users", { method: "POST", body: Object.fromEntries(new FormData(e.target)) });
    toast("Account created");
    e.target.reset();
    loadUsers();
  });
});
$("#userRole").addEventListener("change", (e) => ($("#userHospital").disabled = e.target.value !== "HOSPITAL_ADMIN"));
