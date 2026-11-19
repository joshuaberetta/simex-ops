"""SIMEX ops web app: inject scheduling and sending (first tab), more tabs later."""

import csv
import hmac
import json
import io
import logging
import mimetypes
import re
import time
import uuid
import zipfile
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func
from starlette.middleware.sessions import SessionMiddleware

from . import backup, config, db, importer, inbound, mailer, render, scheduler, seed, settings
from .db import (DELIVERY_MODES, STATUSES, Attachment, Audit, EmailEvent, Grade, ImportBatch, InboundAttachment, InboundEmail,
                 Inject, Message, Person, RubricCriterion, SessionLocal, Sender, Team, TestRun, TestRunItem, audit,
                 utcnow)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
HERE = Path(__file__).parent
MAX_ATTACHMENT = 30 * 1024 * 1024
MAX_ZIP_TOTAL = 300 * 1024 * 1024

if not config.SECRET_KEY or not config.APP_PASSWORD:
    if config.PUBLIC_URL.startswith("https://"):
        raise SystemExit("SECRET_KEY and APP_PASSWORD must be set in production.")
    logging.warning("SECRET_KEY / APP_PASSWORD not set: using development defaults.")


@asynccontextmanager
async def lifespan(app):
    db.init()
    with SessionLocal() as s:
        seed.seed(s)
    stop = scheduler.start() if config.SCHEDULER_ENABLED else None
    yield
    if stop:
        stop.set()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
templates = Jinja2Templates(directory=HERE / "templates")


# --- time helpers ---------------------------------------------------------------------------

def to_local(dt):
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc).astimezone(config.TZ)


def fmt(dt, pattern="%a %d %b %H:%M"):
    loc = to_local(dt)
    return loc.strftime(pattern) if loc else ""


def from_local_input(value):
    """<input type=datetime-local> value in exercise time -> naive UTC."""
    if not value:
        return None
    local = datetime.fromisoformat(value).replace(tzinfo=config.TZ)
    return local.astimezone(timezone.utc).replace(tzinfo=None)


def local_input(dt):
    return fmt(dt, "%Y-%m-%dT%H:%M") if dt else ""


def ago(dt):
    if not dt:
        return "never"
    secs = int((utcnow() - dt).total_seconds())
    return f"{secs}s ago" if secs < 120 else f"{secs // 60} min ago"


templates.env.filters.update(local=fmt, local_input=local_input, ago=ago)
templates.env.globals.update(TZ=config.TZ_NAME, MAIL_DOMAIN=config.MAIL_DOMAIN, DELIVERY_MODES=DELIVERY_MODES,
                             STATUSES=STATUSES)


# --- auth, CSRF, flash -----------------------------------------------------------------------

OPEN_PATHS = ("/login", "/healthz", "/static/", "/webhooks/")  # webhooks check their own signature
_failed_logins: dict[str, list[float]] = {}


@app.middleware("http")
async def guard(request: Request, call_next):
    path = request.url.path
    if request.method == "POST":
        origin = request.headers.get("origin")
        if origin and origin.split("://", 1)[-1] != request.headers.get("host"):
            return Response("Cross-site request refused.", status_code=403)
    if not path.startswith(OPEN_PATHS) and not request.session.get("user"):
        return RedirectResponse(f"/login?next={path}", status_code=303)
    return await call_next(request)


# Added after guard() so it wraps it: the session must be loaded before guard runs.
app.add_middleware(SessionMiddleware, secret_key=config.SECRET_KEY or "dev-only-secret", session_cookie="simex",
                   max_age=14 * 24 * 3600, same_site="lax", https_only=config.SECURE_COOKIES)


def flash(request, message, kind="ok"):
    request.session.setdefault("flash", []).append([kind, message])


def page(request, name, **ctx):
    with SessionLocal() as s:
        cfg = settings.get_all(s)
        nxt = (s.query(Inject).filter(Inject.status == "scheduled", Inject.send_at.is_not(None))
               .order_by(Inject.send_at).first())
        counts = dict(s.query(Inject.status, func.count()).group_by(Inject.status).all())
        new_replies = s.query(InboundEmail).filter_by(status="new", kind="live").count()
    ctx.update(request=request, cfg=cfg, next_inject=nxt, status_counts=counts, now=utcnow(), new_replies=new_replies,
               sched=scheduler.state, mailer_name=mailer.get().name, flashes=request.session.pop("flash", []),
               tab=ctx.get("tab", ""))
    return templates.TemplateResponse(request, name, ctx)


def back(path):
    return RedirectResponse(path, status_code=303)


@app.get("/healthz")
def healthz():
    return {"ok": True, "last_tick": scheduler.state["last_tick"].isoformat() if scheduler.state["last_tick"] else None}


@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request, next: str = "/"):
    return templates.TemplateResponse(request, "login.html", {"request": request, "next": next, "error": None})


@app.post("/login")
async def login(request: Request):
    form = await request.form()
    ip = request.headers.get("x-forwarded-for", request.client.host if request.client else "").split(",")[0]
    recent = [t for t in _failed_logins.get(ip, []) if t > time.time() - 600]
    nxt = str(form.get("next") or "/")
    if not nxt.startswith("/") or nxt.startswith("//"):
        nxt = "/"
    if len(recent) >= 10:
        return templates.TemplateResponse(request, "login.html", {"request": request, "next": nxt,
                                          "error": "Too many attempts. Try again in a few minutes."}, status_code=429)
    ok_user = hmac.compare_digest(str(form.get("username", "")).strip().lower(), config.APP_USERNAME)
    ok_pass = hmac.compare_digest(str(form.get("password", "")), config.APP_PASSWORD or "nairobi2026")
    if not (ok_user and ok_pass):
        _failed_logins[ip] = recent + [time.time()]
        return templates.TemplateResponse(request, "login.html", {"request": request, "next": nxt,
                                          "error": "Wrong username or password."}, status_code=401)
    request.session.clear()
    request.session["user"] = config.APP_USERNAME
    return back(nxt)


@app.post("/logout")
def logout(request: Request):
    request.session.clear()
    return back("/login")


@app.get("/")
def home():
    return back("/injects")


# --- injects ------------------------------------------------------------------------------

@app.get("/injects", response_class=HTMLResponse)
def injects(request: Request, day: str = "", status: str = "", q: str = ""):
    with SessionLocal() as s:
        rows = s.query(Inject).order_by(Inject.send_at.is_(None), Inject.send_at, Inject.seq).all()
        days = sorted({fmt(i.send_at, "%Y-%m-%d") for i in rows if i.send_at})
        if day:
            rows = [i for i in rows if fmt(i.send_at, "%Y-%m-%d") == day or (day == "none" and not i.send_at)]
        if status:
            rows = [i for i in rows if i.status == status]
        if q:
            ql = q.lower()
            rows = [i for i in rows if ql in " ".join(filter(None, [i.mel_id, i.subject, i.body, i.sender_local])).lower()]
        senders = {x.local_part: x for x in s.query(Sender).all()}
        att_counts = dict(s.query(Attachment.inject_id, func.count()).group_by(Attachment.inject_id).all())
        groups = []
        for i in rows:
            key = fmt(i.send_at, "%A %d %B %Y") if i.send_at else "No send time"
            if not groups or groups[-1][0] != key:
                groups.append((key, []))
            groups[-1][1].append(i)
    return page(request, "injects.html", tab="injects", groups=groups, days=days, day=day, status=status, q=q,
                senders=senders, att_counts=att_counts, total=len(rows))


