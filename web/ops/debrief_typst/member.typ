#import "lib.typ": *
#let d = json(sys.inputs.data)
#let b = d.body
#let f = d.figures

#show: report.with(title: d.name, kicker: "Personal feedback · " + d.team,
  subtitle: if d.team_role != none { d.team_role + " · private" } else { "Private" }, generated: d.generated,
  note: [Private feedback for you alone, from the messages you wrote in SIMEX Mail. Drafted by SIMEX AI and reviewed by
    ExCon. It is not a grade and is not compared with anyone else's.])

#lead(b.headline)

#kpis((
  ("Messages written", get(f, "messages_written"), none),
  ("External", get(f, "external"), none),
  ("Inside the team", get(f, "internal"), none),
  ("Conversations", get(f, "conversations"), none),
))

= Your contribution
#paras(b.contribution)

= Strengths
#bullets(b.strengths)

= To work on
#bullets(b.to_work_on, numbered: true)

= Your writing
#paras(get(b, "writing", "assessment"))

#if get(b, "writing", "tip", default: "") != "" {
  callout[*Tip* · #b.writing.tip]
}

#if b.examples.len() > 0 {
  [= From your messages]
  for e in b.examples { example(e, "Example") }
}
