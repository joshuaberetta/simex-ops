"""Participant views: the team's simulated mail client ("SIMEX Mail", laid out like a webmail
client participants know). Every query is filtered by the logged-in participant's team;
nothing here reads control-side data.

ExCon can open the same pages read-only as any team at /inbox/as/<team id>/ ("view as
team"): exactly what the team sees, without a participant session and without marking
anything read.
"""

import json
import re
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
from .access import is_excon, login_required_any, participant_of
from .models import Mail, MailAttachment, MailLabel, PushSubscription, Sender, Team, ThreadLabel, audit

PAGE = 50
FOLDERS = [("inbox", "Inbox"), ("starred", "Starred"), ("sent", "Sent"), ("drafts", "Drafts"), ("all", "All mail"),
           ("trash", "Trash")]
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
    if participant_of(request.user):
        return redirect("/inbox/")
    logout(request)
    messages.error(request, "This account has no team. Ask ExCon.")
    return redirect("/login/")


def healthz(request):
    return JsonResponse({"ok": True, "scheduler_tick": conf.get("scheduler_tick") or None})


# --- who is looking -------------------------------------------------------------------------------

class Viewer:
    """The team whose mailbox is shown, and whether the viewer may change it."""

    def __init__(self, request, as_team=None):
        self.person = participant_of(request.user)
        if as_team is not None:
            if not is_excon(request.user):
                raise PermissionDenied
            self.team = get_object_or_404(Team, pk=as_team)
            self.readonly, self.person, self.base = True, None, f"/inbox/as/{self.team.id}"
        elif self.person:
            self.team, self.readonly, self.base = self.person.team, False, "/inbox"
        else:
            raise Http404


def mailbox_view(view):
    @login_required_any
    def wrapper(request, *args, as_team=None, **kwargs):
        if as_team is None and not participant_of(request.user) and is_excon(request.user):
            return redirect("/inboxes/")  # ExCon: choose a team to view as
        v = Viewer(request, as_team)
        if request.method == "POST" and v.readonly:
            raise PermissionDenied("Read-only view.")
        return view(request, v, *args, **kwargs)
    wrapper.__name__ = view.__name__
    return wrapper


# --- address book ----------------------------------------------------------------------------------

def address_book(team):
    """Personas ({team} filled with the own team) and, if allowed, the other teams."""
    ctx = {"team": team.code, "team_name": team.name, "name": team.name, "first_name": team.name}
    book = []
    for s in Sender.objects.filter(active=True):
        local = rnd.fill(s.local_part, ctx).lower()
        book.append({"key": f"p:{local}", "name": rnd.fill(s.display_name, ctx) or local,
                     "addr": rnd.persona_address(s, local, ctx), "kind": "persona"})
    if conf.flag("team_to_team"):
        for t in Team.objects.filter(active=True, is_test=False).exclude(pk=team.pk):
            book.append({"key": f"t:{t.id}", "name": t.name, "addr": team_address(t), "kind": "team"})
    return book


def team_address(t):
    return f"{t.code}@teams.{settings.MAIL_DOMAIN}"


def _resolve(keys, book, team):
    """Address-book keys -> entries. Unknown persona keys (e.g. replying to a persona that was
    since retired) are still accepted as long as the username is valid."""
    by = {b["key"]: b for b in book}
    out = []
    for k in keys:
        k = (k or "").strip()
        if not k:
            continue
        if k in by:
            out.append(by[k])
        elif k.startswith("p:") and rnd.LOCAL.match(k[2:]):
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

def label_of(team, folder):
    """The team's MailLabel behind a folder key "l<id>", or None."""
    if folder and folder[0] == "l" and folder[1:].isdigit():
        return MailLabel.objects.filter(team=team, pk=int(folder[1:])).first()
    return None


def _folder_qs(team, folder, q=""):
    qs = Mail.objects.filter(team=team)
    label = label_of(team, folder)
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
        else:  # all
            qs = qs.exclude(folder="drafts")
    if q:
        qs = qs.filter(Q(subject__icontains=q) | Q(body__icontains=q) | Q(from_name__icontains=q)
                       | Q(from_addr__icontains=q) | Q(to_label__icontains=q))
    return qs


# A mail's files without their contents (an inject's documents can be many MB, in every team's inbox).
FILES = Prefetch("files", MailAttachment.objects.defer("data"))


