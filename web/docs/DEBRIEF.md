# Debrief reports (offline)

`manage.py debrief` builds the end-of-exercise reports from a **local copy** of the production
database: one for each team (for the hotwash), one for each team leader (private), optionally one
for each member (private, `--members`), and ExCon's after-action report. Typst lays them out as
PDFs. Code: `ops/debrief.py`, templates `ops/debrief_typst/`.

Dashboard → Reports in the app is a different, lighter tool. It reads the teams' messages to the
personas, not all their mail.

## 1. Copy the database (after the sim ends)

**From the app (simplest):** Settings → Database dump → **Make a database dump**, wait for the
notification, **Download database dump**. The app's server runs `pg_dump` (it is already among
the database's trusted sources), so there is no firewall to open. Then restore it locally, below.

**Or by hand:** `pg_dump` reads the database directly, never through the app:

```sh
doctl databases list                                    # the cluster id of risa-dash-db
doctl databases firewalls append <cluster-id> --rule ip_addr:$(curl -s ifconfig.me)
pg_dump --no-owner --no-acl -Fc "$DATABASE_URL" -f simex_ops-$(date +%Y%m%d-%H%M).dump
doctl databases firewalls list <cluster-id>             # then remove your rule:
doctl databases firewalls remove <cluster-id> --uuid <rule-uuid>
```

`DATABASE_URL` is the app's secret (DO console → the app → Settings). You need `pg_dump` /
`pg_restore` at least at the server's major version (`brew install postgresql@17`, or `libpq`).

```sh
createdb simex_local
pg_restore --no-owner -d simex_local simex_ops-*.dump
```

The dump holds participants' mail, logins and password hashes. Keep it and `debrief-out/` on
your machine only (both are git-ignored), and delete them when the reports are final.

## 2. Environment

Run from `web/`, against the copy only. The command refuses a database host that isn't local.

```sh
export DATABASE_URL=postgres:///simex_local
export AI_PROVIDER=bedrock AI_REGION=us-east-1          # and your AWS credentials / AWS_PROFILE
export AI_MODEL_DEEP=us.anthropic.claude-opus-5-5       # optional: the default deep model is Sonnet
unset RESEND_API_KEY VAPID_PRIVATE_KEY                  # nothing can email or push from the copy
```

Never run `run_scheduler` on the copy: live sending may still be armed in its settings.

## 3. Run

```sh
.venv/bin/python manage.py debrief estimate             # collect, then the size of the notes pass
.venv/bin/python manage.py debrief all --team a         # try one team first, read its PDFs
.venv/bin/python manage.py debrief all --members        # everything (add --ai-written, see below)
```

There are four steps, and each one can be rerun on its own:

| Step | What it does | Model |
|---|---|---|
| `collect` | Every message in each team's mailboxes (team inbox, members' own, the leader's), counted once even when it was copied to several mailboxes, grouped by conversation. Writes `corpus/`, `figures/`. | no |
| `map` | Notes on every conversation and every message the team wrote: audience (government, UN, NGO, media, donor...), register 1–5, clarity 1–5, issues (too informal, curt, no courtesy, jargon, wrong claims...), a quote, and the AI-written estimate. Batches are cached by their input, so a rerun only redoes failed ones. | `--map-model` (deep) |
| `reduce` | The figures, computed in code from the notes (by audience, by author, the leader's answer times), then the write-ups. A report already in `reports/` is kept unless you pass `--force`. | `--model` (deep) |
| `render` | `reports/*.json` → `pdf/`. | no |

**Editing:** change the text in `reports/<name>.json` (`body`), then run `render`. A later
`reduce` without `--force` keeps your edit.

**Prompts:** the instructions are `MAP_RULES`, `TEAM_RULES`, `LEADER_RULES`, `MEMBER_RULES` and
`EXCON_RULES` in `ops/debrief.py`. You can also override them in the database copy, through
`conf` `ai_prompt_debrief_<map|team|leader|member|excon>`.

## What each report contains

- **Team:** overview, timeliness with a per-inject table, strengths, improvements, critical points
  missed, communication with external parties by audience (register and clarity chips, examples),
  inside the team (coordination, leadership, information management), points to note (tone and
  register, language, accuracy, safety, conduct), lessons, recommendations and questions for the
  debrief.
- **Team leader:** tasking, information flow, decisions, representing the team, responsiveness to
  members, strengths, what to develop, lessons.
- **Member:** contribution, strengths, at most three things to work on, a writing tip, and examples
  from their own messages. Developmental only: no grades, no comparison with teammates, never the
  AI-written estimate.
- **ExCon:**
  - teams compared (table and a paragraph each) and themes;
  - lessons for ExCon by area: design, injects, personas and the desk, timing, platform,
    assessment;
  - lessons common to the teams;
  - register by team and audience;
  - flags by team, and the AI-written estimate by team;
  - an injects table, the injects that worked and those to revise, recommendations.

## What the model sees and doesn't see

- **One team at a time.** A team's notes and reports come from its own mailboxes only.
- **No participant names.** Members are "Member n" (the same numbering as Dashboard → Reports),
  and their full names are replaced in the text. A member's real name appears only on the cover of
  their own PDF.
- **Persona details, but not ExCon's notes.** The model gets each persona's name, organisation and
  category, so it can judge register. It never gets `Sender.notes`, role plays or any
  control-side material (`tests/test_roleplay.py` scans `debrief.py`).
- **The AI-written estimate is limited.** It is always in ExCon's report, in the team and leader
  reports only with `--ai-written`, and never in member reports. It is always given as an estimate
  in bands, never as a finding.
