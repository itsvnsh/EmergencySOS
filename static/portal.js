// EmergencySOS mobile portal: send an SOS, then follow it live until a hospital is assigned.
"use strict";

const $ = (s) => document.querySelector(s);
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const nice = (s) => esc(String(s ?? "").replace(/_/g, " ").toLowerCase().replace(/\b\w/g, (c) => c.toUpperCase()));

const TYPE_HELP = {
  CARDIAC: "chest pain, heart", STROKE: "face droop, slurred speech", TRAUMA: "accident, injury, fall",
  RESPIRATORY: "can't breathe", BURN: "fire, hot liquid", MATERNITY: "pregnancy, labour",
  POISONING: "poison, overdose", GENERAL: "fever, weakness, other",
};
const SEV = { 1: "mild", 2: "low", 3: "moderate", 4: "serious", 5: "critical" };

const st = { meta: null, user: null, type: null, sev: 3, map: null, pin: null, track: null, trackMap: null, es: null, poll: null };

// localStorage can be missing or blocked (private mode, some embeds): never let it break the page
const store = {
  get(k) { try { return JSON.parse(localStorage.getItem(k)); } catch { return null; } },
  set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch { /* ignore */ } },
  del(k) { try { localStorage.removeItem(k); } catch { /* ignore */ } },
};

async function api(path, opts = {}) {
  const o = { headers: { "Content-Type": "application/json" }, ...opts };
  if (o.body && typeof o.body !== "string") o.body = JSON.stringify(o.body);
  let res, data;
  try { res = await fetch(path, o); data = await res.json(); } catch { throw new Error("No connection to the server. Call 112."); }
  if (!res.ok || data.ok === false) throw new Error(data.error || `Error ${res.status}`);
  return data;
}

let toastT;
function toast(msg, err = false) {
  const t = $("#toast");
  t.textContent = msg; t.className = "toast show" + (err ? " error" : "");
  clearTimeout(toastT); toastT = setTimeout(() => (t.className = "toast"), 3500);
}

// ---------------- setup ----------------
async function init() {
  const [meta, me] = await Promise.all([api("/api/meta"), api("/api/me")]);
  st.meta = meta; st.user = me.user;
  renderWho();
  $("#types").innerHTML = meta.emergency_types.map((t) =>
    `<button type="button" class="type" role="radio" aria-checked="false" data-t="${t}">${nice(t)}<small>${esc(TYPE_HELP[t] || "")}</small></button>`).join("");
  $("#sev").innerHTML = [1, 2, 3, 4, 5].map((v) =>
    `<button type="button" role="radio" aria-checked="${v === 3}" data-v="${v}">${v}<small>${SEV[v]}</small></button>`).join("");
  $("#preset").innerHTML = `<option value="">Or choose a place…</option>`
    + meta.locations.map((l, i) => `<option value="${i}">${esc(l.name)}</option>`).join("");
  if (st.user?.role === "PATIENT") {
    $("#pname").value = st.user.full_name;
    $("#pphone").value = st.user.phone || "";
    loadMine();
  }
  setupMap();
  const first = meta.locations[0];
  setLocation(first.lat, first.lon, first.name);

  // resume tracking: link (#track=id.token) or the last request sent from this phone
  const m = location.hash.match(/track=(\d+)\.([0-9a-f]+)/);
  const saved = m ? { id: +m[1], token: m[2] } : store.get("sos_track");
  if (saved) startTracking(saved, true);
}

function renderWho() {
  const u = st.user;
  if (!u) { $("#who").innerHTML = `<a href="/login">Log in</a>`; return; }
  const link = u.role === "PATIENT" ? "" : ` · <a href="/">Console</a>`;
  $("#who").innerHTML = `${esc(u.full_name)}${link} <button id="logout">Log out</button>`;
  $("#logout").onclick = async () => { await api("/api/logout", { method: "POST" }); location.reload(); };
}

$("#types").addEventListener("click", (e) => {
  const b = e.target.closest(".type"); if (!b) return;
  st.type = b.dataset.t;
  document.querySelectorAll(".type").forEach((x) => x.setAttribute("aria-checked", x === b));
});
$("#sev").addEventListener("click", (e) => {
  const b = e.target.closest("button"); if (!b) return;
  st.sev = +b.dataset.v;
  document.querySelectorAll("#sev button").forEach((x) => x.setAttribute("aria-checked", x === b));
});

