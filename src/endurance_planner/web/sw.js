// Service worker: the app shell is cached at install; GET requests of the plan are
// network-first and fall back to the last copy saved on this device (offline reading).
// Changes (POST) always go to the network.
const VERSION = "v2";
const SHELL_CACHE = `shell-${VERSION}`;
const DATA_CACHE = `data-${VERSION}`;
const SHELL = [
  "/",
  "/index.html",
  "/app.css",
  "/app.js",
  "/api.js",
  "/dom.js",
  "/manifest.json",
  "/icons/icon.svg",
  "/icons/icon-192.png",
  "/icons/icon-512.png",
];
const DATA = /^\/(plan|profile)(\/|$)/;

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(SHELL_CACHE).then((cache) => cache.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) => Promise.all(keys.filter((key) => ![SHELL_CACHE, DATA_CACHE].includes(key)).map((key) => caches.delete(key))))
      .then(() => self.clients.claim()),
  );
});

async function fromCache(request, cacheName) {
  const cached = await caches.match(request, { cacheName });
  if (!cached) return null;
  // Mark offline copies so the app can say it shows saved data.
  const headers = new Headers(cached.headers);
  headers.set("X-Served-From", "cache");
  return new Response(await cached.blob(), { status: cached.status, statusText: cached.statusText, headers });
}

async function networkFirst(request, cacheName) {
  try {
    const response = await fetch(request);
    if (response.ok) {
      const cache = await caches.open(cacheName);
      await cache.put(request, response.clone());
    }
    return response;
  } catch (error) {
    const cached = await fromCache(request, cacheName);
    if (cached) return cached;
    throw error;
  }
}

self.addEventListener("fetch", (event) => {
  const request = event.request;
  const url = new URL(request.url);
  if (request.method !== "GET" || url.origin !== self.location.origin) return;
  if (url.pathname === "/health" || url.pathname === "/openapi.json") return;
  const cacheName = DATA.test(url.pathname) ? DATA_CACHE : SHELL_CACHE;
  const key = cacheName === SHELL_CACHE && url.pathname === "/" ? new Request("/index.html") : request;
  event.respondWith(networkFirst(key, cacheName));
});
