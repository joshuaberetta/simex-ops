// SIMEX Messenger (templates/ops/chat/app.html, ops/views_messenger.py): the room list, one room's
// posts, the composer, and a poll of the server's change feed by cursor (a 204 when nothing changed):
// every 2 s with a room open and the page in view, 6 s otherwise, 30 s hidden; a push wakes it.
// Text is never inserted as HTML: posts are built with textContent and a few safe inline formats.
(function () {
  "use strict";
  const CFG = JSON.parse(document.getElementById("mx-config").textContent);
  const BASE = CFG.base;
  const $ = (s, el) => (el || document).querySelector(s);
  const $$ = (s, el) => Array.from((el || document).querySelectorAll(s));
  const csrf = () => (document.cookie.match(/(?:^|;\s*)csrftoken=([^;]+)/) || [])[1] || "";
  const H = { "X-CSRFToken": csrf(), "X-Requested-With": "fetch" };

  async function get(url) {
    const r = await fetch(url, { headers: H, credentials: "same-origin" });
    if (r.status === 204) return null;
    if (r.status === 401) { location.href = "/login/?next=" + encodeURIComponent(location.pathname); return null; }
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(j.error || "Something went wrong.");
    return j;
  }
  async function post(url, data) {
    let body = data;
    if (!(data instanceof FormData)) {
      body = new FormData();
      for (const [k, v] of Object.entries(data || {})) {
        if (Array.isArray(v)) v.forEach((x) => body.append(k, x)); else if (v !== undefined && v !== null) body.append(k, v);
      }
    }
    const r = await fetch(url, { method: "POST", body, headers: { ...H, "X-CSRFToken": csrf() }, credentials: "same-origin" });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(j.error || "Something went wrong.");
    return j;
  }
  const snack = (text) => {
    const s = $("[data-snack]");
    s.textContent = text; s.hidden = false;
    clearTimeout(snack.t); snack.t = setTimeout(() => { s.hidden = true; }, 4000);
  };
  const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text !== undefined) e.textContent = text; return e; };
  const initial = (name) => ((name || "?").replace(/^[^A-Za-z0-9]+/, "")[0] || "?").toUpperCase();
  const avatar = (name, colour, small) => { const a = el("span", "mx-av" + (small ? " sm" : ""), initial(name)); a.style.background = colour || "#5f6368"; return a; };
  const size = (n) => n > 1048576 ? (n / 1048576).toFixed(1) + " MB" : n > 1024 ? Math.round(n / 1024) + " KB" : n + " B";

  // *bold*, _italic_, `code`, links and @mentions, from text (never HTML).
  function rich(parent, text, mentions) {
    const re = /(https?:\/\/[^\s<>"]+)|(\*[^*\n]+\*)|(_[^_\n]+_)|(`[^`\n]+`)|(@\[[mlpt]:[^\]]+\])/g;
    let at = 0, m;
    while ((m = re.exec(text))) {
      if (m.index > at) parent.appendChild(document.createTextNode(text.slice(at, m.index)));
      const t = m[0];
      if (m[1]) { const a = el("a", "", t); a.href = t; a.target = "_blank"; a.rel = "noopener noreferrer"; parent.appendChild(a); }
      else if (m[2]) parent.appendChild(el("b", "", t.slice(1, -1)));
      else if (m[3]) parent.appendChild(el("i", "", t.slice(1, -1)));
      else if (m[4]) parent.appendChild(el("code", "", t.slice(1, -1)));
      else { const key = t.slice(2, -1); parent.appendChild(el("span", "mx-at", "@" + (names[key] || key))); }
      at = m.index + t.length;
    }
    if (at < text.length) parent.appendChild(document.createTextNode(text.slice(at)));
    return parent;
  }

  // --- state ----------------------------------------------------------------------------------
  let rooms = [], cursor = 0, open = null, openRoom = null, posts = new Map(), more = false, loadingOlder = false;
  let replyTo = null, editing = null, files = [], names = {}, members = [];
  const listEl = $("[data-rooms]"), postsEl = $("[data-posts]"), roomEl = $("[data-room]"), placeholder = $("[data-placeholder]");
  const body = document.body;

  // --- the room list --------------------------------------------------------------------------
  function paintRooms() {
    const q = ($("[data-search]").value || "").trim().toLowerCase();
    listEl.textContent = "";
    const shown = rooms.filter((r) => !q || r.name.toLowerCase().includes(q) || (r.last && r.last.body.toLowerCase().includes(q)));
    if (!shown.length) { listEl.appendChild(el("li", "mx-empty", q ? "No chat matches." : CFG.readonly ? "No chats yet." : "No chats yet: start one with the pencil.")); return; }
    let section = null;
    for (const r of shown) {
      if (CFG.desk && r.section !== section) {
        section = r.section;
        listEl.appendChild(el("li", "mx-sec " + section, CFG.sections[section] || section));
      }
      const li = el("li", "mx-rrow" + (r.id === open ? " on" : "") + (r.unread ? " unread" : ""));
      const a = el("a"); a.href = `${BASE}/r/${r.id}/`; a.dataset.id = r.id;
      const av = avatar(r.kind === "group" ? r.name : r.name, r.colour);
      if (r.kind === "group") av.classList.add("group");
      if (r.kind === "team") av.classList.add("team");
      a.appendChild(av);
      const mid = el("span", "mx-rmid");
      const top = el("span", "mx-rtop");
      top.appendChild(el("b", "", r.name));
      if (r.last) top.appendChild(el("small", "", r.last.t === undefined ? "" : (isToday(r.last) ? r.last.t : r.last.day)));
      mid.appendChild(top);
      const line = el("span", "mx-rlast");
      if (r.last) line.textContent = (r.last.system ? "" : r.last.name ? r.last.name + ": " : "") + r.last.body;
      else line.textContent = r.kind === "team" ? "Your whole team" : "";
      mid.appendChild(line);
      a.appendChild(mid);
      const side = el("span", "mx-rside");
      if (r.pinned) side.appendChild(el("span", "mx-pin", "📌"));
      if (r.muted) side.appendChild(el("span", "mx-muted-ic", "🔕"));
      if (r.frozen) side.appendChild(el("span", "mx-muted-ic", "🔒"));
      if (CFG.desk && r.npc) side.appendChild(el("span", "mx-owner", "AI: " + r.npc));
      if (CFG.desk && r.owner) side.appendChild(el("span", "mx-owner" + (r.mine ? " mine" : ""), r.mine ? "You" : r.owner));
      if (CFG.desk && r.section === "attention" && r.since) {
        const mins = Math.floor((Date.now() - r.since) / 60000);
        side.appendChild(el("span", "mx-wait" + (mins < 5 ? " warn" : ""), mins < 1 ? "now" : mins + " min"));
      }
      if (r.unread) side.appendChild(el("span", "mx-badge", String(r.unread)));
      a.appendChild(side);
      a.onclick = (e) => { e.preventDefault(); openChat(r.id, true); };
      li.appendChild(a);
      listEl.appendChild(li);
    }
  }
  const today = () => new Date().toDateString();
  const isToday = (x) => new Date(x.ms).toDateString() === today() || CFG.simOnly;
  function mergeRooms(list) {
    const by = new Map(rooms.map((r) => [r.id, r]));
    for (const r of list) by.set(r.id, r);
    const order = Object.keys(CFG.sections || {});
    rooms = Array.from(by.values()).sort((a, b) => CFG.desk
      ? (order.indexOf(a.section) - order.indexOf(b.section)) ||
        (a.section === "attention" ? (a.since || 0) - (b.since || 0) : (b.last ? b.last.id : 0) - (a.last ? a.last.id : 0))
      : (b.pinned - a.pinned) || ((b.last ? b.last.id : 0) - (a.last ? a.last.id : 0)));
    paintRooms();
    badge();
  }
  function badge() {
    const n = CFG.desk ? rooms.filter((r) => r.section === "attention").length : rooms.filter((r) => r.unread && !r.muted).length;
    document.title = (n ? `(${n}) ` : "") + document.title.replace(/^\(\d+\) /, "");
    if (navigator.setAppBadge && !CFG.readonly) { try { n ? navigator.setAppBadge(n) : navigator.clearAppBadge(); } catch (e) { /* none */ } }
  }
  $("[data-search]").addEventListener("input", paintRooms);

  // --- one room -------------------------------------------------------------------------------
  async function openChat(id, push) {
    open = id;
    if (push) history.pushState({ room: id }, "", `${BASE}/r/${id}/`);
    body.classList.add("mx-in-room");
    roomEl.hidden = false; placeholder.hidden = true;
    postsEl.textContent = ""; posts = new Map(); replyTo = null; cancelEdit(); paintReply();
    paintRooms();
    let j;
    try { j = await get(`${BASE}/r/${id}/posts/`); } catch (e) { snack(e.message); return; }
    if (!j || open !== id) return;
    openRoom = j.room; members = j.room.members; more = j.more;
    names = Object.fromEntries(members.map((m) => [m.key, m.name]));
    const head = $(".mx-room-head");
    const av = $("[data-room-av]", head);
    const r = rooms.find((x) => x.id === id) || {};
    av.textContent = initial(j.room.name); av.style.background = r.colour || "#5f6368";
    $("[data-room-name]").textContent = j.room.name;
    const live = members.filter((m) => !m.left);
    $("[data-room-sub]").textContent = CFG.desk ? (j.room.personas || []).map((p) => p.name).join(", ") + " · " + live.filter((m) => !m.key.startsWith("p:")).map((m) => m.name).join(", ")
      : j.room.kind === "direct" ? (live.find((m) => m.key !== CFG.me.key) || {}).org || "Direct chat"
      : j.room.kind === "team" ? "Everyone in the team and its leader" : live.length + " members";
    const form = $("[data-compose]");
    if (form) {
      $("[data-frozen]").hidden = !j.room.frozen;
      $(".mx-row", form).hidden = !j.room.canPost;
    }
    if (CFG.desk) paintDesk(j.room);
    for (const p of j.posts) addPost(p);
    scrollEnd(true);
    markRead();
    if (form && matchMedia("(pointer: fine)").matches) $("[data-text]").focus();
  }
  function paintDesk(room) {
    const sel = $("[data-as]");
    if (sel) {
      sel.textContent = "";
      for (const p of room.personas || []) { const o = el("option", "", p.name); o.value = p.local; sel.appendChild(o); }
      if (room.addressed) sel.value = room.addressed;
      sel.hidden = !(room.personas || []).length;
    }
    const st = $("[data-deskstate]");
    const what = { attention: "A team is waiting for an answer", chatter: "The team wrote, nobody asked a persona", later: "Later",
                   rest: "Answered or nothing to answer" }[room.section] || "";
    st.textContent = what + (room.owner ? ` · ${room.mine ? "yours" : room.owner + "'s"}` : " · nobody has it") +
      (room.npc ? ` · AI persona answering (${room.npc.who || ""})` : "");
    $('[data-desk-act="take"]').hidden = room.mine;
    $('[data-desk-act="release"]').hidden = !room.mine;
  }
  $$("[data-desk-act]").forEach((b) => {
    b.onclick = async () => {
      try { await post(`${BASE}/r/${open}/mark/`, { act: b.dataset.deskAct }); snack({ take: "It's yours.", release: "Released.", done: "Marked: no reply needed.", watch: "You follow this chat." }[b.dataset.deskAct]); }
      catch (e) { snack(e.message); return; }
      await refreshRooms(); openChat(open, false);
    };
  });
  function closeChat() {
    open = null; openRoom = null;
    body.classList.remove("mx-in-room");
    roomEl.hidden = true; placeholder.hidden = false;
    paintRooms();
  }
  $("[data-back]").onclick = () => { history.pushState({}, "", `${BASE}/`); closeChat(); };
  window.addEventListener("popstate", () => {
    const m = location.pathname.match(/\/r\/(\d+)\/$/);
    if (m) openChat(Number(m[1]), false); else closeChat();
  });

  function dayOf(p) { return p.day; }
  function addPost(p, atTop) {
    const old = posts.get(p.id);
    const node = renderPost(p);
    posts.set(p.id, { data: p, node });
    if (old) { old.node.replaceWith(node); return; }
    if (atTop) {
      postsEl.insertBefore(node, postsEl.firstChild);
    } else {
      const last = Array.from(posts.values()).map((x) => x.data).filter((x) => x.id < p.id).pop();
      if (!last || dayOf(last) !== dayOf(p)) postsEl.appendChild(el("div", "mx-day", p.day));
      postsEl.appendChild(node);
    }
  }
  function renderPost(p) {
    if (p.kind === "system") { const s = el("div", "mx-sys", p.body); s.id = "p" + p.id; return s; }
    const w = el("div", "mx-post" + (p.mine ? " mine" : "") + (p.deleted ? " deleted" : "") + (p.npc ? " npc" : "") +
                 (p.by && p.by.startsWith("p:") ? " persona" : ""));
    w.id = "p" + p.id;
    w.dataset.id = p.id;
    const prev = Array.from(posts.values()).map((x) => x.data).filter((x) => x.id < p.id && x.kind !== "system").pop();
    const grouped = prev && prev.by === p.by && dayOf(prev) === p.day && p.ms - prev.ms < 5 * 60000;
    if (!p.mine && !grouped) w.appendChild(avatar(p.name, p.colour, true)); else if (!p.mine) w.appendChild(el("span", "mx-av-space"));
    const b = el("div", "mx-bubble");
    if ((!p.mine || CFG.desk) && !grouped && openRoom && (openRoom.kind !== "direct" || CFG.desk)) { const n = el("div", "mx-pname", p.name); n.style.color = p.colour; b.appendChild(n); }
    if (p.reply) {
      const q = el("button", "mx-quote"); q.type = "button";
      q.appendChild(el("b", "", p.reply.name)); q.appendChild(el("span", "", p.reply.body));
      q.onclick = () => { const t = document.getElementById("p" + p.reply.id); if (t) { t.scrollIntoView({ block: "center" }); t.classList.add("flash"); setTimeout(() => t.classList.remove("flash"), 1200); } };
      b.appendChild(q);
    }
    if (p.deleted && !p.body) b.appendChild(el("div", "mx-text mx-gone", "This message was deleted"));
    else {
      if (p.deleted) b.appendChild(el("div", "mx-gone-tag", `Deleted by ${p.deleted_by || "its author"}${p.deleted_at ? " · " + p.deleted_at : ""}`));
      if (p.body) b.appendChild(rich(el("div", "mx-text"), p.body, p.mentions));
      for (const f of p.files || []) {
        if (f.image) {
          const a = el("a", "mx-img"); a.href = f.url + "?inline=1"; a.target = "_blank"; a.rel = "noopener";
          const img = el("img"); img.src = f.url + "?inline=1"; img.alt = f.name; img.loading = "lazy";
          a.appendChild(img); b.appendChild(a);
        } else {
          const a = el("a", "mx-file"); a.href = f.url; a.setAttribute("download", "");
          a.appendChild(el("span", "mx-file-ic", (f.name.split(".").pop() || "file").slice(0, 4).toUpperCase()));
          const t = el("span"); t.appendChild(el("b", "", f.name)); t.appendChild(el("small", "", size(f.size)));
          a.appendChild(t); b.appendChild(a);
        }
      }
      for (const e of p.edits || []) b.appendChild(el("div", "mx-edit-was", `Before the edit (${e.at}): ${e.body}`));
    }
    const meta = el("div", "mx-meta");
    if (p.npc) meta.appendChild(el("span", "mx-ai", "AI persona"));
    if (p.edited && !p.deleted) meta.appendChild(el("span", "", "edited"));
    meta.appendChild(el("span", "", p.t));
    if (p.sim) { const s = el("span", "mx-sim", "◷ " + p.sim); s.title = "Scenario time"; meta.appendChild(s); }
    if (p.mine && !p.pending) meta.appendChild(el("span", "mx-tick" + (p.seen ? " seen" : ""), p.seen ? "✓✓" : "✓"));
    if (p.pending) meta.appendChild(el("span", "mx-tick", p.failed ? "!" : "🕓"));
    b.appendChild(meta);
    const reacts = Object.entries(p.reactions || {});
    if (reacts.length) {
      const r = el("div", "mx-reacts");
      for (const [emo, who] of reacts) { const c = el("span", "mx-react" + (emo === p.my_reaction ? " mine" : ""), `${emo} ${who.length}`); c.title = who.join(", "); r.appendChild(c); }
      b.appendChild(r);
    }
    w.appendChild(b);
    b.addEventListener("click", (e) => {  // a phone: tap a message for reply, react, edit, delete
      if (e.target.closest("a, button") || !matchMedia("(max-width: 760px)").matches) return;
      $$(".mx-post.tools-on").forEach((x) => { if (x !== w) x.classList.remove("tools-on"); });
      w.classList.toggle("tools-on");
    });
    if (!CFG.readonly && !p.deleted && !p.pending && openRoom && openRoom.canPost) {
      const tools = el("div", "mx-tools");
      const btn = (label, title, fn) => { const x = el("button", "mx-tool", label); x.type = "button"; x.title = title; x.setAttribute("aria-label", title); x.onclick = fn; tools.appendChild(x); };
      btn("↩", "Reply", () => { replyTo = p; paintReply(); $("[data-text]").focus(); });
      if (!CFG.desk) {
        btn("☺", "React", () => reactMenu(p, tools));
        if (p.mine && Date.now() - p.ms < CFG.editMinutes * 60000) btn("✎", "Edit", () => startEdit(p));
        if (p.mine) btn("🗑", "Delete for everyone", () => del(p));
      }
      w.appendChild(tools);
    }
    return w;
  }
  function reactMenu(p, anchor) {
    const old = $(".mx-emoji"); if (old) old.remove();
    const m = el("div", "mx-emoji");
    for (const e of CFG.emoji) {
      const x = el("button", "", e); x.type = "button";
      x.onclick = async () => { m.remove(); try { const j = await post(`${BASE}/p/${p.id}/react/`, { emoji: e }); addPost(j.post); } catch (err) { snack(err.message); } };
      m.appendChild(x);
    }
    anchor.appendChild(m);
    setTimeout(() => document.addEventListener("click", function off(ev) { if (!m.contains(ev.target)) { m.remove(); document.removeEventListener("click", off); } }), 0);
  }
  async function del(p) {
    if (!confirm("Delete this message for everyone? It says “This message was deleted” instead (ExCon can still read it).")) return;
    try { const j = await post(`${BASE}/p/${p.id}/delete/`, {}); addPost(j.post); } catch (e) { snack(e.message); }
  }
  function startEdit(p) {
    editing = p; replyTo = null; paintReply();
    $("[data-editing]").hidden = false;
    const t = $("[data-text]"); t.value = p.body; t.focus(); grow();
  }
  function cancelEdit() {
    editing = null;
    const e = $("[data-editing]"); if (e) e.hidden = true;
  }
  function scrollEnd(force) {
    const near = postsEl.scrollHeight - postsEl.scrollTop - postsEl.clientHeight < 160;
    if (force || near) postsEl.scrollTop = postsEl.scrollHeight;
  }
  postsEl.addEventListener("scroll", async () => {
    if (postsEl.scrollTop < 80 && more && !loadingOlder && open) {
      loadingOlder = true;
      const first = Math.min(...posts.keys());
      const h = postsEl.scrollHeight;
      try {
        const j = await get(`${BASE}/r/${open}/posts/?before=${first}`);
        more = j.more;
        for (const p of j.posts.slice().reverse()) addPost(p, true);
        postsEl.scrollTop = postsEl.scrollHeight - h;
      } catch (e) { /* next scroll */ }
      loadingOlder = false;
    }
    if (postsEl.scrollHeight - postsEl.scrollTop - postsEl.clientHeight < 40) markRead();
  });
  let readSent = 0;
  function markRead() {
    if (CFG.readonly || CFG.desk || !open || document.hidden) return;
    const top = Math.max(0, ...posts.keys());
    if (top <= readSent && readSent) return;
    readSent = top;
    post(`${BASE}/r/${open}/read/`, { upto: top }).catch(() => {});
    const r = rooms.find((x) => x.id === open);
    if (r && r.unread) { r.unread = 0; paintRooms(); badge(); }
  }

  // --- composing ------------------------------------------------------------------------------
  const form = $("[data-compose]");
  const text = $("[data-text]");
  function grow() { if (!text) return; text.style.height = "auto"; text.style.height = Math.min(text.scrollHeight, 160) + "px"; }
  function paintReply() {
    const r = $("[data-replying]"); if (!r) return;
    r.hidden = !replyTo;
    if (replyTo) { $("[data-replying-name]").textContent = replyTo.name || "You"; $("[data-replying-text]").textContent = " " + (replyTo.body || "").slice(0, 100); }
  }
  function paintFiles() {
    const a = $("[data-attached]"); a.textContent = "";
    files.forEach((f, i) => {
      const c = el("span", "mx-chip", `📎 ${f.name} (${size(f.size)})`);
      const x = el("button", "", "×"); x.type = "button"; x.setAttribute("aria-label", "Remove " + f.name);
      x.onclick = () => { files.splice(i, 1); paintFiles(); };
      c.appendChild(x); a.appendChild(c);
    });
  }
  // The outbox: a message is kept here (and in localStorage) until the server has it, under its
  // client key, so a flaky phone connection never loses or doubles one.
  const OUTBOX = "simex-chat-outbox";
  const outbox = () => { try { return JSON.parse(localStorage.getItem(OUTBOX) || "[]"); } catch (e) { return []; } };
  const saveOutbox = (o) => localStorage.setItem(OUTBOX, JSON.stringify(o.slice(-50)));
  async function deliver(item, fileList) {
    const fd = new FormData();
    fd.append("body", item.body); fd.append("client_key", item.key);
    if (item.reply) fd.append("reply_to", item.reply);
    (item.mentions || []).forEach((k) => fd.append("mentions", k));
    if (item.persona) fd.append("persona", item.persona);
    (fileList || []).forEach((f) => fd.append("files", f, f.name));
    const j = await post(`${BASE}/r/${item.room}/send/`, fd);
    saveOutbox(outbox().filter((x) => x.key !== item.key));
    return j.post;
  }
  async function flushOutbox() {
    for (const item of outbox()) {
      try {
        const p = await deliver(item);
        if (item.room === open) { const tmp = document.querySelector(`[data-key="${item.key}"]`); if (tmp) tmp.remove(); addPost(p); }
      } catch (e) {
        if (/not in this chat|frozen|Write something/.test(e.message)) saveOutbox(outbox().filter((x) => x.key !== item.key));
        break;
      }
    }
  }
  if (form) {
    text.addEventListener("input", () => { grow(); typingPing(); mentionMenu(); });
    text.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.shiftKey && matchMedia("(pointer: fine)").matches) { e.preventDefault(); form.requestSubmit(); }
      if (e.key === "Escape") { replyTo = null; paintReply(); cancelEdit(); }
    });
    $("[data-file]").addEventListener("change", (e) => {
      const max = CFG.maxMb * 1048576;
      for (const f of e.target.files) files.push(f);
      if (files.reduce((n, f) => n + f.size, 0) > max) { snack(`Files can be ${CFG.maxMb} MB in all.`); files = files.slice(0, -e.target.files.length); }
      e.target.value = ""; paintFiles();
    });
    $("[data-reply-cancel]").onclick = () => { replyTo = null; paintReply(); };
    $("[data-edit-cancel]").onclick = () => { cancelEdit(); text.value = ""; grow(); };
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const bodyText = text.value.trim();
      if (!open || (!bodyText && !files.length)) return;
      if (editing) {
        try { const j = await post(`${BASE}/p/${editing.id}/edit/`, { body: bodyText }); addPost(j.post); } catch (err) { snack(err.message); return; }
        cancelEdit(); text.value = ""; grow(); return;
      }
      const item = { key: (crypto.randomUUID ? crypto.randomUUID() : String(Math.random()).slice(2)).replace(/-/g, "").slice(0, 32),
                     room: open, body: bodyText, reply: replyTo ? replyTo.id : null, persona: CFG.desk ? ($("[data-as]") || {}).value : undefined, mentions: pickedMentions.filter((k) => bodyText.includes("@[" + k + "]")) };
      const sending = files.slice();
      const tmp = renderPost({ id: 0, kind: "text", by: CFG.desk ? "p:" + item.persona : CFG.me.key, name: CFG.me.name, mine: true, pending: true, body: bodyText,
                               files: sending.map((f) => ({ name: f.name, size: f.size })), t: "", day: "", ms: Date.now(), reactions: {} });
      tmp.dataset.key = item.key; postsEl.appendChild(tmp); scrollEnd(true);
      text.value = ""; grow(); files = []; paintFiles(); replyTo = null; paintReply(); pickedMentions = [];
      if (!sending.length) saveOutbox(outbox().concat([item]));
      try {
        const p = await deliver(item, sending);
        tmp.remove(); addPost(p); scrollEnd(true);
        if (CFG.desk && openRoom) { openRoom.section = "rest"; paintDesk(openRoom); refreshRooms(); }
      } catch (err) {
        const t = $(".mx-tick", tmp); if (t) t.textContent = "!";
        snack(sending.length ? err.message : "Not sent yet: it goes when the connection is back.");
        if (/not in this chat|frozen/.test(err.message)) saveOutbox(outbox().filter((x) => x.key !== item.key));
      }
    });
  }
  let lastTyping = 0;
  function typingPing() {
    if (!open || Date.now() - lastTyping < 4000) return;
    lastTyping = Date.now();
    post(`${BASE}/r/${open}/typing/`, {}).catch(() => {});
  }
  // @mentions: typing "@" offers the room's members; picked, the text holds @[key] (shown as @Name).
  let pickedMentions = [];
  function mentionMenu() {
    const box = $("[data-mention]");
    const m = text.value.slice(0, text.selectionStart).match(/@([\w .-]{0,30})$/);
    if (!m || !openRoom || openRoom.kind === "direct") { box.hidden = true; return; }
    const q = m[1].toLowerCase();
    const opts = members.filter((x) => !x.left && x.key !== CFG.me.key && !x.key.startsWith("t:") && x.name.toLowerCase().includes(q)).slice(0, 6);
    box.textContent = "";
    if (!opts.length) { box.hidden = true; return; }
    for (const o of opts) {
      const b = el("button", "", o.name); b.type = "button";
      b.onclick = () => {
        const before = text.value.slice(0, text.selectionStart).replace(/@([\w .-]{0,30})$/, `@[${o.key}] `);
        text.value = before + text.value.slice(text.selectionStart);
        pickedMentions.push(o.key); names[o.key] = o.name; box.hidden = true; text.focus();
      };
      box.appendChild(b);
    }
    box.hidden = false;
  }

  // --- new chat, chat info -------------------------------------------------------------------
  const newDialog = $("[data-new-dialog]");
  let bookList = [], picked = new Set();
  if ($("[data-new]")) $("[data-new]").onclick = async () => {
    picked = new Set(); paintPicked();
    $("[data-new-err]").hidden = true;
    newDialog.showModal();
    try {
      const j = await get(`${BASE}/book/`);
      bookList = j.book;
      const as = $("[data-new-as]");
      if (as) { as.textContent = ""; for (const p of j.personas || []) { const o = el("option", "", p.name + (p.org ? " · " + p.org : "")); o.value = p.local; as.appendChild(o); } }
    } catch (e) { bookList = []; }
    paintBook();
  };
  function paintBook() {
    const q = ($("[data-book-search]").value || "").toLowerCase();
    const ul = $("[data-book]"); ul.textContent = "";
    const kinds = CFG.desk ? { team: "Teams (their shared chat with the persona)", member: "Members", tl: "Team leaders" }
      : { member: "Your team", leader: "Your team leader", persona: "People in the exercise", team: "Other teams" };
    let last = "";
    for (const b of bookList.filter((x) => !q || x.name.toLowerCase().includes(q) || (x.org || "").toLowerCase().includes(q))) {
      if (b.kind !== last) { ul.appendChild(el("li", "mx-book-h", kinds[b.kind] || "")); last = b.kind; }
      const li = el("li");
      const lab = el("label", "mx-bk");
      const cb = el("input"); cb.type = "checkbox"; cb.checked = picked.has(b.key);
      cb.onchange = () => {
        if (CFG.desk) { picked = new Set(cb.checked ? [b.key] : []); $$("input[type=checkbox]", ul).forEach((x) => { if (x !== cb) x.checked = false; }); }
        else cb.checked ? picked.add(b.key) : picked.delete(b.key);
        paintPicked();
      };
      lab.appendChild(cb);
      lab.appendChild(avatar(b.name, b.kind === "persona" ? "#0f7b5f" : "#5f6368", true));
      const t = el("span"); t.appendChild(el("b", "", b.name)); if (b.org) t.appendChild(el("small", "", b.org));
      lab.appendChild(t); li.appendChild(lab); ul.appendChild(li);
    }
    if (!ul.children.length) ul.appendChild(el("li", "mx-empty", "Nobody matches."));
  }
  function paintPicked() {
    const n = picked.size;
    $("[data-group-name]").hidden = n < 2 || !CFG.canGroup;
    $("[data-picked]").textContent = n > 1 ? (CFG.canGroup ? `${n} people: a group` : "Pick one person (members can't start groups)") : "";
    $("[data-new-ok]").disabled = !n || (n > 1 && !CFG.canGroup);
    $("[data-new-ok]").textContent = n > 1 ? "Create group" : "Chat";
  }
  $("[data-book-search]").addEventListener("input", paintBook);
  $("[data-new-form]").addEventListener("submit", async (e) => {
    e.preventDefault();
    const title = picked.size > 1 ? $("[name=title]", newDialog).value : "";
    try {
      const j = await post(`${BASE}/new/`, CFG.desk ? { key: Array.from(picked)[0], persona: $("[data-new-as]").value }
                                                : { keys: Array.from(picked), title });
      newDialog.close();
      await refreshRooms();
      openChat(j.id, true);
    } catch (err) { const x = $("[data-new-err]"); x.textContent = err.message; x.hidden = false; }
  });
  $$("[data-dialog-cancel]").forEach((b) => { b.onclick = (e) => { e.preventDefault(); b.closest("dialog").close(); }; });

  const infoDialog = $("[data-info-dialog]");
  $$("[data-info]").forEach((b) => { b.onclick = () => openInfo(); });
  function openInfo() {
    if (!openRoom) return;
    $("[data-info-title]").textContent = openRoom.name;
    $("[data-info-err]").hidden = true;
    const ul = $("[data-members]"); ul.textContent = "";
    for (const m of members.filter((x) => !x.left || CFG.readonly)) {
      const li = el("li");
      li.appendChild(avatar(m.name, m.colour, true));
      const t = el("span"); t.appendChild(el("b", "", m.name + (m.key === CFG.me.key ? " (you)" : "")));
      const sub = [m.org, m.admin ? "admin" : "", m.key.startsWith("t:") ? "every member of the team" : "", m.left ? "left" : ""].filter(Boolean).join(" · ");
      if (sub) t.appendChild(el("small", "", sub));
      li.appendChild(t);
      if (openRoom.admin && openRoom.kind === "group" && m.key !== CFG.me.key && !m.left) {
        const x = el("button", "mx-pill sm", "Remove"); x.type = "button";
        x.onclick = () => roomAct("remove", { keys: [m.key] });
        li.appendChild(x);
      }
      ul.appendChild(li);
    }
    const acts = $("[data-info-actions]"); acts.textContent = "";
    const btn = (label, fn) => { const b = el("button", "mx-pill", label); b.type = "button"; b.onclick = fn; acts.appendChild(b); };
    if (!CFG.readonly) {
      btn(openRoom.muted ? "Unmute" : "Mute", () => roomAct(openRoom.muted ? "unmute" : "mute"));
      btn(openRoom.pinned ? "Unpin" : "Pin to the top", () => roomAct(openRoom.pinned ? "unpin" : "pin"));
      if (openRoom.kind === "group" && openRoom.admin && openRoom.canPost) {
        btn("Rename", () => { const t = prompt("Group name", openRoom.name); if (t) roomAct("rename", { title: t }); });
        btn("Add people", async () => {
          infoDialog.close();
          picked = new Set(); paintPicked();
          try { bookList = (await get(`${BASE}/book/`)).book.filter((b) => !members.some((m) => m.key === b.key && !m.left)); } catch (e) { bookList = []; }
          newDialog.showModal(); paintBook();
          $("[data-new-ok]").textContent = "Add";
          $("[data-group-name]").hidden = true;
          newDialog.dataset.adding = "1";
        });
      }
      if (openRoom.kind === "group" && openRoom.canPost) btn("Leave the group", () => { if (confirm("Leave this group?")) roomAct("leave"); });
    }
    infoDialog.showModal();
  }
  $("[data-new-form]").addEventListener("submit", async (e) => {
    if (newDialog.dataset.adding !== "1") return;
    e.stopImmediatePropagation();
  }, true);
  $("[data-new-ok]").addEventListener("click", async (e) => {
    if (newDialog.dataset.adding !== "1") return;
    e.preventDefault();
    delete newDialog.dataset.adding;
    newDialog.close();
    await roomAct("add", { keys: Array.from(picked) }, true);
  });
  newDialog.addEventListener("close", () => { delete newDialog.dataset.adding; });
  async function roomAct(act, extra, quiet) {
    try {
      await post(`${BASE}/r/${open}/settings/`, { act, ...(extra || {}) });
      if (infoDialog.open) infoDialog.close();
      if (act === "leave") { closeChat(); rooms = rooms.filter((r) => r.id !== open); }
      await refreshRooms();
      if (open) openChat(open, false);
    } catch (err) {
      if (quiet) snack(err.message); else { const x = $("[data-info-err]"); x.textContent = err.message; x.hidden = false; }
    }
  }

  // --- the poll ------------------------------------------------------------------------------
  async function refreshRooms() {
    const j = await get(`${BASE}/rooms/`);
    if (!j) return;
    rooms = []; mergeRooms(j.rooms);
    if (!cursor) cursor = j.c;
  }
  let polling = false, timer = null;
  async function poll() {
    if (polling) return;
    polling = true;
    clearTimeout(timer);
    try {
      const j = await get(`${BASE.replace(/\/$/, "")}/poll/?c=${cursor}${open ? "&r=" + open : ""}`);
      if (j) {
        cursor = j.c;
        if (j.rooms && j.rooms.length) mergeRooms(j.rooms);
        for (const p of j.posts || []) {
          const pending = document.querySelector(".mx-post[data-key]");
          if (p.mine && pending && !posts.has(p.id)) continue;  // our own, on its way back from send
          const isNew = !posts.has(p.id);
          addPost(p);
          if (isNew) { scrollEnd(false); if (!p.mine) chime(); }
        }
        if (j.seen) for (const [id, n] of Object.entries(j.seen)) { const x = posts.get(Number(id)); if (x && x.data.mine && x.data.seen !== n) { x.data.seen = n; addPost(x.data); } }
        $("[data-typing]").textContent = (j.typing || []).length ? `${j.typing.join(", ")} ${j.typing.length > 1 ? "are" : "is"} typing…` : "";
        if (open && (j.posts || []).length) markRead();
      } else if ($("[data-typing]").textContent) {
        $("[data-typing]").textContent = "";
      }
    } catch (e) { /* offline: try again later */ }
    polling = false;
    const gap = document.hidden ? 30000 : open ? 2000 : 6000;
    timer = setTimeout(poll, gap);
  }
  document.addEventListener("visibilitychange", () => { if (!document.hidden) { poll(); markRead(); flushOutbox(); } });
  window.addEventListener("online", () => { flushOutbox(); poll(); });
  let ctx = null;
  function chime() {
    if (!document.hidden || CFG.readonly) return;
    try {
      ctx = ctx || new (window.AudioContext || window.webkitAudioContext)();
      const o = ctx.createOscillator(), g = ctx.createGain();
      o.frequency.value = 880; g.gain.value = 0.05; o.connect(g); g.connect(ctx.destination);
      o.start(); o.stop(ctx.currentTime + 0.12);
    } catch (e) { /* no sound */ }
  }

  // --- alerts (Web Push, Messenger's own service worker and subscription) ---------------------
  const pushable = !CFG.readonly && !!CFG.vapid && "serviceWorker" in navigator && "PushManager" in window;
  const swReady = "serviceWorker" in navigator ? navigator.serviceWorker.register(CFG.sw, { scope: "/chat/" }).catch(() => null) : Promise.resolve(null);
  const b64 = (s) => Uint8Array.from(atob(s.replace(/-/g, "+").replace(/_/g, "/") + "===".slice((s.length + 3) % 4)), (c) => c.charCodeAt(0));
  async function pushOn() {
    const reg = await swReady;
    if (!reg || !pushable || Notification.permission !== "granted") return false;
    try {
      const key = b64(CFG.vapid);
      let sub = await reg.pushManager.getSubscription();
      sub = sub || await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: key });
      const j = sub.toJSON();
      await post(`/chat/push/`, { endpoint: j.endpoint, p256dh: j.keys.p256dh, auth: j.keys.auth });
      return true;
    } catch (e) { return false; }
  }
  const bar = $("[data-alerts-bar]");
  const ios = /iPad|iPhone|iPod/.test(navigator.userAgent) || (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);
  const standalone = navigator.standalone || matchMedia("(display-mode: standalone)").matches;
  function paintAlerts() {
    if (CFG.readonly || !CFG.vapid) { bar.hidden = true; $("[data-alerts]").hidden = true; return; }
    if (ios && !standalone) {
      $("[data-alerts-text]").textContent = "Tap Share, then Add to Home Screen, and open Messenger from your home screen: it is an app of its own, beside SIMEX Mail.";
      $("[data-alerts-on]").hidden = true;
      bar.hidden = localStorage.getItem("simex-chat-install") === "1";
      return;
    }
    bar.hidden = !("Notification" in window) || Notification.permission !== "default" || localStorage.getItem("simex-chat-alerts") === "later";
  }
  $("[data-alerts-on]").onclick = async () => {
    const p = await Notification.requestPermission();
    if (p === "granted") { await pushOn(); snack("Alerts are on for this device."); }
    paintAlerts();
  };
  $("[data-alerts-later]").onclick = () => { localStorage.setItem(ios && !standalone ? "simex-chat-install" : "simex-chat-alerts", ios && !standalone ? "1" : "later"); bar.hidden = true; };
  $("[data-alerts]").onclick = async () => {
    if (!("Notification" in window)) { snack("This browser can't show alerts."); return; }
    if (Notification.permission === "granted") { snack((await pushOn()) ? "Alerts are on for this device." : "Alerts need the home-screen app on this device."); return; }
    localStorage.removeItem("simex-chat-alerts"); paintAlerts();
  };
  if (pushable) {
    pushOn();
    navigator.serviceWorker.addEventListener("message", (e) => { if (e.data && e.data.type === "simex-push") poll(); });
    const out = $("form.mx-signout");
    if (out) out.addEventListener("submit", (e) => {  // a shared phone must not show the last person's chats
      if (out.dataset.done) return;
      e.preventDefault(); out.dataset.done = "1";
      const off = (async () => { const reg = await swReady; const sub = reg && await reg.pushManager.getSubscription();
        if (sub) { await post(`/chat/push/`, { endpoint: sub.endpoint, off: "1" }).catch(() => {}); await sub.unsubscribe(); } })();
      Promise.race([off.catch(() => {}), new Promise((r) => setTimeout(r, 2000))]).then(() => out.submit());
    });
  }
  paintAlerts();

  // The first time: Messenger is monitored, said plainly.
  if (!CFG.readonly && !CFG.desk && localStorage.getItem("simex-chat-notice") !== "1") {
    const d = $("[data-notice-dialog]");
    d.addEventListener("close", () => localStorage.setItem("simex-chat-notice", "1"));
    d.showModal();
  }

  // --- start ---------------------------------------------------------------------------------
  refreshRooms().then(() => {
    if (CFG.room) openChat(CFG.room, false);
    flushOutbox();
    poll();
  }).catch((e) => snack(e.message));
})();