def conversations(team, folder, q=""):
    """Gmail-style conversation rows: one per thread, newest first."""
    mails = list(_folder_qs(team, folder, q).prefetch_related(FILES).order_by("-created_at", "-id")[:2000])
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
    for m in (Mail.objects.filter(team=team, thread__in=order).exclude(folder="drafts")
              .only("id", "thread", "folder", "read_at", "from_name", "from_addr", "to_label", "starred", "trashed_at",
                    "subject")):
        all_in.setdefault(m.thread, []).append(m)
    on, ids_on = {}, {}
    for tl in ThreadLabel.objects.filter(label__team=team, thread__in=order).select_related("label"):
        ids_on.setdefault(tl.thread, []).append(tl.label_id)
        if folder != f"l{tl.label_id}":  # the label being viewed isn't repeated on every row
            on.setdefault(tl.thread, []).append(_label_dict(tl.label))
    rows = []
    for t in order:
        latest = threads[t][0]
        conv = [m for m in all_in.get(t, []) if (folder == "trash") == (m.trashed_at is not None)] or threads[t]
        names = []
        for m in sorted(conv, key=lambda x: x.id):
            n = "me" if m.folder == "sent" else (m.from_name or m.from_addr)
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
    return rows


def _label_dict(label, unread=0):
    return {"id": label.id, "key": f"l{label.id}", "name": label.name, "colour": label.colour,
            "fg": LABEL_COLOURS.get(label.colour, ""), "unread": unread}


def labels(team):
    """The sidebar: the team's labels with their unread conversations."""
    unread = set(Mail.objects.filter(team=team, folder="inbox", read_at__isnull=True, trashed_at__isnull=True)
                 .values_list("thread", flat=True))
    by_label = {}
    for lid, t in ThreadLabel.objects.filter(label__team=team).values_list("label_id", "thread"):
        if t in unread:
            by_label[lid] = by_label.get(lid, 0) + 1
    return sorted((_label_dict(lab, by_label.get(lab.id, 0)) for lab in MailLabel.objects.filter(team=team)),
                  key=lambda x: x["name"].lower())


def _counts(team):
    return {"inbox_unread": _folder_qs(team, "inbox").filter(read_at__isnull=True).values("thread").distinct().count(),
            "drafts": _folder_qs(team, "drafts").count()}


def _ctx(v, **extra):
    c = {"v": v, "team": v.team, "base": v.base, "readonly": v.readonly, "folders": FOLDERS, "me": v.person,
         "my_address": team_address(v.team), **_counts(v.team)}
    c.update(extra)
    c["labels"] = labels(v.team)
    folder = c.get("folder", "")
    c["folder_name"] = dict(FOLDERS).get(folder) or next((lab["name"] for lab in c["labels"] if lab["key"] == folder), "")
    c["label_colours"] = LABEL_COLOURS
    c["simex_js"] = {"base": v.base, "readonly": v.readonly, "book": address_book(v.team), "team": v.team.name,
                     "maxMb": int(conf.get("max_upload_mb") or 10), "folder": c.get("folder", ""),
                     "unread": c["inbox_unread"],
                     "vapid": "" if v.readonly else push.public_key(),
                     "labels": [{"id": lab["id"], "name": lab["name"]} for lab in c["labels"]]}
    return c


@mailbox_view
def inbox(request, v):
    folder = request.GET.get("folder", "inbox")
    if folder not in dict(FOLDERS) and not label_of(v.team, folder):
        folder = "inbox"
    q = (request.GET.get("q") or "").strip()
    rows = conversations(v.team, "all" if q and folder == "inbox" else folder, q)
    try:
        page = max(1, int(request.GET.get("page") or 1))
    except ValueError:
        page = 1
    total = len(rows)
    start = (page - 1) * PAGE
    return render(request, "ops/mail/list.html", _ctx(
        v, folder=folder, q=q, rows=rows[start:start + PAGE], total=total, first=start + 1 if total else 0,
        last=min(start + PAGE, total), page=page, has_prev=page > 1, has_next=start + PAGE < total, today=timezone.localdate()))