// ---------------- location ----------------
function setupMap() {
  if (!window.L) { $("#pickMap").classList.add("hidden"); $("#locText").textContent = "Map not available offline: use your location or pick a place."; return; }
  st.map = L.map("pickMap", { zoomControl: true }).setView([30.30, 78.04], 12);
  L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", { maxZoom: 18, attribution: "&copy; OpenStreetMap" }).addTo(st.map);
  st.pin = L.marker([30.30, 78.04], { draggable: true }).addTo(st.map);
  st.pin.on("dragend", () => { const p = st.pin.getLatLng(); setLocation(p.lat, p.lng, "Pin on the map", false); });
  st.map.on("click", (e) => setLocation(e.latlng.lat, e.latlng.lng, "Pin on the map", false));
}

function setLocation(lat, lon, label, moveMap = true) {
  $("#lat").value = (+lat).toFixed(6);
  $("#lon").value = (+lon).toFixed(6);
  $("#locText").textContent = `${label}: ${(+lat).toFixed(4)}, ${(+lon).toFixed(4)}`;
  if (st.pin) {
    st.pin.setLatLng([lat, lon]);
    if (moveMap) st.map.setView([lat, lon], 14);
  }
}
$("#preset").addEventListener("change", (e) => {
  const l = st.meta.locations[e.target.value];
  if (l) setLocation(l.lat, l.lon, l.name);
});
$("#geoBtn").addEventListener("click", () => {
  if (!navigator.geolocation) return toast("Location is not available on this device", true);
  const btn = $("#geoBtn");
  btn.disabled = true; btn.textContent = "Finding you…";
  navigator.geolocation.getCurrentPosition(
    (p) => { setLocation(p.coords.latitude, p.coords.longitude, "Your location"); done(); },
    (err) => {
      done();
      // browsers only share location on https or localhost
      toast(err.code === 1 ? "Location permission denied - tap the map instead" : "Could not get your location - tap the map", true);
    },
    { enableHighAccuracy: true, timeout: 10000 }
  );
  function done() { btn.disabled = false; btn.innerHTML = "&#9737; Use my current location"; }
});

// ---------------- send ----------------
$("#sosForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const err = $("#formError");
  err.textContent = "";
  if (!st.type) { err.textContent = "Choose what happened (step 1)."; $("#types").scrollIntoView({ behavior: "smooth", block: "center" }); return; }
  if (!$("#pname").value.trim()) { err.textContent = "Enter the patient's name."; $("#pname").focus(); return; }
  const btn = $("#sendBtn");
  btn.disabled = true; btn.textContent = "SENDING…";
  try {
    const data = Object.fromEntries(new FormData(e.target));
    const res = await api("/api/sos", { method: "POST", body: { ...data, emergency_type: st.type, severity: st.sev } });
    toast("SOS received");
    startTracking({ id: res.request.request_id, token: res.track_token });
    if (st.user?.role === "PATIENT") loadMine();
  } catch (ex) {
    err.textContent = ex.message;
  } finally {
    btn.disabled = false; btn.textContent = "SEND SOS";
  }
});

// ---------------- track ----------------
function startTracking(t, quiet = false) {
  st.track = t;
  store.set("sos_track", t);
  history.replaceState(null, "", `#track=${t.id}.${t.token || ""}`);
  $("#sendView").classList.add("hidden");
  $("#trackView").classList.remove("hidden");
  window.scrollTo(0, 0);
  refreshTrack(quiet);
  // live: the server pushes an event whenever anything changes; poll slowly as a backup
  if (window.EventSource && !st.es) {
    st.es = new EventSource("/api/stream");
    st.es.addEventListener("change", () => refreshTrack(true));
  }
  clearInterval(st.poll);
  st.poll = setInterval(() => refreshTrack(true), 20000);
}

function stopTracking() {
  clearInterval(st.poll);
  if (st.es) { st.es.close(); st.es = null; }
}

async function refreshTrack(quiet) {
  if (!st.track) return;
  let s;
  try {
    s = await api(`/api/sos/${st.track.id}/status?token=${encodeURIComponent(st.track.token || "")}`);
  } catch (ex) {
    if (!quiet) toast(ex.message, true);
    if (/not found/i.test(ex.message)) { store.del("sos_track"); showSend(); }
    return;
  }
  renderStatus(s);
  if (["COMPLETED", "CANCELLED"].includes(s.request.status)) stopTracking();
}

