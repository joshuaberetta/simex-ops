# SIMEX ops: operator manual

For ExCon members who deliver the SIMEX injects, watch the team mailboxes, answer in character and grade the responses. The app is at **https://ops.simex.kobolabs.dev**; ExCon signs in there as `undac` (or their own staff login). Participants use SIMEX Mail at **https://mail.simex.kobolabs.dev** (a webmail-style sign-in page, then their team's mailbox) with the team logins you hand out. The two addresses share one sign-in: a participant who signs in at ops. is sent on to mail., and ExCon pages opened on mail. go back to ops. All times are **Nairobi time**.

Read section 7, "Safety controls", before your first shift.

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

| Tab | What it is for |
|---|---|
| **Injects** | The schedule. Filter by day or status; open an inject to edit, preview, test or send it; change the channel in bulk. Import from the MEL is here too. |
| **Replies** | Everything participants sent: team summary, inject × team grid, **Read / opens & clicks**, the inbox, and grading. |
| **Dashboard** | The exercise at a glance: per team, replies, response times, scores, sentiment over time and a funnel; the rubric heatmap; flags, possibly AI-written replies, what teams ask for and commit to; an AI-written exercise summary (5.7). |
| **Team inboxes** | Each team's mailbox at a glance (received, unread, sent, last sign-in), **View as team**, and **Clear a team's mailbox** after testing (section 6). |
| **Test runs** | Replays injects on a fast clock into the **Test team** mailbox (and, for email injects, to a test address). |
| **Senders** | The personas: username (`cnn`), display name ("CNN Breaking News"). They are also the participants' address book. |
| **People & logins** | Teams, participants, and their logins (create, reset password, disable, printable credential sheet). |
| **Log** | Every delivery the app made, with content and status, plus the activity log of every action. |
| **Settings** | **Delivery channel**, footer, subject prefix and inject code in the subject, **AI analysis**, team-to-team messaging, attachment limit, email options, grace period, **live sending switch**, backup and restore. |
| **Help** | This manual. |

The status bar under the tabs shows the Nairobi clock, **Live disarmed** (green) or **LIVE ARMED** (red), **ALL SENDING PAUSED** when paused, when the scheduler last ran (red if it stopped), the next inject due, counts of overdue and failed injects, and the **Pause all sending** button.

---

## 3. Before the exercise (the day before at the latest)

### 3.1 Load the injects
1. In the knowledge base: `python3 -I scripts/export_inject_bundle.py`. It writes `work/simex-ops/injects.json` and `attachments.zip`.
2. **Injects → Import / update**, upload `injects.json` (or `MEL-MIL.xlsx`) and click **Check file**.
3. Read the summary (new / updated / with local edits / unchanged), then **Apply import**. New injects are **in-app**.
4. On the same page, upload `attachments.zip`. Files are matched to injects by the names in the MEL's *Supporting documents* column.
5. Open a few injects: documents the MEL lists but that are still missing are shown in orange under **Attachments**. Upload those by hand (max 30 MB each; put a link in the body for the 1-20 briefing video).

Only participant-facing fields are imported. Expected outcomes, story, theme, key figures and the ExCon description never enter the app. Re-importing updates what changed in the MEL, keeps your edits (unless you tick "Overwrite my local edits") and never touches what was already delivered.

### 3.2 Teams, participants and logins
**People & logins.** A new app has placeholder teams A–D (with placeholder people, no logins) and the **Test team**.

1. Paste the roster into **Paste a roster**, one line per person: `name,email,team code,role` (email optional for in-app; role `tl` for team leaders). Keep **create logins** and **remove the placeholder people** ticked.
   ```
   Jane Doe,jane@example.org,a,tl
   John Roe,,a
   ```
