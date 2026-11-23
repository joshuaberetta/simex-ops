/* The persona desk's Assign tab (ops/desk_board.py): the board, where a conversation is dragged
   onto someone to give it to them (posted to /inbox/desk/mark/ like Assign to...), and the
   Default assignments grid, saved cell by cell. Pointer events (mouse, pen and touch), no library;
   every card also has a "Move to" menu for keyboards and small screens. */
(function () {
  "use strict";
  const $ = (s, el) => (el || document).querySelector(s);
  const $$ = (s, el) => Array.from((el || document).querySelectorAll(s));
  const csrf = () => (document.cookie.match(/(?:^|;\s*)csrftoken=([^;]+)/) || [])[1] || ($('[name="csrfmiddlewaretoken"]') || {}).value || "";
  const post = (url, data) => {
    const body = new URLSearchParams();
    Object.entries(data).forEach(([k, v]) => [].concat(v).forEach(x => body.append(k, x)));
    return fetch(url, { method: "POST", body, headers: { "X-CSRFToken": csrf(), "X-Requested-With": "fetch" }, credentials: "same-origin" })
      .then(r => r.json().catch(() => ({ ok: false, error: "The server didn't answer." })).then(j => (r.ok ? j : Object.assign({ ok: false }, j))));
  };
  const esc = s => String(s == null ? "" : s).replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const json = id => { const el = document.getElementById(id); return el ? JSON.parse(el.textContent) : null; };

  /* ---------------- the board ---------------- */
  const root = $("[data-board]");
  if (root) board(root);

  function board(root) {
    let data = json("simex-board"), v = json("simex-board-v");
    let skew = data.now - Date.now();  // the server's clock against this one, for the waits
    const cols = $("[data-cols]", root), err = $("[data-board-err]", root), selInfo = $("[data-sel]", root);
    const excon = $("[data-show-excon]", root);
    const selected = new Set();
    let dragging = null, busy = 0;
    excon.checked = localStorage.getItem("simex-board-excon") === "1";
    excon.addEventListener("change", () => { localStorage.setItem("simex-board-excon", excon.checked ? "1" : "0"); render(); });
    $$("select", $("[data-board-filters]", root)).forEach(s => s.addEventListener("change", () => s.form.submit()));

    const mins = ms => Math.max(0, Math.round((Date.now() + skew - ms) / 60000));
    const showErr = msg => { err.textContent = msg || ""; err.hidden = !msg; };

    function state(c) {
      if (c.section === "attention") return c.waiting ? `<b class="w">waiting ${mins(c.since)} min</b>` : `<b class="w">follow-up due</b>`;
      if (c.section === "quiet") return "probably no reply needed";
      if (c.section === "later") return c.follow ? `follow up at ${esc(c.follow.at)}` : "AI persona answering";
      return c.done_by ? `no reply needed (${esc(c.done_by)})` : "answered";
    }

    function moveMenu(c) {
      const opts = data.columns.filter(col => !col.gone).map(col =>
        `<option value="${col.uid}"${col.uid === c.owner ? " selected" : ""}>${col.uid ? esc(col.name) + (col.desk ? "" : " (ExCon)") : "Not assigned"}</option>`).join("");
      return `<select class="gm-ab-move" data-move aria-label="Move ${esc(c.subject)} to">${opts}</select>`;
    }

    function card(c) {
      const p = c.persona;
      const chips = [c.team ? `<span class="gm-label gm-teamchip">${esc(c.team)}</span>` : "",
        p ? `<span class="gm-label gm-persona" style="background:${esc(p.colour)};color:${esc(p.fg)}">${esc(p.name)}</span>` : "",
        c.urgent ? `<span class="gm-label gm-st-urgent">Urgent</span>` : "",
        c.npc === "held" || c.npc === "failed" ? `<span class="gm-label gm-st-urgent">AI answer ${esc(c.npc)}</span>` : "",
        c.auto ? `<span class="gm-label gm-ab-auto" title="Given by the default assignments">by default</span>` : ""].join("");
      const here = c.here.length ? `<div class="gm-ab-here">${c.here.map(h => esc(h.name) + (h.replying ? " is replying" : " has it open")).join(", ")}</div>` : "";
      return `<div class="gm-ab-card s-${esc(c.section)}${selected.has(c.thread) ? " sel" : ""}" data-thread="${esc(c.thread)}" tabindex="0" role="listitem"
        style="border-left-color:${esc(p ? p.colour : "#c4c7c5")}">
        <div class="gm-ab-chips"><span class="gm-ab-grip" data-grip title="Drag to someone" aria-hidden="true"></span>${chips}</div>
        <a class="gm-ab-subj" href="${esc(c.url)}">${esc(c.subject)}</a>
        <div class="gm-ab-meta">${state(c)} · ${c.count} message${c.count === 1 ? "" : "s"} · ${esc(c.latest)}</div>
        ${c.ask ? `<div class="gm-ab-ask">${esc(c.ask)}</div>` : ""}${here}
        <div class="gm-ab-foot">${moveMenu(c)}</div></div>`;
    }

    function shown(col) {
      return col.uid === 0 || col.desk || col.n || excon.checked;
    }

    function render() {
      cols.innerHTML = data.columns.filter(shown).map(col => {
        const head = col.uid === 0 ? "Not assigned" : esc(col.name) + (col.me ? " <span class=\"muted\">(you)</span>" : "")
          + (col.desk ? "" : " <span class=\"muted\">(ExCon)</span>") + (col.gone ? " <span class=\"muted\">(no longer on the desk)</span>" : "");
        const dfl = col.defaults.length ? `<div class="gm-ab-dfl" title="Their personas by default">${col.defaults.map(d =>
          `<span class="gm-label gm-persona" style="background:${esc(d.colour)};color:${esc(d.fg)}" title="${esc(d.name)}: ${esc(d.teams)}">${esc(d.name)}</span>`).join("")}</div>` : "";
        return `<section class="gm-ab-col${col.uid === 0 ? " nobody" : ""}${col.gone ? " gone" : ""}" data-uid="${col.uid}" aria-label="${col.uid ? esc(col.name) : "Not assigned"}">
          <header><div class="gm-ab-name">${col.uid && col.online ? "<i class=\"gm-ab-on\" title=\"Online\"></i>" : ""}${head}</div>
            <div class="gm-ab-n"><b>${col.n}</b> · ${col.attention ? `<b class="w">${col.attention} need attention</b>` : "none need attention"}${col.oldest ? ` · longest ${mins(col.oldest)} min` : ""}</div>${dfl}</header>
          <div class="gm-ab-list" role="list">${col.cards.map(card).join("") || "<div class=\"gm-ab-empty\">Nothing here</div>"}</div></section>`;
      }).join("");
      selInfo.hidden = !selected.size;
      selInfo.textContent = selected.size ? `${selected.size} selected (Esc to clear)` : "";
    }

    function findCard(thread) {
      for (const col of data.columns) {
        const c = col.cards.find(x => x.thread === thread);
        if (c) return c;
      }
      return null;
    }

    function move(threads, uid) {
      threads = threads.filter(t => { const c = findCard(t); return c && c.owner !== uid; });
      if (!threads.length) return;
      const to = data.columns.find(c => c.uid === uid);
      if (!to || to.gone) return;
      // Straight away on the board; the server's board follows.
      threads.forEach(t => {
        data.columns.forEach(col => {
          const i = col.cards.findIndex(x => x.thread === t);
          if (i >= 0) {
            const [c] = col.cards.splice(i, 1);
            c.owner = uid; c.auto = false;
            to.cards.unshift(c);
          }
        });
      });
      data.columns.forEach(col => { col.n = col.cards.length; col.attention = col.cards.filter(c => c.section === "attention").length; });
      selected.clear();
      render();
      showErr("");
      busy++;
      post("/inbox/desk/mark/", uid ? { threads, act: "assign", assignee: uid } : { threads, act: "release" })
        .then(j => { if (!j.ok) showErr(j.error || "Not moved."); })
        .catch(() => showErr("Not moved: no connection."))
        .finally(() => { busy--; v = ""; refresh(); });
    }

    cols.addEventListener("change", e => {
      const s = e.target.closest("[data-move]");
      if (!s) return;
      const t = s.closest("[data-thread]").dataset.thread;
      move(selected.has(t) ? [...selected] : [t], Number(s.value));
    });

    cols.addEventListener("keydown", e => {
      const c = e.target.closest && e.target.closest(".gm-ab-card");
      if (c && e.target === c && (e.key === " " || e.key === "Enter")) {
        e.preventDefault();
        if (e.key === "Enter") location.href = $(".gm-ab-subj", c).href;
        else toggle(c.dataset.thread);
      }
    });
    document.addEventListener("keydown", e => { if (e.key === "Escape" && selected.size) { selected.clear(); render(); } });

    function toggle(t) {
      selected.has(t) ? selected.delete(t) : selected.add(t);
      render();
      const el = cols.querySelector(`[data-thread="${CSS.escape(t)}"]`);
      if (el) el.focus();
    }

    // Dragging: press on a card (not its link or menu), move a few pixels, drop on a column.
    cols.addEventListener("pointerdown", e => {
      const c = e.target.closest(".gm-ab-card");
      if (!c || e.button > 0 || e.target.closest("a, select")) return;
      if (e.pointerType !== "mouse" && !e.target.closest("[data-grip]")) return;  // touch: by the grip, the rest scrolls
      dragging = { el: c, thread: c.dataset.thread, x: e.clientX, y: e.clientY, on: false, ghost: null, over: null, id: e.pointerId };
      c.setPointerCapture(e.pointerId);
    });
    cols.addEventListener("pointermove", e => {
      const d = dragging;
      if (!d || e.pointerId !== d.id) return;
      if (!d.on) {
        if (Math.abs(e.clientX - d.x) + Math.abs(e.clientY - d.y) < 6) return;
        d.on = true;
        d.threads = selected.has(d.thread) ? [...selected] : [d.thread];
        d.ghost = d.el.cloneNode(true);
        d.ghost.classList.add("gm-ab-ghost");
        d.ghost.style.width = d.el.offsetWidth + "px";
        if (d.threads.length > 1) d.ghost.insertAdjacentHTML("beforeend", `<span class="gm-ab-count">${d.threads.length}</span>`);
        document.body.appendChild(d.ghost);
        d.el.classList.add("dragging");
        document.body.classList.add("gm-ab-dragging");
      }
      e.preventDefault();
      d.ghost.style.transform = `translate(${e.clientX + 8}px, ${e.clientY + 8}px)`;
      const under = document.elementFromPoint(e.clientX, e.clientY);
      const col = under && under.closest(".gm-ab-col:not(.gone)");
      if (d.over !== col) {
        if (d.over) d.over.classList.remove("over");
        if (col) col.classList.add("over");
        d.over = col;
      }
      // Near an edge of the columns: scroll them along.
      const r = cols.getBoundingClientRect();
      if (e.clientX > r.right - 40) cols.scrollLeft += 16;
      else if (e.clientX < r.left + 40) cols.scrollLeft -= 16;
    });
    const end = e => {
      const d = dragging;
      if (!d || e.pointerId !== d.id) return;
      dragging = null;
      if (!d.on) {  // a click: select it, or not
        if (e.type === "pointerup") toggle(d.thread);
        return;
      }
      d.ghost.remove();
      d.el.classList.remove("dragging");
      document.body.classList.remove("gm-ab-dragging");
      if (d.over) d.over.classList.remove("over");
      if (e.type === "pointerup" && d.over) move(d.threads, Number(d.over.dataset.uid));
    };
    cols.addEventListener("pointerup", end);
    cols.addEventListener("pointercancel", end);

    // Kept up to date: the board again whenever it changed.
    function refresh() {
      if (dragging || busy || document.hidden) return;
      const q = new URLSearchParams(location.search);
      q.set("v", v || "");
      fetch("/inbox/desk/assign/board/?" + q, { credentials: "same-origin", headers: { "X-Requested-With": "fetch" } })
        .then(r => (r.ok ? r.json() : null)).then(j => {
          if (!j || dragging || busy) return;
          v = j.v;
          if (j.board) {
            data = j.board;
            skew = data.now - Date.now();
            const have = new Set(data.columns.flatMap(c => c.cards.map(x => x.thread)));
            [...selected].forEach(t => have.has(t) || selected.delete(t));
            render();
          }
        }).catch(() => {});
    }
    setInterval(refresh, 10000);
    document.addEventListener("visibilitychange", refresh);
    render();
  }

  /* ---------------- default assignments ---------------- */
  const grid = $("[data-defaults]");
  if (grid) defaults(grid);

  function defaults(root) {
    const err = $("[data-grid-err]", root), count = $("[data-unassigned]", root), apply = $("[data-apply]", root);
    const showErr = msg => { err.textContent = msg || ""; err.hidden = !msg; };
    $$("[data-cell]", root).forEach(s => { s.dataset.was = s.value; });

    function inherit(row) {
      const all = $('[data-cell][data-team=""]', row).value;
      $$('[data-cell]:not([data-team=""])', row).forEach(s => {
        $("[data-inherit]", s).textContent = all ? "as all teams" : "Nobody";
        s.closest("td").classList.toggle("inh", !s.value);
      });
    }

    root.addEventListener("change", e => {
      const s = e.target;
      const row = s.closest("[data-sender]");
      if (!row) return;
      if (s.matches("[data-cell]")) {
        s.disabled = true;
        post("/inbox/desk/assign/defaults/set/", { sender: row.dataset.sender, team: s.dataset.team, user: s.value })
          .then(j => {
            if (!j.ok) throw new Error(j.error || "Not saved.");
            s.dataset.was = s.value;
            count.textContent = j.unassigned;
            apply.disabled = !j.unassigned;
            showErr("");
          })
          .catch(x => { s.value = s.dataset.was; showErr(x.message || "Not saved: no connection."); })
          .finally(() => { s.disabled = false; inherit(row); });
      } else if (s.matches("[data-row]") && s.value) {
        const clear = s.value === "clear";
        post("/inbox/desk/assign/defaults/set/", clear ? { act: "clear", sender: row.dataset.sender }
          : { act: "row", sender: row.dataset.sender, user: s.value })
          .then(j => { if (!j.ok) throw new Error(j.error || "Not saved."); location.reload(); })
          .catch(x => { s.value = ""; showErr(x.message || "Not saved: no connection."); });
      }
    });
  }
})();
