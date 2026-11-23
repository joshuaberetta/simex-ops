// A role player's pages (/inbox/rp/): alerts on this device (Web Push through the SIMEX Mail
// service worker, scope /inbox/), countdowns, and a reload when ExCon changes the schedule.
(function () {
  "use strict";
  const body = document.body;
  const VAPID = body.dataset.vapid;
  const csrf = (document.cookie.match(/(?:^|; )csrftoken=([^;]+)/) || [])[1] ||
    (document.querySelector("input[name=csrfmiddlewaretoken]") || {}).value || "";
  const post = (url, data) => fetch(url, {
    method: "POST", body: new URLSearchParams(data),
    headers: { "X-CSRFToken": decodeURIComponent(csrf), "x-requested-with": "fetch" },
  });
  const ios = /iPad|iPhone|iPod/.test(navigator.userAgent) || (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);
  const standalone = navigator.standalone || matchMedia("(display-mode: standalone)").matches;
  const canNotify = "Notification" in window;
  const permission = () => (canNotify ? Notification.permission : "unsupported");
  const pushable = !!VAPID && "serviceWorker" in navigator && "PushManager" in window;
  const swReady = pushable ? navigator.serviceWorker.register("/inbox/sw.js", { scope: "/inbox/" }).catch(() => null)
    : Promise.resolve(null);
  const b64 = (s) => Uint8Array.from(atob(s.replace(/-/g, "+").replace(/_/g, "/") + "===".slice((s.length + 3) % 4)),
                                     (c) => c.charCodeAt(0));

  async function pushOn() {
    const reg = await swReady;
    if (!reg || permission() !== "granted") return false;
    try {
      const key = b64(VAPID);
      let sub = await reg.pushManager.getSubscription();
      const k = sub && sub.options && sub.options.applicationServerKey;
      if (sub && k && new Uint8Array(k).join() !== key.join()) { await sub.unsubscribe(); sub = null; }
      sub = sub || await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: key });
      const j = sub.toJSON();
      const r = await post("/inbox/rp/push/", { endpoint: j.endpoint, p256dh: j.keys.p256dh, auth: j.keys.auth });
      return r.ok;
    } catch (e) { return false; }
  }
  async function pushOff() {
    const reg = await swReady;
    const sub = reg && await reg.pushManager.getSubscription();
    if (sub) { await post("/inbox/rp/push/", { endpoint: sub.endpoint, off: "1" }); await sub.unsubscribe(); }
  }
  async function turnOn() {
    if (canNotify && Notification.permission === "default") await Notification.requestPermission();
    await pushOn();
    paint();
  }

  const bar = document.querySelector("[data-alert-bar]");
  const installBar = document.querySelector("[data-install-bar]");
  const setup = document.querySelector("[data-setup]");
  const bell = document.querySelector("[data-alerts]");
  function paint() {
    const p = permission();
    if (bell) bell.classList.toggle("off", p !== "granted");
    if (setup) {
      const step = ios && !standalone ? "install" : !pushable || p === "unsupported" ? "nonotify"
        : p === "denied" ? "blocked" : p === "granted" ? "done" : "alerts";
      setup.querySelectorAll("[data-step]").forEach((el) => { el.hidden = el.dataset.step !== step; });
      // The home-screen app starts at the page it was added from: this person's sign-in link.
      if (step === "install" && setup.dataset.appUrl) history.replaceState(null, "", setup.dataset.appUrl);
      return;
    }
    if (installBar) installBar.hidden = !(ios && !standalone && pushable);
    if (bar) bar.hidden = !(pushable && p === "default" && !(ios && !standalone));
  }
  document.querySelectorAll("[data-alerts-on]").forEach((b) => { b.onclick = turnOn; });
  if (bell) bell.onclick = () => (permission() === "granted" ? pushOn().then(() => alert("Alerts are on for this device."))
    : turnOn());
  paint();
  if (pushable) pushOn();  // keeps the server's copy current

  // Signing out stops this device's alerts (a shared phone).
  const out = document.querySelector('form[action="/logout/"]');
  if (out && pushable) out.addEventListener("submit", (e) => {
    if (out.dataset.done) return;
    e.preventDefault();
    out.dataset.done = "1";
    Promise.race([pushOff().catch(() => {}), new Promise((r) => setTimeout(r, 2000))]).then(() => out.submit());
  });

  // Countdowns: "in 12 min", "started 3 min ago".
  function tick() {
    const now = Date.now();
    document.querySelectorAll("[data-countdown]").forEach((el) => {
      const m = Math.round((new Date(el.dataset.countdown).getTime() - now) / 60000);
      el.textContent = m > 90 ? `in ${Math.floor(m / 60)} h ${m % 60} min` : m > 0 ? `in ${m} min`
        : m === 0 ? "now" : `started ${-m} min ago`;
    });
  }
  tick();
  setInterval(tick, 20000);

  // ExCon moved something, or a co-player is in position: reload (unless the page is hidden).
  let v = body.dataset.v;
  async function poll() {
    if (document.hidden) return;
    try {
      const r = await fetch(`/inbox/rp/poll/?v=${encodeURIComponent(v)}`, { headers: { "x-requested-with": "fetch" } });
      const j = await r.json();
      if (j.changed && !setup) location.reload();
      v = j.v;
    } catch (e) { /* offline: next time */ }
  }
  setInterval(poll, 30000);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) poll(); });
  if (pushable) navigator.serviceWorker.addEventListener("message", (e) => { if (e.data && e.data.type === "simex-push") poll(); });
})();