function renderStatus(s) {
  const r = s.request, a = s.allocation;
  const p = `<span class="badge p${r.priority}">P${r.priority} ${esc(s.priority_label)}</span>`;
  let card;
  if (r.status === "PENDING") {
    card = `<div class="card status wait"><span class="pulse"></span>
      <div class="big">Help request received</div>
      <p>${p} &middot; ${s.queue ? `${esc(s.queue)} queue` : ""}</p>
      <p>Position <b>${s.queue_position ?? "-"}</b> of ${s.queue_length} waiting. A dispatcher is finding the right hospital.</p>
      <p class="hint">Stay with the patient. This page updates by itself.</p></div>`;
  } else if (r.status === "ALLOCATED" && a) {
    card = `<div class="card status ok"><div class="big">Hospital assigned</div>
      <p><b>${esc(a.hospital_name)}</b><br>${esc(a.hospital_area)}</p>
      <p>${s.route ? `${s.route.km} km &middot; about <b>${Math.round(s.route.minutes)} min</b> by road${s.route.source === "ROAD" ? "" : " (estimate)"}` : ""}</p>
      ${a.hospital_phone ? `<a class="call-btn" href="tel:${esc(a.hospital_phone.replace(/[^0-9+]/g, ""))}">&#9742; Call ${esc(a.hospital_phone)}</a>` : ""}</div>`;
  } else if (r.status === "COMPLETED") {
    card = `<div class="card status done"><div class="big">Case completed</div>
      <p>${a ? `Treated at <b>${esc(a.hospital_name)}</b>.` : ""} Take care.</p></div>`;
  } else {
    card = `<div class="card status bad"><div class="big">Request cancelled</div><p>Call 112 if you still need help.</p></div>`;
  }
  $("#statusCard").outerHTML = card.replace('<div class="card', '<div id="statusCard" class="card');

  $("#detailCard").innerHTML = `<h2>Request #${r.request_id}</h2><dl>
    <dt>Patient</dt><dd>${esc(r.patient_name)}</dd>
    <dt>Emergency</dt><dd>${nice(r.emergency_type)}, severity ${r.severity}</dd>
    <dt>Needs</dt><dd>${nice(r.required_resource)} + ${nice(r.required_specialist)}</dd>
    ${a?.doctor_name ? `<dt>Doctor</dt><dd>${esc(a.doctor_name)} (${nice(a.specialization)})</dd>` : ""}
    <dt>Sent at</dt><dd>${esc(String(r.created_at).slice(11, 16))}</dd></dl>
    <p class="hint">Keep this page open, or save this link to come back: request #${r.request_id}.</p>`;

  drawRoute(s);
}

function drawRoute(s) {
  const box = $("#trackMap");
  if (!window.L || !s.route || !s.allocation) { box.classList.add("hidden"); return; }
  box.classList.remove("hidden");
  if (!st.trackMap) {
    st.trackMap = L.map("trackMap");
    L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", { maxZoom: 18, attribution: "&copy; OpenStreetMap" }).addTo(st.trackMap);
    st.trackLayer = L.layerGroup().addTo(st.trackMap);
  }
  st.trackLayer.clearLayers();
  const line = L.polyline(s.route.path, { color: "#dc2626", weight: 5, dashArray: s.route.source === "ROAD" ? null : "8 8" }).addTo(st.trackLayer);
  L.circleMarker(s.route.path[0], { radius: 8, color: "#fff", weight: 2, fillColor: "#dc2626", fillOpacity: 1 }).addTo(st.trackLayer).bindPopup("Patient");
  L.marker(s.route.path[s.route.path.length - 1]).addTo(st.trackLayer).bindPopup(esc(s.allocation.hospital_name));
  setTimeout(() => { st.trackMap.invalidateSize(); st.trackMap.fitBounds(line.getBounds(), { padding: [24, 24] }); }, 0);
}

function showSend() {
  stopTracking();
  st.track = null;
  history.replaceState(null, "", location.pathname);
  $("#trackView").classList.add("hidden");
  $("#sendView").classList.remove("hidden");
}
$("#newBtn").addEventListener("click", () => { store.del("sos_track"); showSend(); });

// ---------------- my requests (logged-in patient) ----------------
async function loadMine() {
  const { requests } = await api("/api/my/requests");
  if (!requests.length) return;
  $("#myRequests").classList.remove("hidden");
  $("#myList").innerHTML = requests.map((r) => `<button class="req" data-id="${r.request_id}">
    <span><b>#${r.request_id} ${nice(r.emergency_type)}</b><small>${esc(String(r.created_at).slice(0, 16))}${r.hospital_name ? ` · ${esc(r.hospital_name)}` : ""}</small></span>
    <span class="tag ${r.status}">${esc(r.status.toLowerCase())}</span></button>`).join("");
}
$("#myList").addEventListener("click", (e) => {
  const b = e.target.closest(".req");
  if (b) startTracking({ id: +b.dataset.id, token: "" });     // the patient's own login is enough
});

init().catch((e) => toast(e.message, true));
