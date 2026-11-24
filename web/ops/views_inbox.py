"""Participant views: the simulated mail client ("SIMEX Mail", laid out like a webmail
client participants know). A mailbox is (team, owner): the team's shared inbox (owner None),
used by its members, a member's own mailbox at /inbox/me/ (owner = the member: a copy of the
team's mail, and mail between the member, their teammates and their leader), or the team
leader's own mailbox (owner = the leader). Every query goes through the Viewer's box, so
nobody sees another mailbox, let alone another team; nothing here reads control-side data.

A team leader also opens their team's inbox read-only at /inbox/team/ (with the AI reading
of the team's replies, shown to leaders only) and the team Dashboard (views_leader.py).
A team leader can also read the other team leaders' mailboxes, read-only, at
/inbox/leaders/<team id>/ (never another team's inbox), and their own members' mailboxes,
read-only, at /inbox/members/<person id>/.
ExCon can open any team's inbox read-only at /inbox/as/<team id>/ ("view as team"), its
leader's at /inbox/as/<team id>/leader/ and a member's at /inbox/as/<team id>/member/<person id>/:
exactly what they see, without marking anything read. The persona desk (ops/desk.py) has its
shared mailbox at /inbox/desk/ (team None, owner None), writing as the personas, and the same
read-only views of every team.
"""

import copy
import logging
import json
import re
import uuid
from collections import Counter
from datetime import timedelta

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import get_user_model, login, logout
from django.contrib.auth.views import LoginView, LogoutView
from django.core.cache import cache
from django.core.exceptions import PermissionDenied
from django.db import IntegrityError, transaction
from django.db.models import Count, Prefetch, Q
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.http import content_disposition_header
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from . import (addresses, conf, delivery, desk, desk_ai, inbound, push, render as rnd, richtext, signin, simclock,
               suggest_leader)
from .access import desk_of, is_excon, leader_of, login_required_any, participant_of, rp_of
from .models import (CalendarEvent, Mail, MailAction, MailAttachment, MailLabel, MessageAttachment, Person, PushSubscription, Response,
                     Sender, Team, ThreadLabel, audit)

log = logging.getLogger(__name__)
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

    def form_valid(self, form):
        resp = super().form_valid(form)
        resp.delete_cookie(SIGNED_OUT, domain=settings.SESSION_COOKIE_DOMAIN)
        return resp


# Set on sign-out: on this device the home-screen app (which starts at a sign-in link) no longer
# signs back in by itself, so a shared phone doesn't open the last person's mail.
SIGNED_OUT = "simex_signed_out"


class SignOut(LogoutView):
    next_page = "/login/"

    def post(self, request, *args, **kwargs):
        resp = super().post(request, *args, **kwargs)
        resp.set_cookie(SIGNED_OUT, "1", max_age=365 * 24 * 3600, domain=settings.SESSION_COOKIE_DOMAIN,
                        secure=settings.SESSION_COOKIE_SECURE, httponly=True, samesite="Lax")
        return resp


def signin_link(request, token):
    """The QR code on a login slip: sign in on this device and set it up (alerts, home screen).
    ?app=1 is the home-screen app starting: already signed in, or signed out on purpose here,
    it just opens the mailbox."""
    user = signin.user_for(token)
    app = request.GET.get("app") == "1"
    if app and (request.user == user or request.COOKIES.get(SIGNED_OUT)):
        user = None
    if not user:
        if not app and not request.user.is_authenticated:
            messages.error(request, "This sign-in code no longer works. Sign in with the username and password "
                                    "on your slip, or ask ExCon for a new slip.")
        return redirect("/inbox/")
    if request.user != user:
        request.signin_method = "qr"
        login(request, user, backend=settings.AUTHENTICATION_BACKENDS[0])
        audit("signin_qr", user.username, user.username)
    resp = redirect(signin.setup_path(user))
    resp.delete_cookie(SIGNED_OUT, domain=settings.SESSION_COOKIE_DOMAIN)
    return resp


def home(request):
    if not request.user.is_authenticated:
        return redirect("/login/")
    if is_excon(request.user):
        return redirect("/injects/")
    if participant_of(request.user) or leader_of(request.user):
        return redirect("/inbox/")
    if desk_of(request.user):
        return redirect("/inbox/desk/")
    if rp_of(request.user):
        return redirect("/inbox/rp/")
    logout(request)
    messages.error(request, "This account has no team. Ask ExCon.")
    return redirect("/login/")


def healthz(request):
    return JsonResponse({"ok": True, "scheduler_tick": conf.get("scheduler_tick") or None})


# --- who is looking -------------------------------------------------------------------------------

class Viewer:
    """The mailbox shown (box: team and owner), who is looking (person: the member or leader,
    "me") and whether they may change it. box "me" = a member's own mailbox; "team" = a
    leader's read-only view of their team's inbox; "peer" = a leader's read-only view of
    another team leader's mailbox (as_team: that team); "member" = a leader's read-only view
    of one of their members' mailboxes (pid), or ExCon's (as_team, pid); "leader" = ExCon
    viewing a team leader's mailbox; "desk" = the persona desk's shared mailbox (team None),
    where person is the desk member (None for ExCon staff). The persona desk also has ExCon's
    read-only views of the teams (as_team)."""

    def __init__(self, request, as_team=None, box="", pid=None):
        self.person = participant_of(request.user)
        self.leader = leader_of(request.user)
        self.desk_person = desk_of(request.user)
        self.owner, self.leader_mode = None, False  # leader_mode: a leader reading their team's inbox
        self.peer = box == "peer"
        self.desk = box == "desk"
        self.user = request.user
        if box in ("me", "member") and not conf.flag("personal_mailboxes"):
            raise Http404
        if self.desk:
            if not (self.desk_person or is_excon(request.user)):
                raise PermissionDenied("Persona desk only.")
            self.team, self.person, self.leader = None, self.desk_person, None
            self.readonly, self.base = False, "/inbox/desk"
        elif self.peer:
            if not self.leader:
                raise PermissionDenied("Team leaders only.")
            self.team = get_object_or_404(peer_teams(self.leader), pk=as_team)
            self.owner = self.team.leader()
            if not self.owner:
                raise Http404
            self.person, self.readonly, self.base = self.leader, True, f"/inbox/leaders/{self.team.id}"
        elif as_team is not None:
            if not (is_excon(request.user) or self.desk_person):
                raise PermissionDenied
            self.team = get_object_or_404(Team, pk=as_team)
            self.readonly, self.person, self.leader, self.base = True, None, None, f"/inbox/as/{self.team.id}"
            if box == "leader":
                self.owner = self.team.leader()
                if not self.owner:
                    raise Http404
                self.base += "/leader"
            elif box == "member":
                self.owner = get_object_or_404(team_members(self.team), pk=pid)
                self.base += f"/member/{self.owner.id}"
        elif box == "member":
            if not self.leader:
                raise PermissionDenied("Team leaders only.")
            self.team, self.person = self.leader.team, self.leader
            self.owner = get_object_or_404(team_members(self.team), pk=pid)
            self.readonly, self.base = True, f"/inbox/members/{self.owner.id}"
        elif self.leader:
            self.team, self.person = self.leader.team, self.leader
            if box == "team":
                self.readonly, self.leader_mode, self.base = True, True, "/inbox/team"
            elif not box:
                self.owner, self.readonly, self.base = self.leader, False, "/inbox"
            else:
                raise Http404
        elif self.person and not box:
            self.team, self.readonly, self.base = self.person.team, False, "/inbox"
        elif self.person and box == "me":
            self.team, self.owner, self.readonly, self.base = self.person.team, self.person, False, "/inbox/me"
        elif self.person:
            raise PermissionDenied("Team leaders only.")
        else:
            raise Http404
        self.box = {"team": self.team, "owner": self.owner}
        # Claude's readings (replies, documents): a leader reading their own team, ExCon staff
        # reading a team's mailboxes. Never members, other leaders or the persona desk.
        self.sees_ai = bool(self.leader_mode or (box == "member" and self.leader and not as_team)
                            or (as_team is not None and not self.peer and is_excon(request.user)))
        self.folders = TEAM_FOLDERS if self.leader_mode else PEER_FOLDERS if self.peer or box == "member" and self.leader \
            else FOLDERS

    def mails(self):
        return Mail.objects.filter(**self.box)

    def labels(self):
        return MailLabel.objects.filter(**self.box)

    def events(self):
        """The events on this mailbox's calendar (SIMEX Calendar, ops/calendar.py): those with a
        row for it, the organiser's included. The persona desk's are every persona's."""
        return CalendarEvent.objects.filter(attendees__team=self.team, attendees__owner=self.owner,
                                            attendees__hidden=False).distinct()

    @property
    def leader_box(self):
        """A team leader's own mailbox (their own or, for ExCon or a peer, viewed)."""
        return self.owner is not None and self.owner.is_leader

    @property
    def member_box(self):
        """A team member's own mailbox (their own or, for their leader or ExCon, viewed)."""
        return self.owner is not None and not self.owner.is_leader


def team_members(team):
    """A team's members who have a mailbox of their own (participants, active or not)."""
    return Person.objects.participants().filter(team=team).select_related("team_role")


def peer_teams(leader):
    """The teams whose leader's mailbox a team leader may read: the other active teams with a
    leader (the Test team only for the Test team's leader, and the other way round)."""
    return (Team.objects.filter(active=True, is_test=leader.team.is_test, people__role="tl", people__active=True)
            .exclude(pk=leader.team_id).distinct())


def mailbox_view(view):
    @login_required_any
    def wrapper(request, *args, as_team=None, box="", pid=None, **kwargs):
        if as_team is None and box != "desk" and not participant_of(request.user) and not leader_of(request.user):
            if desk_of(request.user):
                return redirect("/inbox/desk/")
            if rp_of(request.user):
                return redirect("/inbox/rp/")  # a role player has no mailbox: their role plays
            if is_excon(request.user):
                return redirect("/inboxes/")  # ExCon: choose a team to view as
        if box == "me" and not conf.flag("personal_mailboxes") and participant_of(request.user):
            return redirect("/inbox/")
        v = Viewer(request, as_team, box, pid)
        # The persona desk's Inbox in sections (default) or, like any email, newest first: each
        # login's choice, kept in its session (?order=newest | sections).
        v.desk_newest = bool(v.desk and request.session.get("desk_order") == "newest")
        if request.method == "POST" and v.readonly:
            raise PermissionDenied("Read-only view.")
        return view(request, v, *args, **kwargs)
    wrapper.__name__ = view.__name__
    return wrapper


# --- address book ----------------------------------------------------------------------------------

