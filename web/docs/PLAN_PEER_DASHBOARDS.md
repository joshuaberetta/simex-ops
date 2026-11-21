# Plan: the other teams' Dashboards for team leaders

Status: not built (planned 2026-11-21). One decision is open (below). Delete this file once
it's built and MANUAL.md, LEADER_GUIDE.md and CLAUDE.md describe it.

## What it is

Team leaders can already read the other team leaders' mailboxes, read-only (Team leaders
tab, `/inbox/leaders/<team id>/`). This plan does the same for the Dashboard, so a leader
can see how the other teams are doing. It adds **no new top-level tab**. The Dashboard tab
gets sub-tabs instead: **Our team · Team B · Team C …**. The leader's own team is first and
is the default.

## Routes (`ops/urls.py`)

- `/inbox/dashboard/` stays the leader's own team.
- New: `/inbox/dashboard/<int:as_team>/` is another team's Dashboard, read-only. It is a 404
  for the leader's own team, for an inactive team, and across the exercise/Test divide. That
  divide is the same rule as `views_inbox.peer_teams`: the Test team only for the Test
  team's leader, and the other way round.

## View (`ops/views_leader.py`)

- Move the body of `dashboard()` into a helper `_dashboard(request, lead, team, peer=False)`.
  `dashboard(request, as_team=None)` picks the team and checks it, then calls the helper.
- It stays `@leader_required`, so the test that walks the URLconf covers the new route.
- It keeps `leader_tabs(lead, "dashboard")` so the top tab stays on Dashboard. It adds
  `dash_tabs`: the own team first, then the other teams.
- Which teams appear: **every other active team of the same kind** (exercise or Test),
  including a team that has no leader, because it still has a dashboard. `peer_teams` only
  lists teams that have a leader, so this needs a sibling query, e.g. `dashboard_teams(leader)`.
- In the peer case, filter or replace the context per the open decision below.

## Template (`templates/ops/mail/dashboard.html`)

- A sub-tab strip at the top of `.gm-dash`, styled like the leader tab bar.
- A peer page gets a read-only note, like the peer mailbox's `gm-ro-note` /
  `gm-ro-bar`: "{{ team.name }}'s Dashboard, read-only."
- No `/inbox/team/t/...` links on a peer page. They would 403, because a leader never reads
  another team's inbox.

## Open decision: what a peer page shows

The leader's own Dashboard includes material from the team's inbox. A leader of another
team is never allowed to see that material today:

- links to reply threads
- the text of replies ("The reply" in Possibly AI-written)
- members' names (Who is writing, the AI-written list)
- Claude's per-reply summaries (Flags)
- the asks and commitments extracted from replies.

**Recommended: a status view.**
- **Show:** the reply cards (messages written, injects answered, median first reply, late or
  missing), the delivery → read → answered funnel, replies per hour, and Claude's overall
  cards (replies analysed, mean quality, tone mix, AI-assisted counts).
- **Show, with members numbered:** "Who is writing", as "Member 1, 2…" in the manner of
  `report.for_team`, never names.
- **Hide:** Possibly AI-written, Flags, What the team is asking for, and Decisions and
  commitments.

**Alternative:** show every section, but members numbered, no thread links and no reply
text.

## Docs

- `docs/MANUAL.md`: in §4, the leaders' four tabs ("…and the team's **Dashboard**", plus the
  other teams' as sub-tabs). In §5.7, the "Team leaders see Claude's reading of their own
  team's replies" bullet: say what they see of other teams.
- `docs/LEADER_GUIDE.md` (served as the leader's guide under **?**): the Dashboard section.
- `CLAUDE.md`: the sentence on leaders' views ("…and their team's Dashboard
  (`ops/views_leader.py`)") gains the other teams' Dashboards, read-only, at
  `/inbox/dashboard/<team id>/`.

## Tests (`ops/tests/test_leaders.py`)

- `dash_tabs` lists the own team first, then the other active teams of the same kind. A
  team without a leader is included. The Test team is kept apart in both directions.
- `/inbox/dashboard/<own team id>/`, an inactive team and the other kind of team are 404s.
- Members and ExCon get 403 on the new route.
- A peer page contains no member names, no reply text and no `/inbox/team/` links. Seed a
  reply with a distinctive name and text, then assert they are absent.
- The own Dashboard is unchanged: the existing tests still pass.
