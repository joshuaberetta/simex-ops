"""Participant views: the simulated mail client ("SIMEX Mail", laid out like a webmail
client participants know). A mailbox is (team, owner): the team's shared inbox (owner None),
used by its members, or the team leader's own mailbox (owner = the leader). Every query goes
through the Viewer's box, so nobody sees another mailbox, let alone another team; nothing
here reads control-side data.

A team leader also opens their team's inbox read-only at /inbox/team/ (with the AI reading
of the team's replies, shown to leaders only) and the team Dashboard (views_leader.py).
A team leader can also read the other team leaders' mailboxes, read-only, at
/inbox/leaders/<team id>/ (never another team's inbox).
ExCon can open any team's inbox read-only at /inbox/as/<team id>/ ("view as team") and its
leader's at /inbox/as/<team id>/leader/: exactly what they see, without marking anything read.
"""

import json
import re
import uuid
from datetime import timedelta

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import logout
from django.contrib.auth.views import LoginView
from django.core.cache import cache
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import Prefetch, Q
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.http import content_disposition_header
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from . import conf, inbound, push, render as rnd
from .access import is_excon, leader_of, login_required_any, participant_of
from .models import (Mail, MailAttachment, MailLabel, MessageAttachment, PushSubscription, Response, Sender,
                     Team, ThreadLabel, audit)

PAGE = 50
FOLDERS = [("inbox", "Inbox"), ("starred", "Starred"), ("sent", "Sent"), ("drafts", "Drafts"), ("all", "All mail"),
           ("trash", "Trash")]
# A leader's read-only view of their team's inbox: what the team wrote first, no drafts or trash.
TEAM_FOLDERS = [("replies", "Team replies"), ("inbox", "Inbox"), ("sent", "Sent"), ("all", "All mail")]
# A leader's read-only view of another team leader's mailbox: no drafts or trash.
PEER_FOLDERS = [("inbox", "Inbox"), ("starred", "Starred"), ("sent", "Sent"), ("all", "All mail")]
# The team's own labels show as folders "l<id>" (?folder=l12). Gmail's palette: background -> text.
LABEL_COLOURS = {"#fb4c2f": "#fff", "#ffad47": "#462a00", "#fad165": "#4a3b00", "#16a766": "#fff", "#43d692": "#063d22",
                 "#4a86e8": "#fff", "#a479e2": "#fff", "#f691b3": "#4d0f25", "#8e8e8e": "#fff", "#2a2b2e": "#fff"}


# --- auth & misc -----------------------------------------------------------------------------------

class ThrottledLogin(LoginView):
    """Login with simple limits per 10 minutes: 10 failures for one username from one address,
    200 from one address in all (a whole venue is often one address behind its NAT, so a few
    mistyped passwords must not lock every team, or ExCon, out). On the mail host it looks
    like the webmail sign-in page."""
    redirect_authenticated_user = True
    PER_USER, PER_IP = 10, 200

    def get_template_names(self):
        return ["ops/mail/login.html" if getattr(self.request, "mail_host", False) else "ops/login.html"]

    def _keys(self):
        ip = (self.request.META.get("HTTP_X_FORWARDED_FOR") or self.request.META.get("REMOTE_ADDR") or "").split(",")[0]
        user = (self.request.POST.get("username") or "").strip().lower()[:150]
        return f"login-fail:{ip.strip()}:{user}", f"login-fail:{ip.strip()}"

    def post(self, request, *args, **kwargs):
        per_user, per_ip = self._keys()
        if cache.get(per_user, 0) >= self.PER_USER or cache.get(per_ip, 0) >= self.PER_IP:
            messages.error(request, "Too many attempts. Try again in a few minutes.")
            return redirect("/login/")
        return super().post(request, *args, **kwargs)

    def form_invalid(self, form):
        for key in self._keys():
            cache.set(key, cache.get(key, 0) + 1, 600)
        return super().form_invalid(form)


def home(request):
    if not request.user.is_authenticated:
        return redirect("/login/")
    if is_excon(request.user):
        return redirect("/injects/")
    if participant_of(request.user) or leader_of(request.user):
        return redirect("/inbox/")
    logout(request)
    messages.error(request, "This account has no team. Ask ExCon.")
    return redirect("/login/")


def healthz(request):
    return JsonResponse({"ok": True, "scheduler_tick": conf.get("scheduler_tick") or None})


# --- who is looking -------------------------------------------------------------------------------

class Viewer:
    """The mailbox shown (box: team and owner), who is looking (person: the member or leader,
    "me") and whether they may change it. box "team" = a leader's read-only view of their
    team's inbox; box "peer" = a leader's read-only view of another team leader's mailbox
(as_team: that team); box "leader" = ExCon viewing a team leader's mailbox."""

    def __init__(self, request, as_team=None, box=""):
        self.person = participant_of(request.user)
        self.leader = leader_of(request.user)
        self.owner, self.leader_mode = None, False  # leader_mode: a leader reading their team's inbox
        self.peer = box == "peer"
        if self.peer:
            if not self.leader:
                raise PermissionDenied("Team leaders only.")
            self.team = get_object_or_404(peer_teams(self.leader), pk=as_team)
            self.owner = self.team.leader()
            if not self.owner:
                raise Http404
            self.person, self.readonly, self.base = self.leader, True, f"/inbox/leaders/{self.team.id}"
        elif as_team is not None:
            if not is_excon(request.user):
                raise PermissionDenied
            self.team = get_object_or_404(Team, pk=as_team)
            self.readonly, self.person, self.leader, self.base = True, None, None, f"/inbox/as/{self.team.id}"
            if box == "leader":
                self.owner = self.team.leader()
                if not self.owner:
                    raise Http404
                self.base += "/leader"
        elif self.leader:
            self.team, self.person = self.leader.team, self.leader
            if box == "team":
                self.readonly, self.leader_mode, self.base = True, True, "/inbox/team"
            else:
                self.owner, self.readonly, self.base = self.leader, False, "/inbox"
        elif self.person and not box:
            self.team, self.readonly, self.base = self.person.team, False, "/inbox"
        elif self.person:
            raise PermissionDenied("Team leaders only.")
        else:
            raise Http404
        self.box = {"team": self.team, "owner": self.owner}
        self.folders = TEAM_FOLDERS if self.leader_mode else PEER_FOLDERS if self.peer else FOLDERS

    def mails(self):
        return Mail.objects.filter(**self.box)

    def labels(self):
        return MailLabel.objects.filter(**self.box)

    @property
    def leader_box(self):
        """A team leader's own mailbox (their own or, for ExCon, viewed)."""
        return self.owner is not None