@mailbox_view
def thread_view(request, v, thread):
    folder = request.GET.get("folder", "inbox")
    if folder not in dict(FOLDERS) and not label_of(v.team, folder):
        folder = "inbox"
    qs = Mail.objects.filter(team=v.team, thread=thread).exclude(folder="drafts")
    if folder != "trash":
        qs = qs.filter(trashed_at__isnull=True)
    mails = list(qs.select_related("author", "to_person").prefetch_related(FILES).order_by("created_at", "id"))
    if not mails:
        raise Http404
    if not v.readonly:
        Mail.objects.filter(id__in=[m.id for m in mails], folder="inbox", read_at__isnull=True).update(read_at=timezone.now())
        inbound.record_read([m for m in mails if m.folder == "inbox" and m.message_id], v.person,
                            request.META.get("HTTP_USER_AGENT", ""))
    # Neighbours in the folder list, for j/k and the arrows.
    rows = conversations(v.team, folder)
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
    subject = mails[0].subject
    thread_js = {"subject": subject, "thread": thread, "last": last.id,
                 "reply": [k for k in reply_to if k], "replyAll": [k for k in reply_all if k],
                 "msgs": {str(m.id): {"from": m.from_name, "addr": m.from_addr, "body": m.body, "subject": m.subject,
                                      "date": timezone.localtime(m.created_at).strftime("%a, %b %-d, %Y at %H:%M"),
                                      "to": m.to_label, "reply": [_key_for_sender(m)] if m.folder == "inbox"
                                      else list(m.recipients.get("to", []))} for m in mails}}
    return render(request, "ops/mail/thread.html", _ctx(
        v, folder=folder, mails=mails, subject=subject, thread=thread, prev_t=prev_t, next_t=next_t,
        pos=i + 1, total=len(keys), last=last, thread_js=thread_js,
        in_inbox=any(m.folder == "inbox" and not m.archived for m in mails), starred=any(m.starred for m in mails),
        thread_labels=[_label_dict(tl.label) for tl in ThreadLabel.objects.filter(label__team=v.team, thread=thread)
                       .select_related("label").order_by("label__name")]))


def _key_for_sender(mail):
    if mail.from_addr.endswith(f"@teams.{settings.MAIL_DOMAIN}"):
        t = Team.objects.filter(code=mail.from_addr.split("@")[0]).first()
        return f"t:{t.id}" if t else ""
    return f"p:{mail.persona or mail.from_addr.split('@')[0]}"


@mailbox_view
def guide(request, v):
    from . import docs
    return render(request, "ops/mail/guide.html", _ctx(v, folder="", guide=docs.html("PARTICIPANT_GUIDE.md")))


# --- actions ------------------------------------------------------------------------------------

@mailbox_view
@require_POST
def action(request, v):
    threads = request.POST.getlist("threads")
    act = request.POST.get("action")
    qs = Mail.objects.filter(team=v.team, thread__in=threads)
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
        drafts = Mail.objects.filter(team=v.team, folder="drafts")
        (drafts.filter(id__in=ids) if ids else drafts.filter(thread__in=threads)).delete()
    if request.headers.get("x-requested-with") == "fetch":
        return JsonResponse({"ok": True})
    return redirect(request.POST.get("next") if (request.POST.get("next") or "").startswith("/inbox") else "/inbox/")


def _relabel(v, threads, act, ids=(), to="", from_=""):
    """label_add / label_remove the labels ids. move: to a label id or "inbox"; the conversations
    leave the Inbox (or come back to it) and the label being viewed (from_ = "l<id>")."""
    threads = list(Mail.objects.filter(team=v.team, thread__in=threads).values_list("thread", flat=True).distinct())
    if act == "move":
        to = str(to)
        if to != "inbox" and not (to.isdigit() and MailLabel.objects.filter(team=v.team, pk=int(to)).exists()):
            return
        current = label_of(v.team, from_)
        if current and str(current.id) != to:
            ThreadLabel.objects.filter(label=current, thread__in=threads).delete()
        qs = Mail.objects.filter(team=v.team, thread__in=threads, folder="inbox")
        if to == "inbox":
            qs.update(archived=False, trashed_at=None)
            return
        ids = [int(to)]
        qs.update(archived=True)
    for label in MailLabel.objects.filter(team=v.team, pk__in=ids):
        if act == "label_remove":
            ThreadLabel.objects.filter(label=label, thread__in=threads).delete()
        else:
            have = set(label.threads.filter(thread__in=threads).values_list("thread", flat=True))
            ThreadLabel.objects.bulk_create([ThreadLabel(label=label, thread=t) for t in threads if t not in have])


