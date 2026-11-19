# SIMEX ops

FastAPI + Jinja (server-rendered, no build step) + SQLAlchemy 2 (Postgres on DO, SQLite locally) web app at https://ops.simex.kobolabs.dev that schedules and sends UNDAC SIMEX email injects via Resend (personas at @simex.kobolabs.dev, which also receives), and tracks and grades participants' replies. README.md is the human-facing version of this file; keep them in sync.

- `app/main.py` routes (tabs: Injects, Test runs, Senders, People & teams, Log, Settings), auth (single login from env), CSRF origin check. New tools go in as new tabs.
- `app/db.py` models; `app/importer.py` MEL/JSON import (participant-facing fields only, plan/apply with local-edit tracking); `app/render.py` inject -> concrete emails (placeholders, footer, test redirect); `app/scheduler.py` loop + live/test/one-off sending; `app/mailer.py` Resend client (idempotency keys) and log mailer; `app/settings.py` DB-backed operator settings.
- `app/inbound.py` Resend webhook (Svix signature), fetch of received emails, matching to message/inject/team (reply-to token `persona+token@`, else a guess); grading (`RubricCriterion`, `Grade`) and the Replies tab are in `main.py`.
- Tests: `.venv/bin/python -m pytest -q` (SQLite, log mailer, scheduler off; `tick()` is called directly).

Django version (branch `django-inbox`, directory `django/`, see `django/README.md`): same features plus in-app team mailboxes as the default channel, participant logins, read-only "view as team", exact in-app reply matching, a separate scheduler worker and Django migrations. Its own tests: `cd django && .venv/bin/python -m pytest -q`. Participant pages (`ops/views_inbox.py`, `templates/ops/mail/`) must filter every query by the viewer's team; every ExCon view is `@excon_required` (a test walks the URLconf).

Rules:
- **Never store control-side material** (expected outcomes, story, theme, key figures, relevance, MEL description). The importer drops them; keep it that way. No exercise content in git (`*.xlsx`, `injects*.json` are ignored).
- Every email gets a `Message` row before it is sent, with a deterministic idempotency key; keep that order so a crash mid-send never duplicates.
- Live sends require `live_armed`; `paused` stops everything; scheduled injects past the grace period become `overdue`, never auto-sent. Placeholder addresses (`.invalid`, example domains) are stripped from live sends.
- Times are naive UTC in the DB, shown and entered in `EXERCISE_TZ` (Africa/Nairobi).
- One app instance only (in-process scheduler). Secrets are not in `.do/app.yaml`; use `.do/render_spec.py` before `doctl apps update`.
- Pushing to `main` deploys to production: run the tests first.
