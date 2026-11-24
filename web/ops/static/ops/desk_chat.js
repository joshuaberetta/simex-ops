// ChatSIMEX (ops/desk_chat.py, views_desk_chat.py): the persona desk's chat with SIMEX AI, a drawer
// on every desk page. Each question is a turn written in the background: the drawer polls it,
// showing what SIMEX AI is looking up, then the answer with its sources, a "who is" briefing card,
// a draft reply (checked against the team's timeline) and what to hold back. Chats are the
// login's own. Nothing here sends anything.
(() => {
  const box = document.querySelector("[data-chat]");
  const toggle = document.querySelector("[data-chat-toggle]");
  if (!box || !toggle) return;
  const $ = (s) => box.querySelector(s);
  const base = "/inbox/desk/chat/";
  const log = $("[data-cx-log]"), list = $("[data-cx-list]"), form = $("[data-cx-form]"), q = $("[data-cx-q]");
  const go = $("[data-cx-go]"), teamSel = $("[data-cx-team]");
  const pageThread = box.dataset.thread || "", pageTeam = box.dataset.team || "";
  const csrf = () => (form.querySelector("[name=csrfmiddlewaretoken]") || {}).value || "";
  const state = { chat: null, pinned: pageThread, persona: "", personaName: "", teams: [], loaded: false, timer: null };
  const KEY = "cx-chat", OPEN = "cx-open";

  const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };
  const get = async (url) => { const r = await fetch(url, { headers: { "x-requested-with": "fetch" }, credentials: "same-origin" }); return [r, await r.json().catch(() => ({}))]; };
  const post = async (url, data) => {
    const fd = new FormData(); Object.entries(data).forEach(([k, v]) => fd.append(k, v == null ? "" : v));
    fd.append("csrfmiddlewaretoken", csrf());
    const r = await fetch(url, { method: "POST", body: fd, headers: { "x-requested-with": "fetch", "X-CSRFToken": csrf() }, credentials: "same-origin" });
    return [r, await r.json().catch(() => ({}))];
  };

  // --- open / close ------------------------------------------------------------------------------
  const open = (yes) => {
    box.hidden = !yes; toggle.setAttribute("aria-expanded", yes ? "true" : "false"); toggle.classList.toggle("on", yes);
    document.body.classList.toggle("cx-open", yes);
    try { localStorage.setItem(OPEN, yes ? "1" : ""); } catch (e) { /* private mode */ }
    if (yes) { load(); setTimeout(() => q.focus(), 50); }
  };
  toggle.addEventListener("click", () => open(box.hidden));
  $("[data-cx-close]").addEventListener("click", () => open(false));
  window.simexChat = (question, opts) => { // Brief me and others: open with a question
    open(true);
    if (opts && opts.persona) setPersona(opts.persona, opts.name || opts.persona);
    if (question) { q.value = question; if (opts && opts.ask) form.requestSubmit(); }
  };

  // --- scope -------------------------------------------------------------------------------------
  const pin = $("[data-cx-pin]"), per = $("[data-cx-persona]");
  const showScope = () => {
    pin.hidden = !state.pinned;
    per.hidden = !state.persona; $("[data-cx-persona-label]").textContent = state.personaName || state.persona;
  };
  const setPersona = (local, name) => { state.persona = local || ""; state.personaName = name || local || ""; showScope(); };
  $("[data-cx-unpin]").addEventListener("click", () => { state.pinned = ""; showScope(); });
  $("[data-cx-unpersona]").addEventListener("click", () => setPersona("", ""));
  const scope = () => ({ team: teamSel.value, thread: state.pinned, persona: state.persona });

  // --- rendering ---------------------------------------------------------------------------------
  const REF = /\b(doc|inject|rp|mail|thread|persona):([\w.@+-]+[\w@])/g;
  const withRefs = (text) => {
    const p = el("div", "cx-text");
    String(text || "").split(/\n{2,}/).forEach((para) => {
      const node = el("p");
      let last = 0;
      para.replace(REF, (m, kind, key, at) => {
        node.append(document.createTextNode(para.slice(last, at)));
        node.append(refChip(kind, key));
        last = at + m.length;
        return m;
      });
      node.append(document.createTextNode(para.slice(last)));
      p.append(node);
    });
    return p;
  };
  const refChip = (kind, key, label) => {
    const text = label || `${kind}:${key}`;
    if (kind === "thread") { const a = el("a", "cx-ref thread", text); a.href = `/inbox/desk/t/${encodeURIComponent(key)}/`; return a; }
    if (kind === "persona") {
      const b = el("button", "cx-ref persona", text); b.type = "button"; b.title = "Ask about this persona";
      b.onclick = () => setPersona(key, key); return b;
    }
    return el("span", `cx-ref ${kind}`, text);
  };
  const srcChip = (src) => { const s = String(src || ""), i = s.indexOf(":"); return i > 0 ? refChip(s.slice(0, i), s.slice(i + 1)) : el("span", "cx-ref", s); };
  const section = (title, items, fmt, cls) => {
    if (!items || !items.length) return null;
    const s = el("div", `cx-sec ${cls || ""}`); s.append(el("h4", null, title));
    const ul = el("ul"); items.forEach((x) => { const li = el("li"); fmt ? fmt(li, x) : li.append(withRefs(x)); ul.append(li); });
    s.append(ul); return s;
  };

  const briefCard = (b) => {
    const c = el("div", "cx-brief");
    const h = el("div", "cx-brief-head");
    h.append(el("b", null, b.name || b.persona || "?"));
    if (b.organisation || b.role) h.append(el("span", null, [b.role, b.organisation].filter(Boolean).join(", ")));
    c.append(h);
    if (b.persona) c.append(refChip("persona", b.persona));
    [["How to play them", b.how_to_play], ["What they know", b.knows]].forEach(([t, v]) => {
      if (v) { const s = el("div", "cx-sec"); s.append(el("h4", null, t)); s.append(withRefs(v)); c.append(s); }
    });
    [section("Said so far", b.said), section("Coming (NOT YET: hold back)", b.coming, null, "hold"),
      section("Open now", b.open), section("Other matches", b.other_matches)].forEach((x) => x && c.append(x));
    if (b.persona) {
      const ask = el("button", "cx-btn", "Ask about them"); ask.type = "button";
      ask.onclick = () => { setPersona(b.persona, b.name || b.persona); q.focus(); };
      c.append(ask);
    }
    return c;
  };

  const draftBox = (d) => {
    const c = el("div", "cx-draft");
    c.append(el("h4", null, `Draft for ${d.team || "?"}${d.persona ? ` as ${d.persona}` : ""}`));
    if (d.warnings && d.warnings.length) c.append(section("Check before sending", d.warnings, (li, w) => li.append(document.createTextNode(w)), "warn"));
    c.append(el("pre", "cx-body", d.body || ""));
    const acts = el("div", "cx-acts");
    if (d.thread && d.thread === pageThread && window.simexReplyWith) {
      const use = el("button", "cx-btn primary", "Use in reply"); use.type = "button";
      use.onclick = () => window.simexReplyWith(String(d.mail || ""), d.body || "");
      acts.append(use);
    } else if (d.thread) {
      const a = el("a", "cx-btn", "Open the conversation"); a.href = `/inbox/desk/t/${encodeURIComponent(d.thread)}/`; acts.append(a);
    }
    const copy = el("button", "cx-btn", "Copy"); copy.type = "button";
    copy.onclick = async () => { try { await navigator.clipboard.writeText(d.body || ""); copy.textContent = "Copied"; } catch (e) { copy.textContent = "Copy failed"; } };
    acts.append(copy); c.append(acts);
    const facts = section(`What it rests on (${(d.facts || []).length})`, d.facts, (li, f) => {
      li.className = `cx-fact ${f.state}`; li.append(document.createTextNode(`${f.fact} `)); li.append(el("small", null, `${f.source}: ${f.label}`));
    });
    if (facts) { const det = el("details", "cx-sec"); det.append(el("summary", null, facts.querySelector("h4").textContent)); det.append(facts.querySelector("ul")); c.append(det); }
    return c;
  };

  const TOOL = { search: "searching the material for", search_mail: "searching the mail for", conversations: "listing the conversations",
    who: "looking up", read: "reading", roster: "reading the roster", personas: "listing the personas" };
  const turnEl = (t) => {
    const w = el("div", "cx-turn"); w.dataset.turn = t.id;
    const qq = el("div", "cx-q"); qq.append(el("p", null, t.question));
    if (t.scope_label) qq.append(el("small", null, t.scope_label));
    w.append(qq);
    const a = el("div", "cx-a");
    if (t.status === "running") {
      a.append(el("p", "cx-wait", "SIMEX AI is looking things up…"));
      const ul = el("ul", "cx-trace");
      (t.trace || []).forEach((x) => ul.append(el("li", null, `${TOOL[x.tool] || x.tool} ${x.q ? `“${x.q}”` : ""}${x.where && x.where !== "all" && x.where !== "desk" ? ` (${x.where})` : ""}`)));
      a.append(ul);
    } else if (t.status === "failed") {
      a.append(el("p", "cx-err", `No answer: ${t.error}`));
    } else {
      const b = t.body || {};
      a.append(withRefs(t.answer));
      if (b.brief) a.append(briefCard(b.brief));
      if (b.draft) a.append(draftBox(b.draft));
      [section("Don't get ahead of", b.hold_back, (li, h) => { li.append(el("b", null, h.what)); li.append(document.createTextNode(` · ${h.when || ""} `)); li.append(srcChip(h.source)); }, "hold"),
        section("Not in the package", b.not_available), section("To decide", b.questions)].forEach((x) => x && a.append(x));
      if (b.sources && b.sources.length) {
        const det = el("details", "cx-sec"); det.append(el("summary", null, `Sources (${b.sources.length})`));
        const ul = el("ul"); b.sources.forEach((s) => { const li = el("li"); li.append(srcChip(s.ref)); li.append(document.createTextNode(` ${s.what || ""}`)); ul.append(li); });
        det.append(ul); a.append(det);
      }
      a.append(el("small", "cx-at", t.at));
    }
    w.append(a);
    return w;
  };

  const starters = () => {
    const s = el("div", "cx-start");
    s.append(el("p", null, "Ask about anything on the desk. SIMEX AI searches the desk's and the teams' mail, the injects, the role plays and the Library, and says where each team stands."));
    ["Who is … ?", "What has been said about … ?", "What is coming for this team?", pageThread ? "How should I answer this?" : "How should I answer Team … about … ?"]
      .forEach((x) => { const b = el("button", "cx-btn", x); b.type = "button"; b.onclick = () => { q.value = x.replace(" … ?", " ").replace(" …", " "); q.focus(); }; s.append(b); });
    return s;
  };

  const render = () => {
    log.replaceChildren();
    const turns = state.chat ? state.chat.turns || [] : [];
    if (!turns.length) log.append(starters());
    turns.forEach((t) => log.append(turnEl(t)));
    log.scrollTop = log.scrollHeight;
    const running = turns.find((t) => t.status === "running");
    go.disabled = !!running || !!state.off;
    clearTimeout(state.timer);
    if (running) state.timer = setTimeout(poll, 2500, running.id);
  };

  async function poll(id) {
    try {
      const [r, j] = await get(`${base}turn/${id}/`);
      if (!r.ok || !state.chat) return;
      const i = state.chat.turns.findIndex((t) => t.id === id);
      if (i >= 0) state.chat.turns[i] = j.turn;
      render();
    } catch (e) { state.timer = setTimeout(poll, 5000, id); }
  }

  // --- chats ------------------------------------------------------------------------------------
  async function load() {
    if (state.loaded) return;
    state.loaded = true;
    const [r, j] = await get(base);
    if (!r.ok) return;
    state.teams = j.teams || [];
    state.off = j.off || "";
    state.teams.forEach((t) => { const o = el("option", null, t.name + (t.test ? " (test)" : "")); o.value = t.id; teamSel.append(o); });
    if (pageTeam) teamSel.value = pageTeam;
    if (state.off) form.before(el("p", "cx-err cx-off", state.off));
    let id = null;
    try { id = sessionStorage.getItem(KEY); } catch (e) { /* private mode */ }
    if (id && !pageThread) { await openChat(id); } else render();
    showScope();
  }

  async function openChat(id) {
    const [r, j] = await get(`${base}${id}/`);
    if (!r.ok) { state.chat = null; render(); return; }
    state.chat = j.chat;
    if (!pageThread) {
      state.pinned = j.chat.thread || "";
      teamSel.value = j.chat.team || pageTeam || "";
      setPersona(j.chat.persona || "", j.chat.persona || "");
    }
    try { sessionStorage.setItem(KEY, j.chat.id); } catch (e) { /* private mode */ }
    list.hidden = true; render(); showScope();
  }

  $("[data-cx-new]").addEventListener("click", () => {
    state.chat = null; state.pinned = pageThread; setPersona("", ""); teamSel.value = pageTeam;
    try { sessionStorage.removeItem(KEY); } catch (e) { /* private mode */ }
    list.hidden = true; render(); showScope(); q.focus();
  });

  $("[data-cx-history]").addEventListener("click", async () => {
    if (!list.hidden) { list.hidden = true; return; }
    const [r, j] = await get(base);
    if (!r.ok) return;
    list.replaceChildren();
    if (!(j.chats || []).length) list.append(el("p", "cx-muted", "No chats yet."));
    (j.chats || []).forEach((c) => {
      const row = el("div", "cx-row");
      const b = el("button", "cx-row-open"); b.type = "button";
      b.append(el("span", null, c.title)); b.append(el("small", null, c.at));
      b.onclick = () => openChat(c.id);
      const del = el("button", "gm-icon-btn sm", "✕"); del.type = "button"; del.title = "Delete this chat";
      del.onclick = async () => {
        await post(`${base}${c.id}/delete/`, {});
        row.remove();
        if (state.chat && state.chat.id === c.id) { state.chat = null; render(); }
      };
      row.append(b, del); list.append(row);
    });
    list.hidden = false;
  });

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const question = q.value.trim();
    if (!question || go.disabled) return;
    go.disabled = true;
    if (!state.chat) {
      const [r, j] = await post(base, scope());
      if (!r.ok) { go.disabled = false; log.append(el("p", "cx-err", j.error || "Could not start a chat.")); return; }
      state.chat = j.chat;
      try { sessionStorage.setItem(KEY, j.chat.id); } catch (err) { /* private mode */ }
    }
    const [r, j] = await post(`${base}${state.chat.id}/ask/`, { question, ...scope() });
    if (!r.ok) { go.disabled = false; log.append(el("p", "cx-err", j.error || "Could not ask.")); log.scrollTop = log.scrollHeight; return; }
    q.value = "";
    state.chat.turns = [...(state.chat.turns || []), j.turn];
    render();
  });
  q.addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); form.requestSubmit(); } });

  let wasOpen = "";
  try { wasOpen = localStorage.getItem(OPEN); } catch (e) { /* private mode */ }
  showScope();
  if (wasOpen) open(true);
})();