def address_book(v):
    """Who the viewer can write to. In the team inbox and a member's own mailbox: the personas
    ({team} filled with the own team, so the team leader persona is their own leader), their
    teammates (personal mailboxes), from their own mailbox the team itself and, if allowed,
    the other teams. In a team leader's mailbox: their own team and its members only. On the
    persona desk: every active team, member and team leader, and the other personas (copied on
    a message to a team: desk.copy_entries)."""
    if v.desk:
        teams = list(Team.objects.filter(active=True))
        return ([{"key": f"t:{t.id}", "name": t.name, "addr": team_address(t), "kind": "team"} for t in teams]
                + desk_members(teams) + desk_leaders(teams) + desk.copy_entries())
    team = v.team
    mates = []
    if conf.flag("personal_mailboxes"):
        locs = member_locals(team)
        mates = [{"key": f"m:{p.id}", "name": addresses.member_name(p, team), "addr": member_address(p, locs), "kind": "member"}
                 for p in team_members(team).filter(active=True) if p not in (v.owner, v.person)]
    if v.leader_box:
        return [{"key": f"t:{team.id}", "name": team.name, "addr": team_address(team), "kind": "team"}] + mates
    ctx = rnd.team_ctx(team)
    book = []
    for s in Sender.objects.filter(active=True):
        local = rnd.fill(s.local_part, ctx).lower()
        book.append({"key": f"p:{local}", "name": rnd.fill(s.display_name, ctx) or local,
                     "addr": rnd.persona_address(s, local, ctx), "kind": "leader" if s.team_leader else "persona",
                     "org": s.organisation})  # the organisation it speaks for, shown to the teams
    book += mates
    if v.member_box or (mates and v.person and not v.readonly):  # the team, from a member's own address
        book.append({"key": f"t:{team.id}", "name": team.name, "addr": team_address(team), "kind": "team"})
    if conf.flag("team_to_team"):
        for t in Team.objects.filter(active=True, is_test=False).exclude(pk=team.pk):
            book.append({"key": f"t:{t.id}", "name": t.name, "addr": team_address(t), "kind": "team"})
    return book


def desk_members(teams):
    """The persona desk writes to members one by one too: every active member of those teams,
    at their own SIMEX Mail address (with personal mailboxes; else their team inbox has it, for
    them) or, when injects go by email, their own email."""
    by_email = conf.get("channel_mode") == "email"
    locs = member_locals() if not by_email else {}
    people = (Person.objects.participants().filter(active=True, team__in=teams).select_related("team")
              .order_by("team__name", "name"))
    return [{"key": f"m:{p.id}", "name": addresses.member_name(p), "kind": "member", "team": p.team_id,
             "addr": (p.email or "no email address") if by_email else member_address(p, locs)} for p in people]


def leader_name(p, team=None):
    """How the desk names a team leader: "Team A leader (Jane Doe)"."""
    team = team or p.team
    return f"{team.name} leader ({p.name})" if team else p.name


def desk_leaders(teams):
    """The persona desk (and Compose) write to the team leaders too: each active team's leader,
    in their own mailbox (at the team leader persona's address they play) or, when injects go
    by email, at their email."""
    by_email = conf.get("channel_mode") == "email"
    out = []
    for t in teams:
        p = t.leader()
        if not p:
            continue
        lp = rnd.leader_persona(t)
        addr = (p.email or "no email address") if by_email else (lp["addr"] if lp else team_address(t))
        out.append({"key": f"l:{p.id}", "name": leader_name(p, t), "kind": "tl", "team": t.id, "addr": addr})
    return out


CONTACT_SCAN = 3000  # the latest mails of a mailbox read for its contacts


def growing(v):
    """Whether the viewer's address book is the one it has built (a team inbox or a member's own
    mailbox, as in any webmail), rather than everyone it may write to (a team leader, the desk)."""
    return not (v.leader_box or v.desk or v.peer or v.leader_mode)


def contacts(v, book=None):
    """The address book shown: those the mailbox has written to or heard from, latest first.
    It starts empty; anyone else in address_book(v) is reached by typing their exact address
    (lookup). A member's contacts are their own mailbox's and the team inbox's."""
    book = address_book(v) if book is None else book
    if not growing(v):
        return book
    boxes = Q(team=v.team, owner=None)
    if v.member_box or (v.person and not v.person.is_leader):
        me = v.owner if v.member_box else v.person
        boxes |= Q(team=v.team, owner=me)
    last, memo = {}, {}
    rows = (Mail.objects.filter(boxes).exclude(folder="draft").order_by("-id")
            .values("folder", "from_addr", "persona", "recipients", "created_at")[:CONTACT_SCAN])
    for m in rows:
        keys = []
        if m["folder"] == "inbox":
            keys.append(_key_for_sender(Mail(from_addr=m["from_addr"], persona=m["persona"]), memo))
        r = m["recipients"] or {}
        keys += list(r.get("to", [])) + list(r.get("cc", [])) + (list(r.get("bcc", [])) if m["folder"] == "sent" else [])
        for k in keys:
            if k and k not in last:
                last[k] = m["created_at"]
    known = [b for b in book if b["key"] in last]
    return sorted(known, key=lambda b: last[b["key"]], reverse=True)


def team_address(t):
    return addresses.team_address(t)


def member_locals(team=None):
    """{person id: their own address} (addresses.member_addresses: the Senders tab's pattern)."""
    return addresses.member_addresses()


def member_address(p, locs=None):
    """A member's own address, e.g. jane.doe@a.teams.<MAIL_DOMAIN> (Senders tab)."""
    return (locs or member_locals()).get(p.id) or f"member.{p.id}@{settings.MAIL_DOMAIN}"


member_by_address = addresses.member_by_address


# What participants do in their mailbox, kept with its time for the end-of-exercise reports
# (MailAction). Reading and downloading are Events (inbound.record_read).
TRACKED = {"star", "unstar", "read", "unread", "archive", "unarchive", "trash", "restore", "label_add", "label_remove",
           "move", "discard", "label_create", "label_delete", "search", "draft", "send",
           "cal_create", "cal_update", "cal_cancel", "cal_rsvp"}  # SIMEX Calendar (ops/calendar.py)


def track(v, action, threads=(), **detail):
    """Log a participant's (or a leader's, in their own mailbox) action. Never ExCon's or a
    read-only view."""
    if v.readonly or not v.person or v.desk or action not in TRACKED:
        return None
    return MailAction.objects.create(**v.box, person=v.person, action=action, threads=[t for t in threads if t][:200],
                                     detail=detail)


def my_identity(v):
    """(From name, address) of what the viewer sends: a member as themselves at the team's
    address, or from their own mailbox at their own; a team leader as the team leader persona;
    the persona desk chooses a persona for each message."""
    if v.desk:
        return "Persona desk", ""
    if v.leader_box:
        lp = rnd.leader_persona(v.team)
        return (lp["name"], lp["addr"]) if lp else (v.owner.name, team_address(v.team))
    if v.member_box:
        return addresses.member_name(v.owner, v.team), member_address(v.owner)
    return (addresses.member_name(v.person, v.team) if v.person else v.team.name), team_address(v.team)


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
        elif k.startswith("p:") and rnd.LOCAL.match(k[2:]) and not v.leader_box and not v.desk:
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


# In a team leader's mailbox, what their team writes to the team leader persona is labelled by the
# address it came from: the team's (written in the team inbox) or a member's own.
AUTO_LABELS = {"team": ("Team account", "#4a86e8"), "member": ("Personal account", "#16a766")}


def auto_label(mail, kind):
    """Label mail's conversation in its mailbox with the AUTO_LABELS one for kind (made if
    missing; a label of that name the leader made already is used)."""
    name, colour = AUTO_LABELS[kind]
    box = {"team_id": mail.team_id, "owner_id": mail.owner_id}
    label = (MailLabel.objects.filter(**box, auto=kind).first()
             or MailLabel.objects.filter(**box, name__iexact=name).first())
    if not label:
        try:
            with transaction.atomic():  # two members' first mails to the leader at once
                label = MailLabel.objects.create(**box, name=name, colour=colour, auto=kind)
        except IntegrityError:
            label = MailLabel.objects.get(**box, name=name)
    if label.auto != kind:
        MailLabel.objects.filter(pk=label.pk).update(auto=kind)
    ThreadLabel.objects.get_or_create(label=label, thread=mail.thread)


# --- list -----------------------------------------------------------------------------------------

def label_of(v, folder):
    """The mailbox's MailLabel behind a folder key "l<id>", or None."""
    if folder and folder[0] == "l" and folder[1:].isdigit():
        return v.labels().filter(pk=int(folder[1:])).first()
    return None


def desk_filter(v, folder):
    """On the persona desk, a sidebar filter "a<sender id>" (one persona's conversations) or
    "t<team id>" (one team's): (kind, Sender or Team), or None."""
    if not (v.desk and folder and folder[0] in "at" and folder[1:].isdigit()):
        return None
    obj = (Sender if folder[0] == "a" else Team).objects.filter(pk=int(folder[1:])).first()
    return (folder[0], obj) if obj else None


# The persona desk's own folders: what is assigned to me, what I follow, and (from its Dashboard)
# what one person has ("o<user id>") or nobody has ("o0").
DESK_FOLDERS = {"mine": "Assigned to me", "watching": "Following"}


def desk_assigned(v, folder):
    """On the persona desk, a folder of conversations by who has them: (name, threads, a
    queryset of thread ids) or None."""
    from .models import DeskConversation, DeskWatch
    if not (v.desk and folder):
        return None
    owned = DeskConversation.objects.filter(owner__isnull=False)
    if folder == "mine":
        return DESK_FOLDERS[folder], owned.filter(owner=v.user).values("thread")
    if folder == "watching":
        return DESK_FOLDERS[folder], (DeskWatch.objects.filter(user=v.user, off=False)
                                      .exclude(thread__in=owned.filter(owner=v.user).values("thread")).values("thread"))
    if folder[0] == "o" and folder[1:].isdigit():
        uid = int(folder[1:])
        if uid == 0:
            return "Not assigned", None
        u = get_user_model().objects.filter(pk=uid).first()
        if u:
            return f"Assigned to {desk.user_name(u)}", owned.filter(owner_id=uid).values("thread")
    return None


def persona_locals(sender):
    """The usernames a Sender writes as: its own, or with {team}, one per team."""
    if "{team}" not in sender.local_part:
        return [sender.local_part]
    return [rnd.fill(sender.local_part, rnd.team_ctx(t)).lower() for t in Team.objects.all()]


