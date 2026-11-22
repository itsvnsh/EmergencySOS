// EmergencySOS mobile portal: send an SOS, then follow it live until a hospital is assigned.
// Tier 2: English / Hindi (portal_i18n.js) and an alert (sound + vibration + notification)
// the moment a hospital is assigned.
"use strict";

const $ = (s) => document.querySelector(s);
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const nice = (s) => esc(String(s ?? "").replace(/_/g, " ").toLowerCase().replace(/\b\w/g, (c) => c.toUpperCase()));

const st = { meta: null, user: null, type: null, sev: 3, map: null, pin: null, track: null, trackMap: null, es: null, poll: null,
  lang: "en", last: null, lastStatus: null, alerts: true, audio: null, locLabel: null };

// localStorage can be missing or blocked (private mode, some embeds): never let it break the page
const store = {
  get(k) { try { return JSON.parse(localStorage.getItem(k)); } catch { return null; } },
  set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch { /* ignore */ } },
  del(k) { try { localStorage.removeItem(k); } catch { /* ignore */ } },
};

// ---------------- language ----------------
function t(key, vars = {}) {
  const s = I18N[st.lang][key] ?? I18N.en[key] ?? key;
  return s.replace(/\{(\w+)\}/g, (_, k) => vars[k] ?? "");
}
// a server code (CARDIAC, ICU_BED, ...) in the chosen language, escaped
const code = (group, c) => esc(I18N_CODES[st.lang][group]?.[c] ?? I18N_CODES.en[group]?.[c] ?? String(c ?? "").replace(/_/g, " "));

function applyLang() {
  document.documentElement.lang = st.lang;
  document.querySelectorAll("[data-i18n]").forEach((el) => { el.textContent = t(el.dataset.i18n); });
  document.querySelectorAll("[data-i18n-html]").forEach((el) => { el.innerHTML = t(el.dataset.i18nHtml); });
  const lb = $("#langBtn");
  lb.textContent = t("langSwitch");
  lb.lang = st.lang === "en" ? "hi" : "en";
  if (st.meta) { renderChoices(); renderPreset(); }
  if (st.locLabel) $("#locText").textContent = locText();
  renderWho();
  renderAlertBtn();
  if (st.last) renderStatus(st.last);
}
$("#langBtn").addEventListener("click", () => {
  st.lang = st.lang === "en" ? "hi" : "en";
  store.set("sos_lang", st.lang);
  applyLang();
});

async function api(path, opts = {}) {
  const o = { headers: { "Content-Type": "application/json" }, ...opts };
  if (o.body && typeof o.body !== "string") o.body = JSON.stringify(o.body);
  let res, data;
  try { res = await fetch(path, o); data = await res.json(); } catch { throw new Error(t("noServer")); }
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
  applyLang();
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

function renderChoices() {
  $("#types").innerHTML = st.meta.emergency_types.map((c) =>
    `<button type="button" class="type" role="radio" aria-checked="${c === st.type}" data-t="${c}">${code("type", c)}<small>${code("typeHelp", c)}</small></button>`).join("");
  $("#sev").innerHTML = [1, 2, 3, 4, 5].map((v) =>
    `<button type="button" role="radio" aria-checked="${v === st.sev}" data-v="${v}">${v}<small>${code("sev", v)}</small></button>`).join("");
}
function renderPreset() {
  const old = $("#preset").value;
  $("#preset").innerHTML = `<option value="">${esc(t("preset"))}</option>`
    + st.meta.locations.map((l, i) => `<option value="${i}">${esc(l.name)}</option>`).join("");
  $("#preset").value = old;
}

function renderWho() {
  const u = st.user;
  if (!u) { $("#who").innerHTML = `<a href="/login">${esc(t("login"))}</a>`; return; }
  const link = u.role === "PATIENT" ? "" : ` · <a href="/">Console</a>`;
  $("#who").innerHTML = `${esc(u.full_name)}${link} <button id="logout">${esc(t("logout"))}</button>`;
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
  if (!window.L) { $("#pickMap").classList.add("hidden"); $("#locText").textContent = t("mapOffline"); return; }
  st.map = L.map("pickMap", { zoomControl: true }).setView([30.30, 78.04], 12);
  L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", { maxZoom: 18, attribution: "&copy; OpenStreetMap" }).addTo(st.map);
  st.pin = L.marker([30.30, 78.04], { draggable: true }).addTo(st.map);
  st.pin.on("dragend", () => { const p = st.pin.getLatLng(); setLocation(p.lat, p.lng, { key: "pinOnMap" }, false); });
  st.map.on("click", (e) => setLocation(e.latlng.lat, e.latlng.lng, { key: "pinOnMap" }, false));
}

// label: a place name, or {key} for a translated one ("Pin on the map")
function locText() {
  const l = st.locLabel;
  return `${l.label.key ? t(l.label.key) : l.label}: ${(+l.lat).toFixed(4)}, ${(+l.lon).toFixed(4)}`;
}
function setLocation(lat, lon, label, moveMap = true) {
  $("#lat").value = (+lat).toFixed(6);
  $("#lon").value = (+lon).toFixed(6);
  st.locLabel = { lat, lon, label };
  $("#locText").textContent = locText();
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
  if (!navigator.geolocation) return toast(t("noGeo"), true);
  const btn = $("#geoBtn");
  btn.disabled = true; btn.textContent = t("geoFinding");
  navigator.geolocation.getCurrentPosition(
    (p) => { setLocation(p.coords.latitude, p.coords.longitude, { key: "yourLocation" }); done(); },
    (err) => {
      done();
      // browsers only share location on https or localhost
      toast(err.code === 1 ? t("geoDenied") : t("geoFailed"), true);
    },
    { enableHighAccuracy: true, timeout: 10000 }
  );
  function done() { btn.disabled = false; btn.textContent = t("geo"); }
});

