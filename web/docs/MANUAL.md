# SIMEX ops: operator manual

For ExCon members who deliver the SIMEX injects, watch the team mailboxes, answer in character and grade the responses. The app is at **https://ops.simex.kobolabs.dev**; ExCon signs in there as `undac` (or their own staff login). Participants use SIMEX Mail at **https://mail.simex.kobolabs.dev** (a webmail-style sign-in page, then their team's mailbox) with the team logins you hand out. The two addresses share one sign-in: a participant who signs in at ops. is sent on to mail., and ExCon pages opened on mail. go back to ops. All times are **Nairobi time**.

Read section 7, "Safety controls", before your first shift.

SIMEX ops and SIMEX Mail are open source under the GNU AGPL-3.0: every page of both links to the source code on GitHub (the footer in ops, the bottom of the folder list and the sign-in page in SIMEX Mail). Exercise content is never in the repository.

---

## 1. How it works in one minute

- **Injects.** Each email inject from the MEL is one row on the **Injects** tab, with a send time, a persona as sender (e.g. `ccdpr@simex.kobolabs.dev`), recipients, a subject, a body and attachments.
- **Delivery.** One switch in **Settings → Delivery channel** decides how injects reach the teams (the status bar shows it: *via email* / *via SIMEX Mail*):
  - **Email (Resend)**, the main flow: injects go to the participants' real addresses from the personas (`ccdpr@simex.kobolabs.dev`), and their replies come back into the app.
  - **SIMEX Mail**, the backup: injects land in the teams' mailboxes inside this site, a webmail client participants open in their browser. No real email is involved. Use it if email has problems (see 5.6).
  - **Per inject**: each inject's own channel field decides (mixing both).
- **When.** At each inject's time the scheduler delivers it, but only if **live sending is armed** and nothing is paused. One inject usually becomes one message per team.
- **Answers.** Participants reply to (or write to) personas from SIMEX Mail. Every answer appears on the **Replies** tab, matched exactly to the inject and team it answers. You read it, answer in character, and grade it against the inject's rubric.
- **Nothing reaches participants by accident:**
  - live sending is off until someone arms it;
  - **Pause all sending** stops everything at once;
  - an inject that missed its time is held as **overdue**, never delivered late on its own.

## 2. The tabs

The tabs come in two groups: what you use while the exercise runs (**Injects**, **Replies**, **Team inboxes**, **Compose**, **Dashboard**, **Log**), then, after the divider, setting it up (**People & logins**, **Senders**, **Test runs**, **Settings**).

| Tab | What it is for |
|---|---|
| **Injects** | The schedule, grouped by day (click a day to fold it; each shows its count, time span, statuses, files and how many need attention). Filter by day, status or **Needs attention**; open an inject to edit, preview, test or send it; change the channel in bulk. Import from the MEL is here too, and **Draft with AI** (5.8). |
| **Replies** | Everything participants sent: team summary, inject × team grid, **Read / opens & clicks**, the inbox, and grading. |
| **Team inboxes** | Each team's mailbox at a glance (received, unread, sent, last sign-in), **View as team**, the team leader's and each member's own mailbox, **Open the persona desk** (5.10), and **Clear mailboxes** after testing (6.2). |
| **Compose** | Write as any persona (but the team leader persona) to teams, team members or team leaders, now, without making an inject: the persona desk's **Write as a persona** on these screens (5.3), with what was sent from here and the desk below. |
| **Dashboard** | The exercise at a glance: per team, replies, response times, scores, sentiment over time and a funnel; the rubric heatmap; flags, possibly AI-written replies, what teams ask for and commit to; an AI-written exercise summary (5.7). **End-of-exercise reports** for each team and for ExCon (6.1). |
| **Log** | Every delivery the app made, with content and status, plus the activity log of every action. |
| **People & logins** | Teams, participants, and their logins (create, reset password, disable, printable credential sheet). |
| **Senders** | The personas: username (`cnn`), display name ("CNN Breaking News"). Participants write to them (their address book grows with use). |
| **AI personas** | Only while Settings → AI personas is on: personas that answer the teams on their own, in character (5.9). |
| **Test runs** | Replays injects on a fast clock into the **Test team** mailbox (and, for email injects, to a test address). |
| **Settings** | **Delivery channel**, footer, subject prefix and inject code in the subject, **AI analysis** and **AI instructions**, team-to-team messaging, attachment limit, email options, grace period, **live sending switch**, backup and restore, and **Start afresh** before going live (6.2). |
| **Help** | This manual (top right, next to your name). |

The status bar under the tabs shows the Nairobi clock, **Live disarmed** (green) or **LIVE ARMED** (red), **ALL SENDING PAUSED** when paused, when the scheduler last ran (red if it stopped), the next inject due, counts of overdue and failed injects, and the **Pause all sending** button.

---

## 3. Before the exercise (the day before at the latest)

### 3.1 Load the injects
1. In the knowledge base: `python3 -I scripts/export_inject_bundle.py`. It writes `work/simex-ops/injects.json` and `attachments.zip`.
2. **Injects → Import / update**, upload `injects.json` (or `MEL-MIL.xlsx`) and click **Check file**.
3. Read the summary (new / updated / with local edits / unchanged), then **Apply import**. New injects are **in-app**.
4. On the same page, upload `attachments.zip`. Files are matched to injects by the names in the MEL's *Supporting documents* column. Matching runs in the background once the upload has finished: the page shows its progress (*matching … of … injects*) and then how many files were attached and which matched no inject. You can leave the page meanwhile. If it shows *interrupted* (the app restarted, e.g. a deploy), upload the zip again: files already attached are replaced, not duplicated.
5. Open a few injects: documents the MEL lists but that are still missing are shown in orange under **Attachments**. Upload those by hand (max 30 MB each; put a link in the body for the 1-20 briefing video).

**Start over** (bottom of the same page): delete all attachments (to load a new zip), or all injects with their attachments, rubrics, grades and test-run items (to import the MEL from scratch). Type DELETE to confirm. Deliveries, inbox mail and replies stay, no longer linked to an inject. Download a backup first (Settings).

Only participant-facing fields are imported. Expected outcomes, story, theme, key figures and the ExCon description never enter the app. Re-importing updates what changed in the MEL, keeps your edits (unless you tick "Overwrite my local edits", which only overwrites fields the MEL itself changed) and never touches what was already delivered. A new inject is **scheduled** only if it has a send date *and* a readable time still in the future; one with no time ("TBC") or a time already past comes in as a **draft** for you to time and schedule. An import that fails part-way changes nothing.

**When the MEL changes** (as often as you like, also during the exercise): download the sheet again and repeat steps 2–3. Injects are matched by **Inject #**, so ask the MEL authors never to renumber or reuse one (a renumbered inject arrives as a new one, and the old one stays).
- A changed **send time** also fixes the status: a draft that had no time becomes **scheduled**, an **overdue** inject moved to a later time is **scheduled** again, and an inject whose time was removed goes back to **draft**. The check page shows these as e.g. *overdue → scheduled*. A draft you unscheduled yourself stays a draft.
- Injects that are **no longer in the file** are listed under *Not in this file*. They are still sent unless you tick **Cancel the … unsent inject(s) that are no longer in this file** before applying. Injects you created here (not imported) and injects already sent are never cancelled.