def _label_name(team, name, exclude=None):
    """A clean, unused label name, or raise ValueError with the message to show."""
    name = " ".join((name or "").split())[:60]
    if not name:
        raise ValueError("Enter a label name.")
    if name.lower() in {label.lower() for _, label in FOLDERS} | {"inbox", "spam"}:
        raise ValueError(f"\"{name}\" is already a folder. Choose another name.")
    if MailLabel.objects.filter(team=team, name__iexact=name).exclude(pk=exclude).exists():
        raise ValueError(f"The label \"{name}\" already exists.")
    return name


@mailbox_view
@require_POST
def labels_save(request, v):
    """Create (op=create, optionally applied to threads), rename, recolour or delete a label."""
    P, op = request.POST, request.POST.get("op")
    label = None if op == "create" else get_object_or_404(MailLabel, pk=int(P.get("id") or 0), team=v.team)
    try:
        if op == "create":
            label = MailLabel.objects.create(team=v.team, name=_label_name(v.team, P.get("name")))
            if P.getlist("threads"):
                _relabel(v, P.getlist("threads"), "move" if P.get("move") else "label_add", [label.id], label.id,
                         P.get("from", ""))
        elif op == "rename":
            label.name = _label_name(v.team, P.get("name"), exclude=label.pk)
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
    m = get_object_or_404(Mail, pk=mid, team=v.team)
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
    d = Mail.objects.filter(pk=_id(P.get("draft_id")), team=v.team, folder="drafts").first()
    if d is None and _id(P.get("draft_id")):
        return JsonResponse({"error": "This draft was sent or discarded."}, status=410)
    if d is None:
        d = Mail(team=v.team, folder="drafts", author=v.person, from_name=_from_name(v), from_addr=team_address(v.team))
    parent = Mail.objects.filter(pk=_id(P.get("in_reply_to")), team=v.team).first()
    d.recipients = {"to": _keys(P.get("to")), "cc": _keys(P.get("cc")), "bcc": _keys(P.get("bcc"))}
    book = address_book(v.team)
    d.to_label = _label(_resolve(d.recipients["to"], book, v.team))
    d.subject, d.body = P.get("subject") or "", (P.get("body") or "").replace("\r\n", "\n")
    d.in_reply_to = parent
    d.forward_of = Mail.objects.filter(pk=_id(P.get("forward_of")), team=v.team).first()
    d.thread = parent.thread if parent else (d.thread or "")
    d.created_at = timezone.now()
    d.save()
    return JsonResponse({"id": d.id, "saved": timezone.localtime(d.created_at).strftime("%H:%M")})


@mailbox_view
def draft_get(request, v, mid):
    d = get_object_or_404(Mail, pk=mid, team=v.team, folder="drafts")
    return JsonResponse({"id": d.id, "to": d.recipients.get("to", []), "cc": d.recipients.get("cc", []),
                         "bcc": d.recipients.get("bcc", []), "subject": d.subject, "body": d.body,
                         "in_reply_to": d.in_reply_to_id, "forward_of": d.forward_of_id})


def _id(v):
    """A posted id, or 0 for none / not a number."""
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def _from_name(v):
    return f"{v.person.name} ({v.team.name})" if v.person else v.team.name


@mailbox_view
@require_POST
def send(request, v):
    P = request.POST
    book = address_book(v.team)
    to = _resolve(_keys(P.get("to")), book, v.team)
    cc = _resolve(_keys(P.get("cc")), book, v.team)
    bcc = _resolve(_keys(P.get("bcc")), book, v.team)
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
    parent = Mail.objects.filter(pk=_id(P.get("in_reply_to")), team=v.team).first()
    fwd = Mail.objects.filter(pk=_id(P.get("forward_of")), team=v.team).first()
    with transaction.atomic():  # all or nothing: a failure must not leave a Sent copy to send again
        sent = _send(request, v, P, to, cc, bcc, subject, body, files, parent, fwd)
    if ajax:
        return JsonResponse({"ok": True, "thread": sent.thread, "url": f"{v.base}/t/{sent.thread}/?folder=sent"})
    messages.success(request, "Message sent.")
    return redirect(f"{v.base}/t/{sent.thread}/?folder=sent")


