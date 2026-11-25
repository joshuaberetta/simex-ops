// What's new (ops/whatsnew.py): the notes in a notifications drawer. With something new the
// drawer stays closed: a small star on its button (data-wn-dot) says so, and opening the
// drawer marks the notes seen (POST data-wn-url), which takes the star away. A drawer of its own (data-wn-bell: SIMEX Mail
// mailboxes without the desk's bell, the role players' pages) opens and closes here; the
// desk's and ExCon's are their own scripts', opened by clicking their button.
(function () {
  "use strict";
  const sec = document.querySelector("[data-wn]");
  if (!sec) return;
  const csrf = () => (document.cookie.match(/(?:^|;\s*)csrftoken=([^;]+)/) || [])[1] || (document.querySelector('[name="csrfmiddlewaretoken"]') || {}).value || "";
  const toggle = document.querySelector(sec.dataset.wnToggle);
  const bell = sec.closest("[data-wn-bell]");
  const panel = bell ? bell.querySelector("[data-wn-panel]") : null;
  const dot = document.querySelector("[data-wn-dot]");
  if (bell && toggle) {
    const open = (on) => { panel.hidden = !on; toggle.setAttribute("aria-expanded", on ? "true" : "false"); };
    toggle.addEventListener("click", (e) => { e.stopPropagation(); open(panel.hidden); });
    document.addEventListener("click", (e) => { if (!panel.hidden && !e.target.closest("[data-wn-bell]")) open(false); });
    document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !panel.hidden) open(false); });
  }
  const isOpen = () => { const p = panel || sec.closest("[hidden], .gm-dn-panel, .notices"); return p && !p.hidden; };
  let told = false;
  const seen = () => {
    if (told) return;
    told = true;
    if (dot) dot.hidden = true;
    const keys = Array.from(sec.querySelectorAll(".wn-note.new")).map((n) => n.dataset.wnKey);
    if (!keys.length) return;
    const body = new URLSearchParams(keys.map((k) => ["key", k]));
    fetch(sec.dataset.wnUrl, { method: "POST", body, credentials: "same-origin", headers: { "X-CSRFToken": csrf(), "X-Requested-With": "fetch" } }).catch(() => {});
  };
  if (toggle) toggle.addEventListener("click", () => setTimeout(() => { if (isOpen()) seen(); }, 0));
})();
