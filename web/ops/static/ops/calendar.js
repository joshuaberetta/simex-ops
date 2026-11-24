/* SIMEX Calendar (ops/calendar.py, views_calendar.py): Day, Week, Month and Schedule views of
   the calendars of a mailbox, drawn here from <base>/calendar/events/. Times come as
   "YYYY-MM-DDTHH:MM" on the viewer's clock (a member with the simulation clock on: the
   scenario's time only) and are worked on as such, in minutes, never through the browser's
   time zone. Drag on the grid to create; drag your own events to move them, their bottom edge to
   change their length (pointer events: mouse, pen and touch; no library). Keys: T today, N / P
   next / previous, D W M A the views, C create, Escape closes. */
(function () {
  "use strict";
  const $ = (s, el) => (el || document).querySelector(s);
  const $$ = (s, el) => Array.from((el || document).querySelectorAll(s));
  const csrf = () => (document.cookie.match(/(?:^|;\s*)csrftoken=([^;]+)/) || [])[1] || ($('[name="csrfmiddlewaretoken"]') || {}).value || "";
  const post = (url, data) => {
    const body = new URLSearchParams();
    Object.entries(data).forEach(([k, v]) => body.append(k, v == null ? "" : v));
    return fetch(url, { method: "POST", body, headers: { "X-CSRFToken": csrf(), "X-Requested-With": "fetch" }, credentials: "same-origin" })
      .then(r => r.json().catch(() => ({ ok: false, error: "The server didn't answer." })).then(j => (r.ok ? j : Object.assign({ ok: false }, j))));
  };
  const esc = s => String(s == null ? "" : s).replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const json = id => { const el = document.getElementById(id); return el ? JSON.parse(el.textContent) : null; };

  const C = json("simex-cal");
  const root = $("[data-cal]");
  if (!C || !root) return;
  const bodyEl = $("[data-cal-body]"), titleEl = $("[data-cal-title]"), errEl = $("[data-cal-err]"), pop = $("[data-cal-pop]");
  const dlg = $("[data-cal-dialog]"), form = $("[data-cal-form]");
  const F = n => form.elements.namedItem(n);  // by name: form.id and form.title are the form's own
  const HOUR = 48, SNAP = 15, DAY_M = 1440;
  const MON = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  const LMON = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"];
  const DAY = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
  const LDAY = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"];

  /* ---------- naive times, in minutes ---------- */
  const toM = s => Date.UTC(+s.slice(0, 4), +s.slice(5, 7) - 1, +s.slice(8, 10), +(s.slice(11, 13) || 0), +(s.slice(14, 16) || 0)) / 60000;
  const fromM = m => new Date(Math.round(m) * 60000).toISOString().slice(0, 16);
  const dayOf = s => s.slice(0, 10);
  const addDays = (d, n) => fromM(toM(d) + n * DAY_M).slice(0, 10);
  const dow = d => new Date(toM(d) * 60000).getUTCDay();
  const monday = d => addDays(d, -((dow(d) + 6) % 7));
  const hm = s => s.slice(11, 16);
  const dnum = d => +d.slice(8, 10), mnum = d => +d.slice(5, 7) - 1, ynum = d => +d.slice(0, 4);
  const snap = m => Math.round(m / SNAP) * SNAP;
  const longDate = d => `${LDAY[dow(d)]} ${dnum(d)} ${LMON[mnum(d)]} ${ynum(d)}`;
  const shortDate = d => `${DAY[dow(d)]} ${dnum(d)} ${MON[mnum(d)]}`;

  let nowM = toM(C.now), syncedAt = Date.now(), rate = C.rate;
  const now = () => nowM + (Date.now() - syncedAt) / 60000 * rate;
  const today = () => fromM(Math.floor(now())).slice(0, 10);
  // For leaders, the desk and ExCon: a real time's scenario time (the clock's speed from now).
  const simM0 = C.simNow ? toM(C.simNow) : 0, realM0 = toM(C.now);
  const simOf = s => C.simNow ? fromM(simM0 + (toM(s) - realM0) * C.simRate) : "";

  /* ---------- state ---------- */
  const params = new URLSearchParams(location.search);
  const VIEWS = ["day", "week", "month", "schedule"];
  let view = VIEWS.includes(params.get("view")) ? params.get("view") : localStorage.getItem("simex-cal-view") || (innerWidth < 700 ? "schedule" : "week");
  if (!VIEWS.includes(view)) view = "week";
  let date = /^\d{4}-\d\d-\d\d$/.test(params.get("date") || "") ? params.get("date") : today();
  let mini = date.slice(0, 7) + "-01";
  const layerKey = "simex-cal-layers:" + C.base;
  const shownLayers = new Set((() => {
    try { const v = JSON.parse(localStorage.getItem(layerKey)); if (Array.isArray(v)) return v; } catch (e) { /* default */ }
    return C.layers.filter(l => l.on).map(l => l.key);
  })().filter(k => C.layers.some(l => l.key === k)));
  const layerOf = k => C.layers.find(l => l.key === k);
  let events = [], loadedRange = "", dragging = null, scrolled = false, busy = false;

  function range() {
    if (view === "day") return [date, addDays(date, 1)];
    if (view === "week") { const m = monday(date); return [m, addDays(m, 7)]; }
    if (view === "month") { const m = monday(date.slice(0, 8) + "01"); return [m, addDays(m, 42)]; }
    return [date, addDays(date, 30)];
  }

  function showErr(msg) { errEl.textContent = msg || ""; errEl.hidden = !msg; }

  function load(force) {
    const [a, b] = range();
    const key = a + b;
    if (!force && key === loadedRange) { render(); return Promise.resolve(); }
    return fetch(`${C.base}/calendar/events/?from=${a}&to=${b}`, { credentials: "same-origin" })
      .then(r => r.json()).then(j => {
        if (!j.ok) { showErr(j.error || "Couldn't load the calendar."); return; }
        showErr("");
        events = j.events; loadedRange = key;
        nowM = toM(j.now); rate = j.rate; syncedAt = Date.now();
        render();
      }).catch(() => showErr("Couldn't reach SIMEX Calendar. Retrying…"));
  }

  const visible = () => events.filter(e => shownLayers.has(e.layer));
  const onDay = (e, d) => toM(e.start) < toM(d) + DAY_M && toM(e.end) > toM(d);
  const isAllDayish = e => e.allDay || toM(e.end) - toM(e.start) >= DAY_M;

  /* ---------- render ---------- */
  function render() {
    $$("[data-cal-view]").forEach(b => { const on = b.dataset.calView === view; b.classList.toggle("on", on); b.setAttribute("aria-selected", on); });
    const url = new URL(location.href);
    url.searchParams.set("view", view); url.searchParams.set("date", date);
    history.replaceState(null, "", url);
    titleEl.textContent = title();
    if (view === "month") renderMonth(); else if (view === "schedule") renderSchedule(); else renderGrid(view === "day" ? [date] : [0, 1, 2, 3, 4, 5, 6].map(i => addDays(monday(date), i)));
    renderMini();
  }

  function title() {
    if (view === "day") return longDate(date);
    if (view === "month") return `${LMON[mnum(date)]} ${ynum(date)}`;
    if (view === "schedule") return `From ${dnum(date)} ${MON[mnum(date)]} ${ynum(date)}`;
    const a = monday(date), b = addDays(a, 6);
    if (mnum(a) === mnum(b)) return `${dnum(a)} – ${dnum(b)} ${MON[mnum(b)]} ${ynum(b)}`;
    return `${dnum(a)} ${MON[mnum(a)]}${ynum(a) !== ynum(b) ? " " + ynum(a) : ""} – ${dnum(b)} ${MON[mnum(b)]} ${ynum(b)}`;
  }

  function when(e) {
    const a = dayOf(e.start), endDay = hm(e.end) === "00:00" ? addDays(dayOf(e.end), -1) : dayOf(e.end);
    if (e.allDay) return a === endDay ? shortDate(a) : `${shortDate(a)} – ${shortDate(endDay)}`;
    if (a === dayOf(e.end) || (hm(e.end) === "00:00" && endDay === a)) return `${shortDate(a)}, ${hm(e.start)} – ${hm(e.end)}`;
    return `${shortDate(a)} ${hm(e.start)} – ${shortDate(dayOf(e.end))} ${hm(e.end)}`;
  }
  const simWhen = e => e.sim ? when({ start: e.sim.start, end: e.sim.end, allDay: e.allDay }) : "";
  const modeName = m => (C.modes.find(x => x[0] === m) || [m, m])[1];

  function lanes(list, d) {  // side-by-side columns for overlapping events in one day
    const d0 = toM(d), items = list.map(e => ({ e, a: Math.max(toM(e.start), d0), b: Math.min(toM(e.end), d0 + DAY_M) }))
      .sort((x, y) => x.a - y.a || y.b - x.b);
    let cluster = [], cEnd = -1;
    const out = [];
    const flush = () => {
      const cols = [];
      cluster.forEach(it => {
        let i = cols.findIndex(end => end <= it.a);
        if (i < 0) { i = cols.length; cols.push(0); }
        cols[i] = it.b; it.col = i;
      });
      cluster.forEach(it => { it.n = cols.length; out.push(it); });
      cluster = [];
    };
    items.forEach(it => { if (it.a >= cEnd && cluster.length) flush(); cluster.push(it); cEnd = Math.max(cEnd, it.b); });
    if (cluster.length) flush();
    return out;
  }

  function chip(e, cls) {
    return `<button type="button" class="cal-chip ${cls || ""}${e.editable ? " editable" : ""}" data-id="${e.id}" style="--c:${esc(e.colour)};--fg:${esc(e.fg)}" title="${esc(e.title)}">` +
      (e.allDay || cls === "bar" ? "" : `<span class="cal-dot"></span><span class="cal-chip-t">${hm(e.start)}</span> `) + `${esc(e.title)}</button>`;
  }

  function renderGrid(days) {
    const t = today(), list = visible();
    const head = days.map(d => `<div class="cal-dh${d === t ? " today" : ""}"><span>${DAY[dow(d)]}</span><button type="button" data-goto="${d}" title="${longDate(d)}">${dnum(d)}</button></div>`).join("");
    const allday = days.map(d => `<div class="cal-adcell" data-day="${d}">${list.filter(e => isAllDayish(e) && onDay(e, d)).map(e => chip(e, "bar")).join("")}</div>`).join("");
    const hours = Array.from({ length: 24 }, (_, h) => `<div class="cal-hour"><span>${h ? String(h).padStart(2, "0") + ":00" : ""}</span></div>`).join("");
    const cols = days.map(d => {
      const evs = lanes(list.filter(e => !isAllDayish(e) && onDay(e, d)), d).map(it => {
        const e = it.e, top = (it.a - toM(d)) / 60 * HOUR, h = Math.max((it.b - it.a) / 60 * HOUR, 18);
        const short = h < 40;
        return `<div class="cal-ev${e.editable ? " editable" : ""}${short ? " short" : ""}" data-id="${e.id}" tabindex="0" role="button"
          style="top:${top}px;height:${h - 2}px;left:calc(${100 * it.col / it.n}% + 1px);width:calc(${100 / it.n}% - 4px);--c:${esc(e.colour)};--fg:${esc(e.fg)}">
          <b>${esc(e.title)}</b><span class="cal-ev-t">${hm(e.start)} – ${hm(e.end)}${e.location ? ", " + esc(e.location) : ""}</span>
          ${e.sim && !short ? `<span class="cal-ev-sim" title="Scenario time">◷ ${hm(e.sim.start)} – ${hm(e.sim.end)}</span>` : ""}
          ${e.editable ? '<i class="cal-resize" data-resize aria-hidden="true"></i>' : ""}</div>`;
      }).join("");
      const nl = d === t ? `<div class="cal-now" style="top:${(now() - toM(d)) / 60 * HOUR}px"></div>` : "";
      return `<div class="cal-col${d === t ? " today" : ""}" data-day="${d}">${evs}${nl}</div>`;
    }).join("");
    const keep = $(".cal-scroll", bodyEl);
    const scrollTop = keep ? keep.scrollTop : null;
    bodyEl.innerHTML = `<div class="cal-grid" style="--days:${days.length};--hour:${HOUR}px">
      <div class="cal-head"><div class="cal-gutter"></div>${head}</div>
      <div class="cal-allday"><div class="cal-gutter">All day</div>${allday}</div>
      <div class="cal-scroll"><div class="cal-hours">${hours}</div><div class="cal-cols">${cols}</div></div></div>`;
    const sc = $(".cal-scroll", bodyEl);
    if (scrollTop !== null && scrolled) sc.scrollTop = scrollTop;
    else { sc.scrollTop = Math.max(0, ((now() % DAY_M) / 60 - 1.5) * HOUR); scrolled = true; }
  }

  function renderMonth() {
    const t = today(), list = visible(), first = monday(date.slice(0, 8) + "01"), m = mnum(date);
    const head = [1, 2, 3, 4, 5, 6, 0].map(i => `<div class="cal-mh">${DAY[i]}</div>`).join("");
    let cells = "";
    for (let i = 0; i < 42; i++) {
      const d = addDays(first, i);
      const evs = list.filter(e => onDay(e, d)).sort((a, b) => (b.allDay - a.allDay) || toM(a.start) - toM(b.start));
      const more = evs.length > 3 ? `<button type="button" class="cal-more" data-goto="${d}">${evs.length - 3} more</button>` : "";
      cells += `<div class="cal-mcell${mnum(d) !== m ? " out" : ""}${d === t ? " today" : ""}" data-day="${d}">
        <button type="button" class="cal-mday" data-goto="${d}" title="${longDate(d)}">${dnum(d) === 1 ? MON[mnum(d)] + " " : ""}${dnum(d)}</button>
        ${evs.slice(0, 3).map(e => chip(e, isAllDayish(e) ? "bar" : "")).join("")}${more}</div>`;
    }
    bodyEl.innerHTML = `<div class="cal-month"><div class="cal-mhead">${head}</div><div class="cal-mgrid">${cells}</div></div>`;
  }

  function renderSchedule() {
    const list = visible().slice().sort((a, b) => toM(a.start) - toM(b.start));
    let html = "";
    for (let i = 0; i < 30; i++) {
      const d = addDays(date, i), evs = list.filter(e => onDay(e, d));
      if (!evs.length) continue;
      html += `<div class="cal-sday${d === today() ? " today" : ""}"><button type="button" class="cal-sdate" data-goto="${d}"><b>${dnum(d)}</b><span>${MON[mnum(d)]}, ${DAY[dow(d)]}</span></button><ul>` +
        evs.map(e => `<li><button type="button" class="cal-srow" data-id="${e.id}" style="--c:${esc(e.colour)}">
          <span class="cal-stime">${isAllDayish(e) ? "All day" : `${dayOf(e.start) < d ? "…" : hm(e.start)} – ${dayOf(e.end) > d && hm(e.end) !== "00:00" ? "…" : hm(e.end)}`}${e.sim ? `<small title="Scenario time">◷ ${e.allDay ? "" : hm(e.sim.start)}</small>` : ""}</span>
          <span class="cal-dot"></span><span class="cal-stitle"><b>${esc(e.title)}</b>${e.location ? `<small>${esc(e.location)}</small>` : ""}</span></button></li>`).join("") + "</ul></div>";
    }
    bodyEl.innerHTML = `<div class="cal-schedule">${html || `<p class="cal-empty">Nothing planned in the 30 days from ${longDate(date)}.${C.readonly ? "" : " Create an event with the Create button."}</p>`}</div>`;
  }

  function renderMini() {
    const el = $("[data-cal-mini]");
    if (!el) return;
    const first = monday(mini), m = mnum(mini), t = today(), [a, b] = range();
    let cells = "";
    for (let i = 0; i < 42; i++) {
      const d = addDays(first, i);
      const inRange = view !== "schedule" && view !== "month" && d >= a && d < b;
      cells += `<button type="button" class="${mnum(d) !== m ? "out" : ""}${d === t ? " today" : ""}${d === date ? " sel" : ""}${inRange ? " rng" : ""}" data-goto="${d}" aria-label="${longDate(d)}">${dnum(d)}</button>`;
    }
    el.innerHTML = `<div class="cal-mini-head"><b>${LMON[m]} ${ynum(mini)}</b><span><button type="button" data-mini="-1" aria-label="Previous month">‹</button><button type="button" data-mini="1" aria-label="Next month">›</button></span></div>
      <div class="cal-mini-grid">${["M", "T", "W", "T", "F", "S", "S"].map(x => `<span>${x}</span>`).join("")}${cells}</div>`;
  }

  /* ---------- navigation ---------- */
  function go(d, v) {
    if (v) { view = v; localStorage.setItem("simex-cal-view", v); }
    if (d) { date = d; mini = d.slice(0, 7) + "-01"; }
    closePop();
    load();
  }
  function step(n) {
    if (view === "day") go(addDays(date, n));
    else if (view === "week") go(addDays(date, 7 * n));
    else if (view === "schedule") go(addDays(date, 30 * n));
    else { const y = ynum(date), m = mnum(date) + n, d = new Date(Date.UTC(y, m, 1)).toISOString().slice(0, 10); go(d); }
  }
  $("[data-cal-today]").addEventListener("click", () => go(today()));
  $("[data-cal-prev]").addEventListener("click", () => step(-1));
  $("[data-cal-next]").addEventListener("click", () => step(1));
  $$("[data-cal-view]").forEach(b => b.addEventListener("click", () => go(null, b.dataset.calView)));
  document.addEventListener("click", (e) => {
    const g = e.target.closest("[data-goto]");
    if (g && !dragging) { e.preventDefault(); go(g.dataset.goto, g.closest("[data-cal-mini]") || g.closest(".cal-schedule") ? null : "day"); return; }
    const mn = e.target.closest("[data-mini]");
    if (mn) {
      const y = ynum(mini), m = mnum(mini) + +mn.dataset.mini;
      mini = new Date(Date.UTC(y, m, 1)).toISOString().slice(0, 10);
      renderMini();
    }
  });
  $$("[data-cal-layer]").forEach(cb => {
    cb.checked = shownLayers.has(cb.value);
    cb.addEventListener("change", () => {
      if (cb.checked) shownLayers.add(cb.value); else shownLayers.delete(cb.value);
      localStorage.setItem(layerKey, JSON.stringify([...shownLayers]));
      render();
    });
  });

  /* ---------- the event popover ---------- */
  const byId = id => events.find(e => String(e.id) === String(id));
  function openPop(ev, anchor) {
    const e = byId(ev);
    if (!e) return;
    const lay = layerOf(e.layer);
    pop.innerHTML = `<div class="cal-pop-bar">
        ${e.editable ? `<button type="button" class="gm-icon-btn sm" data-pop-edit title="Edit" aria-label="Edit">✎</button><button type="button" class="gm-icon-btn sm" data-pop-del title="Delete" aria-label="Delete">🗑</button>` : ""}
        <button type="button" class="gm-icon-btn sm" data-pop-close title="Close" aria-label="Close">✕</button></div>
      <div class="cal-pop-title"><span class="cal-sq" style="background:${esc(e.colour)}"></span><h3>${esc(e.title)}</h3></div>
      <p class="cal-pop-when">${esc(when(e))}</p>
      ${e.sim ? `<p class="cal-pop-sim" title="Scenario time">◷ Scenario: ${esc(simWhen(e))}</p>` : ""}
      ${e.location || e.mode !== "in_person" ? `<p class="cal-pop-where"><b>${esc(modeName(e.mode))}</b>${e.location ? " · " + esc(e.location) : ""}</p>` : ""}
      ${e.description ? `<div class="cal-pop-desc">${esc(e.description)}</div>` : ""}
      <p class="cal-pop-cal">${esc(lay ? lay.name : e.calendar)}${e.organiser ? ` · organised by ${esc(e.organiser)}` : ""}${!e.editable ? " · read-only" : ""}</p>`;
    pop.hidden = false;
    pop.dataset.id = e.id;
    const r = anchor.getBoundingClientRect(), w = pop.offsetWidth, h = pop.offsetHeight;
    let left = r.right + 8, top = r.top;
    if (left + w > innerWidth - 8) left = r.left - w - 8;
    if (left < 8) left = Math.max(8, Math.min(innerWidth - w - 8, r.left));
    if (top + h > innerHeight - 8) top = Math.max(8, innerHeight - h - 8);
    pop.style.left = left + "px"; pop.style.top = top + "px";
    $("[data-pop-close]", pop).focus();
  }
  function closePop() { pop.hidden = true; }
  pop.addEventListener("click", (e) => {
    const ev = byId(pop.dataset.id);
    if (e.target.closest("[data-pop-close]")) closePop();
    else if (e.target.closest("[data-pop-edit]") && ev) { closePop(); openEditor(ev); }
    else if (e.target.closest("[data-pop-del]") && ev) { closePop(); remove(ev); }
  });
  document.addEventListener("pointerdown", (e) => { if (!pop.hidden && !pop.contains(e.target) && !e.target.closest("[data-id]")) closePop(); });

  /* ---------- the editor ---------- */
  const writable = C.layers.filter(l => l.writable);
  function openEditor(ev) {
    if (!dlg) return;
    const e = ev || {};
    const isNew = !e.id;
    const start = e.start || fromM(snap(now()) + 60), end = e.end || fromM(toM(start) + 60);
    const allDay = !!e.allDay;
    form.reset();
    F("id").value = e.id || "";
    F("title").value = e.title || "";
    F("start_date").value = dayOf(start); F("start_time").value = allDay ? "09:00" : hm(start);
    const lastDay = allDay && hm(end) === "00:00" ? addDays(dayOf(end), -1) : dayOf(end);
    F("end_date").value = lastDay; F("end_time").value = allDay ? "10:00" : hm(end);
    F("all_day").checked = allDay;
    F("mode").value = e.mode || "in_person";
    F("location").value = e.location || ""; F("description").value = e.description || "";
    const pick = e.layer || (writable.find(l => shownLayers.has(l.key)) || writable[0] || {}).key;
    if (pick) F("layer").value = pick;
    $("[data-cal-dtitle]").textContent = isNew ? "New event" : "Edit event";
    $("[data-cal-delete]").hidden = isNew;
    $("[data-cal-ferr]").hidden = true;
    timesShown();
    dlg.showModal();
    setTimeout(() => F("title").focus(), 0);
  }
  function formTimes() {
    const ad = F("all_day").checked;
    return { start: ad ? F("start_date").value : `${F("start_date").value}T${F("start_time").value || "00:00"}`,
             end: ad ? F("end_date").value : `${F("end_date").value}T${F("end_time").value || "00:00"}`, allDay: ad };
  }
  function timesShown() {
    $$(".cal-dt input[type=time]", form).forEach(i => { i.hidden = F("all_day").checked; });
    const hint = $("[data-cal-simhint]");
    if (!C.simNow || F("all_day").checked || !F("start_date").value) { hint.hidden = true; return; }
    const t = formTimes();
    hint.textContent = `◷ In the scenario: ${shortDate(dayOf(simOf(t.start)))} ${hm(simOf(t.start))} – ${hm(simOf(t.end))}`;
    hint.hidden = false;
  }
  if (form) {
    let lastStart = null;
    form.addEventListener("focusin", (e) => { if (e.target.name === "start_date" || e.target.name === "start_time") lastStart = formTimes().start; });
    form.addEventListener("change", (e) => {
      if ((e.target.name === "start_date" || e.target.name === "start_time") && lastStart && !F("all_day").checked) {
        // Moving the start keeps the length, as in any calendar.
        const t = formTimes(), len = toM(t.end) - toM(lastStart);
        if (len > 0) { const end = fromM(toM(t.start) + len); F("end_date").value = dayOf(end); F("end_time").value = hm(end); }
        lastStart = formTimes().start;
      }
      timesShown();
    });
    form.addEventListener("submit", (e) => {
      e.preventDefault();
      const t = formTimes();
      const err = $("[data-cal-ferr]");
      post(`${C.base}/calendar/save/`, { id: F("id").value, title: F("title").value, start: t.start, end: t.end, all_day: t.allDay ? "1" : "",
        mode: F("mode").value, location: F("location").value, description: F("description").value, layer: F("layer").value })
        .then(j => {
          if (!j.ok) { err.textContent = j.error || "Couldn't save it."; err.hidden = false; return; }
          dlg.close();
          if (!shownLayers.has(j.event.layer)) { shownLayers.add(j.event.layer); const cb = $(`[data-cal-layer][value="${CSS.escape(j.event.layer)}"]`); if (cb) cb.checked = true; }
          load(true);
        });
    });
    $("[data-cal-cancel]").addEventListener("click", () => dlg.close());
    $("[data-cal-delete]").addEventListener("click", () => { const ev = byId(F("id").value); if (ev) { dlg.close(); remove(ev); } });
  }
  function remove(ev) {
    if (!confirm(`Delete “${ev.title}”?`)) return;
    post(`${C.base}/calendar/delete/${ev.id}/`, {}).then(j => { if (!j.ok) showErr(j.error || "Couldn't delete it."); load(true); });
  }
  function saveTimes(ev, start, end) {  // after a drag
    busy = true;
    ev.start = start; ev.end = end; render();
    return post(`${C.base}/calendar/save/`, { id: ev.id, title: ev.title, start, end, all_day: ev.allDay ? "1" : "",
      mode: ev.mode, location: ev.location, description: ev.description, layer: ev.layer })
      .then(j => { busy = false; if (!j.ok) showErr(j.error || "Couldn't move it."); load(true); });
  }
  $$("[data-cal-create]").forEach(b => b.addEventListener("click", () => openEditor(null)));

  /* ---------- dragging (pointer events) ---------- */
  const minuteAt = (col, y) => Math.max(0, Math.min(DAY_M, (y - col.getBoundingClientRect().top) / HOUR * 60));
  const colAt = (x, y) => { const el = document.elementFromPoint(x, y); return el && el.closest(".cal-col"); };
  const cellAt = (x, y) => { const el = document.elementFromPoint(x, y); return el && el.closest("[data-day]"); };

  bodyEl.addEventListener("pointerdown", (e) => {
    if (e.button !== 0) return;
    const evEl = e.target.closest(".cal-ev, .cal-chip");
    const col = e.target.closest(".cal-col");
    if (evEl) {
      const ev = byId(evEl.dataset.id);
      dragging = { kind: "click", el: evEl, ev, x: e.clientX, y: e.clientY, moved: false,
        resize: !!e.target.closest("[data-resize]"), month: evEl.classList.contains("cal-chip") };
      if (ev && ev.editable && col) dragging.offset = minuteAt(col, e.clientY) - (toM(ev.start) - toM(col.dataset.day));
      bodyEl.setPointerCapture(e.pointerId);
      return;
    }
    if (col && !C.readonly && !e.target.closest("[data-goto]")) {
      const m = Math.floor(minuteAt(col, e.clientY) / SNAP) * SNAP;
      const ghost = document.createElement("div");
      ghost.className = "cal-ghost";
      col.appendChild(ghost);
      dragging = { kind: "create", col, a: m, b: m + 60, ghost, x: e.clientX, y: e.clientY, moved: false };
      paintGhost();
      bodyEl.setPointerCapture(e.pointerId);
      e.preventDefault();
    } else if (!C.readonly && view === "month") {
      const cell = e.target.closest(".cal-mcell");
      if (cell && !e.target.closest("button")) dragging = { kind: "mcreate", day: cell.dataset.day, x: e.clientX, y: e.clientY };
    }
  });
  function paintGhost() {
    const d = dragging, a = Math.min(d.a, d.b), b = Math.max(d.a, d.b);
    d.ghost.style.top = a / 60 * HOUR + "px";
    d.ghost.style.height = Math.max((b - a) / 60 * HOUR - 2, 14) + "px";
    d.ghost.textContent = `${fromM(a).slice(11, 16)} – ${fromM(b).slice(11, 16)}`;
  }
  bodyEl.addEventListener("pointermove", (e) => {
    const d = dragging;
    if (!d) return;
    if (!d.moved && Math.hypot(e.clientX - d.x, e.clientY - d.y) < 5) return;
    d.moved = true;
    if (d.kind === "click" && d.ev && d.ev.editable) d.el.classList.add("dragging");  // out of elementFromPoint's way
    if (d.kind === "create") {
      const m = snap(minuteAt(d.col, e.clientY));
      d.b = m === d.a ? d.a + SNAP : m;
      paintGhost();
    } else if (d.kind === "click" && d.ev && d.ev.editable) {
      if (d.month) {
        d.el.classList.add("dragging");
        d.el.style.transform = `translate(${e.clientX - d.x}px, ${e.clientY - d.y}px)`;
        $$(".cal-mcell.drop").forEach(c => c.classList.remove("drop"));
        const cell = cellAt(e.clientX, e.clientY);
        if (cell && cell.classList.contains("cal-mcell")) cell.classList.add("drop");
        return;
      }
      const col = colAt(e.clientX, e.clientY) || d.el.parentElement;
      const len = toM(d.ev.end) - toM(d.ev.start);
      if (d.resize) {
        const day0 = toM(d.el.parentElement.dataset.day);
        const endM = Math.max(toM(d.ev.start) + SNAP, day0 + snap(minuteAt(d.el.parentElement, e.clientY)));
        d.newStart = d.ev.start; d.newEnd = fromM(endM);
        d.el.style.height = Math.max((endM - Math.max(toM(d.ev.start), day0)) / 60 * HOUR - 2, 16) + "px";
      } else {
        const startIn = snap(minuteAt(col, e.clientY) - (d.offset || 0));
        const s = toM(col.dataset.day) + startIn;
        d.newStart = fromM(s); d.newEnd = fromM(s + len);
        if (d.el.parentElement !== col) col.appendChild(d.el);
        d.el.style.top = startIn / 60 * HOUR + "px";
        d.el.style.left = "1px"; d.el.style.width = "calc(100% - 4px)";
      }
      d.el.classList.add("dragging");
      const t = $(".cal-ev-t", d.el);
      if (t) t.textContent = `${hm(d.newStart)} – ${hm(d.newEnd)}`;
    }
  });
  function endDrag(e) {
    const d = dragging;
    if (!d) return;
    dragging = null;
    if (d.kind === "create") {
      d.ghost.remove();
      const day0 = toM(d.col.dataset.day), a = Math.min(d.a, d.b), b = Math.max(d.a, d.b);
      openEditor({ start: fromM(day0 + a), end: fromM(day0 + (b > a ? b : a + 60)) });
    } else if (d.kind === "mcreate") {
      if (Math.hypot(e.clientX - d.x, e.clientY - d.y) < 5) openEditor({ start: d.day + "T09:00", end: d.day + "T10:00" });
    } else if (d.kind === "click") {
      if (!d.moved || !d.ev || !d.ev.editable) { openPop(d.el.dataset.id, d.el); return; }
      if (d.month) {
        $$(".cal-mcell.drop").forEach(c => c.classList.remove("drop"));
        const cell = cellAt(e.clientX, e.clientY), from = d.el.closest("[data-day]");
        if (!cell || !from || cell === from) { render(); return; }
        const shift = toM(cell.dataset.day) - toM(from.dataset.day);
        saveTimes(d.ev, fromM(toM(d.ev.start) + shift), fromM(toM(d.ev.end) + shift));
        return;
      }
      if (d.newStart && (d.newStart !== d.ev.start || d.newEnd !== d.ev.end)) saveTimes(d.ev, d.newStart, d.newEnd);
      else render();
    }
  }
  bodyEl.addEventListener("pointerup", endDrag);
  bodyEl.addEventListener("pointercancel", () => { if (dragging && dragging.ghost) dragging.ghost.remove(); dragging = null; render(); });
  bodyEl.addEventListener("keydown", (e) => {
    const evEl = e.target.closest("[data-id]");
    if (evEl && (e.key === "Enter" || e.key === " ")) { e.preventDefault(); openPop(evEl.dataset.id, evEl); }
  });
  // The schedule's and month's rows open on a click (no drag there but in Month).
  bodyEl.addEventListener("click", (e) => {
    const row = e.target.closest(".cal-srow");
    if (row) openPop(row.dataset.id, row);
  });

  /* ---------- keys ---------- */
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !pop.hidden) { closePop(); return; }
    if (e.metaKey || e.ctrlKey || e.altKey || (dlg && dlg.open) || e.target.closest("input, textarea, select, [contenteditable]")) return;
    const k = e.key.toLowerCase();
    if (k === "t") go(today());
    else if (k === "n" || k === "j") step(1);
    else if (k === "p" || k === "k") step(-1);
    else if (k === "d") go(null, "day");
    else if (k === "w") go(null, "week");
    else if (k === "m") go(null, "month");
    else if (k === "a") go(null, "schedule");
    else if (k === "c" && !C.readonly) openEditor(null);
  });

  // Others' changes and the "now" line: every 30 s while the page is in view.
  setInterval(() => { if (!document.hidden && !dragging && !busy && !(dlg && dlg.open) && pop.hidden) load(true); }, 30000);
  load(true);
})();