2. The page then shows **New logins: shown once**: username and password for each new person. Click **Printable sheet** and print it (one slip per person, with the SIMEX Mail address https://mail.simex.kobolabs.dev), or copy it somewhere safe. Passwords are not stored readable; if one is lost, use **reset password** on that person.
3. Each team shares **one mailbox**: everyone in Team A sees Team A's mail, and what one member sends appears in everyone's *Sent*, with the sender's name.
4. Give yourself a login in the **Test team** (Add person → team "Test team" → create login) to try SIMEX Mail as a participant.

- **Team code** fills `{team}` in addresses (`undac-{team}-tl` → `undac-a-tl@…`); **team name** fills `{team_name}`.
- **Disable** blocks a login without deleting the person; **delete** removes both.
- More ExCon logins: Django admin (`/admin/`, staff users or the `excon` group).

### 3.3 Personas (Senders)
One row per persona, created from the MEL's sender aliases. Fix display names (e.g. `CCDPR` → "CCDPR Deputy Director"); **rename in injects** keeps injects in step when a username changes. Add personas the MEL lacks (e.g. `cnn` / "CNN Breaking News").

Active senders are the participants' **address book**: they can write to any of them. Untick **active** to hide one.

### 3.4 Settings
- **Default footer:** "EXERCISE EXERCISE EXERCISE …" at the bottom of every message.
- **Subject prefix** (e.g. `[SIMEX] `) and **put the inject code in the subject**: every inject's subject then starts with its code, e.g. `[1-15] Request for assessment`, so teams and ExCon can refer to it. Off by default; it applies to injects delivered after you turn it on (both channels).
- **AI analysis:** off by default; see 5.7.
- **Teams may write to other teams:** off by default (teams write only to personas). Turn it on if the scenario wants OSOCC-to-OSOCC traffic; those messages show under Team inboxes but are not "replies".
- **Max attachments per message:** what participants may attach (default 10 MB).
- **Email channel:** reply-to (leave empty), ExCon Bcc, test addresses. Only matters for injects on the email channel.
- **Grace period:** 10 minutes by default (see 7.3).

### 3.5 Go through the injects
Filter **Injects** by day and open each one:
- **Send at:** the real (Nairobi) time; the MEL's scenario time is separate.
- **Channel:** set for all injects by Settings → Delivery channel; the inject page shows which one applies. Only in *Per inject* mode can you choose here.
- **Sender username / name**, **Recipients** (*one per team*, *one per participant* — lands in that person's team mailbox addressed to them —, *everyone*), teams to leave out.
- **Subject and body:** plain text; `{team}`, `{team_name}`, `{name}`, `{first_name}` are filled per message.
- **Preview** (right): exactly what each team gets. **Clear every orange warning.**
- **Replies & rubric:** tick "teams are expected to reply", set "within (minutes)", add criteria with max points (or copy another inject's rubric).

Bulk: tick injects in the list and use **Schedule / Back to draft / Cancel / Shift by N min / Channel: In-app | Email** (the Channel buttons appear only in *Per inject* mode; otherwise Settings decides for all injects).

### 3.6 Prepare the backup channel
Even if you plan to use email, make SIMEX Mail ready so you can switch in a minute:
1. **People & logins:** check every participant is in the right team and create their logins (one by one or by pasting the roster). Passwords are shown once: print the **credentials sheet** and keep it with ExCon.
2. Sign in once as a test participant (or use **Team inboxes → View as team**) to see what teams would see.
3. Note the address to give teams: **https://mail.simex.kobolabs.dev** (they land in their mailbox after signing in).
4. Tell teams with iPhones to add SIMEX Mail to their home screen (Safari → **Share** → **Add to Home Screen**), open it from there and tap **Turn on alerts**: only then do they get notifications with SIMEX Mail closed. SIMEX Mail shows iPhone users this hint itself. Other phones and computers get them once alerts are on.

### 3.7 Rehearse (at least once) — section 4.

### 3.8 Take a backup
**Settings → Download backup.** One file with everything: injects, attachments, mailboxes, logins, grades.

---

## 4. Testing without reaching participants

### 4.1 Send one test
On an inject, **Test → Send test**. In-app injects land in the **Test team** mailbox (subject `[TEST]`, a banner says which team it was really for); email injects go to the address you type. The inject's status doesn't change.

### 4.2 Test run (a rehearsal on a fast clock)
**Test runs → New test run**: choose the injects (all, a day, or ids), the start, the timing ("one every 30 s" or "real gaps, 60× faster"), and for email injects a test address pattern (`you+{key}@gmail.com`). **Dry run** builds and logs only. The run page refreshes every 10 s and has **Pause / Resume / Next one now / Cancel**, and a link to the **Test team inbox**.

To rehearse the whole loop, sign in as your Test team login (another browser or a private window), open the test messages, reply, and watch them appear under **Replies → Test team**.

### 4.3 View as team
**Team inboxes → View as team** opens SIMEX Mail exactly as that team sees it, **read-only**: nothing is marked read, nothing can be sent, and your session stays ExCon. Use it to check what a team has received, or to look over their shoulder during the debrief. You also see the labels the team has made to organise its mail (Gmail-style folders); they're the team's own and change nothing on the ExCon side.

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
- **Ad-hoc inject:** **Injects → New inject**.
- **Stop everything:** **Pause all sending**; **Resume sending** continues without duplicates.

### 5.4 Replies and grading
- **Replies tab** (refreshes every minute): the **Teams** table, the **Injects × teams** grid (green replied, orange late, red overdue with no reply, blue waiting), **Read / opens & clicks**, and the **Inbox** of everything teams sent to personas.
  - In-app replies are matched **exactly** to the message they answer. A *new* message a team writes to a persona is matched to that persona's latest message to the team and marked **guess**; check it. **unmatched** needs assigning.
- **Inbox → Conversations** (the default) shows one row per back-and-forth: an in-app conversation, or for email the same team, persona and subject. It gives the latest message, how many the team sent and how many were answered, and **awaiting answer** when the team wrote last and no persona has answered since. **Messages** lists every message on its own, as before.
- **A reply's page** shows the whole conversation in order: the inject (or message) that started it, each message from the team and each persona answer. The highlighted one is the message the page is about; click another to open it. **Answer as <persona>** always answers the latest message, so the conversation stays in one thread.
- On that page you can also read it, download its files, **view in the team's mailbox**, correct the **Match**, set **Status** (ignored replies don't count), and **Answer as <persona>**: the answer lands in the team's mailbox in the same conversation (or goes by email for email threads), and their next reply is tracked too.
- **Grading:** from a grid cell or **Grade …**: all of the team's replies to that inject together; a score per criterion, comment, name; **Save and next team**. Non-responses can be graded too. **Export grades (CSV)** for the debrief.

Write rubrics as observable behaviour ("acknowledged within 30 min", "named the cluster lead"), 2–4 points each. Nothing is copied from the MEL's expected outcomes.

**Alerts without refreshing.** Every ExCon page checks for news every 10 seconds. A new reply, an AI flag (*needs ExCon attention*, *safety*), or an inject that fails or goes overdue shows a toast at the bottom right (click it to open the reply), plays a chime, updates the **Replies** badge and the tab title and icon, and shows a desktop notification while the tab is in the background. **🔔 Alerts** in the status bar turns this on or off; click it once on each computer to allow desktop notifications. Open tabs share what has been seen, so an alert comes once however many ExCon pages are open. Participants get the same kind of alerts in SIMEX Mail (a chime, a blue bar, the tab badge and a desktop notification once they click **Turn on alerts**). Their notifications are also *pushed*: a device that turned on alerts is told about each new message even with SIMEX Mail closed (on an iPhone, only from the home-screen app). They show the sender and subject, nothing more. Pushes need the `VAPID_PRIVATE_KEY` secret; without it, alerts only work while SIMEX Mail is open.

### 5.5 End of each day
**Settings → Disarm** (unless injects run overnight), **Download backup**, deal with overdue or failed injects.

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
- **"AI-written"** is a hint to look closer, never proof: it is judged from the style of the text and how fast it was written after the team opened the inject. Short messages and non-native English make it unreliable. Don't confront a participant on this alone.
- **Dashboard tab:** a tile per team (expected injects answered, median first reply, graded score, AI-suggested score, sentiment of each reply over the exercise, tone mix, a delivered → read → answered → graded funnel), the **rubric results** heatmap (switch between *Graded* and *AI suggested*), **where the AI and the graders disagree** (a quarter of the points or more: check the rubric or the reply), **Flags**, **Possibly AI-written** (60 % and above), **What teams are asking for** and **Decisions and commitments**. **Write the summary** produces a situation summary across teams (themes, each team's strengths and concerns, what ExCon may want to act on now, points for the hotwash) in about a minute.
- **Settings → AI analysis:** on/off; *analyse every reply as it arrives* (off: only when you click **Analyse now**); include Test team replies (useful in rehearsals); a **daily spending cap** in USD (default 5; a full exercise day costs well under a dollar). The card shows today's calls, spend and the last error.
- What Claude sees: the inject as the team received it, the rubric you wrote, and the teams' replies. Nothing else is stored in the app to send.

## 6. After the exercise
Export grades and a final backup, disarm, and disable the participant logins (People & logins) once the debrief no longer needs them.

**Clearing a team's mailbox after testing:** **Team inboxes → Clear a team's mailbox**, pick the team, type `CLEAR`. It deletes everything in that team's mailbox (received, sent, drafts, and the team's labels), its replies on the Replies tab with their AI analysis, the persona answers to them and the team's grades. Deliveries stay in the Log. It can't be undone, so download a backup first.

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
| A participant can't sign in | People & logins → **reset password**, give them the new one. Check the login isn't disabled and the person has a team. |
| A team says nothing arrived | Team inboxes → View as team. If it's there, they may be looking at another folder or another team's login. |
| Inject **overdue** | Send now, or move its time and Schedule. |
| Inject **failed / partial** | Open it, read the error, fix it, "only retry the failed deliveries". |
| A reply is "unmatched" or a "guess" | Open it and set the inject and team under **Match**. |
| Email channel: "Mailer: log" | `RESEND_API_KEY` is missing; email injects are only logged. In-app is unaffected. |
| Emails aren't arriving / bouncing | Log → the email → delivery status. If it can't be fixed quickly, switch to SIMEX Mail (5.6). |
| AI analysis says "No provider configured" or calls fail | The web and scheduler components need `ANTHROPIC_API_KEY`, or `AI_PROVIDER=bedrock` with `AWS_BEARER_TOKEN_BEDROCK` (and `AWS_REGION`). The last error is on Settings → AI analysis. Grading by hand is unaffected. |
| AI analysis stopped for the day | The daily cap was reached: raise it on Settings → AI analysis. |
| Need to undo a big mistake | Settings → Restore a backup (type RESTORE). A backup of this app replaces everything, including logins. A backup from the first (email-only) version of the app is merged in instead: settings, senders, roster, injects with attachments, rubrics and grades. |

## 9. Quick reference: a day of the exercise
1. Status bar OK (scheduler recent).
2. Today's injects scheduled, times right, no warnings.
3. Teams signed in. Arm live sending.
4. Watch **Replies**, **Dashboard** and **Team inboxes**; answer in character; grade as answers arrive.
5. Handle overdue or failed injects; pause if anything looks wrong.
6. End of day: disarm, download a backup.
