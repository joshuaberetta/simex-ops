# SIMEX Messenger and the Files view: build plan

Teams don't only email. In a real deployment, and in every SIMEX so far, members coordinate
on Signal and WhatsApp: a team group, side chats between two members, a quick message to the
team leader, sometimes a WhatsApp to a counterpart in the Ministry. Today that traffic leaves
the platform. ExCon can't see it, SIMEX AI can't read it, the Dashboards and the Network
undercount the team's real coordination, and the reports miss half the story.

**SIMEX Messenger** is a third app in SIMEX Mail's app drawer, beside Mail and Calendar. It
works like Signal or WhatsApp: direct chats, groups, the team's own room, replies, reactions,
read receipts, files and pictures, and push on the phone. It follows the same direction rules as
SIMEX Mail, so members reach their teammates, their leader and the personas, and the persona
desk reaches the teams, members and leaders as any persona. AI personas answer in chat too.
Everything is kept and monitored like mail.

This plan also covers a **Files** view: every file sent in the exercise (email, SIMEX Mail,
chat, inject documents) in one list for the desk, team leaders and ExCon. It is grouped by
team, can be filtered and links back to the conversation each file came from. It doesn't
depend on Messenger, so it can be built first (step 1).

The rules in `CLAUDE.md` still apply:

- Teams never cross.
- Members see only simulation time.
- Nothing control-side is shown.
- Every query is filtered by the viewer's mailbox, or here by the viewer's rooms.
- The AI is "SIMEX AI" on every screen, its suggestions are ExCon-only, and it never writes to
  `Grade`.
- Every page links to the source code.

## Status (2026-11-27)

Built, with tests (`test_files.py`, `test_messenger.py`, `test_messenger_desk.py`):

- **Step 1, Files**: all of it.
- **Step 2, Messenger for the teams**:
  - the models, the settings, team rooms, direct chats and groups;
  - the app with its own manifest, service worker and push subscriptions;
  - receipts, typing, edits and deletions, the monitoring notice, simulation time;
  - the leader's read-only view and ExCon's `/chats/` (freeze, delete for everyone, Markdown export);
  - backup, reset, `MailAction`, What's new, the app tiles and the docs.
- **Step 3, personas and the desk**:
  - personas in rooms;
  - turns as Responses, persona posts as Messages of kind `chat`;
  - the desk's Messenger console (sections, the persona selector, take / no reply needed / follow, open a chat as a persona);
  - `DeskConversation` `chat:<room>`, notices, default owners.
- **Part of step 5**: AI personas answer chat turns addressed to them, in a minute or two, in a chat's style. They reuse `NpcReply` and `npc.send`. There are no separate chat timing fields on the profile yet: `npc.CHAT_MINUTES`.
- **Part of step 6**: the Network counts posts (`chat`, `group`).

Decided since the plan:

- **Messenger is its own app on phones.** It has its own manifest (`id` `/chat/`), icon, service worker (scope `/chat/`) and push subscriptions (`PushSubscription.app`). Added to the home screen, it opens and notifies as an app of its own, beside SIMEX Mail, on the same host and sign-in.
- **No hosting change** for now: the poll stays cheap instead.
  - A `RoomChange` feed is the cursor, and a 204 is the answer when nothing changed.
  - The newest change id is cached a second per process, and a viewer's room ids for 30 s (refreshed on a member change).
  - The service worker keeps the app's versioned files on the device.
  - Posted files are served `immutable`.

Still to build:

- **Step 4**: SIMEX AI's windowed readings of rooms (`PostReview`) and the chat prompt for ReplyAnalysis. Turns are already read as replies once closed. Also desk triage of chats, chat files in `DocumentAnalysis` (the `post_file` field exists), the AI meter in rooms, and Notices for flags.
- **Step 3 remainder**:
  - chat cards on the Assign board, and the desk side bar's counts;
  - several rooms side by side and the keyboard shortcuts;
  - `chat_take_on_type`;
  - desk pushes for chat (desk members get the bell).
- **Step 5 remainder**: profile chat timing, the typing indicator for AI personas, `chat_npc_chime`, and AI-to-AI caps in rooms.
- **Step 6 remainder**: chat on the Dashboards, `desk_stats`, reports (`chat_figures`), debrief, activity timeline, ChatSIMEX's tools and Suggest a reply's chat mode.
- **Steps 7 and 8.**

## Name

"SIMEX Chat" would be confused with **ChatSIMEX**, the desk's chat with SIMEX AI
(`ops/desk_chat.py`, `DeskChat`). So this plan uses:

- **SIMEX Messenger** in the UI ("Messenger" on the tile).
- `ops/messenger*.py` and `views_messenger*.py` in the code.
- `Room`, `RoomMember` and `Post` as the models (never `Chat…`).

Decision 0 below asks you to confirm the name.

## Decisions (recommended; the ones to confirm are listed at the end)

1. **Its own models, joined to the existing pipelines.** A chat isn't a mailbox. One room has
   one row per post, not one copy per mailbox, and it has members, receipts and reactions. So
   Messenger gets its own tables (`Room`, `Post`, …). It joins the existing machinery at three
   points so that almost nothing downstream has to learn a new shape:
   - **`Response`**: a team's *turn* addressed to a persona (decision 6) is a `Response`
     with `channel="chat"`. Because of that, these keep working on chat without changes to
     their own logic: ReplyAnalysis, `NpcReply`, Suggest a reply, the Replies tab, the
     reports' response times and the desk's pairing.
   - **`Message`**: everything a persona posts (desk, ExCon, AI persona) gets a `Message`
     of kind `chat`, `channel="chat"`, created before the post and with a deterministic
     `idem_key`, as the rules ask of every delivery. That gives idempotent AI-persona release
     (`npc-<id>`), `Message.response` for "answered at", `author`, `npc` and `ai_draft`.
   - **`DeskConversation`**: the desk's per-conversation state, keyed `thread = "chat:<room
     id>"`. Taking, assigning, following, done, follow-ups, the Assign board, default
     assignments and `DeskWatch` / `DeskNotice` all work on chat rooms unchanged.