### 3.2 Teams, participants and logins
**People & logins.** A new app has placeholder teams A–D (with placeholder people, no logins) and the **Test team**. **People** comes first, grouped by team (then the persona desk and anyone without a team): each team's heading shows its members, its leader, how many logins are shared or not, who has no personal email or no login, its sign-in slips, and **Email logins not shared yet** for that team. Click a heading to fold the team (remembered in your browser); **Expand all / Collapse all** above. Teams and Team roles are below.

**Team members and team leaders.** Each team has **team members**, the participants (its focal point, or a few people sharing the team's one mailbox), and **one team leader**: an ExCon person who monitors the team, is not part of it, and plays the team leader persona (3.3). A team can't have two leaders, and a leader needs a team.

**Team roles.** Members can also hold a function within their team, such as *Deputy Team Leader*, so that the Dashboard and the reports can show behaviour by role. Add the roles under **Team roles** (name and order; renaming one renames it for everyone holding it, deleting one leaves its members without a role), then pick each member's in their row in **People** (the select appears for team members only; team leaders, from ExCon, have none), or give it in the roster's role column. A team role changes nothing in SIMEX Mail: the member still shares the team's inbox like the others.

1. Paste the roster into **Paste a roster**, one line per person: `name,email,team code,role` (email optional for in-app; role `tl` for the team's leader, empty for members, or a member's team role such as `Deputy Team Leader`, added to Team roles if new). Keep **create logins** and **remove the placeholder people** ticked.
   ```
   Jane Doe,jane@example.org,a,tl
   John Roe,,a,Deputy Team Leader
   Ann Poe,,a
   ```
2. The page then shows **New logins: shown once**: username and password for each new person. Click **Printable sheet** and print it (one slip per person, with the SIMEX Mail address https://mail.simex.kobolabs.dev and a **QR code**), or copy it somewhere safe. Passwords are not stored readable; if one is lost, use **reset password** on that person.
   - **QR sign-in.** Scanning a slip's QR code with a phone's camera signs that person in on the phone and opens **Set up this device**: one button to turn on alerts (on an iPhone, first **Add to Home Screen**; the home-screen app is then signed in already). A code works on any number of devices for 14 days from printing, or until that person's password is reset or their login disabled; it never signs in an ExCon login (persona desk logins get one). Treat a slip like the password: hand it to its person.
   - **Sign-in slips** prints slips again (QR code and username, passwords unchanged): for everyone (link above the People table), a team (**sign-in slips** on its row) or one person (**slip** next to their username). Use it the day before, so the codes are fresh.
3. Each team shares **one mailbox**: everyone in Team A sees Team A's mail, and what one member sends appears in everyone's *Sent*, with the sender's name. With **personal mailboxes** on (Settings, on by default) each member also has **My mail**, their own mailbox at `name@<team code>.teams.simex.kobolabs.dev` (or the address set on the Senders tab, 3.3): a copy of everything delivered to the team (an inject for one person, only theirs), plus personal mail between teammates and with their team leader (To, Cc, Bcc, as in any webmail). Members choose the **From** address of each message (the team's or their own, colour-coded); what goes from the team's address is in the team inbox's Sent. A member's answer to a persona from their own address is also put in the team inbox's Sent, and is the reply ExCon sees and grades, as before; mail only between members never reaches the Replies tab. Reading a copy in My mail counts as the team reading the inject. The team leader writes to the team (team inbox and every My mail) or to chosen members only (their My mail); In the leader's mailbox, mail from the team is labelled **Team account** or **Personal account** by the address it came from. Members are alerted (and pushed) for team mail and personal mail alike, in either tab. ExCon and the leader read every member's My mail read-only (**Team inboxes** → the member's name; the leader's **Team members** tab).
4. **Team leaders** sign in at the same SIMEX Mail address and get five tabs: **My mail** (their own mailbox, as the team leader persona), the **team inbox** read-only (with Claude's reading of each reply from the team: who wrote it, tone, quality, flags, possibly AI-written), **Team members** (each member with their team role, what they have written and read, alerts, last sign-in, and their own mailbox read-only), **Team leaders** (the other team leaders' mailboxes, read-only, one per team in the side bar; never the other teams' inboxes) and the team's **Dashboard**, plus, while AI personas are on (5.9), **AI personas**: the team's conversations with them. They only ever see their own team's inbox, write only to their own team, and never see injects, rubrics or grades. Their guide is under **?** in SIMEX Mail. Changing someone's role takes effect on their next page; their devices then turn alerts on again.
5. **Personal emails and login emails.** Each person has, next to the email-channel address, a **personal email**: their own address outside the exercise. It receives their login email and the email copies of injects marked *also by email to personal addresses* (3.5); the email channel still uses the first address.
   - **Import a roster** (right column) takes a spreadsheet (.xlsx) or CSV with a header row `Name`, `Email` (the personal email), `Team` (`A` or `Team A`) and optionally `Role` (`tl`). **Check the roster** shows the plan before anything changes: who is matched (same email, same name, or a *similar name* in the same team, e.g. "Joel Cretet (Tony)" for "Joel Cretet": check those), what changes (personal email, team), who is added as a team member, lines skipped (an unusable address, no team) and who is here but not in the file (left as they are). **Apply** with **create logins** ticked gives the new people logins (shown once). Names, usernames and the email-channel address are never changed. New usernames are the team code, first and last name (`c.karina.avalos`). The roster paste takes a fifth column for the personal email too: `name,email,team code,role,personal email`.
   - **Email logins** (right column) emails each person, at their personal email, their username, a **new password**, the QR code and sign-in link of their slip, how to start (phone: scan, alerts, Home Screen on an iPhone; laptop: the address and password) and what their mailboxes are, plus an optional **note** (e.g. when and where the exercise starts). Choose everyone or a team; by default only those whose login isn't shared yet (tick **also those already shared** to send new passwords to them too). **Answers go to**: an ExCon address for their answers (remembered); left empty, answers land on the Replies tab, ignored. One person: **email login** in their row. Sending issues a new password, so their printed slips and old password stop working; the Log keeps each email without its password and link. The QR code and link work for 14 days from sending, the password until it is reset.
   - Each row shows **shared** (when, by email or by hand) or **not shared**, and *email failed* or a bounce. A slip handed out on paper: **mark shared** (**unmark** to undo). **Reset password** marks the login not shared, as nobody has the new one yet.
6. **Persona desk** (5.10): role-players who answer as the personas get the role **Persona desk (ExCon)** (no team; roster role `desk`, team code empty). They sign in at the SIMEX Mail address with their username and password (or scan the QR code on their slip or in their login email: it opens **Set up this device** for the persona desk's mailbox) and reach SIMEX Mail only, never these screens.
7. Give yourself a login in the **Test team** (Add person → team "Test team" → create login) to try SIMEX Mail as a participant.

- **Team code** fills `{team}` in addresses (`undac-{team}-tl` → `undac-a-tl@…`); **team name** fills `{team_name}`.
- **Disable** blocks a login without deleting the person; **delete** removes both.
- More ExCon logins: Django admin (`/admin/`, staff users or the `excon` group).

### 3.3 Personas (Senders)
One row per persona, created from the MEL's sender aliases. Fix display names (e.g. `CCDPR` → "CCDPR Deputy Director"); **rename in injects** keeps injects in step when a username changes. Add personas the MEL lacks (e.g. `cnn` / "CNN Breaking News").

**Which mailbox.** SIMEX Mail names the mailbox at the top, next to the logo, each kind in its own colour: a team inbox (blue), a member's **My mail** (green), a team leader's mailbox and tabs (purple), the persona desk (teal), and ExCon's read-only views (amber, with *Back to ExCon*). The search box says which mailbox it searches.

Active senders are who participants can write to. Their **address book** starts empty and grows with use, as in a webmail: whoever wrote to their mailbox or was written to (a member's book is their own mailbox's and the team inbox's), latest first. To write to a persona first, a team types its full SIMEX Mail address (from an inject, a document, a signature): SIMEX Mail never lists the personas they haven't been in touch with. Teammates' and the team's addresses are found the same way. Team leaders' and the persona desk's books are full lists, as before (the desk's lists every team and every member). In SIMEX Mail a member is named with their team, e.g. **Team A (Jane Doe)**, as the From of what they send, in To and Cc and in the address book. Untick **active** to make a persona unreachable.

**The team leader persona.** Tick **team leader** on the persona each team's leader plays (`undac-{team}-tl`; the import ticks it for you). Then, for each team:
- what is sent as it (injects, follow-ups, your **Answer as** on the Replies tab, and what the leader writes) lands in the team's inbox as usual **and** in the leader's **Sent**, in the same conversation;
- what the team writes to it (or copies it on) is a reply as for any persona (Replies tab, AI analysis, grading) **and** arrives in the leader's **Inbox**;
- the leader's own answers to the team show on the reply's page as *answered by the team leader*, so check there before answering as the leader yourself.

**Address in SIMEX Mail.** Each persona has a **username** (what injects use, and its email address: `username@simex.kobolabs.dev`) and, for SIMEX Mail, any address you like: the username and domain after it on its row, e.g. `news` @ `cnn.com`. Empty parts default to the username and simex.kobolabs.dev. Participants see that address on messages and in their address book. Two personas can show the same name on different domains: `news@cnn.com` (username `cnn-news`) and `news@reuters.com` (username `reuters-news`); only the full SIMEX Mail address has to be unique.
**Team and member addresses.** Below the personas, set the SIMEX Mail addresses of the teams' shared inboxes and of members' own mailboxes (My mail) the same way, a username and a domain: `{team}` is the team code; for members `{name}` is the full name (`jane.doe`), `{first}` and `{last}` the first and last names. For example `undac-{team}` @ `epeigonia.org` gives Team A `undac-a@epeigonia.org`, and `{first}.{last}` @ `epeigonia.org` gives `jane.doe@epeigonia.org`. Empty parts use the defaults (`a@teams.simex.kobolabs.dev`, `jane.doe@a.teams.simex.kobolabs.dev`). A member whose address is taken already (a namesake in another team, a team or a persona) gets their number after it (`jane.doe.42@…`); a team address can't be a persona's, nor a persona's a team's. The table under the form shows each team's address and a few of its members'. SIMEX Mail only (nothing is emailed there); mail already sent keeps the address it had, and answering it still reaches the right mailbox. Set them before the exercise.
**Colour on the persona desk.** Each persona has a colour on the persona desk (5.10), picked from its username; choose another in its row (the select next to *rename in injects*) when two look alike.
By email every persona sends from **its username @simex.kobolabs.dev**, the only domain Resend can send from, so switching to email (5.6) changes the addresses teams see.
When you import the MEL, the check page offers to give senders without a domain the address in the MEL's sender column (ticked; it lists them). If the MEL uses one name on several domains (`news@cnn.com`, `news@reuters.com`), they become separate personas, `cnn-news` and `reuters-news`. An address you set here is never changed by an import.

### 3.4 Settings
- **Default footer:** "EXERCISE EXERCISE EXERCISE …" at the bottom of every message.
- **Subject prefix** (e.g. `[SIMEX] `) and **put the inject code in the subject**: every inject's subject then starts with its code, e.g. `[1-15] Request for assessment`, so teams and ExCon can refer to it. Off by default; it applies to injects delivered after you turn it on (both channels).
- **AI analysis:** off by default; see 5.7.
- **Teams may write to other teams:** off by default (teams write only to personas). Turn it on if the scenario wants OSOCC-to-OSOCC traffic; those messages show under Team inboxes but are not "replies".
- **Personal mailboxes:** on by default. Each member also has their own mailbox (**My mail**) next to the team inbox (3.2). Off: only the team inbox, as before; the My mail copies already made stay but aren't shown.
- **Max attachments per message:** what participants may attach (default 10 MB).
- **Email channel:** reply-to (leave empty), ExCon Bcc, test addresses. Only matters for injects on the email channel.
- **Grace period:** 10 minutes by default (see 7.3).

### 3.5 Go through the injects
Start with **Needs attention** ticked: it lists only the unsent injects with something to fix, each with tags under its subject (hover a tag for the detail):
- red: **N docs missing** (named in the MEL's *Supporting documents* but not attached; upload them on the inject page or remove the line), **no sender**, **no subject**, **no body** (and nothing attached), **failed**;
- yellow, to look at: **no attachment** (the text says *attached* but nothing is), **unknown sender**, **placeholder** (XXX, [INSERT …], TBC, a list of team slots to cut down), **ExCon note** (`***` left in the body), **subject is a file name**, **no send time**;
- purple **AI: …**: what Claude found reading the inject (section 5.7), e.g. a weekday that doesn't match the date, a greeting that doesn't fit the recipients, a document the text mentions that isn't attached.

The inject page lists the same points under **Before sending**. If an AI point is wrong or intended, **Dismiss the AI points** hides them from the list until the inject changes. Sent and cancelled injects are never flagged. Folded days are remembered in your browser; **Expand all / Collapse all** are in the bar, and **all** selects only the injects of open days.

Then open each one:
- **Send at:** the real (Nairobi) time; the MEL's scenario time is separate.
- **Channel:** set for all injects by Settings → Delivery channel; the inject page shows which one applies. Only in *Per inject* mode can you choose here.
- **Sender username / name**, **Recipients** (*one per team*, *one per participant* — lands in that person's team mailbox addressed to them —, *everyone*), teams to leave out, and **also to team leaders**: a copy in each team leader's **My mail** (by email: a copy to the leader's address). The import ticks it when the MEL's recipients name the TL ("Team members & TL"); the Injects list shows *+ team leaders*. An inject from the team leader persona is in the leader's Sent anyway. A follow-up reaches the leaders' copies too. A leader who answers their email copy is kept on the Replies tab as *from the team leader* and never counts as the team's reply.
- **Also by email to personal addresses** (while Settings → Delivery channel isn't *Email*): delivered in SIMEX Mail as usual **and** emailed by Resend to each recipient's personal email (3.2): the team's members (one email each, so their addresses stay private; *everyone* and *only the addresses below* as one email with every participant in Bcc), and the team leaders when *also to team leaders* is ticked. The inject's extra To, Cc and Bcc are not used. People without a personal email are listed in the preview's warnings. An answer to the email copy comes into the Replies tab like any email reply (matched to the inject and the person). Test sends and test runs leave the email copies out; the preview shows them. The Injects list shows *+ email*.
- **Subject and body:** plain text; `{team}`, `{team_name}`, `{name}`, `{first_name}` are filled per message.
- **Preview** (right): exactly what each team gets. **Clear every orange warning.**
- **A different version per team** (e.g. 1-12, a letter of attestation for each team): on the inject page, **Duplicate** → *Copy for* **Team A only**, with **and take that team off 1-12** ticked, then **Duplicate inject**. The copy, **1-12-A**, is a draft for Team A only, with the same send time, files and rubric; 1-12 no longer goes to Team A. Change the copy (swap its attachment, edit the body), **Save** and **Schedule** it, and repeat for the other teams (from 1-12 each time). *The same teams as this one* makes a plain copy (1-12.2). Copies are never touched by a re-import of the MEL, and each copy counts as its own inject on the Replies tab and in the reports. The Injects list names the teams each inject goes to in the **To** column (*All teams* when none are left out).
- **Replies & rubric:** tick "teams are expected to reply", set "within (minutes)", add criteria with max points (or copy another inject's rubric). Tick **critical** for a key inject, and **must-have** for a criterion a team must not miss: in the end-of-exercise reports (6.1) a critical inject with no reply in time, or a must-have criterion graded at a third of its points or less, is listed as a *critical point missed*. Set them before the exercise.

Bulk: tick injects in the list and use **Schedule / Back to draft / Cancel / Shift by N min / Channel: In-app | Email / Personal email: Also email | SIMEX Mail only** (the Channel buttons appear only in *Per inject* mode; otherwise Settings decides for all injects; the Personal email ones unless the channel is *Email*).

### 3.6 Prepare the backup channel
Even if you plan to use email, make SIMEX Mail ready so you can switch in a minute:
1. **People & logins:** check every participant is in the right team and create their logins (one by one or by pasting the roster). Passwords are shown once: print the **credentials sheet** and keep it with ExCon.
2. Sign in once as a test participant (or use **Team inboxes → View as team**) to see what teams would see.
3. Note the address to give teams: **https://mail.simex.kobolabs.dev** (they land in their mailbox after signing in).
4. Hand out the slips and have everyone **scan the QR code** with their phone: it signs them in and walks them through alerts, including, on an iPhone, adding SIMEX Mail to the home screen (only then do iPhones get notifications with SIMEX Mail closed). On a laptop they sign in with the username and password and click **Turn on alerts**.

### 3.7 Rehearse (at least once) — section 4.
End the rehearsal as you will end the exercise: grade the Test team's replies, then compile its report (**Dashboard → End-of-exercise reports → Test team**, 6.1) to check the figures and see what the write-up looks like. Clear the Test team's mailboxes afterwards (6.2); when every rehearsal is done, **Settings → Start afresh** (6.2) so the exercise starts with empty mailboxes, Dashboard and Log.

### 3.8 Take a backup
**Settings → Download backup.** One file with everything: injects, attachments, mailboxes, logins, grades.
Late in the exercise it can be several hundred MB (every file participants sent is in it): download it on a good connection. The database also has DigitalOcean's own daily backups.

---

## 4. Testing without reaching participants

### 4.1 Send one test
On an inject, **Test → Send test**. In-app injects land in the **Test team** mailbox (subject `[TEST]`, a banner says which team it was really for); email injects go to the address you type. The inject's status doesn't change.

### 4.2 Test run (a rehearsal on a fast clock)
**Test runs → New test run**: choose the injects (all, a day, or ids), the start, the timing ("one every 30 s" or "real gaps, 60× faster"), and where to deliver: **SIMEX Mail** (everything lands in the Test team inbox, with a banner naming the real team) or **email** (to a test address pattern, `you+{key}@gmail.com`), whatever Settings → Delivery channel is set to for the live exercise. With *Per inject* there is a third choice, each inject's own channel. **Dry run** builds and logs only. The run page refreshes every 10 s and has **Pause / Resume / Next one now / Cancel**, and a link to the **Test team inbox**.

To rehearse the whole loop, sign in as your Test team login (another browser or a private window), open the test messages, reply, and watch them appear under **Replies → Test team**.

### 4.3 View as team
**Team inboxes → View as team** opens SIMEX Mail exactly as that team sees it (**Team leader** next to it: that team's leader's mailbox), **read-only**: nothing is marked read, nothing can be sent, and your session stays ExCon. Use it to check what a team has received, or to look over their shoulder during the debrief. You also see the labels the team has made to organise its mail (Gmail-style folders); they're the team's own and change nothing on the ExCon side.

---

## 5. During the exercise

### 5.1 Start of each day
1. Status bar: the scheduler ran seconds ago (if it says "is the worker running?", see 8).
2. **Injects** for today: everything that should go out is **scheduled**; shift times if the exercise runs late.
3. If you deliver by SIMEX Mail: hand out (or re-check) logins; teams sign in at **https://mail.simex.kobolabs.dev** and keep the tab open.
4. **Settings → Live sending:** type `SEND LIVE`, **Arm live sending**.

### 5.2 Watching deliveries
- **scheduled → sending → sent**. In-app deliveries are instant; **partial** / **failed** mean some or all deliveries failed (in-app this is rare: usually a team without an inbox).
- **Team inboxes** shows what each team has received and whether they have read it.
- **Replies → Read / opens & clicks:** for in-app, *read* = someone in the team opened the conversation (with the time to first read and how often it was opened), *downloads* = attachments downloaded. For email, Resend's opens and clicks (a lower bound) and bounces.

### 5.3 Sending by hand, holding, changing
- **Send now:** on the inject, type its id under **Send live now**. In-app messages land at once.
- **Hold:** **Back to draft** or **Cancel inject**. **Edit:** change and **Save** — what is saved at the send time is what goes out.
- **Ad-hoc inject:** **Injects → New inject**: any persona to any teams, participants or (email) addresses. It starts a new conversation; use it for new information, not to correct one already sent.
- **Just a message:** **Compose** writes as any persona but the team leader persona to the teams, members and team leaders you tick, at once, with no inject (as **Write as a persona** on the persona desk, 5.10): one message each, in SIMEX Mail or by email as the delivery channel is, logged as kind *desk* (kept out of the injects' figures; the teams' replies to it are matched like any other). A member's goes to their own mailbox only. A team leader's (e.g. **Team A leader (Jane Doe)**) goes to the leader's own mailbox only, never the team's (by email, to the leader's email address). SIMEX Mail doesn't let a leader write to personas, so they can only read it there; by email, their answer comes into the Replies tab as *from the team leader*. Live sending must be armed (but for the Test team) and sending not paused. Below the form: everything sent from Compose and the desk, with who sent it. Its conversations show on the persona desk, where you answer the teams' replies.
- **Follow-up / correction** (a document or link was missing, a figure was wrong): on the delivered inject, **Send a follow-up**. Tick who gets it (every recipient of the inject is ticked; untick the teams that don't need it), keep the subject `Re: {subject}` ({subject} is the original subject, so mail clients group it with the original), write the message (placeholders {team} {team_name} {name} {first_name} work), tick attachments of the inject or add new files (they are also added to the inject), type the inject id and **Send follow-up**. It comes from the inject's persona in the team's existing conversation (in-app, delivered at once) or as a reply in the email thread (email, sent by the scheduler within seconds, quoting the original). It is a live send: live sending must be armed and sending not paused. Teams' replies to it count as replies to the inject, and it shows in the conversation on the reply page and in the Log (kind *followup*).
- **Let Claude write it:** a new inject, a follow-up or a persona's answer can be drafted with AI first (5.8); you still send it here.
- **Not sent yet?** Just fix the inject (add the attachment, edit the body) and **Save**; no follow-up needed.
- **Stop everything:** **Pause all sending**; **Resume sending** continues without duplicates.

### 5.4 Replies and grading
- **Replies tab** (refreshes every minute): the **Teams** table, the **Injects × teams** grid (green replied, orange late, red overdue with no reply, blue waiting), **Read / opens & clicks**, and the **Inbox** of everything teams sent to personas.
  - In-app replies are matched **exactly** to the message they answer. A *new* message a team writes to a persona is matched to that persona's latest message to the team and marked **guess**; check it. **unmatched** needs assigning.
  - Out-of-office and other automatic answers to an email inject are kept but set to **ignored** (they don't count, get no AI persona answer and no AI analysis); set the Status back if one was a real reply.
- **Inbox → Conversations** (the default) shows one row per back-and-forth: an in-app conversation, or for email the same team, persona and subject. It gives the latest message, how many the team sent and how many were answered, and **awaiting answer** when the team wrote last and no persona has answered since. **Messages** lists every message on its own, as before.
- **A reply's page** shows the whole conversation in order: the inject (or message) that started it, each message from the team and each persona answer. The highlighted one is the message the page is about; click another to open it. **Answer as <persona>** always answers the latest message, so the conversation stays in one thread.
- **Files** show under the message they came with (the team's, the inject's or a persona answer's): click the name to download. Photos and screenshots (PNG, JPEG, GIF, WebP) are previewed in the page; click one to open it full size. A file too large to keep shows as *too large, not stored*.
- On that page you can also read it, **view in the team's mailbox**, correct the **Match**, set **Status** (ignored replies don't count), and **Answer as <persona>**: the answer lands in the team's mailbox in the same conversation (or goes by email for email threads), and their next reply is tracked too. **Attach files** adds files from your computer, and **Attach documents of <inject>** any of the matched inject's documents (up to 28 MB in all). An answer can be just files.
- **Grading:** from a grid cell or **Grade …**: all of the team's replies to that inject together; a score per criterion, comment, name; **Save and next team**. Non-responses can be graded too. **Export grades (CSV)** for the debrief.
- **Grade as you go, with the reports in mind:** the team's end-of-exercise report is written from your grades and comments, so write a comment as you would say it to the team. Answer teams from the reply page (or an AI draft) rather than outside the app: the reports count what teams asked for and how fast it was answered.

Write rubrics as observable behaviour ("acknowledged within 30 min", "named the cluster lead"), 2–4 points each. Nothing is copied from the MEL's expected outcomes.

**Notifications.** **Notifications** in the top bar (with the number you haven't read) opens a panel on the right listing what has happened, newest first: new replies (✉), AI flags (⚑ *needs ExCon attention*, *safety*), injects that failed, were only partly delivered or went overdue (⚠), and finished jobs and system news (⚙: an AI draft ready, the exercise summary or the end-of-exercise reports written, inject documents imported, the scheduler stopped). Click one to open it (it is then read); **✕** dismisses it. The buttons at the top show only replies, flags, delivery or system news; **Mark all read** and **Clear all** act on what is shown. Read and dismissed are yours alone: each ExCon login has its own list, the same on every computer it uses. Esc or a click elsewhere closes the panel; **📌** keeps it open beside the page as you move between tabs (click it again, or ✕ at the top, to unpin). Notifications are kept even while no ExCon page is open, so nothing is missed overnight.

**Alerts without refreshing.** Every ExCon page checks for news every 10 seconds. Something new shows a short toast at the bottom right while the panel is closed (click it to open it), plays a chime (not for finished jobs), updates the **Notifications** and **Replies** badges and the tab title and icon, and shows a desktop notification while the tab is in the background. **🔔 Alerts** in the status bar turns the sound and desktop notifications on or off; click it once on each computer to allow desktop notifications. Open tabs share what has been seen, so an alert comes once however many ExCon pages are open. Participants get the same kind of alerts in SIMEX Mail (a chime, a blue bar, the tab badge and a desktop notification once they click **Turn on alerts**). Their notifications are also *pushed*: a device that turned on alerts is told about each new message even with SIMEX Mail closed (on an iPhone, only from the home-screen app). They show the sender and subject, nothing more. **Sign out** stops them on that device (so a shared phone or laptop never shows the last team's mail); signing in again turns them back on. Pushes need the `VAPID_PRIVATE_KEY` secret; without it, alerts only work while SIMEX Mail is open.

### 5.5 End of each day
**Settings → Disarm** (unless injects run overnight), **Download backup**, deal with overdue or failed injects, and grade the day's replies while they are fresh (the reports use the grades, 6.1).

---

### 5.6 Switching to SIMEX Mail when email has problems
Switch when teams don't receive emails (spam folders, bounces, the venue network blocking mail, Resend down or out of quota) and it can't be fixed quickly.
1. **Pause all sending** (top right) while you decide, so nothing half-goes.
2. **Settings → Delivery channel → SIMEX Mail**, type `SWITCH`, **Apply**. The status bar now says *via SIMEX Mail*.
3. Tell the teams (in person or by phone): open **https://mail.simex.kobolabs.dev** and sign in with the login on their credentials sheet.
4. Re-deliver what they missed: open the injects that went out by email but didn't arrive, **Back to draft** if they show *sent*, then **Send now** — they now land in the team mailboxes. (Or select them in the list and **Schedule** with a new time.)
5. **Resume sending.** Everything not yet sent goes to the mailboxes from now on.

Replies to emails sent earlier keep arriving on the Replies tab. To go back to email, switch the channel back the same way. **Per inject** mode lets you mix: e.g. personal emails (attestations) by email, everything else in-app.

### 5.7 AI analysis and the Dashboard
When **Settings → AI analysis** is on, Claude reads what teams send. It is for ExCon only: participants never see any of it, and it never changes a grade.
- **Each reply** (Claude Haiku, within a minute of arriving): a one-line summary, tone and sentiment, clarity / completeness / actionability (1–5), what the team asks for and commits to, the figures it states, flags (*misreads the inject*, *needs ExCon attention*, *safety or duty-of-care issue*, *sent to the wrong persona*, *steps out of the exercise*, *no action or decision*), and how likely the text is **AI-written** (0–100 %). It shows on the reply's page under **AI analysis**, with **Re-run**.
- **Each team's thread on an inject with a rubric** (Claude Sonnet, once the thread has been quiet for 3 minutes, and again if the team writes more): suggested points per criterion with a reason and a quote from the reply. On the grade page each criterion shows **AI n** with the reason; **Fill in the AI suggestions** copies them into the form, and nothing is saved until you click **Save**. The grid on the Replies tab shows *AI n / max* under the grade.
- **Team leaders** see Claude's reading of their own team's replies (not the grade suggestions, not other teams): in the team inbox under each message and on their Dashboard tab in SIMEX Mail.
- **"AI-written"** is a hint to look closer, never proof: it is judged from the style of the text and how fast it was written after the team opened the inject. Short messages and non-native English make it unreliable. Don't confront a participant on this alone.
- **Dashboard tab:** a tile per team (expected injects answered, median first reply, graded score, AI-suggested score, sentiment of each reply over the exercise, tone mix, a delivered → read → answered → graded funnel), the **rubric results** heatmap (switch between *Graded* and *AI suggested*), **where the AI and the graders disagree** (a quarter of the points or more: check the rubric or the reply), **Flags**, **Possibly AI-written** (60 % and above), **What teams are asking for** and **Decisions and commitments**. Once there are team roles (3.2), **By team role** groups the members of all teams by role: members, messages written (in all and per member), their share of the writing, injects answered, median minutes to reply from first seeing what they answer, inject messages read per member, documents opened, inbox actions (star, archive, label, search, send…), Claude's quality reading, likely AI-written, and the messages written per team (with how many members hold the role there). A team leader's Dashboard has the same table for their team, and shows each writer's team role. **Write the summary** produces a situation summary across teams (themes, each team's strengths and concerns, what ExCon may want to act on now, points for the hotwash) in about a minute.
- **Each unsent inject** (Claude Haiku, before the exercise and again whenever the inject changes): reads it as ExCon wrote it, with the list of all injects for context, and reports only concrete problems: attachments it mentions but doesn't have, unfilled placeholders, notes for ExCon left in, names, places or figures that contradict each other, a date the inject contradicts itself on (a weekday that doesn't match its date), a greeting that doesn't fit the recipients, a persona that doesn't fit the content. It knows the exercise compresses time: jumps in scenario time between injects, scenario times that differ from send times, and injects out of scenario-time order are intended and not reported. They show as purple **AI: …** tags on the Injects list and under **Before sending** on the inject page, with **Check again with AI** and **Dismiss**. On the Injects tab, **Check unchecked with AI** reads only the unsent injects that have no review of them as they are now (new, changed since, or failed), **Check all** re-reads every unsent inject (the count next to them says how many have been read as they are now), and **Check with AI** in the bar above the list reads just the ones you tick. A full pass over 100 injects costs about five cents.
- **Settings → AI analysis:** on/off; *analyse every reply as it arrives* (off: only when you click **Analyse now**); include Test team replies (useful in rehearsals); *review unsent injects*; a **daily spending cap** in USD (default 5; a full exercise day costs well under a dollar). The card shows today's calls, spend and the last error.
- **Settings → AI instructions:** what Claude is told for each kind of reading: the **exercise context** (opens all of them), the **inject check**, **reading each reply**, **rubric suggestions**, the **exercise summary**, **Draft with AI**, and the **team debrief** and **ExCon** reports (6.1). Open one, edit the text and click **Save instructions**; an edited one is marked *edited* and has **Reset to default**. Changes apply to the next reading (use **Re-run** to redo one); changing the inject check (or the exercise context) checks every unsent inject again. You can change what Claude looks for and how it writes, not what it is given or the fields it answers with. Never paste control-side material (expected outcomes, story, key figures) here.
- What Claude sees: the inject as the team received it (for the inject review: as written, with the subjects of the other injects), the rubric you wrote, and the teams' replies (for a draft: all the injects, the personas and what the teams wrote). Nothing else is stored in the app to send.

### 5.8 Draft with AI
Claude (Sonnet) can draft a message for you to edit and send. It never sends anything. Settings → AI analysis must be on; a draft takes a few seconds and costs about a cent (ten cents for the first one after the injects change, which Claude has to read in full again).
- **Where to start:**
  - **Injects → Draft with AI**: say what you need ("the governor of Plaida complains on the radio that the team ignored the island"), optionally an inject it follows on from and a team it reacts to.
  - **On an inject**, the **Draft with AI** card: *a new inject that follows on from this one* or, once it is delivered, *a follow-up in this conversation*. You can pick a team to react to.
  - **On a reply**: **Draft an answer** (the persona's answer in the same conversation) or **Draft a new inject reacting to this**.
- **What it uses:** every inject (the text of each one, and whether the teams have it yet), the personas, and what the teams wrote. When it starts from a team, it reads that team's whole thread on the inject and a line for each of its recent messages. It writes in the style of the existing injects from that kind of sender, keeps to the places, figures and names the teams have been given, and doesn't give away planned injects. The app holds no storyline, expected outcomes or key figures, so check any **new facts** it lists against the scenario.
- **The draft page** shows the message (persona, subject, scenario time, suggested send time and recipients, whether a reply is expected, files it mentions) and, for ExCon only, **why** (what it does in the exercise), **new facts to check**, **to confirm** and **built on**. **Revise** asks for changes ("shorter, from WFP instead") and keeps the earlier draft. **Recent drafts** lists them all.
- **Using it:**
  - **Make it a draft inject** creates an unscheduled inject `AI-n` with the draft's sender, text, scenario time, suggested send time, recipients (the team only, if the draft is for one team), reply expectation and the documents it mentions. The documents show as missing until you attach them. The AI review then reads it like any other inject. Edit it, then schedule or send it as usual.
  - A follow-up opens the inject's **Send a follow-up** form, filled in and ticked for the team it was drafted for.
  - An answer opens the reply's **Answer as <persona>** box, filled in.
  - Either way you read it and send it yourself. A persona Claude made up shows as *new persona*: add it on **Senders** or pick an existing one.
- The app records which injects, follow-ups and answers were sent from an AI draft, and whether you changed the text first. The end-of-exercise reports count them (6.1).

### 5.9 AI personas
Some personas can answer the teams on their own, like non-player characters in a game: a deputy governor, an agency contact, a journalist. Claude (Sonnet) writes each answer in character, after a delay drawn from the persona's timing, in the same conversation, in SIMEX Mail or by email. Turn it on in **Settings → AI personas** (off by default; AI analysis must be on too). It answers **the Test team only** until you choose **every team** there. The **AI personas** tab appears while it is on.
- **Making one** (AI personas tab):
  - **From the injects**: a profile for each persona your injects come from. Claude reads each one's injects to work out who they are and how they write. Their knowledge comes only from what the teams have been told, never from their planned injects. This is one call each, about a cent apiece.
  - **Generate with AI**: describe a new one ("the deputy governor of the worst-hit province: proud, defensive, knows which roads are cut; slow to answer"). Claude writes the name, address, profile and timing from your brief and the injects.
  - **From an existing persona**: give a persona on **Senders** a profile, written by you or by Claude from its injects.
  - Profiles Claude writes are **off**, and new personas are out of the address book, until you have read them and saved them. Check the **new facts** Claude lists against the scenario. Team leader personas and `{team}` personas can't be AI personas.
- **The profile**:
  - who they are: name, username, the address shown in SIMEX Mail, role, organisation
  - **personality and voice**
  - **what they know and may share**: only what the teams may learn, never control-side material
  - **boundaries**: what they won't share or promise
  - **timing**: *Instant* (1–3 min), *Responsive* (5–20 min), *Busy official* (20–90 min, sometimes within minutes, sometimes never), *Bureaucrat* (1–4 h), *Unpredictable* (often within minutes, else hours, 15 % never), or *Custom*, with optional office hours in exercise time
  - **Answers on its own**; **sends its answers straight away** (untick it to read and send each answer yourself); **in the address book**

  The wait is drawn when a message arrives. More messages in the same conversation before the persona answers get one answer to all of them.
- **What it knows when answering**: its profile, the other personas, every inject the teams have received, the conversation, and what it told the same team before. It doesn't invent official figures or events. When asked something outside its knowledge, it answers as a real person would ("I'll check").
- **Try it** (the persona's page): write as a team and see the answer at once, with how long it would have made the team wait. Nothing is delivered.
- **Answers**, on the persona's page and the tab:
  - *queued* (with when it is due): **Answer now**, or skip it
  - *held*: edit it, then **Send**, or **Don't answer**
  - *failed* (after three tries): **Try again**
  - *skipped* (its timing drew no answer, or you skipped it): **Answer anyway**

  Each answer shows what it shared, any flags (*asked for something it doesn't know*, *stepped out of the exercise*, *ExCon should look*, *pushed past its boundaries*) and a note for you. The Notifications panel tells you when one is sent, waiting for you or failed.
- On the **Replies** tab, the team's messages to an AI persona are replies like any other. A persona's answers are marked *AI persona answer*. They are left out of ExCon's own answer times in the reports.
- **Team leaders** get an **AI personas** tab in SIMEX Mail (read-only): every conversation their team has had with an AI persona, both ways, with when the persona's answer is due, and the AI personas their team can write to. They never see another team's conversations, or the profiles.
- **Pause all sending** stops the answers too. They go out when you resume. Answers count toward the daily AI cap.

### 5.10 Persona desk
A shared mailbox in SIMEX Mail for the people playing the personas, so answering the teams doesn't have to go through the Replies tab one message at a time. Give each role-player a login with the role **Persona desk (ExCon)** (3.2); you can open it yourself too (**Team inboxes → Open the persona desk**, or `/inbox/desk/` in SIMEX Mail). Several people can be on it at once: they share one mailbox (read, stars, labels, drafts).
- **Inbox**: every message a team writes to a persona, in SIMEX Mail or by email (once fetched), except to the team leader persona (the leaders play it), automatic answers and team leaders' own replies. **Sent**: every delivery from a persona: injects, follow-ups, answers from the Replies tab, the AI personas' answers and what the desk writes. A conversation is normally one team's; an email inject sent to all teams in Bcc is one conversation, which every team's answers join.
- **Telling the personas apart**: a coloured tag per persona (its colour on the Senders tab) and a grey tag for the team on every conversation and message, a coloured edge on each row and message, and in the side bar a filter per persona and per team with their unread conversations. Each persona message says whether it was an inject, a follow-up, sent by ExCon, by a desk member (by name) or written by an AI persona.
- **Reply** answers as the persona the team wrote to: it is that message's answer, exactly as **Answer as** on the reply's page (the Replies tab shows it, the team's next reply is matched to it), in SIMEX Mail or by email as the conversation is. **From** lists every active persona but the team leader persona: choosing another one writes as that persona in the same conversation. **Write as a persona** starts a new conversation with one or more teams, team members or team leaders (one message each; typing in To, Cc or Bcc suggests every active team, member and leader, by name or address). A team leader's (e.g. **Team A leader (Jane Doe)**) goes to the leader's own mailbox only, never the team's (by email, to the leader's email address). SIMEX Mail doesn't let a leader write to personas, so they can only read it there; by email, their answer comes into the Replies tab as *from the team leader*. A member's goes to their own mailbox only (**My mail**; with personal mailboxes off, to the team inbox, marked for them; by email, to their own email address), and the persona's answers to their reply go on there. Messages the desk writes that aren't answers are logged as kind *desk*: they have no inject, so they are kept out of the injects' delivery and reply figures, but the teams' replies to them are matched like any other. New messages need **live sending armed** (not for the Test team); nothing goes while sending is **paused**.
- **AI personas**: answering a conversation as an AI persona cancels that persona's queued answer to it (the AI personas queue shows *Answered by … on the persona desk*).
- **Who else is on it**: a conversation shows who else on the desk has it open, and who is writing an answer.
- **Teams & team leaders** (tab): every team's inbox, its leader's mailbox and members' own mailboxes, read-only, as **View as team**; nothing is marked read for them. The desk never sees injects that aren't sent, rubrics, grades or Claude's reading of the replies.
- Desk members turn on alerts like participants: each new message to a persona is pushed to their devices. Their guide is under **?** in SIMEX Mail.
- Reading on the desk is never counted as the team reading. **Clear mailboxes** (every mailbox) also clears the desk's copies of that team's mail.

## 6. After the exercise
In this order:
1. **Disarm** and wait for the last answers; **Download backup**.
2. **Finish grading** every team on every inject with a rubric (the Replies grid has no empty cells left for injects you mean to grade). Check *guess* and *unmatched* replies: a reply on the wrong inject counts as a miss in the reports.
3. **Compile the reports** (6.1). Read and edit each team's report; ask the team's leader to read it too (they know the team best). Then **Write it again** on the ExCon report so it reads your edited text.
4. **Print** each team's report to PDF and hand it to that team at the debrief; the ExCon report goes to ExCon and the exercise director.
5. **Export grades (CSV)** and a final backup, then disable the participant logins (People & logins) once the debrief no longer needs them.

### 6.1 End-of-exercise reports
**Dashboard → End-of-exercise reports** (Exercise or Test team). **Compile** makes a report for each team, written for the team to read at the debrief, and one for ExCon comparing the teams. Each compile is a new version; older ones stay listed. Finish grading first: the reports use the grades as they are when you compile.

- **Figures** (from the app's records, also with AI analysis off): injects received and opened; expected replies sent, **missed** and **late**; time to reply (median, 90th percentile, fastest, slowest, from delivery and from the team first opening it, first half of the exercise against the second); **critical points missed** (3.5); every inject with its reply and score; **what the team asked for**, to whom, and whether and how fast the persona answered; writing (clarity, completeness, actionability, words per message, tone); who they corresponded with; how many of the messages they received ExCon drafted with AI; and **how they managed the inbox**: who in the team read and wrote how much, with their team role (numbered, not named, in the team's copy; named on ExCon's) and, with team roles, the same grouped **by team role** (in the ExCon report across all teams too), injects answered before their documents were opened (in-app), mail read but never answered or answered long after, forwards, copies and mail to the team leader, how the inbox was left (unread, archived, starred, unsent drafts), labels, stars later removed, searches and time spent drafting. The ExCon report adds a table of the teams (and one of their inbox habits), how each inject went across teams, ExCon's own answers (how many, how fast, per persona), AI drafts made, used and edited, failed deliveries and bounces, AI cost, and where the AI-suggested scores and the grades are far apart.
- **The write-up** (Claude Sonnet, AI analysis on; a few minutes; roughly a dollar per team): for each team an overview, timeliness, what went well, what to improve, critical points missed with evidence, a comment on what they asked for, **writing style** with a strong and a weaker quote from their own messages and tips, **how they managed their inbox** (as observations, without singling anyone out; it counts the messages members sent each other from their own mailboxes, not what they said), recommendations and questions for the debrief. It reads only that team's mail: the injects it received, its messages, your answers and follow-ups, the reading of each message and your grades and grader's notes. It uses the human grades only, never the AI-suggested scores. ExCon's report reads the team reports (as you edited them) and a note on every message, and covers the comparison, themes, injects that worked and injects to revise for the next SIMEX, how ExCon ran it, and the AI suggestions against the grades.
- **Inbox habits are recorded from SIMEX Mail only**, from the moment this version was deployed: stars, archive, labels, searches, drafts and sends are logged with their time, besides reads and file downloads. Not ExCon's "view as team", nor a leader reading the team inbox. The participant guide tells participants it is recorded for the debrief. With the email channel only reply times, opens and clicks are known.
- **AI-written estimate:** left out of the team reports (and of what Claude reads for them) unless you tick **Include the AI-written estimate** when compiling. ExCon always sees it on the report page, marked *ExCon only*.
- **Check and edit:** open each report. Every section has **Edit** (Markdown: `**bold**`, `- ` list items, `> ` quotes) and **Reset to Claude's**; edited sections are marked. **Write it again** redoes one report from the same figures and replaces your edits to it (write ExCon's again after editing the team reports, so it reads your text). If a write-up fails, the figures are still there.
- **Print:** **Print** on a report, or on the compile **Print the team reports** / **Print all**, opens a printable page: use the browser's Print → Save as PDF. Each report starts on a new page. Give each team only its own report; the ExCon report is not for participants.

### 6.2 Clearing mailboxes and starting afresh
**One team, or all teams, after a test: Team inboxes → Clear mailboxes.** Pick the team (or **All teams**) and the mailboxes, type `CLEAR`:
- **Every mailbox** deletes everything in the team inbox, the team leader's mailbox and each member's own (received, sent, drafts, labels, and what was done in them), the team's replies on the Replies tab with their AI analysis, the persona answers to them, the team's grades and the persona desk's copies of its mail.
- **Members' own mailboxes only** empties just the members' My mail; the team inbox and its replies stay.
- Deliveries stay in the Log, and in the Dashboard's Delivered and Read, unless you tick **Also delete the deliveries to them**.

**Before going live: Settings → Start afresh.** Type `START AFRESH`. For every team it deletes every mailbox (team inboxes, leaders', members' own, the persona desk), replies, persona answers, grades, AI analyses, the exercise summary and reports, test runs, notifications and every delivery in the Log with its read and download figures: the Dashboard, Replies and Log start from zero. Injects, documents, rubrics, personas, people, logins (and login emails), settings and the audit trail stay, and live sending stays armed or disarmed as it was. With **Put injects already sent back on the schedule** (ticked), injects sent, failed or overdue during testing are scheduled again at their send time (those whose time has passed go overdue: send them by hand or change their time). Received email from before the reset is never synced back.

Neither can be undone: download a backup first.

---

## 7. Safety controls
- **Live sending switch** (Settings): disarmed = nothing reaches teams; arming needs `SEND LIVE`. A restored backup always comes back disarmed.
- **Pause all sending:** stops live and test deliveries immediately, even halfway through an inject's teams; resuming never delivers anything twice.
- **Overdue instead of late:** an inject more than the grace period past its time (after an outage, or arming late) is held as **overdue**. Send it by hand or reschedule it.
- **No duplicates:** every delivery is recorded before it happens; an in-app message is created in the same transaction that marks it delivered.
- **Confirmations:** typing the inject id for a manual live send or a deletion; `RESTORE` for a restore.
- **Separation:** participant logins can only see their own team's mailbox. They cannot open injects, schedules, rubrics, grades, other teams, or this manual.

---

## 8. Troubleshooting

| What you see | What to do |
|---|---|
| "Scheduler: … is the worker running?" | The scheduler worker is down. Check the *scheduler* component in DigitalOcean; meanwhile send urgent injects by hand (in-app sends deliver immediately). |
| "Too many attempts" at sign-in | 10 wrong passwords for one login lock that login for 10 minutes on that network; other logins (and ExCon) still work. Reset the password if they have lost it. |
| A participant can't sign in | People & logins → **reset password**, give them the new one (and its slip, with a new QR code: the old one stops working). "This sign-in code no longer works": the slip is older than 14 days or the password was reset; print a new **slip**. Check the login isn't disabled and the person has a team. |
| "You are signed out" in SIMEX Mail | Their session ended (password reset, signed out in another tab, or 14 days old). They sign in again in a new tab and come back: what they were writing is still there; press Send again. |
| A team says nothing arrived | Team inboxes → View as team. If it's there, they may be looking at another folder or another team's login. |
| Inject **overdue** | Send now, or move its time and Schedule. |
| An inject went out with a missing attachment, link or a mistake | Open it → **Send a follow-up** (5.3): add the file, explain, send it in the same conversation. |
| Inject **failed / partial** | Open it, read the error, fix it, "only retry the failed deliveries". One inject failing never holds up the others. |
| An email reply is missing or shows no text | The scheduler checks Resend every minute for received email the webhook missed (last 2 hours) and retries failed fetches. For older mail, Replies → **Sync email from Resend**. |
| A reply is "unmatched" or a "guess" | Open it and set the inject and team under **Match**. |
| Email channel: "Mailer: log" | `RESEND_API_KEY` is missing; email injects are only logged. In-app is unaffected. |
| Emails aren't arriving / bouncing | Log → the email → delivery status. If it can't be fixed quickly, switch to SIMEX Mail (5.6). |
| AI analysis says "No provider configured" or calls fail | The web and scheduler components need `ANTHROPIC_API_KEY`, or `AI_PROVIDER=bedrock` with `AWS_BEARER_TOKEN_BEDROCK` (and `AWS_REGION`). The last error is on Settings → AI analysis. Grading by hand is unaffected. |
| AI analysis stopped for the day | The daily cap was reached: raise it on Settings → AI analysis. |
| "… has exercise mail or grades: make it inactive" when deleting a team | Deleting would take its inbox, replies and grades with it. Untick **active** on People & teams instead (it stops getting injects). |
| Need to undo a big mistake | Settings → Restore a backup (type RESTORE). A backup of this app replaces everything, including logins. A backup from the first (email-only) version of the app is merged in instead: settings, senders, roster, injects with attachments, rubrics and grades. |

## 9. Quick reference: a day of the exercise
1. Status bar OK (scheduler recent).
2. Today's injects scheduled, times right, no warnings.
3. Teams signed in. Arm live sending.
4. Watch **Replies**, **Dashboard** and **Team inboxes**; answer in character; grade as answers arrive.
5. Handle overdue or failed injects; pause if anything looks wrong.
6. End of day: disarm, download a backup, grade the day's replies.

**End of the exercise:** disarm, backup, finish grading, compile the reports, edit them (with each team's leader), rewrite the ExCon report, print, debrief (section 6).
