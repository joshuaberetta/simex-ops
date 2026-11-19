// Small progressive enhancements; every page works without them.
document.addEventListener("click", (e) => {
  const el = e.target.closest("[data-confirm]");
  if (el && !confirm(el.dataset.confirm)) e.preventDefault();
});

document.querySelectorAll("[data-select-all]").forEach((box) => {
  box.addEventListener("change", () => {
    box.closest("form").querySelectorAll('input[name="ids"]').forEach((c) => { c.checked = box.checked; });
  });
});

const check = document.querySelector("[data-domain-check]");
if (check) {
  check.addEventListener("click", async () => {
    const out = document.querySelector("pre.domain");
    out.hidden = false;
    out.textContent = "Checking…";
    try {
      const r = await fetch("/settings/domain");
      const d = await r.json();
      if (d.error) { out.textContent = d.error; return; }
      const lines = [`${d.name}: ${d.status}`];
      (d.records || []).forEach((x) => lines.push(`${x.status.padEnd(9)} ${x.type.padEnd(5)} ${x.name}  ${x.value}`));
      out.textContent = lines.join("\n");
    } catch (err) { out.textContent = String(err); }
  });
}

// Live clock in the exercise timezone.
const clock = document.querySelector(".clock");
if (clock) {
  const f = new Intl.DateTimeFormat("en-GB", { timeZone: clock.dataset.tz, weekday: "short", day: "2-digit",
    month: "short", hour: "2-digit", minute: "2-digit", second: "2-digit" });
  const tick = () => { clock.textContent = f.format(new Date()); };
  tick();
  setInterval(tick, 1000);
}