def _folder_qs(v, folder, q=""):
    qs = v.mails()
    label = label_of(v, folder)
    flt = desk_filter(v, folder)
    own = desk_assigned(v, folder)
    if flt:
        qs = qs.filter(trashed_at__isnull=True).exclude(folder="drafts")
        qs = qs.filter(desk_team=flt[1]) if flt[0] == "t" else qs.filter(persona__in=persona_locals(flt[1]))
    elif own:
        from .models import DeskConversation
        qs = qs.filter(trashed_at__isnull=True).exclude(folder="drafts")
        qs = (qs.exclude(thread__in=DeskConversation.objects.filter(owner__isnull=False).values("thread"))
              if own[1] is None else qs.filter(thread__in=own[1]))
    elif label:
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
                 "has_files": bool(m.attached), "people": "Draft", "draft": True, "subject": m.subject}
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
                    "subject", "author__name", "persona", "desk_team", "is_test").select_related("author")):
        all_in.setdefault(m.thread, []).append(m)
    on, ids_on = {}, {}
    for tl in ThreadLabel.objects.filter(label__in=v.labels(), thread__in=order).select_related("label"):
        ids_on.setdefault(tl.thread, []).append(tl.label_id)
        if folder != f"l{tl.label_id}":  # the label being viewed isn't repeated on every row
            on.setdefault(tl.thread, []).append(_label_dict(tl.label))
    rows = []
    memo, team_names = {}, dict(Team.objects.values_list("id", "name")) if v.desk else {}
    for t in order:
        latest = threads[t][0]
        conv = [m for m in all_in.get(t, []) if (folder == "trash") == (m.trashed_at is not None)] or threads[t]
        names = []
        for m in sorted(conv, key=lambda x: x.id):
            n = "me" if m.folder == "sent" and not v.leader_mode and not v.desk else (m.from_name or m.from_addr)
            if v.leader_mode and m.folder == "sent" and m.author:
                n = m.author.name  # who in the team wrote it
            if n not in names:
                names.append(n)
        if folder == "sent":
            people = "To: " + (latest.to_label.split(" <")[0] if latest.to_label else "")
        else:
            people = ", ".join(names[-3:])
        first = min(conv, key=lambda x: x.id)
        # Replied (the list's return arrow): something was sent after the latest mail received.
        got = max((m.id for m in conv if m.folder == "inbox"), default=None)
        replied = got is not None and any(m.folder == "sent" and m.id > got for m in conv)
        rows.append({"thread": t, "latest": latest, "count": len(conv), "subject": first.subject,
                     "unread": any(m.folder == "inbox" and m.read_at is None for m in conv),
                     "starred": any(m.starred for m in conv), "people": people,
                     "has_files": any(m.attached for m in threads[t]), "draft": False,
                     "labels": sorted(on.get(t, []), key=lambda x: x["name"].lower()), "all_labels": ids_on.get(t, []),
                     "desk": desk_chips(conv, memo, team_names) if v.desk else None,
                     "replied": replied and folder != "sent",
                     "invite": any(m.event_id for m in threads[t])})  # SIMEX Calendar
    if v.sees_ai:
        flags = ai_flags(v.team, {t: threads[t] for t in order})
        for r in rows:
            r["ai"] = flags.get(r["thread"])
    if v.desk and folder not in ("drafts", "trash"):  # what still needs an answer (desk.states)
        sts = desk.states(order, v.user)
        here = desk.on_now(order, v.user.id)
        for r in rows:
            r["state"], r["here"] = sts.get(r["thread"]), here.get(r["thread"])
        if sectioned(v, folder):
            rows.sort(key=lambda r: desk.sort_key(r["state"], r["latest"].created_at))
    if folder == "replies":  # newest team reply first
        last_sent = {}
        for th, at in v.mails().filter(folder="sent", thread__in=order).values_list("thread", "created_at"):
            last_sent[th] = max(at, last_sent.get(th, at))
        rows.sort(key=lambda r: last_sent.get(r["thread"]) or r["latest"].created_at, reverse=True)
    return rows


def sectioned(v, folder):
    """The persona desk's Inbox (and its persona and team filters): in sections, what needs
    attention first (desk.SECTIONS), unless the viewer chose newest first (desk_orderable)."""
    return desk_orderable(v, folder) and not getattr(v, "desk_newest", False)


def desk_orderable(v, folder):
    """The desk lists that can be in sections or newest first."""
    return v.desk and (folder == "inbox" or bool(desk_filter(v, folder)) or bool(desk_assigned(v, folder)))


def ai_level(pct):
    """The AI-written estimate's band, for the meter's colour and words: likely (70 % and up),
    possibly (40 %), else unlikely."""
    return "likely" if pct >= 70 else "possibly" if pct >= 40 else "unlikely"


def ai_flags(team, threads):
    """{thread: {"pct", "level", "why"}} for conversations holding a message the team wrote, or a
    file it sent, that SIMEX AI has read for how likely it is AI-assisted: the highest estimate,
    shown as a meter on the row. threads: {thread: [Mail]} with their files. For the readers of
    Viewer.sees_ai only."""
    from .models import DocumentAnalysis, ReplyAnalysis
    if not team or not threads:
        return {}
    best = {}

    def keep(t, pct, why):
        if pct is not None and pct > best.get(t, {}).get("pct", -1):
            best[t] = {"pct": pct, "level": ai_level(pct), "why": why}

    for t, pct, sig, subj in (ReplyAnalysis.objects.filter(status="done", response__team=team, response__mail__thread__in=list(threads))
                              .values_list("response__mail__thread", "ai_written", "ai_signals", "response__subject")):
        keep(t, pct, f"Message: {sig or subj}")
    shas = {f.sha256: t for t, ms in threads.items() for m in ms for f in m.files.all() if f.sha256}
    for d in DocumentAnalysis.objects.filter(team=team, status="done", sha256__in=list(shas)):
        keep(shas[d.sha256], d.ai_written, f"{d.filename}: {d.ai_signals}")
    return best


def desk_chips(mails, memo, team_names):
    """What a desk conversation shows to tell it apart: its personas (name and colour) and the
    teams on the other side."""
    personas, teams = [], []
    for m in sorted(mails, key=lambda x: x.id):
        if m.persona and m.persona not in [p["local"] for p in personas]:
            personas.append(desk.describe(m.persona, memo=memo))
        name = team_names.get(m.desk_team_id)
        if name and name not in teams:
            teams.append(name)
    return {"personas": personas, "teams": teams, "test": any(m.is_test for m in mails),
            "colour": personas[0]["colour"] if personas else ""}


def desk_side(v):
    """The persona desk's sidebar: each persona and each team, with its unread conversations."""
    unread = list(v.mails().filter(folder="inbox", read_at__isnull=True, trashed_at__isnull=True, archived=False)
                  .values_list("thread", "persona", "desk_team_id").distinct())
    from .models import NpcProfile
    ai = set(NpcProfile.objects.filter(enabled=True).values_list("sender_id", flat=True)) if conf.flag("npc_enabled") else set()
    personas = []
    for s in desk.personas():
        locs = set(persona_locals(s))
        bg, fg = desk.colour(s)
        personas.append({"key": f"a{s.id}", "name": s.display_name or s.local_part, "addr": s.inapp_address,
                         "colour": bg, "fg": fg, "ai": s.id in ai,
                         "unread": len({t for t, p, _ in unread if p in locs})})
    teams = [{"key": f"t{t.id}", "name": t.name, "test": t.is_test,
              "unread": len({th for th, _, tid in unread if tid == t.id})} for t in Team.objects.filter(active=True)]
    return {"personas": personas, "teams": teams, "attention": desk.attention_count(), "mine": _desk_mine(v)}


def _desk_mine(v):
    """The sidebar's Assigned to me and Following: how many of each need attention, and in all."""
    out = {}
    for key in DESK_FOLDERS:
        threads = list(Mail.objects.filter(team=None, owner=None, trashed_at__isnull=True,
                                           thread__in=desk_assigned(v, key)[1]).exclude(folder="drafts")
                       .order_by().values_list("thread", flat=True).distinct())
        sts = desk.states(threads, v.user)
        out[key] = {"n": len(threads), "attention": sum(1 for st in sts.values() if st["section"] == desk.ATTENTION)}
    return out


def desk_tabs(person, current, user=None):
    """A persona desk member's tab bar: the desk, and the teams' and team leaders' mailboxes
    (read-only); with user, the bell of their desk notices."""
    teams = list(Team.objects.filter(active=True))
    leads = {p.team_id: p for p in Person.objects.filter(role="tl", active=True, team__in=teams)}
    for t in teams:
        t.lead = leads.get(t.id)
    return {"current": current, "teams": teams, "unread": _unread(None, None),
            "bell": desk.notice_counts(user) if user else None,
            "chat": conf.flag("desk_chat")}  # ChatSIMEX, on every desk page


def apps(v, request):
    """The app drawer (the grid button in SIMEX Mail's top bar, ops/mail/_apps.html): the tiles
    of the apps this login has, in a fixed order per role, the one open marked, with unread mail
    on the mail tiles. v: the mailbox shown, if any (ExCon viewing a team gets that mailbox's
    Mail and Calendar)."""
    user = getattr(request, "user", None)
    if not user or not user.is_authenticated:
        return []
    from . import roleplay
    member, lead, desk_p, rp = participant_of(user), leader_of(user), desk_of(user), rp_of(user)

    def tile(key, name, url, icon, n=0):
        return {"key": key, "name": name, "url": url, "icon": icon, "n": n}

    tiles = []
    if member:
        tiles.append(tile("mail", f"{member.team.name} inbox", "/inbox/", "mail", _unread(member.team, None)))
        if conf.flag("personal_mailboxes"):
            tiles.append(tile("me", "My mail", "/inbox/me/", "mail_me", _unread(member.team, member)))
        tiles += [tile("calendar", "Calendar", "/inbox/calendar/", "calendar"), tile("help", "Help", "/inbox/help/", "help")]
    elif lead:
        tiles += [tile("mail", "My mail", "/inbox/", "mail", _unread(lead.team, lead)),
                  tile("team", f"{lead.team.name} inbox", "/inbox/team/", "mail_team"),
                  tile("members", "Team members", "/inbox/members/", "people"),
                  tile("calendar", "Calendar", "/inbox/calendar/", "calendar"),
                  tile("dashboard", "Dashboard", "/inbox/dashboard/", "dashboard")]
        if roleplay.has_role_plays(lead):
            tiles.append(tile("rp", "Role plays", "/inbox/rp/", "roleplay"))
        tiles.append(tile("help", "Help", "/inbox/help/", "help"))
    elif desk_p or (v is not None and v.desk):
        tiles += [tile("desk", "Persona desk", "/inbox/desk/", "mail_desk", _unread(None, None)),
                  tile("calendar", "Calendar", "/inbox/desk/calendar/", "calendar"),
                  tile("teams", "Teams", "/inbox/desk/teams/", "people"),
                  tile("dashboard", "Dashboard", "/inbox/desk/dashboard/", "dashboard"),
                  tile("network", "Network", "/inbox/desk/network/", "network"),
                  tile("assign", "Assign", "/inbox/desk/assign/", "assign")]
        if desk_p and roleplay.has_role_plays(desk_p):
            tiles.append(tile("rp", "Role plays", "/inbox/rp/", "roleplay"))
        tiles.append(tile("help", "Help", "/inbox/desk/help/", "help"))
    elif rp:
        tiles += [tile("rp", "Role plays", "/inbox/rp/", "roleplay"), tile("help", "Briefing", "/inbox/rp/help/", "help")]
    if v is not None and v.readonly and v.base.startswith("/inbox/as/"):  # ExCon or the desk viewing a team
        what = f"{v.owner.name}'s" if v.owner else f"{v.team.name}'s"
        tiles += [tile("viewed", f"{what} mail", v.base + "/", "mail_view"),
                  tile("viewed_cal", f"{what} calendar", v.base + "/calendar/", "calendar")]
    if is_excon(user) and not (member or lead or desk_p or rp):
        tiles.append(tile("excon", "Back to ExCon", "/inboxes/" if v is not None and not v.desk else "/", "excon"))
    path = request.path
    cal = "/calendar/" in path
    best = None
    for t in tiles:  # the open one: the longest url this page is under (a calendar page: its Calendar)
        if (t["icon"] == "calendar") != cal:
            continue
        if path.startswith(t["url"]) and (best is None or len(t["url"]) > len(best["url"])):
            best = t
    if v is not None and not v.readonly:  # the Calendar tile: invitations to answer, events about to start
        from . import calendar
        info = calendar.poll_info(v)
        for t in tiles:
            if t["key"] == "calendar" and info:
                t["n"] = info["badge"]
    if cal and best is None:
        best = next((t for t in tiles if t["icon"] == "calendar"), None)
    for t in tiles:
        t["on"] = t is best
    return tiles


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


