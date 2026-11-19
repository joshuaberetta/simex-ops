// SIMEX Mail service worker (scope /inbox/): shows the push notification for new mail and opens
// the conversation when it is tapped. No caching: SIMEX Mail always needs the network.
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (e) => e.waitUntil(self.clients.claim()));

self.addEventListener("push", (e) => {
  let d = {};
  try { d = e.data ? e.data.json() : {}; } catch (err) { d = { body: e.data && e.data.text() }; }
  e.waitUntil((async () => {
    await self.registration.showNotification(d.title || "SIMEX Mail", {
      body: d.body || "New message", tag: d.tag || "simex-mail", renotify: true, data: { url: d.url || "/inbox/" },
      icon: "{{ icon }}", badge: "{{ icon }}",
    });
    // Open windows refresh their count and chime straight away instead of at the next poll.
    for (const c of await self.clients.matchAll({ type: "window" })) c.postMessage({ type: "simex-push" });
    if (self.navigator.setAppBadge) {
      const n = (await self.registration.getNotifications()).length;
      try { await self.navigator.setAppBadge(n); } catch (err) { /* no badge support */ }
    }
  })());
});

self.addEventListener("notificationclick", (e) => {
  e.notification.close();
  const url = new URL((e.notification.data && e.notification.data.url) || "/inbox/", self.location.origin).href;
  e.waitUntil((async () => {
    const wins = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
    for (const c of wins) {
      if (new URL(c.url).origin !== self.location.origin) continue;
      try { await c.focus(); await c.navigate(url); return; } catch (err) { /* not ours to steer: open a new one */ }
    }
    return self.clients.openWindow(url);
  })());
});
