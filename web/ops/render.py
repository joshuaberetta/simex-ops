"""Turn an inject into the concrete deliveries it produces: emails (one per team, per person,
or one for everyone) or in-app inbox items (one per team inbox / person), with placeholders
filled, footer added and, for tests, recipients redirected."""

import html as htmlmod
import re
from dataclasses import dataclass, field

from django.conf import settings as config

from .models import Person, Sender, Team

PLACEHOLDER = re.compile(r"\{(team|team_name|name|first_name)\}")
EMAIL = re.compile(r"^[^@\s,;<>]+@[^@\s,;<>]+\.[^@\s,;<>]+$")
LOCAL = re.compile(r"^[a-z0-9][a-z0-9._+-]*$")
# Addresses that must never get a live email (seeded placeholders, documentation domains).
PLACEHOLDER_DOMAIN = re.compile(r"(\.invalid|@example\.(com|org|net)|\.example)$", re.I)
URL = re.compile(r"(https?://[^\s<>\"']+)")
PERSONAL = " · personal email"  # ends the target of an in-app inject's email copy (also by email)


@dataclass
class Draft:
    target: str
    from_addr: str
    reply_to: str | None
    to: list
    cc: list
    bcc: list
    subject: str
    body: str
    footer: str
    attachment_ids: list
    warnings: list = field(default_factory=list)
    banner: str | None = None
    original_to: dict | None = None
    team_id: int | None = None
    person_id: int | None = None
    reply_local: str = ""  # with reply_to None, replies go to <reply_local>+<token>@MAIL_DOMAIN
    channel: str = "email"
    inbox_team_id: int | None = None  # in-app: the team inbox it lands in
    inbox_owner_id: int | None = None  # in-app: ... or that team leader's own mailbox
    to_label: str = ""  # in-app: what the To line shows
    from_name: str = ""
    body_html: str = ""  # the body's formatting and images (clean HTML, ops/richtext.py), or "" for plain text

    @property
    def reply_display(self):
        return self.reply_to or f"{self.reply_local}+<ref>@{config.MAIL_DOMAIN} (tracked in Replies)"

    @property
    def text(self):
        parts = ([self.banner] if self.banner else []) + [self.body]
        return "\n\n".join(parts) + (("\n\n--\n" + self.footer) if self.footer else "")

    @property
    def html(self):
        return wrap_html(self.body_html or text_to_html(self.body), self.footer, banner=self.banner)


def split_addresses(value):
    return [a.strip() for a in re.split(r"[,;\s]+", value or "") if a.strip()]


def fill(template, ctx):
    return PLACEHOLDER.sub(lambda m: ctx.get(m.group(1), m.group(0)), template or "")


def text_to_html(text):
    out = []
    for para in re.split(r"\n\s*\n", text.strip()):
        esc = htmlmod.escape(para)
        esc = URL.sub(r'<a href="\1">\1</a>', esc)
        out.append("<p>" + esc.replace("\n", "<br>\n") + "</p>")
    return "\n".join(out)


def wrap_html(body_html, footer_text, banner=None):
    parts = ['<div style="font-family:Arial,Helvetica,sans-serif;font-size:14px;line-height:1.45;color:#111">']
    if banner:
        parts.append('<div style="border:2px solid #b45309;background:#fffbeb;padding:8px 10px;margin-bottom:14px;'
                     'font-size:13px">' + text_to_html(banner) + "</div>")
    parts.append(body_html)
    if footer_text:
        parts.append('<hr style="border:none;border-top:1px solid #ccc;margin:18px 0 8px">'
                     '<div style="font-size:12px;color:#666">' + text_to_html(footer_text) + "</div>")
    parts.append("</div>")
    return "\n".join(parts)


def footer_for(inj, cfg):
    if inj.footer_mode == "none":
        return ""
    if inj.footer_mode == "custom":
        return inj.footer or ""
    return cfg.get("footer", "")


def sender_for(inj):
    return Sender.objects.filter(local_part=inj.sender_local).first() if inj.sender_local else None