2. **Who may chat with whom is the address book.** It is `views_inbox.address_book`, read
   through a chat-specific wrapper (`messenger.book(v)`) that applies the Messenger settings.
   It is checked on every room create, member add and post, just as `_send` checks it.

   | Who | May open a chat or group with |
   |---|---|
   | Member | Teammates (`chat_direct`), their leader (as the team leader persona), personas (`chat_personas`), other teams only with `team_to_team` |
   | Team leader | Their team (the team room), their members, and groups of them. No personas (as in mail, the leader's book is their team) |
   | Persona desk / ExCon | Any team (the team's persona room, below), any member, any leader (as kind `tl` in mail), as any persona |

   A group can contain **at most one team** unless `team_to_team` is on. The desk's "All
   teams" never makes a cross-team room: as in mail, it expands to one room per team (one post
   each, same text).
3. **Kinds of room.**
   - `team`: one per team, made automatically. Its members are the key `t:<team>`, so every
     participant of the team is in it as people join or leave, plus the leader (`l:<pid>`).
     Nobody can leave it. It is the chat version of the team inbox.
   - `direct`: exactly two keys. Member↔member, member↔leader, member↔persona,
     persona↔team (`p:` + `t:`: the team's shared chat with that persona, like the team
     inbox's mail with it), persona↔leader.
   - `group`: three or more keys, named, with admins. Anyone allowed by decision 2 can make
     one.
   - `inject`: a room opened by an inject delivered by chat (step 8, optional).
4. **Keys are address-book keys.** `RoomMember.key` is `m:<pid>`, `l:<pid>`, `p:<username>`
   or `t:<team id>`, the same keys as compose, the Network and the calendar. A persona is
   always identified by its username (`p:<username>`), never by a display name.
5. **A persona joins from where it is added.** When a persona is added to an existing team
   group, the desk, AI personas and Suggest a reply see posts **from the join onwards**. The
   team's deliberation before that stays team-internal. ExCon, and leaders reading their
   team's rooms, see everything. The same goes for any member added later, as in
   WhatsApp/Signal.
6. **A turn, not a post, is what a persona answers.** People send three short posts in a
   row. A team-side *turn* is the consecutive posts by one author in a room with a persona
   member, closed by a gap of `TURN_GAP` (90 s), a post by anyone else, or 20 posts. Each
   turn is one `Response`, created on its first post and grown as posts are added:
   - `Response.text` is the posts joined; `Post.response` points to it.
   - `persona` is who the turn is addressed to (decision 7).
   - `subject` is the room's title.

   Everything keyed on `Response` (ReplyAnalysis waits `ai.SETTLE`, `NpcReply` merges by
   `inbound.thread_key` = `c:<room id>`) then sees one message, not ten.
7. **Addressing.** Who a team-side turn in a room with personas is for is worked out in this
   order:
   1. a direct room with one persona;
   2. an `@mention` of a persona (the composer offers the room's personas);
   3. a reply (quote) to a persona's post;
   4. the only persona in the room;
   5. otherwise SIMEX AI's quick reading (`messenger_ai.address`, the fast model, the room's
      last 20 posts the personas can see).

   The reading also gives `need` (reply | optional | none) and `urgency`. A turn addressed to
   no persona is still a `Response` (persona empty; the personas did hear it), but it never
   queues an `NpcReply` and goes to the desk's **Chatter** section, not Needs attention.
8. **No desk Mail copies.** `desk.on_response` / `on_delivered` skip `channel="chat"`. The
   desk reads the rooms themselves. Copying every chat post into the desk's mailbox would
   bury the email. The desk's states for chat are computed by `messenger_desk.states` in the
   same shape as `desk.states`, so sections, counts, the board and the Dashboard merge both.
9. **Plain text with light formatting.** `Post.body` is always text: `*bold*`, `_italic_`,
   `` `code` ``, auto-linked URLs and `@mentions`, rendered in the browser from text (never
   HTML from the user). Pictures and files are `PostAttachment`s. There is no rich HTML in
   chat, so `ops/richtext.py` isn't involved and search, SIMEX AI, the reports and the
   Network keep reading text only.
10. **Edits and deletions are monitored.** A participant may edit a post within
    `chat_edit_minutes` (15) and delete it at any time. Others see "edited" or "This message
    was deleted". Every earlier version (`PostEdit`) and the deleted text are kept:
    - ExCon sees them on its views, with who deleted the post and when.
    - The AI readings and reports read what was actually sent (the first version), noting
      edits.
    - Leaders and the desk see what participants see.
11. **Participants are told it is monitored.** The first time someone opens Messenger, a
    one-time card says: "Messenger is part of the exercise. ExCon, and for your team's chats
    your team leader, can read everything sent here, including edited and deleted messages."
    The same text is in the Help and the participant guide. Keeping the exercise's
    communication in the platform is a briefing rule. Messenger makes that easy to follow, but
    nothing can technically stop a WhatsApp.
12. **Leaders read their team's rooms read-only** (`/inbox/team/chat/`), as they read the
    team inbox and members' own mailboxes. They get SIMEX AI's reading and the AI meter
    (`sees_ai`) there. This includes members' direct chats with each other. That follows the
    members' mailboxes rule, but it is the one to confirm (question 2).
13. **Members see simulation time only.** `Post.sim_at` is stamped like `Mail.sim_at` (an
    inject's own post takes its `scenario_dt`). Timestamps, day separators and "last seen"
    are shown through the same `listdate` / `msgdate` filters. Read receipts show "Seen",
    not a real time, to members.
14. **Push shows who and where, never the text.** As with mail ("sender and subject only"),
    the notification is "Jane Doe in Team A logistics" or "Minister of Interior", with a
    `tag` per room so a burst collapses into one notification. `chat_push_preview` (off) would
    add the first 80 characters.
15. **Polling, not sockets, for now.** The web container runs 3 gunicorn workers with one
    thread each on 1 vCPU, and the cache is the default per-process LocMem (no `CACHES` in
    `simex/settings.py`). Long polling or SSE would tie up a worker per open chat. So
    Messenger uses short, cheap polls with a cursor (see Real time and capacity) and Web Push
    for phones in the background. An ASGI sidecar for SSE is a later option, not v1.

## Data

```
Room
  kind            team | direct | group | inject
  title           "" for direct/team (named from the members); a group's name
  team            FK Team, null      the one team side (null: persona-only, or cross-team with team_to_team)
  teams           JSON [team ids]    every team with a member here (for teams-never-cross checks and filters)
  has_persona     bool               the desk's rooms
  inject          FK Inject, null    kind inject (step 8)
  created_by      FK Person, null; created_by_user FK User, null (desk/ExCon)
  created_at, last_post_id, last_post_at   (denormalised for the poll and lists)
  is_test         bool (the Test team)
  frozen          bool               ExCon: read-only for everyone (moderation)
  colour          the avatar colour
  indexes: (team, last_post_id), (has_persona, last_post_id)

RoomMember
  room            FK Room
  key             m:<pid> | l:<pid> | p:<username> | t:<team id>
  person          FK Person, null    (m:, l:)
  persona         username, ""       (p:)
  team            FK Team, null      (t:, and the person's team for m:/l:)
  admin           bool
  joined_at, joined_post_id          posts before this are hidden from this member (decision 5)
  left_at, added_by (key or "ExCon")
  muted_until     null | datetime (forever = far future)
  unique (room, key) while not left; index (key, left_at)

RoomRead        a person's read position, one row per (room, person): upto post id, at
                (a t: member's people each have their own row; personas have none: the desk's
                reading is DeskConversation)
ReadMark        append-only log, one row each time someone's position advances by at least one post
                (room, person, upto, at): for the reports' "time to read" figures, kept small by
                writing only on advance

Post
  room            FK Room
  author          FK Person, null    member or leader
  author_user     FK User, null      desk or ExCon login posting as a persona
  persona         username, ""       the persona it is posted as (desk, ExCon, AI persona)
  kind            text | system (joined, left, renamed, created) | inject
  body            text
  reply_to        FK self, null
  mentions        JSON [keys]
  forward_of      FK self, null      (forwarding stays inside the viewer's rooms)
  response        FK Response, null  the team-side turn it belongs to (decision 6)
  message         FK Message, null   a persona's post (decision 1)
  npc             bool               written and sent by an AI persona
  client_key      the composer's random key: unique (room, client_key), so a resend on a flaky
                  phone connection never posts twice
  created_at, sim_at
  edited_at, deleted_at, deleted_by (key or "ExCon")
  is_test
  index (room, id)

PostEdit        post, body (the earlier text), at, by
PostReaction    post, key (who), emoji (a fixed set of 6, as WhatsApp's quick row), at; unique (post, key)
PostAttachment  post, filename, content_type, size, data, sha256 (set on save, like MailAttachment),
                width/height (pictures), attachment FK (an inject document shared into chat, no copy)
RoomTyping      room, key, name, until  (who is typing; one upsert at most every 3 s per typer)
                A table, not the cache: the cache is per gunicorn worker (decision 15). The same
                flaw affects `desk.presence` today; moving it here, or to a DatabaseCache, fixes both.

PostReview      SIMEX AI's reading of a window of a room (messenger_ai, below): room, upto
                (post id), status/model/prompt_version/input_hash/attempts/error, summary,
                open_points, items JSON [{post ids, flags, ai_written, asks, commitments, facts}]
```

Changes to existing models:

- `Response.post` (FK Post, null): the turn's first post. `channel="chat"`.
- `Message.kind` gains `chat`. `Message.room` is not needed (via `Post.message`).
- `NpcProfile` gains `chat_min_seconds`, `chat_max_seconds` and `chat_quick_chance` (chat
  answers come in seconds to a few minutes, not 5–20 minutes), and `chat_style` (a short
  note on how it writes in chat).
- `DocumentAnalysis.post_file` (FK PostAttachment, null).
- `MailNote` becomes able to point at a `Post` (`post` FK, `mail` nullable) in step 7.
- `MailAction.action` gains the `chat_*` actions (decision on tracking below).

Settings (Settings → SIMEX Messenger):

| Setting | Default | What it controls |
|---|---|---|
| `chat_enabled` | off | Shows the app at all |
| `chat_direct` | on | Members' direct chats with each other |
| `chat_groups` | on | Members may create groups |
| `chat_personas` | on | Participants may start chats with personas; the desk can always reach them |
| `chat_edit_minutes` | 15 | How long a post can be edited |
| `chat_push_preview` | off | Adds the start of the text to the push |
| `chat_file_mb` | 20 | Largest file a post may carry |
| `chat_npc` | on | AI personas answer in chat, within `npc_enabled` / `npc_scope` |
| `chat_npc_chime` | off | Lets AI personas answer unaddressed group turns that SIMEX AI reads as needing them |
| `chat_review` | on | SIMEX AI's readings of chat |
| `desk_chat_wait_minutes` | 5 | Chat's own wait notice (shorter than email's `desk_wait_minutes`) |

Team-to-team rooms follow `team_to_team`.

## Flows

### A team-side post (`messenger.post`)

1. Check the room is one of the viewer's (`messenger.rooms_of(v)`, the only way to reach a
   room). Check it isn't frozen, and that the author is still a member.
2. Save the `Post` (`client_key` idempotent) and its files. Update `Room.last_post_*`.
3. Turn: if the room has persona members and the author is team-side, extend the open turn's
   `Response` or start one. On a start, run `inbound`-style hooks in this order:
   `npc.on_response` (decision 7 addressing first; the quick AI reading is queued if needed),
   then `messenger_desk.on_turn` (desk state, `desk.auto_assign("chat:<room>")`,
   `DeskNotice` kind `chat` to `desk.audience` once per turn, not per post, with the bell's
   "nobody needs to answer" if an AI persona will).
4. After commit, push in a thread (`push.send_people`, the room's other people, minus muted
   and minus whoever has the room open, which the poll knows). Collapse with `tag=room-<id>`.
   At most one push per room per person per 30 s while their unread count only grows.
5. `MailAction` `chat_post` (with the room id, not the text) for the reports'
   activity-over-time figures.

### A persona's post (desk, ExCon, AI persona: `messenger.post_as`)

1. The poster chooses the persona if the room has several. The default is the persona the
   waiting turn was addressed to.
2. Create the `Message` (kind `chat`, `persona`, `team` = room.team, `person` for a
   member's direct chat, `response` = the latest open turn's Response, `author` = the
   desk member's Person, `npc`, `ai_draft`, `idem_key` = `chat-<room>-<client_key>` or
   `npc-<job id>`), then the `Post` with `message`, `status="sent"` at once (it is in-app).
3. `desk.ACTOR` is set as in `desk.send`, so the poster isn't notified of their own post.
   Skip that persona's queued `NpcReply` in this room (as `desk.skip_ai`).
4. The desk's state of `chat:<room>` updates by itself (states are computed).
5. `desk.send`'s rule "needs live armed but for the Test team" applies to the desk posting
   into a room the desk opened. Answering a team that wrote first doesn't need arming, as with
   answers in mail.

### Read, reactions, typing, edit and delete

- Opening a room moves `RoomRead` (and writes a `ReadMark` if it advanced). `views_inbox.track`
  isn't called for reads (too noisy); a read is a `ReadMark`. **Readers who are only viewing
  write nothing**: leaders reading their team's rooms, the desk reading teams' rooms, ExCon.
- Reactions: `chat_react` MailAction. Edits and deletions: `PostEdit` / `deleted_at`, plus
  `chat_edit` / `chat_delete` MailActions.
- Room changes (create, rename, add, remove, leave, mute): a system post in the room, and a
  `chat_room` MailAction.

### Withdrawing and clearing

- ExCon can delete any post for everyone (kept, `deleted_by="ExCon"`). A persona's post sent
  by mistake can also be withdrawn on its Log page, as with mail (`delivery.withdraw` learns
  `channel="chat"`: the Post is deleted, the Message becomes `withdrawn`).
- `reset.clear_team` clears the team's rooms (scope "members' own only" = its direct chats).
  `start_afresh` clears every room, post and read, keeps the `team` rooms (empty) and recreates
  any that are missing. Each case calls `messenger_desk.prune`, `messenger_ai.prune`,
  `ai_docs.prune` and `mail_notes.prune`.

## Participant and leader UI

SIMEX Messenger is mounted on the participants' bases like the calendar
(`<base>/chat/`; write routes in `mail_patterns` only; `@mailbox_view`; none for `peer`). It has
its own templates (`templates/ops/mail/chat/`), `static/ops/messenger.{js,css}`, no build step,
and is mobile first (most chat happens on phones).

- **Room list** (left on desktop, the whole screen on a phone). Rows show the avatar (team
  colour; a persona's `Sender.colour` with its organisation), name, last post preview, time
  (simulation time for members), an unread badge, a muted icon and a ✓✓ for read. Pinned
  rooms come first, then the newest. Search covers names and posts (only the viewer's rooms,
  only posts since their join).
- **Room view**: day separators, grouped bubbles, a reply quote, reactions, "edited",
  tombstones, file cards and picture thumbnails (served as inline images only as sniffed
  PNG/JPEG/GIF/WebP, as `mail_file?inline=1`; anything else downloads), "typing…", "Seen by
  3" in groups and ✓✓ in direct chats. Older posts load on scroll up (`?before=<id>`, 50 at a
  time).
- **Composer**: text with `@` autocomplete (room members), attach (files, camera on phones),
  reply, emoji, Enter to send (Shift+Enter for a new line; on phones the send button). An
  outbox in `localStorage` keyed by `client_key` retries until the server confirms, and shows
  a clock, then ✓.
- **New chat / New group**: pick from `messenger.book(v)` (the address book's entries, same
  chips as compose). A group gets a name. Exact-address lookup (`views_inbox.lookup`) works
  as in mail for people not in the growing contacts. Personas are shown with their
  organisation.
- **Room info**: members (personas with organisation), shared files (feeds the Files view),
  mute, leave, and rename and add for admins.
- The app drawer gets a **Messenger** tile with the unread count. The mail page's top bar
  gets a small chat badge. The manifest gets a `shortcuts` entry, so the home-screen app
  opens Messenger directly.
- **Leaders**: their own Messenger (the team room, direct chats with members, groups), and
  **Team chats** (`/inbox/team/chat/`, read-only, `@leader_required`): every room of their
  team, with SIMEX AI's reading per room and the AI meter on posts (`sees_ai`).
- What's new: an entry for participants, leaders, the desk and ExCon. `_whatsnew.html`, the
  `SOURCE_URL` link and `_apps.html` go on the Messenger base template.

## The persona desk: chat at volume

Chat traffic is much heavier than email: many short turns, several at once, a team expecting
an answer in minutes. The desk gets a **Chats** tab (`/inbox/desk/chat/`,
`views_messenger_desk.py`, `@desk_or_excon`) built as a console, not a mailbox:

- **Queue (left)**: one row per room, never per post, in the desk sections. The sections
  come from `messenger_desk.states`, in `desk.states`' shape:
  - **Needs attention**: an addressed team turn with no persona post after it, or a
    follow-up due. Sorted by wait, with a timer that turns amber at half and red at
    `desk_chat_wait_minutes`.
  - **Chatter**: team turns in persona rooms addressed to nobody (decision 7). No timer;
    a count of unread.
  - **Later**: an AI persona is answering (`npc.OPEN` jobs, with who and when, "typing" in
    the last seconds), or a follow-up not due.
  - **Answered**.

  Each row shows team, persona chip (colour), room kind (direct / group / team room), owner,
  the triage's `ask` line and the unread count. Filters: persona, team, owner (mine /
  nobody's / anyone's), section and kind. Live, from the poll.
