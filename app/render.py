"""Turn an inject into the concrete emails it produces (one per team, per person, or one
for everyone), with placeholders filled, footer added and, for tests, recipients redirected."""

import html as htmlmod
import re
from dataclasses import dataclass, field

from . import config
from .db import Person, Sender, Team

PLACEHOLDER = re.compile(r"\{(team|team_name|name|first_name)\}")
EMAIL = re.compile(r"^[^@\s,;<>]+@[^@\s,;<>]+\.[^@\s,;<>]+$")
LOCAL = re.compile(r"^[a-z0-9][a-z0-9._+-]*$")
# Addresses that must never get a live email (seeded placeholders, documentation domains).
PLACEHOLDER_DOMAIN = re.compile(r"(\.invalid|@example\.(com|org|net)|\.example)$", re.I)
URL = re.compile(r"(https?://[^\s<>\"']+)")


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

    @property
    def text(self):
        parts = ([self.banner] if self.banner else []) + [self.body]
        return "\n\n".join(parts) + (("\n\n--\n" + self.footer) if self.footer else "")

    @property
    def html(self):
        return wrap_html(text_to_html(self.body), self.footer, banner=self.banner)


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


def sender_for(s, inj):
    return s.query(Sender).filter_by(local_part=inj.sender_local).one_or_none() if inj.sender_local else None


def _targets(s, inj, warnings):
    """(target label, placeholder context, to-list) per email."""
    teams = s.query(Team).filter_by(active=True).order_by(Team.sort, Team.name).all()
    if inj.team_ids:
        teams = [t for t in teams if t.id in inj.team_ids]
    def members(t):
        return [p for p in t.people if p.active]
    if inj.delivery == "each_team":
        if not teams:
            warnings.append("No active teams: add teams on the People tab.")
        out = []
        for t in teams:
            to = [t.email] if t.email else [p.email for p in members(t)]
            if not to:
                warnings.append(f"{t.name} has no members or team email.")
                continue
            out.append((t.name, {"team": t.code, "team_name": t.name, "name": t.name, "first_name": t.name}, to))
        return out
    if inj.delivery == "each_person":
        out = []
        for t in teams:
            for p in members(t):
                first = p.name.split()[0] if p.name.split() else p.name
                out.append((f"{p.name} ({t.name})",
                            {"team": t.code, "team_name": t.name, "name": p.name, "first_name": first}, [p.email]))
        if not inj.team_ids:  # people not in a team are included when sending to everyone
            for p in s.query(Person).filter_by(active=True, team_id=None).order_by(Person.name):
                first = p.name.split()[0] if p.name.split() else p.name
                out.append((p.name, {"team": "", "team_name": "", "name": p.name, "first_name": first}, [p.email]))
        if not out:
            warnings.append("No active participants to send to.")
        return out
    if inj.delivery == "all_bcc":
        people = [p.email for t in teams for p in members(t)]
        if not inj.team_ids:
            people += [p.email for p in s.query(Person).filter_by(active=True, team_id=None)]
        if not people:
            warnings.append("No active participants to send to.")
            return []
        return [("all participants (Bcc)", {"team": "", "team_name": "", "name": "", "first_name": ""}, ["__bcc__"] + people)]
    return [("custom", {"team": "", "team_name": "", "name": "", "first_name": ""}, [])]


def build(s, inj, cfg):
    """All emails for an inject, or [] with warnings explaining why not."""
    warnings = []
    sender = sender_for(s, inj)
    if not inj.sender_local:
        warnings.append("No sender set.")
    elif not sender:
        warnings.append(f"Sender '{inj.sender_local}' is not in the Senders list (the address is still used).")
    reply_to = inj.reply_to or (sender.reply_to if sender else None) or cfg.get("default_reply_to") or None
    footer = footer_for(inj, cfg)
    to_extra, cc, bcc = split_addresses(inj.to_extra), split_addresses(inj.cc), split_addresses(inj.bcc)
    excon = split_addresses(cfg.get("excon_bcc"))
    drafts = []
    for target, ctx, to in _targets(s, inj, warnings):
        local = fill(inj.sender_local or "", ctx).lower()
        name = fill(inj.from_name or (sender.display_name if sender else "") or local, ctx)
        name = name.replace('"', "'")
        from_addr = f'"{name}" <{local}@{config.MAIL_DOMAIN}>'
        this_bcc = list(bcc) + excon
        if to and to[0] == "__bcc__":
            # Resend needs a To address. Use the reply-to inbox (ExCon), never the sender's
            # own address: nothing receives mail there, so it would bounce.
            this_bcc = to[1:] + this_bcc
            to = [reply_to] if reply_to else excon[:1]
        to = to + to_extra
        subject = (cfg.get("subject_prefix") or "") + fill(inj.subject or "", ctx)
        d = Draft(target=target, from_addr=from_addr, reply_to=reply_to, to=_dedupe(to), cc=_dedupe(cc),
                  bcc=_dedupe(this_bcc), subject=subject, body=fill(inj.body or "", ctx), footer=footer,
                  attachment_ids=[a.id for a in inj.attachments])
        total = sum(a.size for a in inj.attachments)
        if total > 28 * 1024 * 1024:  # Resend: 40 MB per email after base64 (+33%)
            d.warnings.append(f"Attachments total {total / 1e6:.0f} MB: over Resend's 40 MB per email once encoded.")
        if not LOCAL.match(local):
            d.warnings.append(f"Sender username '{local}' is not a valid email username.")
        if not d.to:
            d.warnings.append("No To address." + (" All-Bcc emails go To the reply-to address: set one."
                                                  if inj.delivery == "all_bcc" else ""))
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
        lines.append("WARNING: no To address, so the live send would fail. Set a default reply-to (Settings).")
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
