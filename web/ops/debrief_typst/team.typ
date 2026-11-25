#import "lib.typ": *
#let d = json(sys.inputs.data)
#let b = d.body
#let f = d.figures
#let c = d.comms
#let states = ("replied": "Answered", "late": "Late", "missing": "No reply", "waiting": "Waiting",
  "none": "No reply asked")

#show: report.with(title: d.team, kicker: "Team debrief", subtitle: "SIMEX end-of-exercise report",
  generated: d.generated,
  note: [Written for the team, for the hotwash. The figures are computed by ExCon's system from what happened in
    SIMEX Mail. The text was drafted by SIMEX AI from every message the team wrote and was reviewed by ExCon.
    Team members are numbered, not named.])

#lead(b.headline)

#kpis((
  ("Injects received", get(f, "delivery", "injects_delivered"), none),
  ("Replies sent / expected", pair(get(f, "replies", "answered"), get(f, "replies", "replies_expected")), none),
  ("Median reply time", get(f, "time_to_reply", "median_minutes_from_delivery"), "minutes from delivery"),
  ("Messages written", get(c, "messages", "written_by_team"),
    [#fmt(get(c, "messages", "external")) external · #fmt(get(c, "messages", "internal")) internal]),
))

= Overview
#paras(b.overview)

= Timeliness
#paras(b.timeliness)

#let rows = get(f, "injects", default: ())
#if rows.len() > 0 {
  tbl(("Inject", "Subject", "Critical", "Reply", "Minutes", "Score"),
    rows.map(r => (r.inject, r.subject, if r.critical [Yes] else [], states.at(r.state, default: r.state),
      r.minutes, if r.score != none [#r.score / #r.max] else [])),
    columns: (auto, 1fr, auto, auto, auto, auto))
}

= What went well
#bullets(b.strengths)

= What to improve
#bullets(b.improvements)

= Critical points missed
#if b.critical_missed.len() == 0 { nothing(msg: "None.") } else {
  for m in b.critical_missed {
    flag((kind: "other", detail: [*#m.inject* · #m.what], evidence: m.evidence))
  }
}

= Communication with external parties
#paras(get(b, "external_communication", "assessment"))

#let aud = get(c, "by_audience", default: ()).filter(a => a.key not in ("team_leader", "teammates"))
#if aud.len() > 0 {
  [== Messages by audience]
  bars(aud.map(a => (a.audience, a.messages, if a.register_avg != none [register #a.register_avg/5] else [])))
  v(6pt)
  tbl(("Audience", "Messages", "Register", "Clarity", "With issues", "Most common issues"),
    aud.map(a => (a.audience, a.messages, chip(a.register_avg), chip(a.clarity_avg), a.with_issues,
      a.issues.keys().join(", ", default: ""))),
    columns: (auto, auto, auto, auto, auto, 1fr))
}

#for a in get(b, "external_communication", "by_audience", default: ()) {
  [== #a.audience]
  paras(a.assessment)
}

#example(get(b, "external_communication", "good_example"), "A message to keep")
#example(get(b, "external_communication", "weak_example"), "A message to improve")

= Inside the team
#kpis((
  ("Internal messages", get(c, "messages", "internal"), none),
  ("By the team leader", get(c, "messages", "by_leader"), none),
  ("Forwards", get(c, "messages", "forwards"), none),
  ("Leader's median answer", get(c, "leader", "median_minutes_to_answer"), "minutes, to members"),
))

== Coordination
#paras(b.internal_coordination)

== Leadership
#paras(b.leadership)

== Information management
#paras(b.information_management)

= Points to note
#flags(b.flags)

#if d.include_ai and get(c, "ai_estimate") != none {
  [= AI-written estimate]
  callout[An estimate by SIMEX AI of which messages and documents were written with an AI assistant, from signals
    such as structure, boilerplate and writing speed. It is not a detection and can be wrong; it is here to start a
    conversation about how AI tools were used.]
  let e = c.ai_estimate
  kpis((("Messages assessed", e.messages_assessed, none), ("Likely AI-assisted", e.likely_ai_assisted, none),
        ("Possibly AI-assisted", e.possibly_ai_assisted, none), ("Documents flagged", e.documents.len(), none)))
  if e.messages.len() > 0 {
    v(6pt)
    tbl(("Message", "Inject", "Estimate", "Signals"), e.messages.map(m => (m.id, m.inject, m.band, m.signals)),
      columns: (auto, auto, auto, 1fr))
  }
}

= Lessons for your team
#bullets(b.lessons, numbered: true)

= Recommendations
#bullets(b.recommendations)

= Questions for the debrief
#bullets(b.hotwash_questions, numbered: true)