@app.post("/injects/bulk")
async def injects_bulk(request: Request):
    form = await request.form()
    ids = [int(x) for x in form.getlist("ids")]
    action = form.get("action")
    back_to = str(form.get("back") or "/injects")
    if not back_to.startswith("/injects"):
        back_to = "/injects"
    if not ids:
        flash(request, "Select injects first.", "warn")
        return back(back_to)
    n = 0
    with SessionLocal() as s:
        for inj in s.query(Inject).filter(Inject.id.in_(ids)):
            if action == "schedule" and inj.status in ("draft", "overdue", "cancelled", "failed") and inj.send_at:
                inj.status = "scheduled"
            elif action == "unschedule" and inj.status in ("scheduled", "overdue"):
                inj.status = "draft"
            elif action == "cancel" and inj.status in ("draft", "scheduled", "overdue"):
                inj.status = "cancelled"
            elif action == "shift" and inj.send_at and inj.status not in importer.LOCKED:
                inj.send_at += timedelta(minutes=int(form.get("minutes") or 0))
            else:
                continue
            n += 1
        audit(s, f"bulk_{action}", f"{n} injects" + (f", {form.get('minutes')} min" if action == "shift" else ""))
        s.commit()
    flash(request, f"{action.capitalize()}: {n} of {len(ids)} selected injects changed.")
    return back(back_to)


@app.post("/injects/new")
def inject_new(request: Request):
    with SessionLocal() as s:
        n = s.query(Inject).filter(Inject.mel_id.like("NEW-%")).count() + 1
        while s.query(Inject).filter_by(mel_id=f"NEW-{n}").first():
            n += 1
        inj = Inject(mel_id=f"NEW-{n}", status="draft", delivery="each_team",
                     seq=(s.query(func.max(Inject.seq)).scalar() or 0) + 1)
        s.add(inj)
        audit(s, "inject_new", inj.mel_id)
        s.commit()
        return back(f"/injects/{inj.id}")


def _inject_or_404(s, iid):
    inj = s.get(Inject, iid)
    if not inj:
        raise HTTPException(404)
    return inj


@app.get("/injects/{iid}", response_class=HTMLResponse)
def inject_page(request: Request, iid: int):
    with SessionLocal() as s:
        inj = _inject_or_404(s, iid)
        cfg = settings.get_all(s)
        drafts, warnings = render.build(s, inj, cfg)
        msgs = s.query(Message).filter_by(inject_id=iid).order_by(Message.id.desc()).limit(100).all()
        teams = s.query(Team).order_by(Team.sort, Team.name).all()
        senders = s.query(Sender).order_by(Sender.local_part).all()
        sender = render.sender_for(s, inj)
        atts = list(inj.attachments)
        docs = [d for d in (inj.supporting_documents or "").splitlines() if d.strip()]
        missing_docs = [d for d in docs if not any(_doc_matches(d, a.filename) for a in atts)]
        rubric = s.query(RubricCriterion).filter_by(inject_id=iid).order_by(RubricCriterion.sort, RubricCriterion.id).all()
        reply_count = s.query(InboundEmail).filter_by(inject_id=iid).filter(InboundEmail.status != "ignored").count()
        with_rubric = (s.query(Inject.id, Inject.mel_id).join(RubricCriterion, RubricCriterion.inject_id == Inject.id)
                       .filter(Inject.id != iid).distinct().order_by(Inject.mel_id).all())
    return page(request, "inject.html", tab="injects", inj=inj, drafts=drafts, warnings=warnings, msgs=msgs,
                teams=teams, senders=senders, sender=sender, atts=atts, missing_docs=missing_docs, rubric=rubric,
                reply_count=reply_count, with_rubric=with_rubric)


@app.post("/injects/{iid}")
async def inject_save(request: Request, iid: int):
    form = await request.form()
    with SessionLocal() as s:
        inj = _inject_or_404(s, iid)
        if inj.status == "sending":
            flash(request, "This inject is being sent; edit it when sending has finished.", "warn")
            return back(f"/injects/{iid}")
        mel_id = str(form.get("mel_id") or "").strip()
        if mel_id and mel_id != inj.mel_id:
            if s.query(Inject).filter(Inject.mel_id == mel_id, Inject.id != iid).first():
                flash(request, f"Inject id {mel_id} already exists.", "error")
                return back(f"/injects/{iid}")
            inj.mel_id = mel_id
        try:
            inj.send_at = from_local_input(form.get("send_at"))
        except ValueError:
            flash(request, "Send time not understood.", "error")
            return back(f"/injects/{iid}")
        local = str(form.get("sender_local") or "").strip().lower()
        local = re.sub(r"@.*$", "", local)
        inj.sender_local = local or None
        for f in ("from_name", "reply_to", "to_extra", "cc", "bcc", "subject", "footer", "scenario_dt"):
            v = form.get(f)
            setattr(inj, f, (str(v).strip() or None) if f not in ("footer",) else (str(v) or None))
        inj.body = str(form.get("body") or "").replace("\r\n", "\n") or None
        if form.get("delivery") in DELIVERY_MODES:
            inj.delivery = form.get("delivery")
        if form.get("footer_mode") in ("default", "custom", "none"):
            inj.footer_mode = form.get("footer_mode")
        team_ids = [int(x) for x in form.getlist("team_ids")]
        all_ids = [t.id for t in s.query(Team).filter_by(active=True)]
        inj.team_ids = None if not team_ids or set(all_ids) <= set(team_ids) else team_ids
        if inj.status == "scheduled" and not inj.send_at:
            inj.status = "draft"
        audit(s, "inject_edit", inj.mel_id)
        s.commit()
    flash(request, "Saved.")
    return back(f"/injects/{iid}")


@app.post("/injects/{iid}/status")
async def inject_status(request: Request, iid: int):
    form = await request.form()
    action = form.get("action")
    with SessionLocal() as s:
        inj = _inject_or_404(s, iid)
        if action == "schedule":
            if not inj.send_at:
                flash(request, "Set a send time first.", "error")
            elif inj.send_at < utcnow():
                flash(request, "The send time has passed: change it, or use Send now.", "error")
            elif inj.status in ("draft", "overdue", "cancelled", "failed"):
                inj.status = "scheduled"
        elif action == "unschedule" and inj.status in ("scheduled", "overdue"):
            inj.status = "draft"
        elif action == "cancel" and inj.status in ("draft", "scheduled", "overdue"):
            inj.status = "cancelled"
        elif action == "reset" and inj.status in ("sent", "partial", "failed"):
            inj.status = "draft"  # allows sending again (new attempt)
        audit(s, f"inject_{action}", inj.mel_id)
        s.commit()
    return back(f"/injects/{iid}")