def _send(request, v, P, to, cc, bcc, subject, body, files, parent, fwd):
    from_name, from_addr = _from_name(v), team_address(v.team)
    sent = Mail.objects.create(
        team=v.team, folder="sent", thread=parent.thread if parent else "", from_name=from_name, from_addr=from_addr,
        to_label=_label(to), cc_label=_label(cc), recipients={"to": [e["key"] for e in to], "cc": [e["key"] for e in cc],
                                                             "bcc": [e["key"] for e in bcc]},
        persona=next((e["key"][2:] for e in to + cc + bcc if e["kind"] == "persona"), ""),
        subject=subject, body=body, author=v.person, in_reply_to=parent, forward_of=fwd,
        inject=parent.inject if parent else (fwd.inject if fwd else None), is_test=v.team.is_test, read_at=timezone.now())
    for f in files:
        MailAttachment.objects.create(mail=sent, filename=f.name[:300], size=f.size,
                                      content_type=f.content_type or "application/octet-stream", data=f.read())
    if fwd:  # forwarded attachments travel with the message (as references, not copies)
        for a in fwd.files.all():
            MailAttachment.objects.create(mail=sent, attachment=a.attachment, filename=a.filename, size=a.size,
                                          content_type=a.content_type, data=a.data)
    personas = [e["key"][2:] for e in to + cc + bcc if e["kind"] == "persona"]
    if personas:
        inbound.from_mail(sent, personas)
    for e in to + cc + bcc:
        if e["kind"] == "team":
            t = Team.objects.filter(pk=int(e["key"][2:]), active=True).first()
            if not t or not conf.flag("team_to_team"):
                continue
            visible_to = _label(to)
            copy = Mail.objects.create(team=t, folder="inbox", from_name=from_name, from_addr=from_addr,
                                       to_label=visible_to, cc_label=_label(cc), to_team=t, subject=subject,
                                       body=body, author=v.person, is_test=v.team.is_test,
                                       thread=_peer_thread(t, v.team, parent))
            for a in sent.files.all():
                MailAttachment.objects.create(mail=copy, attachment=a.attachment, filename=a.filename, size=a.size,
                                              content_type=a.content_type, data=a.data)
            push.notify_mail(copy)
            audit("team_to_team", f"{v.team.name} -> {t.name}: {subject[:80]}", request.user.get_username())
    Mail.objects.filter(pk=_id(P.get("draft_id")), team=v.team, folder="drafts").delete()
    return sent


def _peer_thread(team, sender_team, parent):
    """Team-to-team replies continue the other team's copy of the conversation: a mail of the
    same subject between those two teams (never a persona's thread every team has)."""
    if parent and parent.from_addr == team_address(team):
        subject = inbound.SUBJECT_PREFIX.sub("", parent.subject)
        between = Q(from_addr=team_address(sender_team)) | Q(folder="sent", to_label__contains=f"<{team_address(sender_team)}>")
        peer = (Mail.objects.filter(between, team=team, trashed_at__isnull=True)
                .filter(Q(subject__iexact=subject) | Q(subject__iregex=r"^((re|fwd?)\s*:\s*)+" + re.escape(subject) + "$"))
                .order_by("-id").first())
        if peer:
            return peer.thread
    return ""


# --- polling & files ------------------------------------------------------------------------------

@login_required_any
def poll(request, as_team=None):
    try:
        v = Viewer(request, as_team)
    except Http404:
        return JsonResponse({"unread": 0, "latest": 0})
    qs = Mail.objects.filter(team=v.team, folder="inbox", trashed_at__isnull=True)
    latest = qs.order_by("-id").first()
    try:
        since = int(request.GET.get("since") or 0)
    except ValueError:
        since = 0
    new = [{"id": m.id, "from": m.from_name or m.from_addr, "subject": m.subject, "thread": m.thread}
           for m in (qs.filter(id__gt=since).order_by("-id")[:5] if "since" in request.GET else [])]
    return JsonResponse({"unread": qs.filter(archived=False, read_at__isnull=True).values("thread").distinct().count(),
                         "latest": latest.id if latest else 0, "new": new,
                         "subject": latest.subject if latest else "", "from": latest.from_name if latest else ""})


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
def mail_file(request, fid, as_team=None):
    a = get_object_or_404(MailAttachment.objects.select_related("mail", "attachment"), pk=fid)
    person = participant_of(request.user)
    if not is_excon(request.user) and not (person and a.mail.team_id == person.team_id):
        raise Http404
    data = a.content()
    if data is None:
        raise Http404
    if person and a.mail.team_id == person.team_id and a.mail.folder == "inbox" and a.mail.message_id:
        inbound.record_read([a.mail], person, request.META.get("HTTP_USER_AGENT", ""), what="download", link=a.filename)
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