def peer_teams(leader):
    """The teams whose leader's mailbox a team leader may read: the other active teams with a
    leader (the Test team only for the Test team's leader, and the other way round)."""
    return (Team.objects.filter(active=True, is_test=leader.team.is_test, people__role="tl", people__active=True)
            .exclude(pk=leader.team_id).distinct())


def mailbox_view(view):
    @login_required_any
    def wrapper(request, *args, as_team=None, box="", **kwargs):
        if as_team is None and not participant_of(request.user) and not leader_of(request.user) \
                and is_excon(request.user):
            return redirect("/inboxes/")  # ExCon: choose a team to view as
        v = Viewer(request, as_team, box)
        if request.method == "POST" and v.readonly:
            raise PermissionDenied("Read-only view.")
        return view(request, v, *args, **kwargs)
    wrapper.__name__ = view.__name__
    return wrapper


# --- address book ----------------------------------------------------------------------------------

def address_book(v):
    """Who the viewer can write to. In the team inbox: the personas ({team} filled with the
    own team, so the team leader persona is their own leader) and, if allowed, the other
    teams. In a team leader's mailbox: their own team only."""
    team = v.team
    if v.leader_box:
        return [{"key": f"t:{team.id}", "name": team.name, "addr": team_address(team), "kind": "team"}]
    ctx = rnd.team_ctx(team)
    book = []
    for s in Sender.objects.filter(active=True):
        local = rnd.fill(s.local_part, ctx).lower()
        book.append({"key": f"p:{local}", "name": rnd.fill(s.display_name, ctx) or local,
                     "addr": rnd.persona_address(s, local, ctx), "kind": "leader" if s.team_leader else "persona"})
    if conf.flag("team_to_team"):
        for t in Team.objects.filter(active=True, is_test=False).exclude(pk=team.pk):
            book.append({"key": f"t:{t.id}", "name": t.name, "addr": team_address(t), "kind": "team"})
    return book


def team_address(t):
    return f"{t.code}@teams.{settings.MAIL_DOMAIN}"


def my_identity(v):
    """(From name, address) of what the viewer sends: a member as themselves at the team's
    address; a team leader as the team leader persona."""
    if v.leader_box:
        lp = rnd.leader_persona(v.team)
        return (lp["name"], lp["addr"]) if lp else (v.owner.name, team_address(v.team))
    return (f"{v.person.name} ({v.team.name})" if v.person else v.team.name), team_address(v.team)


def _resolve(keys, book, v):
    """Address-book keys -> entries. In the team inbox, unknown persona keys (e.g. replying to
    a persona that was since retired) are still accepted as long as the username is valid,
    except another team's leader: teams never write across. A team leader can only use the
    book (their own team)."""
    by = {b["key"]: b for b in book}
    other_leaders = None
    out = []
    for k in keys:
        k = (k or "").strip()
        if not k:
            continue
        if k in by:
            out.append(by[k])
        elif k.startswith("p:") and rnd.LOCAL.match(k[2:]) and not v.leader_box:
            if other_leaders is None:
                other_leaders = {lp for lp, tid in rnd.leader_locals().items() if tid != v.team.id}
            if k[2:] not in other_leaders:
                out.append({"key": k, "name": k[2:], "addr": rnd.persona_addr(k[2:]), "kind": "persona"})
    seen, uniq = set(), []
    for e in out:
        if e["key"] not in seen:
            seen.add(e["key"])
            uniq.append(e)
    return uniq


def _label(entries):
    return ", ".join(f"{e['name']} <{e['addr']}>" for e in entries)


# --- list -----------------------------------------------------------------------------------------

def label_of(v, folder):
    """The mailbox's MailLabel behind a folder key "l<id>", or None."""
    if folder and folder[0] == "l" and folder[1:].isdigit():
        return v.labels().filter(pk=int(folder[1:])).first()
    return None


def _folder_qs(v, folder, q=""):
    qs = v.mails()
    label = label_of(v, folder)
    if label:
        qs = qs.filter(trashed_at__isnull=True, thread__in=label.threads.values("thread")).exclude(folder="drafts")
    elif folder == "trash":
        qs = qs.filter(trashed_at__isnull=False)
    else:
        qs = qs.filter(trashed_at__isnull=True)
        if folder == "inbox":
            qs = qs.filter(folder="inbox", archived=False)
        elif folder == "starred":
            qs = qs.filter(starred=True).exclude(folder="drafts")
        elif folder == "sent":
            qs = qs.filter(folder="sent")
        elif folder == "drafts":
            qs = qs.filter(folder="drafts")
        elif folder == "replies":  # conversations the team wrote in (a leader's view of the team inbox)
            qs = qs.filter(thread__in=v.mails().filter(folder="sent").values("thread")).exclude(folder="drafts")
        else:  # all
            qs = qs.exclude(folder="drafts")
    if q:
        qs = qs.filter(Q(subject__icontains=q) | Q(body__icontains=q) | Q(from_name__icontains=q)
                       | Q(from_addr__icontains=q) | Q(to_label__icontains=q))
    return qs


