# Role-player coordination: build plan

Role players are ExCon-side people who play characters in the simulation face to face, by phone or
VTC (airport staff, immigration, RC, press, the team leaders' own scripted calls). They need to
be at the right place at the right time, for the right team, with their script, documents and
FAQ, and to be reminded. ExCon needs one board showing who is where, what is next, what is
late and what is done.

## Decision: control-side material, walled off

The "never store control-side material" rule is relaxed **for the role-play area only**.
`RolePlay.script`, `RolePlay.outcomes` and `RolePlay.notes`, the general briefing and the FAQ
live in their own models. They are read only by:

- ExCon (the Role play tab, `ops/views_roleplay.py`)
- the role players assigned to them (`/inbox/rp/`, `ops/views_rp.py`)

They are never read by participant or leader mailbox views, AI analysis, AI personas, drafts or
reports. They are never copied into an `Inject`. A test scans those modules to keep it so.
The MEL rows they come from are not injects: the inject importer keeps skipping them.

## Data

- `RolePlay` is one event of the role-play schedule. Its key is the MEL serial, `-b` for a
  second row of the same serial (1-41 RC for teams A&C, 1-41-b for teams B&D). It holds:
  - the day and the sheet's time text
  - duration
  - title (Description), role (e.g. "Immigration officers x 2"), location, method (F2F / Phone / VTC)
  - script, expected outcomes, documents to bring or print, ExCon notes
  - an optional flag, the role players assigned (`players`, Persons) and the last imported values
- `RolePlaySlot` is one sitting: a start time, minutes, the teams it is for (none = all
  together), a status (pending, ready = role player in position, done, skipped), who set it
  and when, a "reminded" marker and a "changed" marker.
- `RolePlayFile` holds the documents for an event (upload, download only).
- Role players are `Person` with role `rp` ("Role player (ExCon)"): no team, a login with
  the QR sign-in, SIMEX Mail host only. Team leaders (`tl`) can be assigned too. A leader
  is only shown the slots of their own team, or of all teams.
- `conf` keys:
  - `rp_lead` (minutes before a slot to remind, default 15)
  - `rp_email` (email a reminder to role players without push)
  - `rp_briefing` (the general briefing, Markdown)
  - `rp_faq` (Markdown)
  - `rp_days` (JSON: day name → date)

## Import (Role play → Import)

Plan, then apply, like the roster. One upload, a MEL-MIL workbook or a separate role-player
schedule sheet, read sheet by sheet:

- **MEL-MIL sheet**: rows whose method is F2F, Phone, VTC, MS Teams or Signal give the
  serial, day, time text, duration, title, method, script (Content), outcomes and documents.
- **Role-player schedule sheet** (Serial, Time, Duration, Role, Role player, Event): gives
  the role and the role players. Rows merge with the MEL ones by serial. A repeated serial is
  a second event.

Times become slots:

| Time text | Slots |
|---|---|
| `13:00`, `13:00:00` | one slot, all teams |
| `1440 - A` | 14:40 for team A |
| `1430 - A / 1450 - B / …` (one per line) | one slot per line |
| `18:00 A - B / 18:20 C - D` | 18:00 for A and B, 18:20 for C and D |
| `18:00 + 18:20` with the role "RC - teams A&C" | 18:00 for A, 18:20 for C |
| `19:10` with duration `4 x 5 min (D C B A)` | 19:10 D, 19:15 C, 19:20 B, 19:25 A |
| `17:05` with duration `4 x 5 min` | four slots in team order |

The day name becomes a date through `rp_days`. These are guessed from the MEL's email rows,
which carry both Day and Send date, and ExCon can correct them on the plan.

Role-player names are split on `+`, `,`, `&`, ` - `, ` and `, then matched to People by
name (roster.key), first name, or team leaders' first names. New names are added as
role-player Persons without a login. "n/a" and blanks leave the event unassigned.

Re-import updates fields ExCon hasn't edited (snapshot in `imported`, like injects). A slot
that is already done or in progress is never moved.

## ExCon: Role play tab (`/roleplay/`)

- **Board**: every slot of a day by time, with a "now" line. It shows the serial, event,
  teams, role, players (✓ when they've acknowledged or are in position), location and status
  buttons (In position / Done / Skip / Undo). Filter by role player, location or team.
  Refreshes itself when something changes (poll).
- **Slip**: push every pending slot from a given time (that day) back or forward by N
  minutes. The role players affected are told.
- **Checks**:
  - a role player double-booked (overlapping slots)
  - an event with no role player, no location or no time
  - a team in two places at once
- **Event page**: edit every field, assign players, edit slots, upload files.
- **Role players**: who plays what, their login and alert status, and links to their run sheet.
- **Briefing & FAQ**: the text all role players see.
- **Print**: a run sheet per role player or per location, and the documents-to-print list.

## Role players: `/inbox/rp/` (SIMEX Mail host, same login and QR flow)

- **Next up**: the next slot with a countdown, where, which team, how long, and who with,
  plus *I'm in position* and *Done* buttons.
- **My day**: all their slots.
- **Event brief**: script, outcomes, files, notes, the others playing it.
- **Briefing / FAQ**.
- **Set up this device**: alerts and Add to Home Screen. It reuses the service worker and
  `PushSubscription`.

Team leaders who are assigned role plays get a "Role play" tab in their mailbox.

## Notifications

- The scheduler tick (`roleplay.tick`) pushes each slot's players `rp_lead` minutes before it
  starts, then at the start. If `rp_email` is on, it emails those with no push device
  (personal email, else email).
- A slip or reassignment pushes "Changed: 1-29 now 15:25 (was 15:10)".
- An ExCon `Notice` fires when a slot has started and nobody is in position (`rp-late:<slot>`),
  and when the import finds a double booking.

## Also

- `reset.start_afresh` sets slots back to pending and clears reminders. `clear_team` leaves
  them alone.
- MANUAL.md gets a "Role play" section, plus a role-player guide (RP_GUIDE.md) at
  `/inbox/rp/help/`.
- Every new view is wrapped: `@excon_required` (ExCon) or `rp_required` (role players and
  assigned leaders). The URLconf walk tests cover them.
- The source link is on the new base template.

## Build order

1. Models, migration, role `rp`, access, the slot-time parser, the importer, with tests.
2. ExCon tab: board, event page, slip, checks, import pages, print.
3. Role-player portal, sign-in, push subscription, leader tab.
4. Reminders and notices in the scheduler, reset, manual, CLAUDE.md.
