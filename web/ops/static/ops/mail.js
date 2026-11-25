// SIMEX Mail: compose window, recipient chips, drafts, list/thread actions, shortcuts, polling.
(function () {
  "use strict";
  const CFG = JSON.parse(document.getElementById("simex-config").textContent);
  const BASE = CFG.base;
  const $ = (s, el = document) => el.querySelector(s);
  const $$ = (s, el = document) => Array.from(el.querySelectorAll(s));
  // The cookie first: signing in again (in another tab) rotates the token and the page's copy goes stale.
  const csrf = () => (document.cookie.match(/(?:^|;\s*)csrftoken=([^;]+)/) || [])[1] || ($('[name="csrfmiddlewaretoken"]') || {}).value || "";
  // A fetch that ended on the sign-in page (the session ended with the page open).
  const signedOut = (r) => r && (r.redirected && /\/login\//.test(r.url) || r.status === 401);
  const signInAgain = () => snack("You are signed out. Sign in again (in a new tab), then come back: what you wrote is still here.",
    { label: "Sign in", run: () => window.open("/login/", "_blank") }, true);
  const BOOK = CFG.book || [];
  const byKey = Object.fromEntries(BOOK.map((b) => [b.key, b]));
  const COLOURS = ["#1a73e8", "#d93025", "#188038", "#e37400", "#9334e6", "#007b83", "#c5221f", "#5f6368", "#b06000", "#1967d2", "#a142f4", "#12b5cb"];
  const colour = (s) => { let h = 0; for (const c of s || "?") h = (h * 31 + c.charCodeAt(0)) >>> 0; return COLOURS[h % COLOURS.length]; };
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  function post(url, data, signal) {
    const body = data instanceof FormData ? data : new URLSearchParams(data);
    return fetch(url, { method: "POST", body, signal, headers: { "X-CSRFToken": csrf(), "X-Requested-With": "fetch" }, credentials: "same-origin" });
  }

  // A compose session's key (see Composer.save).
  function newKey() {
    if (window.crypto && crypto.randomUUID) return crypto.randomUUID();
    return Array.from({ length: 4 }, () => Math.random().toString(36).slice(2, 10)).join("-");
  }

  function snack(text, action, loud) {
    const el = $("#snackbar");
    el.innerHTML = esc(text) + (action ? ` <button type="button">${esc(action.label)}</button>` : "") +
      (loud ? ' <button type="button" data-close aria-label="Dismiss">✕</button>' : "");
    el.classList.toggle("loud", !!loud);
    el.hidden = false;
    if (action) $("button", el).onclick = action.run;
    if (loud) $("[data-close]", el).onclick = () => { el.hidden = true; };
    clearTimeout(snack.t);
    snack.t = setTimeout(() => { el.hidden = true; }, loud ? 30000 : 6000);
  }

  // --- sidebar, account card, menus --------------------------------------------------------------
  const side = $("[data-toggle-side]");
  if (side) side.onclick = () => {
    if (window.matchMedia("(max-width: 760px)").matches) document.body.classList.toggle("side-open");
    else document.body.classList.toggle("side-collapsed");
  };
  document.addEventListener("click", (e) => {
    const acc = e.target.closest("[data-account]");
    const card = $(".gm-account-card");
    if (card) { if (acc) card.hidden = !card.hidden; else if (!e.target.closest(".gm-account-card")) card.hidden = true; }
    const menuBtn = e.target.closest("[data-menu]");
    $$(".gm-menu").forEach((m) => {
      if (m.contains(e.target) && e.target.closest("input, label, .gm-lmenu-title")) return;  // typing / ticking in a label menu
      if (!menuBtn || m.previousElementSibling !== menuBtn) m.hidden = true;
    });
    if (menuBtn) menuBtn.nextElementSibling.hidden = !menuBtn.nextElementSibling.hidden;
    $$("[data-fmore][open]").forEach((d) => { if (!d.contains(e.target)) d.open = false; });  // a list's More filters panel
    const det = e.target.closest("[data-details]");
    if (!e.target.closest(".gm-details")) $$(".gm-details").forEach((d) => { if (!det || d.previousElementSibling !== det) d.hidden = true; });
    if (det) { e.stopPropagation(); det.nextElementSibling.hidden = !det.nextElementSibling.hidden; }
    if (document.body.classList.contains("side-open") && !e.target.closest(".gm-side, [data-toggle-side]")) document.body.classList.remove("side-open");
  });

  // --- composer (floating window and inline reply share it) ---------------------------------------
  class Composer {
    constructor(form, opts = {}) {
      this.form = form;
      this.opts = opts;
      this.keys = { to: [], cc: [], bcc: [] };
      this.files = [];
      this.dirty = false;
      this.saving = null;
      for (const field of ["to", "cc", "bcc"]) this.bindChips(field);
      $$("[data-show]", form).forEach((b) => b.onclick = () => { $(`[data-field="${b.dataset.show}"]`, form).hidden = false; b.hidden = true; $(`[data-chip-input="${b.dataset.show}"]`, form).focus(); });
      $("[data-files]", form).onchange = (e) => { this.files.push(...e.target.files); e.target.value = ""; this.renderFiles(); };
      // The message itself: formatting and pictures in the text (rte.js).
      const bar = $("[data-rte-bar]", form);
      this.rte = new window.SimexEditor($("[data-rte]", form), bar, { files: (fs) => { this.files.push(...fs); this.renderFiles(); this.touch(); } });
      $("[data-rte-image-pick]", form).onchange = (e) => { this.rte.pick(Array.from(e.target.files)); e.target.value = ""; };
      const fmt = $("[data-rte-toggle]", form);
      const showBar = (on) => { bar.hidden = !on; fmt.setAttribute("aria-pressed", on ? "true" : "false"); fmt.classList.toggle("on", on); };
      showBar(localStorage.getItem("simex-rte-bar") !== "0");
      fmt.onclick = () => { const on = bar.hidden; showBar(on); localStorage.setItem("simex-rte-bar", on ? "1" : "0"); };
      form.addEventListener("input", (e) => { if (!e.target.matches("[data-chip-input]")) this.touch(); });
      form.addEventListener("submit", (e) => { e.preventDefault(); this.send(); });
      // Enter in a one-line field (subject, an address being typed) doesn't send the message.
      form.addEventListener("keydown", (e) => {
        if (e.key === "Enter" && !(e.metaKey || e.ctrlKey) && e.target.matches("input:not([type=submit]):not([type=button])")) e.preventDefault();
      });
      $("[data-discard]", form).onclick = () => this.discard();
      if (form.elements.from) { form.elements.from.addEventListener("change", () => this.paintFrom()); this.paintFrom(); }
      this.key = newKey();
      this.timer = setInterval(() => { if (this.dirty && !this.saving) this.save(); }, 4000);
    }
    touch() { this.dirty = true; $("[data-draft-state]", this.form).textContent = ""; }
    bindChips(field) {
      const input = $(`[data-chip-input="${field}"]`, this.form);
      const box = $(`[data-field="${field}"] .gm-suggest`, this.form);
      let idx = 0, items = [];
      const show = () => {
        const q = input.value.trim().toLowerCase();
        items = BOOK.filter((b) => !this.keys[field].includes(b.key) && (!q || b.name.toLowerCase().includes(q) || b.addr.toLowerCase().includes(q))).slice(0, 60);  // the list scrolls
        if (!items.length || document.activeElement !== input) { box.hidden = true; return; }
        idx = Math.min(idx, items.length - 1);
        box.innerHTML = items.map((b, i) => `<div data-i="${i}" class="${i === idx ? "on" : ""}"><span class="gm-avatar" style="background:${colour(b.name)}">${esc((b.name || "?")[0].toUpperCase())}</span><span>${esc(b.name)}<small>${b.org ? esc(b.org) + " · " : ""}${esc(b.addr)}${b.kind === "team" ? " · team" : b.kind === "group" ? "" : b.kind === "member" ? " · team member" : b.kind === "tl" ? " · team leader" : ""}</small></span></div>`).join("");
        box.hidden = false;
      };
      input.addEventListener("input", () => { idx = 0; input.classList.remove("bad"); input.title = ""; show(); });
      input.addEventListener("focus", show);
      input.addEventListener("blur", () => setTimeout(() => { box.hidden = true; this.commitTyped(field, input); }, 150));
      input.addEventListener("keydown", (e) => {
        if (e.key === "ArrowDown") { idx = Math.min(idx + 1, items.length - 1); show(); e.preventDefault(); }
        else if (e.key === "ArrowUp") { idx = Math.max(idx - 1, 0); show(); e.preventDefault(); }
        else if ((e.key === "Enter" || e.key === "Tab" || e.key === ",") && !box.hidden && items[idx]) { e.preventDefault(); this.add(field, items[idx].key); input.value = ""; show(); }
        else if ((e.key === "Enter" || e.key === "," || e.key === ";" || (e.key === "Tab" && input.value.trim())) && input.value.trim()) { e.preventDefault(); this.commitTyped(field, input); }
        else if (e.key === "Backspace" && !input.value && this.keys[field].length) { this.remove(field, this.keys[field].at(-1)); }
      });
      box.addEventListener("mousedown", (e) => { const d = e.target.closest("[data-i]"); if (d) { e.preventDefault(); this.add(field, items[+d.dataset.i].key); input.value = ""; show(); } });
      $(`[data-chips="${field}"]`, this.form).addEventListener("click", () => input.focus());
    }
    // An address typed in full: from the address book, else anyone the mailbox may write to who
    // has exactly that address (the server says; then they're in the book from now on).
    async commitTyped(field, input) {
      const v = input.value.trim().replace(/[,;]+$/, "").replace(/^.*<([^>]+)>$/, "$1").trim().toLowerCase();
      if (!v) return;
      const hit = BOOK.find((b) => b.addr.toLowerCase() === v || b.name.toLowerCase() === v);
      if (hit) { this.add(field, hit.key); input.value = ""; return; }
      try {
        const r = await fetch(`${BASE}/lookup/?q=${encodeURIComponent(v)}`, { credentials: "same-origin", headers: { "X-Requested-With": "fetch" } });
        if (r.ok) {
          const b = await r.json();
          if (!byKey[b.key]) { BOOK.unshift(b); byKey[b.key] = b; }
          if (input.value.trim().toLowerCase().replace(/[,;]+$/, "").endsWith(v)) input.value = "";
          this.add(field, b.key);
          return;
        }
      } catch (e) { /* offline: left as typed */ }
      input.classList.add("bad");
      input.title = "No one in SIMEX Mail has this address: check it.";
    }
    async commitAll() {
      for (const f of ["to", "cc", "bcc"]) {
        const input = $(`[data-chip-input="${f}"]`, this.form);
        if (input && input.value.trim()) await this.commitTyped(f, input);
      }
    }
    async lookupName(key) {  // a recipient outside the book (a draft's, a reply's): its name and address
      try {
        const r = await fetch(`${BASE}/lookup/?key=${encodeURIComponent(key)}`, { credentials: "same-origin", headers: { "X-Requested-With": "fetch" } });
        if (r.ok) { byKey[key] = await r.json(); ["to", "cc", "bcc"].forEach((f) => this.keys[f].includes(key) && this.renderChips(f)); }
      } catch (e) { /* the key stays shown */ }
    }
    add(field, key) {
      const group = byKey[key] && byKey[key].kind === "group" ? byKey[key] : null;
      if (group) {  // the desk's "All teams": a chip for each team, each team its own message
        const taken = ["to", "cc", "bcc"].flatMap((f) => this.keys[f]);
        group.members.filter((k) => !taken.includes(k)).forEach((k) => this.keys[field].push(k));
        this.renderChips(field); this.touch();
        return;
      }
      if (!key || this.keys[field].includes(key)) return;
      this.keys[field].push(key);
      if (!byKey[key]) {
        if (key.startsWith("p:")) byKey[key] = { key, name: key.slice(2), addr: key.slice(2) + "@…", kind: "persona" };
        this.lookupName(key);
      }
      this.renderChips(field); this.touch();
    }
    remove(field, key) { this.keys[field] = this.keys[field].filter((k) => k !== key); this.renderChips(field); this.touch(); }
    renderChips(field) {
      const wrap = $(`[data-chips="${field}"]`, this.form);
      const input = $(`[data-chip-input="${field}"]`, wrap);
      $$(".gm-chip", wrap).forEach((c) => c.remove());
      for (const k of this.keys[field]) {
        const b = byKey[k] || { name: k, addr: "" };
        const chip = document.createElement("span");
        chip.className = "gm-chip";
        chip.title = b.addr;
        chip.innerHTML = `<span class="gm-avatar" style="background:${colour(b.name)}">${esc((b.name || "?")[0].toUpperCase())}</span>${esc(b.name)}<button type="button" aria-label="Remove">×</button>`;
        $("button", chip).onclick = (e) => { e.stopPropagation(); this.remove(field, k); };
        wrap.insertBefore(chip, input);
      }
      this.form.elements[field].value = this.keys[field].join(",");
      if (this.keys[field].length && field !== "to") $(`[data-field="${field}"]`, this.form).hidden = false;
    }
    renderFiles() {
      const box = $("[data-att-chips]", this.form);
      box.innerHTML = "";
      this.files.forEach((f, i) => {
        const c = document.createElement("span");
        c.className = "gm-att-chip";
        c.innerHTML = `${esc(f.name)} <small>(${Math.max(1, Math.round(f.size / 1024))}K)</small><button type="button" aria-label="Remove">×</button>`;
        $("button", c).onclick = () => { this.files.splice(i, 1); this.renderFiles(); };
        box.appendChild(c);
      });
    }
    load(d) {
      this.tried = false;
      this.key = d.key || newKey();  // a new compose session: a save still on its way for the last one is ignored
      this.form.elements.draft_id.value = d.id || "";
      this.form.elements.in_reply_to.value = d.in_reply_to || "";
      this.form.elements.forward_of.value = d.forward_of || "";
      for (const f of ["to", "cc", "bcc"]) { this.keys[f] = []; (d[f] || []).forEach((k) => this.add(f, k)); this.renderChips(f); }
      this.form.elements.subject.value = d.subject || "";
      this.rte.set(d.html || "", d.body || "");
      if (this.form.elements.from) {
        const sel = this.form.elements.from;
        // The persona desk has no default: "" until a persona is chosen (a reply: the one written to).
        const fallback = CFG.fromDefault === "" ? "" : (CFG.fromDefault || "team");
        sel.value = d.from || fallback;
        if (!sel.value) sel.value = fallback;  // a persona no longer on the list
        this.paintFrom();
      }
      this.files = []; this.renderFiles();
      this.dirty = false;
    }
    paintFrom() {  // which address it goes from, plain to see: the team's or the member's own (the desk: the persona's colour)
      const sel = this.form.elements.from;
      if (!sel) return;
      const field = sel.closest(".gm-from-field"), opt = sel.selectedOptions && sel.selectedOptions[0];
      field.classList.toggle("own", sel.value === "me");
      field.classList.toggle("unset", !sel.value);
      field.style.setProperty("--from-colour", (opt && opt.dataset.colour) || "transparent");
    }
    data(withFiles) {
      const fd = new FormData();
      for (const n of ["draft_id", "in_reply_to", "forward_of", "subject"]) fd.append(n, this.form.elements[n].value);
      fd.append("draft_key", this.key);
      fd.append("body", this.rte.text());
      fd.append("html", this.rte.html());
      this.posted = this.rte.cids();  // what the draft will have once this is saved
      for (const { cid, file } of this.rte.pending()) { fd.append("inline", file, file.name || "image"); fd.append("inline_cid", cid); }
      if (this.form.elements.from) fd.append("from", this.form.elements.from.value);
      for (const f of ["to", "cc", "bcc"]) fd.append(f, this.keys[f].join(","));
      if (withFiles) this.files.forEach((f) => fd.append("files", f));
      return fd;
    }
    empty() { return !this.keys.to.length && !this.keys.cc.length && !this.form.elements.subject.value.trim() && !this.rte.text() && !this.rte.hasImages(); }
    // One save at a time: on a slow connection a save still on its way when the next one was
    // due used to go without the draft's id and start another draft (Drafts filled with
    // fragments of one message). The session key (this.key) makes a retry after a lost answer
    // update the same draft; a save asked for meanwhile follows the one on its way.
    save() {
      if (CFG.readonly || this.sending || this.empty()) return this.saving || Promise.resolve();
      if (this.saving) return this.saving.then(() => (this.dirty ? this.save() : undefined));
      this.dirty = false;
      this.tried = true;
      const key = this.key;
      this.saving = (async () => {
        const ctl = new AbortController(), t = setTimeout(() => ctl.abort(), 20000);  // a stalled request: retried
        try {
          const r = await post(`${BASE}/draft/`, this.data(false), ctl.signal);
          if (this.key !== key) return;  // the window was cleared or opened on another message meanwhile
          if (r.status === 410) return;  // sent or discarded meanwhile: no new draft
          if (!r.ok) { this.dirty = true; return; }  // signed out or offline: saved on a later try
          const j = await r.json();
          if (this.form.elements.draft_id.value !== String(j.id)) this.rte.saved([]);
          this.form.elements.draft_id.value = j.id;
          this.rte.saved(this.posted || []);
          $("[data-draft-state]", this.form).textContent = "Draft saved";
        } catch (e) { if (this.key === key) this.dirty = true; } finally { clearTimeout(t); this.saving = null; }
      })();
      return this.saving;
    }
    async send() {
      if (this.sending) return;  // a second press (or Ctrl+Enter) while the first is on its way
      const btn = $(".gm-send", this.form);
      const mb = (this.files.reduce((s, f) => s + f.size, 0) + this.rte.size()) / 1048576;
      if (mb > CFG.maxMb) { snack(`Attachments and pictures are over ${CFG.maxMb} MB.`); return; }
      await this.commitAll();  // an address still being typed
      if ($$(".gm-chip-input.bad", this.form).length) { snack("Check the addresses: one of them isn't in SIMEX Mail."); return; }
      if (!this.keys.to.length && !this.keys.cc.length && !this.keys.bcc.length) { snack("Please specify at least one recipient."); return; }
      const from = this.form.elements.from;
      if (from && !from.value) { snack("Choose the persona it is from."); from.focus(); return; }
      btn.disabled = true;
      // No autosave while sending: a save landing after the send would leave a copy in Drafts.
      this.sending = true;
      let r;
      try {
        await this.saving;
        r = await post(`${BASE}/send/`, this.data(true));
      } catch (e) {
        r = null;
      } finally {
        btn.disabled = false;
      }
      if (signedOut(r)) { this.sending = false; signInAgain(); return; }
      const j = r ? await r.json().catch(() => ({})) : {};
      if (!r || !r.ok || !j.ok) { this.sending = false; snack(j.error || "Sending failed. Check your connection and try again."); return; }
      clearInterval(this.timer);
      sessionStorage.setItem("simex-snack", j.warning ? `Message sent. ${j.warning}` : "Message sent");
      if (this.opts.onSent) this.opts.onSent(j);
      location.reload();
    }
    async discard() {
      await this.saving;
      const id = this.form.elements.draft_id.value, params = [["action", "discard"], ["draft_key", this.key]];
      if (id) params.push(["draft_ids", id]);
      if (id || this.tried) await post(`${BASE}/action/`, new URLSearchParams(params)).catch(() => {});
      this.load({});
      if (this.opts.onClose) this.opts.onClose(true);
      snack("Draft discarded");
    }
  }

  // floating window
  const win = $("#compose");
  let floating = null;
  if (win && !CFG.readonly) {
    floating = new Composer($("form", win), { onClose: () => { win.hidden = true; } });
    const open = (d) => {
      win.hidden = false; win.classList.remove("min");
      floating.load(d || {});
      $(".gm-compose-title", win).textContent = (d && d.subject) || "New Message";
      setTimeout(() => $('[data-chip-input="to"]', win).focus(), 30);
    };
    window.simexCompose = open;
    $$("[data-compose]").forEach((b) => b.onclick = () => open());
    $("[data-compose-minimize]", win).onclick = (e) => { e.stopPropagation(); win.classList.toggle("min"); win.classList.remove("full"); };
    $("[data-compose-expand]", win).onclick = (e) => { e.stopPropagation(); win.classList.toggle("full"); win.classList.remove("min"); };
    $("[data-compose-close]", win).onclick = async (e) => { e.stopPropagation(); await floating.save(); win.hidden = true; if (floating.form.elements.draft_id.value) snack("Draft saved"); };
    $(".gm-compose-head", win).onclick = () => { if (win.classList.contains("min")) win.classList.remove("min"); };
    $("[name=subject]", win).addEventListener("input", (e) => { $(".gm-compose-title", win).textContent = e.target.value || "New Message"; });
  }

  // --- list page ------------------------------------------------------------------------------------
  const bulk = $("[data-bulk]");
  if (bulk) {
    const rows = $$(".gm-row", bulk);
    const boxes = () => $$('input[name="threads"]', bulk);
    const sync = () => {
      const n = boxes().filter((b) => b.checked).length;
      $(".gm-tb-selected", bulk).hidden = n === 0 || CFG.readonly;
      $(".gm-tb-idle", bulk).hidden = n > 0 && !CFG.readonly;
      rows.forEach((r) => {
        const on = $('input[name="threads"]', r).checked;
        r.classList.toggle("selected", on);
        const av = $("[data-select-row]", r); if (av) av.setAttribute("aria-checked", on ? "true" : "false");
      });
      document.body.classList.toggle("selecting", n > 0 && !CFG.readonly);
      const all = $("[data-select-all]", bulk);
      all.checked = n > 0 && n === boxes().length;
    };
    $("[data-select-all]", bulk).onchange = (e) => { boxes().forEach((b) => b.checked = e.target.checked); sync(); };
    boxes().forEach((b) => b.onchange = sync);
    $$("[data-select-and]", bulk).forEach((b) => b.onclick = () => {
      boxes().forEach((x) => x.checked = true);
      const i = document.createElement("input"); i.type = "hidden"; i.name = "action"; i.value = b.dataset.selectAnd; bulk.appendChild(i); bulk.submit();
    });
    // Phones (no tick boxes): tap the avatar to select, as in the Gmail app; while anything is
    // selected, tapping a row selects or unselects it instead of opening it.
    const toggleRow = (r) => { const c = $('input[name="threads"]', r); c.checked = !c.checked; sync(); };
    $$("[data-select-row]", bulk).forEach((av) => {
      av.addEventListener("click", (e) => { e.stopPropagation(); toggleRow(av.closest(".gm-row")); });
      av.addEventListener("keydown", (e) => { if (e.key === " " || e.key === "Enter") { e.preventDefault(); toggleRow(av.closest(".gm-row")); } });
    });
    // Ctrl/Cmd-click or middle-click opens the conversation in a new tab, as a link would:
    // the persona desk keeps its list in one tab and works conversations in others.
    const newTab = (r) => { if (r.dataset.href) window.open(r.dataset.href, "_blank", "noopener"); };
    rows.forEach((r) => r.addEventListener("auxclick", (e) => {
      if (e.button !== 1 || e.target.closest("button, a, label")) return;
      e.preventDefault(); newTab(r);
    }));
    rows.forEach((r) => r.addEventListener("mousedown", (e) => { if (e.button === 1 && r.dataset.href && !e.target.closest("a")) e.preventDefault(); }));  // no autoscroll
    rows.forEach((r) => r.addEventListener("click", (e) => {
      if (e.target.closest("button, a, label")) return;
      if ((e.metaKey || e.ctrlKey) && r.dataset.href) { newTab(r); return; }
      if (document.body.classList.contains("selecting") && getComputedStyle($("[data-select-row]", r) || r).display === "flex") {
        toggleRow(r); return;
      }
      if (r.dataset.draft) { openDraft(r.dataset.draft); return; }
      location.href = r.dataset.href;
    }));
    $$("[data-star-thread]").forEach((b) => b.onclick = async (e) => {
      e.stopPropagation();
      const on = !b.classList.contains("on");
      await post(`${BASE}/action/`, { action: on ? "star" : "unstar", threads: b.dataset.starThread });
      b.classList.toggle("on", on);
      b.innerHTML = on ? STAR : STAR_BORDER;
    });
    $$("[data-row-action]").forEach((b) => b.onclick = async (e) => {
      e.stopPropagation();
      const row = b.closest(".gm-row");
      const act = b.dataset.rowAction;
      const params = new URLSearchParams([["action", act], ["threads", row.dataset.thread]]);
      if (act === "discard") params.append("draft_ids", row.dataset.draft);
      await post(`${BASE}/action/`, params);
      if (act === "read" || act === "unread") { location.reload(); return; }
      row.remove();
      snack(act === "archive" ? "Conversation archived." : act === "trash" ? "Conversation moved to Trash." : act === "restore" ? "Conversation moved to Inbox." : "Draft discarded.",
        act === "archive" || act === "trash" ? { label: "Undo", run: async () => { await post(`${BASE}/action/`, { action: act === "archive" ? "unarchive" : "restore", threads: row.dataset.thread }); location.reload(); } } : null);
    });
    // The persona desk: a conversation needs no reply (desk.mark), from its row.
    $$("[data-desk-act]").forEach((b) => b.onclick = async (e) => {
      e.stopPropagation();
      const row = b.closest(".gm-row");
      const r = await post("/inbox/desk/mark/", { act: b.dataset.deskAct, threads: row.dataset.thread });
      if (!r.ok) { snack("Not saved: try again."); return; }
      sessionStorage.setItem("simex-snack", "Marked as needing no reply.");
      location.reload();
    });
    // keyboard focus in the list
    let focus = -1;
    window.simexListMove = (d) => {
      if (!rows.length) return;
      rows.forEach((r) => r.classList.remove("focus"));
      focus = Math.max(0, Math.min(rows.length - 1, focus + d));
      rows[focus].classList.add("focus"); rows[focus].scrollIntoView({ block: "nearest" });
    };
    window.simexListOpen = () => { if (focus >= 0) rows[focus].click(); };
    window.simexListOpenTab = () => { if (focus >= 0) newTab(rows[focus]); };
    // Back to the list's tab after working conversations in others: reload it so what was answered
    // there has moved on (not while anything is selected or being written).
    let hiddenAt = 0;
    document.addEventListener("visibilitychange", () => {
      if (document.hidden) { hiddenAt = Date.now(); return; }
      if (hiddenAt && Date.now() - hiddenAt > 5000 && !boxes().some((b) => b.checked) && (!win || win.hidden)) location.reload();
    });
    window.simexListAct = (act) => { if (focus >= 0) { const b = $(`[data-row-action="${act}"]`, rows[focus]); if (b) b.click(); } };
    window.simexListToggle = () => { if (focus >= 0) { const c = $('input[name="threads"]', rows[focus]); c.checked = !c.checked; sync(); } };
  }
  // --- labels (the team's own folders): Move to, Label as, create / edit / colour / remove, drag & drop
  const after = (text) => { sessionStorage.setItem("simex-snack", text); location.reload(); };
  const threadForm = $("[data-thread-actions]");
  // The conversations a toolbar menu acts on, each with the ids of its labels.
  const targets = () => (threadForm ? [threadForm] : $$('input[name="threads"]:checked').map((b) => b.closest(".gm-row")))
    .map((el) => ({ thread: el.dataset.thread, labels: (el.dataset.labels || "").split(",").filter(Boolean) }));
  const labelName = (id) => ((CFG.labels || []).find((l) => String(l.id) === String(id)) || {}).name || "";
  const convs = (n) => (n === 1 ? "Conversation" : `${n} conversations`);
  async function relabel(params, done) {
    const r = await post(`${BASE}/action/`, params);
    if (r.ok) after(done); else snack("Something went wrong. Try again.");
  }
  async function move(threads, to) {
    const p = new URLSearchParams([["action", "move"], ["to", to], ["from", CFG.folder]]);
    threads.forEach((t) => p.append("threads", t));
    const r = await post(`${BASE}/action/`, p);
    if (!r.ok) { snack("Something went wrong. Try again."); return; }
    sessionStorage.setItem("simex-snack", `${convs(threads.length)} moved to "${to === "inbox" ? "Inbox" : labelName(to)}".`);
    if (threadForm) location.href = $("[data-back]")?.href || `${BASE}/?folder=${encodeURIComponent(CFG.folder)}`; else location.reload();  // as Gmail: back to the list
  }
  $$("[data-label-filter]").forEach((f) => f.addEventListener("input", () => {
    const q = f.value.trim().toLowerCase();
    $$("[data-name]", f.closest(".gm-lmenu")).forEach((el) => { el.hidden = q && !el.dataset.name.includes(q); });
  }));
  $$("[data-move-to]").forEach((b) => b.onclick = () => move(targets().map((t) => t.thread), b.dataset.moveTo));
  $$("[data-label-menu]").forEach((menu) => {
    const boxes = $$('input[type="checkbox"]', menu);
    const apply = $("[data-label-apply]", menu);
    menu.previousElementSibling.addEventListener("click", () => {  // opening: tick what the selection already has
      const ts = targets();
      boxes.forEach((b) => {
        const n = ts.filter((t) => t.labels.includes(b.value)).length;
        b.checked = n > 0 && n === ts.length; b.indeterminate = n > 0 && n < ts.length;
        b.dataset.was = b.indeterminate ? "some" : b.checked ? "all" : "none";
      });
      apply.hidden = true;
      const f = $("[data-label-filter]", menu); f.value = ""; f.dispatchEvent(new Event("input"));
      setTimeout(() => f.focus(), 0);
    });
    boxes.forEach((b) => b.onchange = () => {
      apply.hidden = !boxes.some((x) => (x.indeterminate ? "some" : x.checked ? "all" : "none") !== x.dataset.was);
    });
    apply.onclick = async () => {
      const threads = targets().map((t) => t.thread);
      const add = boxes.filter((b) => !b.indeterminate && b.checked && b.dataset.was !== "all").map((b) => b.value);
      const del = boxes.filter((b) => !b.indeterminate && !b.checked && b.dataset.was !== "none").map((b) => b.value);
      for (const [act, ids] of [["label_add", add], ["label_remove", del]]) {
        if (!ids.length) continue;
        const p = new URLSearchParams([["action", act]]);
        threads.forEach((t) => p.append("threads", t)); ids.forEach((i) => p.append("label", i));
        await post(`${BASE}/action/`, p);
      }
      after(add.length === 1 && !del.length ? `${convs(threads.length)} added to "${labelName(add[0])}".`
        : del.length === 1 && !add.length ? `${convs(threads.length)} removed from "${labelName(del[0])}".` : "Labels changed.");
    };
  });
  $$("[data-unlabel]").forEach((b) => b.onclick = () =>
    relabel({ action: "label_remove", threads: threadForm.dataset.thread, label: b.dataset.unlabel },
            `Conversation removed from "${labelName(b.dataset.unlabel)}".`));

  // New label / edit: a small dialog. From a Move to / Label as menu the new label is applied at once.
  const dlg = $("#label-dialog");
  function labelDialog({ title, prompt, ok, name = "", submit }) {
    const f = $("[data-label-form]", dlg), err = $("[data-dialog-err]", dlg);
    $("[data-dialog-title]", dlg).textContent = title; $("[data-dialog-prompt]", dlg).textContent = prompt;
    $("[data-dialog-ok]", dlg).textContent = ok; f.elements.name.value = name; err.hidden = true;
    f.onsubmit = async (e) => {
      e.preventDefault();
      const r = await post(`${BASE}/labels/`, submit(f.elements.name.value));
      const j = await r.json().catch(() => ({}));
      if (!r.ok) { err.textContent = j.error || "Something went wrong."; err.hidden = false; return; }
      dlg.close(); after(submit.done(f.elements.name.value.trim()));
    };
    $("[data-dialog-cancel]", dlg).onclick = () => dlg.close();
    dlg.showModal(); f.elements.name.select();
  }
  $$("[data-label-create]").forEach((b) => b.onclick = () => {
    const mode = b.dataset.labelCreate;  // "" (sidebar), "move" or "label"
    const threads = mode ? targets().map((t) => t.thread) : [];
    const submit = (name) => {
      const p = new URLSearchParams([["op", "create"], ["name", name], ["from", CFG.folder]]);
      if (mode === "move") p.append("move", "1");
      threads.forEach((t) => p.append("threads", t));
      return p;
    };
    submit.done = (name) => (threads.length ? `${convs(threads.length)} ${mode === "move" ? "moved" : "added"} to "${name}".`
      : `Label "${name}" created.`);
    labelDialog({ title: "New label", prompt: "Please enter a new label name:", ok: "Create", submit });
  });
  $$("[data-label-rename]").forEach((b) => b.onclick = () => {
    const submit = (name) => ({ op: "rename", id: b.dataset.labelRename, name });
    submit.done = (name) => `Label renamed to "${name}".`;
    labelDialog({ title: "Edit label", prompt: "Label name:", ok: "Save", name: b.dataset.name, submit });
  });
  $$("[data-label-delete]").forEach((b) => b.onclick = async () => {
    if (!confirm(`Remove the label "${b.dataset.name}"? Its conversations stay in your mailbox (All mail).`)) return;
    await post(`${BASE}/labels/`, { op: "delete", id: b.dataset.labelDelete });
    const here = CFG.folder === `l${b.dataset.labelDelete}`;
    sessionStorage.setItem("simex-snack", `Label "${b.dataset.name}" removed.`);
    if (here) location.href = `${BASE}/`; else location.reload();
  });
  $$(".gm-lmore [data-menu]").forEach((b) => b.addEventListener("click", () => {
    const m = b.nextElementSibling, r = b.getBoundingClientRect();
    m.style.top = `${Math.min(r.bottom + 4, window.innerHeight - 220)}px`;
    m.style.left = `${Math.max(8, Math.min(r.left, window.innerWidth - 200))}px`;
  }));
  $$("[data-label-colour]").forEach((b) => b.onclick = async () => {
    await post(`${BASE}/labels/`, { op: "colour", id: b.dataset.id, colour: b.dataset.labelColour });
    location.reload();
  });

  // Drag conversations (the selected ones, or the one dragged) onto a label or the Inbox: Move to.
  let dragged = null;
  $$(".gm-row[draggable]").forEach((r) => r.addEventListener("dragstart", (e) => {
    const box = $('input[name="threads"]', r);
    dragged = box.checked ? $$('input[name="threads"]:checked').map((b) => b.value) : [r.dataset.thread];
    e.dataTransfer.effectAllowed = "move";
    e.dataTransfer.setData("text/plain", convs(dragged.length));
  }));
  document.addEventListener("dragend", () => { dragged = null; $$(".drop-over").forEach((x) => x.classList.remove("drop-over")); });
  $$("[data-drop-to]").forEach((a) => {
    if (CFG.readonly) return;
    a.addEventListener("dragover", (e) => { if (dragged) { e.preventDefault(); a.classList.add("drop-over"); } });
    a.addEventListener("dragleave", () => a.classList.remove("drop-over"));
    a.addEventListener("drop", (e) => { if (!dragged) return; e.preventDefault(); move(dragged, a.dataset.dropTo); });
  });

  const STAR = '<svg class="ic" width="20" height="20" viewBox="0 0 24 24"><path d="M12 17.27L18.18 21l-1.64-7.03L22 9.24l-7.19-.61L12 2 9.19 8.63 2 9.24l5.46 4.73L5.82 21z"/></svg>';
  const STAR_BORDER = '<svg class="ic" width="20" height="20" viewBox="0 0 24 24"><path d="M22 9.24l-7.19-.62L12 2 9.19 8.63 2 9.24l5.46 4.73L5.82 21 12 17.27 18.18 21l-1.63-7.03L22 9.24zM12 15.4l-3.76 2.27 1-4.28-3.32-2.88 4.38-.38L12 6.1l1.71 4.04 4.38.38-3.32 2.88 1 4.28L12 15.4z"/></svg>';

  async function openDraft(id) {
    if (!window.simexCompose) return;
    const r = await fetch(`${BASE}/draft/${id}/`, { credentials: "same-origin" });
    if (r.ok) window.simexCompose(await r.json());
  }

  // --- thread page ----------------------------------------------------------------------------------
  const tdata = $("#thread-data") ? JSON.parse($("#thread-data").textContent) : null;
  let inline = null;
  if (tdata) {
    $$("[data-toggle-msg]").forEach((h) => h.addEventListener("click", (e) => {
      if (e.target.closest("button, a, .gm-details")) return;
      const art = h.closest(".gm-msg");
      if (art.classList.contains("closed")) { art.classList.remove("closed"); art.classList.add("open"); }
      else if (art.nextElementSibling && art.nextElementSibling.classList.contains("gm-msg")) { art.classList.add("closed"); art.classList.remove("open"); }
    }));
    $$(".gm-msg.closed").forEach((a) => a.addEventListener("click", (e) => { if (!e.target.closest("button, a")) { a.classList.remove("closed"); a.classList.add("open"); } }));
    $$("[data-star-mail]").forEach((b) => b.onclick = async (e) => {
      e.stopPropagation();
      const r = await post(`${BASE}/star/${b.dataset.starMail}/`, {});
      const j = await r.json();
      b.classList.toggle("on", j.starred); b.innerHTML = j.starred ? STAR : STAR_BORDER;
    });
    const box = $(".gm-inline-reply");
    if (box) {
      inline = new Composer($("form", box), { inline: true });
      const re = (s) => /^re:/i.test(s) ? s : `Re: ${s}`;
      const fw = (s) => /^fwd?:/i.test(s) ? s : `Fwd: ${s}`;
      const openInline = (mode, id) => {
        const m = tdata.msgs[id] || tdata.msgs[tdata.last];
        let d;
        if (mode === "forward") {
          const head = `---------- Forwarded message ---------\nFrom: ${m.from} <${m.addr}>\nDate: ${m.date}\nSubject: ${m.subject}\nTo: ${m.to}`;
          d = { to: [], subject: fw(m.subject), forward_of: id, from: m.as || tdata.from,
            body: `\n\n${head}\n\n${m.body}`,
            // a formatted message is forwarded as it is, its pictures with it
            html: m.html ? `<div><br></div><div><br></div><div>${esc(head).replace(/\n/g, "<br>")}</div><div><br></div>${m.html}` : "" };
        } else {
          // The conversation above is the context, so (as in webmail) the quote is not pasted in.
          d = { to: mode === "all" ? tdata.replyAll : (m.reply && m.reply.length ? m.reply : tdata.reply), subject: re(m.subject), in_reply_to: id, body: "",
                from: m.as || tdata.from };  // the persona desk answers as the persona written to
        }
        inline.load(d);
        $(".gm-inline-form", box).dataset.mode = mode;
        box.hidden = false; box.classList.remove("min"); $("[data-reply-actions]").hidden = true;
        title();
        inline.rte.focus(true);
        if (!box.classList.contains("popped")) box.scrollIntoView({ block: "nearest", behavior: "smooth" });
        if (mode === "forward") $('[data-chip-input="to"]', box).focus();
      };
      // Pop out (as in webmail): the same reply, in a window over the page, so the conversation
      // scrolls while it is written; pop in puts it back under the conversation.
      const title = () => { $("[data-reply-title]", box).textContent = inline.form.elements.subject.value || "Reply"; };
      inline.form.elements.subject.addEventListener("input", title);
      const pop = (on) => {
        box.classList.toggle("popped", on); box.classList.remove("min");
        document.body.classList.toggle("reply-popped", on);
        inline.rte.focus();
        if (!on) box.scrollIntoView({ block: "nearest", behavior: "smooth" });
      };
      const shut = () => { box.hidden = true; box.classList.remove("popped", "min"); document.body.classList.remove("reply-popped"); $("[data-reply-actions]").hidden = false; };
      $("[data-reply-popout]", box).onclick = () => pop(true);
      $("[data-reply-popin]", box).onclick = (e) => { e.stopPropagation(); pop(false); };
      $("[data-reply-minimize]", box).onclick = (e) => { e.stopPropagation(); box.classList.toggle("min"); };
      $("[data-reply-head]", box).onclick = () => box.classList.remove("min");
      $("[data-reply-close]", box).onclick = async (e) => { e.stopPropagation(); await inline.save(); shut(); if (inline.form.elements.draft_id.value) snack("Draft saved"); };
      window.simexReplyPopped = () => !box.hidden && box.classList.contains("popped");
      inline.opts.onClose = shut;
      $$("[data-reply]").forEach((b) => b.onclick = (e) => { e.stopPropagation(); openInline("reply", b.dataset.reply); });
      $$("[data-reply-all]").forEach((b) => b.onclick = (e) => { e.stopPropagation(); openInline("all", b.dataset.replyAll); });
      $$("[data-forward]").forEach((b) => b.onclick = (e) => { e.stopPropagation(); openInline("forward", b.dataset.forward); });
      window.simexReply = (mode) => openInline(mode, String(tdata.last));
      // Suggest a reply (suggest.js): answer that message with the suggested text.
      window.simexReplyWith = (id, body) => {
        openInline("reply", tdata.msgs[id] ? id : String(tdata.last));
        inline.rte.setText(body);
        inline.rte.focus(true);
      };
    }
    // The persona desk: who else on the desk has this conversation open, or is answering it.
    const pres = $("[data-presence]");
    if (pres && tdata.presence) {
      const ping = async () => {
        if (document.hidden) return;
        const replying = !!(box && !box.hidden && inline && !inline.empty());
        try {
          const r = await post(tdata.presence, { thread: tdata.thread, replying: replying ? "1" : "0" });
          if (!r.ok) return;
          const others = (await r.json()).others || [];
          const busy = others.filter((o) => o.replying), names = (l) => l.map((o) => o.name).join(", ");
          pres.hidden = !others.length;
          pres.classList.toggle("replying", busy.length > 0);
          pres.textContent = busy.length ? `${names(busy)} ${busy.length > 1 ? "are" : "is"} writing an answer in this conversation.`
            : `${names(others)} also ${others.length > 1 ? "have" : "has"} this conversation open.`;
        } catch (e) { /* offline: next time */ }
      };
      ping();
      setInterval(ping, 15000);
      if (box) box.addEventListener("input", () => { if (!ping.sent) { ping.sent = true; ping(); } });
    }
  }

  // --- keyboard shortcuts ---------------------------------------------------------------------------
  document.addEventListener("keydown", (e) => {
    const t = e.target;
    if (e.key === "Escape") {
      $$("[data-fmore][open]").forEach((d) => { d.open = false; });
      if (win && !win.hidden) { $("[data-compose-close]", win).click(); }
      else if (window.simexReplyPopped && window.simexReplyPopped()) { $("[data-reply-close]").click(); }
      if (t.matches && t.matches("input, textarea, [contenteditable]")) t.blur();
      return;
    }
    if (e.metaKey || e.ctrlKey || e.altKey) {
      if ((e.key === "Enter") && t.closest && t.closest("[data-composer]")) { t.closest("[data-composer]").requestSubmit(); }
      return;
    }
    if (t.matches && t.matches("input, textarea, select, [contenteditable]")) return;
    const k = e.key;
    if (k === "c" && window.simexCompose) { e.preventDefault(); window.simexCompose(); }
    else if ((k === "v" || k === "l") && !CFG.readonly) {  // Move to / Labels, for the open or selected conversations
      const b = $(k === "v" ? "[data-key-move]" : "[data-key-label]");
      if (b && b.offsetParent) { e.preventDefault(); b.click(); }
    }
    else if (k === "/") { e.preventDefault(); $("[data-search]").focus(); }
    else if (tdata) {
      if (k === "r" && window.simexReply) { e.preventDefault(); window.simexReply("reply"); }
      else if (k === "a" && window.simexReply) { e.preventDefault(); window.simexReply("all"); }
      else if (k === "f" && window.simexReply) { e.preventDefault(); window.simexReply("forward"); }
      else if (k === "j") { const a = $("[data-key-j]"); if (a && !a.classList.contains("disabled")) location.href = a.href; }
      else if (k === "k") { const a = $("[data-key-k]"); if (a && !a.classList.contains("disabled")) location.href = a.href; }
      else if (k === "e") { const b = $("[data-key-archive]"); if (b) b.click(); }
      else if (k === "#") { const b = $("[data-key-trash]"); if (b) b.click(); }
      else if (k === "u") { location.href = $("[data-back]").href; }
    } else if (bulk) {
      if (k === "j") window.simexListMove(1);
      else if (k === "k") window.simexListMove(-1);
      else if (k === "O") window.simexListOpenTab();
      else if (k === "Enter" || k === "o") window.simexListOpen();
      else if (k === "e") window.simexListAct("archive");
      else if (k === "#") window.simexListAct("trash");
      else if (k === "x") window.simexListToggle();
    }
  });

  // --- the persona desk's bell: this person's own notices (desk.notify) -----------------------------
  const dn = (() => {
    const box = $("[data-dn]");
    if (!box) return { update() {} };
    const panel = $("[data-dn-panel]", box), list = $("[data-dn-list]", box), badge = $("[data-dn-n]", box);
    const btn = $("[data-dn-toggle]", box);
    const start = JSON.parse(($("#simex-dn") || {}).textContent || "{}");
    const seenKey = "simex-dn-latest";
    let latest = Number(localStorage.getItem(seenKey) || 0), known = start.latest || 0;
    if (!localStorage.getItem(seenKey)) { latest = known; localStorage.setItem(seenKey, String(known)); }
    const setBadge = (n) => { badge.textContent = n ? n : ""; };
    async function load() {
      const r = await fetch("/inbox/desk/notices/", { credentials: "same-origin", headers: { "X-Requested-With": "fetch" } });
      if (!r.ok) return null;
      const j = await r.json();
      setBadge(j.unread);
      list.innerHTML = j.items.length ? j.items.map((n) => `<li class="gm-dn-item${n.read ? "" : " unread"} k-${esc(n.kind)}" data-id="${n.id}" data-url="${esc(n.url)}">
          <button type="button" class="gm-dn-open"><b>${esc(n.title)}</b>${n.test ? ' <span class="gm-label test">Test</span>' : ""}
          ${n.body ? `<span class="gm-dn-body">${esc(n.body)}</span>` : ""}<small>${esc(n.at)} · ${esc(n.ago)}</small></button>
          <button type="button" class="gm-icon-btn sm gm-dn-x" data-dn-x title="Dismiss" aria-label="Dismiss">✕</button></li>`).join("")
        : '<li class="gm-dn-empty">Nothing for you yet.</li>';
      return j;
    }
    const open = (on) => { panel.hidden = !on; btn.setAttribute("aria-expanded", on ? "true" : "false"); if (on) load(); };
    btn.onclick = () => open(panel.hidden);  // no stopPropagation: opening it closes the What's new panel
    document.addEventListener("click", (e) => { if (!panel.hidden && !e.target.closest("[data-dn]")) open(false); });
    document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !panel.hidden) open(false); });
    list.addEventListener("click", async (e) => {
      const li = e.target.closest(".gm-dn-item");
      if (!li) return;
      if (e.target.closest("[data-dn-x]")) {
        const r = await post("/inbox/desk/notices/mark/", { act: "dismiss", ids: li.dataset.id });
        if (r.ok) { li.remove(); setBadge((await r.json()).unread); if (!$(".gm-dn-item", list)) load(); }
        return;
      }
      await post("/inbox/desk/notices/mark/", { act: "read", ids: li.dataset.id });
      if (li.dataset.url) location.href = li.dataset.url; else load();
    });
    $("[data-dn-all]", box).onclick = async () => { await post("/inbox/desk/notices/mark/", { act: "read", all: "1" }); load(); };
    $("[data-dn-clear]", box).onclick = async () => { await post("/inbox/desk/notices/mark/", { act: "dismiss", all: "1" }); load(); };
    return {
      async update(c) {  // from the poll: {unread, latest}
        setBadge(c.unread);
        if (c.latest > known) { known = c.latest; if (!panel.hidden) load(); }
        if (c.latest > latest) {
          const j = !panel.hidden ? null : await load();
          const fresh = j ? j.items.filter((n) => n.id > latest && !n.read) : [];
          latest = c.latest; localStorage.setItem(seenKey, String(latest));
          if (fresh.length) {
            const n = fresh[0], more = fresh.length > 1 ? ` (+${fresh.length - 1} more)` : "";
            snack(`${n.title}${more}`, { label: "Open", run: () => open(true) }, true);
          }
        } else if (c.latest < latest) { latest = c.latest; localStorage.setItem(seenKey, String(latest)); }  // cleared
      },
    };
  })();

  // --- polling: unread count, tab title and icon, chime, desktop notification ----------------------
  const N = window.simexNotify;
  const soundBtn = $("[data-sound]");
  const paintSound = () => {
    if (!soundBtn) return;
    soundBtn.classList.toggle("on", N.enabled());
    soundBtn.title = N.enabled() ? "Alerts on: sound and notifications (click to mute)" : "Alerts muted (click to turn on)";
  };
  if (soundBtn) soundBtn.onclick = async () => {
    N.setEnabled(!N.enabled()); paintSound();
    if (N.enabled()) { await N.ask(); N.beep(); paintBanner(); pushOn(); } else pushOff();
  };
  paintSound();

  // Push (Web Push via the service worker): new mail is notified even with SIMEX Mail closed, e.g.
  // on an iPhone once it is on the home screen. While this device is subscribed the page leaves
  // system notifications to the service worker (no doubles) and keeps the chime and the snackbar.
  const PUSH = "simex-push";
  // A team leader's tabs (their mail, the team inbox read-only, the Dashboard) all keep their device
  // subscribed for their own mailbox; ExCon's read-only views never subscribe.
  const alertsHere = !CFG.readonly || CFG.leader;
  const PUSH_BASE = CFG.pushBase || BASE;
  const pushable = alertsHere && !!CFG.vapid && "serviceWorker" in navigator && "PushManager" in window;
  const swReady = pushable ? navigator.serviceWorker.register("/inbox/sw.js", { scope: "/inbox/" }).catch(() => null)
    : Promise.resolve(null);
  const b64 = (s) => Uint8Array.from(atob(s.replace(/-/g, "+").replace(/_/g, "/") + "===".slice((s.length + 3) % 4)),
                                     (c) => c.charCodeAt(0));
  const sameKey = (sub, key) => {
    const k = sub.options && sub.options.applicationServerKey;
    return !k || new Uint8Array(k).join() === key.join();
  };
  async function pushOn() {
    const reg = await swReady;
    if (!reg || !N.enabled() || N.permission() !== "granted") return;
    try {
      const key = b64(CFG.vapid);
      let sub = await reg.pushManager.getSubscription();
      if (sub && !sameKey(sub, key)) { await sub.unsubscribe(); sub = null; }  // the server key changed
      sub = sub || await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: key });
      const j = sub.toJSON();
      const r = await post(`${PUSH_BASE}/push/`, { endpoint: j.endpoint, p256dh: j.keys.p256dh, auth: j.keys.auth });
      if (r.ok) localStorage.setItem(PUSH, "1");
    } catch (e) { localStorage.removeItem(PUSH); }
  }
  async function pushOff() {
    localStorage.removeItem(PUSH);
    const reg = await swReady;
    const sub = reg && await reg.pushManager.getSubscription();
    if (sub) { await post(`${PUSH_BASE}/push/`, { endpoint: sub.endpoint, off: "1" }); await sub.unsubscribe(); }
  }
  const pushing = () => pushable && localStorage.getItem(PUSH) === "1" && N.permission() === "granted";
  if (pushable) {
    pushOn();  // keeps the server's copy current (new login on this device, restored backup, new key)
    navigator.serviceWorker.addEventListener("message", (e) => { if (e.data && e.data.type === "simex-push") poll(); });
    // Signing out stops this device's notifications (a shared phone or laptop must not show the
    // last team's mail); signing in again turns them back on.
    const out = document.querySelector('form[action="/logout/"]');
    if (out) out.addEventListener("submit", (e) => {
      if (out.dataset.done) return;
      e.preventDefault();
      out.dataset.done = "1";
      Promise.race([pushOff().catch(() => {}), new Promise((r) => setTimeout(r, 2000))]).then(() => out.submit());
    });
  }

  // iPhone/iPad in Safari: notifications only work from the home-screen app, so say how to add it.
  const install = $("#gm-install");
  const ios = /iPad|iPhone|iPod/.test(navigator.userAgent) || (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);
  const standalone = navigator.standalone || matchMedia("(display-mode: standalone)").matches;
  if (install && CFG.vapid && alertsHere && ios && !standalone && localStorage.getItem("simex-install-asked") !== "1") {
    install.hidden = false;
    $("[data-install-ok]", install).onclick = () => { localStorage.setItem("simex-install-asked", "1"); install.hidden = true; };
  }

  // Ask once for desktop notifications (it needs a click), unless muted or already decided.
  const banner = $("#gm-alerts");
  function paintBanner() {
    if (!banner) return;
    banner.hidden = !alertsHere || !N.enabled() || N.permission() !== "default" || localStorage.getItem("simex-alerts-asked") === "1";
  }
  if (banner) {
    $("[data-alerts-on]", banner).onclick = async () => {
      await N.ask(); N.beep(); localStorage.setItem("simex-alerts-asked", "1"); paintBanner(); pushOn();
    };
    $("[data-alerts-later]", banner).onclick = () => { localStorage.setItem("simex-alerts-asked", "1"); paintBanner(); };
    paintBanner();
  }

  // The device setup page after a QR sign-in: the one step this device still needs.
  const setup = $("[data-setup]");
  if (setup) {
    [banner, install].forEach((el) => { if (el) el.hidden = true; });  // this page says it instead
    const paintSetup = () => {
      const p = N.permission();
      const step = ios && !standalone ? "install" : p === "unsupported" ? "nonotify" : p === "denied" ? "blocked"
        : p === "granted" && N.enabled() ? "done" : "alerts";
      $$("[data-step]", setup).forEach((el) => { el.hidden = el.dataset.step !== step; });
      // The home-screen app starts at the page it was added from (or the manifest's start): make
      // that this person's sign-in link, as an iPhone's app doesn't share Safari's sign-in.
      if (step === "install") history.replaceState(null, "", setup.dataset.appUrl);
    };
    $("[data-setup-alerts]", setup).onclick = async () => {
      N.setEnabled(true); paintSound();
      await N.ask(); N.beep(); localStorage.setItem("simex-alerts-asked", "1"); pushOn(); paintSetup();
    };
    paintSetup();
  }

  // The simulation clock (ops/simclock.py): ticks from the time the server gave and its speed;
  // each poll brings it back in line (ExCon may set it, change its speed or turn it off).
  const simEl = $("[data-simclock]");
  const simCfg = document.getElementById("simex-clock");
  let sim = simCfg ? JSON.parse(simCfg.textContent) : null, simT0 = Date.now();
  function simTick() {
    if (!simEl || !sim) return;
    const tz = sim.tz, at = new Date(sim.sim + (Date.now() - simT0) * sim.rate);
    const part = (o) => new Intl.DateTimeFormat("en-GB", { timeZone: tz, ...o }).format(at);
    $("[data-sim-mon]", simEl).textContent = part({ month: "short" });
    $("[data-sim-day]", simEl).textContent = part({ day: "numeric" });
    $("[data-sim-time]", simEl).textContent = part({ hour: "2-digit", minute: "2-digit", hourCycle: "h23" });
    $("[data-sim-date]", simEl).textContent = part({ weekday: "short", day: "numeric", month: "short", year: "numeric" }).replace(/,/g, "");
    const real = $("[data-real-time]", simEl);
    if (real) {
      const now = new Date(), r = (o) => new Intl.DateTimeFormat("en-GB", { timeZone: tz, ...o }).format(now);
      real.textContent = r({ hour: "2-digit", minute: "2-digit", hourCycle: "h23" });
      $("[data-real-date]", simEl).textContent = r({ weekday: "short", day: "numeric", month: "short" }).replace(/,/g, "");
    }
  }
  function simSync(c) {
    if (!simEl) return;
    simEl.hidden = !c;
    if (c) { sim = c; simT0 = Date.now(); simTick(); }
  }
  if (simEl) { simTick(); setInterval(simTick, 1000); }

  // SIMEX Calendar (ops/calendar.py poll_info): the drawer's Calendar badge, and a toast with the alert
  // sound for an event about to start, once per event (and change) on this device.
  const CAL_SEEN = "simex-cal-reminded";
  function calSync(c) {
    $$('[data-app="calendar"] .sx-app-ic, [data-app="viewed_cal"] .sx-app-ic').forEach((ic) => {
      let b = $(".sx-app-n", ic);
      if (!b) { b = document.createElement("span"); b.className = "sx-app-n"; ic.appendChild(b); }
      b.hidden = !c.badge;
      b.textContent = c.badge || "";
      b.setAttribute("aria-label", `${c.invites} invitation${c.invites === 1 ? "" : "s"} to answer, ${c.badge - c.invites} event${c.badge - c.invites === 1 ? "" : "s"} starting soon`);
    });
    let seen = [];
    try { seen = JSON.parse(localStorage.getItem(CAL_SEEN) || "[]"); } catch (e) { seen = []; }
    const fresh = (c.soon || []).filter((s) => !seen.includes(s.key));
    if (!fresh.length) return;
    localStorage.setItem(CAL_SEEN, JSON.stringify(seen.concat(fresh.map((s) => s.key)).slice(-200)));
    const s = fresh[0];
    snack(`${s.title}${s.body ? " · " + s.body : ""}`, { label: "Open", run: () => { location.href = s.url; } }, true);
    if (!CFG.readonly) N.alert({ title: s.title, body: s.body, url: `${location.origin}${s.url}`, tag: `cal-${s.key}`,
                                 flashText: s.title, desktop: !pushing() });
  }
  // The side bar's Upcoming: shown or folded, this browser's choice.
  const up = $("[data-upcoming]");
  if (up) {
    const UP = "simex-upcoming-off", list = $("[data-upcoming-list]", up), btn = $("[data-upcoming-toggle]", up);
    const paint = () => { const off = localStorage.getItem(UP) === "1"; list.hidden = off; btn.setAttribute("aria-expanded", off ? "false" : "true"); up.classList.toggle("off", off); };
    btn.addEventListener("click", () => { localStorage.setItem(UP, localStorage.getItem(UP) === "1" ? "0" : "1"); paint(); });
    paint();
  }

  const seenKey = `simex-latest:${BASE}`;
  let latest = localStorage.getItem(seenKey) === null ? null : Number(localStorage.getItem(seenKey));
  let pollSignedOut = false;
  async function poll() {
    try {
      const r = await fetch(latest === null ? `${BASE}/poll/` : `${BASE}/poll/?since=${latest}`, { credentials: "same-origin", headers: { "X-Requested-With": "fetch" } });
      if (signedOut(r)) { if (!pollSignedOut) signInAgain(); pollSignedOut = true; return; }
      pollSignedOut = false;
      if (!r.ok) return;
      const j = await r.json();
      if ("clock" in j) simSync(j.clock);
      const n = j.unread;
      $$("[data-unread-count]").forEach((el) => el.textContent = n ? n : "");
      const inboxLink = $('[data-folder="inbox"]'); if (inboxLink) inboxLink.classList.toggle("bold", n > 0);
      if (j.me_unread !== undefined) {  // a member's tab bar: the team inbox and their own mailbox
        $$("[data-tab-me]").forEach((el) => el.textContent = j.me_unread && !BASE.endsWith("/me") ? j.me_unread : "");
        $$("[data-tab-teambox]").forEach((el) => el.textContent = j.team_unread && BASE.endsWith("/me") ? j.team_unread : "");
      }
      if (j.dn) dn.update(j.dn);  // the persona desk's bell
      if (j.cal) calSync(j.cal);  // SIMEX Calendar: events about to start, invitations to answer
      if (j.desk_unread !== undefined) {  // the persona desk's tab bar
        $$("[data-tab-desk]").forEach((el) => el.textContent = j.desk_unread && !BASE.startsWith("/inbox/desk") ? j.desk_unread : "");
      }
            if (j.mail_unread !== undefined) {  // a team leader's tab bar
        $$("[data-tab-mail]").forEach((el) => el.textContent = j.mail_unread ? j.mail_unread : "");
        $$("[data-tab-team]").forEach((el) => el.textContent = j.team_new && !BASE.endsWith("/team") ? j.team_new : "");
      }
      N.title(n);
      N.favicon(n, "#0b57d0");
      const badge = j.mail_unread !== undefined ? j.mail_unread : n;  // a leader's badge: their own mail
      if (alertsHere && navigator.setAppBadge) (badge ? navigator.setAppBadge(badge) : navigator.clearAppBadge()).catch(() => {});
      const fresh = latest !== null ? (j.new || []).filter((m) => m.id > latest) : [];
      if (fresh.length) {
        const m = fresh[0];
        const more = fresh.length > 1 ? ` (+${fresh.length - 1} more)` : "";
        const url = m.url || `${BASE}/t/${m.thread}/`;  // mail to a member personally opens in My mail
        snack(`New message from ${m.from}: ${m.subject}${more}`, { label: "Open", run: () => { location.href = url; } }, true);
        if (!CFG.readonly) {
          N.alert({ title: `${m.from}${more}`, body: m.subject, url: `${location.origin}${url}`, tag: `mail-${m.id}`,
                    flashText: `New message from ${m.from}`, desktop: !pushing() });
        }
        if (!m.other && CFG.folder === "inbox" && bulk && !$$('input[name="threads"]:checked').length && (!win || win.hidden)) {
          setTimeout(() => {  // show the new row; the alert comes back after the reload
            if ($$('input[name="threads"]:checked').length || (win && !win.hidden) || $("[data-composer] [data-rte]:focus")) return;
            sessionStorage.setItem("simex-snack-new", JSON.stringify({ text: `New message from ${m.from}: ${m.subject}${more}`, url }));
            location.reload();
          }, 4000);
        }
      }
      latest = latest !== null && j.latest < latest ? j.latest : Math.max(latest ?? 0, j.latest);  // lower: the data was restored
      localStorage.setItem(seenKey, String(latest));
    } catch (e) { /* offline: try again next time */ }
  }
  poll();
  setInterval(poll, 10000);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) poll(); });

  const fresh = sessionStorage.getItem("simex-snack-new");
  if (fresh) {
    sessionStorage.removeItem("simex-snack-new");
    const f = JSON.parse(fresh);
    snack(f.text, { label: "Open", run: () => { location.href = f.url; } }, true);
  }
  const pending = sessionStorage.getItem("simex-snack");
  if (pending) { sessionStorage.removeItem("simex-snack"); snack(pending); }
})();