def _unread(team, owner):
    return (Mail.objects.filter(team=team, owner=owner, folder="inbox", archived=False, read_at__isnull=True,
                                trashed_at__isnull=True).values("thread").distinct().count())


def leader_tabs(leader, current):
    """The tab bar a team leader sees above SIMEX Mail: their mail, the team inbox, their team's
    members (with their team roles and, read-only, their own mailboxes), the other team
    leaders' mailboxes, the Dashboard, with AI personas on, their team's conversations with them,
    and the role plays they play (ops/roleplay.py), if any."""
    team = leader.team
    from . import npc, roleplay
    return {"current": current, "team": team, "peers": list(peer_teams(leader)), "mail_unread": _unread(team, leader),
            "npcs": npc.shown_to(team), "roleplay": roleplay.has_role_plays(leader),
            "team_new": Mail.objects.filter(team=team, owner=None, id__gt=leader.lead_seen).exclude(folder="drafts").count(),
            "members": list(team_members(team).filter(active=True)), "personal": conf.flag("personal_mailboxes")}


def member_tabs(person, current):
    """A member's tab bar (with personal mailboxes on): the team inbox and their own mailbox."""
    if not conf.flag("personal_mailboxes"):
        return None
    return {"current": current, "team": person.team, "team_unread": _unread(person.team, None),
            "me_unread": _unread(person.team, person)}


def _seen(v):
    """A leader looked at the team inbox: its new-mail badge starts again from here."""
    if v.leader_mode:
        last = Mail.objects.filter(team=v.team, owner=None).order_by("-id").values_list("id", flat=True).first() or 0
        if last > v.leader.lead_seen:
            type(v.leader).objects.filter(pk=v.leader.pk).update(lead_seen=last)
            v.leader.lead_seen = last


DESK = {"name": "Persona desk", "id": None}  # what the templates show as the desk's "team"


# The kind of mailbox, for the strip in the top bar and its colours (mail.css body.box-<kind>), so
# nobody mistakes whose mail they are reading or writing from.
BOX_COLOURS = {"team": "#eef3fd", "me": "#edf7ef", "leader": "#f4effb", "desk": "#ecf6f5", "excon": "#fbf4e4"}


def box_theme(v=None, kind="", title="", sub=""):
    """{kind, title, sub, search, colour}: a viewer's mailbox, or one given (the leader's and the
    desk's other tabs)."""
    if v is not None:
        team = v.team.name if v.team else ""
        what = (f"{team}'s team leader mailbox" if v.leader_box else f"{v.owner.name}'s own mailbox ({team})"
                if v.member_box else f"{team} inbox")
        if v.desk:
            kind, title, sub = "desk", "Persona desk", "Exercise Control: you write as the personas"
        elif v.desk_person:
            kind, title, sub = "desk", "Persona desk", f"Viewing {what} · read-only"
        elif v.leader:
            kind = "leader"
            if v.readonly:
                title, sub = "Team leader", f"Viewing {what} · read-only"
            else:
                title, sub = "Team leader mailbox", f"{team} · Exercise Control"
        elif v.readonly:
            kind, title, sub = "excon", "ExCon view", f"{what} · read-only"
        elif v.member_box:
            kind, title, sub = "me", "My mail", f"{v.owner.name} · your own, in {team}"
        else:
            kind, title, sub = "team", f"{team} inbox", f"Shared by everyone in {team}"
    search = {"team": title, "me": "My mail", "leader": "mail", "desk": "the desk", "excon": "mail"}.get(kind, "mail")
    return {"kind": kind, "title": title, "sub": sub, "search": search, "colour": BOX_COLOURS.get(kind, "#f6f8fc")}



def sees_sim_only(v):
    """A team member: SIMEX Mail shows them the simulation time only (ops/simclock.py)."""
    return bool(v.person) and not v.leader and not v.desk and not v.desk_person


def _ctx(v, **extra):
    choices = from_choices(v)
    c = {"v": v, "team": DESK if v.desk else v.team, "base": v.base, "readonly": v.readonly, "folders": v.folders,
         "me": v.person, "my_address": my_identity(v)[1], "my_name": my_identity(v)[0], **_counts(v),
         "from_choices": choices,
         # The desk picks the persona every time: a default persona sent mail as the wrong one.
         "from_default": "" if v.desk else "me" if v.member_box else "team"}
    c.update(extra)
    c["box"] = box_theme(v)
    from . import calendar
    c["upcoming"] = calendar.upcoming(v)  # SIMEX Calendar: the side bar's next events
    c["labels"] = [] if v.leader_mode else labels(v)
    folder = c.get("folder", "")
    c["folder_name"] = dict(v.folders).get(folder) or next((lab["name"] for lab in c["labels"] if lab["key"] == folder), "")
    c["label_colours"] = LABEL_COLOURS
    if v.desk:
        c["desk_side"] = desk_side(v)
        flt = desk_filter(v, folder)
        if flt:
            c["folder_name"] = (flt[1].display_name or flt[1].local_part) if flt[0] == "a" else flt[1].name
        elif desk_assigned(v, folder):
            c["folder_name"] = desk_assigned(v, folder)[0]
        c["desk_assignees"] = desk.assignees(v.user)
    if v.desk_person:
        c["desk_tabs"] = desk_tabs(v.desk_person, "desk" if v.desk else "teams", v.user)
        c["back_url"], c["me"] = "/inbox/desk/teams/", v.desk_person  # also on the teams it views
    elif v.desk:  # ExCon staff on the desk: its tabs too
        c["desk_tabs"] = desk_tabs(None, "desk", v.user)
    if v.leader:
        c["leader_tabs"] = leader_tabs(v.leader, "team" if v.leader_mode else "peer" if v.peer
                                       else "members" if v.member_box else "mail")
    elif v.person and not v.readonly:
        c["member_tabs"] = member_tabs(v.person, "me" if v.member_box else "team")
    c["simex_js"] = {"base": v.base, "readonly": v.readonly, "book": contacts(v), "team": c["team"]["name"]
                     if v.desk else v.team.name, "desk": v.desk,
                     "maxMb": int(conf.get("max_upload_mb") or 10), "folder": c.get("folder", ""),
                     "unread": c["inbox_unread"], "leader": bool(v.leader), "fromDefault": c["from_default"],
                     # A team leader's device subscribes for their own mailbox from any of their tabs.
                     "vapid": "" if (v.readonly and not v.leader) or (v.desk and not v.person) else push.public_key(),
                     "pushBase": "/inbox" if v.leader or v.member_box else v.base,
                     "labels": [{"id": lab["id"], "name": lab["name"]} for lab in c["labels"]]}
    return c


def _folder(v, request):
    default = "replies" if v.leader_mode else "inbox"
    folder = request.GET.get("folder", default)
    if folder not in dict(v.folders) and (v.leader_mode or not label_of(v, folder)) and not desk_filter(v, folder) \
            and not desk_assigned(v, folder):
        folder = default
    return folder


@mailbox_view
def inbox(request, v):
    folder = _folder(v, request)
    order = request.GET.get("order")
    if v.desk and order in ("newest", "sections"):
        request.session["desk_order"] = order
        v.desk_newest = order == "newest"
    q = (request.GET.get("q") or "").strip()
    rows = conversations(v, "all" if q and folder in ("inbox", "replies") else folder, q)
    try:
        page = max(1, int(request.GET.get("page") or 1))
    except ValueError:
        page = 1
    total = len(rows)
    start = (page - 1) * PAGE
    _seen(v)
    if q and page == 1:
        last = MailAction.objects.filter(**v.box, person=v.person, action="search").order_by("-at").first()
        if not (last and last.detail.get("q") == q[:200] and last.at > timezone.now() - timedelta(minutes=2)):
            track(v, "search", q=q[:200], folder=folder, results=total)
    sections = None
    if not q and sectioned(v, folder):  # the persona desk: what needs attention first
        counts = Counter(r["state"]["section"] for r in rows)
        sections = {key: {"name": name, "n": counts.get(key, 0)} for key, name in desk.SECTIONS}
    return render(request, "ops/mail/list.html", _ctx(
        v, folder=folder, q=q, rows=rows[start:start + PAGE], sections=sections,
        desk_order=("newest" if v.desk_newest else "sections") if not q and desk_orderable(v, folder) else "", desk_ai=desk_ai.enabled(), total=total, first=start + 1 if total else 0,
        last=min(start + PAGE, total), page=page, has_prev=page > 1, has_next=start + PAGE < total, today=timezone.localdate()))


def ai_band(score):
    """SIMEX AI's 0-100 estimate that a reply was AI-written, as words (an estimate, never proof)."""
    if score is None:
        return ""
    return "Likely AI-assisted" if score >= 70 else "Possibly AI-assisted" if score >= 40 else ""


