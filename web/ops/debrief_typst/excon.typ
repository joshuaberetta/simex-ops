#import "lib.typ": *
#let d = json(sys.inputs.data)
#let b = d.body
#let f = d.figures
#let c = d.comms
#let areas = (design: "Exercise design", injects: "Injects", personas_desk: "Personas and the desk", timing: "Timing and load",
  platform: "Platform and tools", assessment: "Assessment", other: "Other")
#let teams = get(f, "teams", default: ())

#show: report.with(title: "After-action report", kicker: "ExCon · restricted", subtitle: "SIMEX end-of-exercise report",
  generated: d.generated,
  note: [For ExCon and the exercise director, not participants. Figures computed by ExCon's system from every
    mailbox; text drafted by SIMEX AI. AI-written figures are estimates, not findings.])

#lead(b.headline)

#kpis((
  ("Teams", teams.len(), none),
  ("Injects sent", get(f, "injects", default: ()).len(), none),
  ("Messages by the teams", c.values().map(x => x.messages.written_by_team).sum(default: 0), none),
  ("ExCon's median answer", get(f, "excon", "median_minutes_to_answer"), "minutes"),
))

= Overview
#paras(b.overview)

= Teams compared
#tbl(("Team", "Replies / expected", "Median min", "Grade %", "Written", "External", "Register", "Likely AI"),
  teams.map(t => {
    let k = c.at(t.team, default: none)
    (t.team, pair(t.replies.answered, t.replies.replies_expected), t.time_to_reply.median_minutes_from_delivery,
     t.grade_percent, get(k, "messages", "written_by_team"), get(k, "messages", "external"),
     chip(if k == none { none } else {
       let regs = k.by_audience.filter(a => a.key not in ("team_leader", "teammates") and a.register_avg != none)
       if regs.len() == 0 { none } else { calc.round(regs.map(a => a.register_avg * a.messages).sum() / regs.map(a => a.messages).sum(), digits: 1) }
     }),
     get(k, "ai_estimate", "likely_ai_assisted"))
  }))

#for t in b.comparison {
  [== #t.team]
  paras(t.summary)
}

= Themes
#for t in b.themes {
  [== #t.title]
  paras(t.detail)
}

= Lessons for ExCon
#for (key, label) in areas {
  let ls = b.lessons_for_excon.filter(l => l.area == key)
  if ls.len() > 0 {
    [== #label]
    for l in ls {
      block(below: 8pt, { l.lesson; if l.evidence != "" { linebreak(); text(9pt, fill: muted, style: "italic", l.evidence) } })
    }
  }
}

= Lessons for the teams
#for l in b.lessons_for_teams {
  block(below: 8pt, { strong(l.lesson); if l.evidence != "" { linebreak(); text(9pt, fill: muted, l.evidence) } })
}

= Communication with external parties
#let auds = ()
#for (_, k) in c { for a in k.by_audience { if a.key not in ("team_leader", "teammates") and a.audience not in auds { auds.push(a.audience) } } }
#if auds.len() > 0 {
  [== Register by team and audience (1–5)]
  tbl(("Team",) + auds, c.pairs().map(((team, k)) => (team,) + auds.map(name => {
    let a = k.by_audience.find(x => x.audience == name)
    if a == none { [] } else { [#chip(a.register_avg) #text(7pt, fill: muted)[#a.messages]] }
  })))
  text(8pt, fill: muted)[Average register (tone, formality and courtesy for the recipient) of the teams' messages to
    each audience; the small figure is the number of messages.]
}
#for a in b.communication_by_audience {
  [== #a.audience]
  paras(a.finding)
}

= Flags
#flags(b.flags, team-key: "team")

= AI-written estimate
#callout[SIMEX AI's estimate of AI-assisted writing, per message and document, in bands (likely ≥ 70, possibly ≥ 40).
  Signals are structure, boilerplate and writing speed; non-native English and good writing are not signals. Treat it
  as a pattern to discuss, never as evidence about a person.]
#tbl(("Team", "Messages assessed", "Likely", "Possibly", "Documents flagged"),
  c.pairs().map(((team, k)) => (team, get(k, "ai_estimate", "messages_assessed"), get(k, "ai_estimate", "likely_ai_assisted"),
    get(k, "ai_estimate", "possibly_ai_assisted"), get(k, "ai_estimate", "documents", default: ()).len())))

= Injects
#tbl(("Inject", "Subject", "Critical", "Teams", "Answered", "Late / missing", "Median min", "Grade %"),
  get(f, "injects", default: ()).map(i => (i.inject, i.subject, if i.critical [Yes] else [], i.teams, i.answered,
    i.late_or_missing, i.median_minutes, i.mean_grade_percent)),
  columns: (auto, 1fr, auto, auto, auto, auto, auto, auto))

== Injects that worked
#bullets(b.injects_that_worked)

== Injects to revise
#bullets(b.injects_to_revise)

= Recommendations for the next SIMEX
#bullets(b.recommendations, numbered: true)
