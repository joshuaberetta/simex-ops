# SIMEX inbox (Django)

The Django version of SIMEX ops, **the app in production at https://ops.simex.kobolabs.dev** (spec `../.do/app.yaml`). It keeps every feature of the first, FastAPI app (`../app/`) and adds **in-app team mailboxes (SIMEX Mail)**. Settings → Delivery channel chooses email (Resend, the main flow, and the default), SIMEX Mail (the backup if email has problems) or per inject. Operator manual: [`docs/MANUAL.md`](docs/MANUAL.md) (served at `/help/`); participant guide: [`docs/PARTICIPANT_GUIDE.md`](docs/PARTICIPANT_GUIDE.md) (at `/inbox/help/`).

## What is different from the FastAPI app

| | FastAPI app | Django app |
|---|---|---|
| Delivery | Email via Resend | Email via Resend (default) or **in-app** (SIMEX Mail), for all injects or per inject (Settings → Delivery channel); follow-ups in the same thread |
| Participants | Email addresses only | Logins (one per person, also as a QR code on the login slip), each in a team; a shared **team mailbox** ("SIMEX Mail", a familiar webmail layout) and, with Settings → Personal mailboxes (on by default), each member's own mailbox (My mail) with a copy of all team mail; optional team roles (e.g. Deputy Team Leader) for the figures |
| Replies | Resend inbound webhook, matched by reply token or a guess | In-app replies matched **exactly**; email as before |
| ExCon view of teams | — | **View as team** (read-only, nothing marked read) |
| Team leaders | — | One per team, from ExCon (role `tl`, never a participant): in SIMEX Mail, their own mailbox as the team leader persona, their team's inbox read-only with Claude's reading of each reply, and the team's Dashboard (`ops/views_leader.py`); guide [`docs/LEADER_GUIDE.md`](docs/LEADER_GUIDE.md) |
| Persona desk | — | ExCon role-players (role `desk`, no team, SIMEX Mail only): one shared mailbox at `/inbox/desk/` with every team message to a persona and every persona delivery, colour-tagged per persona and team; they answer or write as any persona but the team leader one, and read every team's and team leader's mailbox read-only (`ops/desk.py`); a Dashboard tab of how the desk is keeping up and what the teams ask each persona (`ops/desk_stats.py`); the Inbox puts what still needs an answer first (waiting, follow-ups, taken by whom, Claude's reading of what needs a reply: `ops/desk_ai.py`); conversations assigned to someone on the desk (Assigned to me, Following, who has what on the Dashboard) and each person's own notifications (a bell: assigned, follow-up due, team wrote again, someone answered); guide [`docs/DESK_GUIDE.md`](docs/DESK_GUIDE.md) |
| Engagement | Resend opens/clicks | In-app **read** (first read, times opened) and attachment downloads; email opens/clicks/bounces as before |
| Users | One shared login | Django auth: ExCon = staff or group `excon`; participants see only `/inbox/` for their team, on its own host (`MAIL_URL`, https://mail.simex.kobolabs.dev, with a webmail-style sign-in), installable on a phone's home screen with push notifications for new mail (`ops/push.py`, `VAPID_PRIVATE_KEY` from `manage.py vapid_keys`) |
| Comms network | — | Dashboard → Network: a live map of who writes to whom (teams, members, leaders, personas; a line per direction, new messages travelling along it), also as a wall screen (`ops/network.py`, `static/ops/network.js`) |
| AI | — | Claude's reading of each reply (tags, rubric suggestions, exercise summary; Dashboard), a review of each unsent inject, Draft with AI (injects, follow-ups, answers), **AI personas** that answer the teams in character (off by default); never written to grades |
| Reports | CSV export | End-of-exercise report per team and one for ExCon (Dashboard → Reports): figures from the app, sections written by Claude and edited by ExCon, printed to PDF |
| ExCon alerts | — | Notifications panel (new replies, AI flags, delivery problems, finished jobs, scheduler stopped) |
| Scheduler | Thread in the web process | Separate worker (`manage.py run_scheduler`) |
| Schema | `create_all` | Django migrations |

## Decisions made (the user can overturn them)

- **Accounts:** one login per participant; one shared mailbox per team; every sent message records who wrote it. ExCon creates logins on **People & logins** (single, or bulk CSV `name,email,team code,role`), sees generated passwords once, can print a credential sheet, reset passwords, disable logins. Each person also has a **personal email**: **Email logins** sends them a new password with their QR code and how to start, and marks the login shared (also "by hand" for printed slips); **Import a roster** (.xlsx / CSV, planned before applied) sets personal emails and teams and adds the people missing. Django admin is a secondary tool only.
- **Who participants can write to:** the active Senders (personas) as an address book, their own team leader among them (the team leader persona). **Team-to-team** messages are a setting, off by default. A team leader writes only to their own team.
- **Channel:** Settings → Delivery channel (`email` by default, `inapp`, or `per_inject` to use each inject's own); always read through `Inject.effective_channel`. `email` keeps the Resend path with idempotency keys, placeholder stripping, reply tokens and the Svix-verified webhook.
- **In-app delivery:** each_team → that team's mailbox; each_person → the person's team mailbox, addressed to them; everyone → a copy in every team's mailbox. "Only these addresses" is email-only. An inject marked **also by email** is, in-app, emailed too (Resend) to each recipient's personal email.
- **Tests:** in-app test sends and test runs go to the **Test team** mailbox with a banner naming the real team; email tests keep the address redirect.
- **Scheduler as a worker component** rather than a thread: the web runs several gunicorn workers, and a separate process keeps deliveries going during web redeploys. Rows are claimed with conditional UPDATEs and `SELECT … FOR UPDATE SKIP LOCKED` on Postgres; in-app items are created in the same transaction that marks the delivery sent.
- **Participant client styling:** the layout follows the webmail clients participants know (top search bar, Compose button and folders on the left, conversation list, threaded view, floating compose window, keyboard shortcuts), built with system fonts and inline Material Design icons (Apache 2.0). It is branded **SIMEX Mail** with its own logo; it uses no Google logo, name, fonts or assets, and must not be presented as Gmail.

## Run locally

```
cd web
uv venv -p 3.12 .venv && uv pip install -p .venv -r requirements.txt pytest pytest-django
.venv/bin/python manage.py migrate && .venv/bin/python manage.py seed_simex      # ExCon login undac / nairobi2026 (APP_PASSWORD: only its first password), teams A-D, Test team
.venv/bin/python manage.py runserver                                            # http://127.0.0.1:8000
.venv/bin/python manage.py run_scheduler                                        # in a second terminal
.venv/bin/python -m pytest -q                                                   # tests (SQLite, log mailer)
```

Environment: `DATABASE_URL` (default SQLite), `SECRET_KEY`, `APP_USERNAME`, `APP_PASSWORD`, `PUBLIC_URL` (an `https://` URL switches on production settings), `MAIL_URL` (SIMEX Mail's own host; unset, it is served on `PUBLIC_URL`), `RESEND_API_KEY` (absent: log-only mailer), `RESEND_WEBHOOK_SECRET`, `MAIL_DOMAIN`, `MAILER` (`resend` | `log`), `RESEND_MIN_INTERVAL`, `EXERCISE_TZ`, `TICK_SECONDS`, `VAPID_PRIVATE_KEY` (push notifications, from `manage.py vapid_keys`), `SOURCE_URL` (the source code linked in the footers, default the GitHub repo).

AI analysis (`ops/ai.py`, Dashboard tab, manual 5.7): `AI_PROVIDER` = `anthropic` (with `ANTHROPIC_API_KEY`), `bedrock` (AWS credentials: `AWS_BEARER_TOKEN_BEDROCK`, or a profile / SSO session locally; `AWS_REGION`, default us-east-1) or `off`; when unset it is `anthropic` if `ANTHROPIC_API_KEY` is set, else `bedrock` if `AWS_BEARER_TOKEN_BEDROCK` is, else off. `AI_MODEL_FAST` / `AI_MODEL_DEEP` override the models (default Claude Haiku 5.5 for each reply, each document a team sends (`ops/ai_docs.py`, `ops/docscan.py`; Settings can switch it to the deep model) and the review of unsent injects, Claude Sonnet 5.5 for rubric suggestions and the summary; on Bedrock the `us.anthropic.` inference profiles). It runs in a thread of `run_scheduler` (`--no-ai` to skip). Locally with AWS SSO: `aws sso login`, then `AI_PROVIDER=bedrock .venv/bin/python manage.py run_scheduler`, and turn it on in Settings → AI analysis. Tests use a fake client and never call a model.

## Deploy

`../.do/app.yaml` (app `simex-ops`, components `web` and `scheduler`, both built from this directory). A push to `main` deploys, so run the tests first. Secrets are not in the spec: `.venv/bin/python .do/render_spec.py <app-id>` (from the repo root) renders a copy with them for `doctl apps update <app-id> --spec .do/app.rendered.yaml`; delete it afterwards. The database is its own database and user on an existing cluster, with the app in the cluster's trusted sources.

Before a live exercise: `python manage.py preflight` (on the web component's console) is a read-only check of settings, teams and logins, the scheduler and every unsent inject, as OK / CHECK / ERROR lines.

## Layout

`simex/` settings and URLs · `ops/models.py` · `ops/conf.py` settings kept in the database · `ops/importer.py`, `render.py`, `mailer.py` (ported) · `ops/documents.py` zip of inject documents · `ops/legacy_import.py` backup from the first app · `ops/delivery.py` scheduler and delivery for both channels · `ops/inbound.py` email webhook, in-app responses, engagement · `ops/views_excon.py` ExCon tabs · `ops/views_inbox.py` the participant mail client (also mounted read-only at `/inbox/as/<team>/`) · `ops/views_leader.py` team leaders' views · `ops/desk.py` the persona desk (`desk_ai.py`: what needs an answer) · `ops/signin.py` QR sign-in · `ops/push.py` Web Push · `ops/ai.py`, `views_ai.py`, `checks.py` Claude analysis, drafts and inject checks · `ops/ai_docs.py`, `docscan.py` reading the documents teams send · `ops/npc.py`, `views_npc.py` AI personas · `ops/report.py`, `views_report.py` reports · `ops/notices.py` ExCon notifications · `ops/templates/ops/mail/` participant templates, `ops/static/ops/mail.{css,js}` · `ops/tests/`.

## Licence

GNU Affero General Public License v3.0: see [`../LICENSE`](../LICENSE). Every page of ops and SIMEX Mail links to the source code (`SOURCE_URL`), as the AGPL asks of software used over a network.
