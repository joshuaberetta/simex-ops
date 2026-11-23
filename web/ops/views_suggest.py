"""Suggest a reply (ops/suggest.py) for the persona desk and ExCon, and ExCon's Library page
(ops/kb.py). The suggestions' notes may quote role-play scripts and planned injects: every view
here is for the desk (desk_or_excon) or ExCon (excon_required), never participants or leaders."""

from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db.models import Q
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from . import ai, kb, suggest
from .access import desk_of, excon_required, is_excon, login_required_any
from .models import KB_RELEASES, KbDocument, Mail, ReplySuggestion, Response, audit


def desk_or_excon(view):
    @login_required_any
    def wrapper(request, *args, **kwargs):
        if not (desk_of(request.user) or is_excon(request.user)):
            raise PermissionDenied("Persona desk and ExCon only.")
        return view(request, *args, **kwargs)
    wrapper.__name__ = view.__name__
    wrapper.desk_only = True
    return wrapper


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _who(request):
    p = desk_of(request.user)
    return p.name if p else request.user.get_username()


def _target(src):
    """(Response, thread, desk Mail id) the request is about: a desk conversation or a reply."""
    thread = (src.get("thread") or "")[:40]
    if thread:
        if not Mail.objects.filter(team=None, owner=None, thread=thread).exists():
            raise Http404
        r, mail_id = suggest.team_message(thread)
        return r, thread, mail_id
    r = Response.objects.filter(pk=_int(src.get("response"))).first()
    if not r:
        raise Http404
    return r, "", None


def _json(s, mail_id=None):
    return {**suggest.as_json(s), "mail": mail_id}


@desk_or_excon
def suggestion(request):
    """GET: the latest suggestion for a desk conversation (?thread=) or a reply (?response=);
    POST: ask for a new one (prompt: what the desk wants, optional)."""
    src = request.POST if request.method == "POST" else request.GET
    r, thread, mail_id = _target(src)
    suggest.expire()
    if request.method != "POST":
        s = suggest.latest(r, thread) if r else None
        return JsonResponse({"suggestion": _json(s, mail_id) if s else None, "ai": ai.enabled()})
    if r is None:
        return JsonResponse({"error": "No team message to answer in this conversation."}, status=400)
    if not r.team_id:
        return JsonResponse({"error": "This message has no team: match it to a team first (Replies)."}, status=400)
    if not ai.enabled():
        return JsonResponse({"error": "AI analysis is off (Settings → AI analysis)."}, status=400)
    if not ai.under_cap():
        return JsonResponse({"error": "Today's AI spending cap is reached (Settings → AI analysis)."}, status=400)
    running = ReplySuggestion.objects.filter(response=r, status="running").first()
    s = running or suggest.start(r, thread, (src.get("prompt") or "").strip(), _who(request))
    if not running:
        audit("suggest", f"#{s.id} reply {r.id}", _who(request))
    return JsonResponse({"suggestion": _json(s, mail_id), "ai": True})


@desk_or_excon
def suggestion_get(request, sid):
    s = get_object_or_404(ReplySuggestion, pk=sid)
    suggest.expire()
    s.refresh_from_db()
    mail_id = suggest.team_message(s.thread)[1] if s.thread else None
    return JsonResponse({"suggestion": _json(s, mail_id), "ai": ai.enabled()})


# --- the Library (ExCon) -------------------------------------------------------------------------------

@excon_required
def library(request):
    """The participant-facing documents Claude looks things up in: upload the knowledge base's
    export, see and correct whether the participants have each one."""
    if request.method == "POST" and request.FILES.get("file"):
        up = request.FILES["file"]
        try:
            docs = kb.read_bundle(up.read())
        except kb.LibraryError as e:
            messages.error(request, f"{up.name}: {e}")
            return redirect("/library/")
        n = kb.apply(docs, delete_missing=request.POST.get("delete") == "1", user=request.user.get_username())
        messages.success(request, f"Library updated from {up.name}: {n['new']} new, {n['updated']} updated"
                                  + (f", {n['kept']} with your release setting kept" if n["kept"] else "")
                                  + (f", {n['deleted']} removed" if n["deleted"] else "") + ".")
        return redirect("/library/")
    q = (request.GET.get("q") or "").strip()
    rel = request.GET.get("release") or ""
    docs = KbDocument.objects.defer("text")
    if q:
        docs = docs.filter(Q(title__icontains=q) | Q(path__icontains=q) | Q(serial__iexact=q))
    if rel in KB_RELEASES:
        docs = docs.filter(release=rel)
    page = Paginator(docs.order_by("category", "path"), 100).get_page(request.GET.get("page"))
    return render(request, "ops/library.html", {"tab": "injects", "page": page, "q": q, "release": rel,
                                                "RELEASES": KB_RELEASES, "counts": kb.counts()})


@excon_required
@require_POST
def library_doc(request, did):
    d = get_object_or_404(KbDocument, pk=did)
    P = request.POST
    if P.get("delete") == "1":
        d.delete()
        messages.success(request, f"{d.title} removed from the library.")
    else:
        rel, serial = P.get("release"), (P.get("serial") or "").strip()[:40]
        if rel in KB_RELEASES:
            if rel == "with" and not serial:
                messages.error(request, "Say which inject or role play (serial) it comes with.")
            else:
                d.release, d.serial = rel, serial if rel == "with" else d.serial
                d.save(update_fields=["release", "serial", "updated_at"])
                audit("library_release", f"{d.key} {rel} {serial}", request.user.get_username())
    nxt = P.get("next") or "/library/"
    return redirect(nxt if nxt.startswith("/library/") else "/library/")


@excon_required
def library_text(request, did):
    d = get_object_or_404(KbDocument, pk=did)
    return render(request, "ops/library_doc.html", {"tab": "injects", "d": d, "RELEASES": KB_RELEASES})


# ExCon's own routes to the same (the ops host sends /inbox/ GETs to the mail host).
excon_suggestion = excon_required(suggestion)
excon_suggestion_get = excon_required(suggestion_get)
