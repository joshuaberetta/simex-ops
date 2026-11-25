// Notes beside a conversation (ops/mail_notes.py, templates/ops/mail/_notes.html): each email's
// group (SIMEX AI's assessment, then people's notes) is placed level with the email, like
// comments in the margin of a document; on a narrow window the panel is a drawer instead.
(function () {
  "use strict";
  const side = document.querySelector("[data-nt-side]");
  if (!side) return;
  const $ = (s, el) => (el || document).querySelector(s);
  const $$ = (s, el) => Array.from((el || document).querySelectorAll(s));
  const wrap = $("[data-nt-wrap]");
  const thread = $(".gm-thread");
  const URL_ = side.dataset.url;
  const PREF = "simex-notes";
  const csrf = () => (document.cookie.match(/(?:^|;\s*)csrftoken=([^;]+)/) || [])[1] || "";

  const narrow = () => wrap.clientWidth < 1060;
  const pref = () => { try { return localStorage.getItem(PREF); } catch (e) { return null; } };
  const isOpen = () => document.body.classList.contains("nt-open");
  function setOpen(on, remember) {
    document.body.classList.toggle("nt-open", on);
    $$("[data-nt-toggle]").forEach((b) => b.setAttribute("aria-pressed", on ? "true" : "false"));
    if (remember) { try { localStorage.setItem(PREF, on ? "on" : "off"); } catch (e) { /* private mode */ } }
    layout();
  }

  // --- placing the groups beside their emails ---------------------------------------------------
  function layout() {
    const over = narrow();
    side.classList.toggle("nt-overlay", over);
    const groups = $$(".nt-group", side).filter((g) => !g.classList.contains("nt-empty") || g.classList.contains("nt-active"));
    $$(".nt-group", side).forEach((g) => { g.style.top = ""; });
    if (over || !isOpen()) { side.style.minHeight = ""; return; }
    const base = side.getBoundingClientRect().top;
    const top = $("[data-nt-top]", side);
    let y = top ? top.offsetHeight + 12 : 0;
    groups.forEach((g) => {
      const art = document.getElementById("msg-" + g.dataset.ntFor);
      const want = art ? art.getBoundingClientRect().top - base : y;
      const at = Math.max(want, y);
      g.style.top = at + "px";
      y = at + g.offsetHeight + 10;
    });
    side.style.minHeight = y + "px";
  }
  let raf = 0;
  const relayout = () => { cancelAnimationFrame(raf); raf = requestAnimationFrame(layout); };
  if (window.ResizeObserver) { new ResizeObserver(relayout).observe(thread); new ResizeObserver(relayout).observe(wrap); }
  window.addEventListener("resize", relayout);

  // --- talking to the server ----------------------------------------------------------------------
  function badge(n) {
    $$("[data-nt-badge]").forEach((b) => { b.textContent = n ? String(n) : ""; });
  }
  function replace(html) {
    // keep what is being typed in an open form through a refresh
    const typing = {};
    $$(".nt-group", side).forEach((g) => {
      const f = $("[data-nt-form]", g);
      if (f && !f.hidden) typing[g.dataset.ntFor] = f.elements.body.value;
    });
    side.innerHTML = html;
    Object.entries(typing).forEach(([id, text]) => {
      const g = $(`.nt-group[data-nt-for="${id}"]`, side);
      if (g) openForm(g, text, false);
    });
    const inner = $("[data-nt-inner]", side);
    badge(inner ? Number(inner.dataset.n || 0) : 0);
    markButtons();
    relayout();
    poll();
  }
  async function send(data) {
    const body = new FormData();
    Object.entries(data).forEach(([k, v]) => body.append(k, v));
    const r = await fetch(URL_, { method: "POST", body, credentials: "same-origin",
      headers: { "X-CSRFToken": csrf(), "X-Requested-With": "fetch" } });
    let j = {};
    try { j = await r.json(); } catch (e) { j = { error: `Not saved (${r.status}).` }; }
    if (j.html) replace(j.html);
    if (j.error) alert(j.error);
    return r.ok;
  }
  async function refresh() {
    const r = await fetch(URL_, { credentials: "same-origin", headers: { "X-Requested-With": "fetch" } });
    if (r.ok) replace((await r.json()).html);
  }
  let polls = 0, timer = 0;
  function poll() {
    clearTimeout(timer);
    const inner = $("[data-nt-inner]", side);
    if (!inner || !inner.hasAttribute("data-pending") || polls > 60) return;
    timer = setTimeout(async () => {
      polls += 1;
      try { await refresh(); } catch (e) { poll(); }
    }, 5000);
  }
  // each email's button in the conversation shows how many notes it has
  function markButtons() {
    $$("[data-nt-add]").forEach((b) => {
      const g = $(`.nt-group[data-nt-for="${b.dataset.ntAdd}"]`, side);
      const n = g ? $$(".nt-note, .nt-ai:not(.nt-muted)", g).length : 0;
      b.classList.toggle("has", n > 0);
      const s = $("span", b);
      if (n && s) s.textContent = n;
      else if (n) b.insertAdjacentHTML("beforeend", `<span>${n}</span>`);
      else if (s) s.remove();
    });
  }

  // --- the panel's controls ---------------------------------------------------------------------------
  function openForm(g, text, focus) {
    g.classList.add("nt-active");
    const f = $("[data-nt-form]", g);
    f.hidden = false;
    $("[data-nt-open]", g).hidden = true;
    if (text) f.elements.body.value = text;
    relayout();
    if (focus !== false) f.elements.body.focus();
  }
  function closeForm(g) {
    const f = $("[data-nt-form]", g);
    f.hidden = true; f.elements.body.value = "";
    $("[data-nt-open]", g).hidden = false;
    g.classList.remove("nt-active");
    relayout();
  }
  function showMail(id) {
    const art = document.getElementById("msg-" + id);
    if (!art) return;
    if (art.classList.contains("closed")) { art.classList.remove("closed"); art.classList.add("open"); }
    art.scrollIntoView({ behavior: "smooth", block: "start" });
    art.classList.add("nt-flash");
    setTimeout(() => art.classList.remove("nt-flash"), 1600);
  }
  function focusGroup(id) {
    if (!isOpen()) setOpen(true, !narrow());
    const g = $(`.nt-group[data-nt-for="${id}"]`, side);
    if (!g) return;
    if (!$(".nt-note, .nt-ai", g)) openForm(g);
    else g.classList.add("nt-active");
    relayout();
    setTimeout(() => {
      g.scrollIntoView({ behavior: "smooth", block: "nearest" });
      g.classList.add("nt-flash");
      setTimeout(() => g.classList.remove("nt-flash"), 1600);
    }, 50);
  }

  document.addEventListener("click", (e) => {
    const t = e.target.closest("[data-nt-toggle], [data-nt-add], [data-nt-open], [data-nt-cancel], [data-nt-goto], [data-nt-edit], [data-nt-del], [data-nt-review], [data-nt-save], [data-nt-unedit]");
    if (!t) return;
    const g = t.closest(".nt-group");
    if (t.matches("[data-nt-toggle]")) { setOpen(!isOpen(), true); return; }
    e.preventDefault(); e.stopPropagation();
    if (t.matches("[data-nt-add]")) { const id = t.dataset.ntAdd; showMail(id); focusGroup(id); }
    else if (t.matches("[data-nt-open]")) openForm(g);
    else if (t.matches("[data-nt-cancel]")) closeForm(g);
    else if (t.matches("[data-nt-goto]")) showMail(t.dataset.ntGoto);
    else if (t.matches("[data-nt-review]")) { t.disabled = true; polls = 0; send({ act: "review" }); }
    else if (t.matches("[data-nt-del]")) {
      const n = t.closest("[data-nt-note]");
      if (confirm("Delete this note?")) send({ act: "delete", note: n.dataset.ntNote });
    } else if (t.matches("[data-nt-edit]")) {
      const n = t.closest("[data-nt-note]");
      const raw = $("[data-nt-raw]", n).value;
      $("[data-nt-body]", n).innerHTML = `<textarea rows="4" maxlength="4000"></textarea><div class="nt-add-acts">` +
        `<button type="button" class="nt-btn" data-nt-unedit>Cancel</button><button type="button" class="nt-btn primary" data-nt-save>Save</button></div>`;
      const ta = $("textarea", n);
      ta.value = raw; ta.focus();
      relayout();
    } else if (t.matches("[data-nt-save]")) {
      const n = t.closest("[data-nt-note]");
      send({ act: "edit", note: n.dataset.ntNote, body: $("[data-nt-body] textarea", n).value });
    } else if (t.matches("[data-nt-unedit]")) {
      refresh();
    }
  }, true);
  side.addEventListener("submit", async (e) => {
    const f = e.target.closest("[data-nt-form]");
    if (!f) return;
    e.preventDefault();
    const body = f.elements.body.value.trim();
    if (!body) { f.elements.body.focus(); return; }
    const g = f.closest(".nt-group");
    f.elements.body.value = "";  // sent: not kept as typing through the refresh
    f.hidden = true;
    const ok = await send({ act: "add", mail: g.dataset.ntFor, body });
    if (!ok) { const g2 = $(`.nt-group[data-nt-for="${g.dataset.ntFor}"]`, side); if (g2) openForm(g2, body); }
  });
  side.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && (e.metaKey || e.ctrlKey) && e.target.matches("textarea")) {
      e.preventDefault();
      const save = e.target.closest("[data-nt-note]") && $("[data-nt-save]", e.target.closest("[data-nt-note]"));
      if (save) save.click(); else if (e.target.form) e.target.form.requestSubmit();
    }
  });
  // hovering a group lights up its email
  side.addEventListener("mouseover", (e) => {
    const g = e.target.closest(".nt-group");
    $$(".gm-msg.nt-hl").forEach((a) => a.classList.remove("nt-hl"));
    if (g) { const a = document.getElementById("msg-" + g.dataset.ntFor); if (a) a.classList.add("nt-hl"); }
  });
  side.addEventListener("mouseleave", () => $$(".gm-msg.nt-hl").forEach((a) => a.classList.remove("nt-hl")));
  // opening or closing an email moves the ones below it
  thread.addEventListener("click", () => setTimeout(relayout, 0));

  const p = pref();
  setOpen(p ? p === "on" : !narrow(), false);
  markButtons();
  poll();
  // a link from a notes list: #msg-<id> opens that email and its notes
  const m = location.hash.match(/^#msg-(\d+)$/);
  if (m) setTimeout(() => { showMail(m[1]); focusGroup(m[1]); }, 100);
})();