DOMAIN = re.compile(r"^(?=.{3,200}$)([a-z0-9{}](?:[a-z0-9{}_-]*[a-z0-9}])?\.)+[a-z]{2,}$")


def persona_address(sender, local, ctx=None, channel="inapp"):
    """The address a persona (username local, {team} filled) shows. In-app: the sender's own
    SIMEX Mail address (any username and domain, Senders tab); by email always
    <local>@MAIL_DOMAIN, the only domain Resend can send from."""
    if channel == "inapp" and sender:
        shown = fill(sender.inapp_local, ctx or {}).lower() if sender.inapp_local else local
        return f"{shown}@{fill(sender.domain, ctx or {}).lower() if sender.domain else config.MAIL_DOMAIN}"
    return f"{local}@{config.MAIL_DOMAIN}"


def sender_by_local(local):
    """(Sender, placeholder context) behind a persona username, also one filled in from a
    {team} username (undac-a-tl from undac-{team}-tl)."""
    s = Sender.objects.filter(local_part=local).first()
    if s:
        return s, {}
    for s in Sender.objects.filter(local_part__contains="{team}"):
        pattern = re.escape(s.local_part).replace(re.escape("{team}"), r"(?P<team>[a-z0-9]+)")
        m = re.fullmatch(pattern, local or "")
        if m:
            return s, {"team": m.group("team")}
    return None, {}


def persona_addr(local, channel="inapp"):
    s, ctx = sender_by_local(local)
    return persona_address(s, local, ctx, channel)


def team_ctx(team):
    return {"team": team.code, "team_name": team.name, "name": team.name, "first_name": team.name}


def leader_persona(team):
    """The team leader persona as team's leader plays it: {"local", "name", "addr"} (username
    with {team} filled, name shown, SIMEX Mail address), or None when no persona is marked
    as the team leader on the Senders tab."""
    s = Sender.objects.filter(team_leader=True, active=True).first()
    if not s or not team:
        return None
    ctx = team_ctx(team)
    local = fill(s.local_part, ctx).lower()
    return {"local": local, "name": (fill(s.display_name, ctx) or local).replace('"', "'"),
            "addr": persona_address(s, local, ctx)}


def leader_locals():
    """{filled team leader username: team id}, over all active teams."""
    s = Sender.objects.filter(team_leader=True, active=True).first()
    if not s:
        return {}
    return {fill(s.local_part, team_ctx(t)).lower(): t.id for t in Team.objects.filter(active=True)}


def scope_teams(inj):
    teams = list(Team.objects.filter(active=True, is_test=False))
    if inj.team_ids:
        teams = [t for t in teams if t.id in inj.team_ids]
    return teams