# A mail's files without their contents (an inject's documents can be many MB, in every team's inbox).
FILES = Prefetch("files", MailAttachment.objects.defer("data"))


def conversations(v, folder, q=""):
    """Gmail-style conversation rows: one per thread, newest first."""
    mails = list(_folder_qs(v, folder, q).prefetch_related(FILES).order_by("-created_at", "-id")[:2000])
    if folder == "drafts":
        return [{"thread": m.thread, "latest": m, "count": 1, "unread": False, "starred": m.starred,
                 "has_files": bool(m.files.all()), "people": "Draft", "draft": True, "subject": m.subject}
                for m in mails]
    threads = {}
    order = []
    for m in mails:
        if m.thread not in threads:
            threads[m.thread] = []
            order.append(m.thread)
        threads[m.thread].append(m)
    # Counts and unread state over the whole conversation (all folders but drafts and trash).
    all_in = {}
    for m in (v.mails().filter(thread__in=order).exclude(folder="drafts")
              .only("id", "thread", "folder", "read_at", "from_name", "from_addr", "to_label", "starred", "trashed_at",
                    "subject", "author__name").select_related("author")):
        all_in.setdefault(m.thread, []).append(m)
    on, ids_on = {}, {}
    for tl in ThreadLabel.objects.filter(label__in=v.labels(), thread__in=order).select_related("label"):
        ids_on.setdefault(tl.thread, []).append(tl.label_id)
        if folder != f"l{tl.label_id}":  # the label being viewed isn't repeated on every row
            on.setdefault(tl.thread, []).append(_label_dict(tl.label))
    rows = []
    for t in order:
        latest = threads[t][0]
        conv = [m for m in all_in.get(t, []) if (folder == "trash") == (m.trashed_at is not None)] or threads[t]
        names = []
        for m in sorted(conv, key=lambda x: x.id):
            n = "me" if m.folder == "sent" and not v.leader_mode else (m.from_name or m.from_addr)
            if v.leader_mode and m.folder == "sent" and m.author:
                n = m.author.name  # who in the team wrote it
            if n not in names:
                names.append(n)
        if folder == "sent":
            people = "To: " + (latest.to_label.split(" <")[0] if latest.to_label else "")
        else:
            people = ", ".join(names[-3:])
        first = min(conv, key=lambda x: x.id)
        rows.append({"thread": t, "latest": latest, "count": len(conv), "subject": first.subject,
                     "unread": any(m.folder == "inbox" and m.read_at is None for m in conv),
                     "starred": any(m.starred for m in conv), "people": people,
                     "has_files": any(m.files.all() for m in threads[t]), "draft": False,
                     "labels": sorted(on.get(t, []), key=lambda x: x["name"].lower()), "all_labels": ids_on.get(t, [])})
    if folder == "replies":  # newest team reply first
        last_sent = {}
        for th, at in v.mails().filter(folder="sent", thread__in=order).values_list("thread", "created_at"):
            last_sent[th] = max(at, last_sent.get(th, at))
        rows.sort(key=lambda r: last_sent.get(r["thread"]) or r["latest"].created_at, reverse=True)
    return rows


def _label_dict(label, unread=0):
    return {"id": label.id, "key": f"l{label.id}", "name": label.name, "colour": label.colour,
            "fg": LABEL_COLOURS.get(label.colour, ""), "unread": unread}


def labels(v):
    """The sidebar: the mailbox's labels with their unread conversations."""
    unread = set(v.mails().filter(folder="inbox", read_at__isnull=True, trashed_at__isnull=True)
                 .values_list("thread", flat=True))
    by_label = {}
    for lid, t in ThreadLabel.objects.filter(label__in=v.labels()).values_list("label_id", "thread"):
        if t in unread:
            by_label[lid] = by_label.get(lid, 0) + 1
    return sorted((_label_dict(lab, by_label.get(lab.id, 0)) for lab in v.labels()),
                  key=lambda x: x["name"].lower())


def _counts(v):
    return {"inbox_unread": _folder_qs(v, "inbox").filter(read_at__isnull=True).values("thread").distinct().count(),
            "drafts": _folder_qs(v, "drafts").count()}


def leader_tabs(leader, current):
    """The tab bar a team leader sees above SIMEX Mail: their mail, the team inbox, the other
    team leaders' mailboxes, the Dashboard."""
    team = leader.team
    mine = Mail.objects.filter(team=team, owner=leader, folder="inbox", archived=False, read_at__isnull=True,
                               trashed_at__isnull=True)
    return {"current": current, "team": team, "peers": list(peer_teams(leader)), "mail_unread": mine.values("thread").distinct().count(),
            "team_new": Mail.objects.filter(team=team, owner=None, id__gt=leader.lead_seen).exclude(folder="drafts").count()}


def _seen(v):
    """A leader looked at the team inbox: its new-mail badge starts again from here."""
    if v.leader_mode:
        last = Mail.objects.filter(team=v.team, owner=None).order_by("-id").values_list("id", flat=True).first() or 0
        if last > v.leader.lead_seen:
            type(v.leader).objects.filter(pk=v.leader.pk).update(lead_seen=last)
            v.leader.lead_seen = last