def _team_notes(mails, team):
    """A leader reading the team inbox: for each mail the team sent, who wrote it, minutes
    since the message it answers, and Claude's reading of it and of its files. Viewer.sees_ai
    only: the team's leader, ExCon."""
    from . import ai
    from .models import ReplyAnalysis
    sent = [m for m in mails if m.folder == "sent"]
    resp = {r.mail_id: r for r in Response.objects.filter(mail__in=sent)}
    if any(m.id not in resp for m in sent):  # a member's own copy: its Response is on the team inbox's
        twins = {}
        for r in Response.objects.filter(team=team, mail__thread__in={m.thread for m in sent}).select_related("mail"):
            twins[(r.mail.thread, r.mail.body, r.mail.author_id)] = r
        for m in sent:
            if m.id not in resp and (m.thread, m.body, m.author_id) in twins:
                resp[m.id] = twins[(m.thread, m.body, m.author_id)]
    tags = {a.response_id: a for a in ReplyAnalysis.objects.filter(response__in=resp.values(), status="done")}
    from .models import DocumentAnalysis
    docs = {d.sha256: d for d in DocumentAnalysis.objects.filter(
        team=team, status="done", sha256__in=[f.sha256 for m in sent for f in m.files.all() if f.sha256])}
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
                  "flags": [ai.FLAGS[f] for f in (a.flags if a else []) if f in ai.FLAGS],
                  "meter_msg": {"pct": a.ai_written, "level": ai_level(a.ai_written), "why": ""}
                  if a and a.ai_written is not None else None,
                  "docs": [{"d": d, "band": ai_band(d.ai_written),
                            "meter": {"pct": d.ai_written, "level": ai_level(d.ai_written), "why": ""}
                            if d.ai_written is not None else None} for d in
                           (docs.get(f.sha256) for f in m.files.all()) if d]}
        # The meter on the message's header: the highest estimate, the message's or a file's.
        scores = ([(a.ai_written, f"Message: {a.ai_signals or 'no signs given'}")] if a and a.ai_written is not None else []) + \
                 [(x["d"].ai_written, f"{x['d'].filename}: {x['d'].ai_signals}") for x in m.note["docs"] if x["d"].ai_written is not None]
        if scores:
            pct, why = max(scores, key=lambda x: x[0])
            m.note["meter"] = {"pct": pct, "level": ai_level(pct), "why": why}


@mailbox_view
def thread_view(request, v, thread):
    folder = _folder(v, request)
    qs = v.mails().filter(thread=thread).exclude(folder="drafts")
    if folder != "trash":
        qs = qs.filter(trashed_at__isnull=True)
    mails = list(qs.select_related("author", "to_person", "in_reply_to", *(["message"] if v.desk else []))
                 .prefetch_related(FILES).order_by("created_at", "id"))
    if not mails:
        raise Http404
    if not v.readonly:
        v.mails().filter(id__in=[m.id for m in mails], folder="inbox", read_at__isnull=True).update(read_at=timezone.now())
        if not v.desk:  # the desk reading isn't a team reading
            inbound.record_read([m for m in mails if m.folder == "inbox" and m.message_id], v.person,
                                request.META.get("HTTP_USER_AGENT", ""))
    if v.sees_ai:
        _team_notes(mails, v.team)
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
    # Reply all: also everyone else on the last message (one we received, as copied to us, too).
    others = last.recipients.get("to", []) + last.recipients.get("cc", []) if last.recipients else []
    mine = {f"m:{v.owner.id}"} if v.member_box else set()
    reply_all = [k for k in dict.fromkeys(reply_to + others) if k not in mine]
    can_reply = True
    if v.leader_box:  # a team leader answers their team only; an inject from a persona can be forwarded
        own = {b["key"] for b in address_book(v)}
        reply_to, reply_all = [k for k in reply_to if k in own], [k for k in reply_all if k in own]
        can_reply = bool(reply_to)
    desk_js = {}
    if v.desk:  # the team on the other side, as the persona of the message
        teams = [f"t:{m.desk_team_id}" for m in mails if m.desk_team_id]
        reply_to = [f"t:{last_in.desk_team_id}"] if last_in and last_in.desk_team_id else teams[-1:]
        reply_all = list(dict.fromkeys(teams))
        can_reply = bool(reply_to)
        desk_js = _desk_thread(mails)
    subject = mails[0].subject
    from . import calendar
    cards = calendar.cards(v, mails)  # SIMEX Calendar: the invitation cards
    for m in mails:
        m.cal = cards.get(m.id)
    memo = {}
    thread_js = {"subject": subject, "thread": thread, "last": last.id,
                 "reply": [k for k in reply_to if k], "replyAll": [k for k in reply_all if k],
                 "msgs": {str(m.id): {"from": m.from_name, "addr": m.from_addr, "body": m.body, "subject": m.subject,
                                      "html": shown_html(v, m),
                                      "date": timezone.localtime(m.sim_at or m.created_at).strftime("%a, %b %-d, %Y at %H:%M"),
                                      "to": m.to_label,
                                      "reply": ([f"t:{m.desk_team_id}"] if m.desk_team_id else []) if v.desk
                                      else [_key_for_sender(m, memo)] if m.folder == "inbox"
                                      else list(m.recipients.get("to", [])),
                                      **({"as": m.desk_as} if v.desk else {})} for m in mails},
                 **desk_js}
    return render(request, "ops/mail/thread.html", _ctx(
        v, folder=folder, mails=mails, subject=subject, thread=thread, prev_t=prev_t, next_t=next_t,
        pos=i + 1, total=len(keys), last=last, thread_js=thread_js, can_reply=can_reply,
        cal_events=calendar.thread_events(v, mails), cal_create=not v.readonly and not v.peer,
        desk_chips=desk_chips(mails, {}, dict(Team.objects.values_list("id", "name"))) if v.desk else None,
        desk_state=desk.states([thread], v.user).get(thread) if v.desk else None,
        desk_conv=desk.conversation(thread) if v.desk else None, desk_ai=v.desk and desk_ai.enabled(),
        desk_watching=v.desk and desk.watching(thread, v.user),
        leader_suggest=v.leader_box and can_reply and suggest_leader.enabled()
        and bool({m.author_id for m in mails if m.folder == "inbox"} & _member_ids(v.team)),
        next_waiting=_next_waiting(rows, thread) if v.desk else None,
        in_inbox=any(m.folder == "inbox" and not m.archived for m in mails), starred=any(m.starred for m in mails),
        thread_labels=[_label_dict(tl.label) for tl in ThreadLabel.objects.filter(label__in=v.labels(), thread=thread)
                       .select_related("label").order_by("label__name")]))


def _member_ids(team):
    return set(team_members(team).values_list("id", flat=True))


def _next_waiting(rows, thread):
    """The next desk conversation that needs attention after this one, if any."""
    return next((r["thread"] for r in rows if r["thread"] != thread and r.get("state")
                 and r["state"]["section"] == desk.ATTENTION), None)


def _desk_thread(mails):
    """The persona desk reading a conversation: each message's persona chip, team, who on the
    desk (or which AI persona) wrote it, and the From each one is answered as (desk_as, the
    Sender's "p:<username>", {team} unfilled); the conversation's default From and its presence URL."""
    memo, choices = {}, {f["key"] for f in desk_from_choices()}
    for m in mails:
        m.desk_chip = desk.describe(m.persona, m.desk_team, memo) if m.persona else None
        s, _ = rnd.sender_by_local(m.persona) if m.persona else (None, None)
        m.desk_as = f"p:{s.local_part}" if s and f"p:{s.local_part}" in choices else ""
        msg = m.message if m.folder == "sent" and m.message_id else None
        m.desk_by = "" if not msg else ("Written by the AI persona" if msg.npc else f"Sent by {m.author.name}"
                                        if m.author else "Inject" if msg.kind in ("live", "test", "oneoff")
                                        else "Follow-up (ExCon)" if msg.kind == "followup" else "Sent by ExCon")
    last_in = next((m for m in reversed(mails) if m.folder == "inbox" and m.desk_as), None)
    default = (last_in or next((m for m in reversed(mails) if m.desk_as), None))
    return {"from": default.desk_as if default else "", "presence": "/inbox/desk/presence/"}


def _key_for_sender(mail, memo=None):
    """The address-book key of who sent mail: a team, a member's own address or a persona.
    memo: a dict kept over one page's messages."""
    memo = {} if memo is None else memo
    if mail.from_addr not in memo:
        t = addresses.team_by_address(mail.from_addr)
        p = None if t else member_by_address(mail.from_addr)
        memo[mail.from_addr] = f"t:{t.id}" if t else f"m:{p.id}" if p else None
    if memo[mail.from_addr]:
        return memo[mail.from_addr]
    return f"p:{mail.persona or mail.from_addr.split('@')[0]}"


@mailbox_view
def lookup(request, v):
    """An address typed in To, Cc or Bcc: who has it, among those the mailbox may write to
    (exactly that address: the list of personas is never shown), or 404. key=: the name and
    address behind a key already on a draft or a reply."""
    q, key = (request.GET.get("q") or "").strip().lower(), (request.GET.get("key") or "").strip()
    book = address_book(v)
    if key:  # a recipient already on a draft or a reply: only its name and address
        hit = (_resolve([key], book, v) or [None])[0]
    else:
        hit = next((b for b in book if q and b["addr"].lower() == q), None) if not v.readonly else None
    if not hit:
        return JsonResponse({"error": "No one in SIMEX Mail has this address."}, status=404)
    return JsonResponse(hit)


@mailbox_view
def setup(request, v):
    """After a QR sign-in: turn on alerts here (on an iPhone, first add SIMEX Mail to the home
    screen: the app then starts at this person's sign-in link, as it doesn't share Safari's)."""
    return render(request, "ops/mail/setup.html", _ctx(v, folder="", app_url=signin.path(request.user) + "?app=1"))


@mailbox_view
def guide(request, v):
    from . import docs
    name = ("DESK_GUIDE.md" if v.desk or v.desk_person else "LEADER_GUIDE.md" if v.leader or v.leader_box
            else "PARTICIPANT_GUIDE.md")
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
        keys = [k for k in request.POST.getlist("draft_key") if _draft_key({"draft_key": k})]
        drafts = v.mails().filter(folder="drafts")
        (drafts.filter(Q(id__in=ids) | Q(draft_key__in=keys)) if ids or keys else drafts.filter(thread__in=threads)).delete()
    if act in ("label_add", "label_remove", "move"):
        names = list(v.labels().filter(pk__in=[int(x) for x in request.POST.getlist("label") + [request.POST.get("to", "")]
                                               if str(x).isdigit()]).values_list("name", flat=True))
        track(v, act, threads, labels=names, to="Inbox" if request.POST.get("to") == "inbox" else "")
    else:
        track(v, act, threads)
    if request.headers.get("x-requested-with") == "fetch":
        return JsonResponse({"ok": True})
    return redirect(request.POST.get("next") if (request.POST.get("next") or "").startswith("/inbox") else "/inbox/")


