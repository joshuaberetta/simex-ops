# SIMEX Calendar and the app drawer: build plan

Teams arrange meetings by email all through the exercise: a coordination meeting at the OSOCC,
a call with the Ministry, a VTC with the Resident Coordinator, a meeting between two teams. Today
those arrangements stay in the mail threads. Nobody, the persona desk included, can see what was
agreed, who is meant to be there or what comes next.

SIMEX Calendar is a second app in SIMEX Mail, in the style of Google Calendar. Every account
(members, team leaders, the persona desk, ExCon viewing as a team) can schedule a meeting, invite
people, teams and personas, answer invitations and get reminders. It opens from a new **app
drawer** in the SIMEX Mail header, in the style of Google's (the grid of dots).

The rules in `CLAUDE.md` still apply:

- Teams never cross.
- Members see only simulation time.
- Nothing control-side is shown.
- Every query is filtered by the viewer's mailbox.

## Decisions (proposed)

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
3. **Times are scenario times.** An event is stored at the time its participants see, so a
   member never sees a real date. That is the simulation time when the clock is on, and real
   time when it is off. Reminders are due when `simclock.now()` reaches them. This means "10
   minutes before" counts scenario minutes, and the reminders keep up if ExCon changes the
   clock speed. The event stores which clock it was made on (`sim`). If the clock is turned on
   or off later, events made on the other clock are shown with a "time base changed" note
   instead of being moved silently.
4. **Who may invite whom is the address book** (`views_inbox.address_book` / `contacts` /
   `lookup`). It is checked on save, exactly as on send. Nothing new to configure:
   - a member: the personas, their teammates, their own team, and other teams only with
     `team_to_team`
   - a leader: their team and its members
   - the desk: every team, member and leader
5. **Personas attend through the desk.** A persona invited by a team gets a pending invitation
   on the desk's calendar, plus the desk Inbox copy. A desk member accepts or declines as that
   persona (`desk.send` with the RSVP). AI personas never accept by themselves at first: their
   invitations are held for the desk, like a held `NpcReply`. Auto-accept is a later Settings
   option.
6. **Organisation shown.** Each persona attendee is shown by name and organisation. The
   organisation is a new participant-facing field, `Sender.organisation` (for example
   "Ministry of Interior"). It is set on the Senders tab and in the senders import/export.
   `Sender.category` stays ExCon-only.
7. **No AI for participants.** SIMEX AI never proposes or creates events for members or leaders,
   because arranging meetings is part of what is being exercised. For the desk, "Add to
   calendar" suggestions from a team's email are a later, optional step (see phase 5).
8. **The calendar is in-app only at first.** When injects go by email (`channel_mode` email),
   participants don't use SIMEX Mail, so phase 6 adds `.ics` invitations to the email channel.
   Until then, the Calendar is offered only where the viewer's mail is in-app.

## Data

```
CalendarEvent
  uid            short random id; also the invitation thread (Mail.thread)
  team, owner    the organiser's mailbox (None, None = the desk)
  organiser_key  address-book key: t:<id> | m:<pid> | l:<pid> | p:<username>
  organiser_name as shown
  title, location, description          participant-facing text, capped
  mode           in_person | phone | vtc | other
  starts_at, ends_at, all_day           scenario time (decision 3), naive in EXERCISE_TZ->UTC as usual
  sim            bool: made on the simulation clock
  status         confirmed | cancelled
  sequence       bumped on every change that matters to attendees (time, place, cancel)
  source_mail    FK Mail, null: "made from this email"
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
  responded_at, responded_by (Person, or user for the desk/ExCon)
  comment        their note with the answer
  remind_minutes default conf cal_lead (10), per attendee
  reminded_at    reminder fired (reset when the event moves)
  hidden         removed from this calendar (declined and hidden)

Mail.event       FK CalendarEvent, null
Mail.invite      "" | request | update | cancel | reply   (shown as the invitation card)
CalendarPref     per Person/user: view (week|day|month|agenda), layers shown, colours
```

