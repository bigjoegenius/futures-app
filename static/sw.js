// Futures Controller — service worker
const CACHE = "futures-ctrl-v1";
const SHELL = [
    "/",
    "/static/style.css",
    "/static/app.js",
    "/manifest.json",
];

self.addEventListener("install", (e) => {
    e.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL)).catch(() => {}));
    self.skipWaiting();
});

self.addEventListener("activate", (e) => {
    e.waitUntil(
        caches.keys().then(keys =>
            Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k)))
        )
    );
    self.clients.claim();
});

self.addEventListener("fetch", (e) => {
    const url = new URL(e.request.url);
    // API calls: network-first (we want fresh data)
    if (url.pathname.startsWith("/api/")) {
        e.respondWith(fetch(e.request).catch(() => new Response(JSON.stringify({ error: "offline" }),
            { headers: { "Content-Type": "application/json" } })));
        return;
    }
    // Shell: cache-first
    e.respondWith(
        caches.match(e.request).then(cached => cached || fetch(e.request).then(resp => {
            if (resp.ok && e.request.method === "GET") {
                const clone = resp.clone();
                caches.open(CACHE).then(c => c.put(e.request, clone));
            }
            return resp;
        }))
    );
});