- **Workspace (right)**: the open room, with the composer posting *as* a persona (a picker
  when the room has several, defaulting to the addressed one). Under the composer:
  **Suggest a reply** (chat mode: short, conversational, at most a few sentences;
  `suggest.py` reads the room's posts the persona can see as the conversation) and
  **Done / no reply needed** (`DeskConversation.done_upto` = the post id). On wide screens,
  **up to three rooms side by side** (columns), each with its own composer, so one desk member
  can play a persona in several teams' chats at once.
- **Collision control**: presence on each room and in each row ("Ana is replying as the
  Minister", from `RoomTyping` for desk keys). Starting to type in a room nobody owns takes it
  (`desk.mark take`, optional, on by default as a `DeskPref` `chat_take_on_type`). Posting
  while someone else is typing as the same persona asks first.
- **Keyboard**: `j`/`k` next/previous waiting room, `Enter` open, `r` focus composer,
  `e` done, `t` take, `a` assign, `1`–`3` choose a column, `Esc` back to the queue.
- **Load controls**:
  - Notices are per turn (`DeskNotice` key `chat-in:<room>:<turn response id>`), not per
    post.
  - `only_mine` applies, as with mail.
  - `DeskPref` gains `chat_mute` (rooms or personas a desk login never hears about) and
    `chat_columns` (1–3).
  - Default assignments (`DeskDefault` by persona and team) assign chat rooms on their first
    turn, as for mail. Most desks will want "this desk member plays the Minister in every
    team's chats".
  - **Persona view**: the queue filtered to one persona with all its rooms, for a desk member
    who plays only that persona.
  - `desk.tick` adds `desk-wait:chat:<room>:<post id>` Notices for ExCon and owner notices at
    `desk_chat_wait_minutes`.
- The desk side bar shows "Needs attention: 3 mail · 7 chat". The **Assign** board shows chat
  rooms as cards (chat icon) beside mail conversations. Assigned to me / Following include
  them. `desk_stats.by_assignee` counts both.
- The desk reads teams' internal rooms read-only through the Teams tab
  (`/inbox/as/<team>/chat/`, like the mailboxes). Reading them never records anything.
- ChatSIMEX (`desk_chat.py`) learns the rooms. `desk_mails` and `team_mails` gain chat posts
  (refs `chat:<room>#<post>`, team-internal ones labelled as such), and `persona_brief`'s
  "what they said per team" includes the persona's posts. `desk_chat.check` applies the same
  warnings: another team's posts, team-internal posts, anything the team doesn't have yet.

## ExCon

- **Team inboxes → Chats** (`/chats/`, `views_messenger_excon.py`, `@excon_required`): every
  room. Filters: team, kind, persona, member, has flags, has files, edited or deleted, test.
  Search over all posts.
- **Room view**: read-only, everything since the room began, with edit history and deleted
  posts shown struck through ("deleted by Jane Doe 14:03"), SIMEX AI's reading in a side panel
  and the AI meter.
- **Moderation**: freeze or unfreeze a room, remove a member, delete a post for everyone, and
  switch the chat settings live (e.g. turn off `chat_direct` mid-exercise).
- **Export**: a room transcript as Markdown or CSV (`format=`); every room of a team as a zip
  of Markdown.
- **Compose → Chat**: post as a persona into a team's persona room, a member's or a leader's
  direct chat, or "All teams" (one room per team). This goes through `messenger.post_as`
  with author None ("Sent by ExCon").
- ExCon also opens the desk's Chats console as it opens the desk.
- **Notifications**: a `Notice` for an AI flag on chat (`escalate`, `safety`, `out_of_role`,
  `wrong_recipient`), a chat wait past `desk_chat_wait_minutes` with nobody on it, an AI
  persona's chat answer held or failed (`notices.npc`), and a room frozen automatically (not in
  v1).

## SIMEX AI on chat (`ops/messenger_ai.py`, the scheduler's AI loop)

Reading every chat post one by one would cost too much and gain little. SIMEX AI reads
**windows of a room** instead: the posts since its last reading (`PostReview.upto`), once the
room has been quiet for `ai.SETTLE` or after 40 new posts, with the previous window's summary
as context. One fast-model call per window returns:

- **Per team-side turn addressed to a persona** (it has a `Response`): the usual
  ReplyAnalysis fields (summary, tone, clarity, completeness, actionability, asks,
  commitments, facts, flags, `ai_written`, signals), written into that Response's
  `ReplyAnalysis`. Every Dashboard figure that reads ReplyAnalysis then includes chat. The
  prompt says these are chat messages, so short isn't "incomplete".
- **Per team-internal turn** (no persona): flags (`ai.FLAGS`, plus `leaks` for sharing
  another team's or control-side information, if it appears) and `ai_written` for turns of 40
  or more words. These go into `PostReview.items`, shown to leaders (`sees_ai`) and ExCon.
- **The window**: a summary and open points (decisions pending, who owes what), as
  `ThreadReview` does for mail. This drives the room's side panel and the reports.
- **Desk triage** for the latest addressed turn: `need`, `urgency`, `ask`, `reason` into
  `DeskConversation.ai_*` (the conversation only, as `desk_ai`).

**Addressing** (decision 7, step 5) is a separate, very small call made quickly
(`messenger_ai.address`, 10 s after a turn closes, in `npc.tick`'s pass), because an AI
persona and the desk can't wait for the settle.

**Files**: `ai_docs.team_files()` gains `PostAttachment`s posted by members and leaders (own
data, not a shared inject document). So `discover` and `DocumentAnalysis` (once per team and
sha256) read chat files too, with `post_file` and the covering turn's Response. They appear in
`/documents/`, the leader's Dashboard and the Files view below.

The AI meter (`views_inbox.ai_flags` / `_ai_meter.html`) gets a room version
(`messenger_ai.meter(room)`) for room rows and posts, for `sees_ai` viewers only.

The rules of the other AI readings hold: suggestions are ExCon-only (and leaders' for their
team, as now), never `Grade`, never control-side. `messenger_ai.py` joins the modules
`tests/test_roleplay.py` and `test_suggest.py` scan.

## AI personas in chat

- `npc.on_response` already queues an `NpcReply` for an enabled AI persona when a turn is
  addressed to it. Chat's differences:
  - **Timing**: `chat_min_seconds`–`chat_max_seconds` (defaults from the profile's timing,
    e.g. "responsive" 20–120 s, "slow" 2–15 min), `chat_quick_chance`, the same
    `no_reply_chance` and office hours.
  - **Merging**: later turns in the same room before it is sent move the job on
    (`thread_key` `c:<room>`), and a merged job is drafted again, as now.
  - **Typing**: from 8–20 s before release, the room shows the persona typing (a
    `RoomTyping` row the scheduler writes), as people expect in chat.
  - **Writing**: `npc.write` gets a chat mode, with the posts the persona can see since its
    join (decision 5), `chat_style`, the "short, chat-like, no greeting or sign-off" rules, and
    at most 3 posts (an answer may split in two).
  - **Sending**: `npc.send` branches on `response.channel == "chat"` to
    `messenger.post_as(npc=True, idem=npc-<id>)`.
- **In groups**: an AI persona answers only a turn addressed to it (mention, reply to it,
  direct room, or SIMEX AI's addressing naming it). With several AI personas in a group,
  only the one addressed answers. An unaddressed turn that SIMEX AI reads as needing a
  particular persona is answered only with `chat_npc_chime` (off by default); otherwise it is
  in the desk's Chatter.
- **AI to AI**: a persona-only or persona-heavy room is capped at `npc.PP_AI_TURNS` AI posts
  in a row (`npc.ai_run` on the room).
- **The desk keeps control**: the Scheduled folder and the desk console's Later section list
  chat jobs. The foot of the room shows the same `_npc_scheduled.html` panel (edit,
  reschedule, cancel, send now, "Answer it myself"). `npc.take_over` takes over waiting chat
  rooms too. A person posting as that persona in the room skips the job.
- `npc_scope` (test | all) and `in_scope(team)` apply unchanged.

## The Network, Dashboards, reports and the rest

- **Network** (`ops/network.py`): a new source `_posts(ctx, after_id)` (cursor `c<id>`).
  Each non-system post gives an edge from its author's node to every other current member's
  node at the time of the post: `t:` for a team key, `m:`, `l:` (the team leader persona's
  posts are its leader's), `p:<username>`. Kind `chat` for direct rooms and `group` for
  groups and team rooms, so the filter "kind of message" can show or hide them. Message kind
  `chat` is excluded from `_messages`, so a persona's post is counted once (from the Post).
  The edge weight is one per post, so a busy group shows thick edges. The wall screen
  `?screen=1` needs no change.
- **ExCon Dashboards** (`views_ai.py`): chat volume per team over time, internal versus to
  personas, active members (numbered for team-facing views), ReplyAnalysis figures (which
  include chat turns), and a "chat vs mail" share. `by_team_role` breaks these down.
- **Desk Dashboard** (`desk_stats.py`): chat pairs (team turn → first persona post after it)
  with median and p90 answer time, shown apart from mail times (minutes against hours). It
  reads only asks, commitments, facts and `DESK_FLAGS`, as now.
- **Leader Dashboard** (`views_leader.py`): their team's chat activity and SIMEX AI's
  readings.
- **Reports** (`report.py`): `chat_figures(team)`:
  - posts, turns, rooms and groups created;
  - share of members active (numbered);
  - internal versus to personas;
  - team→persona and persona→team answer times, and time to read persona posts (from
    `ReadMark`).

  Chat goes into ExCon's table and into `team_traffic` (the team's own rooms only; posts
  since a persona's join for persona rooms; internal rooms as team-internal). `for_team`
  numbers the members. `MailAction` `chat_*` are left out of the inbox figures, like `cal_*`.
- **Debrief** (`debrief.py collect`): every post of a team's rooms once each, by room, members
  as "Member n" with names replaced, edits noted, deleted posts included and marked (ExCon's
  view). `DEBRIEF.md` updated.
- **Activity timeline** (`activity.timeline`): posts authored, rooms created and reads (from
  `ReadMark`).
- **Calendar**: "Create event" from a post (the room's members as guests, checked against
  the book as now). This is a later step, not v1.
- **Backup** (`backup.MODELS`: every new model; `test_dbdump.py`), **reset** (above) and
  **withdraw** (above).
- **CLAUDE.md**, `MANUAL.md`, `PARTICIPANT_GUIDE.md`, `LEADER_GUIDE.md` and `DESK_GUIDE.md`
  are updated in each step that changes what people see.

## The Files view (`ops/file_index.py`, `views_files.py`)

Every file of the exercise, in one list, computed from what is kept (nothing stored), like the
Network and the desk's Dashboard. It is mounted in three places:

| Where | Who | Sees |
|---|---|---|
| `/inbox/desk/files/` | desk and ExCon | Every team's files, persona-facing and team-internal (as the desk reads the teams' mailboxes). No SIMEX AI readings |
| `/inbox/files/` | a team leader (`@leader_required`) | Their team only, with SIMEX AI's document readings (`sees_ai`) |
| `/files/` | ExCon (`@excon_required`) | Everything, with the readings, the AI band, notes and exports |

**Sources** (`file_index.rows(scope)`, each yielding one dict shape):

- `MailAttachment` (cid "" only: never pictures in the text). Copies of one Mail in several
  mailboxes (team inbox, members' copies, leader's Sent) are shown as one row with "in 4
  mailboxes", grouped by (Message or thread, sha256).
- `ResponseAttachment` (email from teams).
- `MessageAttachment` (files the desk or ExCon attached to an answer).
- `PostAttachment` (Messenger).
- Inject documents (`Attachment` via `Message.attachment_ids`). Off by default ("Show inject
  documents"), since each one is sent to every team.

**Columns**: file name and icon by type, size, from (member numbered for team-facing views,
leader, persona with colour, "ExCon"), to (team, member, persona, room), **direction**
(team → persona, persona → team, within the team, team → team, inject), **source** (email,
SIMEX Mail, Messenger), when (simulation and real time; members never use this view), the
inject it belongs to (if any), copies (the same sha256 sent again in the team), and notes.
ExCon and the leader also see SIMEX AI's reading (doc type, summary, answers the inject,
AI-written band). Every row links back to its conversation: `mail_notes.mail_url`-style
for mail (the right base for the viewer: desk, `/inbox/as/…`, the leader's boxes) and
`<base>/chat/<room>/#p<post>` for chat.

**Group by**: team (the default, with a count and total size per team), conversation,
sender, inject, file (one row per sha256 with every place it went), or day.

**Filters** (in the URL only, like `mail_filters`): team, direction, source, type (PDF,
Word, Excel, picture, other), sender (person or persona), inject, from/to dates, read by SIMEX
AI (done / queued / failed / not readable), AI band (ExCon and leader), has notes, test
teams, and search over file names and SIMEX AI's summaries.

**Actions**:

- Preview (pictures inline as sniffed images, PDFs in a new tab with
  `Content-Disposition: inline` only for `application/pdf`, others download).
- Download.
- ExCon only: export the list as CSV, or the filtered files as a zip streamed to the browser
  (`zipfile` writing into the response in chunks, capped at 500 MB; nothing written on the
  server, as with the database dump).

Downloads from the desk and ExCon never record `Event`s. A leader downloading doesn't either
(it is a monitoring view, not the team's mailbox).

**Speed**: each source is one `values()` query with `defer("data")`, filtered by scope and
the URL filters in the database where possible, merged and sorted in Python, and paged by 100.
At a few thousand files that is well within a request. If it grows past ~20k rows, a
`FileRef` index table written on save is the escape hatch. It isn't needed now.

## Real time and capacity

- **One poll per client.** In Messenger, `/inbox/chat/poll/` replaces the mail poll and
  carries the mail badge. In Mail, `/poll/` gains `chat` (unread rooms count). The request is
  `?c=<post cursor>&r=<open room>&t=<typing seq>` and is answered from:
  - the rooms whose `last_post_id` is greater than the cursor among the viewer's rooms (one
    indexed query on `RoomMember` by key in {m:pid, l:pid, t:team} joined to `Room`);
  - the open room's new posts, receipts and typing.

  With nothing new it returns `204`. The desk's console poll does the same over
  `has_persona` rooms.
- **Rates**: 2 s for the focused, open room; 6 s when Messenger is open but idle; 30 s in a
  background tab. Phones in the background rely on Web Push.
- **The budget**: about 90 participants and leaders, 10 desk and 10 ExCon. At worst that is
  roughly 30–40 polls per second, each about 10 ms. The web container is **1 vCPU and 3
  single-thread workers**. That is too close for comfort alongside mail, downloads and the
  dashboards. Before the first exercise with Messenger on:
  - raise the web component to `apps-s-2vcpu-4gb` and 5 workers for exercise days
    (`.do/app.yaml`, applied through `.do/render_spec.py`);
  - keep the poll view lean: no template, `values()` only, memoised room ids per request;
  - run `manage.py chat_load` (a new load script: N fake clients polling and posting against
    a staging app) to measure before trusting the numbers. The shared database "once choked"
    on the dump, so watch its CPU too.
- **Later, if needed**: an ASGI sidecar (`uvicorn`, a Django async view streaming SSE from
  Postgres `LISTEN/NOTIFY`) as a second DO component on `/inbox/chat/stream/`, with polling
  kept as the fallback.

## Tracking (`MailAction`)

These new actions are logged through `views_inbox.track`, with the room id in `threads` as
`chat:<id>`:

`chat_post`, `chat_room` (created, renamed, added, removed, left), `chat_mute`,
`chat_react`, `chat_edit`, `chat_delete`, `chat_download`, `chat_search`.

`MailAction.team` is required. For a cross-team room it is the actor's team. Reads are
`ReadMark`s, not actions.

## Tests (each step adds its own; the suite stays green on every step)

- **Scoping**:
  - a member can't open, read, post to or poll a room they aren't in;
  - posts before a member's or persona's join stay hidden from them;
  - teams never cross (no room with two teams without `team_to_team`; "All teams" makes one
    room per team);
  - `messenger.book` matches `address_book` under every setting.
- **The URLconf walk**: every new ExCon view `@excon_required`, every leader view
  `@leader_required`, every desk view `desk_or_excon`.
- **Turns**: posts merge into one `Response`; a gap, another author or 20 posts closes a
  turn; the addressing order; `NpcReply` merging on `c:<room>`; `client_key` idempotency;
  `Message` before `Post` with its `idem_key`.
- **The desk**: `messenger_desk.states` sections, done, follow-up, assign, defaults and
  notices once per turn; no desk `Mail` for chat.
- **SIMEX AI** (fake client): windowed readings write ReplyAnalysis for addressed turns and
  `PostReview` items otherwise; chat files are found by `ai_docs.discover`; the AI meter shows
  only for `sees_ai`.
- **AI personas**: chat timing, typing, the group addressing rule, `PP_AI_TURNS`, `send_now` /
  cancel / take over.
- **The Network**: posts counted once, the edges for team keys, a persona's post not
  double-counted through `Message`.
- **Sim time**: members never get a real date in Messenger's HTML or JSON.
- **Wiring**: `test_dbdump.py` (models in backup), reset, withdraw and prune;
  `test_roleplay.py` / `test_suggest.py` scanning `messenger*.py` and `file_index.py`.
- **Files view**: scope per viewer (a leader sees only their team, the desk never sees AI
  readings), copies grouped, links to the right base, the filters, no `Event` on download.

## Steps

1. **Files view (mail only)**. Independent and useful now: `file_index.py`, the three
   mounts, the filters, grouping, links back and the CSV/zip export. Messenger adds its source
   in step 2.
2. **Messenger core for the teams**:
   - the models, the settings, the team rooms, direct chats and groups within a team;
   - the participant and leader UI, poll, push, read receipts, typing, edits and deletes, sim
     time and the monitoring card;
   - the leader's read-only Team chats, ExCon's Chats list and room view (read-only,
     moderation);
   - backup, reset, `MailAction`, What's new, the app tile and Help;
   - the capacity check (`chat_load`, the instance size);
   - `PostAttachment` into the Files view.
3. **Personas in chat and the desk console**:
   - persona members and the turn → `Response` bridge, persona posts → `Message`;
   - the desk's Chats console (queue, sections, columns, presence, keyboard), `DeskConversation`
     `chat:` states, the Assign board, defaults, notices, prefs, side bar counts;
   - ExCon Compose → Chat, withdraw;
   - Suggest a reply in chat mode;
   - the desk reading teams' rooms (`/inbox/as/<team>/chat/`).
4. **SIMEX AI on chat**: addressing, windowed readings into ReplyAnalysis and `PostReview`,
   desk triage, chat files in `DocumentAnalysis`, the AI meter, Notices.
5. **AI personas in chat**: chat timing, typing, chat-mode writing, the group rules,
   the Scheduled panel and take over.
6. **Figures**: the Network, ExCon / desk / leader Dashboards, reports, debrief, the activity
   timeline, ChatSIMEX's tools.
7. **Notes on chat**: `MailNote` on a `Post` (desk, leader and ExCon scopes, as now), shown in
   the room's side panel and in the Notes lists and exports.
8. **Optional**:
   - injects delivered by chat (a per-inject channel `chat`, an `inject` room, follow-ups as
     posts in it; `Inject.effective_channel` and the delivery figures learn it);
   - "Create event" from a post;
   - voice notes, transcribed for SIMEX AI and the debrief;
   - an SSE sidecar.

Steps 2 to 5 are each a few days of work, with tests. Messenger is worth turning on in an
exercise from step 3, and is complete for monitoring from step 4.

## Questions to confirm before building

1. **The name**: "SIMEX Messenger" (recommended, to avoid confusion with ChatSIMEX), or
   something else?
2. **Leaders reading members' direct chats**: recommended yes, read-only, like members' own
   mailboxes. The alternative is the team room and groups they are in only.
3. **A persona added to a group sees only posts from then on** (recommended), or the whole
   history.
4. **AI personas only answer when addressed** (recommended: `chat_npc_chime` off by default),
   or also chime in when SIMEX AI reads that a group needs them.
5. **The text in push notifications**: off (recommended, as for mail), or a preview.
6. **The hosting change**: the web component to 2 vCPU / 5 workers on exercise days, about
   $25/month more while it is on.
