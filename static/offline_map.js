// EmergencySOS - map base layer with an offline fallback (Tier 3).
// Weak college Wi-Fi must not break the demo. Two layers of defence:
//
//  1. a service worker (/sw.js) keeps every map tile you have looked at; with no
//     internet those tiles come from the cache, so a map seen once still works.
//  2. if street tiles still cannot be loaded (no internet and nothing cached, or
//     tiles too slow), the map switches to a drawn OFFLINE map: a light grid with
//     the known places of Dehradun labelled. Hospital and patient markers, routes
//     and distances are drawn by the app as usual, so the demo still makes sense.
//     When the connection comes back the street map returns by itself.
//
// Usage: sosBaseLayer(map, { places: [{name, lat, lon}] })   (opts.url replaces the tile URL, for testing)
"use strict";

(function () {
  const TILE_URL = "https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png";
  const ATTRIBUTION = "&copy; OpenStreetMap contributors";
  const SLOW_MS = 7000;            // no tile at all after this long = treat as offline
  const ERRORS_BEFORE_OFFLINE = 3;

  // 1. tile cache (browsers allow service workers only on https or localhost)
  if ("serviceWorker" in navigator && window.isSecureContext) {
    navigator.serviceWorker.register("/sw.js").catch(() => { /* the map works without it */ });
  }

  // 2. the drawn offline map
  function offlineLayer(map, places) {
    const layer = L.gridLayer({ maxZoom: 18, attribution: "Offline map (street tiles not reachable)" });
    layer.createTile = function (coords) {
      const size = this.getTileSize();
      const tile = document.createElement("canvas");
      tile.width = size.x; tile.height = size.y;
      const g = tile.getContext("2d");
      g.fillStyle = "#eef3f8"; g.fillRect(0, 0, size.x, size.y);
      g.strokeStyle = "#d3dde9"; g.lineWidth = 1;
      g.beginPath(); g.moveTo(0.5, 0); g.lineTo(0.5, size.y); g.moveTo(0, 0.5); g.lineTo(size.x, 0.5); g.stroke();
      // a fine grid inside each tile
      g.strokeStyle = "#e3eaf2";
      g.beginPath();
      for (let i = size.x / 4; i < size.x; i += size.x / 4) { g.moveTo(i + 0.5, 0); g.lineTo(i + 0.5, size.y); g.moveTo(0, i + 0.5); g.lineTo(size.x, i + 0.5); }
      g.stroke();
      // corner coordinates so distances can still be read
      const nw = map.unproject([coords.x * size.x, coords.y * size.y], coords.z);
      g.fillStyle = "#94a3b8"; g.font = "10px system-ui, sans-serif";
      g.fillText(`${nw.lat.toFixed(3)}, ${nw.lng.toFixed(3)}`, 4, 12);
      // labelled places (drawn in every tile they overlap, so labels cross tile edges)
      g.font = "600 12px system-ui, sans-serif";
      (places || []).forEach((p) => {
        const pt = map.project([p.lat, p.lon], coords.z);
        const x = pt.x - coords.x * size.x, y = pt.y - coords.y * size.y;
        if (x < -220 || x > size.x + 20 || y < -20 || y > size.y + 20) return;
        g.fillStyle = "#64748b";
        g.beginPath(); g.arc(x, y, 4, 0, 2 * Math.PI); g.fill();
        g.lineWidth = 3; g.strokeStyle = "#eef3f8";
        g.strokeText(p.name, x + 7, y + 4);
        g.fillStyle = "#334155"; g.fillText(p.name, x + 7, y + 4);
      });
      return tile;
    };
    return layer;
  }

  function notice(map, onRetry) {
    const Ctl = L.Control.extend({
      options: { position: "bottomleft" },
      onAdd() {
        const div = L.DomUtil.create("div", "offline-note");
        div.innerHTML = '<b>Offline map</b> - street tiles not reachable. Places, hospitals and distances are still right. '
          + '<button type="button">Try again</button>';
        L.DomEvent.disableClickPropagation(div);
        div.querySelector("button").addEventListener("click", onRetry);
        return div;
      },
    });
    return new Ctl();
  }

  window.sosBaseLayer = function (map, opts = {}) {
    const state = { mode: null, loaded: 0, errors: 0, timer: null };
    const online = L.tileLayer(opts.url || TILE_URL, { maxZoom: 18, attribution: ATTRIBUTION });
    const offline = offlineLayer(map, opts.places);
    let note = null;

    online.on("tileload", () => { state.loaded++; clearTimeout(state.timer); });
    online.on("tileerror", () => {
      state.errors++;
      if (state.mode === "online" && state.loaded === 0 && state.errors >= ERRORS_BEFORE_OFFLINE) goOffline();
    });

    function goOnline() {
      if (state.mode === "online") return;
      state.mode = "online"; state.loaded = 0; state.errors = 0;
      if (map.hasLayer(offline)) map.removeLayer(offline);
      if (note) { map.removeControl(note); note = null; }
      online.addTo(map);
      online.redraw();
      clearTimeout(state.timer);
      state.timer = setTimeout(() => { if (state.loaded === 0) goOffline(); }, SLOW_MS);
    }
    function goOffline() {
      if (state.mode === "offline") return;
      state.mode = "offline";
      clearTimeout(state.timer);
      if (map.hasLayer(online)) map.removeLayer(online);
      offline.addTo(map);
      note = notice(map, goOnline).addTo(map);
      map.getContainer().classList.add("map-offline");
    }
    // with the tile cache running, keep the street map when offline: seen tiles come from the cache,
    // and if none load the tile errors / timeout above still switch to the drawn map
    const cached = () => Boolean(navigator.serviceWorker && navigator.serviceWorker.controller);
    window.addEventListener("online", goOnline);
    window.addEventListener("offline", () => { if (!cached()) goOffline(); });

    if (navigator.onLine === false && !cached()) goOffline(); else goOnline();
    map.on("layeradd", () => map.getContainer().classList.toggle("map-offline", state.mode === "offline"));
    return { mode: () => state.mode, goOnline, goOffline, layers: { online, offline } };
  };
})();