def _targets(inj, warnings, personal=False):
    """(target label, placeholder context, to-list, team id, person id) per email. personal:
    the copies of an in-app inject to people's personal emails (never a team's address, and
    "only the addresses below" goes to everyone, as in SIMEX Mail)."""
    teams = scope_teams(inj)
    addr = (lambda p: p.personal_email) if personal else (lambda p: p.email)
    mode = "all_bcc" if personal and inj.delivery == "custom" else inj.delivery

    def members(t):
        return [p for p in t.people.participants() if p.active and addr(p)]
    if personal:
        for t in teams:
            missing = [p.name for p in t.people.participants() if p.active and not p.personal_email]
            if missing:
                warnings.append(f"{t.name}: no personal email for {', '.join(missing)}.")
    if mode == "each_team":
        if not teams:
            warnings.append("No active teams: add teams on the People tab.")
        out = []
        for t in teams:
            if personal:  # one email per member: their addresses stay private
                out += [(f"{t.name}: {p.name}", team_ctx(t), [addr(p)], t.id, p.id) for p in members(t)]
                continue
            to = [t.email] if t.email else [p.email for p in members(t)]
            if not to:
                warnings.append(f"{t.name} has no members or team email.")
                continue
            out.append((t.name, {"team": t.code, "team_name": t.name, "name": t.name, "first_name": t.name}, to,
                        t.id, None))
        return out + _leader_targets(inj, teams, addr, personal)
    if mode == "each_person":
        out = []
        for t in teams:
            for p in members(t):
                first = p.name.split()[0] if p.name.split() else p.name
                out.append((f"{p.name} ({t.name})",
                            {"team": t.code, "team_name": t.name, "name": p.name, "first_name": first}, [addr(p)],
                            t.id, p.id))
        if not inj.team_ids and not personal:  # people not in a team are included when sending to everyone
            for p in Person.objects.participants().filter(active=True, team=None).exclude(email="").order_by("name"):
                first = p.name.split()[0] if p.name.split() else p.name
                out.append((p.name, {"team": "", "team_name": "", "name": p.name, "first_name": first}, [p.email],
                            None, p.id))
        if not out:
            warnings.append("No active participants to send to.")
        return out + _leader_targets(inj, teams, addr, personal)
    if mode == "all_bcc":
        people = [addr(p) for t in teams for p in members(t)]
        if not inj.team_ids and not personal:
            people += [p.email for p in Person.objects.participants().filter(active=True, team=None).exclude(email="")]
        if not people:
            warnings.append("No active participants to send to.")
            return []
        return [("all participants (Bcc)", {"team": "", "team_name": "", "name": "", "first_name": ""},
                 ["__bcc__"] + people, None, None)] + _leader_targets(inj, teams, addr, personal)
    return [("custom", {"team": "", "team_name": "", "name": "", "first_name": ""}, [], None, None)]


def _leader_targets(inj, teams, addr=lambda p: p.email, personal=False):
    """Email: an inject marked "Also to team leaders" -> one email to each team's leader (for
    the personal copies, as in SIMEX Mail, not when it comes from the team leader persona)."""
    if not inj.to_leaders:
        return []
    if personal and (s := sender_for(inj)) and s.team_leader:
        return []
    out = []
    for t in teams:
        lead = t.leader()
        if lead and addr(lead):
            out.append((f"{lead.name} (team leader, {t.name})", {**team_ctx(t), "name": lead.name,
                                                                 "first_name": lead.first_name},
                        [addr(lead)], t.id, lead.id))
    return out


def _sender_checks(inj, warnings):
    sender = sender_for(inj)
    if not inj.sender_local:
        warnings.append("No sender set.")
    elif not sender:
        warnings.append(f"Sender '{inj.sender_local}' is not in the Senders list (the address is still used).")
    return sender


def build(inj, cfg):
    """Every delivery of an inject on its channel, or [] with warnings explaining why not. In-app,
    an inject marked "also by email" adds its email copies to people's personal emails."""
    if inj.effective_channel != "inapp":
        return build_email(inj, cfg)
    drafts, warnings = build_inapp(inj, cfg)
    if inj.also_email:
        copies, more = build_email(inj, cfg, personal=True)
        drafts, warnings = drafts + copies, warnings + [f"Email copy: {w}" for w in more if w not in warnings]
    return drafts, warnings


def subject_for(inj, cfg, ctx):
    """Subject prefix, then "[<inject code>] " when Settings asks for it, then the subject."""
    code = f"[{inj.mel_id}] " if cfg.get("subject_inject_code") == "1" and inj.mel_id else ""
    return (cfg.get("subject_prefix") or "") + code + fill(inj.subject or "", ctx)


def _identity(inj, sender, ctx):
    local = fill(inj.sender_local or "", ctx).lower()
    name = fill(inj.from_name or (sender.display_name if sender else "") or local, ctx).replace('"', "'")
    return local, name


