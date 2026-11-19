# SIMEX ops: operator manual for the email injects

For ExCon members who schedule and send the SIMEX email injects, follow the replies and grade them. The app is at **https://ops.simex.kobolabs.dev** (login `undac`). All times in the app are **Nairobi time**.

Read section 7, "Safety controls", before your first shift.

---

## 1. How it works in one minute

- **Injects.** Each email inject from the MEL is one row on the **Injects** tab, with a send time, a sender (a persona such as `ccdpr@simex.kobolabs.dev`), recipients, a subject, a body and attachments.
- **Sending.** When an inject's time comes, the app sends it through Resend, but only if **live sending is armed** and nothing is paused. One inject usually becomes several emails, one per team.
- **Replies.** When participants reply, the reply comes back to the persona and appears on the **Replies** tab, already matched to the inject and the team. There you read it, answer in character if needed, and grade it against the inject's rubric.
- **Nothing reaches participants by accident:**
  - live sending is off until someone arms it;
  - **Pause all sending** stops everything at once;
  - an inject that missed its time is held as **overdue**, never sent late on its own.

## 2. The tabs

| Tab | What it is for |
|---|---|
| **Injects** | The schedule. Filter by day or status, open an inject to edit it, preview it, test it or send it. Import from the MEL is here too. |
| **Replies** | Everything participants sent back: team summary, inject × team grid, opens and clicks, the inbox, and grading. |
| **Test runs** | Replays injects on a fast clock to test inboxes, so you can rehearse without emailing participants. |
| **Senders** | The personas: username (`cnn`), display name ("CNN Breaking News"), optional reply-to. |
| **People & teams** | The roster: teams (code, name, optional shared inbox) and participants (name, email, team, role). |
| **Log** | Every email the app produced, with its content and delivery status, plus an activity log of every action. |
| **Settings** | Footer, subject prefix, reply-to, ExCon Bcc, test addresses, grace period, **live sending switch**, backup and restore, Resend domain check. |

The bar under the tabs is always visible:
- the Nairobi clock;
- **Live disarmed** (green) or **LIVE ARMED** (red), and **ALL SENDING PAUSED** when paused;
- the mailer (if it says "log", nothing is really sent);
- when the scheduler last ran;
- the next inject due;
- counts of overdue and failed injects;
- the **Pause all sending** button.

---

## 3. Before the exercise (the day before at the latest)

### 3.1 Load the injects
1. In the knowledge base: `python3 -I scripts/export_inject_bundle.py`. It writes `work/simex-ops/injects.json` and `attachments.zip`.
2. **Injects → Import / update**, then upload `injects.json` (or `MEL-MIL.xlsx` directly) and click **Check file**.
3. Read the summary (new / updated / with local edits / unchanged), then **Apply import**.
4. On the same page, upload `attachments.zip`. The app matches the files to injects by the names in the MEL's *Supporting documents* column and tells you which files matched nothing.
5. Open a few injects. Under **Attachments**, any document the MEL lists that is still missing is shown in orange. Upload those by hand. The UNDSS briefing video for 1-20 is too large to attach (357 MB), so put a link in the body instead.

Only participant-facing fields are imported. Expected outcomes, story, theme, key figures and the ExCon description never enter the app.

**If the MEL changes later**, import again. The app updates what changed in the MEL, keeps the edits you made in the app (unless you tick "Overwrite my local edits"), and never touches injects already sent.

### 3.2 Enter the roster
**People & teams.** A new app has placeholder teams A–D and placeholder people whose addresses end in `placeholder.invalid`. Live sends skip those addresses.

The fastest way is to paste the roster into **Paste a roster** as CSV lines, `name,email,team code,role` (role `tl` for team leaders, otherwise empty), and keep "remove the placeholder people" ticked:

```
Jane Doe,jane@example.org,a,tl
John Roe,john@example.org,a
```

- **Team code** is what fills `{team}` in addresses (e.g. `undac-{team}-tl` becomes `undac-a-tl@…`). **Team name** fills `{team_name}`.
- If a team uses one shared mailbox, put it in the team's **Shared inbox**. "One email per team" injects then go there instead of to each member.

### 3.3 Check the personas
**Senders.** One row per persona, created from the MEL's sender aliases. Fix display names, e.g. `CCDPR` to "CCDPR Deputy Director". With **rename in injects** ticked, changing a username also updates every inject that uses it.

- **Leave reply-to empty.** Replies then come back to the persona and are tracked on the Replies tab. Anything typed in a reply-to (here, on an inject, or in Settings) sends replies elsewhere, and they are **not** tracked.
- You can add personas the MEL doesn't have (e.g. `cnn` / "CNN Breaking News").

