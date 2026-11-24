# SIMEX Calendar and the app drawer: build plan

Teams arrange meetings by email all through the exercise: a coordination meeting at the OSOCC,
a call with the Ministry, a VTC with the Resident Coordinator, a meeting between two teams. Today
those arrangements stay in the mail threads. Nobody, the persona desk included, can see what was
agreed, who is meant to be there or what comes next.

SIMEX Calendar is a second app in SIMEX Mail, in the style of Google Calendar. Every account
(members, team leaders, the persona desk, ExCon viewing as a team) can schedule a meeting, invite
people, teams and personas, answer invitations and get reminders. It opens from a new **app
drawer** in the SIMEX Mail header (the grid of dots).

The rules in `CLAUDE.md` still apply:

- Teams never cross.
- Members see only simulation time.
- Nothing control-side is shown.
- Every query is filtered by the viewer's mailbox.

## Decisions

1. **A calendar belongs to a mailbox.** It uses the same `(team, owner)` key as `Mail` and
   `MailLabel`, so `Viewer` scopes it the same way and teams never cross:
   - the team calendar (team, None), shared by the members like the team inbox
   - a member's own calendar (team, member), with `personal_mailboxes`
   - a team leader's own calendar (team, leader)
   - the persona desk's calendar (None, None), one layer per persona
2. **An invitation is an email.** As in Gmail, inviting someone puts a mail in their mailbox
   with an invitation card (Yes / Maybe / No). Answering sends "Accepted: …" back to the
   organiser. Because it is ordinary `Mail`, everything already built keeps working:
   - push notifications
   - the persona desk's copies and sections, which show a team's invitation to a persona as
     waiting
   - the Network
   - `MailAction` and the reports
   - `sim_at`
   - withdrawing and clearing

   The calendar is a view over events, and the invitation mail is how events travel.