def build_inapp(inj, cfg):
    """In-app: one inbox item per team (each_team, all_bcc) or per participant (each_person). A
    participant's item lands in their own mailbox if Settings has personal mailboxes (not in the
    team inbox, which would hold one copy per member), else in the team inbox addressed to them."""
    warnings = []
    personal = cfg.get("personal_mailboxes", "1") == "1"
    sender = _sender_checks(inj, warnings)
    footer = footer_for(inj, cfg)
    teams = scope_teams(inj)
    atts = list(inj.attachments.all()) if inj.pk else []
    if not teams:
        warnings.append("No active teams: add teams on the People tab.")
    targets = []
    for t in teams:
        if inj.delivery == "each_person":
            people = [p for p in t.people.participants() if p.active]
            if not people:
                warnings.append(f"{t.name} has no participants.")
            for p in people:
                targets.append((f"{p.name} ({t.name})", {"team": t.code, "team_name": t.name, "name": p.name,
                                                          "first_name": p.first_name}, t, p, f"{t.name} ({p.name})"))
        elif inj.delivery == "each_team":
            targets.append((t.name, {"team": t.code, "team_name": t.name, "name": t.name, "first_name": t.name},
                            t, None, t.name))
        else:  # all_bcc / custom: the same message in every team's inbox
            targets.append((t.name, {"team": "", "team_name": "", "name": "", "first_name": ""}, t, None,
                            "All teams"))
    # "Also to team leaders": a copy in each team leader's own mailbox, unless the inject comes
    # from the team leader persona (the leader then has it in Sent).
    if inj.to_leaders and not (sender and sender.team_leader):
        for t in teams:
            lead = t.leader()
            if lead:
                targets.append((f"{lead.name} (team leader, {t.name})", {**team_ctx(t), "name": lead.name,
                                                                         "first_name": lead.first_name},
                                t, lead, lead.name))
    drafts = []
    for target, ctx, team, person, to_label in targets:
        local, name = _identity(inj, sender, ctx)
        subject = subject_for(inj, cfg, ctx)
        d = Draft(target=target, from_addr=f'"{name}" <{persona_address(sender, local, ctx)}>', reply_to=None, to=[], cc=[],
                  bcc=[], subject=subject, body=fill(inj.body or "", ctx), footer=footer,
                  attachment_ids=[a.id for a in atts], team_id=team.id, person_id=person.id if person else None,
                  reply_local=local, channel="inapp", inbox_team_id=team.id, to_label=to_label, from_name=name,
                  inbox_owner_id=person.id if person and (person.is_leader or personal) else None)
        if not LOCAL.match(local):
            d.warnings.append(f"Sender username '{local}' is not a valid email username.")
        if not subject.strip():
            d.warnings.append("Empty subject.")
        drafts.append(d)
    return drafts, warnings


def build_email(inj, cfg, personal=False):
    """All emails for an inject, or [] with warnings explaining why not. personal: the copies of
    an in-app inject to people's personal emails (without the inject's extra To, Cc and Bcc)."""
    warnings = []
    sender = sender_for(inj)
    if not inj.sender_local:
        warnings.append("No sender set.")
    elif not sender:
        warnings.append(f"Sender '{inj.sender_local}' is not in the Senders list (the address is still used).")
    reply_to = inj.reply_to or (sender.reply_to if sender else None) or cfg.get("default_reply_to") or None
    footer = footer_for(inj, cfg)
    to_extra, cc, bcc = split_addresses(inj.to_extra), split_addresses(inj.cc), split_addresses(inj.bcc)
    if personal:
        to_extra, cc, bcc = [], [], []
    excon = split_addresses(cfg.get("excon_bcc"))
    drafts = []
    atts = list(inj.attachments.all()) if inj.pk else []
    for target, ctx, to, team_id, person_id in _targets(inj, warnings, personal):
        if personal:
            target += PERSONAL
        local, name = _identity(inj, sender, ctx)
        from_addr = f'"{name}" <{local}@{config.MAIL_DOMAIN}>'
        this_bcc = list(bcc) + excon
        if to and to[0] == "__bcc__":
            # Resend needs a To address: the reply-to if one is set, else the persona itself
            # (it arrives in Replies, where mail from our own domain is ignored).
            this_bcc = to[1:] + this_bcc
            to = [reply_to] if reply_to else [f"{local}@{config.MAIL_DOMAIN}"]
        to = to + to_extra
        subject = subject_for(inj, cfg, ctx)
        d = Draft(target=target, from_addr=from_addr, reply_to=reply_to, to=_dedupe(to), cc=_dedupe(cc),
                  bcc=_dedupe(this_bcc), subject=subject, body=fill(inj.body or "", ctx), footer=footer,
                  attachment_ids=[a.id for a in atts], team_id=team_id, person_id=person_id,
                  reply_local=local, from_name=name)
        total = sum(a.size for a in atts)
        if total > 28 * 1024 * 1024:  # Resend: 40 MB per email after base64 (+33%)
            d.warnings.append(f"Attachments total {total / 1e6:.0f} MB: over Resend's 40 MB per email once encoded.")
        if not LOCAL.match(local):
            d.warnings.append(f"Sender username '{local}' is not a valid email username.")
        if not d.to:
            d.warnings.append("No To address.")
        if not subject.strip():
            d.warnings.append("Empty subject.")
        for a in d.to + d.cc + d.bcc + ([reply_to] if reply_to else []):
            if not EMAIL.match(a):
                d.warnings.append(f"'{a}' does not look like an email address.")
            elif PLACEHOLDER_DOMAIN.search(a):
                d.warnings.append(f"{a} is a placeholder address: live sends skip it.")
        drafts.append(d)
    return drafts, warnings


