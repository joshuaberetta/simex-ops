"""SIMEX Mail addresses of the teams and of their members' own mailboxes (Senders tab → Team
and member addresses): a username and a domain each, with {team} (the team code) and, for
members, {name} (their full name), {first} and {last}, e.g. undac-{team}@epeigonia.org and
{first}.{last}@epeigonia.org. Empty parts: the defaults below. In-app only: nothing is
emailed to these addresses. Mail keeps the address it was sent from, so the old defaults are
still recognised on older mail."""

import re
import unicodedata

from django.conf import settings

from . import conf, render as rnd
from .models import Person, Sender, Team

KEYS = ("team_local", "team_domain", "member_local", "member_domain")
PLACEHOLDER = re.compile(r"\{(team|name|first|last)\}")


def defaults():
    return {"team_local": "{team}", "team_domain": f"teams.{settings.MAIL_DOMAIN}",
            "member_local": "{name}", "member_domain": f"{{team}}.teams.{settings.MAIL_DOMAIN}"}


def patterns():
    """The four parts in use: Settings (addr_<key>), else the defaults."""
    cfg, d = conf.get_all(), defaults()
    return {k: (cfg.get(f"addr_{k}") or d[k]).lower() for k in KEYS}


def fill(pattern, ctx):
    return PLACEHOLDER.sub(lambda m: ctx.get(m.group(1), ""), pattern or "")


def slug(text):
    """jane.doe from "Jane Doe", without accents or a nickname in brackets."""
    s = unicodedata.normalize("NFKD", re.sub(r"\(.*?\)", " ", text or "")).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", ".", s).strip(".")


def team_address(t, pats=None):
    pats = pats or patterns()
    ctx = {"team": t.code}
    return f"{fill(pats['team_local'], ctx)}@{fill(pats['team_domain'], ctx)}".lower()


def persona_addresses(teams=None):
    """Every SIMEX Mail address a persona shows (a {team} one once per team)."""
    teams = teams if teams is not None else list(Team.objects.all())
    out = set()
    for s in Sender.objects.all():
        team_bound = "{team}" in (s.local_part + s.inapp_local + s.domain)
        for t in (teams if team_bound else [None]):
            ctx = rnd.team_ctx(t) if t else {}
            out.add(rnd.persona_address(s, rnd.fill(s.local_part, ctx).lower(), ctx).lower())
    return out


def member_addresses(pats=None):
    """{person id: their own address} for every team member (participants, active or not, so
    an address never moves to someone else): from the patterns, with their id added when a
    team, a persona or an earlier member already has it."""
    pats = pats or patterns()
    teams = list(Team.objects.all())
    taken = {team_address(t, pats) for t in teams} | persona_addresses(teams)
    out = {}
    for p in Person.objects.participants().filter(team__isnull=False).select_related("team").order_by("id"):
        words = slug(p.name).split(".") or [""]
        ctx = {"team": p.team.code, "name": slug(p.name) or "member", "first": words[0], "last": words[-1]}
        local, domain = fill(pats["member_local"], ctx).strip(".") or "member", fill(pats["member_domain"], ctx)
        addr = f"{local}@{domain}".lower()
        if addr in taken:
            addr = f"{local}.{p.id}@{domain}".lower()
        taken.add(addr)
        out[p.id] = addr
    return out


def team_by_address(addr):
    addr = (addr or "").lower()
    pats = patterns()
    for t in Team.objects.all():
        if team_address(t, pats) == addr:
            return t
    local, _, domain = addr.partition("@")  # the first default, on older mail
    return Team.objects.filter(code=local).first() if domain == f"teams.{settings.MAIL_DOMAIN}".lower() else None


def member_by_address(addr):
    """The member whose own address this is (now, or under the first default), or None."""
    addr = (addr or "").lower()
    pid = next((pid for pid, a in member_addresses().items() if a == addr), None)
    if pid:
        return Person.objects.filter(pk=pid).first()
    local, _, domain = addr.partition("@")
    suffix = f".teams.{settings.MAIL_DOMAIN}".lower()
    team = Team.objects.filter(code=domain[:-len(suffix)]).first() if domain.endswith(suffix) else None
    if not team:
        return None
    for p in Person.objects.participants().filter(team=team):
        old = re.sub(r"[^a-z0-9]+", ".", unicodedata.normalize("NFKD", p.name).encode("ascii", "ignore")
                     .decode().lower()).strip(".")
        if local in (old, f"{old}.{p.id}"):
            return p
    return None


def check(pats):
    """What is wrong with these patterns (a list of messages, empty when fine)."""
    errors = []
    sample = {"team": "a", "name": "jane.doe", "first": "jane", "last": "doe"}
    if "{team}" not in pats["team_local"] + pats["team_domain"]:
        errors.append("The team address needs {team} (in the username or the domain): each team has its own.")
    if not {"name", "first", "last"} & set(PLACEHOLDER.findall(pats["member_local"])):
        errors.append("The member username needs {name}, or {first} and {last}.")
    for key in KEYS:
        filled = fill(pats[key], sample)
        ok = rnd.LOCAL.match(filled) if key.endswith("local") else rnd.DOMAIN.match(filled) and "{" not in filled
        if not ok:
            errors.append(f"'{pats[key]}' is not a valid {'username' if key.endswith('local') else 'domain'}.")
    if not errors:
        teams = list(Team.objects.all())
        clash = sorted({team_address(t, pats) for t in teams} & persona_addresses(teams))
        if clash:
            errors.append(f"Already a persona's address: {', '.join(clash)}.")
    return errors