def _relabel(v, threads, act, ids=(), to="", from_=""):
    """label_add / label_remove the labels ids. move: to a label id or "inbox"; the conversations
    leave the Inbox (or come back to it) and the label being viewed (from_ = "l<id>")."""
    threads = list(v.mails().filter(thread__in=threads).order_by().values_list("thread", flat=True).distinct())
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
    label = None if op == "create" else get_object_or_404(v.labels(), pk=_id(P.get("id")))
    try:
        if op == "create":
            label = MailLabel.objects.create(**v.box, name=_label_name(v, P.get("name")))
            track(v, "label_create", P.getlist("threads"), name=label.name)
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
            track(v, "label_delete", name=label.name)
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
    """Autosave of the compose window (no files: those are attached on send; but the images in
    the text are kept with the draft, so it opens again as it was)."""
    P = request.POST
    key = _draft_key(P)
    d = v.mails().filter(pk=_id(P.get("draft_id")), folder="drafts").first()
    if d is None and key and not _id(P.get("draft_id")):  # its first save's answer never came back
        d = v.mails().filter(folder="drafts", draft_key=key).order_by("id").first()
    if d is None and (_id(P.get("draft_id")) or
                      (key and Mail.objects.filter(draft_key=key).exclude(folder="drafts").exists())):
        return JsonResponse({"error": "This draft was sent or discarded."}, status=410)
    new = d is None
    if new:
        name, addr = my_identity(v)
        d = Mail(**v.box, folder="drafts", author=v.person, from_name=name, from_addr=addr)
    parent = v.mails().filter(pk=_id(P.get("in_reply_to"))).first()
    d.recipients = {"to": _keys(P.get("to")), "cc": _keys(P.get("cc")), "bcc": _keys(P.get("bcc"))}
    if P.get("from") in {f["key"] for f in from_choices(v)}:
        d.recipients["from"] = P["from"]
    d.to_label = _label(_resolve(d.recipients["to"], address_book(v), v))
    d.in_reply_to = parent
    d.forward_of = v.mails().filter(pk=_id(P.get("forward_of"))).first()
    d.subject = P.get("subject") or ""
    d.body, d.html, inline = _composed(request, v, d if not new else None, d.forward_of)
    d.thread = parent.thread if parent else (d.thread or "")
    d.created_at, d.sim_at = timezone.now(), simclock.now()
    d.draft_key = key or d.draft_key
    d.save()
    if d.draft_key:  # two saves of one session that crossed on the way: one draft
        v.mails().filter(folder="drafts", draft_key=d.draft_key).exclude(pk=d.pk).delete()
    have = set(d.files.exclude(cid="").values_list("cid", flat=True)) if not new else set()
    for name, ctype, data, cid in inline:
        if cid not in have:
            MailAttachment.objects.create(mail=d, filename=name, content_type=ctype, size=len(data), data=data, cid=cid)
    d.files.exclude(cid="").exclude(cid__in=[x[3] for x in inline]).delete()
    if new:
        track(v, "draft", [d.thread], draft=d.id)
    shown = d.sim_at if d.sim_at and sees_sim_only(v) else d.created_at
    return JsonResponse({"id": d.id, "key": d.draft_key, "saved": timezone.localtime(shown).strftime("%H:%M")})


def _draft_key(P):
    """The composer's session key (mail.js), or "" if none or not one of ours."""
    key = (P.get("draft_key") or "").strip()
    return key if re.fullmatch(r"[A-Za-z0-9-]{8,40}", key) else ""


def _drop_draft(v, P, sent=None):
    """After a send: its draft (by id, or by its session key if the id never reached the
    composer), and the key kept on the Sent copy so a late autosave makes no new draft."""
    key = _draft_key(P)
    q = Q(pk=_id(P.get("draft_id"))) | Q(draft_key=key) if key else Q(pk=_id(P.get("draft_id")))
    v.mails().filter(q, folder="drafts").delete()
    if key and sent is not None:
        Mail.objects.filter(pk=sent.pk).update(draft_key=key)


@mailbox_view
def draft_get(request, v, mid):
    d = get_object_or_404(v.mails(), pk=mid, folder="drafts")
    return JsonResponse({"id": d.id, "to": d.recipients.get("to", []), "cc": d.recipients.get("cc", []),
                         "bcc": d.recipients.get("bcc", []), "subject": d.subject, "body": d.body, "key": d.draft_key,
                         "html": shown_html(v, d),
                         "in_reply_to": d.in_reply_to_id, "forward_of": d.forward_of_id,
                         "from": d.recipients.get("from", "")})


def shown_html(v, m):
    """A Mail's formatted body for SIMEX Mail (its images from its files), or ""."""
    if not m.html:
        return ""
    return richtext.render(m.html, m.files.all(), lambda f: f"{v.base}/file/{f.id}/?inline=1")


def _composed(request, v, draft=None, fwd=None):
    """(text, html, inline) of the body the composer posted: html its clean HTML ("" when it has
    no formatting: the text says it all), inline the images in it as (filename, content type,
    data, cid), each uploaded now ("inline" files, "inline_cid" their cids) or kept from the
    draft or the mail it forwards; an image with none of those is left out."""
    P = request.POST
    text = (P.get("body") or "").replace("\r\n", "\n").rstrip()
    if not (P.get("html") or "").strip():
        return text, "", []
    found = {}
    for f, cid in zip(request.FILES.getlist("inline"), P.getlist("inline_cid")):
        data = f.read()
        ctype = richtext.image_type(data)
        if ctype and richtext.CID.match(cid or ""):
            ext = ctype.split("/")[1].replace("jpeg", "jpg")
            name = re.sub(r"[\r\n/\\]+", " ", f.name or "")[:200] or "image"
            found[cid] = (name if "." in name else f"{name}.{ext}", ctype, data, cid)
    for m in (draft, fwd):
        for a in (m.files.exclude(cid="") if m is not None else []):
            data = a.content()
            if a.cid not in found and data is not None:
                found[a.cid] = (a.filename, a.content_type, data, a.cid)
    html = richtext.clean(P["html"], image=lambda c: f"cid:{c}" if c in found else None)
    inline = [found[c] for c in richtext.cids(html)]
    text = richtext.to_text(html) or text
    return text, (html if richtext.is_rich(html) else ""), inline


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
    files = request.FILES.getlist("files")
    fwd = v.mails().filter(pk=_id(P.get("forward_of"))).first()
    draft = v.mails().filter(pk=_id(P.get("draft_id")), folder="drafts").first()
    body, html, inline = _composed(request, v, draft, fwd)
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
    if sum(f.size for f in files) + sum(len(x[2]) for x in inline) > limit:
        return fail(f"Attachments and images are over {limit // (1024 * 1024)} MB.")
    parent = v.mails().filter(pk=_id(P.get("in_reply_to"))).first()
    rich = (html, inline)
    if v.desk:
        return _send_desk(request, v, P, to, cc, bcc, subject, body, files, parent, fwd, fail, ajax, rich)
    w = sender_box(v, P.get("from"))  # a member chooses which address it goes from
    if not v.leader_box and not w.member_box and not [e for e in to + cc + bcc if e["key"] != f"t:{v.team.id}"]:
        return fail(f"That is {v.team.name}'s own address: send it from your own address, or add someone else.")
    try:
        with transaction.atomic():  # all or nothing: a failure must not leave a Sent copy to send again
            if v.leader_box:
                sent = _send_as_leader(request, v, P, to, cc, bcc, subject, body, files, parent, fwd, rich)
            else:
                # It continues the conversation as that mailbox has it (the same delivery), if it does.
                twin = (w.mails().filter(thread=parent.thread, message_id=parent.message_id, folder=parent.folder).first()
                        if w is not v and parent and parent.message_id else None)
                sent = _send(request, w, P, to, cc, bcc, subject, body, files, twin or parent, fwd, rich)
                if w is not v and v.member_box:  # from the team's address: in their own Sent as well
                    delivery.copy_mail(sent, owner=v.owner, folder="sent", read_at=sent.read_at, persona=sent.persona,
                                       in_reply_to=parent, forward_of=fwd)
            _drop_draft(v, P, sent)
    except SendFailed as e:
        return fail(str(e))
    started = MailAction.objects.filter(**v.box, action="draft", detail__draft=_id(P.get("draft_id"))).first() \
        if _id(P.get("draft_id")) else None
    track(v, "send", [sent.thread], kind="forward" if fwd else ("reply" if parent else "new"), to=len(to), cc=len(cc),
          bcc=len(bcc), files=len(files), images=len(inline), formatted=bool(html),
          draft_minutes=round((timezone.now() - started.at).total_seconds() / 60, 1) if started else None)
    url = f"{(v if v.mails().filter(thread=sent.thread).exists() else w).base}/t/{sent.thread}/?folder=sent"
    if ajax:
        return JsonResponse({"ok": True, "thread": sent.thread, "url": url})
    messages.success(request, "Message sent.")
    return redirect(url)


def _send_desk(request, v, P, to, cc, bcc, subject, body, files, parent, fwd, fail, ajax, rich=("", [])):
    """The persona desk writes as a persona to one or more teams (desk.send). Each team's
    message goes on its own: one not armed or not deliverable doesn't stop the others. Other
    personas in To or Cc are shown on it (desk.send copies); one in Bcc is left out (the
    desk is every persona's mailbox: it has the message already)."""
    entries = [e for e in to + cc + bcc if e["kind"] != "persona"]
    copies = {"to": [e for e in to if e["kind"] == "persona"], "cc": [e for e in cc if e["kind"] == "persona"]}
    uploads = [(re.sub(r"[\r\n]+", " ", f.name) or "file", f.content_type or "application/octet-stream", f.read())
               for f in files] + list(rich[1])
    docs = []
    for a in (fwd.files.all() if fwd else []):  # forwarded: inject documents by reference, other files copied
        if a.cid:
            continue  # an image in its text: in rich[1] if still in the forward's
        if a.attachment_id:
            docs.append(a.attachment)
        elif a.data is not None:
            uploads.append((a.filename, a.content_type, bytes(a.data)))
    try:
        msgs, errors = desk.send(v.person, request.user.get_username(), P.get("from"), entries, subject, body,
                                 uploads, docs, parent, body_html=rich[0], copies=copies)
    except desk.DeskError as e:
        return fail(str(e))
    sent = v.mails().filter(message__in=msgs, folder="sent").order_by("id").first()
    _drop_draft(v, P, sent)
    url = f"{v.base}/t/{sent.thread}/?folder=sent" if sent else f"{v.base}/?folder=sent"
    warning = f"Not sent to {'; '.join(errors)}" if errors else ""
    if ajax:
        return JsonResponse({"ok": True, "thread": sent.thread if sent else "", "url": url, "warning": warning})
    messages.success(request, f"Message sent. {warning}".strip())
    return redirect(url)