Notes:

- A calendar's events are its mailbox's `CalendarAttendee` rows. A team calendar shows the
  events where the team (`t:`) is a guest or the organiser. A member's calendar shows their
  `m:` rows. That is the query, always through a new `Viewer.events()` (like `v.mails()`).
- A member's Calendar page shows **both** of their calendars (team and mine) as two layers
  with checkboxes, like Google's "My calendars". A leader's page shows their own calendar,
  plus the team calendar read-only. The desk shows one layer per persona, coloured with
  `desk.colour`.
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
  - `/inbox/as/<team>/calendar/` (read-only) and `/inbox/team/calendar/` (the leader's
    read-only view)

  The read-only bases get only the read routes (`team_patterns`), so the URLconf tests keep
  holding.
- Views:
  - Week (default on wide screens): a time grid with the "now" line in scenario time
  - Day
  - Month
  - Schedule (agenda, the default on phones)
- Side bar: a mini month, the calendar layers with checkboxes and colours, and invitations
  awaiting an answer.
- Create by dragging on the grid, or with the Create button. Move or resize your own events by
  dragging (pointer events, no library, like `desk_board.js`). An event opens a popover with
  details, guests and their answers, Yes / Maybe / No, Edit and Delete.
- Shown in scenario time for members (`sees_sim_only`). Leaders, the desk and ExCon also see
  the real time in the popover. "Today" is the scenario's today.
- In the Inbox: the invitation card in the thread view and an "Invitation" chip on the list
  row. The mail side bar gets an **Upcoming** box with the next three events (like Gmail's
  side panel), optional per person.
- `static/ops/calendar.js`, plus a calendar section in `mail.css`. The page polls
  `<base>/calendar/poll/?v=` for changes, by version as on the Assign board.

## The app drawer

A grid button (nine dots) in `gm-top-right`, before Help, opens a panel of app tiles. It is a
template, `ops/mail/_apps.html`, included in the mail base and the role-player base. Its tiles
come from a function, `views_inbox.apps(v, request)`, which returns them for that viewer:

| Who | Tiles |
|---|---|
| Member | Mail (team inbox), My mail, Calendar, Help |
| Team leader | My mail, Team inbox, Team members, Calendar, Dashboard, Role plays (if any), Help |
| Persona desk | Persona desk, Calendar, Teams, Dashboard, Assign, Help |
| Role player | Role plays, Calendar (if also on the desk), Help |
| ExCon (viewing) | the viewed mailbox's Mail and Calendar, Back to ExCon |

How the tiles look and behave:

- Each tile has an inline-SVG icon in the SIMEX colours and a badge where it helps: unread
  mail, invitations to answer, the next event within the lead time.
- It closes on Escape or a click outside, and works with the keyboard (arrow keys, focus
  trap).
- On phones it is a full-width sheet.
- No "favourites" editing at first: the order is fixed per role.

The existing tab bars stay. Calendar is reached from the drawer, and from the Upcoming box and
the invitation cards. Retiring the tab bars in favour of the drawer can come later, once people
are used to it.

## Notifications

