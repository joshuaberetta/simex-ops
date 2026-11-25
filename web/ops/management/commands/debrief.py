"""manage.py debrief [collect|map|reduce|render|all]: the end-of-exercise debrief reports, built
offline from a local copy of the database (ops/debrief.py). Each step reads the last one's
files in --out; rerun any step on its own (map is cached by input, reduce keeps reports
already written unless --force, render never calls the model)."""

import json
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from ops import ai, debrief, report

LOCAL_HOSTS = {"", "localhost", "127.0.0.1", "::1"}


class Command(BaseCommand):
    help = "Build the debrief reports (team, team leader, members, ExCon) from a local copy of the database."

    def add_arguments(self, p):
        p.add_argument("step", choices=["collect", "map", "reduce", "render", "all", "estimate"])
        p.add_argument("--out", default="debrief-out", help="Working directory (default: debrief-out)")
        p.add_argument("--team", action="append", help="Team code (repeat; default: every active team)")
        p.add_argument("--members", action="store_true", help="Also write a private report for each member")
        p.add_argument("--ai-written", action="store_true",
                       help="Include the AI-written estimate in team and leader reports (always in ExCon's)")
        p.add_argument("--map-model", default="deep", choices=["fast", "deep"], help="Model for the notes (default deep)")
        p.add_argument("--model", default="deep", choices=["fast", "deep"], help="Model for the write-ups (default deep)")
        p.add_argument("--workers", type=int, default=4)
        p.add_argument("--force", action="store_true", help="Redo cached notes and rewrite existing reports")
        p.add_argument("--skip-excon", action="store_true")
        p.add_argument("--typst", default="typst")
        p.add_argument("--remote-ok", action="store_true", help="Allow a database that isn't on this machine")

    def handle(self, step, **o):
        db = settings.DATABASES["default"]
        if db.get("ENGINE", "").endswith("postgresql") and (db.get("HOST") or "") not in LOCAL_HOSTS and not o["remote_ok"]:
            raise CommandError(f"The database is on {db.get('HOST')}: run this on a local copy (pg_dump → pg_restore), "
                               "not production. --remote-ok to override.")
        out = Path(o["out"]).resolve()
        out.mkdir(parents=True, exist_ok=True)
        teams = debrief.teams()
        if o["team"]:
            teams = [t for t in teams if t.code in o["team"]]
            if not teams:
                raise CommandError("No such team.")
        needs_ai = step in ("map", "reduce", "all")
        if needs_ai and not ai.available():
            raise CommandError("No AI provider: set AI_PROVIDER=bedrock (and AWS credentials, AI_REGION) or anthropic.")
        if needs_ai:
            self.stdout.write(f"Models: notes {ai.model_id(o['map_model'])}, write-ups {ai.model_id(o['model'])}")

        if step in ("collect", "all", "estimate"):
            self.collect(out, teams)
        if step == "estimate":
            return self.estimate(out, teams)
        if step in ("map", "all"):
            self.map(out, teams, o)
        if step in ("reduce", "all"):
            self.reduce(out, teams, o)
        if step in ("render", "all"):
            self.render(out, o)

    # --- steps

    def collect(self, out, teams):
        from ops.views_excon import reply_grid
        grid = reply_grid(debrief.KIND)
        for t in teams:
            figs = report.team_figures(t, debrief.KIND, grid)
            corpus = debrief.collect(t, figs)
            debrief._write(out / "corpus" / f"team-{corpus['slug']}.json", corpus)
            debrief._write(out / "figures" / f"team-{corpus['slug']}.json", figs)
            mine = sum(1 for i in corpus["items"] if i["side"] == "team")
            self.stdout.write(f"{t.name}: {len(corpus['items'])} messages ({mine} by the team), "
                              f"{len(corpus['threads'])} conversations")

    def _corpus(self, out, t):
        c = debrief._read(out / "corpus" / f"team-{debrief._slug(t)}.json")
        if not c:
            raise CommandError(f"No corpus for {t.name}: run collect first.")
        return c

    def estimate(self, out, teams):
        total_in = 0
        for t in teams:
            c = self._corpus(out, t)
            bs = debrief.batches(c)
            chars = sum(len(x) for b in bs for _, _, x in b)
            total_in += chars
            self.stdout.write(f"{t.name}: {len(bs)} note calls, ~{chars // 4:,} tokens of mail")
        self.stdout.write(f"Notes: ~{total_in // 4:,} input tokens in all; write-ups add roughly a third again. "
                          "Prices: see ai.PRICES (Bedrock bills its own rates).")

    def map(self, out, teams, o):
        for t in teams:
            c = self._corpus(out, t)
            self.stdout.write(f"{t.name}: notes…")
            n = debrief.run_map(c, out, o["map_model"], o["workers"], o["force"],
                                progress=lambda k, total: self.stdout.write(f"  {k}/{total}", ending="\r"))
            self.stdout.write(f"  {len(n['messages'])} message notes, {len(n['threads'])} conversations"
                              + (f", {n['failed']} batches FAILED (rerun map)" if n["failed"] else ""))

    def _notes(self, out, slug):
        n = debrief._read(out / "notes" / f"team-{slug}.json")
        if not n:
            raise CommandError(f"No notes for {slug}: run map first.")
        return n

    def _keep(self, path, force):
        if path.exists() and not force:
            self.stdout.write(f"  kept {path.name} (--force to rewrite)")
            return True
        return False

    def reduce(self, out, teams, o):
        reports_dir = out / "reports"
        corpora, notes_by, team_reps, figs_all = [], {}, [], []
        for t in teams:
            c = self._corpus(out, t)
            n = self._notes(out, c["slug"])
            figs = debrief._read(out / "figures" / f"team-{c['slug']}.json")
            corpora.append(c)
            notes_by[c["slug"]] = n
            figs_all.append(figs)
            self.stdout.write(f"{t.name}:")
            path = reports_dir / f"team-{c['slug']}.json"
            if not self._keep(path, o["force"]):
                debrief._write(path, debrief.team_report(c, n, figs, o["model"], o["ai_written"]))
                self.stdout.write("  team report written")
            team_reps.append(debrief._read(path))
            path = reports_dir / f"leader-{c['slug']}.json"
            if not self._keep(path, o["force"]):
                r = debrief.leader_report(c, n, figs, o["model"], o["ai_written"])
                if r:
                    debrief._write(path, r)
                    self.stdout.write("  leader report written")
            if o["members"]:
                for p in c["people"]:
                    path = reports_dir / f"member-{c['slug']}-{p['id']}.json"
                    if self._keep(path, o["force"]):
                        continue
                    try:
                        r = debrief.member_report(c, n, figs, p, o["model"])
                    except ai.AiError as e:
                        self.stderr.write(f"  {p['label']}: {e}")
                        continue
                    if r:
                        debrief._write(path, r)
                        self.stdout.write(f"  {p['label']}: written")
        if not o["skip_excon"] and not o["team"]:
            path = reports_dir / "excon.json"
            if not self._keep(path, o["force"]):
                debrief._write(path, debrief.excon_report(corpora, notes_by, team_reps, o["model"], figs_all))
                self.stdout.write("ExCon report written")

    def render(self, out, o):
        done = 0
        for path in sorted((out / "reports").glob("*.json")):
            rep = json.loads(path.read_text())
            pdf = out / "pdf" / debrief.pdf_name(rep)
            try:
                debrief.render_pdf(out, path, pdf, o["typst"])
            except FileNotFoundError:
                raise CommandError("typst not found: brew install typst, or --typst /path/to/typst")
            except Exception as e:
                self.stderr.write(f"{path.name}: {getattr(e, 'stderr', '') or e}")
                continue
            done += 1
            self.stdout.write(f"{pdf.relative_to(out)}")
        self.stdout.write(f"{done} PDFs in {out / 'pdf'}")