// ---------------- send ----------------
$("#sosForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const err = $("#formError");
  err.textContent = "";
  if (!st.type) { err.textContent = t("errType"); $("#types").scrollIntoView({ behavior: "smooth", block: "center" }); return; }
  if (!$("#pname").value.trim()) { err.textContent = t("errName"); $("#pname").focus(); return; }
  unlockAudio();                  // browsers only allow sound after a tap: this tap counts
  const btn = $("#sendBtn");
  btn.disabled = true; btn.textContent = t("sending");
  try {
    const data = Object.fromEntries(new FormData(e.target));
    const res = await api("/api/sos", { method: "POST", body: { ...data, emergency_type: st.type, severity: st.sev } });
    toast(t("received"));
    startTracking({ id: res.request.request_id, token: res.track_token });
    if (st.user?.role === "PATIENT") loadMine();
  } catch (ex) {
    err.textContent = ex.message;
  } finally {
    btn.disabled = false; btn.textContent = t("send");
  }
});

// ---------------- track ----------------
function startTracking(tr, quiet = false) {
  st.track = tr;
  st.lastStatus = null; st.last = null;
  store.set("sos_track", tr);
  history.replaceState(null, "", `#track=${tr.id}.${tr.token || ""}`);
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
  const before = st.lastStatus;
  st.last = s; st.lastStatus = s.request.status;
  renderStatus(s);
  // alert only on a change we saw happen (not when reopening an old, already assigned request)
  if (before === "PENDING" && s.request.status === "ALLOCATED") hospitalAssignedAlert(s);
  if (["COMPLETED", "CANCELLED"].includes(s.request.status)) stopTracking();
}