### 3.4 Settings
- **Default footer:** "EXERCISE EXERCISE EXERCISE …" is added to every email. Keep it unless the course team says otherwise.
- **Subject prefix:** optional, e.g. `[SIMEX] `.
- **Default reply-to:** leave empty (see 3.3).
- **ExCon Bcc:** an ExCon address that gets a copy of every email. Useful as a paper trail.
- **Grace period:** 10 minutes by default (see 7.3).

### 3.5 Go through the injects
For each day, filter **Injects** by day and open each inject:
- **Send at:** the real (Nairobi) time it goes out. The MEL's scenario time is shown separately.
- **Sender username / name / reply-to:** blank fields fall back to the Senders list.
- **Recipients:**
  - *One email per team*: To all members of each team, so teams get separate copies.
  - *One email per participant*: personal emails, e.g. attestations.
  - *One email, all participants in Bcc*: one email to everyone, with the persona as the visible To.
  - *Only the addresses below*.

  Untick teams to leave them out, and add extra To/Cc/Bcc if needed.
- **Subject and body:** plain text. `{team}`, `{team_name}`, `{name}` and `{first_name}` are filled in for each email.
- **Footer:** default, custom or none.
- **Preview** (right-hand side): exactly what each team will get, with warnings in orange such as placeholder addresses, an empty subject or oversized attachments. **Clear every warning before the exercise.**
- **Replies & rubric:** tick "teams are expected to reply", set "within (minutes)", and add rubric criteria with max points (see 5.4). You can also copy another inject's rubric.

To change many injects at once, tick them in the list and use **Schedule / Back to draft / Cancel / Shift by N min**.

### 3.6 Rehearse (do this at least once)
See section 4. A full rehearsal on a fast clock takes 20–30 minutes.

### 3.7 Take a backup
**Settings → Download backup.** It is one file with everything, attachments included. Keep it somewhere safe.

---

## 4. Testing without emailing participants

### 4.1 Send one test
On an inject, under **Test**, enter your own address and click **Send test**.
- You receive the email exactly as a team would. The subject starts with `[TEST]`, and a banner at the top lists who would really receive it.
- Tick "one email per recipient group" to get every team's version.
- This does not change the inject's status.

### 4.2 Test run (a rehearsal on a fast clock)
**Test runs → New test run.**
1. **Injects:** all with a send time, one day, or a list of ids (`1-03, 1-11, 3-30`).
2. **Start:** defaults to two minutes from now.
3. **Timing:** "one inject every 30 s", or "keep the real gaps, 60× faster" (one hour becomes one minute).
4. **Send to:** a pattern for where the emails go instead of the participants. `{key}` is replaced with the original recipient's username.
   - `you+{key}@gmail.com`: one tagged copy per original recipient in your Gmail.
   - `delivered+{key}@resend.dev`: Resend's test inbox (accepted, never delivered). Use it to test the flow and volume without filling a mailbox.
5. **Dry run:** nothing goes to Resend; emails are only built and logged.
6. Click **Create and start**.

The run page refreshes every 10 seconds and shows each item's due time, status and errors. It has **Pause**, **Resume**, **Next one now** (pulls everything forward) and **Cancel run**. Test runs don't need live sending armed, but **Pause all sending** pauses them too.

**Testing replies:** reply to one of the test emails from your own inbox. The reply appears under **Replies → Replies to test emails**, matched to the inject and team.

---

## 5. During the exercise

### 5.1 Start of each day
1. Check the status bar: the scheduler ran a few seconds ago, and the mailer says "resend", not "log".
2. Check **Injects** filtered to today: everything that should go out is **scheduled** (blue) and the times are right. If the exercise runs late, select the remaining injects and **Shift by** N minutes.
3. **Settings → Live sending:** type `SEND LIVE` and click **Arm live sending**. The bar turns red: LIVE ARMED.

From then on, the app sends each scheduled inject at its time. Nobody has to press anything.

### 5.2 Watching the sends
- An inject goes **scheduled → sending → sent**. **Partial** means some of its emails failed; **failed** means none went out.
- Open the inject to see each email's status, delivery ("delivered", or "bounced" in red) and whether it was opened.
- **Log** shows every email, newest first. Click one to see exactly what was sent.
- **To fix a failure:** open the inject, read the error at the top, fix it (usually an address), tick "only retry the failed emails", type the inject id and click **Send to participants**.

### 5.3 Sending by hand, holding, changing
- **Send now** (early, or for an overdue inject): on the inject, under **Send live now**, type the inject id and click **Send to participants**. Live sending must be armed.
- **Hold one inject:** **Back to draft**, or **Cancel inject**.
- **Edit before it goes:** change it and click **Save**. Whatever is saved at the send time is what goes out.
- **Ad-hoc inject:** **Injects → New inject**, fill it in, then **Schedule** or send now.
- **Stop everything:** **Pause all sending** (top right). Emails already handed to Resend still arrive; the rest wait. **Resume sending** continues them.

