/* Dashboard → Network: the live map of who writes to whom (ops/network.py gives the sends).
   A small force layout on a canvas, no library. Dots: teams (members folded in until a team is
   opened), team leaders, members, personas. A line per direction, as thick as its messages,
   bright where it starts; a new message travels along its line as a glowing dot. */
(function () {
  "use strict";
  const cfgEl = document.getElementById("net-cfg");
  const root = document.querySelector("[data-net]");
  if (!cfgEl || !root) return;
  const cfg = JSON.parse(cfgEl.textContent);
  const $ = (sel) => root.querySelector(sel);
  const canvas = $("[data-net-canvas]"), stage = $("[data-net-stage]"), tip = $("[data-net-tip]");
  const g = canvas.getContext("2d");
  const STORE = "simex-net-" + cfg.kind;
  const POLL_MS = 5000, RELOAD_MS = 5 * 60 * 1000, TRAVEL_MS = 1800, GLOW_MS = 12000, FADE_MS = 45 * 60 * 1000;

  // --- state -------------------------------------------------------------------------------------
  const meta = new Map();     // node id -> {id, type, name, team, slot, role, ai, addr}
  let sends = [];             // [{id, at, f, to[], k, s}], oldest first
  const seen = new Set();
  let cursor = {};
  let clock = null, clockAt = 0;
  const saved = load();
  const opts = Object.assign({view: "teams", win: "all", injects: true, minLine: 1, minPersona: 1}, saved.opts || {});
  const STYLE = {theme: "auto", dot: 1, line: 1, lineAlpha: 1, curve: 1, labels: "auto", labelSize: 1, motion: true, legend: true};
  const style = Object.assign({}, STYLE, saved.style || {});
  const expanded = new Set(saved.expanded || []);
  const pos = saved.pos || {};             // id -> [x, y, pinned]
  let view = saved.viewT || null;          // {k, x, y}: screen = world * k + (x, y)
  let follow = !saved.viewT, aim = null;  // follow: keep the whole map in view
  const sim = new Map();                   // id -> live node {id, x, y, vx, vy, r, fx, fy, ...}
  let links = [], linkBy = new Map(), visible = new Set();
  let stats = {};                          // id -> {out, in, self}
  const particles = [];                    // {key, start}
  const fresh = new Set();                 // sends just arrived: highlighted in the feed once
  const flash = new Map();                 // id -> time it last sent or got something
  let alpha = 1, hover = null, selected = saved.selected || null, dirty = true, dragging = null, panning = null;
  let W = 0, H = 0, dpr = 1;

  function load() {
    try { return JSON.parse(localStorage.getItem(STORE) || "{}"); } catch (e) { return {}; }
  }
  function save() {
    const p = {};
    sim.forEach((n) => { p[n.id] = [Math.round(n.x), Math.round(n.y), n.fx != null ? 1 : 0]; });
    Object.assign(pos, p);
    try {
      localStorage.setItem(STORE, JSON.stringify({opts, style, expanded: [...expanded], pos, viewT: follow ? null : view, selected}));
    } catch (e) { /* full or private: the layout just isn't kept */ }
  }

  // --- colours (the dataviz reference palette, light and dark steps in network.css) ---------------
  let C = {};
  function readColours() {
    const cs = getComputedStyle(root);
    const v = (n) => cs.getPropertyValue(n).trim();
    C = {surface: v("--net-surface"), ink: v("--net-ink"), ink2: v("--net-ink-2"), muted: v("--net-muted"),
         persona: v("--net-persona"), excon: v("--net-excon"), halo: v("--net-halo"), glow: v("--net-glow"),
         slots: [1, 2, 3, 4, 5, 6, 7, 8].map((i) => v("--s" + i))};
  }
  function applyStyle() {
    // The theme's colours are CSS variables (network.css): set it, then read them again.
    const t = style.theme === "auto" ? (cfg.screen ? "dark" : "light") : style.theme;
    root.dataset.netTheme = t;
    if (cfg.screen) document.body.dataset.netTheme = t;
    root.classList.toggle("panelled", !cfg.screen && t !== "light");
    root.classList.toggle("no-legend", !style.legend);
    if (!style.motion) particles.length = 0;
    readColours();
    dirty = true;
  }
  function colourOf(id) {
    const m = meta.get(id);
    if (!m) return C.persona;
    if (m.type === "persona") return C.persona;
    if (m.type === "excon") return C.excon;
    const t = meta.get(m.team);
    return t && t.slot != null && t.slot < 8 ? C.slots[t.slot] : C.muted;  // past eight teams: grey, named by its label
  }
  function rgba(hex, a) {
    if (!hex || hex[0] !== "#") return hex;
    const n = parseInt(hex.length === 4 ? hex.slice(1).split("").map((c) => c + c).join("") : hex.slice(1), 16);
    return `rgba(${(n >> 16) & 255},${(n >> 8) & 255},${n & 255},${a})`;
  }

  // --- what is shown ---------------------------------------------------------------------------------
  function shownId(id) {
    const m = meta.get(id);
    if (m && m.type === "member" && m.team && meta.has(m.team) && opts.view !== "people" && !expanded.has(m.team)) {
      return m.team;
    }
    return id;
  }
  function windowStart(now) {
    if (opts.win === "all") return 0;
    if (opts.win === "today") { const d = new Date(now); d.setHours(0, 0, 0, 0); return d.getTime(); }
    return now - parseInt(opts.win, 10) * 60000;
  }
  function counted(e) { return opts.injects || e.k !== "inject"; }

  function rebuild() {
    const now = Date.now(), t0 = windowStart(now);
    stats = {};
    const st = (id) => (stats[id] = stats[id] || {out: 0, in: 0, self: 0});
    const agg = new Map();
    for (const e of sends) {
      if (e.at < t0 || !counted(e)) continue;
      const f = shownId(e.f);
      st(f).out++;
      const tos = new Set(e.to.map(shownId));
      for (const t of tos) {
        if (t === f) { st(f).self++; continue; }
        st(t).in++;
        const key = f + ">" + t;
        const l = agg.get(key) || {key, a: f, b: t, n: 0, last: 0};
        l.n++; l.last = Math.max(l.last, e.at);
        agg.set(key, l);
      }
    }
    // Dots: every team and leader; members when their team is open; personas with enough mail.
    visible = new Set();
    meta.forEach((m, id) => {
      if (shownId(id) !== id) return;
      const s = stats[id], total = s ? s.out + s.in : 0;
      if (m.type === "team" || m.type === "leader" || m.type === "member") {
        if (!m.gone || total) visible.add(id);
      } else if (total >= Math.max(1, opts.minPersona) || id === selected) {
        visible.add(id);
      }
    });
    links = [...agg.values()].filter((l) => l.n >= opts.minLine && visible.has(l.a) && visible.has(l.b));
    linkBy = new Map(links.map((l) => [l.key, l]));
    for (const l of links) l.both = linkBy.has(l.b + ">" + l.a);
    // Live nodes: keep the ones there are (their place), add the new ones near a neighbour.
    for (const id of [...sim.keys()]) if (!visible.has(id)) sim.delete(id);
    for (const id of visible) {
      let n = sim.get(id);
      if (!n) {
        n = {id, vx: 0, vy: 0};
        const p = pos[id];
        if (p) { n.x = p[0]; n.y = p[1]; if (p[2]) { n.fx = p[0]; n.fy = p[1]; } }
        else {
          const m = meta.get(id), near = sim.get(m && m.team) || neighbour(id);
          const a = Math.random() * Math.PI * 2, d = near ? 40 + Math.random() * 40 : 150 + Math.random() * 120;
          n.x = (near ? near.x : 0) + Math.cos(a) * d; n.y = (near ? near.y : 0) + Math.sin(a) * d;
        }
        sim.set(id, n);
      }
      const m = meta.get(id), s = stats[id], total = s ? s.out + s.in + s.self * 0.5 : 0;
      const base = {team: 11, leader: 7, member: 5, persona: 5, excon: 6}[m.type] || 5;
      n.r = Math.min(52, base + 2.3 * Math.sqrt(total)) * style.dot;
    }
    $("[data-net-empty]").hidden = sends.length > 0;
    alpha = Math.max(alpha, 0.35);
    dirty = true;
    renderStats(); renderDetail(); renderFeed();
  }
  function neighbour(id) {
    for (const l of linkBy.values()) {
      if (l.a === id && sim.has(l.b)) return sim.get(l.b);
      if (l.b === id && sim.has(l.a)) return sim.get(l.a);
    }
    return null;
  }

  // --- layout ------------------------------------------------------------------------------------
  function tick() {
    const ns = [...sim.values()];
    const k = alpha;
    // Repulsion and collision.
    for (let i = 0; i < ns.length; i++) {
      const a = ns[i];
      for (let j = i + 1; j < ns.length; j++) {
        const b = ns[j];
        let dx = b.x - a.x, dy = b.y - a.y;
        let d2 = dx * dx + dy * dy;
        if (d2 < 0.01) { dx = Math.random() - 0.5; dy = Math.random() - 0.5; d2 = 0.25; }
        const d = Math.sqrt(d2);
        let f = -480 * k / Math.max(d, 15);
        const gap = a.r + b.r + 10;
        if (d < gap) f -= (gap - d) * 0.25;
        const fx = dx / d * f, fy = dy / d * f;
        a.vx += fx; a.vy += fy; b.vx -= fx; b.vy -= fy;
      }
    }
    // Springs: mail lines, and a soft tie of members and leaders to their team.
    const spring = (a, b, len, s) => {
      const dx = b.x - a.x, dy = b.y - a.y, d = Math.sqrt(dx * dx + dy * dy) || 1;
      const f = (d - len) * s * k, fx = dx / d * f, fy = dy / d * f;
      a.vx += fx; a.vy += fy; b.vx -= fx; b.vy -= fy;
    };
    const tied = new Set();
    for (const l of links) {
      const a = sim.get(l.a), b = sim.get(l.b);
      if (!a || !b) continue;
      const pair = l.a < l.b ? l.a + "|" + l.b : l.b + "|" + l.a;
      if (tied.has(pair)) continue;
      tied.add(pair);
      spring(a, b, 70 + a.r + b.r, 0.03 + Math.min(0.05, l.n * 0.004));
    }
    for (const n of ns) {
      const m = meta.get(n.id), t = m && m.team && m.team !== n.id ? sim.get(m.team) : null;
      if (t) spring(n, t, (m.type === "leader" ? 60 : 38) + t.r + n.r, 0.08);
      n.vx -= n.x * 0.004 * k; n.vy -= n.y * 0.004 * k;  // gently toward the middle
    }
    for (const n of ns) {
      if (n.fx != null) { n.x = n.fx; n.y = n.fy; n.vx = n.vy = 0; continue; }
      n.vx *= 0.55; n.vy *= 0.55;
      n.x += Math.max(-30, Math.min(30, n.vx)); n.y += Math.max(-30, Math.min(30, n.vy));
    }
    alpha *= 0.985;
  }

  // --- drawing -----------------------------------------------------------------------------------
  function resize() {
    dpr = window.devicePixelRatio || 1;
    W = stage.clientWidth; H = stage.clientHeight;
    canvas.width = W * dpr; canvas.height = H * dpr;
    if (!view) view = {k: 1, x: W / 2, y: H / 2};
    dirty = true;
  }
  function fitTarget() {
    const ns = [...sim.values()];
    if (!ns.length || !W) return null;
    let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
    for (const n of ns) {  // with room for its label below it
      const lw = Math.max(n.r, 55);
      x0 = Math.min(x0, n.x - lw); y0 = Math.min(y0, n.y - n.r * 1.2); x1 = Math.max(x1, n.x + lw); y1 = Math.max(y1, n.y + n.r * 1.2 + 20);
    }
    const pad = 30, k = Math.min(2.5, Math.max(0.15, Math.min((W - 2 * pad) / (x1 - x0 || 1), (H - 2 * pad - 30) / (y1 - y0 || 1))));
    return {k, x: W / 2 - (x0 + x1) / 2 * k, y: (H - 30) / 2 - (y0 + y1) / 2 * k};
  }
  function fit() { follow = true; aim = fitTarget(); if (aim) view = aim; dirty = true; }
  function ease() {
    // Following (until the map is panned or zoomed by hand): the view glides to keep everything in.
    if (!follow) return;
    if (ticks % 20 === 0 || !aim) aim = fitTarget();
    if (!aim) return;
    const d = Math.abs(aim.k - view.k) / view.k + (Math.abs(aim.x - view.x) + Math.abs(aim.y - view.y)) / 400;
    if (d < 0.002) return;
    view = {k: view.k + (aim.k - view.k) * 0.08, x: view.x + (aim.x - view.x) * 0.08, y: view.y + (aim.y - view.y) * 0.08};
    dirty = true;
  }
  function control(a, b, l) {
    // A line bows a little to its left, so the two directions of a pair are two curves.
    const dx = b.x - a.x, dy = b.y - a.y, len = Math.sqrt(dx * dx + dy * dy) || 1;
    const c = Math.max(l.both ? 0.04 : 0, (l.both ? 0.22 : 0.1) * style.curve) * len;  // a pair never quite overlaps
    return {x: (a.x + b.x) / 2 + dy / len * c, y: (a.y + b.y) / 2 - dx / len * c};
  }
  function curve(na, nb, l) {
    // The line from edge to edge of the two dots (so where it arrives shows), and its control point.
    const c = control(na, nb, l);
    const edge = (n, r) => {
      const dx = c.x - n.x, dy = c.y - n.y, d = Math.sqrt(dx * dx + dy * dy) || 1;
      return {x: n.x + dx / d * r, y: n.y + dy / d * r};
    };
    return {a: edge(na, na.r * (meta.get(na.id).type === "leader" ? 1.1 : 1)), c,
            b: edge(nb, nb.r * (meta.get(nb.id).type === "leader" ? 1.1 : 1) + 2 / view.k)};
  }
  function bez(a, c, b, t) {
    const u = 1 - t;
    return {x: u * u * a.x + 2 * u * t * c.x + t * t * b.x, y: u * u * a.y + 2 * u * t * c.y + t * t * b.y};
  }
  function focusSet() {
    const f = hover || selected;
    if (!f || !sim.has(f)) return null;
    const s = new Set([f]);
    for (const l of links) { if (l.a === f) s.add(l.b); if (l.b === f) s.add(l.a); }
    const fm = meta.get(f);
    if (fm && fm.type === "team") sim.forEach((n) => { const m = meta.get(n.id); if (m.team === f) s.add(n.id); });  // its own people
    return s;
  }
  function draw(now) {
    g.setTransform(dpr, 0, 0, dpr, 0, 0);
    g.clearRect(0, 0, W, H);
    g.setTransform(dpr * view.k, 0, 0, dpr * view.k, dpr * view.x, dpr * view.y);
    const k = view.k, focus = focusSet(), fid = hover || selected;
    const maxN = Math.max(1, ...links.map((l) => l.n));
    const fades = opts.win === "all" || opts.win === "today";
    // Lines.
    for (const l of links) {
      const na = sim.get(l.a), nb = sim.get(l.b);
      if (!na || !nb) continue;
      const {a, c, b} = curve(na, nb, l), age = now - l.last, glow = style.motion && age < GLOW_MS;
      let al = fades ? 0.3 + 0.7 * Math.exp(-age / FADE_MS) : 0.85;
      if (focus) al = l.a === fid || l.b === fid ? 1 : 0.07;
      if (glow) al = Math.max(al, focus && !(l.a === fid || l.b === fid) ? 0.3 : 1);
      al = Math.min(1, al * style.lineAlpha);
      const w = Math.min(14, 1 + 1.7 * Math.sqrt(l.n) * (maxN > 40 ? 0.7 : 1)) * style.line / Math.max(0.6, Math.sqrt(k));
      const col = colourOf(l.a);
      const grad = g.createLinearGradient(a.x, a.y, b.x, b.y);
      grad.addColorStop(0, rgba(col, al));
      grad.addColorStop(1, rgba(col, al * 0.18));
      g.beginPath(); g.moveTo(a.x, a.y); g.quadraticCurveTo(c.x, c.y, b.x, b.y);
      g.lineWidth = w; g.strokeStyle = grad; g.lineCap = "round";
      if (glow) { g.shadowColor = col; g.shadowBlur = 14 * (1 - age / GLOW_MS); }
      g.stroke();
      g.shadowBlur = 0;
      if (focus && (l.a === fid || l.b === fid)) arrow(a, c, b, col, w);
    }
    // Messages on their way.
    for (let i = particles.length - 1; i >= 0; i--) {
      const p = particles[i], t = (now - p.start) / TRAVEL_MS;
      if (t >= 1) { particles.splice(i, 1); continue; }
      if (t < 0) continue;
      const l = linkBy.get(p.key), na = l && sim.get(l.a), nb = l && sim.get(l.b);
      if (!na || !nb) continue;
      const {a, c, b} = curve(na, nb, l), e = t < 0.5 ? 2 * t * t : 1 - Math.pow(-2 * t + 2, 2) / 2;
      for (let j = 4; j >= 0; j--) {  // a short tail behind it
        const q = bez(a, c, b, Math.max(0, e - j * 0.025));
        g.beginPath(); g.arc(q.x, q.y, (5.5 - j * 0.8) / Math.sqrt(k), 0, Math.PI * 2);
        g.fillStyle = j ? rgba(colourOf(l.a), 0.5 - j * 0.09) : C.glow;
        if (!j) { g.shadowColor = colourOf(l.a); g.shadowBlur = 16; }
        g.fill(); g.shadowBlur = 0;
      }
    }
    // Dots.
    for (const n of sim.values()) {
      const m = meta.get(n.id), col = colourOf(n.id);
      const dim = focus && !focus.has(n.id);
      g.globalAlpha = dim ? 0.18 : 1;
      const fl = flash.get(n.id), fage = fl ? now - fl : Infinity;
      if (style.motion && fage < GLOW_MS / 2) {  // just wrote or just got mail: a ring that widens and fades
        const t = fage / (GLOW_MS / 2);
        g.beginPath(); g.arc(n.x, n.y, n.r + 4 + t * 18, 0, Math.PI * 2);
        g.strokeStyle = rgba(col, 0.6 * (1 - t)); g.lineWidth = 2.5 / k; g.stroke();
      }
      g.beginPath();
      if (m.type === "leader") {
        const r = n.r * 1.2;
        g.moveTo(n.x, n.y - r); g.lineTo(n.x + r, n.y); g.lineTo(n.x, n.y + r); g.lineTo(n.x - r, n.y); g.closePath();
      } else if (m.type === "excon") {
        g.rect(n.x - n.r, n.y - n.r, n.r * 2, n.r * 2);
      } else {
        g.arc(n.x, n.y, n.r, 0, Math.PI * 2);
      }
      const open = m.type === "team" && (opts.view === "people" || expanded.has(n.id));
      g.fillStyle = open ? rgba(col, 0.16) : (m.type === "member" ? rgba(col, 0.85) : col);
      g.fill();
      g.lineWidth = (open ? 2.5 : 2) / k;
      g.strokeStyle = open ? col : C.surface;  // a surface ring keeps touching dots apart
      if (open) g.setLineDash([5 / k, 4 / k]);
      g.stroke(); g.setLineDash([]);
      if (m.ai) {
        g.beginPath(); g.arc(n.x, n.y, n.r + 3.5 / k, 0, Math.PI * 2);
        g.setLineDash([3 / k, 3 / k]); g.strokeStyle = col; g.lineWidth = 1.5 / k; g.stroke(); g.setLineDash([]);
      }
      if (n.fx != null) {  // pinned: a small mark
        g.beginPath(); g.arc(n.x + n.r * 0.75, n.y - n.r * 0.75, 2.5 / k, 0, Math.PI * 2); g.fillStyle = C.ink2; g.fill();
      }
      if (n.id === selected) {
        g.beginPath(); g.arc(n.x, n.y, n.r + 6 / k, 0, Math.PI * 2); g.strokeStyle = C.ink; g.lineWidth = 1.5 / k; g.stroke();
      }
      g.globalAlpha = 1;
    }
    // Labels last, on a halo, at the same size whatever the zoom.
    g.textAlign = "center"; g.textBaseline = "top"; g.lineJoin = "round";
    for (const n of sim.values()) {
      const m = meta.get(n.id);
      if (style.labels === "none" && !(focus && n.id === (hover || selected))) continue;
      const always = m.type === "team" || m.type === "leader" || m.type === "excon";
      const show = style.labels === "all" || always || (style.labels === "auto" && ((focus && focus.has(n.id)) ||
        (m.type === "persona" && (n.r >= 9 * style.dot || k > 1.1)) || (m.type === "member" && k > 1.3)));
      if (!show || (focus && !focus.has(n.id) && !always)) continue;
      const fs = (m.type === "team" ? 13 : 11.5) * style.labelSize / k;
      g.font = `${m.type === "team" ? 700 : 500} ${fs}px system-ui, -apple-system, "Segoe UI", sans-serif`;
      const text = label(n.id), y = n.y + n.r * (m.type === "leader" ? 1.2 : 1) + 4 / k;
      g.globalAlpha = focus && !focus.has(n.id) ? 0.35 : 1;
      g.strokeStyle = C.halo; g.lineWidth = 3.5 / k; g.strokeText(text, n.x, y);
      g.fillStyle = m.type === "team" ? C.ink : C.ink2; g.fillText(text, n.x, y);
      g.globalAlpha = 1;
    }
  }
  function arrow(a, c, b, col, w) {
    const t = 0.6, p = bez(a, c, b, t), q = bez(a, c, b, t + 0.02);
    const ang = Math.atan2(q.y - p.y, q.x - p.x), s = Math.max(5, w * 2.2);
    g.beginPath();
    g.moveTo(p.x + Math.cos(ang) * s, p.y + Math.sin(ang) * s);
    g.lineTo(p.x + Math.cos(ang + 2.5) * s, p.y + Math.sin(ang + 2.5) * s);
    g.lineTo(p.x + Math.cos(ang - 2.5) * s, p.y + Math.sin(ang - 2.5) * s);
    g.closePath(); g.fillStyle = col; g.fill();
  }
  function label(id) {
    const m = meta.get(id);
    return m ? m.name : id;
  }
  function longName(id) {
    const m = meta.get(id);
    if (!m) return id;
    const t = meta.get(m.team);
    if (m.type === "member") return `${t ? t.name : "?"} (${m.name})`;
    if (m.type === "leader") return `${m.name} · team leader${t ? ", " + t.name : ""}`;
    return label(id);
  }

  let last = 0, ticks = 0, lastSave = 0;
  function frame(now) {
    if (alpha > 0.004 || dragging) { tick(); dirty = true; ticks++; }
    if (!dragging) { if (alpha <= 0.004) ticks++; ease(); }
    if (particles.length) dirty = true;
    const wall = Date.now();
    if (wall - last > 500) { last = wall; dirty = true; }  // glows and fades move slowly
    if (dirty) { draw(wall); dirty = false; }
    if (wall - lastSave > 5000) { lastSave = wall; save(); }
    requestAnimationFrame(frame);
  }

  // --- side panel ------------------------------------------------------------------------------------
  const ago = (ms) => {
    const s = Math.max(0, Math.round((Date.now() - ms) / 1000));
    if (s < 60) return s + "s ago";
    if (s < 3600) return Math.round(s / 60) + " min ago";
    if (s < 86400) return Math.floor(s / 3600) + " h " + Math.round((s % 3600) / 60) + " min ago";
    return new Date(ms).toLocaleString([], {weekday: "short", hour: "2-digit", minute: "2-digit"});
  };
  function el(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  }
  function renderStats() {
    const now = Date.now(), t0 = windowStart(now);
    const n = (from) => sends.filter((e) => e.at >= from && counted(e)).length;
    const replies = sends.filter((e) => e.at >= now - 3600000 && e.k === "participant").length;
    const box = $("[data-net-stats]");
    box.replaceChildren();
    for (const [v, lab] of [[n(now - 15 * 60000), "last 15 min"], [replies, "by teams, 1 h"],
                            [n(t0), opts.win === "all" ? "in all" : "shown"]]) {
      const d = el("div"); d.append(el("b", null, String(v)), el("span", null, lab)); box.append(d);
    }
  }
  function renderDetail() {
    const box = $("[data-net-detail]"), id = hover && hover !== selected && !selected ? hover : selected;
    if (!id || !meta.has(id)) {
      box.replaceChildren(el("p", "muted small", "Point at a dot to see its figures, click it to keep them here. Click a team to open it up into its members; drag a dot to pin it, double-click to let it go."));
      return;
    }
    const m = meta.get(id), s = stats[id] || {out: 0, in: 0, self: 0};
    box.replaceChildren();
    const h = el("h4"), sw = el("span", "sw"); sw.style.background = colourOf(id);
    h.append(sw, document.createTextNode(longName(id)));
    const kind = {team: "Team inbox", leader: "Team leader (plays the team leader persona)", member: "Member" + (m.role ? " · " + m.role : ""),
                  persona: (m.ai ? "AI persona" : "Persona") + (m.addr ? " · " + m.addr : ""), excon: "ExCon, as no persona"}[m.type] || "";
    box.append(h, el("div", "sub", kind));
    const tb = el("table");
    const row = (a, b) => { const tr = el("tr"); tr.append(el("td", null, a), el("td", "n", b)); tb.append(tr); };
    row("Sent", String(s.out)); row("Received", String(s.in));
    if (s.self) row(m.type === "team" ? "Within the team" : "To themselves", String(s.self));
    box.append(tb);
    // Busiest contacts, both ways.
    const with_ = new Map();
    for (const l of links) {
      if (l.a === id) { const c = with_.get(l.b) || [0, 0]; c[0] += l.n; with_.set(l.b, c); }
      if (l.b === id) { const c = with_.get(l.a) || [0, 0]; c[1] += l.n; with_.set(l.a, c); }
    }
    const top = [...with_.entries()].sort((x, y) => (y[1][0] + y[1][1]) - (x[1][0] + x[1][1])).slice(0, 6);
    if (top.length) {
      box.append(el("div", "label", "Writes with"));
      const t2 = el("table");
      for (const [o, [out, inn]] of top) {
        const tr = el("tr"); tr.append(el("td", null, longName(o)), el("td", "n", `→ ${out}  ← ${inn}`)); t2.append(tr);
      }
      box.append(t2);
    }
    // Its latest messages.
    const mine = [];
    for (let i = sends.length - 1; i >= 0 && mine.length < 5; i--) {
      const e = sends[i];
      if (!counted(e)) continue;
      const f = shownId(e.f), tos = e.to.map(shownId);
      if (f === id || tos.includes(id)) mine.push(e);
    }
    if (mine.length) {
      box.append(el("div", "label", "Latest"));
      const ol = el("ol");
      for (const e of mine) {
        const out = shownId(e.f) === id;
        const li = el("li", null, `${out ? "→" : "←"} ${e.s || "(no subject)"}`);
        li.title = `${longName(e.f)} → ${e.to.map(longName).join(", ")} · ${ago(e.at)}`;
        ol.append(li);
      }
      box.append(ol);
    }
    if (m.type === "team" && opts.view !== "people") {
      const b = el("button", "btn small", expanded.has(id) ? "Fold the members in" : "Open up into its members");
      b.type = "button";
      b.onclick = () => { toggleTeam(id); };
      box.append(b);
    }
    if (sim.get(id) && sim.get(id).fx != null) {
      const b = el("button", "btn small", "Unpin"); b.type = "button";
      b.onclick = () => { const n = sim.get(id); n.fx = n.fy = null; alpha = Math.max(alpha, 0.3); renderDetail(); };
      box.append(document.createTextNode(" "), b);
    }
  }
  function renderFeed() {
    const ol = $("[data-net-feed]"), items = [];
    for (let i = sends.length - 1; i >= 0 && items.length < 14; i--) if (counted(sends[i])) items.push(sends[i]);
    ol.replaceChildren();
    for (const e of items) {
      const li = el("li", fresh.has(e.id) ? "fresh" : "");
      const to = e.to.length > 2 ? `${longName(e.to[0])} +${e.to.length - 1}` : e.to.map(longName).join(", ");
      li.append(el("span", "t", ago(e.at)), el("span", "who", `${longName(e.f)} → ${to}`),
                el("span", "s", (e.k === "inject" ? "Inject · " : e.k === "followup" ? "Follow-up · " : "") + (e.s || "(no subject)")));
      li.onclick = () => { selected = shownId(e.f); renderDetail(); dirty = true; };
      ol.append(li);
    }
    if (!items.length) ol.append(el("li", "muted", "Nothing yet."));
    fresh.clear();  // highlighted once
  }

  // --- data ------------------------------------------------------------------------------------------
  async function getJSON(url) {
    const r = await fetch(url, {headers: {"x-requested-with": "fetch"}, credentials: "same-origin"});
    if (r.redirected || r.status === 401 || r.status === 403) throw new Error("signed-out");
    if (!r.ok) throw new Error("HTTP " + r.status);
    return r.json();
  }
  function status(text) { const s = $("[data-net-status]"); s.textContent = text || ""; s.hidden = !text; }
  async function loadAll() {
    const d = await getJSON(cfg.data);
    meta.clear();
    for (const n of d.nodes) meta.set(n.id, n);
    sends = d.sends; seen.clear(); for (const e of sends) seen.add(e.id);
    cursor = d.cursor; setClock(d.clock);
    rebuild();
    status("");
  }
  async function poll() {
    try {
      const q = new URLSearchParams(cursor).toString();
      const d = await getJSON(cfg.poll + "&" + q);
      cursor = d.cursor; setClock(d.clock);
      const add = d.sends.filter((e) => !seen.has(e.id));
      if (add.some((e) => !meta.has(e.f) || e.to.some((t) => !meta.has(t)))) { await loadAll(); arrive(add); return; }
      if (!add.length) { status(""); return; }
      for (const e of add) { seen.add(e.id); sends.push(e); }
      sends.sort((a, b) => a.at - b.at || (a.id < b.id ? -1 : 1));
      rebuild();
      arrive(add);
      status("");
    } catch (err) {
      status(err.message === "signed-out" ? "Signed out: sign in again to keep the map live." : "Can't reach the server: retrying…");
    }
  }
  function arrive(add) {
    // The new messages travel along their lines (staggered when many leave at once, as an inject to every team).
    const now = Date.now();
    let i = 0;
    for (const e of add) {
      if (!counted(e) || now - e.at > 10 * 60000) continue;
      fresh.add(e.id);
      const f = shownId(e.f), start = now + Math.min(i++, 12) * 120;
      flash.set(f, now);
      for (const t of new Set(e.to.map(shownId))) {
        if (t === f) continue;
        const key = f + ">" + t;
        if (linkBy.has(key) && style.motion) particles.push({key, start});
        setTimeout(() => { flash.set(t, Date.now()); dirty = true; }, start - now + TRAVEL_MS);
      }
    }
    renderFeed();
    dirty = true;
  }

  // --- clocks (wall screen) -------------------------------------------------------------------------
  function setClock(c) { clock = c; clockAt = Date.now(); }
  const realEl = document.querySelector("[data-net-real]"), simEl = document.querySelector("[data-net-sim]");
  function clocks() {
    if (realEl) {
      realEl.textContent = "Real " + new Date().toLocaleString("en-GB", {timeZone: (clock && clock.tz) || "Africa/Nairobi",
        weekday: "short", hour: "2-digit", minute: "2-digit"});
    }
    if (simEl) {
      simEl.hidden = !clock;
      if (clock) {
        const t = clock.sim + (Date.now() - clockAt) * (clock.rate || 0);
        simEl.textContent = "Scenario " + new Date(t).toLocaleString("en-GB", {timeZone: clock.tz || "Africa/Nairobi",
          weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit"});
      }
    }
  }

  // --- interaction -----------------------------------------------------------------------------------
  function world(ev) {
    const r = canvas.getBoundingClientRect();
    const sx = ev.clientX - r.left, sy = ev.clientY - r.top;
    return {sx, sy, x: (sx - view.x) / view.k, y: (sy - view.y) / view.k};
  }
  function hit(p) {
    let best = null, bd = Infinity;
    for (const n of sim.values()) {
      const d = Math.hypot(n.x - p.x, n.y - p.y);
      if (d < n.r + 5 / view.k && d < bd) { best = n; bd = d; }
    }
    return best;
  }
  function toggleTeam(id) {
    if (expanded.has(id)) expanded.delete(id); else expanded.add(id);
    rebuild(); save();
  }
  let down = null;
  canvas.addEventListener("pointerdown", (ev) => {
    const p = world(ev), n = hit(p);
    canvas.setPointerCapture(ev.pointerId);
    down = {x: ev.clientX, y: ev.clientY, n, moved: false};
    if (n) { dragging = n; } else { panning = {x: ev.clientX - view.x, y: ev.clientY - view.y}; }
    canvas.classList.add("drag");
  });
  canvas.addEventListener("pointermove", (ev) => {
    const p = world(ev);
    if (down && Math.hypot(ev.clientX - down.x, ev.clientY - down.y) > 4) down.moved = true;
    if (dragging && down && down.moved) {
      dragging.fx = dragging.x = p.x; dragging.fy = dragging.y = p.y;
      alpha = Math.max(alpha, 0.12); dirty = true; tip.hidden = true;
      return;
    }
    if (panning) {
      if (down && down.moved) follow = false;
      view.x = ev.clientX - panning.x; view.y = ev.clientY - panning.y; dirty = true; return;
    }
    const n = hit(p), id = n ? n.id : null;
    canvas.classList.toggle("over", !!n);
    if (id !== hover) { hover = id; dirty = true; if (!selected) renderDetail(); }
    if (n) {
      const m = meta.get(n.id), s = stats[n.id] || {out: 0, in: 0, self: 0};
      tip.replaceChildren(el("b", null, longName(n.id)),
        el("span", "muted", `${s.out} sent · ${s.in} received${s.self ? ` · ${s.self} within` : ""}`));
      if (m.type === "team" && opts.view !== "people") tip.append(el("div", "muted", expanded.has(n.id) ? "Click to fold the members in" : "Click to open up into its members"));
      tip.hidden = false;
      const x = Math.min(p.sx + 14, W - 270), y = Math.min(p.sy + 14, H - 70);
      tip.style.left = x + "px"; tip.style.top = y + "px";
    } else {
      tip.hidden = true;
    }
  });
  const up = () => {
    canvas.classList.remove("drag");
    if (down && !down.moved) {
      const n = down.n;
      if (n) {
        const m = meta.get(n.id);
        selected = n.id;
        if (m.type === "team" && opts.view !== "people") toggleTeam(n.id);
      } else {
        selected = null;
      }
      renderDetail(); dirty = true;
    } else if (dragging) {
      save(); renderDetail();
    }
    dragging = null; panning = null; down = null;
  };
  canvas.addEventListener("pointerup", up);
  canvas.addEventListener("pointercancel", up);
  canvas.addEventListener("pointerleave", () => { if (!down) { hover = null; tip.hidden = true; dirty = true; if (!selected) renderDetail(); } });
  canvas.addEventListener("dblclick", (ev) => {
    const n = hit(world(ev));
    if (n && n.fx != null) { n.fx = n.fy = null; alpha = Math.max(alpha, 0.3); save(); renderDetail(); }
  });
  canvas.addEventListener("wheel", (ev) => {
    ev.preventDefault();
    const p = world(ev), k = Math.min(5, Math.max(0.1, view.k * Math.exp(-ev.deltaY * 0.0015)));
    view = {k, x: p.sx - p.x * k, y: p.sy - p.y * k};
    follow = false; dirty = true;
  }, {passive: false});

  // Controls.
  const segs = root.querySelectorAll("[data-net-view]");
  const showView = () => segs.forEach((b) => b.classList.toggle("on", b.dataset.netView === opts.view));
  segs.forEach((b) => b.addEventListener("click", () => { opts.view = b.dataset.netView; showView(); rebuild(); save(); }));
  const win = $("[data-net-win]"), inj = $("[data-net-injects]"), minLine = $("[data-net-minline]"), minPersona = $("[data-net-minpersona]");
  win.value = opts.win; inj.checked = opts.injects; minLine.value = opts.minLine; minPersona.value = opts.minPersona;
  win.addEventListener("change", () => { opts.win = win.value; rebuild(); save(); });
  inj.addEventListener("change", () => { opts.injects = inj.checked; rebuild(); save(); });
  const num = (e, k) => e.addEventListener("input", () => { opts[k] = Math.max(1, parseInt(e.value, 10) || 1); rebuild(); save(); });
  num(minLine, "minLine"); num(minPersona, "minPersona");
  $("[data-net-fit]").addEventListener("click", fit);
  $("[data-net-unpin]").addEventListener("click", () => {
    sim.forEach((n) => { n.fx = n.fy = null; }); for (const id in pos) pos[id][2] = 0;
    alpha = Math.max(alpha, 0.4); save(); renderDetail();
  });
  $("[data-net-reset]").addEventListener("click", () => {
    for (const id in pos) delete pos[id];
    sim.clear(); view = null; resize(); follow = true; aim = null; ticks = 0; alpha = 1;
    rebuild(); save();
  });
  showView();

  // Style: the look of the map, kept in this browser.
  const styleEls = root.querySelectorAll("[data-net-s]");
  const showStyle = () => styleEls.forEach((e) => {
    const v = style[e.dataset.netS];
    if (e.type === "checkbox") e.checked = !!v; else e.value = v;
  });
  styleEls.forEach((e) => e.addEventListener(e.type === "range" ? "input" : "change", () => {
    const k = e.dataset.netS;
    style[k] = e.type === "checkbox" ? e.checked : e.type === "range" ? parseFloat(e.value) : e.value;
    applyStyle();
    if (k === "dot") rebuild();  // dot sizes feed the layout
    save();
  }));
  $("[data-net-style-reset]").addEventListener("click", () => {
    Object.assign(style, STYLE); showStyle(); applyStyle(); rebuild(); save();
  });
  const stylePanel = $("[data-net-style]");
  document.addEventListener("pointerdown", (ev) => { if (stylePanel.open && !stylePanel.contains(ev.target)) stylePanel.open = false; });
  document.addEventListener("keydown", (ev) => { if (ev.key === "Escape") stylePanel.open = false; });
  showStyle();

  // --- start -----------------------------------------------------------------------------------------
  applyStyle();
  new ResizeObserver(resize).observe(stage);
  resize();
  setInterval(clocks, 1000); clocks();
  setInterval(() => { renderStats(); renderFeed(); if (!selected) return; renderDetail(); }, 15000);
  loadAll().then(() => {
    requestAnimationFrame(frame);
    let polling = false;
    setInterval(async () => {
      if (polling || (document.hidden && !cfg.screen && Math.random() > 0.2)) return;  // background tab: now and then
      polling = true; try { await poll(); } finally { polling = false; }
    }, POLL_MS);
    setInterval(() => loadAll().catch(() => {}), RELOAD_MS);  // withdrawals, cleared mailboxes, new people
  }).catch((err) => status(err.message === "signed-out" ? "Signed out: sign in again." : "Couldn't load the map: reload the page."));
})();
