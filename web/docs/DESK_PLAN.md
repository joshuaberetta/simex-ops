# Persona desk: next build steps

The persona desk is the shared SIMEX Mail mailbox from which ExCon role-players answer the
teams as the personas (`ops/desk.py`, manual 5.10, `docs/DESK_GUIDE.md`). This file lists what
to build next. The rules in `CLAUDE.md` still apply: the desk sees nothing control-side, and it
never sees grades, quality, tone or the AI-written estimate.

## Built so far (November 2026)

- **What still needs an answer** (`decb80c`). The Inbox comes in sections (`desk.states`):
  Needs attention, Probably no reply needed (Claude), Later, and the rest. Each conversation
  has a bar to take it, mark it as needing no reply, follow it up or ask Claude
  (`DeskConversation`, `/inbox/desk/mark/`). Claude reads each waiting conversation
  (`ops/desk_ai.py`, Settings `desk_triage`). ExCon gets a notice when a team has waited
  `desk_wait_minutes` (`desk.tick`).
- **Dashboard tab** (`c4fd4b9`, `/inbox/desk/dashboard/`, `ops/desk_stats.py`). It shows how
  the desk is keeping up: the tiles, an hourly strip, the teams waiting longest, the figures per
  team, per persona and per desk member, and the requests, commitments, facts and desk flags
  from `ReplyAnalysis`.

- **Assigned conversations and each person's notifications** (`DeskWatch`, `DeskNotice`).
  A conversation can be given to anyone on the desk or in ExCon (`desk.mark` `assign`), and the
  side bar has Assigned to me and Following. The Dashboard shows who has which conversations
  (`desk_stats.by_assignee`). Each person has a bell for conversations given to them, their
  follow-ups when due (`desk.tick_follow_ups`), teams writing again and others answering in
  conversations they are involved in, AI persona answers held, and teams waiting on theirs
  (`desk.notify`, `/inbox/desk/notices/`). Step 3 below can build on `assign` and `DeskWatch`.
- **Assign tab** (`ops/desk_board.py`, `views_desk_board.py`, `static/ops/desk_board.js`,
  `/inbox/desk/assign/`). A board of the open conversations in a column per person (and Not
  assigned), moved by dragging (pointer events, no library; it posts to `desk.mark`), with one
  notification per person for several moved at once. Default assignments (`DeskDefault`: a
  persona, a team or all teams, a desk login) given once to a conversation nobody has, at its
  first desk copy (`desk.auto_assign`), or by Apply now (`desk.apply_defaults`); never over a
  choice made by hand (`DeskConversation.auto_done`). This is step 3, by persona and team
  (no from/until yet: change the grid at a shift change).

## Next steps, in order

### 1. Live updates without a page reload
The Inbox sections and the Dashboard only change on a reload; the Dashboard uses a 60 s meta
refresh.
- Add a desk `v` (version) to `/poll/`, bumped by `desk.on_response`, `desk.on_delivered` and
  `desk.mark`.
- When it changes, `mail.js` refreshes the Inbox list in place (keeping the selection) and the
  sidebar's "need attention" count.
- Replace the Dashboard's meta refresh with the same poll.
- Tests: the version changes on a team message, on an answer and on a mark.

### 2. Answer-time targets per persona
Different personas answer at different speeds: the media fast, a ministry slowly.
- Add `Sender.desk_target_minutes`, empty meaning `desk_wait_minutes`. Set it on the Senders
  tab, next to the persona's colour.
- Use it in `desk.tick`'s notice, in the dashboard's "over target" and "within target" figures,
  and on the Waiting chip (red once past target).
- AI personas: give a default from `NpcProfile` timing.

### 3. Assignment by persona (shifts): built as the Assign tab's Default assignments (above)
What is left: from/until times for shifts, a "My personas" side-bar filter, pushing a new team message only to its owner.

The original plan:
With many people on the desk, give each person standing personas instead of only taking one
conversation at a time.
- Add a `DeskAssignment` (person ↔ Sender, optional from/until), set on the desk itself
  ("I play CNN, Reuters") or by ExCon on People & logins.
- A new team message to an assigned persona is taken by its player automatically
  (`DeskConversation.owner`), and only that player gets the push (`push.notify_people`).
- Add a "My personas" filter in the sidebar, and a Dashboard column showing who plays each persona.
- Unassigned personas fall back to today's behaviour.

### 4. Hand-over and shift notes
- Add a short note per conversation, visible to the desk only, for example "promised the convoy
  ETA at 15:00". It is shown in the bar and on the row's hover.
- Add an "End my shift" action: release everything I have taken and write a hand-over list of
  open conversations, follow-ups and notes, shown at the top of the Dashboard for the next shift.

### 5. Suggested answers in the reply box
- Add "Draft with Claude" in the desk's Reply. It uses the same `InjectDraft` machinery as
  ExCon's "persona's answer" (kind `answer`) and is run in a web thread. Only participant-facing
  material goes in: the conversation, the persona's `NpcProfile` knowledge if any, and the
  facts the teams stated (from the Dashboard), never control-side material.
- The draft only fills the box; a person sends it (`Message.ai_draft` / `ai_edited` already
  feed the reports).

### 6. Consistency checks across teams
Several people answer as the same persona to different teams, so answers can drift apart.
- Per persona, list what it told each team: from its Sent copies, read by Claude in the AI
  loop as "facts stated by the persona", the same shape as `ReplyAnalysis.facts`.
- On the Dashboard, show "Persona said different things": a pair of statements that contradict
  each other, for example different road status or casualty figures.
- On the conversation page, show "What this persona told the other teams" next to the reply box.

### 7. End-of-day desk summary
- Add a desk section to the ExCon report (`ops/report.py`, ExCon `Report` only, never a team's):
  figures from `desk_stats.compute`, snapshotted; the answer times per team (equity); the
  busiest personas; requests left unanswered; follow-ups never done.
- Add a "Desk" card on the ops Dashboard linking to the desk Dashboard.

### 8. Smaller items
- The sidebar's persona and team filters count conversations needing attention, not unread.
- Keyboard: `d` = No reply needed, `t` = take it, `f` = follow up, `n` = next waiting.
- Snooze a follow-up from the row's hover (15 min / 1 h).
- Network map (Dashboard → Network): colour persona nodes by how long their teams are waiting.
- Performance: `desk.states` and `desk_stats.compute` read the whole desk mailbox; cache the
  per-thread figures (keyed by the desk version from step 1) if pages get slow above about 2,000
  conversations.

## Each step must

- Keep `@_desk_page` on new desk routes (the URL walk in `tests/test_desk.py` checks them).
- Add any new model to `ops/reset.py` (Start afresh, and Clear mailboxes through
  `desk.prune`) and to `ops/backup.py`.
- Add any new way a persona answers so it leaves a desk Sent copy (`desk.on_delivered`):
  `desk.states` and the Dashboard depend on it.
- Update `docs/MANUAL.md` 5.10, `docs/DESK_GUIDE.md` and `CLAUDE.md`.
