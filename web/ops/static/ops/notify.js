// Shared alerting for SIMEX Mail (participants) and the ExCon pages: a chime, desktop
// notifications, a flashing tab title and a badge on the tab icon. Everything degrades quietly:
// without permission there is no desktop notification, without a click yet there is no sound.
(function () {
  "use strict";
  const KEY = "simex-sound";  // "0" = muted; anything else = on (the default)
  let ctx = null;
  const unlock = () => {
    try {
      ctx = ctx || new (window.AudioContext || window.webkitAudioContext)();
      if (ctx.state === "suspended") ctx.resume();
    } catch (e) { /* no audio */ }
  };
  // Browsers only allow sound after the user has interacted with the page once.
  ["pointerdown", "keydown"].forEach((ev) => document.addEventListener(ev, unlock, { once: true, capture: true }));

  function beep() {
    if (!ctx) return;
    try {
      [0, 0.16, 0.32].forEach((d, i) => {
        const o = ctx.createOscillator(), g = ctx.createGain(), t = ctx.currentTime + d;
        o.frequency.value = [784, 988, 1175][i];
        g.gain.setValueAtTime(0.0001, t);
        g.gain.exponentialRampToValueAtTime(0.25, t + 0.02);
        g.gain.exponentialRampToValueAtTime(0.0001, t + 0.28);
        o.connect(g); g.connect(ctx.destination); o.start(t); o.stop(t + 0.3);
      });
    } catch (e) { /* no audio */ }
  }

  const enabled = () => localStorage.getItem(KEY) !== "0";
  const setEnabled = (on) => localStorage.setItem(KEY, on ? "1" : "0");
  const canDesktop = () => "Notification" in window;
  const permission = () => (canDesktop() ? Notification.permission : "unsupported");

  async function ask() {
    unlock();
    if (canDesktop() && Notification.permission === "default") {
      try { await Notification.requestPermission(); } catch (e) { /* old Safari: callback form */ }
    }
    return permission();
  }

  function desktop(title, body, url, tag) {
    if (!enabled() || permission() !== "granted") return;
    try {
      const n = new Notification(title, { body, tag, requireInteraction: true });
      n.onclick = () => { window.focus(); if (url) location.href = url; n.close(); };
    } catch (e) { /* some mobile browsers only allow notifications from a service worker */ }
  }

  // Tab title: "(3) Inbox …"; while the tab is in the background it alternates with a
  // "New message" line until the user comes back.
  let base = document.title.replace(/^\(\d+\) /, ""), count = 0, flashing = null;
  function title(n) {
    count = n;
    if (!flashing) document.title = (n ? `(${n}) ` : "") + base;
  }
  function flash(text) {
    if (!document.hidden) return;
    clearInterval(flashing);
    let on = false;
    flashing = setInterval(() => { on = !on; document.title = on ? `● ${text}` : (count ? `(${count}) ` : "") + base; }, 1000);
  }
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden && flashing) { clearInterval(flashing); flashing = null; title(count); }
  });

  // Tab icon with a red count badge (drawn, so there is no image file to keep in step).
  function favicon(n, colour) {
    const c = document.createElement("canvas");
    c.width = c.height = 64;
    const g = c.getContext("2d");
    g.fillStyle = colour || "#1f5f99";
    g.beginPath(); g.roundRect ? g.roundRect(4, 12, 56, 40, 8) : g.rect(4, 12, 56, 40); g.fill();
    g.strokeStyle = "#fff"; g.lineWidth = 5; g.lineJoin = "round";
    g.beginPath(); g.moveTo(9, 18); g.lineTo(32, 36); g.lineTo(55, 18); g.stroke();
    if (n) {
      g.fillStyle = "#d93025"; g.beginPath(); g.arc(46, 18, 17, 0, 2 * Math.PI); g.fill();
      g.fillStyle = "#fff"; g.font = "bold 24px system-ui, sans-serif"; g.textAlign = "center"; g.textBaseline = "middle";
      g.fillText(n > 9 ? "9+" : String(n), 46, 19);
    }
    let link = document.querySelector('link[rel="icon"]');
    if (!link) { link = document.createElement("link"); link.rel = "icon"; document.head.appendChild(link); }
    link.href = c.toDataURL("image/png");
  }

  // "New activity": everything at once, for one or more new items.
  function alert({ title: t, body, url, tag, flashText, desktop: showDesktop = true }) {
    if (enabled()) beep();
    if (showDesktop) desktop(t, body, url, tag);
    flash(flashText || t);
  }

  window.simexNotify = { beep, enabled, setEnabled, ask, permission, desktop, title, flash, favicon, alert, unlock };
})();