@app.post("/injects/{iid}/send")
async def inject_send(request: Request, iid: int):
    form = await request.form()
    with SessionLocal() as s:
        inj = _inject_or_404(s, iid)
        if form.get("confirm") != inj.mel_id:
            flash(request, f"Type the inject id ({inj.mel_id}) to confirm a live send.", "error")
            return back(f"/injects/{iid}")
        err = scheduler.start_live(s, inj, retry_failed=form.get("retry") == "1")
    flash(request, err or "Sending started. Refresh to see progress.", "error" if err else "ok")
    return back(f"/injects/{iid}")


@app.post("/injects/{iid}/test")
async def inject_test(request: Request, iid: int):
    form = await request.form()
    address = str(form.get("address") or "").strip()
    if not render.EMAIL.match(address):
        flash(request, "Enter a valid test address.", "error")
        return back(f"/injects/{iid}")
    with SessionLocal() as s:
        inj = _inject_or_404(s, iid)
        settings.put(s, "test_address", address)
        s.commit()
        msgs, warnings = scheduler.send_oneoff(s, inj, address, all_targets=form.get("all") == "1")
        if not msgs:
            flash(request, "Nothing to send: " + "; ".join(warnings), "error")
        for m in msgs:
            if m.status == "sent":
                flash(request, f"Test sent to {address} ({m.target}).")
            else:
                flash(request, f"Test to {address} failed: {m.error}", "error")
    return back(f"/injects/{iid}")


@app.post("/injects/{iid}/delete")
async def inject_delete(request: Request, iid: int):
    form = await request.form()
    with SessionLocal() as s:
        inj = _inject_or_404(s, iid)
        if form.get("confirm") != inj.mel_id:
            flash(request, "Type the inject id to confirm deletion.", "error")
            return back(f"/injects/{iid}")
        audit(s, "inject_delete", inj.mel_id)
        s.delete(inj)
        s.commit()
    flash(request, "Inject deleted.")
    return back("/injects")


@app.post("/injects/{iid}/attachments")
async def attachments_upload(request: Request, iid: int):
    form = await request.form()
    n = 0
    with SessionLocal() as s:
        inj = _inject_or_404(s, iid)
        for up in form.getlist("files"):
            if not getattr(up, "filename", None):
                continue
            data = await up.read()
            if len(data) > MAX_ATTACHMENT:
                flash(request, f"{up.filename} is over 30 MB; not added.", "error")
                continue
            _put_attachment(s, inj, PurePosixPath(up.filename).name, data)
            n += 1
        audit(s, "attachments_upload", f"{inj.mel_id}: {n}")
        s.commit()
    flash(request, f"{n} file(s) attached.")
    return back(f"/injects/{iid}")


def _put_attachment(s, inj, filename, data):
    for a in inj.attachments:
        if a.filename == filename:
            a.data, a.size = data, len(data)
            return a
    a = Attachment(filename=filename, data=data, size=len(data),
                   content_type=mimetypes.guess_type(filename)[0] or "application/octet-stream")
    inj.attachments.append(a)
    return a


@app.get("/attachments/{aid}")
def attachment_get(aid: int):
    with SessionLocal() as s:
        a = s.get(Attachment, aid)
        if not a:
            raise HTTPException(404)
        safe = a.filename.replace('"', "'")
        return Response(a.data, media_type=a.content_type,
                        headers={"Content-Disposition": f'attachment; filename="{safe}"'})


@app.post("/attachments/{aid}/delete")
def attachment_delete(request: Request, aid: int):
    with SessionLocal() as s:
        a = s.get(Attachment, aid)
        if not a:
            raise HTTPException(404)
        iid = a.inject_id
        audit(s, "attachment_delete", a.filename)
        s.delete(a)
        s.commit()
    return back(f"/injects/{iid}")


# --- import -----------------------------------------------------------------------------

@app.get("/import", response_class=HTMLResponse)
def import_form(request: Request):
    with SessionLocal() as s:
        batches = s.query(ImportBatch).order_by(ImportBatch.id.desc()).limit(10).all()
        batches = [(b.id, b.filename, b.created_at, b.applied, len(b.payload)) for b in batches]
    return page(request, "import.html", tab="injects", batches=batches)


@app.post("/import", response_class=HTMLResponse)
async def import_upload(request: Request):
    form = await request.form()
    up = form.get("file")
    if not getattr(up, "filename", None):
        flash(request, "Choose a file.", "error")
        return back("/import")
    try:
        records = importer.parse_upload(up.filename, await up.read())
    except importer.ImportError_ as e:
        flash(request, str(e), "error")
        return back("/import")
    with SessionLocal() as s:
        batch = ImportBatch(filename=up.filename, payload=records)
        s.add(batch)
        s.commit()
        rows, missing = importer.plan(s, records)
    summary = {k: sum(1 for r in rows if r["action"] == k) for k in ("new", "update", "conflict", "unchanged")}
    return page(request, "import_plan.html", tab="injects", batch_id=batch.id, filename=up.filename, rows=rows,
                missing=missing, summary=summary)


@app.post("/import/{bid}/apply")
async def import_apply(request: Request, bid: int):
    form = await request.form()
    with SessionLocal() as s:
        batch = s.get(ImportBatch, bid)
        if not batch or batch.applied:
            flash(request, "That import was already applied or has expired.", "error")
            return back("/import")
        counts = importer.apply(s, batch.payload, overwrite_local=form.get("overwrite") == "1")
        batch.applied = True
        s.commit()
    flash(request, f"Imported: {counts['new']} new, {counts['updated']} updated, {counts['kept_local']} local edits kept, "
                   f"{counts['skipped_sent']} already sent (left alone).")
    return back("/injects")


def _doc_matches(doc_line, filename):
    """Does an attachment file name match a 'Supporting documents' line from the MEL?"""
    d, f = doc_line.strip().lower(), filename.lower()
    stem = f.rsplit(".", 1)[0]
    return f == d or stem == d or stem == d.rsplit(".", 1)[0] or (len(d) > 8 and f.startswith(d))