def _dedupe(addrs):
    seen, out = set(), []
    for a in addrs:
        k = a.lower()
        if k not in seen:
            seen.add(k)
            out.append(a)
    return out


def _key(addr):
    return re.sub(r"[^a-z0-9]+", "-", addr.split("@")[0].lower()).strip("-")[:40] or "x"


def redirect(d: Draft, pattern, label="TEST"):
    """Point every recipient of a draft at the test inbox(es) given by pattern ({key} = the
    original address's username) and add a banner listing the real recipients."""
    def r(addrs):
        return _dedupe([pattern.replace("{key}", _key(a)) for a in addrs])
    lines = [f"{label}: in the live exercise this email would go to"]
    for lab, lst in (("To", d.to), ("Cc", d.cc), ("Bcc", d.bcc)):
        if lst:
            lines.append(f"{lab}: {', '.join(lst)}")
    if not d.to:
        lines.append("WARNING: no To address, so the live send would fail.")
    d.banner = "\n".join(lines)
    d.original_to = {"to": d.to, "cc": d.cc, "bcc": d.bcc}
    d.to, d.cc, d.bcc = r(d.to) or [pattern.replace("{key}", "no-to")], r(d.cc), r(d.bcc)
    d.cc = [a for a in d.cc if a not in d.to]
    d.bcc = [a for a in d.bcc if a not in d.to and a not in d.cc]
    d.subject = f"[{label}] {d.subject}"
    d.warnings = [w for w in d.warnings if "placeholder address" not in w]
    return d


def strip_placeholders(d: Draft):
    """Live sends: drop placeholder addresses. Returns the addresses removed."""
    removed = [a for a in d.to + d.cc + d.bcc if PLACEHOLDER_DOMAIN.search(a)]
    d.to = [a for a in d.to if a not in removed]
    d.cc = [a for a in d.cc if a not in removed]
    d.bcc = [a for a in d.bcc if a not in removed]
    return removed


def redirect_inapp(d: Draft, test_team, label="TEST"):
    """Test runs, in-app: deliver to the Test team's inbox, with a banner naming the real one."""
    d.banner = f"{label}: in the live exercise this goes to the inbox of {d.target}" + (
        f" (To: {d.to_label})" if d.to_label and d.to_label != d.target else "") + "."
    d.original_to = {"inbox": d.target}
    d.inbox_team_id = test_team.id
    if d.inbox_owner_id:  # a team leader's copy goes to the Test team's leader, if it has one;
        owner = Person.objects.filter(pk=d.inbox_owner_id).first()  # a member's, to the Test team's inbox
        lead = test_team.leader() if owner and owner.is_leader else None
        d.inbox_owner_id = lead.id if lead else None
    d.subject = f"[{label}] {d.subject}"
    return d
