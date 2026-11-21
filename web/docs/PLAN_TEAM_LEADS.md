# Plan: Team leaders and team members

Status: built (2026-11-21); MANUAL.md, LEADER_GUIDE.md and CLAUDE.md describe it. Where the build
differs from this plan, the code and those docs are right. Delete this file when it's no longer useful.
No accounts have been handed out and nothing has run live yet, so this changes the model
directly. It needs no data migration beyond the schema.

## What it is

Each team (four of them, independent of each other) has:

- **Team members** (`Person.role = "member"`): the participants. That's the team's focal
  point, or a few people who share **the team's single SIMEX Mail inbox**. As today.
- **A team leader** (`Person.role = "tl"`): an ExCon person who monitors that team. Not a
  participant. They sign in to SIMEX Mail like everyone else and get:
  1. **My mail**: their own mailbox. Some injects go to it as well as to the team inbox,
     and they can write to and get mail from their team's members.
  2. **Team inbox**: a read-only view of their team's shared inbox. Opening mail there
     doesn't mark it read and records no `Event`.
  3. **Dashboard**: their team's reply stats and the AI reading of the replies (tone,
     quality, flags, how likely each was written with AI).

Teams are independent: nobody can write to, see or address anyone in another team. A
leader sees only their own team. Only ExCon sees across teams (its existing tabs and
"view as").

## Decisions

1. **Exactly one leader per team.** Saving a second active leader for a team is refused
   (People & teams and the roster CSV). A leader covering two teams is not supported.
2. **The MEL's team leader persona *is* the team leader** (see "The TL persona is the
   leader"). Injects, follow-ups and ExCon answers sent as `undac-{team}-tl` show in that
   leader's **Sent**, and members' mail to that persona lands in the leader's **Inbox**.
3. **Leaders write only to their own team's members** (the shared team inbox), from the TL
   persona's address. They can't write to other personas, so nothing a leader writes is a
   graded reply. A leader can forward an inject from My mail to the team.
4. **Members write to their leader by writing to the TL persona** (already in their address
   book). As for any persona, that's a `Response`: on the Replies tab, AI-analysed, gradable.
   A member can Cc the leader on a reply to another persona: one Response per persona, plus
   the leader's copy.
5. **Leaders' My mail gets only** injects marked "Also to team leaders" and the TL
   persona's traffic. Not ExCon's answers from other personas: the leader reads those in the
   Team inbox tab.
6. **SIMEX Mail is the default channel**, so the leader features are in-app. With the email
   channel, leaders only get email copies of "Also to team leaders" injects. No Sent mirror
   and no member ↔ leader mail.
7. **Team-to-team mail stays an ExCon setting, off by default.** Address books never list
   another team's leader, and a typed-in key for another team's leader persona is refused.
   A leader's book is their own team only.
8. **"Also to team leaders" on each inject**: a checkbox, set by the importer when the MEL
   recipients column names the TL, and editable by ExCon.
9. **Leader Dashboard = own team only.** No grades: no human rubric scores, AI-suggested
   scores, heatmap or exercise summary. ExCon's Dashboard keeps the cross-team comparison.
10. **AI-written is an estimate.** It's shown in bands ("likely AI-assisted" ≥ 70, "possibly"
   40–69), always with Claude's stated signals and the words-per-minute figure. Never a bare
   percentage, never "detected".
11. **Everyone signs in at SIMEX Mail** (`MAIL_URL`). `/inbox/` is the team inbox for a member
   and My mail for a leader, so there's one URL and one credentials sheet, and push
   notifications and the home-screen app work for leaders too (scope `/inbox/`).

## The mailbox model

Today a mailbox is a team (`Mail.team`, `MailLabel.team`). A leader's mailbox lives in the
same team plus an owner:

- `Mail.owner = FK(Person, null)`: `None` = the team's shared inbox; set = that leader's
  My mail.
- `MailLabel.owner = FK(Person, null)`, same meaning (labels are per mailbox). The unique
  constraint becomes `(team, owner, name)`. `ThreadLabel` follows its label.
- `Message.inbox_owner = FK(Person, null)`: an in-app delivery to a leader's box. `_put_mail`
  copies it to `Mail.owner`.
- `Inject.to_leaders = BooleanField(default=False)`, added to `IMPORT_FIELDS`.
- `Person.lead_seen = IntegerField(default=0)`: last team-inbox Mail id the leader opened,
  for the "new" badge on the Team inbox tab.