3. **Times are real, with the scenario time shown too** (decided: "real time but show scenario
   time too"). An event is stored at its real time, naive UTC like the rest of the DB, entered
   and shown in `EXERCISE_TZ`. Its scenario time is computed from the real time by
   `simclock.at(real)`, and `simclock.real_at(sim)` is the inverse. Applied this way:
   - Team leaders, the persona desk and ExCon see the real time, with the scenario time beside
     it (◷).
   - Members keep the `CLAUDE.md` rule and never see a real date in SIMEX Mail. When the
     simulation clock is on, they see and enter the scenario time only, converted back to real
     time on save. When it is off, everyone sees real time.
   - Reminders run on real time.
   - If ExCon moves the clock, an event keeps its real time and its scenario time moves with
     the clock.
   - With the clock stopped (speed 0), events are placed as if it ran at 1× from where it
     stopped, so they keep their length and order.
4. **Who may invite whom is the address book** (`views_inbox.address_book` / `contacts` /
   `lookup`). It is checked on save, exactly as on send. Nothing new to configure:
   - a member: the personas, their teammates, their own team, and other teams only with
     `team_to_team` (decided: team-to-team meetings follow that setting)
   - a leader: their team and its members
   - the desk: every team, member and leader
5. **Personas attend through the desk, always** (decided). A persona invited by a team gets a
   pending invitation on the desk's calendar, plus the desk Inbox copy. A desk member accepts
   or declines as that persona (`desk.send` with the answer). AI personas never accept
   invitations by themselves: their invitations wait for the desk, like a held `NpcReply`.
   There is no auto-accept setting.
6. **Organisation shown.** Each persona attendee is shown by name and organisation. The
   organisation is a new participant-facing field, `Sender.organisation` (for example
   "Ministry of Interior"). It is set on the Senders tab and in the senders import/export.
   `Sender.category` stays ExCon-only.
7. **No AI for participants.** SIMEX AI never proposes or creates events for members or leaders,
   because arranging meetings is part of what is being exercised. For the desk, "Add to
   calendar" suggestions from a team's email are a later, optional step (step 6).
8. **The calendar is in-app only at first.** When injects go by email (`channel_mode` email),
   participants don't use SIMEX Mail, so step 7 adds `.ics` invitations to the email channel.
9. **Leaders see members' own calendars read-only** (decided), as they read members' own
   mailboxes.

## Data

```
CalendarEvent
  uid            short random id; also the invitation thread (Mail.thread)
  team, owner    the organiser's mailbox (None, None = the desk)
  organiser_key  address-book key: t:<id> | m:<pid> | l:<pid> | p:<username>
  organiser_name as shown
  title, location, description          participant-facing text, capped
  mode           in_person | phone | vtc | other
  starts_at, ends_at, all_day           real time (decision 3)
  status         confirmed | cancelled
  sequence       bumped on every change that matters to attendees (time, place, cancel)
  source_mail    FK Mail, null: "made from this email"
  seeded         FK CalendarSeed, null: found in the mail by SIMEX AI (step 3)
  created_by     Person, null (ExCon from Compose / desk staff)
  is_test        the Test team's
  created_at, updated_at

CalendarAttendee                    one row per guest; the organiser has one too (role organiser)
  event
  key            address-book key (as above)
  team, owner    the mailbox whose calendar shows it (None, None for a persona = the desk)
  persona        username, for p: keys
  person         the member / leader, if any
  name, addr     as shown at the time
  role           organiser | required | optional
  response       needs_action | accepted | tentative | declined
  responded_at, responded_by (Person)
  comment        their note with the answer
  remind_minutes None = conf cal_lead (10), per attendee
  reminded_at    reminder fired (reset when the event moves)
  hidden         removed from this calendar (declined and hidden)

Mail.event       FK CalendarEvent, null                      (step 2)
Mail.invite      "" | request | update | cancel | reply       (step 2: the invitation card)
```

The view (Day, Week, Month, Schedule) and the calendars ticked on the left are kept in the
browser (`localStorage`), so no preferences model is needed.

Notes:

- A calendar's events are its mailbox's `CalendarAttendee` rows. A team calendar shows the
  events where the team (`t:`) is a guest or the organiser. A member's calendar shows their
  `m:` rows. That is the query, always through `calendar.layers(v)` / `Viewer.events()`.
- A member's Calendar page shows **both** of their calendars (team and mine) as two layers
  with checkboxes, like Google's "My calendars". A leader's page shows their own calendar, plus
  the team calendar and the members' own read-only. The desk shows one layer per persona,
  coloured with `desk.colour`.
- No recurrence, no rooms or resources, no time zones other than `EXERCISE_TZ`.

## Flows

**Create.** The organiser fills in title, date and time, place, mode, description and guests.
Guests are chips from the address book, with the same `lookup` as Compose. On save
(`ops/calendar.py`, `calendar.save(v, form)`):

1. Create the event and the attendee rows.
2. Send each guest an invitation mail through the same code path that sending from that
   mailbox uses today:
   - a member or leader: `views_inbox._send` / `_send_as_leader`, from the Sent box chosen with
     `sender_box`
   - the desk: `desk.send`, so a Message of kind `desk` with `Mail.event`

   The mail goes in one thread (`event.uid`). Its body is a plain-text summary, so it reads
   well in any mail view, and `Mail.invite` / `Mail.event` render the card. To a persona, this
   is a team's message to that persona: `inbound.from_mail` makes the `Response`, and the
   desk's Inbox gets its copy and section, so a waiting invitation is waiting like any
   message.

**Answer.** The card and the event's dialog have Yes / Maybe / No and an optional note.
`calendar.respond(v, event, answer)` does three things:

1. Sets the attendee row.
2. Logs a `MailAction` (`cal_rsvp`).
3. Sends a short reply mail ("Accepted: Coordination meeting") to the organiser in the same
   thread.

On the desk, answering as a persona goes through `desk.send`. That leaves the desk Sent copy,
so the conversation leaves Needs attention, and it skips a queued `NpcReply` for that
persona.

**Change / cancel.** Only the organiser's mailbox can make changes, as anyone who can send
from it. A change bumps `sequence`, resets the reminders and sends an `update` mail. Guests
whose time changed are asked to answer again. Cancelling sends `cancel`, and the event stays
on the calendars struck through.

**From an email.** A thread's toolbar gets **Create event**. It is prefilled from the thread:
the subject as title, the people in the thread as guests and `source_mail`. The event links
back to the thread, and the thread shows "Event: …".

**Guests can't see each other's calendars.** When choosing a time, the page shows conflicts
only for calendars the viewer can already read:

- their own
- their team's
- their members' (for a leader)
- the personas' (for the desk)

There is no cross-team free/busy.

## Screens (SIMEX Mail, `templates/ops/mail/calendar/`)

- URL: `<mailbox base>/calendar/`, mounted in `mail_patterns`, so the existing mailbox bases
  give it for free:
  - `/inbox/calendar/` and `/inbox/me/calendar/`
  - `/inbox/desk/calendar/`
  - `/inbox/as/<team>/calendar/` (read-only)
  - `/inbox/team/calendar/` and `/inbox/members/<pid>/calendar/` (the leader's read-only views)

  The read-only bases get only the read routes (`team_patterns`), so the URLconf tests keep
  holding. Another team leader's mailbox (`/inbox/leaders/<team>/`) has no calendar.
- Views:
  - Week (default on wide screens): a time grid with the "now" line
  - Day
  - Month
  - Schedule (agenda, the default on phones)
- Side bar: a mini month, the calendar layers with checkboxes and colours, and invitations
  awaiting an answer.
- Create by dragging on the grid, or with the Create button. Move or resize your own events by
  dragging (pointer events, no library, like `desk_board.js`). An event opens a popover with
  details, guests and their answers, Yes / Maybe / No, Edit and Delete.
- Times as decision 3: members on the scenario's clock only; leaders, the desk and ExCon in
  real time with the scenario time beside each event.
- In the Inbox: the invitation card in the thread view and an "Invitation" chip on the list
  row. The mail side bar gets an **Upcoming** box with the next three events (like Gmail's
  side panel), optional per person.
- `static/ops/calendar.js`, plus a calendar section in `mail.css`. The page refetches its
  events every 30 seconds; a version to poll, as on the Assign board, comes with invitations.

## The app drawer

A grid button (nine dots) in `gm-top-right`, before Help, opens a panel of app tiles. It is a
template, `ops/mail/_apps.html`, included in the mail base and the role-player base. Its tiles
come from a function, `views_inbox.apps(v, request)`, which returns them for that viewer:

| Who | Tiles |
|---|---|
| Member | Team inbox, My mail, Calendar, Help |
| Team leader | My mail, Team inbox, Team members, Calendar, Dashboard, Role plays (if any), Help |
| Persona desk | Persona desk, Calendar, Teams, Dashboard, Assign, Role plays (if any), Help |
| Role player | Role plays, Briefing |
| ExCon (viewing) | the viewed mailbox's Mail and Calendar, Back to ExCon |

How the tiles look and behave:

- Each tile has an inline-SVG icon in the SIMEX colours (the Calendar tile shows the day: the
  scenario's for a member) and a badge where it helps: unread mail now; invitations to answer
  and the next event within the lead time come with steps 2 and 5.
- It closes on Escape or a click outside, and works with the keyboard (arrow keys, focus
  trap).
- On phones it is a full-width sheet.
- No "favourites" editing at first: the order is fixed per role.

The existing tab bars stay. Calendar is reached from the drawer, and later from the Upcoming
box and the invitation cards. Retiring the tab bars in favour of the drawer can come later,
once people are used to it.

## Notifications

- **Reminders.** A new `calendar.tick()` runs in the scheduler's main loop, next to
  `roleplay.tick`. For each accepted, tentative or unanswered attendee whose event starts
  within `remind_minutes` (real minutes) of now, it pushes one notification ("In 10 min:
  Coordination meeting, OSOCC") with `push.notify_people` to the people of that mailbox:
  - a team calendar: the team's participants
  - a member's own: that member
  - a persona: the desk audience (below)

  The notification gives a member the scenario time only. `reminded_at` makes sure each
  reminder fires once. Reminders more than 5 minutes late are dropped, so a restart never
  sends a burst. Leaders' own calendars are reminded to them only.
- **In the open page.** `/poll/` returns `cal`: the next event and any reminder due. `mail.js`
  shows it as a toast with the alert sound, like new mail, and updates the drawer's badge.
- **New invitations, changes, answers.** These arrive as mail, so they are pushed and announced
  as mail already are. No second notification.
- **Persona desk.** For a persona's meeting, the desk needs someone to play the persona at that
  time, by phone, VTC or in person.
  - The event's conversation is a `DeskConversation`, so its owner is whoever plays it. It is
    given through Assign and `desk.auto_assign` as usual.
  - `desk.notify` tells the owner, or `desk.audience` while nobody has it, at
    `remind_minutes`. The notice is pushed.
  - An invitation still unanswered shows in Needs attention.
- **ExCon.** A `Notice` `cal-unstaffed:<event>` when a persona's accepted meeting starts within
  `cal_lead` and nobody on the desk has it. This is ExCon's cue to send a role player.

## Seeding the calendar from the mail (step 3)

The calendar arrives part way through the exercise, so the meetings the teams have already
proposed or agreed by email are in the mail, not on any calendar. ExCon has SIMEX AI find them,
checks what it found, and puts the right ones on the calendars.

**Where.** ExCon only: Calendar / Meetings → **Find meetings in the mail**, for one team or all
of them, optionally between two dates. SIMEX AI's suggestions stay with ExCon, as everywhere.
With `AI_PROVIDER` off, the button is disabled and says why.

**What SIMEX AI reads.** The participant-facing mail threads only:

- the team inboxes
- members' and leaders' own mailboxes
- the persona desk's mailbox

It never reads control-side material: no `RolePlay`, no planned injects. `ops/cal_seed.py`
joins the scans in `tests/test_roleplay.py` and `test_suggest.py`.

**How it runs.** In a thread of the web process, like `InjectDraft`, with `AiCall` purpose
`cal_seed`. Progress shows on the page, and a `Notice` says when it is done. Tests use
`ai.set_client(fake)`.

**What SIMEX AI returns.** A list of candidate meetings. Each candidate has:

- a title
- start and end, as real times: the mail's wording ("tomorrow at 10:00") is resolved against
  the mail's date and its `sim_at`, and then converted to real time with `simclock.real_at`
- the place and the mode (in person, phone, VTC, other)
- the organiser and the guests, as address-book keys (`t:` `m:` `l:` `p:<username>`)
- its status: proposed, confirmed, declined or cancelled
- each guest's answer, where the mail shows one
- a confidence
- the source thread(s) and Mail ids, with the line quoted

**Data.** A `CalendarSeed` per run (who started it, the teams and dates asked, status,
progress, the `AiCall`s) with a `CalendarSeedItem` per candidate (the fields above, the
duplicate it matches if any, ticked or not, and the event it made).

**Review and apply.** Plan, then apply, like the roster and senders imports:

1. ExCon reviews a table of the candidates. Any field can be edited, and rows are ticked to
   keep them.
2. A candidate that duplicates an existing event is flagged: one from the same thread, or with
   the same guests within 30 minutes.
3. **Apply** creates the ticked events and their attendee rows quietly:
   - no invitation mails and no push
   - `source_mail` set, and `CalendarEvent.seeded` = the run
   - the answers as the mail shows them (confirmed → accepted)

A seeded event shows **From the mail**, with a link to the thread. Running it again skips the
threads already seeded, unless ExCon asks for them again.

**Clearing.** Reset and backup cover the seed runs (`CalendarSeed`, `CalendarSeedItem`), and
Start afresh removes them.

## ExCon

- **Calendar view.** Each team's calendar is read-only from Team inboxes
  (`/inbox/as/<team>/calendar/`). A new **Meetings** view (Dashboard → Meetings, or under
  Compose) lists every event across teams and personas: who organised it, the guests and their
  answers, the persona's organisation, the time in scenario and real time, and who plays it. It
  has filters (team, persona, today or the next hours) and an Export.
- **Compose and the desk.** Compose gets "Invite to a meeting", which calls `calendar.save` as
  a persona, through `desk.send` (author None, "Sent by ExCon").
- **Role play.** Optional, step 6: "Make a role play" on a persona meeting creates a `RolePlay`
  with one `RolePlaySlot` for that team at that time. Role players then get its reminders,
  board and status. The link goes one way only: role-play material never shows on any
  participant's calendar.

## Everything else this touches

- `reset.py`: Clear mailboxes removes the events and attendees of the chosen mailboxes, and
  Start afresh removes them all. `backup.py` exports and loads them.
- `delivery.withdraw` of an invitation's Message cancels the event for those teams.
- `views_inbox.TRACKED`: `cal_create`, `cal_update`, `cal_cancel`, then `cal_rsvp`. Never for
  read-only views. The reports' and Dashboard's inbox-management figures leave them out.
- `activity.timeline`: these actions.
- `report.py`: meetings a team organised or accepted, how quickly it answered invitations, and
  invitations it never answered, numbered rather than named in team reports. A later step.
- `network.py`: invitations and answers are sent Mail and Messages, so they are counted already.
  Check that the reply mails don't double-count an edge.
- `desk.on_delivered` / `on_response`: invitations take the normal path. Check that the
  leader-persona and leaders'-copy exclusions still hold.
- `access.py` / URLconf walk: every calendar view is `@mailbox_view`. Read-only bases get no
  write routes.
- `tests/test_roleplay.py` and `test_suggest.py` scan participant-facing modules:
  `calendar.py` and `views_calendar.py` are in the scans.
- `MANUAL.md` (5.13 SIMEX Calendar and the app drawer), `PARTICIPANT_GUIDE.md`,
  `LEADER_GUIDE.md`, `DESK_GUIDE.md`, `README.md` and `CLAUDE.md`.
- AGPL source link: the calendar page extends the mail base, and its own side bar keeps the
  link.

## Build order

1. **The app drawer and a personal calendar.** *Built.*
   - Models and migration (`0032_calendar`), `Viewer.events()`, and `calendar.layers(v)`.
   - The Calendar page on each mailbox base: Week, Day, Month, Schedule, the mini month and
     layers.
   - Create, edit and delete events with no guests, by drag too.
   - Real times, shown to members in scenario time (`simclock.at` / `real_at`).
   - `MailAction`s, reset and backup.
   - The drawer, in SIMEX Mail and on the role players' pages.
   - Tests: teams never cross, read-only views can't write, members never get a real date.
     Manual.
2. **Invitations.**
   - Guests from the address book, invitation and reply mails, and the card in the thread
     view.
   - Yes / Maybe / No; change and cancel; Create event from an email.
   - `MailAction`s. Tests: the address-book check on save, and a team inviting another team
     only with `team_to_team`.
3. **Seed the calendar from the mail** (below). ExCon has SIMEX AI find the meetings already
   proposed or agreed in the mail, reviews them, and applies them quietly as guest events. This
   step comes after invitations because seeded events need guests.
4. **Personas and the desk.**
   - `Sender.organisation`, persona layers on the desk calendar, answering as a persona with
     `desk.send`, and AI personas' invitations always left to the desk.
   - Who plays it, through `DeskConversation`, with desk notices.
   - ExCon Compose's "Invite to a meeting".
5. **Reminders.** `calendar.tick`, push, the toast from `/poll/`, drawer badges, Upcoming in the
   side bar, and the `cal-unstaffed` Notice.
6. **ExCon and extras.** The Meetings view and export, "Make a role play", reports, and
   (optional, desk only) SIMEX AI's "this email proposes a meeting" suggestion, run in the AI
   loop.
7. **The email channel.** `.ics` (METHOD REQUEST / CANCEL) attached to invitation emails, and
   iMIP REPLY parsing in `inbound`.

Each step is deployable alone, and the tests pass at each.

## Decided

- **Clock:** real time, with the scenario time shown too. Members see and enter scenario time
  only (decision 3).
- **Members' own calendars:** their leader sees them read-only, as they see the member's own
  mailbox.
- **Team-to-team meetings:** follow `team_to_team`.
- **AI personas:** never accept invitations themselves; the desk always answers for them.