def desk_from_choices():
    """The personas the desk writes as, each with its colour."""
    out = []
    for s in desk.personas():
        bg, fg = desk.colour(s)
        out.append({"key": f"p:{s.local_part}", "name": s.display_name or s.local_part, "addr": s.inapp_address,
                    "colour": bg, "fg": fg})
    return out


def from_choices(v):
    """The addresses a member may send from (the team's and their own), the personas on the
    persona desk, or [] for anyone else."""
    if v.desk:
        return desk_from_choices()
    if v.readonly or v.leader_box or not v.person or v.person.is_leader or not conf.flag("personal_mailboxes"):
        return []
    name = addresses.member_name(v.person, v.team)
    return [{"key": "team", "name": name, "addr": team_address(v.team)},
            {"key": "me", "name": name, "addr": member_address(v.person)}]


def sender_box(v, choice):
    """The Viewer of the mailbox a member's message goes from: the team inbox ("team") or their
    own ("me"); v itself unless they chose the other one."""
    if not from_choices(v) or choice not in ("team", "me") or (choice == "me") == v.member_box:
        return v
    w = copy.copy(v)
    w.owner = v.person if choice == "me" else None
    w.box = {"team": v.team, "owner": w.owner}
    w.base = "/inbox/me" if choice == "me" else "/inbox"
    return w


class SendFailed(Exception):
    pass


def _send(request, v, P, to, cc, bcc, subject, body, files, parent, fwd, rich=("", [])):
    """A member's message, from the team inbox or their own mailbox. To personas it is a
    Response, always on the team inbox's Sent copy (so the team, its leader and ExCon see every
    answer); to teammates, a copy in their own mailbox; to the team (from their own mailbox),
    the team inbox and every member's; to another team, that team's."""
    from_name, from_addr = my_identity(v)
    everyone = to + cc + bcc
    personas = [e["key"][2:] for e in everyone if e["kind"] in ("persona", "leader")]
    sent = Mail.objects.create(
        **v.box, folder="sent", thread=parent.thread if parent else "", from_name=from_name, from_addr=from_addr,
        to_label=_label(to), cc_label=_label(cc), recipients={"to": [e["key"] for e in to], "cc": [e["key"] for e in cc],
                                                             "bcc": [e["key"] for e in bcc]},
        persona=personas[0] if personas else "",
        subject=subject, body=body, html=rich[0], author=v.person, in_reply_to=parent, forward_of=fwd,
        inject=parent.inject if parent else (fwd.inject if fwd else None), is_test=v.team.is_test, read_at=timezone.now())
    for f in files:
        MailAttachment.objects.create(mail=sent, filename=f.name[:300], size=f.size,
                                      content_type=f.content_type or "application/octet-stream", data=f.read())
    for name, ctype, data, cid in rich[1]:
        MailAttachment.objects.create(mail=sent, filename=name, size=len(data), content_type=ctype, data=data, cid=cid)
    if fwd:  # forwarded attachments travel with the message (as references, not copies)
        for a in fwd.files.filter(cid=""):
            MailAttachment.objects.create(mail=sent, attachment=a.attachment, filename=a.filename, size=a.size,
                                          content_type=a.content_type, data=a.data)
    # What the team sees: written from a member's own mailbox to a persona, the team inbox's Sent has it too.
    team_copy = sent
    if personas and v.member_box:
        # It answers the team inbox's copy of what it replies to (the same delivery), if it has one.
        twin = (Mail.objects.filter(team=v.team, owner=None, thread=parent.thread, message_id=parent.message_id,
                                    folder="inbox").first() if parent and parent.message_id else None)
        team_copy = delivery.copy_mail(sent, owner=None, folder="sent", read_at=sent.read_at, in_reply_to=twin or parent)
    if personas:
        inbound.from_mail(team_copy, personas)

    # Written to the team leader persona: the team's leader has it in their Inbox, same conversation.
    lp = rnd.leader_persona(v.team) if personas else None
    lead = v.team.leader() if lp and lp["local"] in personas else None
    if lead:
        copy = delivery.copy_mail(sent, owner=lead, folder="inbox", persona="")
        auto_label(copy, "member" if v.member_box else "team")
        push.notify_mail(copy)
    own_team = f"t:{v.team.id}" in {e["key"] for e in everyone}
    if own_team and v.member_box:  # to the team: its inbox and every other member's own
        copy = delivery.copy_mail(sent, owner=None, folder="inbox", persona="", to_team_id=v.team.id)
        push.notify_mail(copy)
        delivery.to_members(copy, exclude=v.owner.id)
    mates = {int(e["key"][2:]) for e in everyone if e["kind"] == "member"}
    for p in team_members(v.team).filter(pk__in=mates, active=True).exclude(pk=v.person.id if v.person else None):
        if not (own_team and v.member_box):  # else it has a copy already
            push.notify_mail(delivery.copy_mail(sent, owner=p, folder="inbox", persona=""))
    for e in everyone:
        if e["kind"] == "team" and e["key"] != f"t:{v.team.id}":
            t = Team.objects.filter(pk=int(e["key"][2:]), active=True).first()
            if not t or not conf.flag("team_to_team"):
                continue
            copy = delivery.copy_mail(sent, team_id=t.id, owner=None, folder="inbox", persona="", to_team_id=t.id,
                                      thread=_peer_thread(t, v.team, parent), recipients={})
            push.notify_mail(copy)
            delivery.to_members(copy)
            audit("team_to_team", f"{v.team.name} -> {t.name}: {subject[:80]}", request.user.get_username())
    return sent


def _send_as_leader(request, v, P, to, cc, bcc, subject, body, files, parent, fwd, rich=("", [])):
    """A team leader writes as the team leader persona: to their team, a Message delivered to
    the team inbox (and so every member's own mailbox) at once, which also puts it in the
    leader's Sent (delivery._put_mail); to some members only, a Message to those members' own
    mailboxes (delivery.deliver_personal). In reply to a member's message to the leader persona
    it is that Response's answer, as when ExCon answers on the Replies tab."""
    lp = rnd.leader_persona(v.team)
    if not lp:
        raise SendFailed("No persona is set as the team leader yet: ask ExCon (Senders tab).")
    team, thread = v.team, parent.thread if parent else ""
    everyone = to + cc + bcc
    to_team = f"t:{team.id}" in {e["key"] for e in everyone}
    members = list(team_members(team).filter(pk__in=[int(e["key"][2:]) for e in everyone if e["kind"] == "member"],
                                             active=True))
    if not to_team and not members:
        raise SendFailed(f"Write to {team.name} or its members.")
    answers = (Response.objects.filter(mail__team=team, mail__owner=None, mail__thread=thread, persona=lp["local"])
               .order_by("-id").first() if thread else None)
    to_label = team.name if [e["key"] for e in to] == [f"t:{team.id}"] and not cc else _label(to)
    d = rnd.Draft(target=team.name, from_addr=f'"{lp["name"]}" <{lp["addr"]}>', reply_to=None, to=[], cc=[], bcc=[],
                  subject=subject, body=body, body_html=rich[0], footer="",
                  attachment_ids=[a.attachment_id for a in fwd.files.all() if a.attachment_id] if fwd else [],
                  team_id=team.id, reply_local=lp["local"], channel="inapp", inbox_team_id=team.id, to_label=to_label)
    inj = answers.inject if answers else (parent.inject if parent else (fwd.inject if fwd else None))
    m = delivery.new_message(inj, d, "reply" if answers else "leader", f"leader-{v.owner.id}-{uuid.uuid4().hex}")
    m.response, m.author, m.thread = answers, v.owner, thread
    if not to_team:
        m.target = ", ".join(p.name for p in members)[:200]
    m.save()
    for f in files:
        MessageAttachment.objects.create(message=m, filename=f.name[:300], size=f.size,
                                         content_type=f.content_type or "application/octet-stream", data=f.read())
    delivery.save_files(m, rich[1])
    for a in (fwd.files.filter(cid="") if fwd else []):
        if not a.attachment_id and a.data is not None:
            MessageAttachment.objects.create(message=m, filename=a.filename, size=a.size, content_type=a.content_type,
                                             data=a.data)
    recipients = {"to": [e["key"] for e in to], "cc": [e["key"] for e in cc]}
    if to_team:
        delivery.deliver([m], respect_pause=False)  # like any participant's mail, not held by Pause
        sent = v.mails().filter(message=m, folder="sent").first()
        if m.status != "sent" or not sent:
            raise SendFailed(m.error or "The message could not be delivered. Try again.")
        Mail.objects.filter(message=m).update(cc_label=_label(cc), recipients=recipients)
    else:
        sent = delivery.deliver_personal(m, members, _label(to), _label(cc), recipients)
    audit("leader_mail", f"{m.target}: {subject[:80]}", request.user.get_username())
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


# --- the persona desk's other pages ------------------------------------------------------------------

def _desk_page(view):
    @login_required_any
    def wrapper(request, *args, **kwargs):
        if not (desk_of(request.user) or is_excon(request.user)):
            raise PermissionDenied("Persona desk only.")
        return view(request, *args, **kwargs)
    wrapper.__name__ = view.__name__
    return wrapper


@_desk_page
def desk_teams(request):
    """The persona desk's Teams tab: every team's inbox, team leader's and (with personal
    mailboxes) members' mailboxes, read-only, with what is waiting on the desk from each."""
    person = desk_of(request.user)
    tabs = desk_tabs(person, "teams", request.user)
    personal = conf.flag("personal_mailboxes")
    desk_unread = {r["desk_team_id"]: r["n"] for r in Mail.objects.filter(
        team=None, owner=None, folder="inbox", read_at__isnull=True, trashed_at__isnull=True)
        .order_by().values("desk_team_id").annotate(n=Count("thread", distinct=True))}
    for t in tabs["teams"]:
        t.unread = _unread(t, None)
        t.desk_unread = desk_unread.get(t.id, 0)
        t.members = list(team_members(t).filter(active=True)) if personal else []
        t.last_in = (Mail.objects.filter(team=None, owner=None, folder="inbox", desk_team=t)
                     .order_by("-created_at").values_list("created_at", flat=True).first())
    return render(request, "ops/mail/desk_teams.html", {
        "team": DESK, "me": person, "base": "/inbox/desk", "readonly": True, "desk_tabs": tabs, "teams": tabs["teams"],
        "box": box_theme(kind="desk", title="Persona desk", sub="Teams & team leaders · read-only"),
        "personal": personal, "v": None, "back_url": "/inbox/desk/teams/",
        "simex_js": {"base": "/inbox/desk", "readonly": True, "book": [], "team": DESK["name"], "leader": False,
                     "folder": "", "unread": 0, "vapid": "", "pushBase": "/inbox/desk", "labels": []}})