- `models.py`: `ROLES = {"member": "Team member", "tl": "Team leader (ExCon)"}`,
  `Person.is_leader`, and a `participants()` queryset method (`role="member"`) so "who is a
  participant" is written once.

**Every mailbox query goes through the viewer.** `Viewer` gets `box = {"team": t, "owner": p_or_None}`
and `v.mails()` = `Mail.objects.filter(**v.box)`. The ~32 `Mail` / `MailLabel` /
`ThreadLabel` queries in `views_inbox.py` (`_folder_qs`, `conversations`, `labels`,
`_counts`, `thread_view`, `action`, `_relabel`, `labels_save`, `star_mail`, `draft_*`,
`send`, `poll`, `mail_file`) switch to it, and so do `context.py`'s unread count and the
ExCon counts (`inboxes`, team delete check, Clear mailbox, which also clears that team's
leader boxes). A test builds a team inbox and a leader box with the same subjects and checks
every page and JSON endpoint shows only its own box.

## Who sees what

| Viewer | `/inbox/` | Can write | Other routes |
|---|---|---|---|
| Member | team box (`owner=None`) | yes | none |
| Leader | own box (`owner=self`) | yes, to own team only | `/inbox/team/…` own team box read-only; `/inbox/dashboard/` |
| ExCon | → `/inboxes/` | no | `/inbox/as/<team>/` (as today), new `/inbox/as/<team>/leader/` read-only (one leader per team) |

Access (`ops/access.py`):

- `participant_of` requires `role == "member"`. It stays the key to the team box, and its
  write routes, `context.py` and push.
- `leader_of(user)`: the active `tl` Person with a team behind a login, or None.
- `leader_required(view)`: leader (or ExCon, for checking) only; sets `wrapper.leader_only`.
- `home()`: members and leaders go to `/inbox/`.
- `MailHost`: no change. Leader routes live under `/inbox/` (mail host), and ExCon's new
  leader view lives under `/inbox/as/`, which already stays on `PUBLIC_URL`.
- The URLconf test also walks `leader_only` routes: member → 403, anonymous → login.

## The TL persona is the leader

The MEL's team leader persona (`undac-{team}-tl`, "UNDAC {team_name} TL") is played by
each team's real leader, in SIMEX Mail:

- **Which persona:** `Sender.team_leader = BooleanField` (Senders tab: "This persona is the
  team leader"; at most one; the importer ticks it for a local part ending `-tl`). For team
  t it fills to e.g. `undac-a-tl`. That's the leader's address and From name in SIMEX Mail
  (`render.persona_address`), so members see the same "UNDAC Team A TL" whether ExCon or the
  leader wrote.
- **One thread id in both boxes.** A conversation between a team and its leader has the
  same `Mail.thread` in the team box and the leader's box (boxes are separate, so ids can
  repeat). Continuing a thread on either side is then just "same thread id in the other
  box". No subject matching.
- **Everything sent as the TL persona is a `Message`** and goes through `_put_mail`, which,
  when `Message.persona` is the team's TL persona, also writes a **Sent** copy
  (`folder="sent"`) into each of that team's leaders' boxes, in the same transaction:
  - injects and follow-ups from the TL persona (ExCon-scheduled) → leader's Sent;
  - ExCon answers as the TL persona on the Replies tab → leader's Sent;
  - the leader's own compose / reply in My mail: `_send` creates a `Message` (in-app, persona
    = TL, `team` = own team, kind `reply` with `response` set when it answers a member's
    Response, else new kind `leader`) and delivers it at once. It shows in the Log and,
    as an answer, on the Replies page ("answered by the team leader"). `leader` joins the
    live kinds wherever they're listed (`live`, `reply`, `followup`).
- **Mail to the TL persona** from a member (compose, reply, Cc) is a `Response` as for any
  persona (Replies tab, AI analysis, gradable), and `inbound.from_mail` also puts an Inbox
  copy in each leader's box, same thread, with push.
- An inject from the TL persona with "Also to team leaders" ticked isn't put in the leader's
  Inbox too: they get the Sent copy only.
- On the Replies tab, a Response to the TL persona shows whether the leader has already
  answered it in SIMEX Mail, so ExCon doesn't answer twice.

## Address book and sending (`views_inbox.address_book`, `_resolve`, `_send`)

