#import "lib.typ": *
#let d = json(sys.inputs.data)
#let b = d.body
#let f = d.figures

#show: report.with(title: d.team + " · Team leader", kicker: "Team leader debrief",
  subtitle: "For " + d.name + " · private", generated: d.generated,
  note: [A private debrief for the team leader, from how the team was led in SIMEX Mail. The figures are computed by
    ExCon's system; the text was drafted by SIMEX AI and reviewed by ExCon. Team members are numbered, not named.])

#lead(b.headline)

#kpis((
  ("Messages written", get(f, "messages_written"), none),
  ("To the team", get(f, "to_team"), none),
  ("External", get(f, "external"), [register #fmt(get(f, "register_avg_external"))/5]),
  ("Median answer to members", get(f, "median_minutes_to_answer"),
    [minutes · #fmt(get(f, "unanswered_in_thread")) left unanswered]),
))

= Overview
#paras(b.overview)

= Tasking and delegation
#paras(b.tasking)

= Information flow
#paras(b.information_flow)

= Decisions
#paras(b.decisions)

= Representing the team externally
#paras(b.external_representation)

= Responsiveness to the team
#paras(b.responsiveness)

= Strengths
#bullets(b.strengths)

= To develop
#bullets(b.development)

= Points to note
#flags(b.flags)

= Lessons
#bullets(b.lessons, numbered: true)
