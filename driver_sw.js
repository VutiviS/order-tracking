// INVENTRA driver app: opens without signal. The page, the logo and the map library are kept on the phone; saving
// always goes to the server (the page itself keeps steps tapped without signal and sends them later).
const CACHE = "inventra-driver-v3";
const TILES = CACHE + "-tiles";
const SHELL = ["/driver", "/logo.svg", "/favicon.svg", "/icon-192.png", "/manifest.json"];

self.addEventListener("install", e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL)).catch(() => {}).then(() => self.skipWaiting()));
});

self.addEventListener("activate", e => {
  e.waitUntil(caches.keys().then(keys => Promise.all(keys.filter(k => k !== CACHE && k !== TILES)
    .map(k => caches.delete(k)))).then(() => self.clients.claim()));
});

async function trim(name, max) {
  const c = await caches.open(name), keys = await c.keys();
  for (let i = 0; i < keys.length - max; i++) await c.delete(keys[i]);
}

self.addEventListener("fetch", e => {
  const r = e.request, u = new URL(r.url);
  if (r.method !== "GET") return;
  if (u.origin === location.origin && u.pathname.startsWith("/api/")) return;
  if (r.mode === "navigate") {                     // the page: the newest when online, the kept copy when not
    e.respondWith(fetch(r).then(res => {
      if (res.ok) { const copy = res.clone(); caches.open(CACHE).then(c => c.put("/driver", copy)); }
      return res;
    }).catch(() => caches.match("/driver")));
    return;
  }
  const tile = /tile\.openstreetmap\.org|arcgisonline\.com/.test(u.host);
  const kept = tile || u.origin === location.origin || /unpkg\.com|cdn\.jsdelivr\.net|fonts\.(googleapis|gstatic)\.com/.test(u.host);
  if (!kept) return;
  e.respondWith(caches.match(r).then(hit => hit || fetch(r).then(res => {
    if (res.ok || res.type === "opaque") {
      const copy = res.clone();
      caches.open(tile ? TILES : CACHE).then(c => c.put(r, copy)).then(() => tile && trim(TILES, 600));
    }
    return res;
  })));
});