function renderStatus(s) {
  const r = s.request, a = s.allocation;
  const p = `<span class="badge p${r.priority}">P${r.priority} ${code("priority", r.priority)}</span>`;
  let card;
  if (r.status === "PENDING") {
    card = `<div class="card status wait"><span class="pulse"></span>
      <div class="big">${esc(t("reqReceived"))}</div>
      <p>${p} &middot; ${s.queue ? esc(t("queue", { q: I18N_CODES[st.lang].queue[s.queue] || s.queue })) : ""}</p>
      <p>${t("position", { pos: s.queue_position ?? "-", total: +s.queue_length || 0 })}</p>
      <p class="hint">${esc(t("stay"))}</p></div>`;
  } else if (r.status === "ALLOCATED" && a) {
    card = `<div class="card status ok"><div class="big">${esc(t("assigned"))}</div>
      <p><b>${esc(a.hospital_name)}</b><br>${esc(a.hospital_area)}</p>
      <p>${s.route ? t("route", { km: +s.route.km, min: Math.round(s.route.minutes) }) + (s.route.source === "ROAD" ? "" : esc(t("estimate"))) : ""}</p>
      ${a.hospital_phone ? `<a class="call-btn" href="tel:${esc(a.hospital_phone.replace(/[^0-9+]/g, ""))}">&#9742; ${esc(t("call"))} ${esc(a.hospital_phone)}</a>` : ""}</div>`;
  } else if (r.status === "COMPLETED") {
    card = `<div class="card status done"><div class="big">${esc(t("completed"))}</div>
      <p>${a ? t("treatedAt", { h: esc(a.hospital_name) }) : ""} ${esc(t("takeCare"))}</p></div>`;
  } else {
    card = `<div class="card status bad"><div class="big">${esc(t("cancelled"))}</div><p>${esc(t("stillNeed"))}</p></div>`;
  }
  $("#statusCard").outerHTML = card.replace('<div class="card', '<div id="statusCard" class="card');

  $("#detailCard").innerHTML = `<h2>${esc(t("request", { id: r.request_id }))}</h2><dl>
    <dt>${esc(t("patient"))}</dt><dd>${esc(r.patient_name)}</dd>
    <dt>${esc(t("emergency"))}</dt><dd>${t("severityN", { type: code("type", r.emergency_type), sev: +r.severity })}</dd>
    <dt>${esc(t("needs"))}</dt><dd>${code("resource", r.required_resource)} + ${code("specialist", r.required_specialist)}</dd>
    ${a?.doctor_name ? `<dt>${esc(t("doctor"))}</dt><dd>${esc(a.doctor_name)} (${code("specialist", a.specialization)})</dd>` : ""}
    ${a?.ambulance_resource_id ? `<dt>${esc(t("ambulance"))}</dt><dd>${esc(a.ambulance_returned_at || a.status !== "ACTIVE" ? t("delivered") : t("onTheWay"))}</dd>` : ""}
    <dt>${esc(t("sentAt"))}</dt><dd>${esc(String(r.created_at).slice(11, 16))}</dd></dl>
    <p class="hint">${esc(t("keepOpen", { id: r.request_id }))}</p>`;
  $("#alertBar").classList.toggle("hidden", r.status !== "PENDING");

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
    <span><b>#${r.request_id} ${code("type", r.emergency_type)}</b><small>${esc(String(r.created_at).slice(0, 16))}${r.hospital_name ? ` · ${esc(r.hospital_name)}` : ""}</small></span>
    <span class="tag ${esc(r.status)}">${code("status", r.status)}</span></button>`).join("");
}
$("#myList").addEventListener("click", (e) => {
  const b = e.target.closest(".req");
  if (b) startTracking({ id: +b.dataset.id, token: "" });     // the patient's own login is enough
});

// ---------------- alert when a hospital is assigned ----------------
// Sound is made with the Web Audio API (no sound file to download). Browsers only allow
// sound after the user tapped something, so the SEND tap (or the alert button) unlocks it.
function unlockAudio() {
  try {
    const AC = window.AudioContext || window.webkitAudioContext;
    if (!AC) return;
    st.audio = st.audio || new AC();
    if (st.audio.state === "suspended") st.audio.resume();
  } catch { /* no sound on this device */ }
}

function playChime() {
  const ctx = st.audio;
  if (!ctx || ctx.state !== "running") return false;
  const now = ctx.currentTime;
  // three rising tones, played twice: easy to hear, not a siren
  [0, 0.75].forEach((rep) => [880, 1175, 1568].forEach((f, i) => {
    const o = ctx.createOscillator(), g = ctx.createGain();
    const t0 = now + rep + i * 0.18;
    o.type = "sine"; o.frequency.value = f;
    g.gain.setValueAtTime(0.0001, t0);
    g.gain.exponentialRampToValueAtTime(0.35, t0 + 0.02);
    g.gain.exponentialRampToValueAtTime(0.0001, t0 + 0.35);
    o.connect(g).connect(ctx.destination);
    o.start(t0); o.stop(t0 + 0.4);
  }));
  return true;
}

function hospitalAssignedAlert(s, test = false) {
  if (!st.alerts && !test) return null;
  const sound = playChime();
  let buzz = false;
  try { buzz = navigator.vibrate ? navigator.vibrate([400, 150, 400, 150, 800]) : false; } catch { /* not allowed */ }
  const card = $("#statusCard");
  card.classList.remove("flash"); void card.offsetWidth; card.classList.add("flash");
  if (!test) {
    document.title = "✅ " + t("alertTitle");
    // a phone notification if the page is in the background and the user allowed it
    if (document.hidden && "Notification" in window && Notification.permission === "granted") {
      try {
        new Notification(t("alertTitle"), { tag: "sos-assigned", body: t("notifyBody", {
          h: s?.allocation?.hospital_name || "", min: s?.route ? Math.round(s.route.minutes) : "?" }) });
      } catch { /* some phones only allow notifications from a service worker */ }
    }
  }
  st.lastAlert = { sound, buzz, at: Date.now() };       // for the demo / tests
  return st.lastAlert;
}

function renderAlertBtn() {
  const b = $("#alertBtn");
  b.textContent = (st.alerts ? "🔔 " : "🔕 ") + t(st.alerts ? "alertsOn" : "alertsOff");
  b.setAttribute("aria-pressed", String(st.alerts));
  b.classList.toggle("off", !st.alerts);
}
$("#alertBtn").addEventListener("click", () => {
  st.alerts = !st.alerts;
  store.set("sos_alerts", st.alerts);
  if (st.alerts) {
    unlockAudio();
    if ("Notification" in window && Notification.permission === "default") {
      try { Notification.requestPermission(); } catch { /* old browsers */ }
    }
  }
  renderAlertBtn();
});
$("#testAlertBtn").addEventListener("click", () => { unlockAudio(); setTimeout(() => hospitalAssignedAlert(st.last, true), 60); });
document.addEventListener("visibilitychange", () => { if (!document.hidden) document.title = "Send SOS · EmergencySOS"; });

st.lang = store.get("sos_lang") === "hi" ? "hi" : "en";
st.alerts = store.get("sos_alerts") !== false;
init().catch((e) => toast(e.message, true));