def _ctx(v, **extra):
    c = {"v": v, "team": v.team, "base": v.base, "readonly": v.readonly, "folders": v.folders, "me": v.person,
         "my_address": my_identity(v)[1], "my_name": my_identity(v)[0], **_counts(v)}
    c.update(extra)
    c["labels"] = [] if v.leader_mode else labels(v)
    folder = c.get("folder", "")
    c["folder_name"] = dict(v.folders).get(folder) or next((lab["name"] for lab in c["labels"] if lab["key"] == folder), "")
    c["label_colours"] = LABEL_COLOURS
    if v.leader:
        c["leader_tabs"] = leader_tabs(v.leader, "team" if v.leader_mode else "peer" if v.peer else "mail")
    c["simex_js"] = {"base": v.base, "readonly": v.readonly, "book": address_book(v), "team": v.team.name,
                     "maxMb": int(conf.get("max_upload_mb") or 10), "folder": c.get("folder", ""),
                     "unread": c["inbox_unread"], "leader": bool(v.leader),
                     # A team leader's device subscribes for their own mailbox from any of their tabs.
                     "vapid": "" if v.readonly and not v.leader else push.public_key(),
                     "pushBase": "/inbox" if v.leader else v.base,
                     "labels": [{"id": lab["id"], "name": lab["name"]} for lab in c["labels"]]}
    return c


def _folder(v, request):
    default = "replies" if v.leader_mode else "inbox"
    folder = request.GET.get("folder", default)
    if folder not in dict(v.folders) and (v.leader_mode or not label_of(v, folder)):
        folder = default
    return folder


@mailbox_view
def inbox(request, v):
    folder = _folder(v, request)
    q = (request.GET.get("q") or "").strip()
    rows = conversations(v, "all" if q and folder in ("inbox", "replies") else folder, q)
    try:
        page = max(1, int(request.GET.get("page") or 1))
    except ValueError:
        page = 1
    total = len(rows)
    start = (page - 1) * PAGE
    _seen(v)
    return render(request, "ops/mail/list.html", _ctx(
        v, folder=folder, q=q, rows=rows[start:start + PAGE], total=total, first=start + 1 if total else 0,
        last=min(start + PAGE, total), page=page, has_prev=page > 1, has_next=start + PAGE < total, today=timezone.localdate()))


def ai_band(score):
    """Claude's 0-100 estimate that a reply was AI-written, as words (an estimate, never proof)."""
    if score is None:
        return ""
    return "Likely AI-assisted" if score >= 70 else "Possibly AI-assisted" if score >= 40 else ""