- **Reminders.** A new `calendar.tick()` runs in the scheduler's main loop, next to
  `roleplay.tick`. For each accepted, tentative or unanswered attendee whose event starts
  within `remind_minutes` of `simclock.now()`, it pushes one notification ("In 10 min:
  Coordination meeting, OSOCC") with `push.notify_people` to the people of that mailbox:
  - a team calendar: the team's participants
  - a member's own: that member
  - a persona: the desk audience (below)

  `reminded_at` makes sure each reminder fires once. Reminders more than 5 minutes late are
  dropped, so a clock jump never sends a burst. Leaders' own calendars are reminded to them
  only.
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

## ExCon

- **Calendar view.** Each team's calendar is read-only from Team inboxes (`/inbox/as/<team>/
  calendar/`). A new **Meetings** view (Dashboard → Meetings, or under Compose) lists every
  event across teams and personas: who organised it, the guests and their answers, the
  persona's organisation, the time in scenario and real time, and who plays it. It has filters
  (team, persona, today or the next hours) and an Export.
- **Compose and the desk.** Compose gets "Invite to a meeting", which calls
  `calendar.save` as a persona, through `desk.send` (author None, "Sent by ExCon").
- **Role play.** Optional, phase 5: "Make a role play" on a persona meeting creates a
  `RolePlay` with one `RolePlaySlot` for that team at that time. Role players then get its
  reminders, board and status. The link goes one way only: role-play material never shows on
  any participant's calendar.

## Everything else this touches

- `reset.py`: Clear mailboxes removes the events and attendees of the chosen mailboxes, and
  Start afresh removes them all. `backup.py` exports and loads them.
- `delivery.withdraw` of an invitation's Message cancels the event for those teams.
- `views_inbox.TRACKED`: `cal_create`, `cal_update`, `cal_cancel`, `cal_rsvp`. Never for
  read-only views.
- `activity.timeline`: these actions.
- `report.py`: meetings a team organised or accepted, how quickly it answered invitations, and
  invitations it never answered, numbered rather than named in team reports. A later step.
- `network.py`: invitations and answers are sent Mail and Messages, so they are counted already.
  Check that the reply mails don't double-count an edge.
- `desk.on_delivered` / `on_response`: invitations take the normal path. Check that the
  leader-persona and leaders'-copy exclusions still hold.
- `access.py` / URLconf walk: every calendar view is `@mailbox_view`. Read-only bases get no
  write routes.
- `tests/test_roleplay.py` and `test_suggest.py` scan participant-facing modules: add
  `calendar.py` and `views_calendar.py` to the scans.
- `MANUAL.md` (a new section 5.x: Calendar), `PARTICIPANT_GUIDE.md`, `LEADER_GUIDE.md`,
  `DESK_GUIDE.md`, `README.md` and `CLAUDE.md`.
- AGPL source link: the calendar page extends the mail base, so the link is already there.

## Build order

1. **The app drawer and a personal calendar.** Models and migration; `Viewer.events()`; the
   Calendar page (Week, Day, Month, Schedule) on each mailbox base; create, edit and delete
   events with no guests; scenario-time display; reset and backup; tests for mailbox scoping
   (teams never cross, members never see a real date). Manual.
2. **Invitations.**
   - Guests from the address book, invitation and reply mails, and the card in the thread
     view.
   - Yes / Maybe / No; change and cancel; Create event from an email.
   - `MailAction`s. Tests: the address-book check on save, and a team inviting another team
     only with `team_to_team`.
3. **Personas and the desk.**
   - `Sender.organisation`, persona layers on the desk calendar, answering as a persona with
     `desk.send`, and AI personas' invitations held for the desk.
   - Who plays it, through `DeskConversation`, with desk notices.
   - ExCon Compose's "Invite to a meeting".
4. **Reminders.** `calendar.tick`, push, the toast from `/poll/`, drawer badges, Upcoming in the
   side bar, and the `cal-unstaffed` Notice.
5. **ExCon and extras.** The Meetings view and export, "Make a role play", reports, and
   (optional, desk only) SIMEX AI's "this email proposes a meeting" suggestion, run in the AI
   loop.
6. **The email channel.** `.ics` (METHOD REQUEST / CANCEL) attached to invitation emails, and
   iMIP REPLY parsing in `inbound`.

Each step is deployable alone, and the tests pass at each.

## Open questions

- **Scenario or real time** (decision 3). Scenario time matches what members see. Real time
  would be simpler for reminders, but would show members real dates.
- **Members' own calendars.** Should a member be able to make a private event in their own
  calendar that the leader cannot see? Proposed: the leader sees it read-only, as they see the
  member's own mailbox.
- **Team-to-team meetings.** Should they follow `team_to_team` (proposed), or always be allowed?
- **AI personas.** Should they ever accept invitations by themselves (a Settings option, off),
  or always leave them to the desk?
