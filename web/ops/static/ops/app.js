// Small progressive enhancements; every page works without them.
document.addEventListener("click", (e) => {
  const el = e.target.closest("[data-confirm]");
  if (el && !confirm(el.dataset.confirm)) e.preventDefault();
});

document.querySelectorAll("[data-select-all]").forEach((box) => {
  box.addEventListener("change", () => {
    // Only rows you can see: a collapsed day stays out of a bulk action.
    box.closest("form").querySelectorAll('input[name="ids"]').forEach((c) => {
      const day = c.closest("details");
      c.checked = box.checked && (!day || day.open);
    });
  });
});

// Injects: collapsible days, remembered in this browser.
const dayGroups = document.querySelectorAll("details.day[data-day]");
if (dayGroups.length) {
  const KEY = dayGroups[0].dataset.store || "simex.injects.closed";  // People: the teams folded
  const closed = new Set(JSON.parse(localStorage.getItem(KEY) || "[]"));
  const save = () => localStorage.setItem(KEY, JSON.stringify([...closed]));
  dayGroups.forEach((d) => {
    if (dayGroups.length > 1 && closed.has(d.dataset.day)) d.open = false;
    d.addEventListener("toggle", () => {
      if (d.open) closed.delete(d.dataset.day); else closed.add(d.dataset.day);
      save();
    });
  });
  document.querySelectorAll("[data-days]").forEach((b) => b.addEventListener("click", () => {
    dayGroups.forEach((d) => { d.open = b.dataset.days === "open"; });
  }));
}

const check = document.querySelector("[data-domain-check]");
if (check) {
  check.addEventListener("click", async () => {
    const out = document.querySelector("pre.domain");
    out.hidden = false;
    out.textContent = "Checking…";
    try {
      const r = await fetch("/settings/domain/");
      const d = await r.json();
      if (d.error) { out.textContent = d.error; return; }
      const lines = [`${d.name}: ${d.status}`];
      (d.records || []).forEach((x) => lines.push(`${x.status.padEnd(9)} ${x.type.padEnd(5)} ${x.name}  ${x.value}`));
      out.textContent = lines.join("\n");
    } catch (err) { out.textContent = String(err); }
  });
}

// Live clock in the exercise timezone.
const clock = document.querySelector(".clock");
if (clock) {
  const f = new Intl.DateTimeFormat("en-GB", { timeZone: clock.dataset.tz, weekday: "short", day: "2-digit",
    month: "short", hour: "2-digit", minute: "2-digit", second: "2-digit" });
  const tick = () => { clock.textContent = f.format(new Date()); };
  tick();
  setInterval(tick, 1000);
}

