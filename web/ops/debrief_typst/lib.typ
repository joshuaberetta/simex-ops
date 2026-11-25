// Shared look of the debrief reports (ops/debrief.py, manage.py debrief render).
// Every value from the JSON is shown as text (never evaluated as markup): participants' words
// are untrusted input.

#let accent = rgb("#0b4f8a")
#let accent-soft = rgb("#eaf1f8")
#let ink = rgb("#1b2430")
#let muted = rgb("#5f6b7a")
#let rule = rgb("#d5dce4")
#let amber = rgb("#b45309")
#let red = rgb("#b42318")
#let green = rgb("#1e7a46")
#let sans = ("Avenir Next", "Helvetica Neue", "Helvetica", "Arial", "Libertinus Serif")
#let serif = ("Charter", "Georgia", "Libertinus Serif")

// d.a.b with a default when any step is missing or none.
#let get(d, ..keys, default: none) = {
  let v = d
  for k in keys.pos() {
    if type(v) == dictionary and k in v { v = v.at(k) }
    else if type(v) == array and type(k) == int and k < v.len() { v = v.at(k) }
    else { return default }
  }
  if v == none { default } else { v }
}

#let nothing(msg: "Nothing to report.") = text(fill: muted, style: "italic", msg)

#let fmt(x, suffix: "") = {
  if x == none or x == "" { text(fill: muted)[—] }
  else if type(x) == content { x }
  else if type(x) == float { [#str(calc.round(x, digits: if calc.abs(x) >= 10 { 0 } else { 1 }))#suffix] }
  else { [#str(x)#suffix] }
}

#let pair(a, b, sep: " / ") = if a == none and b == none { text(fill: muted)[—] } else { [#fmt(a)#sep#fmt(b)] }

#let paras(s) = {
  if s == none or s == "" { nothing() } else {
    for p in s.split("\n\n") { [#p.trim()]; parbreak() }
  }
}

#let bullets(xs, numbered: false, empty: "None.") = {
  let xs = if xs == none { () } else { xs.filter(x => x != none and x != "") }
  if xs.len() == 0 { nothing(msg: empty) }
  else if numbered { enum(..xs.map(x => [#x])) }
  else { list(..xs.map(x => [#x])) }
}

#let lead(s) = if s != none and s != "" {
  block(above: 1.2em, below: 1.2em, text(13pt, font: sans, weight: "medium", fill: ink, s))
}

#let kpis(items) = {
  let n = calc.min(calc.max(items.len(), 1), 4)
  grid(columns: (1fr,) * n, gutter: 8pt,
    ..items.map(((label, value, note)) => block(fill: accent-soft, radius: 4pt, inset: 10pt, width: 100%, {
      set text(font: sans, hyphenate: false)
      set par(justify: false)
      text(7.5pt, fill: muted, weight: "bold", tracking: 0.04em, upper(label))
      linebreak()
      text(17pt, weight: "bold", fill: accent, fmt(value))
      if note != none and note != "" { linebreak(); text(8pt, fill: muted, note) }
    })))
}

// Horizontal bars: rows of (label, value, note).
#let bars(rows, max: none, unit: "", colour: accent) = {
  if rows.len() == 0 { return nothing() }
  let vals = rows.map(r => if r.at(1) == none { 0 } else { r.at(1) })
  let m = if max != none { max } else { calc.max(1, ..vals) }
  set text(font: sans, size: 8.5pt)
  grid(columns: (32%, 1fr, 22%), row-gutter: 5pt, column-gutter: 8pt,
    align: (right + horizon, left + horizon, left + horizon),
    ..rows.map(((label, value, note)) => {
      let v = if value == none { 0 } else { value }
      (label,
       box(width: 100%, height: 9pt, {
         place(rect(width: 100%, height: 9pt, fill: luma(238), radius: 2pt))
         place(rect(width: calc.min(v / m, 1) * 100%, height: 9pt, fill: colour, radius: 2pt))
       }),
       text(fill: muted)[#fmt(value)#unit#if note != none and note != "" [ · #note]])
    }).flatten())
}

#let tbl(header, rows, columns: auto, size: 8.5pt) = {
  set text(font: sans, size: size)
  table(columns: if columns == auto { (1fr,) + (auto,) * (header.len() - 1) } else { columns },
    stroke: none, inset: (x: 5pt, y: 4pt),
    fill: (_, y) => if y == 0 { accent } else if calc.even(y) { luma(246) },
    table.header(..header.map(h => text(fill: white, weight: "bold", h))),
    ..rows.flatten().map(c => if c == none { text(fill: muted)[—] } else if type(c) == content { c } else { [#str(c)] }))
}

// A 1-5 rating as a coloured chip.
#let score-fill(v) = if v == none { luma(240) } else if v < 2.5 { rgb("#fbe3e0") } else if v < 3.5 { rgb("#fdf0d5") } else { rgb("#dff1e6") }
#let chip(v) = box(fill: score-fill(v), inset: (x: 5pt, y: 2pt), radius: 3pt, fmt(v))

#let flag-colour(kind) = if kind in ("safety", "conduct") { red } else if kind in ("register", "language", "accuracy") { amber } else { muted }
#let flag-names = (register: "Tone and register", language: "Language", accuracy: "Accuracy", safety: "Safety",
  conduct: "Conduct", ai_estimate: "AI-written estimate", other: "Other")

#let flag(f, team: none) = block(width: 100%, inset: (left: 10pt, rest: 8pt), below: 6pt,
  stroke: (left: 3pt + flag-colour(f.kind)), fill: luma(250), {
    text(7.5pt, font: sans, weight: "bold", fill: flag-colour(f.kind), upper(flag-names.at(f.kind, default: f.kind)))
    if team != none { text(7.5pt, font: sans, weight: "bold")[ · #team] }
    linebreak()
    f.detail
    if get(f, "evidence", default: "") != "" { linebreak(); text(9pt, fill: muted, style: "italic", f.evidence) }
  })

#let flags(xs, team-key: none) = if xs == none or xs.len() == 0 { nothing(msg: "Nothing that needs flagging.") } else {
  for f in xs { flag(f, team: if team-key != none { get(f, team-key) } else { none }) }
}

#let example(e, title) = if e != none and get(e, "quote", default: "") != "" {
  block(width: 100%, fill: accent-soft, inset: 10pt, radius: 4pt, below: 8pt, {
    text(7.5pt, font: sans, weight: "bold", fill: accent, upper(title))
    h(1fr)
    text(8pt, font: sans, fill: muted, get(e, "ref", default: ""))
    linebreak()
    text(style: "italic")["#e.quote"]
    if get(e, "why", default: "") != "" { linebreak(); text(9pt, e.why) }
  })
}

#let callout(body) = block(width: 100%, inset: 10pt, radius: 4pt, stroke: 0.5pt + rule, below: 10pt,
  text(8.5pt, font: sans, fill: muted, body))

#let report(title: "", kicker: "", subtitle: "", generated: "", note: none, body) = {
  set document(title: kicker + ": " + title)
  set page(paper: "a4", margin: (x: 2.1cm, top: 2.3cm, bottom: 2.2cm),
    header: context { if here().page() > 1 { set text(7.5pt, font: sans, fill: muted); [#title #h(1fr) #kicker] } },
    footer: context { set text(7.5pt, font: sans, fill: muted); [SIMEX debrief · #generated #h(1fr) #counter(page).display("1 / 1", both: true)] })
  set text(font: serif, size: 10.5pt, fill: ink, lang: "en")
  set par(leading: 0.66em, spacing: 1em, justify: true)
  set list(marker: text(fill: accent)[•], spacing: 0.75em)
  set enum(spacing: 0.75em)
  show heading: set text(font: sans, fill: accent)
  show heading.where(level: 1): it => block(above: 1.7em, below: 0.8em, width: 100%,
    stroke: (bottom: 0.5pt + rule), inset: (bottom: 5pt), text(13.5pt, weight: "bold", it.body))
  show heading.where(level: 2): it => block(above: 1.2em, below: 0.55em, text(10.5pt, weight: "bold", fill: ink, it.body))

  block(width: 100%, fill: accent, radius: 6pt, inset: (x: 18pt, y: 20pt), {
    set text(fill: white, font: sans)
    text(8pt, tracking: 0.14em, weight: "bold", upper(kicker))
    v(4pt)
    text(24pt, weight: "bold", title)
    if subtitle != "" { linebreak(); text(10.5pt, subtitle) }
  })
  if note != none { v(4pt); callout(note) }
  body
}
