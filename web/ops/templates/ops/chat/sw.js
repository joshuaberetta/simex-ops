// SIMEX Messenger's service worker (scope /chat/): its pushes, and the app's own files kept on
// the device (they are versioned by name, so the cache follows each release). Pages and data always
// come from the network: a chat is only as fresh as the server.
const CACHE = "simex-messenger-{{ version }}";
const ASSETS = [{% for a in assets %}"{{ a|escapejs }}"{% if not forloop.last %}, {% endif %}{% endfor %}];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(ASSETS)).catch(() => null).then(() => self.skipWaiting()));
});
self.addEventListener("activate", (e) => e.waitUntil((async () => {
  for (const k of await caches.keys()) if (k.startsWith("simex-messenger-") && k !== CACHE) await caches.delete(k);
  await self.clients.claim();
})()));

self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== "GET" || url.origin !== self.location.origin || !ASSETS.includes(url.pathname)) return;
  // Served from the device at once, refreshed behind (in production the names are hashed, so a
  // cached file is never stale; in development the refresh keeps it current).
  e.respondWith(caches.open(CACHE).then(async (c) => {
    const hit = await c.match(e.request);
    const fresh = fetch(e.request).then((r) => { if (r.ok) c.put(e.request, r.clone()); return r; }).catch(() => hit);
    return hit || fresh;
  }));
});

self.addEventListener("push", (e) => {
  let d = {};
  try { d = e.data ? e.data.json() : {}; } catch (err) { d = { body: e.data && e.data.text() }; }
  e.waitUntil((async () => {
    await self.registration.showNotification(d.title || "SIMEX Messenger", {
      body: d.body || "New message", tag: d.tag || "simex-chat", renotify: true, data: { url: d.url || "/chat/" },
      icon: "{{ icon }}", badge: "{{ icon }}",
    });
    for (const c of await self.clients.matchAll({ type: "window" })) c.postMessage({ type: "simex-push" });
    if (self.navigator.setAppBadge) {
      const n = (await self.registration.getNotifications()).length;
      try { await self.navigator.setAppBadge(n); } catch (err) { /* no badge support */ }
    }
  })());
});

self.addEventListener("notificationclick", (e) => {
  e.notification.close();
  const url = new URL((e.notification.data && e.notification.data.url) || "/chat/", self.location.origin).href;
  e.waitUntil((async () => {
    const wins = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
    for (const c of wins) {
      if (!new URL(c.url).pathname.startsWith("/chat/")) continue;
      try { await c.focus(); await c.navigate(url); return; } catch (err) { /* open a new one */ }
    }
    return self.clients.openWindow(url);
  })());
});
