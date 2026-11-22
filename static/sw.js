// EmergencySOS service worker (Tier 3): keeps the OpenStreetMap tiles you have
// looked at, so the map still works when the internet drops. Only map tiles are
// cached; the app's own pages and API always go to the server (they are on the
// same laptop / LAN as the browser, so they do not need the internet).
//
// Strategy for a tile: try the network (max 4 s); on success save a copy; if the
// network fails or is too slow, answer from the cache. At most MAX_TILES are kept.
"use strict";

const CACHE = "sos-map-tiles-v1";
const MAX_TILES = 3000;
const NETWORK_TIMEOUT_MS = 4000;
const isTile = (url) => /^https:\/\/[abc]\.tile\.openstreetmap\.org\/\d+\/\d+\/\d+\.png$/.test(url);

self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (e) => e.waitUntil(self.clients.claim()));

async function trim(cache) {
  const keys = await cache.keys();
  for (let i = 0; i < keys.length - MAX_TILES; i++) await cache.delete(keys[i]);     // oldest first
}

// a, b and c serve the same tiles: store them under one key
const keyFor = (url) => url.replace(/^https:\/\/[abc]\./, "https://a.");

async function tile(request) {
  const cache = await caches.open(CACHE);
  const key = keyFor(request.url);
  try {
    const response = await Promise.race([
      fetch(request),
      new Promise((_, reject) => setTimeout(() => reject(new Error("slow")), NETWORK_TIMEOUT_MS)),
    ]);
    if (response && (response.ok || response.type === "opaque")) {
      cache.put(key, response.clone()).then(() => trim(cache)).catch(() => {});
    }
    return response;
  } catch (err) {
    const hit = await cache.match(key);
    if (hit) return hit;
    throw err;                         // the page then shows its drawn offline map
  }
}

self.addEventListener("fetch", (e) => {
  if (e.request.method === "GET" && isTile(e.request.url)) e.respondWith(tile(e.request));
});
