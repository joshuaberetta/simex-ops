// SIMEX Mail's message editor: formatting (bold, italics, underline, strikethrough, colour,
// lists, quotes, links) and images in the text, as in webmail. A contenteditable with a
// toolbar; mail.js's Composer posts html() (images as src="cid:<cid>"), text() and pending()
// (the images not uploaded yet, as "inline" files with "inline_cid"). The server cleans the
// HTML again (ops/richtext.py): what this allows is only what it keeps.
(function () {
  "use strict";
  const TAGS = new Set(["P", "DIV", "BR", "SPAN", "B", "STRONG", "I", "EM", "U", "S", "STRIKE", "DEL", "SUB", "SUP",
    "UL", "OL", "LI", "BLOCKQUOTE", "A", "IMG", "H1", "H2", "H3", "H4", "PRE", "CODE", "HR",
    "TABLE", "THEAD", "TBODY", "TR", "TD", "TH", "FONT"]);
  const DROP = new Set(["SCRIPT", "STYLE", "HEAD", "TITLE", "IFRAME", "OBJECT", "EMBED", "SVG", "MATH", "TEMPLATE",
    "NOSCRIPT", "TEXTAREA", "SELECT", "BUTTON", "FORM", "INPUT", "VIDEO", "AUDIO", "CANVAS", "META", "LINK", "XML"]);
  const STYLES = ["color", "background-color", "font-weight", "font-style", "text-decoration", "text-decoration-line",
    "text-align", "margin-left", "padding-left"];
  const IMAGE_TYPES = ["image/png", "image/jpeg", "image/gif", "image/webp"];
  const MAX_SIDE = 1600, BIG = 1.5 * 1048576;
  const COLOURS = ["#202124", "#5f6368", "#d93025", "#e37400", "#f9ab00", "#188038", "#1a73e8", "#9334e6", "#c5221f", "#0b57d0"];
  let seq = 0;
  const newCid = () => `img-${Date.now().toString(36)}${(seq++).toString(36)}${Math.random().toString(36).slice(2, 8)}`;
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  // Pasted HTML (from a web page, Word...) cut down to what a message keeps.
  function cleanNode(node, out) {
    for (const n of Array.from(node.childNodes)) {
      if (n.nodeType === 3) { out.appendChild(document.createTextNode(n.nodeValue)); continue; }
      if (n.nodeType !== 1) continue;
      const tag = n.tagName.toUpperCase();
      if (DROP.has(tag) || tag.includes(":")) continue;  // <o:p> and the like from Word
      if (!TAGS.has(tag)) { cleanNode(n, out); continue; }
      if (tag === "IMG") {
        const src = n.getAttribute("src") || "";
        if (!src.startsWith("data:image/") && !n.dataset.cid) continue;  // images from the web: never fetched
        const img = document.createElement("img");
        img.setAttribute("src", src);
        if (n.dataset.cid) img.dataset.cid = n.dataset.cid;
        if (n.getAttribute("alt")) img.setAttribute("alt", n.getAttribute("alt"));
        out.appendChild(img);
        continue;
      }
      const el = document.createElement(tag === "FONT" ? "span" : tag.toLowerCase());
      if (tag === "A") {
        const href = (n.getAttribute("href") || "").trim();
        if (/^(https?:|mailto:)/i.test(href)) el.setAttribute("href", href);
      }
      if (tag === "FONT" && n.getAttribute("color")) el.style.color = n.getAttribute("color");
      for (const p of STYLES) {
        const v = n.style && n.style.getPropertyValue(p);
        if (v && !/url|expression|var\(/i.test(v)) el.style.setProperty(p, v);
      }
      if ((tag === "TD" || tag === "TH") && n.getAttribute("colspan")) el.setAttribute("colspan", n.getAttribute("colspan"));
      cleanNode(n, el);
      out.appendChild(el);
    }
    return out;
  }
  function cleanHTML(html) {
    const doc = new DOMParser().parseFromString(html, "text/html");
    const box = document.createElement("div");
    cleanNode(doc.body, box);
    return box.innerHTML;
  }
  function textToHTML(text) {
    return String(text || "").split("\n").map((l) => l ? `<div>${esc(l)}</div>` : "<div><br></div>").join("");
  }

  // A big photo is scaled down (as webmail does) before it goes anywhere; GIFs stay as they are.
  async function shrink(file) {
    if (file.type === "image/gif" || !window.createImageBitmap) return file;
    let bmp;
    try { bmp = await createImageBitmap(file); } catch (e) { return file; }
    const scale = Math.min(1, MAX_SIDE / Math.max(bmp.width, bmp.height));
    if (scale === 1 && file.size <= BIG) return file;
    const c = document.createElement("canvas");
    c.width = Math.round(bmp.width * scale); c.height = Math.round(bmp.height * scale);
    const ctx = c.getContext("2d");
    const png = file.type === "image/png" && file.size <= BIG;  // a screenshot only too wide stays sharp
    if (!png) { ctx.fillStyle = "#fff"; ctx.fillRect(0, 0, c.width, c.height); }
    ctx.drawImage(bmp, 0, 0, c.width, c.height);
    const blob = await new Promise((r) => c.toBlob(r, png ? "image/png" : "image/jpeg", 0.85));
    if (!blob) return file;
    const name = (file.name || "image").replace(/\.[a-z0-9]+$/i, "") + (png ? ".png" : ".jpg");
    return new File([blob], name, { type: blob.type });
  }
  function dataToFile(src, i) {
    const m = /^data:(image\/[a-z+.-]+);base64,(.*)$/i.exec(src);
    if (!m || !IMAGE_TYPES.includes(m[1].toLowerCase())) return null;
    try {
      const bin = atob(m[2]), bytes = new Uint8Array(bin.length);
      for (let k = 0; k < bin.length; k++) bytes[k] = bin.charCodeAt(k);
      return new File([bytes], `pasted-${i}.${m[1].split("/")[1].replace("jpeg", "jpg")}`, { type: m[1] });
    } catch (e) { return null; }
  }

  class Editor {
    constructor(el, bar, opts = {}) {
      this.el = el; this.bar = bar; this.opts = opts;
      this.blobs = new Map();  // cid -> {file, url}: images added here, not on the server yet
      this.sent = new Set();   // ... and those the current draft has already
      el.addEventListener("input", () => { this.tidy(); this.state(); });
      el.addEventListener("paste", (e) => this.paste(e));
      el.addEventListener("drop", (e) => this.drop(e));
      el.addEventListener("dragover", (e) => { if (e.dataTransfer && Array.from(e.dataTransfer.types || []).includes("Files")) e.preventDefault(); });
      el.addEventListener("keydown", (e) => {
        const mod = e.metaKey || e.ctrlKey;
        if (mod && e.key.toLowerCase() === "k") { e.preventDefault(); this.link(); }
        else if (mod && e.shiftKey && (e.key === "7" || e.key === "&")) { e.preventDefault(); this.cmd("insertOrderedList"); }
        else if (mod && e.shiftKey && (e.key === "8" || e.key === "*")) { e.preventDefault(); this.cmd("insertUnorderedList"); }
        else if (mod && e.shiftKey && (e.key === "x" || e.key === "X")) { e.preventDefault(); this.cmd("strikeThrough"); }
      });
      document.addEventListener("selectionchange", () => {
        const s = document.getSelection();
        if (s && s.anchorNode && el.contains(s.anchorNode)) { this.range = s.getRangeAt(0).cloneRange(); this.state(); }
      });
      if (!bar) return;
      // The toolbar never takes the focus (the selection stays where the text is).
      bar.addEventListener("mousedown", (e) => { if (e.target.closest("button")) e.preventDefault(); });
      bar.addEventListener("click", (e) => {
        const b = e.target.closest("button");
        if (!b) return;
        if (b.dataset.cmd) this.cmd(b.dataset.cmd, b.dataset.arg);
        else if (b.dataset.rteLink !== undefined) this.link();
        else if (b.dataset.rteQuote !== undefined) this.quote();
        else if (b.dataset.rteColour !== undefined) { this.restore(); this.cmd("foreColor", b.dataset.rteColour); this.menu(false); }
        else if (b.dataset.rteColours !== undefined) this.menu();
        else if (b.dataset.rteClear !== undefined) { this.cmd("removeFormat"); this.cmd("unlink"); }
      });
      const pal = bar.querySelector("[data-rte-palette]");
      if (pal) pal.innerHTML = COLOURS.map((c) => `<button type="button" class="gm-rte-sw" data-rte-colour="${c}" style="background:${c}" title="${c}" aria-label="Text colour ${c}"></button>`).join("");
    }
    focus(atStart) {
      this.el.focus();
      const s = document.getSelection(), r = document.createRange();
      r.selectNodeContents(this.el); r.collapse(!!atStart);
      s.removeAllRanges(); s.addRange(r);
      if (atStart) this.el.scrollTop = 0;
    }
    restore() {  // back to where the text was being written (after a menu, a file chooser, a prompt)
      this.el.focus();
      if (this.range && this.el.contains(this.range.startContainer)) {
        const s = document.getSelection(); s.removeAllRanges(); s.addRange(this.range);
      }
    }
    cmd(name, arg) {
      if (document.activeElement !== this.el) this.restore();
      document.execCommand("styleWithCSS", false, name === "foreColor");
      document.execCommand(name, false, arg ?? null);
      this.changed();
    }
    menu(on) {
      const pal = this.bar.querySelector("[data-rte-palette]");
      if (pal) pal.hidden = on === undefined ? !pal.hidden : !on;
    }
    quote() {
      const s = document.getSelection();
      const inQuote = s && s.anchorNode && (s.anchorNode.nodeType === 1 ? s.anchorNode : s.anchorNode.parentElement).closest("blockquote");
      if (inQuote && this.el.contains(inQuote)) this.cmd("outdent");
      else this.cmd("formatBlock", "blockquote");
    }
    link() {
      this.restore();
      const s = document.getSelection();
      const a = s && s.anchorNode && (s.anchorNode.nodeType === 1 ? s.anchorNode : s.anchorNode.parentElement).closest("a");
      let url = prompt("Link to (web address or email):", a ? a.getAttribute("href") : "https://");
      if (url === null) { this.restore(); return; }
      url = url.trim();
      this.restore();
      if (!url || url === "https://") { if (a) this.cmd("unlink"); return; }
      if (/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(url)) url = `mailto:${url}`;
      else if (!/^(https?:|mailto:)/i.test(url)) url = `https://${url}`;
      if (s.isCollapsed && !a) this.cmd("insertHTML", `<a href="${esc(url)}">${esc(url)}</a>`);
      else this.cmd("createLink", url);
    }
    state() {
      if (!this.bar) return;
      for (const b of this.bar.querySelectorAll("[data-cmd]")) {
        let on = false;
        try { on = document.queryCommandState(b.dataset.cmd); } catch (e) { /* not a state command */ }
        b.classList.toggle("on", !!on);
      }
    }
    pick(files) { this.restore(); return this.images(files); }  // the toolbar's Insert a picture
    changed() { this.el.dispatchEvent(new Event("input", { bubbles: true })); }
    // Images: inserted where the text is, each a new cid.
    async images(files) {
      const others = [];
      for (const f0 of files) {
        if (!IMAGE_TYPES.includes((f0.type || "").toLowerCase())) { others.push(f0); continue; }
        const f = await shrink(f0);
        const cid = newCid(), url = URL.createObjectURL(f);
        this.blobs.set(cid, { file: f, url });
        if (document.activeElement !== this.el) this.restore();
        document.execCommand("insertHTML", false, `<img src="${url}" data-cid="${cid}" alt="${esc(f0.name || "image")}">`);
      }
      if (others.length && this.opts.files) this.opts.files(others);  // anything else: an attachment
      this.changed();
    }
    paste(e) {
      const cd = e.clipboardData;
      if (!cd) return;
      const files = Array.from(cd.files || []).filter((f) => IMAGE_TYPES.includes(f.type));
      const html = cd.getData("text/html");
      if (files.length && !html) { e.preventDefault(); this.images(files); return; }
      if (html) {
        e.preventDefault();
        document.execCommand("insertHTML", false, cleanHTML(html));
        if (files.length && !this.el.querySelector('img[src^="data:"]')) this.images(files);
        this.tidy(); this.changed();
      }
    }
    drop(e) {
      const files = Array.from((e.dataTransfer && e.dataTransfer.files) || []);
      if (!files.length) return;
      e.preventDefault();
      const r = document.caretRangeFromPoint ? document.caretRangeFromPoint(e.clientX, e.clientY) : null;
      if (r && this.el.contains(r.startContainer)) { this.range = r; }
      this.restore();
      this.images(files);
    }
    // After any change: a pasted data: image becomes a file of the message; any other image
    // without a file of ours (dragged from a web page) goes.
    tidy() {
      let i = 0;
      for (const img of Array.from(this.el.querySelectorAll("img"))) {
        if (img.dataset.cid && (this.blobs.has(img.dataset.cid) || !img.src.startsWith("blob:"))) continue;
        const f = img.src.startsWith("data:") ? dataToFile(img.src, ++i) : null;
        if (!f) { img.remove(); continue; }
        const cid = newCid(), url = URL.createObjectURL(f);
        this.blobs.set(cid, { file: f, url });
        img.src = url; img.dataset.cid = cid;
      }
    }
    set(html, text) {
      for (const b of this.blobs.values()) URL.revokeObjectURL(b.url);
      this.blobs.clear(); this.sent.clear();
      this.el.innerHTML = html ? cleanHTML(html) : textToHTML(text);
      this.state();
    }
    setText(text) { this.set("", text); this.changed(); }
    cids() { return Array.from(this.el.querySelectorAll("img[data-cid]")).map((i) => i.dataset.cid); }
    hasImages() { return !!this.el.querySelector("img"); }
    text() { return (this.el.innerText || "").replace(/\u00a0/g, " ").replace(/\n{3,}/g, "\n\n").trim(); }
    html() {
      if (!this.text() && !this.hasImages()) return "";
      // In an inert document: an image given its cid: src there is never fetched.
      const box = document.implementation.createHTMLDocument("").body;
      box.innerHTML = this.el.innerHTML;
      for (const img of box.querySelectorAll("img")) {
        const cid = img.dataset.cid;
        if (!cid) { img.remove(); continue; }
        img.setAttribute("src", `cid:${cid}`);
      }
      return box.innerHTML;
    }
    pending() {  // [{cid, file}]: the images in the text the server doesn't have yet
      return this.cids().filter((c) => this.blobs.has(c) && !this.sent.has(c)).map((c) => ({ cid: c, file: this.blobs.get(c).file }));
    }
    saved(cids) { this.sent = new Set(cids.filter((c) => this.blobs.has(c))); }  // the draft has exactly these
    size() { return this.cids().reduce((s, c) => s + (this.blobs.has(c) ? this.blobs.get(c).file.size : 0), 0); }
  }
  window.SimexEditor = Editor;
})();
