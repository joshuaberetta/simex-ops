# SIMEX inbox (Django)

The Django version of SIMEX ops, **the app in production at https://ops.simex.kobolabs.dev** (spec `../.do/app.yaml`). It keeps every feature of the first, FastAPI app (`../app/`) and adds **in-app team mailboxes (SIMEX Mail)**. Settings → Delivery channel chooses email (Resend, the main flow), SIMEX Mail (the backup if email has problems) or per inject. Operator manual: [`docs/MANUAL.md`](docs/MANUAL.md) (served at `/help/`); participant guide: [`docs/PARTICIPANT_GUIDE.md`](docs/PARTICIPANT_GUIDE.md) (at `/inbox/help/`).

## What is different from the FastAPI app

| | FastAPI app | Django app |
|---|---|---|
| Delivery | Email via Resend | Per inject: **in-app** (default) or email via Resend |
| Participants | Email addresses only | Logins (one per person), each in a team; a shared **team mailbox** ("SIMEX Mail", a familiar webmail layout) |
| Replies | Resend inbound webhook, matched by reply token or a guess | In-app replies matched **exactly**; email as before |
| ExCon view of teams | — | **View as team** (read-only, nothing marked read) |
| Engagement | Resend opens/clicks | In-app **read** (first read, times opened) and attachment downloads; email opens/clicks/bounces as before |
| Users | One shared login | Django auth: ExCon = staff or group `excon`; participants see only `/inbox/` for their team, on its own host (`MAIL_URL`, https://mail.simex.kobolabs.dev, with a webmail-style sign-in) |
| Scheduler | Thread in the web process | Separate worker (`manage.py run_scheduler`) |
| Schema | `create_all` | Django migrations |

## Decisions made (the user can overturn them)

- **Accounts:** one login per participant; one shared mailbox per team; every sent message records who wrote it. ExCon creates logins on **People & logins** (single, or bulk CSV `name,email,team code,role`), sees generated passwords once, can print a credential sheet, reset passwords, disable logins. Django admin is a secondary tool only.
- **Who participants can write to:** the active Senders (personas) as an address book. **Team-to-team** messages are a setting, off by default.
- **Channel per inject:** `inapp` by default (also for imports); `email` keeps the Resend path with idempotency keys, placeholder stripping, reply tokens and the Svix-verified webhook.
- **In-app delivery:** each_team → that team's mailbox; each_person → the person's team mailbox, addressed to them; everyone → a copy in every team's mailbox. "Only these addresses" is email-only.
- **Tests:** in-app test sends and test runs go to the **Test team** mailbox with a banner naming the real team; email tests keep the address redirect.
- **Scheduler as a worker component** rather than a thread: the web runs several gunicorn workers, and a separate process keeps deliveries going during web redeploys. Rows are claimed with conditional UPDATEs and `SELECT … FOR UPDATE SKIP LOCKED` on Postgres; in-app items are created in the same transaction that marks the delivery sent.
- **Participant client styling:** the layout follows the webmail clients participants know (top search bar, Compose button and folders on the left, conversation list, threaded view, floating compose window, keyboard shortcuts), built with system fonts and inline Material Design icons (Apache 2.0). It is branded **SIMEX Mail** with its own logo; it uses no Google logo, name, fonts or assets, and must not be presented as Gmail.

## Run locally

```
cd web
uv venv -p 3.12 .venv && uv pip install -p .venv -r requirements.txt pytest pytest-django
.venv/bin/python manage.py migrate && .venv/bin/python manage.py seed_simex      # ExCon login undac / nairobi2026 (APP_PASSWORD), teams A-D, Test team
.venv/bin/python manage.py runserver                                            # http://127.0.0.1:8000
.venv/bin/python manage.py run_scheduler                                        # in a second terminal
.venv/bin/python -m pytest -q                                                   # tests (SQLite, log mailer)
```

Environment: `DATABASE_URL` (default SQLite), `SECRET_KEY`, `APP_USERNAME`, `APP_PASSWORD`, `PUBLIC_URL` (an `https://` URL switches on production settings), `RESEND_API_KEY`, `RESEND_WEBHOOK_SECRET`, `MAIL_DOMAIN`, `EXERCISE_TZ`, `TICK_SECONDS`.

AI analysis (`ops/ai.py`, Dashboard tab, manual 5.7): `AI_PROVIDER` = `anthropic` (with `ANTHROPIC_API_KEY`), `bedrock` (AWS credentials: `AWS_BEARER_TOKEN_BEDROCK`, or a profile / SSO session locally; `AWS_REGION`, default us-east-1) or `off`; when unset it is `anthropic` if `ANTHROPIC_API_KEY` is set, else `bedrock` if `AWS_BEARER_TOKEN_BEDROCK` is, else off. `AI_MODEL_FAST` / `AI_MODEL_DEEP` override the models (default Claude Haiku 5.5 for each reply, Claude Sonnet 5.5 for rubric suggestions and the summary; on Bedrock the `us.anthropic.` inference profiles). It runs in a thread of `run_scheduler` (`--no-ai` to skip). Locally with AWS SSO: `aws sso login`, then `AI_PROVIDER=bedrock .venv/bin/python manage.py run_scheduler`, and turn it on in Settings → AI analysis. Tests use a fake client and never call a model.

## Deploy

`.do/app-django.yaml` (app `simex-inbox`, components `web` and `scheduler`). Render the secrets into a copy before `doctl apps create --spec`, then add the app to the database cluster's trusted sources. Not deployed yet.

## Layout

`simex/` settings and URLs · `ops/models.py` · `ops/importer.py`, `render.py`, `mailer.py` (ported) · `ops/delivery.py` scheduler and delivery for both channels · `ops/inbound.py` email webhook, in-app responses, engagement · `ops/views_excon.py` ExCon tabs · `ops/views_inbox.py` the participant mail client (also mounted read-only at `/inbox/as/<team>/`) · `ops/templates/ops/mail/` participant templates, `ops/static/ops/mail.{css,js}` · `ops/tests/`.