@app.post("/import/attachments")
async def import_attachments(request: Request):
    form = await request.form()
    up = form.get("file")
    if not getattr(up, "filename", None) or not up.filename.lower().endswith(".zip"):
        flash(request, "Upload a .zip of the inject documents.", "error")
        return back("/import")
    try:
        zf = zipfile.ZipFile(io.BytesIO(await up.read()))
    except zipfile.BadZipFile:
        flash(request, "Not a valid zip file.", "error")
        return back("/import")
    entries = [e for e in zf.infolist() if not e.is_dir() and not PurePosixPath(e.filename).name.startswith((".", "~$"))
               and "__MACOSX" not in e.filename]
    if sum(e.file_size for e in entries) > MAX_ZIP_TOTAL:
        flash(request, "Zip contents are over 300 MB.", "error")
        return back("/import")
    attached, used = 0, set()
    with SessionLocal() as s:
        for inj in s.query(Inject).filter(Inject.supporting_documents.is_not(None)):
            if inj.status in importer.LOCKED:
                continue
            for line in inj.supporting_documents.splitlines():
                for e in entries:
                    name = PurePosixPath(e.filename).name
                    if _doc_matches(line, name) and e.file_size <= MAX_ATTACHMENT:
                        _put_attachment(s, inj, name, zf.read(e))
                        used.add(e.filename)
                        attached += 1
                        break
        audit(s, "attachments_zip", f"{up.filename}: {attached} attached")
        s.commit()
    unused = [PurePosixPath(e.filename).name for e in entries if e.filename not in used]
    flash(request, f"{attached} attachment(s) matched to injects.")
    if unused:
        flash(request, f"{len(unused)} file(s) in the zip matched no inject: " + ", ".join(unused[:15])
              + (" ..." if len(unused) > 15 else ""), "warn")
    return back("/injects")


# --- senders ---------------------------------------------------------------------------------

@app.get("/senders", response_class=HTMLResponse)
def senders_page(request: Request):
    with SessionLocal() as s:
        senders = s.query(Sender).order_by(Sender.local_part).all()
        usage = dict(s.query(Inject.sender_local, func.count()).group_by(Inject.sender_local).all())
    return page(request, "senders.html", tab="senders", senders=senders, usage=usage)


@app.post("/senders")
async def senders_save(request: Request):
    form = await request.form()
    sid = form.get("id")
    local = re.sub(r"@.*$", "", str(form.get("local_part") or "").strip().lower())
    if not render.LOCAL.match(local.replace("{team}", "x")):
        flash(request, f"'{local}' is not a valid username (letters, digits, . _ + - and {{team}}).", "error")
        return back("/senders")
    reply_to = str(form.get("reply_to") or "").strip() or None
    if reply_to and not render.EMAIL.match(reply_to):
        flash(request, f"Reply-to '{reply_to}' is not an email address.", "error")
        return back("/senders")
    with SessionLocal() as s:
        clash = s.query(Sender).filter(Sender.local_part == local, Sender.id != int(sid or 0)).first()
        if clash:
            flash(request, f"{local}@{config.MAIL_DOMAIN} already exists.", "error")
            return back("/senders")
        x = s.get(Sender, int(sid)) if sid else Sender()
        old = x.local_part
        x.local_part, x.display_name = local, str(form.get("display_name") or "").strip()
        x.reply_to, x.active = reply_to, form.get("active") == "1" or not sid
        if not sid:
            s.add(x)
        renamed = 0
        if old and old != local and form.get("rename_injects") == "1":
            renamed = s.query(Inject).filter(Inject.sender_local == old).update({Inject.sender_local: local})
        audit(s, "sender_save", f"{local}" + (f" (was {old}, {renamed} injects updated)" if old and old != local else ""))
        s.commit()
    flash(request, f"Saved {local}@{config.MAIL_DOMAIN}." + (f" {renamed} injects now use it." if renamed else ""))
    return back("/senders")


@app.post("/senders/{sid}/delete")
def sender_delete(request: Request, sid: int):
    with SessionLocal() as s:
        x = s.get(Sender, sid)
        if x:
            audit(s, "sender_delete", x.local_part)
            s.delete(x)
            s.commit()
    return back("/senders")


# --- people & teams ---------------------------------------------------------------------------

@app.get("/people", response_class=HTMLResponse)
def people_page(request: Request):
    with SessionLocal() as s:
        teams = s.query(Team).order_by(Team.sort, Team.name).all()
        people = s.query(Person).order_by(Person.team_id.is_(None), Person.team_id, Person.role.desc(), Person.name).all()
        for t in teams:
            _ = t.people
    has_placeholders = any(render.PLACEHOLDER_DOMAIN.search(p.email) for p in people)
    return page(request, "people.html", tab="people", teams=teams, people=people, has_placeholders=has_placeholders)


@app.post("/teams")
async def team_save(request: Request):
    form = await request.form()
    tid = form.get("id")
    code = re.sub(r"[^a-z0-9-]", "", str(form.get("code") or "").strip().lower())
    name = str(form.get("name") or "").strip()
    email = str(form.get("email") or "").strip() or None
    if not code or not name:
        flash(request, "A team needs a code (used in addresses) and a name.", "error")
        return back("/people")
    if email and not render.EMAIL.match(email):
        flash(request, f"'{email}' is not an email address.", "error")
        return back("/people")
    with SessionLocal() as s:
        if s.query(Team).filter(Team.code == code, Team.id != int(tid or 0)).first():
            flash(request, f"Team code '{code}' is taken.", "error")
            return back("/people")
        t = s.get(Team, int(tid)) if tid else Team()
        t.code, t.name, t.email = code, name, email
        t.sort = int(form.get("sort") or 0)
        t.active = form.get("active") == "1" or not tid
        if not tid:
            s.add(t)
        audit(s, "team_save", f"{code} {name}")
        s.commit()
    return back("/people")


@app.post("/teams/{tid}/delete")
def team_delete(request: Request, tid: int):
    with SessionLocal() as s:
        t = s.get(Team, tid)
        if t:
            audit(s, "team_delete", t.name)
            s.delete(t)
            s.commit()
    return back("/people")


@app.post("/people")
async def person_save(request: Request):
    form = await request.form()
    pid = form.get("id")
    name, email = str(form.get("name") or "").strip(), str(form.get("email") or "").strip()
    if not name or not render.EMAIL.match(email):
        flash(request, "A person needs a name and a valid email.", "error")
        return back("/people")
    with SessionLocal() as s:
        p = s.get(Person, int(pid)) if pid else Person()
        p.name, p.email = name, email
        p.team_id = int(form.get("team_id")) if form.get("team_id") else None
        p.role = "tl" if form.get("role") == "tl" else "member"
        p.active = form.get("active") == "1" or not pid
        if not pid:
            s.add(p)
        audit(s, "person_save", f"{name} <{email}>")
        s.commit()
    return back("/people")


@app.post("/people/{pid}/delete")
def person_delete(request: Request, pid: int):
    with SessionLocal() as s:
        p = s.get(Person, pid)
        if p:
            audit(s, "person_delete", p.email)
            s.delete(p)
            s.commit()
    return back("/people")


