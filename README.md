# SIMEX ops

Web app for UNDAC SIMEX exercise control at **https://simex.kobolabs.dev**. The first tab schedules
and sends the email injects through [Resend](https://resend.com). The other tabs (test runs,
senders, people, log, settings) support it, and more tools can be added as tabs later.

It is the hosted companion of the local knowledge base in `~/code/undac-simex`. Exercise content
never goes into this repo. It reaches the app only through the import page.

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

- **App**: `simex-ops`, region fra, one instance. The spec is in `.do/app.yaml` and the database
  is the managed Postgres cluster `simex-ops-db`. A push to `main` deploys.
- **Updating the spec**: secrets are not in the file. Run
  `.venv/bin/python .do/render_spec.py <app-id>`, then
  `doctl apps update <app-id> --spec .do/app.rendered.yaml`, then delete the rendered file.
- **Exactly one instance.** The scheduler runs inside the web process.
- **Tables are created at startup.** There are no migrations yet; add Alembic before changing
  existing columns.

## Resend

- The sending domain is `simex.kobolabs.dev`, with DNS on DigitalOcean.
- To set it up, add the domain in Resend (region eu-west-1, like the other kobolabs.dev
  subdomains), then add its DKIM/SPF/MX records with `doctl compute domain records create
  kobolabs.dev …`. Settings → "Check domain status" shows the records and whether they are
  verified.
- Nobody receives mail at `@simex.kobolabs.dev`, so set a default reply-to (Settings) to an ExCon
  inbox.
- Check the plan's daily quota against the schedule. Roughly one email per team per inject, plus
  test runs.

## Scheduler

- The scheduler is a background thread. Every 15 s it marks scheduled injects past the grace
  period as overdue. If live sending is armed and nothing is paused, it claims due injects
  (`scheduled → sending`, in one UPDATE) and sends them. It also sends due test-run items.
- Emails are sent one at a time, with a gap between Resend calls, and retried on 429/5xx.
- An inject is **sent** when all its emails went out, **partial** when some did ("only retry the
  failed emails" is then offered), and **failed** when none did.
