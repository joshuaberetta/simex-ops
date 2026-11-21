# SIMEX ops

Web app for UNDAC SIMEX exercise control at **https://ops.simex.kobolabs.dev**: schedules and delivers the injects, collects the teams' replies, and grades them.

**Two delivery channels, one switch** (Settings → Delivery channel):
- **Email (Resend)**, the main flow. Personas send from and receive at `@simex.kobolabs.dev`.
- **SIMEX Mail**, the backup if email has problems: in-app team mailboxes with a webmail-style client and participant logins. No mail server is involved.

Each team has **team members** (the participants, sharing one team mailbox) and **one team leader** from ExCon, who plays the team leader persona in SIMEX Mail: their own mailbox (what is sent as that persona is in their Sent, the team's mail to it in their Inbox, plus injects marked "also to team leaders"), a read-only view of the team's inbox with Claude's reading of each reply, and the team's Dashboard. Teams never see or write to each other.

| Where | What |
|---|---|
| `web/` | **The app in production** (Django): both channels, participant logins, "view as team", scheduler worker. Read `web/README.md`; the operator manual is `web/docs/MANUAL.md` (in the app under Help), the participant guide `web/docs/PARTICIPANT_GUIDE.md`, the team leaders' `web/docs/LEADER_GUIDE.md`. Spec: `.do/app.yaml` (web + scheduler worker). |
| `app/` | The first version (FastAPI, email only), kept for rollback. Spec: `.do/app-fastapi.yaml`; its manual is `docs/MANUAL.md`. Its backup file can be loaded into the Django app (Settings → Restore merges it in). |

Exercise content never goes into this repo; it reaches the app only through the import page.

---

The rest of this file describes the first (FastAPI) version in `app/`.

**Operator manual:** [docs/MANUAL.md](docs/MANUAL.md), also in the app under **Help**.

## What it does

- **Injects**: imported from `MEL-MIL.xlsx` or an `injects.json` export, then editable. Each inject
  has a send time (Africa/Nairobi), a sender (`<username>@simex.kobolabs.dev` and a display name), a
  reply-to, recipients, a subject, a body, a footer and attachments. Statuses:
  draft → scheduled → sending → sent / partial / failed, plus overdue and cancelled.
- **Recipients**: one email per team (To: its members, or the team's shared inbox), one per
  participant, one with everyone in Bcc (To: the reply-to address), or custom addresses only. You
  can limit any of these to some teams and add extra To/Cc/Bcc. `{team}`, `{team_name}`, `{name}`
  and `{first_name}` are filled in per email, in the sender username, the sender name, the subject
  and the body (e.g. `undac-{team}-tl` → `undac-a-tl@simex.kobolabs.dev`).
- **Senders**: a list of usernames and display names (seeded from the MEL's sender aliases,
  `ccdpr@epeigonia.org` → `ccdpr`), each with an optional reply-to. Add any, e.g.
  `cnn` / "CNN Breaking News".
- **People & teams**: the roster. A fresh database has placeholder teams A–D with
  `…@placeholder.invalid` people. Live sends skip those addresses. Paste the real roster as CSV
  (`name,email,team code,role`).
- **Only participant-facing fields are imported.** Expected outcomes, story, theme, key figures,
  relevance and the ExCon description are dropped at import and never stored.

## Replies and grading

- **Where replies go.** Each email's Reply-To is the persona's address tagged with a reference,
  `cnn+3f9a1c2e@simex.kobolabs.dev`. Resend receives it and calls the webhook
  `/webhooks/resend` (`email.received`, signature checked with `RESEND_WEBHOOK_SECRET`), and the
  app fetches the body and attachments.
- **How they are matched.** The tag identifies the inject and team. A fresh email typed to a
  persona (no tag) is matched by the sender's roster entry plus the persona's latest email to
  that team, preferring the same subject, and is marked "guess". Anything unmatched can be
  assigned by hand. Mail from our own domain is ignored (the To: of all-Bcc emails). Replies to
  test emails are kept separate.
- **Replies tab.** A per-team summary (replies, expected injects answered, late or missing,
  median time to first reply, score), an inject × team matrix (replied / late / waiting / no
  reply), and an inbox. "Sync from Resend" catches anything a webhook missed.
- **Answering in character.** From a reply's page, ExCon can answer as the persona. It is
  threaded with In-Reply-To, and its own replies are tracked too.
- **Rubrics.** Written per inject by ExCon, on the inject page under "Replies & rubric": mark
  "teams are expected to reply", give a time limit, and add criteria with max points (or copy
  another inject's rubric). Nothing comes from the MEL's expected outcomes.
- **Grading.** Per team per inject, scoring all the team's replies together. "Save and next team"
  moves through the teams. Export everything as CSV.
- Delivery events (`email.delivered`, `email.bounced`, ...) on the same webhook update each sent
  email's delivery status. Bounces are logged.
- Setting an explicit reply-to (Settings, sender or inject) diverts replies away from the app,
  and they are then not tracked.

## Safety

- **Live sending is disarmed by default.** Arm it in Settings by typing `SEND LIVE`. While
  disarmed, nothing goes to participants. Send test and test runs still work.
- **Pause all sending** (top right, every page) stops live and test sends at once. Emails not yet
  handed to Resend stay pending and go out on resume.
- **Grace period** (default 10 min): a scheduled inject found later than this is marked
  **overdue** and not sent. This covers an outage, a redeploy, or arming late. Use Send now, or
  change its time and schedule it again.
- **Every email is recorded before it is sent** and carries a Resend idempotency key, so a crash
  or redeploy mid-send resumes without duplicates.
- **A manual live send needs the inject id typed in**, and so does deleting an inject.
- **The default footer says "EXERCISE EXERCISE EXERCISE"** (edit it in Settings, or per inject).

## Testing without emailing participants

1. **Preview**: the inject page shows every email it will produce, with recipients, the filled-in
   placeholders, warnings and the rendered body.
2. **Send test**: sends the inject to one address, with `[TEST]` in the subject and a banner
   listing the real recipients.
3. **Test runs** replay injects (all of them, one day, or a list of ids) through the real
   scheduler and Resend on a compressed clock. Choose one inject every N seconds, or the real gaps
   N× faster. Every recipient is rewritten to a pattern: `you+{key}@gmail.com` gives one tagged
   copy per original recipient, and `delivered+{key}@resend.dev` is Resend's test inbox (accepted,
   never delivered). **Dry run** renders and logs only. Pause, resume, "next one now" and cancel
   are on the run page.

## Getting the injects in

From the knowledge base (it strips control-side fields locally and collects the documents):

```
cd ~/code/undac-simex
python3 -I scripts/export_inject_bundle.py     # -> work/simex-ops/injects.json + attachments.zip
```

1. In the app, go to Injects → Import / update and upload `injects.json` (or `MEL-MIL.xlsx`).
2. Check the list of changes, then apply.
3. Upload `attachments.zip`. Files are matched to injects by the names in the MEL's
   "Supporting documents" column.

**Re-importing** updates fields that changed in the MEL. It keeps fields you edited in the app,
unless you tick "overwrite", and never touches injects already sent. Over 30 MB is too big for one
file: the 357 MB UNDSS briefing (1-20) is left out, so send a link instead.

## Local development

```
uv venv -p 3.12 .venv && uv pip install -p .venv -r requirements.txt pytest
.venv/bin/uvicorn app.main:app --reload --port 8000     # SQLite file, MAILER=log (nothing sent), login undac / nairobi2026
.venv/bin/python -m pytest -q
```

Environment: `DATABASE_URL` (default SQLite), `APP_USERNAME`, `APP_PASSWORD`, `SECRET_KEY`,
`RESEND_API_KEY` (absent: log-only mailer), `MAIL_DOMAIN` (default `simex.kobolabs.dev`),
`EXERCISE_TZ` (default `Africa/Nairobi`), `PUBLIC_URL`, `TICK_SECONDS` (15), `RESEND_MIN_INTERVAL`
(0.6 s between API calls), `MAILER` (`resend` | `log`), `SCHEDULER_ENABLED`.

## Deployment (DigitalOcean App Platform)

- **App**: `simex-ops`, region fra, one instance. The spec is in `.do/app.yaml` and a push to
  `main` deploys.
- **Database**: an App Platform dev Postgres. The account is at its managed-cluster limit, and
  the dev database has **no automatic backups**. Use Settings → Download backup (everything,
  attachments included, as one JSON file) before and after each exercise day. Restore is on the
  same page: it replaces the configuration, clears the logs, and always comes back disarmed.
- **Updating the spec**: secrets are not in the file. Run
  `.venv/bin/python .do/render_spec.py <app-id>`, then
  `doctl apps update <app-id> --spec .do/app.rendered.yaml`, then delete the rendered file.
- **Exactly one instance.** The scheduler runs inside the web process.
- **Tables are created at startup.** There are no migrations yet; add Alembic before changing
  existing columns.

## Resend
- Receiving: enable receiving on `simex.kobolabs.dev` (an MX record on that name, which is why the
  app lives on `ops.`). Add a webhook to `https://ops.simex.kobolabs.dev/webhooks/resend` for
  `email.received` plus the delivery events, and put its signing secret in `RESEND_WEBHOOK_SECRET`.

- The sending domain is `simex.kobolabs.dev`, with DNS on DigitalOcean.
- To set it up, add the domain in Resend (region eu-west-1, like the other kobolabs.dev
  subdomains), then add its DKIM/SPF/MX records with `doctl compute domain records create
  kobolabs.dev …`. Settings → "Check domain status" shows the records and whether they are
  verified.
- Check the plan's daily quota against the schedule. Roughly one email per team per inject, plus
  test runs.

## Scheduler

- The scheduler is a background thread. Every 15 s it marks scheduled injects past the grace
  period as overdue. If live sending is armed and nothing is paused, it claims due injects
  (`scheduled → sending`, in one UPDATE) and sends them. It also sends due test-run items.
- Emails are sent one at a time, with a gap between Resend calls, and retried on 429/5xx.
- An inject is **sent** when all its emails went out, **partial** when some did ("only retry the
  failed emails" is then offered), and **failed** when none did.