@app.post("/people/bulk")
async def people_bulk(request: Request):
    """Paste CSV: name,email,team code,role (tl|member). Replaces placeholders if asked."""
    form = await request.form()
    text = str(form.get("csv") or "")
    added, errors = 0, []
    with SessionLocal() as s:
        if form.get("replace_placeholders") == "1":
            s.query(Person).filter(Person.email.like("%placeholder.invalid")).delete(synchronize_session=False)
        teams = {t.code: t for t in s.query(Team)}
        for n, row in enumerate(csv.reader(io.StringIO(text)), 1):
            row = [c.strip() for c in row]
            if not row or not any(row) or row[0].lower() == "name":
                continue
            name, email = row[0], row[1] if len(row) > 1 else ""
            code = row[2].lower() if len(row) > 2 else ""
            role = "tl" if len(row) > 3 and row[3].lower() in ("tl", "team leader", "leader") else "member"
            if not render.EMAIL.match(email):
                errors.append(f"line {n}: bad email '{email}'")
                continue
            if code and code not in teams:
                t = Team(code=re.sub(r"[^a-z0-9-]", "", code), name=f"Team {code.upper()}", sort=len(teams))
                s.add(t)
                s.flush()
                teams[code] = t
            existing = s.query(Person).filter(func.lower(Person.email) == email.lower()).first()
            p = existing or Person()
            p.name, p.email, p.role, p.active = name, email, role, True
            p.team_id = teams[code].id if code else None
            if not existing:
                s.add(p)
            added += 1
        audit(s, "people_bulk", f"{added} rows")
        s.commit()
    flash(request, f"{added} people added or updated.")
    if errors:
        flash(request, "; ".join(errors[:10]), "error")
    return back("/people")


# --- test runs ----------------------------------------------------------------------------------

@app.get("/tests", response_class=HTMLResponse)
def tests_page(request: Request):
    with SessionLocal() as s:
        runs = s.query(TestRun).order_by(TestRun.id.desc()).limit(30).all()
        stats = {}
        for r in runs:
            stats[r.id] = dict(s.query(TestRunItem.status, func.count()).filter_by(run_id=r.id)
                               .group_by(TestRunItem.status).all())
        days = sorted({fmt(i.send_at, "%Y-%m-%d") for i in s.query(Inject).filter(Inject.send_at.is_not(None))})
    default_start = local_input(utcnow() + timedelta(minutes=2))
    return page(request, "tests.html", tab="tests", runs=runs, stats=stats, days=days, default_start=default_start)


@app.post("/tests")
async def tests_create(request: Request):
    form = await request.form()
    redirect_to = str(form.get("redirect") or "").strip()
    probe = redirect_to.replace("{key}", "x")
    if not render.EMAIL.match(probe):
        flash(request, "The test address pattern must be an email address (optionally with {key}).", "error")
        return back("/tests")
    with SessionLocal() as s:
        q = s.query(Inject).filter(Inject.status.notin_(["cancelled"]))
        scope = form.get("scope")
        if scope == "day":
            day = form.get("day")
            chosen = [i for i in q if fmt(i.send_at, "%Y-%m-%d") == day]
        elif scope == "ids":
            want = [x.strip() for x in re.split(r"[,\s]+", str(form.get("ids") or "")) if x.strip()]
            by = {i.mel_id: i for i in q}
            chosen = [by[w] for w in want if w in by]
            missing = [w for w in want if w not in by]
            if missing:
                flash(request, "Not found: " + ", ".join(missing), "warn")
        else:
            chosen = [i for i in q if i.send_at]
        if not chosen:
            flash(request, "No injects selected for the test run.", "error")
            return back("/tests")
        try:
            start = from_local_input(form.get("start")) or utcnow()
        except ValueError:
            start = utcnow()
        factor = interval = None
        if form.get("timing") == "factor":
            factor = max(float(form.get("factor") or 60), 1.0)
        else:
            interval = max(float(form.get("interval") or 30), 5.0)
        name = str(form.get("name") or "").strip() or f"Test {fmt(utcnow(), '%d %b %H:%M')}"
        settings.put(s, "test_redirect", redirect_to)
        run = scheduler.create_test_run(s, name, chosen, start, redirect_to, form.get("dry_run") == "1",
                                        factor=factor, interval=interval)
    flash(request, f"Test run '{name}' created with {len(chosen)} injects.")
    return back(f"/tests/{run.id}")


@app.get("/tests/{rid}", response_class=HTMLResponse)
def test_run_page(request: Request, rid: int):
    with SessionLocal() as s:
        run = s.get(TestRun, rid)
        if not run:
            raise HTTPException(404)
        items = (s.query(TestRunItem).filter_by(run_id=rid).order_by(TestRunItem.due_at).all())
        for i in items:
            _ = i.inject
        msg_counts = dict(s.query(Message.status, func.count()).filter_by(run_id=rid).group_by(Message.status).all())
    return page(request, "test_run.html", tab="tests", run=run, items=items, msg_counts=msg_counts)


@app.post("/tests/{rid}/action")
async def test_run_action(request: Request, rid: int):
    form = await request.form()
    action = form.get("action")
    with SessionLocal() as s:
        run = s.get(TestRun, rid)
        if not run:
            raise HTTPException(404)
        if action == "pause" and run.status == "active":
            run.status = "paused"
        elif action == "resume" and run.status == "paused":
            run.status = "active"
        elif action == "cancel" and run.status in ("active", "paused"):
            run.status = "cancelled"
            s.query(TestRunItem).filter_by(run_id=rid, status="pending").update({TestRunItem.status: "cancelled"})
        elif action == "now" and run.status in ("active", "paused"):
            # Pull every pending item forward so the rest of the run fires on the next ticks.
            now = utcnow()
            pending = s.query(TestRunItem).filter_by(run_id=rid, status="pending").order_by(TestRunItem.due_at).all()
            if pending:
                shift = pending[0].due_at - now
                for it in pending:
                    it.due_at -= shift
        audit(s, f"test_run_{action}", run.name)
        s.commit()
    return back(f"/tests/{rid}")


# --- log & audit ----------------------------------------------------------------------------------

@app.get("/log", response_class=HTMLResponse)
def log_page(request: Request, kind: str = "", status: str = ""):
    with SessionLocal() as s:
        q = s.query(Message).order_by(Message.id.desc())
        if kind:
            q = q.filter(Message.kind == kind)
        if status:
            q = q.filter(Message.status == status)
        msgs = q.limit(300).all()
        for m in msgs:
            _ = m.inject
        audits = s.query(Audit).order_by(Audit.id.desc()).limit(150).all()
    return page(request, "log.html", tab="log", msgs=msgs, audits=audits, kind=kind, status=status)


@app.get("/log/{mid}", response_class=HTMLResponse)
def message_page(request: Request, mid: int):
    with SessionLocal() as s:
        m = s.get(Message, mid)
        if not m:
            raise HTTPException(404)
        _ = m.inject
        atts = s.query(Attachment.id, Attachment.filename).filter(Attachment.id.in_(m.attachment_ids or [])).all()
        events = s.query(EmailEvent).filter_by(message_id=mid).order_by(EmailEvent.at).all()
    return page(request, "message.html", tab="log", m=m, atts=atts, events=events)


# --- settings -------------------------------------------------------------------------------------

EDITABLE = ["footer", "subject_prefix", "default_reply_to", "excon_bcc", "test_address", "test_redirect",
            "grace_minutes"]


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request):
    return page(request, "settings.html", tab="settings", resend_configured=bool(config.RESEND_API_KEY))