def _team_notes(mails):
    """A leader reading the team inbox: for each mail the team sent, who wrote it, minutes
    since the message it answers, and Claude's reading of it. Leaders only (ExCon side)."""
    from . import ai
    from .models import ReplyAnalysis
    sent = [m for m in mails if m.folder == "sent"]
    resp = {r.mail_id: r for r in Response.objects.filter(mail__in=sent)}
    tags = {a.response_id: a for a in ReplyAnalysis.objects.filter(response__in=resp.values(), status="done")}
    last_in = None
    for m in mails:
        if m.folder == "inbox":
            last_in = m
            continue
        r = resp.get(m.id)
        a = tags.get(r.id) if r else None
        answered = m.in_reply_to if m.in_reply_to_id and m.in_reply_to.folder == "inbox" else last_in
        m.note = {"author": m.author.name if m.author else "", "ai": a,
                  "after": int((m.created_at - answered.created_at).total_seconds() // 60) if answered else None,
                  "band": ai_band(a.ai_written) if a else "",
                  "quality": round((a.clarity + a.completeness + a.actionability) / 3, 1)
                  if a and None not in (a.clarity, a.completeness, a.actionability) else None,
                  "flags": [ai.FLAGS[f] for f in (a.flags if a else []) if f in ai.FLAGS]}


@mailbox_view
def thread_view(request, v, thread):
    folder = _folder(v, request)
    qs = v.mails().filter(thread=thread).exclude(folder="drafts")
    if folder != "trash":
        qs = qs.filter(trashed_at__isnull=True)
    mails = list(qs.select_related("author", "to_person", "in_reply_to").prefetch_related(FILES).order_by("created_at", "id"))
    if not mails:
        raise Http404
    if not v.readonly:
        v.mails().filter(id__in=[m.id for m in mails], folder="inbox", read_at__isnull=True).update(read_at=timezone.now())
        inbound.record_read([m for m in mails if m.folder == "inbox" and m.message_id], v.person,
                            request.META.get("HTTP_USER_AGENT", ""))
    if v.leader_mode:
        _team_notes(mails)
        _seen(v)
    # Neighbours in the folder list, for j/k and the arrows.
    rows = conversations(v, folder)
    keys = [r["thread"] for r in rows]
    i = keys.index(thread) if thread in keys else -1
    prev_t = keys[i - 1] if i > 0 else None
    next_t = keys[i + 1] if 0 <= i < len(keys) - 1 else None
    last = mails[-1]
    # Reply defaults: answer the last message not sent by us; reply-all adds everyone on it.
    last_in = next((m for m in reversed(mails) if m.folder == "inbox"), None)
    reply_to = []
    if last_in:
        reply_to = [_key_for_sender(last_in)]
    elif last.recipients:
        reply_to = list(last.recipients.get("to", []))
    reply_all = list(dict.fromkeys(reply_to + (last.recipients.get("to", []) + last.recipients.get("cc", [])
                                               if last.folder == "sent" else [])))
    can_reply = True
    if v.leader_box:  # a team leader answers their team only; an inject from a persona can be forwarded
        own = {b["key"] for b in address_book(v)}
        reply_to, reply_all = [k for k in reply_to if k in own], [k for k in reply_all if k in own]
        can_reply = bool(reply_to)
    subject = mails[0].subject
    thread_js = {"subject": subject, "thread": thread, "last": last.id,
                 "reply": [k for k in reply_to if k], "replyAll": [k for k in reply_all if k],
                 "msgs": {str(m.id): {"from": m.from_name, "addr": m.from_addr, "body": m.body, "subject": m.subject,
                                      "date": timezone.localtime(m.created_at).strftime("%a, %b %-d, %Y at %H:%M"),
                                      "to": m.to_label, "reply": [_key_for_sender(m)] if m.folder == "inbox"
                                      else list(m.recipients.get("to", []))} for m in mails}}
    return render(request, "ops/mail/thread.html", _ctx(
        v, folder=folder, mails=mails, subject=subject, thread=thread, prev_t=prev_t, next_t=next_t,
        pos=i + 1, total=len(keys), last=last, thread_js=thread_js, can_reply=can_reply,
        in_inbox=any(m.folder == "inbox" and not m.archived for m in mails), starred=any(m.starred for m in mails),
        thread_labels=[_label_dict(tl.label) for tl in ThreadLabel.objects.filter(label__in=v.labels(), thread=thread)
                       .select_related("label").order_by("label__name")]))


def _key_for_sender(mail):
    if mail.from_addr.endswith(f"@teams.{settings.MAIL_DOMAIN}"):
        t = Team.objects.filter(code=mail.from_addr.split("@")[0]).first()
        return f"t:{t.id}" if t else ""
    return f"p:{mail.persona or mail.from_addr.split('@')[0]}"


@mailbox_view
def guide(request, v):
    from . import docs
    name = "LEADER_GUIDE.md" if v.leader or v.leader_box else "PARTICIPANT_GUIDE.md"
    return render(request, "ops/mail/guide.html", _ctx(v, folder="", guide=docs.html(name)))


# --- actions ------------------------------------------------------------------------------------

@mailbox_view
@require_POST
def action(request, v):
    threads = request.POST.getlist("threads")
    act = request.POST.get("action")
    qs = v.mails().filter(thread__in=threads)
    now = timezone.now()
    if act == "star":
        # Star the latest message of each conversation (as Gmail does).
        for t in threads:
            m = qs.filter(thread=t).exclude(folder="drafts").order_by("-created_at").first() or qs.filter(thread=t).first()
            if m:
                Mail.objects.filter(pk=m.pk).update(starred=True)
    elif act == "unstar":
        qs.update(starred=False)
    elif act == "read":
        qs.filter(read_at__isnull=True).update(read_at=now)
    elif act == "unread":
        qs.filter(folder="inbox").update(read_at=None)
    elif act == "archive":
        qs.filter(folder="inbox").update(archived=True)
    elif act == "unarchive":
        qs.filter(folder="inbox").update(archived=False, trashed_at=None)
    elif act == "trash":
        qs.update(trashed_at=now)
    elif act == "restore":
        qs.update(trashed_at=None)
    elif act in ("label_add", "label_remove", "move"):
        _relabel(v, threads, act, [int(x) for x in request.POST.getlist("label") if x.isdigit()],
                 request.POST.get("to", ""), request.POST.get("from", ""))
    elif act == "discard":
        ids = [int(x) for x in request.POST.getlist("draft_ids") if x.isdigit()]
        drafts = v.mails().filter(folder="drafts")
        (drafts.filter(id__in=ids) if ids else drafts.filter(thread__in=threads)).delete()
    if request.headers.get("x-requested-with") == "fetch":
        return JsonResponse({"ok": True})
    return redirect(request.POST.get("next") if (request.POST.get("next") or "").startswith("/inbox") else "/inbox/")


def _relabel(v, threads, act, ids=(), to="", from_=""):
    """label_add / label_remove the labels ids. move: to a label id or "inbox"; the conversations
    leave the Inbox (or come back to it) and the label being viewed (from_ = "l<id>")."""
    threads = list(v.mails().filter(thread__in=threads).values_list("thread", flat=True).distinct())
    if act == "move":
        to = str(to)
        if to != "inbox" and not (to.isdigit() and v.labels().filter(pk=int(to)).exists()):
            return
        current = label_of(v, from_)
        if current and str(current.id) != to:
            ThreadLabel.objects.filter(label=current, thread__in=threads).delete()
        qs = v.mails().filter(thread__in=threads, folder="inbox")
        if to == "inbox":
            qs.update(archived=False, trashed_at=None)
            return
        ids = [int(to)]
        qs.update(archived=True)
    for label in v.labels().filter(pk__in=ids):
        if act == "label_remove":
            ThreadLabel.objects.filter(label=label, thread__in=threads).delete()
        else:
            have = set(label.threads.filter(thread__in=threads).values_list("thread", flat=True))
            ThreadLabel.objects.bulk_create([ThreadLabel(label=label, thread=t) for t in threads if t not in have])


def _label_name(v, name, exclude=None):
    """A clean, unused label name, or raise ValueError with the message to show."""
    name = " ".join((name or "").split())[:60]
    if not name:
        raise ValueError("Enter a label name.")
    if name.lower() in {label.lower() for _, label in FOLDERS} | {"inbox", "spam"}:
        raise ValueError(f"\"{name}\" is already a folder. Choose another name.")
    if v.labels().filter(name__iexact=name).exclude(pk=exclude).exists():
        raise ValueError(f"The label \"{name}\" already exists.")
    return name


@mailbox_view
@require_POST
def labels_save(request, v):
    """Create (op=create, optionally applied to threads), rename, recolour or delete a label."""
    P, op = request.POST, request.POST.get("op")
    label = None if op == "create" else get_object_or_404(v.labels(), pk=int(P.get("id") or 0))
    try:
        if op == "create":
            label = MailLabel.objects.create(**v.box, name=_label_name(v, P.get("name")))
            if P.getlist("threads"):
                _relabel(v, P.getlist("threads"), "move" if P.get("move") else "label_add", [label.id], label.id,
                         P.get("from", ""))
        elif op == "rename":
            label.name = _label_name(v, P.get("name"), exclude=label.pk)
            label.save(update_fields=["name"])
        elif op == "colour":
            label.colour = P.get("colour") if P.get("colour") in LABEL_COLOURS else ""
            label.save(update_fields=["colour"])
        elif op == "delete":
            label.delete()  # its conversations stay where they are
        else:
            raise ValueError("Unknown action.")
    except ValueError as e:
        return JsonResponse({"error": str(e)}, status=400)
    return JsonResponse({"ok": True, "id": label.pk})


@mailbox_view
@require_POST
def star_mail(request, v, mid):
    m = get_object_or_404(v.mails(), pk=mid)
    m.starred = not m.starred
    m.save(update_fields=["starred"])
    return JsonResponse({"starred": m.starred})


# --- compose, drafts, send -----------------------------------------------------------------------

def _keys(value):
    return [k for k in (value or "").split(",") if k.strip()]


@mailbox_view
@require_POST
def draft_save(request, v):
    """Autosave of the compose window (no files: those are attached on send)."""
    P = request.POST
    d = v.mails().filter(pk=_id(P.get("draft_id")), folder="drafts").first()
    if d is None and _id(P.get("draft_id")):
        return JsonResponse({"error": "This draft was sent or discarded."}, status=410)
    if d is None:
        name, addr = my_identity(v)
        d = Mail(**v.box, folder="drafts", author=v.person, from_name=name, from_addr=addr)
    parent = v.mails().filter(pk=_id(P.get("in_reply_to"))).first()
    d.recipients = {"to": _keys(P.get("to")), "cc": _keys(P.get("cc")), "bcc": _keys(P.get("bcc"))}
    d.to_label = _label(_resolve(d.recipients["to"], address_book(v), v))
    d.subject, d.body = P.get("subject") or "", (P.get("body") or "").replace("\r\n", "\n")
    d.in_reply_to = parent
    d.forward_of = v.mails().filter(pk=_id(P.get("forward_of"))).first()
    d.thread = parent.thread if parent else (d.thread or "")
    d.created_at = timezone.now()
    d.save()
    return JsonResponse({"id": d.id, "saved": timezone.localtime(d.created_at).strftime("%H:%M")})


@mailbox_view
def draft_get(request, v, mid):
    d = get_object_or_404(v.mails(), pk=mid, folder="drafts")
    return JsonResponse({"id": d.id, "to": d.recipients.get("to", []), "cc": d.recipients.get("cc", []),
                         "bcc": d.recipients.get("bcc", []), "subject": d.subject, "body": d.body,
                         "in_reply_to": d.in_reply_to_id, "forward_of": d.forward_of_id})


def _id(v):
    """A posted id, or 0 for none / not a number."""
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


@mailbox_view
@require_POST
def send(request, v):
    P = request.POST
    book = address_book(v)
    to = _resolve(_keys(P.get("to")), book, v)
    cc = _resolve(_keys(P.get("cc")), book, v)
    bcc = _resolve(_keys(P.get("bcc")), book, v)
    subject = (P.get("subject") or "").strip() or "(no subject)"
    body = (P.get("body") or "").replace("\r\n", "\n").rstrip()
    files = request.FILES.getlist("files")
    limit = int(conf.get("max_upload_mb") or 10) * 1024 * 1024
    ajax = request.headers.get("x-requested-with") == "fetch"

    def fail(msg):
        if ajax:
            return JsonResponse({"error": msg}, status=400)
        messages.error(request, msg)
        return redirect(f"{v.base}/")
    if not (to or cc or bcc):
        return fail("Add at least one recipient.")
    if not body and not files and not P.get("forward_of"):
        return fail("Write a message first.")
    if sum(f.size for f in files) > limit:
        return fail(f"Attachments are over {limit // (1024 * 1024)} MB.")
    parent = v.mails().filter(pk=_id(P.get("in_reply_to"))).first()
    fwd = v.mails().filter(pk=_id(P.get("forward_of"))).first()
    try:
        with transaction.atomic():  # all or nothing: a failure must not leave a Sent copy to send again
            if v.leader_box:
                sent = _send_as_leader(request, v, P, subject, body, files, parent, fwd)
            else:
                sent = _send(request, v, P, to, cc, bcc, subject, body, files, parent, fwd)
    except SendFailed as e:
        return fail(str(e))
    if ajax:
        return JsonResponse({"ok": True, "thread": sent.thread, "url": f"{v.base}/t/{sent.thread}/?folder=sent"})
    messages.success(request, "Message sent.")
    return redirect(f"{v.base}/t/{sent.thread}/?folder=sent")


class SendFailed(Exception):
    pass


def _send(request, v, P, to, cc, bcc, subject, body, files, parent, fwd):
    from_name, from_addr = my_identity(v)
    everyone = to + cc + bcc
    personas = [e["key"][2:] for e in everyone if e["kind"] in ("persona", "leader")]
    sent = Mail.objects.create(
        team=v.team, folder="sent", thread=parent.thread if parent else "", from_name=from_name, from_addr=from_addr,
        to_label=_label(to), cc_label=_label(cc), recipients={"to": [e["key"] for e in to], "cc": [e["key"] for e in cc],
                                                             "bcc": [e["key"] for e in bcc]},
        persona=personas[0] if personas else "",
        subject=subject, body=body, author=v.person, in_reply_to=parent, forward_of=fwd,
        inject=parent.inject if parent else (fwd.inject if fwd else None), is_test=v.team.is_test, read_at=timezone.now())
    for f in files:
        MailAttachment.objects.create(mail=sent, filename=f.name[:300], size=f.size,
                                      content_type=f.content_type or "application/octet-stream", data=f.read())
    if fwd:  # forwarded attachments travel with the message (as references, not copies)
        for a in fwd.files.all():
            MailAttachment.objects.create(mail=sent, attachment=a.attachment, filename=a.filename, size=a.size,
                                          content_type=a.content_type, data=a.data)
    if personas:
        inbound.from_mail(sent, personas)

    # Written to the team leader persona: the team's leader has it in their Inbox, same conversation.
    lp = rnd.leader_persona(v.team) if personas else None
    lead = v.team.leader() if lp and lp["local"] in personas else None
    if lead:
        copy = Mail.objects.create(team=v.team, owner=lead, folder="inbox", from_name=from_name, from_addr=from_addr,
                                   to_label=_label(to), cc_label=_label(cc), subject=subject, body=body,
                                   author=v.person, is_test=v.team.is_test, inject=sent.inject, thread=sent.thread)
        for a in sent.files.all():
            MailAttachment.objects.create(mail=copy, attachment=a.attachment, filename=a.filename, size=a.size,
                                          content_type=a.content_type, data=a.data)
        push.notify_mail(copy)
    for e in everyone:
        if e["kind"] == "team":
            t = Team.objects.filter(pk=int(e["key"][2:]), active=True).first()
            if not t or not conf.flag("team_to_team"):
                continue
            copy = Mail.objects.create(team=t, folder="inbox", from_name=from_name, from_addr=from_addr,
                                       to_label=_label(to), cc_label=_label(cc), to_team=t, subject=subject,
                                       body=body, author=v.person, is_test=v.team.is_test,
                                       thread=_peer_thread(t, v.team, parent))
            for a in sent.files.all():
                MailAttachment.objects.create(mail=copy, attachment=a.attachment, filename=a.filename, size=a.size,
                                              content_type=a.content_type, data=a.data)
            push.notify_mail(copy)
            audit("team_to_team", f"{v.team.name} -> {t.name}: {subject[:80]}", request.user.get_username())
    v.mails().filter(pk=_id(P.get("draft_id")), folder="drafts").delete()
    return sent


def _send_as_leader(request, v, P, subject, body, files, parent, fwd):
    """A team leader writes to their team, as the team leader persona: a Message delivered to
    the team inbox at once, which also puts it in the leader's Sent (delivery._put_mail). In
    reply to a member's message to the leader persona it is that Response's answer, as when
    ExCon answers on the Replies tab."""
    from . import delivery
    lp = rnd.leader_persona(v.team)
    if not lp:
        raise SendFailed("No persona is set as the team leader yet: ask ExCon (Senders tab).")
    team, thread = v.team, parent.thread if parent else ""
    answers = (Response.objects.filter(mail__team=team, mail__owner=None, mail__thread=thread, persona=lp["local"])
               .order_by("-id").first() if thread else None)
    d = rnd.Draft(target=team.name, from_addr=f'"{lp["name"]}" <{lp["addr"]}>', reply_to=None, to=[], cc=[], bcc=[],
                  subject=subject, body=body, footer="",
                  attachment_ids=[a.attachment_id for a in fwd.files.all() if a.attachment_id] if fwd else [],
                  team_id=team.id, reply_local=lp["local"], channel="inapp", inbox_team_id=team.id, to_label=team.name)
    inj = answers.inject if answers else (parent.inject if parent else (fwd.inject if fwd else None))
    m = delivery.new_message(inj, d, "reply" if answers else "leader", f"leader-{v.owner.id}-{uuid.uuid4().hex}")
    m.response, m.author, m.thread = answers, v.owner, thread
    m.save()
    for f in files:
        MessageAttachment.objects.create(message=m, filename=f.name[:300], size=f.size,
                                         content_type=f.content_type or "application/octet-stream", data=f.read())
    for a in (fwd.files.all() if fwd else []):
        if not a.attachment_id and a.data is not None:
            MessageAttachment.objects.create(message=m, filename=a.filename, size=a.size, content_type=a.content_type,
                                             data=a.data)
    delivery.deliver([m], respect_pause=False)  # like any participant's mail, not held by Pause
    sent = v.mails().filter(message=m, folder="sent").first()
    if m.status != "sent" or not sent:
        raise SendFailed(m.error or "The message could not be delivered. Try again.")
    v.mails().filter(pk=_id(P.get("draft_id")), folder="drafts").delete()
    audit("leader_mail", f"{team.name}: {subject[:80]}", request.user.get_username())
    return sent


def _peer_thread(team, sender_team, parent):
    """Team-to-team replies continue the other team's copy of the conversation: a mail of the
    same subject between those two teams (never a persona's thread every team has)."""
    if parent and parent.from_addr == team_address(team):
        subject = inbound.SUBJECT_PREFIX.sub("", parent.subject)
        between = Q(from_addr=team_address(sender_team)) | Q(folder="sent", to_label__contains=f"<{team_address(sender_team)}>")
        peer = (Mail.objects.filter(between, team=team, owner=None, trashed_at__isnull=True)
                .filter(Q(subject__iexact=subject) | Q(subject__iregex=r"^((re|fwd?)\s*:\s*)+" + re.escape(subject) + "$"))
                .order_by("-id").first())
        if peer:
            return peer.thread
    return ""


# --- polling & files ------------------------------------------------------------------------------

@login_required_any
def poll(request, as_team=None, box=""):
    try:
        v = Viewer(request, as_team, box)
    except Http404:
        return JsonResponse({"unread": 0, "latest": 0})
    qs = v.mails().filter(folder="inbox", trashed_at__isnull=True)
    latest = qs.order_by("-id").first()
    try:
        since = int(request.GET.get("since") or 0)
    except ValueError:
        since = 0
    new = [{"id": m.id, "from": m.from_name or m.from_addr, "subject": m.subject, "thread": m.thread}
           for m in (qs.filter(id__gt=since).order_by("-id")[:5] if "since" in request.GET else [])]
    out = {"unread": qs.filter(archived=False, read_at__isnull=True).values("thread").distinct().count(),
           "latest": latest.id if latest else 0, "new": new,
           "subject": latest.subject if latest else "", "from": latest.from_name if latest else ""}
    if v.leader:  # the tab bar's badges
        tabs = leader_tabs(v.leader, "")
        out.update(mail_unread=tabs["mail_unread"], team_new=tabs["team_new"])
    return JsonResponse(out)


# --- push notifications & the home-screen app ------------------------------------------------------

# Push services the browsers use; the server only ever posts to these (no arbitrary URLs).
PUSH_HOSTS = (".push.apple.com", "fcm.googleapis.com", ".push.services.mozilla.com", ".notify.windows.com")


@mailbox_view
@require_POST
def push_subscribe(request, v):
    """Save this device's push subscription for the signed-in participant ("off": remove it)."""
    P = request.POST
    endpoint = (P.get("endpoint") or "").strip()
    host = endpoint.split("://", 1)[-1].split("/")[0].lower()
    if not endpoint.startswith("https://") or not any(host == h.lstrip(".") or host.endswith(h) for h in PUSH_HOSTS):
        return JsonResponse({"error": "Unknown push service."}, status=400)
    if P.get("off"):
        PushSubscription.objects.filter(endpoint=endpoint, person=v.person).delete()
        return JsonResponse({"ok": True})
    if not (P.get("p256dh") and P.get("auth")):
        return JsonResponse({"error": "Incomplete subscription."}, status=400)
    PushSubscription.objects.update_or_create(endpoint=endpoint, defaults={
        "person": v.person, "p256dh": P["p256dh"][:200], "auth": P["auth"][:100],
        "user_agent": request.META.get("HTTP_USER_AGENT", "")[:400]})
    return JsonResponse({"ok": True})


def service_worker(request):
    """The service worker (scope /inbox/): shows pushed mail, opens it when tapped."""
    from django.templatetags.static import static
    resp = render(request, "ops/mail/sw.js", {"icon": static("ops/icon-192.png")}, content_type="application/javascript")
    resp["Cache-Control"] = "no-cache"
    return resp


def manifest(request):
    """Web app manifest: "Add to Home Screen" opens SIMEX Mail full screen, like an app."""
    from django.templatetags.static import static
    icons = [{"src": static(f"ops/icon-{n}.png"), "sizes": f"{n}x{n}", "type": "image/png", "purpose": "any"}
             for n in (192, 512)]
    icons.append({"src": static("ops/icon-512.png"), "sizes": "512x512", "type": "image/png", "purpose": "maskable"})
    return JsonResponse({"name": "SIMEX Mail", "short_name": "SIMEX Mail", "id": "/inbox/", "start_url": "/inbox/",
                         "scope": "/", "display": "standalone", "background_color": "#f6f8fc",
                         "theme_color": "#f6f8fc", "icons": icons}, content_type="application/manifest+json")


@login_required_any
def mail_file(request, fid, as_team=None, box=""):
    a = get_object_or_404(MailAttachment.objects.select_related("mail", "attachment"), pk=fid)
    mail = a.mail
    person, leader = participant_of(request.user), leader_of(request.user)
    mine = (person and mail.team_id == person.team_id and mail.owner_id is None) or \
        (leader and mail.team_id == leader.team_id and mail.owner_id == leader.id)
    team_view = leader and mail.team_id == leader.team_id and mail.owner_id is None  # read-only
    peer = leader and mail.owner_id and peer_teams(leader).filter(pk=mail.team_id).first()
    peer_view = peer and mail.owner_id == getattr(peer.leader(), "id", None)  # another leader's mailbox, read-only
    if not (mine or team_view or peer_view or is_excon(request.user)):
        raise Http404
    data = a.content()
    if data is None:
        raise Http404
    if mine and mail.folder == "inbox" and mail.message_id:
        inbound.record_read([mail], person or leader, request.META.get("HTTP_USER_AGENT", ""), what="download",
                            link=a.filename)
    resp = HttpResponse(data, content_type="application/octet-stream")  # download only, never rendered inline
    resp["Content-Disposition"] = content_disposition_header(True, re.sub(r"[\r\n]+", " ", a.filename))
    resp["X-Content-Type-Options"] = "nosniff"
    return resp


# --- email webhook -----------------------------------------------------------------------------------

@csrf_exempt
@require_POST
def resend_webhook(request):
    if not settings.RESEND_WEBHOOK_SECRET:
        return HttpResponse("Webhook secret not configured.", status=503)
    try:
        inbound.verify(settings.RESEND_WEBHOOK_SECRET, request.headers, request.body)
    except inbound.BadSignature as e:
        return HttpResponse(f"Bad signature: {e}", status=401)
    try:
        event = json.loads(request.body)
    except ValueError:
        return HttpResponse("Bad JSON.", status=400)
    rid = inbound.record_event(event)
    if rid:
        inbound.fetch_async(rid)
    return JsonResponse({"ok": True})
