/* The app drawer (ops/mail/_apps.html): the grid button opens SIMEX's apps for this login. A click
   outside or Escape closes it (focus back on the button); the arrow keys move between the tiles;
   on a phone it is a sheet across the screen. Used by SIMEX Mail and the role players' pages. */
(function () {
  "use strict";
  const root = document.querySelector("[data-apps]");
  if (!root) return;
  const btn = root.querySelector("[data-apps-toggle]"), panel = root.querySelector("[data-apps-panel]");
  const tiles = () => Array.from(panel.querySelectorAll(".sx-app"));
  const cols = () => {
    const t = tiles();
    if (t.length < 2) return 1;
    const top = t[0].getBoundingClientRect().top;
    const n = t.findIndex(x => x.getBoundingClientRect().top > top + 2);
    return n < 0 ? t.length : n;
  };

  function open(on) {
    panel.hidden = !on;
    btn.setAttribute("aria-expanded", on ? "true" : "false");
    root.classList.toggle("open", on);
    if (on) (panel.querySelector(".sx-app.on") || tiles()[0] || panel).focus();
  }

  btn.addEventListener("click", (e) => { e.stopPropagation(); open(panel.hidden); });
  panel.querySelector("[data-apps-close]").addEventListener("click", () => { open(false); btn.focus(); });
  // A click outside closes it; on a phone, so does one on the backdrop (the root's ::before).
  document.addEventListener("click", (e) => { if (!panel.hidden && (!root.contains(e.target) || e.target === root)) open(false); });
  document.addEventListener("keydown", (e) => {
    if (panel.hidden) return;
    if (e.key === "Escape") { e.preventDefault(); open(false); btn.focus(); return; }
    const t = tiles(), i = t.indexOf(document.activeElement);
    if (i < 0) return;
    const step = { ArrowRight: 1, ArrowLeft: -1, ArrowDown: cols(), ArrowUp: -cols(), Home: -i, End: t.length - 1 - i }[e.key];
    if (step === undefined) {
      if (e.key === "Tab") {  // keep the focus in the drawer while it is open
        const first = t[0], last = t[t.length - 1];
        if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
        else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
      }
      return;
    }
    e.preventDefault();
    const j = i + step;
    if (j >= 0 && j < t.length) t[j].focus();
  });
})();