@app.post("/settings")
async def settings_save(request: Request):
    form = await request.form()
    with SessionLocal() as s:
        for k in EDITABLE:
            v = str(form.get(k) or "").replace("\r\n", "\n")
            if k in ("default_reply_to", "test_address") and v.strip() and not render.EMAIL.match(v.strip()):
                flash(request, f"{k.replace('_', ' ')}: '{v}' is not an email address; not saved.", "error")
                continue
            if k == "excon_bcc":
                bad = [a for a in render.split_addresses(v) if not render.EMAIL.match(a)]
                if bad:
                    flash(request, f"ExCon Bcc: not email addresses: {', '.join(bad)}; not saved.", "error")
                    continue
            if k == "grace_minutes":
                try:
                    v = str(max(0, int(float(v or 10))))
                except ValueError:
                    v = "10"
            settings.put(s, k, v.strip() if k != "footer" else v)
        audit(s, "settings_save")
        s.commit()
    flash(request, "Settings saved.")
    return back("/settings")


@app.post("/settings/arm")
async def settings_arm(request: Request):
    form = await request.form()
    with SessionLocal() as s:
        if form.get("arm") == "1":
            if form.get("confirm", "").strip().upper() != "SEND LIVE":
                flash(request, "Type SEND LIVE to arm live sending.", "error")
                return back("/settings")
            settings.put(s, "live_armed", "1")
            audit(s, "live_armed")
            flash(request, "Live sending armed: scheduled injects will go to participants.", "warn")
        else:
            settings.put(s, "live_armed", "0")
            audit(s, "live_disarmed")
            flash(request, "Live sending disarmed.")
        s.commit()
    return back(str(form.get("back") or "/settings") if str(form.get("back") or "").startswith("/") else "/settings")


@app.post("/settings/pause")
async def settings_pause(request: Request):
    form = await request.form()
    with SessionLocal() as s:
        paused = form.get("paused") == "1"
        settings.put(s, "paused", "1" if paused else "0")
        audit(s, "paused" if paused else "resumed")
        s.commit()
    flash(request, "All sending paused." if paused else "Sending resumed.", "warn" if paused else "ok")
    nxt = str(form.get("back") or "/settings")
    return back(nxt if nxt.startswith("/") and not nxt.startswith("//") else "/settings")


@app.get("/settings/domain")
def settings_domain():
    try:
        return JSONResponse(mailer.get().domain_status(config.MAIL_DOMAIN))
    except Exception as e:
        return JSONResponse({"error": f"{type(e).__name__}: {e}"})