### 5.4 Replies and grading
- **Replies tab** (refreshes every minute):
  - **Teams table:** replies, expected injects answered, late or missing, median time to first reply, score, and emails opened and clicked.
  - **Injects × teams grid:** green = replied, orange = replied late, red = no reply and overdue, blue = waiting. Click a cell to grade.
  - **Opens & clicks:** which team opened which inject and when. Opens are a hint, not proof (see 8).
  - **Inbox:** every reply. Bold rows are new. "guess" means the reply was matched by sender and subject rather than by reply-to, so check it. "unmatched" means you need to assign it.
- **A reply's page:**
  - read it (quoted text is folded) and download its files;
  - **Match:** set or correct the inject, team and person;
  - **Status:** new / reviewed / ignored. Ignored replies don't count;
  - **Answer as <persona>:** reply in character. It goes to the participant from the persona, in the same thread, and their next reply is tracked too.
- **Grading** (from a grid cell or **Grade …** on a reply):
  - all of that team's replies to that inject are shown together;
  - pick a score per criterion, add a comment and your name, and click **Save**, or **Save and next team** to move through the teams;
  - you can grade a non-response too.
- **Export grades (CSV)**, on the Replies tab, for the debrief.

Write rubrics as observable behaviour ("acknowledged within 30 min", "named the cluster lead", "asked for the missing figure"), with 2–4 points each. Rubrics are typed in the app by ExCon; nothing is copied from the MEL's expected outcomes.

### 5.5 End of each day
1. **Settings → Disarm**, unless injects run overnight.
2. **Settings → Download backup.**
3. Check for **overdue** or **failed** injects in the status bar and deal with them.

---

## 6. After the exercise
- **Export grades (CSV)** and download a final backup.
- Disarm live sending.
- The Log and Replies tabs hold the full record of what was sent, opened, clicked and answered, and when.

---

## 7. Safety controls

### 7.1 Live sending switch
**Settings → Live sending.** Disarmed means nothing goes to participants: scheduled injects wait, and Send now is greyed out. Arming needs `SEND LIVE` typed in. A restored backup always comes back disarmed.

### 7.2 Pause all sending
The button at the top right of every page. It stops live sends and test runs immediately, including an inject halfway through its teams. Resume continues where it stopped, without sending anything twice.

### 7.3 Overdue instead of late
If a scheduled inject is more than the **grace period** (Settings, default 10 min) past its time when the app looks at it, for example after an outage, or because you armed late, it is marked **overdue** and **not sent**. That stops a backlog of stale injects going out all at once. To send it anyway, open it and use **Send live now**. To reschedule, change the time and click **Schedule**.

### 7.4 No duplicates
Every email is recorded before it goes to Resend, with a unique key. A crash or redeploy mid-send resumes without sending anything twice.

### 7.5 Confirmations
A live send by hand needs the inject id typed in, and so does deleting an inject.

---

## 8. Troubleshooting

| What you see | What to do |
|---|---|
| Status bar says "Mailer: log" | The Resend key is missing on the server, so nothing is sent. Add `RESEND_API_KEY` in the DigitalOcean app settings. |
| "Scheduler: N min ago" keeps growing | The app is stuck or restarting. Check the app in DigitalOcean; meanwhile send urgent injects by hand once it's back. |
| Inject **overdue** | It missed its time by more than the grace period. Send now, or move its time and Schedule. |
| Inject **failed / partial** | Open it, read the error, fix it, then "only retry the failed emails". |
| Preview warns "placeholder address" | The roster still has placeholder people. Replace them on People & teams. |
| A team says they didn't get it | Log → the email → check To and the delivery status. "bounced" means a bad address. "delivered" means check their spam folder. |
| Replies don't appear | Click **Sync from Resend** on the Replies tab. If still nothing, check the reply-to is empty (3.3) and the Resend webhook is enabled. |
| A reply is "unmatched" or a "guess" | Open it and set the inject and team under **Match**. |
| Opens look wrong | Open tracking is a signal only: image blocking hides opens, and Apple Mail can register opens nobody made. Clicks and replies are reliable. |
| Need to undo a big mistake | Settings → Restore a backup (type RESTORE). It replaces injects, senders, people and settings, and clears the logs. |

## 9. Quick reference: a day of the exercise
1. Status bar OK (scheduler recent, mailer resend).
2. Today's injects scheduled, times right, no warnings.
3. Arm live sending.
4. Watch **Replies**; answer in character; grade as replies come in.
5. Handle any overdue or failed inject from the status bar.
6. Pause if anything looks wrong.
7. End of day: disarm, download a backup.
