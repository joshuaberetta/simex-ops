// Suggest a reply (ops/suggest.py): the persona desk's conversation page and ExCon's reply page.
// The panel asks for Claude's suggested answer, polls until it is written, and shows the answer
// (to use as the reply) with the notes for the desk: sources, what not to get ahead of.
(() => {
  const panel = document.querySelector("[data-suggest]");
  if (!panel) return;
  const url = panel.dataset.url, out = panel.querySelector("[data-sg-out]"), go = panel.querySelector("[data-sg-go]");
  const promptEl = panel.querySelector("[data-sg-prompt]");
  const target = panel.dataset.thread ? { thread: panel.dataset.thread } : { response: panel.dataset.response };
  const csrf = () => (document.querySelector("[name=csrfmiddlewaretoken]") || {}).value || "";
  let timer = null;

  const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };
  const list = (title, items, fmt) => {
    if (!items || !items.length) return null;
    const box = el("div", "sg-sec"); box.append(el("h4", null, title));
    const ul = el("ul"); items.forEach((x) => { const li = el("li"); fmt ? fmt(li, x) : (li.textContent = x); ul.append(li); });
    box.append(ul); return box;
  };

  const useIt = (s) => {
    const body = s.body.body || "";
    if (panel.dataset.mode === "desk" && window.simexReplyWith) { window.simexReplyWith(String(s.mail || ""), body); return; }
    const ta = document.querySelector("#answer textarea[name=body]");
    if (ta) {
      ta.value = body; ta.focus(); ta.scrollIntoView({ block: "center", behavior: "smooth" });
    }
  };

  const render = (s) => {
    out.replaceChildren();
    go.disabled = false;
    if (!s) return;
    if (s.status === "running") {
      go.disabled = true;
      out.append(el("p", "sg-wait", "SIMEX AI is looking things up and writing a suggestion… (usually under a minute)"));
      clearTimeout(timer); timer = setTimeout(load, 3000, s.id);
      return;
    }
    if (s.status === "failed") { out.append(el("p", "sg-err", `The suggestion failed: ${s.error}`)); return; }
    const b = s.body || {};
    out.append(el("p", "sg-meta", `Suggested ${s.at}${s.user ? ` for ${s.user}` : ""}${s.prompt ? ` · asked: “${s.prompt}”` : ""}. Read it before you send it.`));
    if (b.warnings && b.warnings.length) out.append(list("Check before sending", b.warnings));
    if (b.asks) out.append(el("p", "sg-asks", `They ask: ${b.asks}`));
    const ans = el("div", "sg-answer");
    ans.append(el("pre", "sg-body", b.body || ""));
    const acts = el("div", "sg-acts");
    const use = el("button", "sg-btn primary", panel.dataset.mode === "desk" ? "Reply with this" : "Put it in the answer");
    use.type = "button"; use.onclick = () => useIt(s);
    const copy = el("button", "sg-btn", "Copy"); copy.type = "button";
    copy.onclick = async () => { try { await navigator.clipboard.writeText(b.body || ""); copy.textContent = "Copied"; } catch (e) { copy.textContent = "Copy failed"; } };
    acts.append(use, copy); ans.append(acts); out.append(ans);
    const hb = list("Don't get ahead of (still to come for this team)", b.hold_back, (li, h) => {
      li.append(el("b", null, h.what)); li.append(document.createTextNode(` · ${h.when || ""} `)); li.append(el("small", "sg-src", `${h.source}${h.label ? `: ${h.label}` : ""}`));
    });
    [list("Corrections", b.corrections), list("Not in the package", b.not_available), hb].forEach((x) => x && out.append(x));
    if (b.notes) { const n = el("div", "sg-sec"); n.append(el("h4", null, "Notes")); n.append(el("p", null, b.notes)); out.append(n); }
    const q = list("To decide", b.questions); if (q) out.append(q);
    const facts = list("What the answer rests on", b.facts, (li, f) => {
      li.className = `sg-fact ${f.state}`; li.append(document.createTextNode(`${f.fact} `)); li.append(el("small", "sg-src", `${f.source}: ${f.label}`));
    });
    if (facts) { const d = el("details", "sg-sec"); d.append(el("summary", null, `What the answer rests on (${b.facts.length})`)); d.append(facts.querySelector("ul")); out.append(d); }
  };

  async function load(id) {
    const q = id ? `${url}${id}/` : `${url}?${new URLSearchParams(target)}`;
    try {
      const r = await fetch(q, { headers: { "x-requested-with": "fetch" }, credentials: "same-origin" });
      if (!r.ok) return;
      const j = await r.json();
      if (!j.ai) { go.disabled = true; go.title = "AI analysis is off (Settings → AI analysis)"; }
      render(j.suggestion);
    } catch (e) { /* offline: try again on the next click */ }
  }

  go.addEventListener("click", async () => {
    go.disabled = true;
    const fd = new FormData();
    Object.entries(target).forEach(([k, v]) => fd.append(k, v));
    fd.append("prompt", promptEl ? promptEl.value : "");
    fd.append("csrfmiddlewaretoken", csrf());
    const r = await fetch(url, { method: "POST", body: fd, headers: { "x-requested-with": "fetch", "X-CSRFToken": csrf() }, credentials: "same-origin" });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) { go.disabled = false; out.replaceChildren(el("p", "sg-err", j.error || "Could not ask for a suggestion.")); return; }
    render(j.suggestion);
  });
  load();
})();