@_desk_page
def desk_dashboard(request):
    """The persona desk's Dashboard tab: how the desk is keeping up (ops/desk_stats.py)."""
    from . import desk_stats
    person = desk_of(request.user)
    have = desk_stats.kinds()
    kind = request.GET.get("kind") or ("test" if have == {"test"} else "live")
    kind = kind if kind in ("live", "test") else "live"
    return render(request, "ops/mail/desk_dashboard.html", {
        "team": DESK, "me": person, "base": "/inbox/desk", "readonly": True, "desk_tabs": desk_tabs(person, "dashboard", request.user),
        "box": box_theme(kind="desk", title="Persona desk", sub="Dashboard"), "v": None, "back_url": "/inbox/desk/",
        "d": desk_stats.compute(kind), "kind": kind, "have": have, "desk_ai": desk_ai.enabled(),
        "simex_js": {"base": "/inbox/desk", "readonly": True, "book": [], "team": DESK["name"], "leader": False,
                     "folder": "", "unread": 0, "vapid": "", "pushBase": "/inbox/desk", "labels": []}})


@_desk_page
@require_POST
def desk_mark(request):
    """A desk member marks conversations (desk.ACTIONS): take or release, no reply needed or
    reopen, follow up later or stop, ask Claude."""
    P = request.POST
    person = desk_of(request.user)
    name = person.name if person else (request.user.get_full_name() or request.user.get_username())
    try:
        minutes = max(0, min(int(P.get("minutes") or 0), 24 * 60))
    except ValueError:
        minutes = 0
    try:
        n = desk.mark(P.getlist("threads"), P.get("act", ""), request.user, name, minutes, P.get("note", ""),
                      P.get("assignee"))
    except desk.DeskError as e:
        if request.headers.get("x-requested-with") == "fetch":
            return JsonResponse({"ok": False, "error": str(e)}, status=400)
        messages.error(request, str(e))
        nxt = P.get("next") or ""
        return redirect(nxt if nxt.startswith("/inbox/desk/") else "/inbox/desk/")
    if request.headers.get("x-requested-with") == "fetch":
        return JsonResponse({"ok": True, "n": n})
    if P.get("act") == "assign" and n:
        messages.success(request, f"Assigned to {desk.user_name(get_user_model().objects.get(pk=P.get('assignee')))}.")
    nxt = P.get("next") or ""
    if P.get("act") == "done":  # on to the next conversation waiting, else back to the Inbox
        nxt = P.get("next_done") or ("/inbox/desk/" if "/t/" in nxt else nxt)
    return redirect(nxt if nxt.startswith("/inbox/desk/") else "/inbox/desk/")


@_desk_page
def desk_notices(request):
    """The bell's list: the signed-in person's desk notices (desk.notify), newest first."""
    from django.utils.timesince import timesince
    now = timezone.now()
    items = [{"id": n.id, "kind": n.kind, "title": n.title, "body": n.body, "url": n.url, "test": n.test,
              "read": n.read_at is not None, "at": timezone.localtime(n.at).strftime("%H:%M"),
              "ago": "just now" if (now - n.at).total_seconds() < 60 else timesince(n.at, now).split(",")[0] + " ago"}
             for n in desk.notices_of(request.user)[:60]]
    return JsonResponse({"items": items, **desk.notice_counts(request.user)})


@_desk_page
@require_POST
def desk_notices_mark(request):
    """Mark the signed-in person's desk notices read or dismiss them (ids, or all)."""
    P = request.POST
    qs = desk.notices_of(request.user)
    if not P.get("all"):
        qs = qs.filter(id__in=[int(x) for x in P.getlist("ids") if x.isdigit()])
    now = timezone.now()
    qs.filter(read_at__isnull=True).update(read_at=now)
    if P.get("act") == "dismiss":
        qs.update(dismissed_at=now)
    return JsonResponse({"ok": True, **desk.notice_counts(request.user)})


@_desk_page
@require_POST
def desk_presence(request):
    """A desk member has a conversation open (replying: writing in it): who else has it now."""
    thread = (request.POST.get("thread") or "")[:60]
    if not Mail.objects.filter(team=None, owner=None, thread=thread).exists():
        raise Http404
    person = desk_of(request.user)
    name = person.name if person else (request.user.get_full_name() or request.user.get_username())
    return JsonResponse({"others": desk.presence(thread, request.user.id, name, request.POST.get("replying") == "1")})


# --- polling & files ------------------------------------------------------------------------------

@login_required_any
def poll(request, as_team=None, box="", pid=None):
    try:
        v = Viewer(request, as_team, box, pid)
    except Http404:
        return JsonResponse({"unread": 0, "latest": 0})
    qs = v.mails().filter(folder="inbox", trashed_at__isnull=True)
    latest = qs.order_by("-id").first()
    try:
        since = int(request.GET.get("since") or 0)
    except ValueError:
        since = 0
    new = [{"id": m.id, "from": m.from_name or m.from_addr, "subject": m.subject, "thread": m.thread,
            "url": f"{v.base}/t/{m.thread}/"}
           for m in (qs.filter(id__gt=since).order_by("-id")[:5] if "since" in request.GET else [])]
    latest_id = latest.id if latest else 0
    if v.person and not v.readonly and not v.member_box and not v.leader and not v.desk \
            and conf.flag("personal_mailboxes"):
        # In the team inbox, a member is told of mail to them personally too (a copy of the team's
        # mail is in both, and announced once, from here).
        mine = Mail.objects.filter(team=v.team, owner=v.person, folder="inbox", trashed_at__isnull=True)
        latest_id = max(latest_id, mine.order_by("-id").values_list("id", flat=True).first() or 0)
        if "since" in request.GET:
            fresh = list(mine.filter(id__gt=since).order_by("-id")[:5])
            copies = set(qs.filter(thread__in=[m.thread for m in fresh]).values_list("thread", "created_at"))
            new += [{"id": m.id, "from": m.from_name or m.from_addr, "subject": m.subject, "thread": m.thread,
                     "url": f"/inbox/me/t/{m.thread}/", "other": True}
                    for m in fresh if (m.thread, m.created_at) not in copies]
            new.sort(key=lambda x: -x["id"])
    out = {"unread": qs.filter(archived=False, read_at__isnull=True).values("thread").distinct().count(),
           "latest": latest_id, "new": new,
           "subject": latest.subject if latest else "", "from": latest.from_name if latest else ""}
    if v.leader:  # the tab bar's badges
        tabs = leader_tabs(v.leader, "")
        out.update(mail_unread=tabs["mail_unread"], team_new=tabs["team_new"])
    elif v.person and not v.readonly and not v.desk and conf.flag("personal_mailboxes"):
        out.update(me_unread=_unread(v.team, v.person), team_unread=_unread(v.team, None))
    if v.desk_person:  # the desk tab's badge
        out.update(desk_unread=_unread(None, None))
    if v.desk or v.desk_person:  # the bell: their own desk notices
        out["dn"] = desk.notice_counts(request.user)
    out["clock"] = simclock.js()  # the simulation clock widget follows ExCon's changes
    try:  # SIMEX Calendar: events about to start (a toast), invitations to answer (the drawer's badge)
        from . import calendar
        out["cal"] = calendar.poll_info(v)
    except Exception:
        log.exception("calendar poll failed")
    return JsonResponse(out)


# --- push notifications & the home-screen app ------------------------------------------------------

# Push services the browsers use; the server only ever posts to these (no arbitrary URLs).
PUSH_HOSTS = (".push.apple.com", "fcm.googleapis.com", ".push.services.mozilla.com", ".notify.windows.com")


@mailbox_view
@require_POST
def push_subscribe(request, v):
    """Save this device's push subscription for the signed-in participant ("off": remove it)."""
    P = request.POST
    if not v.person:  # ExCon staff on the persona desk: no person to alert
        return JsonResponse({"error": "Alerts are for team and desk accounts."}, status=400)
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
    # Signed in: the home-screen app starts at their sign-in link (an iPhone's app has its own cookies).
    start = signin.path(request.user) + "?app=1" if signin.can_use(request.user) else "/inbox/"
    resp = JsonResponse({"name": "SIMEX Mail", "short_name": "SIMEX Mail", "id": "/inbox/", "start_url": start,
                         "scope": "/", "display": "standalone", "background_color": "#f6f8fc",
                         "theme_color": "#f6f8fc", "icons": icons}, content_type="application/manifest+json")
    resp["Cache-Control"] = "private, no-cache"
    return resp


@login_required_any
def mail_file(request, fid, as_team=None, box="", pid=None):
    a = get_object_or_404(MailAttachment.objects.select_related("mail", "attachment", "mail__owner"), pk=fid)
    mail = a.mail
    person, leader = participant_of(request.user), leader_of(request.user)
    mine = (person and mail.team_id == person.team_id and mail.owner_id in (None, person.id)) or \
        (leader and mail.team_id == leader.team_id and mail.owner_id == leader.id)
    # Read-only: a leader's view of their team's inbox, or of one of their members' own mailboxes.
    team_view = leader and mail.team_id == leader.team_id and (mail.owner_id is None or not mail.owner.is_leader)
    peer = leader and mail.owner_id and peer_teams(leader).filter(pk=mail.team_id).first()
    peer_view = peer and mail.owner_id == getattr(peer.leader(), "id", None)  # another leader's mailbox, read-only
    if not (mine or team_view or peer_view or is_excon(request.user) or desk_of(request.user)):
        raise Http404
    data = a.content()
    if data is None:
        raise Http404
    shown = richtext.image_type(data) if a.cid and request.GET.get("inline") else None
    if shown:  # an image in the text, shown where it is (never counted as a download)
        resp = HttpResponse(data, content_type=shown)
        resp["Content-Disposition"] = "inline"
        resp["X-Content-Type-Options"] = "nosniff"
        resp["Content-Security-Policy"] = "default-src 'none'; sandbox"
        resp["Cache-Control"] = "private, max-age=86400"
        return resp
    if mine and mail.folder == "inbox" and mail.message_id:
        inbound.record_read([mail], person or leader, request.META.get("HTTP_USER_AGENT", ""), what="download",
                            link=a.filename)
    resp = HttpResponse(data, content_type="application/octet-stream")  # download only, never rendered inline
    resp["Content-Disposition"] = content_disposition_header(True, re.sub(r"[\r\n]+", " ", a.filename))
    resp["X-Content-Type-Options"] = "nosniff"
    return resp


@login_required_any
@require_POST
def whatsnew_seen(request):
    """What's new (ops/whatsnew.py): the notes this login has now seen (the drawer opened)."""
    from . import whatsnew
    whatsnew.mark_seen(request.user, request.POST.getlist("key"))
    return JsonResponse({"ok": True})


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