- Member's book: as today, active personas with `{team}` filled with the own team, so the
  TL entry is their own team's leader. Another team's TL persona is never listed.
- Leader's book: **their own team only** (`key "t:<own team id>"`, "Team A" at
  `team_address`). No personas, no other teams.
- `_resolve` takes the viewer and accepts only keys in *that viewer's* book, plus (members
  only, as today) a hand-made `p:` key for a retired persona, **unless it is another team's
  TL persona**. For leaders there's no fallback: a hand-made `p:` key can't reach a persona,
  and a `t:` key must be their own team.
- In a leader's thread view, Reply / Reply all on mail from their team answers the team.
  On an inject from another persona, Reply is replaced by **Forward to team**.

## Injects to leaders

- Inject page and bulk edit: **Also to team leaders** checkbox, shown in the recipients
  summary ("Each team + team leaders").
- Importer: `to_leaders = True` when the Recipients column matches
  `\b(TL|team lead(er)?s?)\b` (case-insensitive), e.g. "Team members & TL". Tracked like
  the other import fields, so a local edit survives a re-import.
- **In-app** (`render.build_inapp`): for each team target, one extra `Draft` per active
  leader of that team, `inbox_owner=leader`, `person_id=leader`, To label = the leader (none if
  the sender is the TL persona: the leader gets the Sent copy instead).
  Idempotency keys add `-l<pid>`, so each copy gets its own `Message` before delivery (the
  existing rule).
- **Email** (not the default channel; `render.build_email`): one extra email per leader with an
  email address, with `person=leader`. In `inbound.match_email`, a reply whose token matches a leader's copy,
  or whose From is a leader, comes in with `match="leader"` and `status="ignored"`: visible
  on Replies with a note, never counted for the team. Leaders are told to read, not reply.
- Follow-ups (`delivery.send_followup`) to an inject that went to leaders also go to the
  leaders' threads (one child `Message` per leader delivery, `parent` = that delivery).
- Test runs: the Test team can have a test leader, so a rehearsal covers this path.
- `delivery.people_of`, `render` `members()` / each_person / all_bcc, `push.send_team` and the
  inject `each_person` target list use members only. A leader never gets a member copy.

## Leader screens

All in the SIMEX Mail shell (`templates/ops/mail/base.html`, refactored into blocks
`topbar` / `tabs` / `side` / `main`). For leaders a **tab bar** sits under the top bar:

```
[ My mail ●2 ]  [ Team A inbox · read-only ●5 ]  [ Dashboard ]
```

Badges: unread in My mail; new team-inbox mail since `lead_seen`. Members see no tabs.

**Team inbox tab** (`/inbox/team/`, the read-only routes of `mail_patterns` mounted on the
team box). It opens on a **Team replies** folder: conversations the team has written in,
newest first, each row naming the member who wrote the latest message (`Mail.author`). The
usual folders (Inbox, Sent, All mail) stay; Drafts and Trash are hidden. In a conversation,
each mail the team sent shows a strip with author, time since the inject was delivered, and
the AI reading if there is one (`mail.response.analysis`): tone, quality, flags, AI-written
band and signals. This strip renders only for leaders. A test renders the same thread as a
member and as ExCon "view as" and asserts none of it appears.

**Dashboard tab** (`/inbox/dashboard/`, `views_leader.py`). Move the data part of
`views_ai.dashboard` into `dashboard_data(kind, team=None)` used by both, so figures can't
drift apart. For the leader's team:

| Section | Content |
|---|---|
| Replies | replies sent, expected injects answered (x / y), late, missing, median first reply |
| Funnel | delivered → read → answered |
| Activity | replies per hour over the exercise |
| Members | replies per member |
| Tone & quality | top tones, mean quality, latest sentiment, sentiment sparkline |
| AI use | analysed, "likely" / "possibly" AI-assisted counts, mean wpm; list of those replies with signals |
| Flags, commitments, asks | from the analysis, newest first |

Links go into the Team inbox tab (`/inbox/team/t/<thread>/`). Email-channel replies (no Mail)
expand inline. AI status line without spend. Refreshes every two minutes. A "Test" switch only
for a test-team leader.

## ExCon side

- **People & teams**: one list per team. The role select reads *Team member* / *Team leader
  (ExCon)*, with the leader first and an ExCon chip. Warnings for a team without a leader or
  without members. Roster CSV `tl` / `team leader` / `leader` = leader. The credentials
  sheet says what each person signs in to. Changing a role deletes that person's push
  subscriptions.
