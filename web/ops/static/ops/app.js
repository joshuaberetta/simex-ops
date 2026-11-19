// Small progressive enhancements; every page works without them.
document.addEventListener("click", (e) => {
  const el = e.target.closest("[data-confirm]");
  if (el && !confirm(el.dataset.confirm)) e.preventDefault();
});

document.querySelectorAll("[data-select-all]").forEach((box) => {
  box.addEventListener("change", () => {
    box.closest("form").querySelectorAll('input[name="ids"]').forEach((c) => { c.checked = box.checked; });
  });
});

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

// ExCon alerts without reloading: new replies, AI flags that need attention, failed and overdue
// injects. A toast on the page, a chime, a desktop notification and the tab title / icon.
if (window.simexNotify && document.getElementById("toasts")) {
  const N = window.simexNotify;
  const box = document.getElementById("toasts");
  const btn = document.querySelector("[data-alerts]");
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
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

  function toast(html, cls, url) {
    const el = document.createElement("div");
    el.className = "toast " + (cls || "");
    el.innerHTML = html + '<button type="button" class="x" aria-label="Dismiss">✕</button>';
    el.querySelector(".x").onclick = (e) => { e.stopPropagation(); el.remove(); };
    if (url) { el.style.cursor = "pointer"; el.onclick = () => { location.href = url; }; }
    box.prepend(el);
    while (box.children.length > 5) box.lastChild.remove();
    setTimeout(() => el.classList.add("old"), 60000);
  }

  // Seen markers are shared by every ExCon tab (localStorage), so moving between pages
  // neither repeats nor misses an alert.
  const get = (k) => Number(localStorage.getItem(k)) || 0;
  let last = null;
  async function poll() {
    try {
      // First visit: no markers yet, so take the current state as seen.
      const first = localStorage.getItem("excon-since") === null || localStorage.getItem("excon-ai") === null;
      const r = await fetch(first ? "/excon/poll/" : `/excon/poll/?since=${get("excon-since")}&ai=${get("excon-ai")}`,
                            { credentials: "same-origin" });
      if (!r.ok) return;
      const j = await r.json();
      if (first || j.max < get("excon-since") || j.ai_max < get("excon-ai")) {  // first visit, or the data was restored
        localStorage.setItem("excon-since", j.max); localStorage.setItem("excon-ai", j.ai_max);
      }
      const fresh = j.items.filter((x) => x.id > get("excon-since"));
      const flags = j.flags.filter((x) => x.id > get("excon-ai"));
      if (fresh.length) localStorage.setItem("excon-since", Math.max(...fresh.map((x) => x.id)));
      if (j.max > get("excon-since") && !fresh.length) localStorage.setItem("excon-since", j.max);
      if (flags.length) localStorage.setItem("excon-ai", Math.max(...flags.map((x) => x.id)));
      if (j.ai_max > get("excon-ai")) localStorage.setItem("excon-ai", j.ai_max);
      for (const x of fresh) {
        toast(`<b>${esc(x.team)}${x.kind === "test" ? " (test)" : ""}</b> → ${esc(x.persona)}<div>${esc(x.subject)}</div><div class="muted">${esc(x.preview)}</div>`,
              "reply", `/replies/${x.id}/`);
      }
      for (const f of flags) {
        toast(`<b>⚑ ${esc(f.team)}: ${f.flags.includes("safety") ? "safety concern" : "needs ExCon attention"}</b><div>${esc(f.summary)}</div>`,
              "flag", `/replies/${f.response}/`);
      }
      if (fresh.length || flags.length) {
        const x = fresh[0], f = flags[0];
        N.alert(f ? { title: `⚑ ${f.team}: needs attention`, body: f.summary, url: `${location.origin}/replies/${f.response}/`, tag: `flag-${f.id}` }
                  : { title: `${x.team} → ${x.persona}${fresh.length > 1 ? ` (+${fresh.length - 1})` : ""}`, body: x.subject,
                      url: `${location.origin}/replies/${x.id}/`, tag: `reply-${x.id}`, flashText: `New reply from ${x.team}` });
      }
      if (last && (j.failed > last.failed || j.overdue > last.overdue)) {
        const what = j.failed > last.failed ? "An inject failed to deliver" : "An inject is overdue";
        toast(`<b>${what}</b><div>Check the Injects tab.</div>`, "flag", `/injects/?status=${j.failed > last.failed ? "failed" : "overdue"}`);
        N.alert({ title: what, body: "Check the Injects tab.", url: `${location.origin}/injects/`, tag: `inj-${j.failed}-${j.overdue}` });
      }
      last = j;
      // the Replies tab badge and the tab title / icon
      const tab = document.querySelector('.tabs a[href="/replies/"]');
      if (tab) {
        let b = tab.querySelector(".badge");
        if (j.new && !b) { b = document.createElement("span"); b.className = "badge"; tab.append(" ", b); }
        if (b) { b.textContent = j.new; b.hidden = !j.new; }
      }
      N.title(j.new);
      N.favicon(j.new, "#17202c");
    } catch (e) { /* offline: next time */ }
  }
  poll();
  setInterval(poll, 10000);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) poll(); });
}