@app.get("/settings/backup")
def settings_backup():
    with SessionLocal() as s:
        data = backup.export(s)
        audit(s, "backup_download")
        s.commit()
    name = f"simex-ops-backup-{fmt(utcnow(), '%Y%m%d-%H%M')}.json"
    return Response(json.dumps(data), media_type="application/json",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


@app.post("/settings/restore")
async def settings_restore(request: Request):
    form = await request.form()
    up = form.get("file")
    if form.get("confirm") != "RESTORE" or not getattr(up, "filename", None):
        flash(request, "Choose a backup file and type RESTORE.", "error")
        return back("/settings")
    try:
        data = json.loads((await up.read()).decode("utf-8"))
        with SessionLocal() as s:
            counts = backup.restore(s, data)
            s.commit()
    except (ValueError, KeyError, TypeError) as e:
        flash(request, f"Restore failed, nothing changed: {e}", "error")
        return back("/settings")
    flash(request, "Restored: " + ", ".join(f"{v} {k}" for k, v in counts.items()) + ". Live sending is disarmed.", "warn")
    return back("/settings")


# --- replies: webhook, inbox, dashboard, grading -------------------------------------------------

@app.post("/webhooks/resend")
async def resend_webhook(request: Request):
    body = await request.body()
    if not config.RESEND_WEBHOOK_SECRET:
        return Response("Webhook secret not configured.", status_code=503)
    try:
        inbound.verify(config.RESEND_WEBHOOK_SECRET, request.headers, body)
    except inbound.BadSignature as e:
        return Response(f"Bad signature: {e}", status_code=401)
    try:
        event = json.loads(body)
    except ValueError:
        return Response("Bad JSON.", status_code=400)
    with SessionLocal() as s:
        iid = inbound.record_event(s, event)
    if iid:
        inbound.fetch_async(iid)
    return {"ok": True}


def _first_sent(s, kind_filter):
    """(inject id, team id or None) -> earliest sent time of the emails for it."""
    out = {}
    q = s.query(Message.inject_id, Message.team_id, func.min(Message.sent_at)).filter(
        Message.status == "sent", Message.kind.in_(kind_filter)).group_by(Message.inject_id, Message.team_id)
    for iid, tid, at in q:
        out[(iid, tid)] = at
    return out


def _rubric_max(s):
    return dict(s.query(RubricCriterion.inject_id, func.sum(RubricCriterion.max_points))
                .group_by(RubricCriterion.inject_id).all())


def _grade_total(g):
    return sum(float(v) for v in (g.scores or {}).values() if v not in (None, ""))


def _engagement(s, kinds, teams):
    """Opens and clicks of sent emails: per (inject, team), per inject for shared (Bcc /
    custom) emails, and per team. Open tracking undercounts (images blocked) and
    overcounts (Apple Mail prefetches), so it is a signal, not proof of reading."""
    msgs = (s.query(Message).filter(Message.status == "sent", Message.kind.in_(kinds), Message.inject_id.is_not(None))
            .order_by(Message.sent_at).all())
    cells, shared, per_team, inject_ids = {}, {}, {t.id: {"sent": 0, "opened": 0, "clicked": 0} for t in teams}, []
    for m in msgs:
        if m.inject_id not in inject_ids:
            inject_ids.append(m.inject_id)
        bucket = cells.setdefault((m.inject_id, m.team_id), []) if m.team_id else shared.setdefault(m.inject_id, [])
        bucket.append(m)
        if m.team_id in per_team:
            pt = per_team[m.team_id]
            pt["sent"] += 1
            pt["opened"] += bool(m.opened_at)
            pt["clicked"] += bool(m.clicked_at)
    injects = {i.id: i for i in s.query(Inject).filter(Inject.id.in_(inject_ids))} if inject_ids else {}
    rows = [(injects[i], [cells.get((i, t.id), []) for t in teams], shared.get(i, [])) for i in inject_ids if i in injects]
    return rows, per_team


@app.get("/replies", response_class=HTMLResponse)
def replies_page(request: Request, kind: str = "live", team: int = 0, inject: int = 0, status: str = "",
                 matched: str = ""):
    kinds = ["live", "reply"] if kind == "live" else ["test", "oneoff"]
    with SessionLocal() as s:
        teams = s.query(Team).filter_by(active=True).order_by(Team.sort, Team.name).all()
        q = s.query(InboundEmail).filter(InboundEmail.kind == ("live" if kind == "live" else "test"))
        all_in = q.order_by(InboundEmail.received_at).all()
        for e in all_in:
            _ = e.inject, e.team, e.person, e.attachments
        sent = _first_sent(s, kinds)
        maxes = _rubric_max(s)
        grades = {(g.inject_id, g.team_id): g for g in s.query(Grade)}
        rows_ids = {e.inject_id for e in all_in if e.inject_id and e.status != "ignored"}
        rows_ids |= {i for (i,) in s.query(Inject.id).filter(Inject.expects_reply.is_(True))}
        injects = (s.query(Inject).filter(Inject.id.in_(rows_ids)).order_by(Inject.send_at.is_(None), Inject.send_at,
                                                                            Inject.seq).all() if rows_ids else [])
        now = utcnow()
        matrix, team_sum = [], {t.id: {"replies": 0, "answered": 0, "expected": 0, "minutes": [], "score": 0.0,
                                       "max": 0.0, "late": 0} for t in teams}
        for inj in injects:
            cells = []
            for t in teams:
                reps = [e for e in all_in if e.inject_id == inj.id and e.team_id == t.id and e.status != "ignored"]
                sent_at = sent.get((inj.id, t.id)) or sent.get((inj.id, None))
                first = reps[0] if reps else None
                minutes = int((first.received_at - sent_at).total_seconds() // 60) if first and sent_at else None
                g = grades.get((inj.id, t.id))
                due = (sent_at + timedelta(minutes=inj.reply_due_minutes)) if sent_at and inj.reply_due_minutes else None
                if reps:
                    state = "late" if due and first.received_at > due else "replied"
                elif sent_at and inj.expects_reply:
                    state = "missing" if due and now > due else "waiting"
                elif sent_at:
                    state = "none"
                else:
                    state = "unsent"
                cell = {"team": t, "replies": reps, "minutes": minutes, "grade": g, "state": state,
                        "score": _grade_total(g) if g else None, "max": maxes.get(inj.id)}
                cells.append(cell)
                ts = team_sum[t.id]
                ts["replies"] += len(reps)
                if inj.expects_reply and sent_at:
                    ts["expected"] += 1
                    ts["answered"] += bool(reps)
                if minutes is not None:
                    ts["minutes"].append(minutes)
                if state in ("late", "missing"):
                    ts["late"] += 1
                if g and maxes.get(inj.id):
                    ts["score"] += cell["score"]
                    ts["max"] += float(maxes[inj.id])
            matrix.append((inj, cells))
        inbox = [e for e in reversed(all_in)
                 if (not team or e.team_id == team) and (not inject or e.inject_id == inject)
                 and (not status or e.status == status) and (matched != "no" or e.match == "none")]
        new_count = sum(1 for e in all_in if e.status == "new")
        unmatched = sum(1 for e in all_in if e.match == "none" and e.status != "ignored")
        injects_all = s.query(Inject.id, Inject.mel_id).order_by(Inject.seq).all()
        eng_rows, eng_team = _engagement(s, kinds, teams)
    return page(request, "replies.html", eng_rows=eng_rows, eng_team=eng_team, tab="replies", kind=kind, teams=teams, matrix=matrix, team_sum=team_sum,
                inbox=inbox[:300], new_count=new_count, unmatched=unmatched, team=team, inject=inject,
                status=status, matched=matched, injects_all=injects_all,
                webhook_ready=bool(config.RESEND_WEBHOOK_SECRET),
                webhook_url=config.PUBLIC_URL.rstrip("/") + "/webhooks/resend")


@app.post("/replies/sync")
def replies_sync(request: Request):
    try:
        added, fetched = inbound.sync()
        flash(request, f"Sync: {added} new received email(s), {fetched} fetched.")
    except Exception as e:
        flash(request, f"Sync failed: {e}", "error")
    return back("/replies")


@app.get("/replies/export.csv")
def replies_export():
    with SessionLocal() as s:
        teams = {t.id: t for t in s.query(Team)}
        crit = {}
        for c in s.query(RubricCriterion).order_by(RubricCriterion.sort, RubricCriterion.id):
            crit.setdefault(c.inject_id, []).append(c)
        out = io.StringIO()
        w = csv.writer(out)
        w.writerow(["inject", "subject", "team", "criterion", "points", "max", "comment", "grader", "updated (UTC)"])
        for g in s.query(Grade).order_by(Grade.inject_id, Grade.team_id):
            inj = s.get(Inject, g.inject_id)
            team = teams.get(g.team_id)
            for c in crit.get(g.inject_id, []):
                w.writerow([inj.mel_id, inj.subject, team.name if team else g.team_id, c.label,
                            (g.scores or {}).get(str(c.id), ""), c.max_points, g.comment or "", g.grader or "",
                            g.updated_at.isoformat(timespec="minutes")])
    return Response(out.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": 'attachment; filename="simex-grades.csv"'})


@app.get("/replies/{rid}", response_class=HTMLResponse)
def reply_page(request: Request, rid: int):
    with SessionLocal() as s:
        em = s.get(InboundEmail, rid)
        if not em:
            raise HTTPException(404)
        _ = em.inject, em.team, em.person, em.attachments
        if em.status == "new":
            em.status = "reviewed"
            s.commit()
        original = s.get(Message, em.message_id) if em.message_id else None
        minutes = inbound.response_minutes(em, s)
        thread = (s.query(InboundEmail).filter(InboundEmail.inject_id == em.inject_id, InboundEmail.team_id == em.team_id,
                                               InboundEmail.id != em.id).order_by(InboundEmail.received_at).all()
                  if em.inject_id and em.team_id else [])
        answers = s.query(Message).filter_by(inbound_id=em.id).order_by(Message.id).all()
        teams = s.query(Team).order_by(Team.sort, Team.name).all()
        people = s.query(Person).order_by(Person.name).all()
        injects_all = s.query(Inject.id, Inject.mel_id, Inject.subject).order_by(Inject.seq).all()
        sender = s.query(Sender).filter_by(local_part=em.persona).first() if em.persona else None
    new_part, quoted = inbound.split_quoted(em.text or "")
    return page(request, "reply.html", tab="replies", em=em, original=original, minutes=minutes, thread=thread,
                answers=answers, teams=teams, people=people, injects_all=injects_all, new_part=new_part,
                quoted=quoted, sender=sender)


@app.post("/replies/{rid}")
async def reply_update(request: Request, rid: int):
    form = await request.form()
    with SessionLocal() as s:
        em = s.get(InboundEmail, rid)
        if not em:
            raise HTTPException(404)
        if "inject_id" in form:
            em.inject_id = int(form.get("inject_id")) if form.get("inject_id") else None
            em.team_id = int(form.get("team_id")) if form.get("team_id") else None
            em.person_id = int(form.get("person_id")) if form.get("person_id") else None
            em.match = "manual" if em.inject_id else "none"
        if form.get("status") in ("new", "reviewed", "ignored"):
            em.status = form.get("status")
        if form.get("refetch") == "1":
            em.fetched = False
        audit(s, "reply_update", f"#{rid}")
        s.commit()
    if form.get("refetch") == "1":
        inbound.fetch(rid)
    return back(f"/replies/{rid}")


@app.post("/replies/{rid}/answer")
async def reply_answer(request: Request, rid: int):
    """ExCon answers a participant in character, from the persona the email was sent to."""
    form = await request.form()
    body = str(form.get("body") or "").replace("\r\n", "\n").strip()
    if not body:
        flash(request, "Write a reply first.", "error")
        return back(f"/replies/{rid}")
    with SessionLocal() as s:
        if settings.flag(s, "paused"):
            flash(request, "Sending is paused.", "error")
            return back(f"/replies/{rid}")
        em = s.get(InboundEmail, rid)
        if not em or not em.persona:
            raise HTTPException(404)
        sender = s.query(Sender).filter_by(local_part=em.persona).first()
        name = (sender.display_name if sender else em.persona).replace('"', "'")
        cfg = settings.get_all(s)
        footer = cfg.get("footer", "")
        subject = em.subject if em.subject.lower().startswith("re:") else f"Re: {em.subject}"
        quoted = "\n".join("> " + line for line in (em.text or "").splitlines()[:60])
        text_body = body + (f"\n\nOn {fmt(em.received_at)}, {em.from_name or em.from_addr} wrote:\n{quoted}" if quoted else "")
        d = render.Draft(target=em.team.name if em.team else em.from_addr, from_addr=f'"{name}" <{em.persona}@{config.MAIL_DOMAIN}>',
                         reply_to=None, to=[em.from_addr], cc=[], bcc=render.split_addresses(cfg.get("excon_bcc")),
                         subject=subject, body=text_body, footer=footer, attachment_ids=[], team_id=em.team_id,
                         person_id=em.person_id, reply_local=em.persona)
        m = scheduler._message(em.inject or Inject(id=None), d, "reply", f"reply-{em.id}-{uuid.uuid4().hex}")
        m.inject_id, m.inbound_id = em.inject_id, em.id
        if em.message_id_header:
            m.extra_headers = {"In-Reply-To": em.message_id_header, "References": em.message_id_header}
        s.add(m)
        audit(s, "reply_answer", f"#{rid} to {em.from_addr}")
        s.commit()
        scheduler.deliver(s, [m])
        ok = m.status == "sent"
    flash(request, "Reply sent." if ok else f"Reply failed: {m.error}", "ok" if ok else "error")
    return back(f"/replies/{rid}")


@app.get("/inbound-attachments/{aid}")
def inbound_attachment(aid: int):
    with SessionLocal() as s:
        a = s.get(InboundAttachment, aid)
        if not a or a.data is None:
            raise HTTPException(404)
        safe = a.filename.replace('"', "'")
        return Response(a.data, media_type="application/octet-stream",  # never render participant files inline
                        headers={"Content-Disposition": f'attachment; filename="{safe}"'})


@app.get("/grades/{iid}/{tid}", response_class=HTMLResponse)
def grade_page(request: Request, iid: int, tid: int):
    with SessionLocal() as s:
        inj, team = s.get(Inject, iid), s.get(Team, tid)
        if not inj or not team:
            raise HTTPException(404)
        crit = s.query(RubricCriterion).filter_by(inject_id=iid).order_by(RubricCriterion.sort, RubricCriterion.id).all()
        g = s.query(Grade).filter_by(inject_id=iid, team_id=tid).first()
        reps = (s.query(InboundEmail).filter_by(inject_id=iid, team_id=tid).filter(InboundEmail.status != "ignored")
                .order_by(InboundEmail.received_at).all())
        for e in reps:
            _ = e.person, e.attachments
        sent = _first_sent(s, ["live", "reply"])
        sent_at = sent.get((iid, tid)) or sent.get((iid, None))
        parts = [(e, inbound.split_quoted(e.text or "")[0]) for e in reps]
        teams = s.query(Team).filter_by(active=True).order_by(Team.sort, Team.name).all()
    return page(request, "grade.html", tab="replies", inj=inj, team=team, crit=crit, g=g, parts=parts,
                sent_at=sent_at, teams=teams)


@app.post("/grades/{iid}/{tid}")
async def grade_save(request: Request, iid: int, tid: int):
    form = await request.form()
    with SessionLocal() as s:
        crit = s.query(RubricCriterion).filter_by(inject_id=iid).all()
        g = s.query(Grade).filter_by(inject_id=iid, team_id=tid).first() or Grade(inject_id=iid, team_id=tid)
        scores = {}
        for c in crit:
            v = str(form.get(f"c{c.id}") or "").strip()
            if v:
                try:
                    scores[str(c.id)] = max(0.0, min(float(v), float(c.max_points)))
                except ValueError:
                    pass
        g.scores, g.comment = scores, str(form.get("comment") or "").strip() or None
        g.grader = str(form.get("grader") or "").strip() or None
        if not g.id:
            s.add(g)
        audit(s, "grade", f"inject {iid} team {tid}: {sum(scores.values()):g}")
        s.commit()
    flash(request, "Grade saved.")
    nxt = form.get("next")
    return back(f"/grades/{iid}/{nxt}" if nxt else "/replies")


@app.post("/injects/{iid}/rubric")
async def rubric_save(request: Request, iid: int):
    form = await request.form()
    with SessionLocal() as s:
        inj = _inject_or_404(s, iid)
        inj.expects_reply = form.get("expects_reply") == "1"
        try:
            inj.reply_due_minutes = int(form.get("reply_due_minutes")) if form.get("reply_due_minutes") else None
        except ValueError:
            inj.reply_due_minutes = None
        for c in s.query(RubricCriterion).filter_by(inject_id=iid):
            if form.get(f"del{c.id}") == "1":
                s.delete(c)
                continue
            c.label = str(form.get(f"label{c.id}") or c.label).strip()
            c.description = str(form.get(f"desc{c.id}") or "").strip() or None
            c.max_points = max(1, int(form.get(f"max{c.id}") or c.max_points))
            c.sort = int(form.get(f"sort{c.id}") or c.sort)
        label = str(form.get("new_label") or "").strip()
        if label:
            s.add(RubricCriterion(inject_id=iid, label=label, description=str(form.get("new_desc") or "").strip() or None,
                                  max_points=max(1, int(form.get("new_max") or 3)),
                                  sort=s.query(RubricCriterion).filter_by(inject_id=iid).count()))
        src = form.get("copy_from")
        if src:
            for c in s.query(RubricCriterion).filter_by(inject_id=int(src)).order_by(RubricCriterion.sort):
                s.add(RubricCriterion(inject_id=iid, label=c.label, description=c.description,
                                      max_points=c.max_points, sort=c.sort))
        audit(s, "rubric_save", inj.mel_id)
        s.commit()
    flash(request, "Rubric saved.")
    return back(f"/injects/{iid}#rubric")