- **Team inboxes**: per team, members' and leader's sign-ins, and "View as team" /
  "View leader's mail".
- **Inject page / list**: the Also to team leaders checkbox and recipients summary.
- **Replies**: `match="leader"` rows (email channel) shown greyed with "from the team leader (not counted)";
  Responses to the TL persona show "answered by the team leader" when the leader has replied.
- **Senders**: "This persona is the team leader" checkbox.
- `ai.reply_block`: drop the "(team leader)" label; leaders don't write graded replies.
- `legacy_import.py`: `tl` → `member` (the first app's meaning).

## Security checklist

- Box isolation: every `views_inbox` query via `v.mails()` / `v.box` (test above).
- Teams independent: a member or leader can't address, see or reply into another team, even
  with hand-made keys, ids or thread ids. Tests post `t:<other team>`, another team's TL persona,
  `p:<persona>` as a leader, and another team's `in_reply_to` / `forward_of` / thread / file id.
- Read-only team box for leaders: write routes not mounted under `/inbox/team/`. Leaders
  never mark the team's mail read and never create `Event`s.
- AI fields only in leader mode.
- Leaders: 403 on every ExCon route; members: 403 on every `leader_only` route.
- No control-side material: leaders see what the team, its leader and personas exchanged,
  plus the AI reading of replies.

## Tests (`ops/tests/test_leaders.py` + updates)

- Role: a leader gets no member copy of each_team / each_person / all_bcc injects (in-app
  and email), isn't matched as a reply's person, isn't a push target for the team box.
- to_leaders: in-app → Mail in team box and leader box, separate Messages and idempotency
  keys; email → separate email; re-sending doesn't duplicate; follow-up reaches both threads;
  importer sets it from "Team members & TL" and keeps a local change.
- TL persona: an inject from `undac-{team}-tl` → team Inbox + leader Sent, same thread; a
  member's reply → Response + leader Inbox copy; the leader's reply → a `Message` (kind
  `reply`, `response` set) → team Inbox + leader Sent, and the Replies page shows it
  answered; an ExCon answer as the TL persona → leader Sent; leader compose → kind `leader`;
  with two leaders both get the copies; re-delivery doesn't duplicate.
- Compose: Cc'ing the leader on another persona → one Response plus a leader copy; the
  isolation attempts above all fail (including `p:undac-b-tl` from Team A).
- Leader screens: tabs, Team replies folder with authors, AI strip (via `ai.set_client(fake)`),
  no read marking, dashboard numbers equal ExCon's for that team, badges.
- Existing tests that use `tl` participants (`test_ops.py` roster l.343, legacy import
  l.658) updated; `test_every_page_renders_with_data` covers the new pages.

## Docs

- `MANUAL.md`: roles; People & teams; "Also to team leaders"; what leaders see; leader
  replies by email are ignored.
- `PARTICIPANT_GUIDE.md`: members can write to their team leader. Plus a short leader
  section (or a separate guide shown to leaders at `/inbox/help/`).
- `CLAUDE.md`: a mailbox is `(team, owner)`, always through `Viewer.box`; the team-leader
  persona is the leader (Sent mirror in `_put_mail`, same thread id in both boxes); kind
  `leader` is a live kind; `member` =
  participant, `tl` = ExCon leader, never a participant (`participants()`); leader routes
  `leader_required`; teams never cross.

## Build order

1. **Roles**: `participants()` everywhere a Person is treated as a participant; People &
   teams wording; `participant_of`; tests. Ships on its own.
2. **Mailbox owner**: `Mail.owner`, `MailLabel.owner`, `Viewer.box`, every query through it,
   isolation test. No visible change yet.
3. **Leader My mail + TL persona**: leader `/inbox/`, `Sender.team_leader`, Sent mirror in
   `_put_mail`, leader copies in `from_mail`, leader compose as `Message`, address books,
   `_resolve`, Forward to team, Replies "answered by the team leader", ExCon "view leader's mail".
4. **Injects to leaders**: `to_leaders`, import, in-app + email delivery, follow-ups,
   inbound `match="leader"`.
5. **Team inbox tab**: read-only team box for leaders, Team replies folder, AI strip, tab bar.
6. **Dashboard tab**: `dashboard_data()` refactor, leader dashboard, badges.
7. Docs, full test run, deploy.