// The simulation clock (Settings → Simulation clock): ticks from the time the page was given at
// its speed; ExCon's poll brings it back in line (window.exconSim), e.g. after following an inject.
const simBox = document.querySelector("[data-excon-sim-box]");
if (simBox) {
  const cfgEl = document.getElementById("excon-sim");
  let sim = cfgEl ? JSON.parse(cfgEl.textContent) : null, t0 = Date.now();
  const q = (s) => simBox.querySelector(s);
  const tick = () => {
    if (!sim) return;
    const at = new Date(sim.sim + (Date.now() - t0) * sim.rate);
    const part = (o) => new Intl.DateTimeFormat("en-GB", { timeZone: sim.tz, ...o }).format(at).replace(/,/g, "");
    q("[data-xsim-mon]").textContent = part({ month: "short" });
    q("[data-xsim-day]").textContent = part({ day: "numeric" });
    q("[data-xsim-time]").textContent = part({ hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23" });
    q("[data-xsim-date]").textContent = part({ weekday: "short", day: "numeric", month: "short", year: "numeric" });
    q("[data-xsim-rate]").textContent = sim.rate === 1 ? "" : `×${sim.rate}`;
  };
  window.exconSim = (c) => {
    simBox.hidden = !c;
    const off = document.querySelector("[data-excon-sim-off]");
    if (off) off.hidden = !!c;
    if (c) { sim = c; t0 = Date.now(); tick(); }
  };
  tick();
  setInterval(tick, 1000);
}

// Grade page: copy the AI's suggested points into the radios (the grader still saves).
const fill = document.querySelector("[data-ai-fill]");
if (fill) {
  fill.addEventListener("click", () => {
    document.querySelectorAll("[data-ai-score]").forEach((tag) => {
      const radio = document.querySelector(`input[name="${tag.dataset.aiScore}"][value="${tag.dataset.value}"]`);
      if (radio) radio.checked = true;
    });
  });
}

// ExCon's Notifications panel, kept up to date without reloading: new replies, AI flags that
// need attention, failed and overdue injects, finished background jobs. Read and dismissed on
// the server, per ExCon user. Something new also gets a short toast (panel closed), a chime,
// a desktop notification and the tab title / icon.
const panel = document.getElementById("notices");
if (window.simexNotify && panel) {
  const N = window.simexNotify;
  const box = document.getElementById("toasts");
  const btn = document.querySelector("[data-alerts]");
  const toggle = document.querySelector("[data-nt-toggle]");
  const list = panel.querySelector("[data-nt-list]"), empty = panel.querySelector("[data-nt-empty]");
  const pin = panel.querySelector("[data-nt-pin]");
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const csrf = () => (document.querySelector('[name="csrfmiddlewaretoken"]') || {}).value || "";
  const post = (url, data) => fetch(url, { method: "POST", body: new URLSearchParams(data || {}), credentials: "same-origin",
                                           headers: { "X-CSRFToken": csrf() } });
  const paint = () => {
    if (!btn) return;
    const on = N.enabled(), denied = N.permission() === "denied";
    btn.textContent = on ? "🔔 Alerts on" : "🔕 Alerts off";
    btn.classList.toggle("primary", on && N.permission() === "granted");
    btn.title = !on ? "Click to turn on sound and desktop notifications for new replies"
      : denied ? "Sound on; desktop notifications are blocked in this browser's site settings"
      : N.permission() === "granted" ? "Sound and desktop notifications on (click to mute)" : "Sound on; click to allow desktop notifications";
  };
  if (btn) btn.onclick = async () => {
    if (N.enabled() && N.permission() === "default") { await N.ask(); N.beep(); }
    else { N.setEnabled(!N.enabled()); if (N.enabled()) { await N.ask(); N.beep(); } }
    paint();
  };
  paint();

  // --- the panel ---
  const ICON = { reply: "✉", flag: "⚑", delivery: "⚠", system: "⚙" };
  let items = [], version = "", kind = localStorage.getItem("excon-notices-kind") || "";
  const pinned = () => document.body.classList.contains("nt-pinned");
  const isOpen = () => !panel.hidden;
  function show(on) {
    panel.hidden = !on;
    toggle.setAttribute("aria-expanded", String(on));
    toggle.classList.toggle("on", on);
    if (on) box.replaceChildren();
  }
  function setPinned(on) {
    document.body.classList.toggle("nt-pinned", on);
    localStorage.setItem("excon-notices-pinned", on ? "1" : "0");
    pin.setAttribute("aria-pressed", String(on));
    pin.title = on ? "Unpin (close it when you click elsewhere)" : "Keep open beside the page";
    if (on) show(true);
  }
  const ago = (iso) => {
    const s = Math.max(0, (Date.now() - new Date(iso)) / 1000);
    if (s < 60) return "just now";
    if (s < 3600) return `${Math.floor(s / 60)} min ago`;
    if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
    return new Date(iso).toLocaleDateString("en-GB", { day: "2-digit", month: "short" });
  };
  function render() {
    const shown = items.filter((n) => !kind || n.kind === kind);
    panel.querySelectorAll("[data-nt-kind]").forEach((b) => b.classList.toggle("on", b.dataset.ntKind === kind));
    list.innerHTML = shown.map((n) => `
      <li class="nt-item ${n.kind}${n.read ? "" : " unread"}" data-id="${n.id}">
        <a href="${esc(n.url || "#")}" title="${new Date(n.at).toLocaleString("en-GB")}">
          <span class="nt-ic" aria-hidden="true">${ICON[n.kind] || "•"}</span>
          <span class="nt-text"><span class="nt-title">${esc(n.title)}</span>
            ${n.body ? `<span class="nt-body">${esc(n.body)}</span>` : ""}
            ${n.note ? `<span class="nt-note">${esc(n.note)}</span>` : ""}
            <span class="nt-time">${ago(n.at)}</span></span>
        </a>
        <button type="button" class="x" data-nt-dismiss="${n.id}" aria-label="Dismiss" title="Dismiss">✕</button>
      </li>`).join("");
    empty.hidden = shown.length > 0;
  }
  function count(n) {
    const b = toggle.querySelector("[data-nt-count]");
    b.textContent = n > 99 ? "99+" : n;
    b.hidden = !n;
  }

  toggle.onclick = (e) => { e.stopPropagation(); show(!isOpen()); };
  pin.onclick = () => setPinned(!pinned());
  panel.querySelector("[data-nt-close]").onclick = () => { if (pinned()) setPinned(false); show(false); };
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && isOpen() && !pinned()) show(false); });
  document.addEventListener("click", (e) => {
    // (a dismissed row is already gone from the page when this runs)
    if (isOpen() && !pinned() && e.target.isConnected && !panel.contains(e.target) && !e.target.closest("#toasts")) show(false);
  });
  panel.querySelectorAll("[data-nt-kind]").forEach((b) => b.onclick = () => {
    kind = b.dataset.ntKind;
    localStorage.setItem("excon-notices-kind", kind);
    render();
  });
  list.addEventListener("click", async (e) => {
    const x = e.target.closest("[data-nt-dismiss]");
    if (x) {
      e.preventDefault();
      items = items.filter((n) => String(n.id) !== x.dataset.ntDismiss);
      render();
      await post(`/excon/notices/${x.dataset.ntDismiss}/dismiss/`);
      poll();
      return;
    }
    const a = e.target.closest("a");
    if (a && a.getAttribute("href") === "#") e.preventDefault();
  });
  const all = (url, label) => async () => {
    const what = kind ? panel.querySelector(`[data-nt-kind="${kind}"]`).textContent.toLowerCase() : "notifications";
    if (label && !confirm(`${label} ${what}?`)) return;
    await post(url, { kind });
    poll();
  };
  panel.querySelector("[data-nt-readall]").onclick = all("/excon/notices/read-all/");
  panel.querySelector("[data-nt-clear]").onclick = all("/excon/notices/clear/", "Clear all");
  if (pinned()) setPinned(true);
  setInterval(render, 30000);  // "3 min ago"

  function toast(n) {
    if (isOpen()) return;
    const el = document.createElement(n.url ? "a" : "div");
    el.className = "toast " + n.kind;
    if (n.url) el.href = n.url;
    el.innerHTML = `<b>${esc(n.title)}</b>${n.body ? `<div>${esc(n.body)}</div>` : ""}${n.note ? `<div class="muted">${esc(n.note)}</div>` : ""}`
      + '<button type="button" class="x" aria-label="Close">✕</button>';
    el.querySelector(".x").onclick = (e) => { e.preventDefault(); e.stopPropagation(); el.remove(); };
    box.prepend(el);
    while (box.children.length > 3) box.lastChild.remove();
    setTimeout(() => el.remove(), 6000);
  }

  // Which notices this browser has already alerted about, shared by every ExCon tab
  // (localStorage), so moving between pages neither repeats nor misses an alert.
  const SEEN = "excon-notice-seen";
  function alertNew() {
    const max = Math.max(0, ...items.map((n) => n.id));
    const seen = localStorage.getItem(SEEN);
    if (seen === null || max < Number(seen)) { localStorage.setItem(SEEN, max); return; }  // first visit, or data restored
    const fresh = items.filter((n) => n.id > Number(seen) && !n.read);
    if (max > Number(seen)) localStorage.setItem(SEEN, max);
    if (!fresh.length) return;
    fresh.slice(0, 3).reverse().forEach(toast);
    const loud = fresh.filter((n) => n.kind !== "system" || n.title.startsWith("The scheduler"));
    const first = ["flag", "delivery", "reply", "system"].map((k) => loud.find((n) => n.kind === k)).find(Boolean);
    if (first) N.alert({ title: first.title + (loud.length > 1 ? ` (+${loud.length - 1})` : ""), body: first.body,
                         url: first.url ? location.origin + first.url : "", tag: `notice-${first.id}`, flashText: first.title });
  }

  async function poll() {
    try {
      const r = await fetch(`/excon/poll/?v=${encodeURIComponent(version)}`, { credentials: "same-origin" });
      if (!r.ok) return;
      const j = await r.json();
      if (j.notices) { items = j.notices; version = j.v; render(); alertNew(); }
      count(j.unread);
      if ("clock" in j && window.exconSim) window.exconSim(j.clock);
      // the Replies tab badge and the tab title / icon
      const tab = document.querySelector('.tabs a[href="/replies/"]');
      if (tab) {
        let b = tab.querySelector(".badge");
        if (j.new && !b) { b = document.createElement("span"); b.className = "badge"; tab.append(" ", b); }
        if (b) { b.textContent = j.new; b.hidden = !j.new; }
      }
      N.title(j.unread);
      N.favicon(j.unread, "#17202c");
    } catch (e) { /* offline: next time */ }
  }
  poll();
  setInterval(poll, 10000);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) poll(); });
}
