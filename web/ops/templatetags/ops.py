import hashlib

from django import template
from django.utils import timezone
from django.utils.html import format_html
from django.utils.safestring import mark_safe

from ops import render as rnd

register = template.Library()


@register.filter
def get(d, key):
    try:
        return d.get(key, "")
    except AttributeError:
        return ""


@register.filter
def waited(dt):
    """How long since dt, short: "4 min", "1 h 05", "2 d"."""
    if not dt:
        return ""
    mins = max(0, int((timezone.now() - dt).total_seconds() // 60))
    if mins < 60:
        return f"{mins} min"
    if mins < 24 * 60:
        return f"{mins // 60} h {mins % 60:02d}"
    return f"{mins // (24 * 60)} d"


@register.filter
def kb(n):
    n = n or 0
    return f"{n / 1024 / 1024:.1f} MB" if n > 1024 * 1024 else f"{max(1, round(n / 1024))} KB"


@register.filter
def num(v):
    return "" if v is None else f"{float(v):g}"


@register.filter
def mailbody(text):
    """Plain message text -> safe HTML paragraphs with links (as in emails)."""
    return mark_safe(rnd.text_to_html(text or "")) if (text or "").strip() else ""


@register.filter
def joinlist(v, sep=", "):
    if isinstance(v, str):
        return v
    return sep.join(str(x) for x in (v or []))


@register.filter
def status_class(v):
    return {"new": "pending", "ignored": "cancelled", "reviewed": "sent"}.get(v, v)


# --- mail client helpers ------------------------------------------------------------------------

AVATAR_COLOURS = ["#1a73e8", "#d93025", "#188038", "#e37400", "#9334e6", "#007b83", "#c5221f", "#5f6368",
                  "#b06000", "#1967d2", "#a142f4", "#12b5cb"]


@register.filter
def avatar_colour(name):
    h = int(hashlib.md5((name or "?").encode()).hexdigest(), 16)
    return AVATAR_COLOURS[h % len(AVATAR_COLOURS)]


@register.filter
def initial(name):
    name = (name or "?").strip().lstrip('"')
    return (name[:1] or "?").upper()


def _maildate(dt, ref=None):
    loc = timezone.localtime(dt)
    today = timezone.localtime(ref).date() if ref else timezone.localdate()
    if loc.date() == today:
        return loc.strftime("%H:%M")
    if loc.year == today.year:
        return loc.strftime("%b %-d")
    return loc.strftime("%d/%m/%Y")


def _fulldate(dt, ref=None):
    loc = timezone.localtime(dt)
    secs = int(((ref or timezone.now()) - dt).total_seconds())
    if secs < 0:
        rel = ""
    elif secs < 60:
        rel = "just now"
    elif secs < 3600:
        rel = f"{secs // 60} minute{'s' if secs // 60 != 1 else ''} ago"
    elif secs < 86400:
        rel = f"{secs // 3600} hour{'s' if secs // 3600 != 1 else ''} ago"
    else:
        rel = f"{secs // 86400} day{'s' if secs // 86400 != 1 else ''} ago"
    year = f", {loc.year}" if ref and loc.year != timezone.localtime(ref).year else ""
    return f"{loc.strftime('%a, %b %-d')}{year}, {loc.strftime('%H:%M')}" + (f" ({rel})" if rel else "")


@register.filter
def maildate(dt):
    """Gmail list style: 14:05 today, Nov 23 this year, 23/11/2034 otherwise."""
    return _maildate(dt) if dt else ""


@register.filter
def fulldate(dt):
    """Thread header style: Mon, Nov 23, 14:05 (2 hours ago)."""
    return _fulldate(dt) if dt else ""


# --- the simulation clock (ops/simclock.py): context "sim" from views_inbox.sim_context ------------

def _sim(context, m):
    """(sim on for this mail, show the real time too, the simulation time now)."""
    sim = context.get("sim") or {}
    return bool(sim.get("on") and m.sim_at), bool(sim.get("both")), sim.get("now")


def simlong(dt):
    return timezone.localtime(dt).strftime("%a, %b %-d, %Y, %H:%M") if dt else ""


@register.simple_tag(takes_context=True)
def listdate(context, m):
    """A list row's date: the simulation date for participants; for leaders, the desk and
    ExCon the real one with the simulation one under it."""
    on, both, ref = _sim(context, m)
    if not on:
        return _maildate(m.created_at)
    if not both:
        return format_html('<span title="{}">{}</span>', simlong(m.sim_at), _maildate(m.sim_at, ref))
    return format_html('{}<small class="gm-simdate" title="Simulation time: {}">{} {}</small>', _maildate(m.created_at),
                       simlong(m.sim_at), mark_safe(icon("clock", 12)), _maildate(m.sim_at, ref))


@register.simple_tag(takes_context=True)
def msgdate(context, m):
    """A message's date in a conversation (as listdate)."""
    on, both, ref = _sim(context, m)
    if not on:
        return format_html('<span title="{}">{}</span>', timezone.localtime(m.created_at).strftime("%a %d %b %Y %H:%M"),
                           _fulldate(m.created_at))
    if not both:
        return format_html('<span title="{}">{}</span>', simlong(m.sim_at), _fulldate(m.sim_at, ref))
    return format_html('<span title="Real time">{}</span><span class="gm-simdate" title="Simulation time">{} {}</span>',
                       _fulldate(m.created_at), mark_safe(icon("clock", 14)), _fulldate(m.sim_at, ref))


@register.simple_tag(takes_context=True)
def detaildates(context, m):
    """The date rows of a message's details."""
    on, both, _ = _sim(context, m)
    real = timezone.localtime(m.created_at).strftime("%b %-d, %Y, %H:%M")
    if not on:
        return format_html("<tr><th>date:</th><td>{}</td></tr>", real)
    sim = timezone.localtime(m.sim_at).strftime("%b %-d, %Y, %H:%M")
    if not both:
        return format_html("<tr><th>date:</th><td>{}</td></tr>", sim)
    return format_html("<tr><th>date:</th><td>{}</td></tr><tr><th>simulation:</th><td>{}</td></tr>", real, sim)


@register.filter
def snippet(text, n=110):
    t = " ".join((text or "").split())
    return t[:n] + ("…" if len(t) > n else "")


@register.filter
def name_only(label):
    """'CNN <cnn@x>' -> 'CNN'."""
    return (label or "").split(" <")[0].strip('"')


@register.filter
def domain(addr):
    return (addr or "").rsplit("@", 1)[-1] if "@" in (addr or "") else ""


@register.filter
def addr_only(label):
    return (label or "").split("<")[-1].rstrip(">") if "<" in (label or "") else (label or "")


@register.filter
def file_kind(name):
    ext = (name or "").rsplit(".", 1)[-1].lower() if "." in (name or "") else ""
    return {"pdf": "pdf", "doc": "doc", "docx": "doc", "odt": "doc", "xls": "xls", "xlsx": "xls", "csv": "xls",
            "ppt": "ppt", "pptx": "ppt", "ppsx": "ppt", "png": "img", "jpg": "img", "jpeg": "img", "gif": "img",
            "zip": "zip"}.get(ext, "file")


# Material Design icon paths (Apache 2.0), inlined so nothing loads from a CDN.
ICONS = {
    "done": "M9 16.17L4.83 12l-1.42 1.41L9 19 21 7l-1.41-1.41z",
    "menu": "M3 18h18v-2H3v2zm0-5h18v-2H3v2zm0-7v2h18V6H3z",
    "search": "M15.5 14h-.79l-.28-.27A6.471 6.471 0 0 0 16 9.5 6.5 6.5 0 1 0 9.5 16c1.61 0 3.09-.59 4.23-1.57l.27.28v.79l5 4.99L20.49 19l-4.99-5zm-6 0C7.01 14 5 11.99 5 9.5S7.01 5 9.5 5 14 7.01 14 9.5 11.99 14 9.5 14z",
    "edit": "M3 17.25V21h3.75L17.81 9.94l-3.75-3.75L3 17.25zM20.71 7.04a.996.996 0 0 0 0-1.41l-2.34-2.34a.996.996 0 0 0-1.41 0l-1.83 1.83 3.75 3.75 1.83-1.83z",
    "inbox": "M19 3H4.99c-1.11 0-1.98.89-1.98 2L3 19c0 1.1.88 2 1.99 2H19c1.1 0 2-.9 2-2V5c0-1.11-.9-2-2-2zm0 12h-4c0 1.66-1.35 3-3 3s-3-1.34-3-3H4.99V5H19v10z",
    "star": "M12 17.27L18.18 21l-1.64-7.03L22 9.24l-7.19-.61L12 2 9.19 8.63 2 9.24l5.46 4.73L5.82 21z",
    "star_border": "M22 9.24l-7.19-.62L12 2 9.19 8.63 2 9.24l5.46 4.73L5.82 21 12 17.27 18.18 21l-1.63-7.03L22 9.24zM12 15.4l-3.76 2.27 1-4.28-3.32-2.88 4.38-.38L12 6.1l1.71 4.04 4.38.38-3.32 2.88 1 4.28L12 15.4z",
    "send": "M2.01 21L23 12 2.01 3 2 10l15 2-15 2z",
    "draft": "M6 2c-1.1 0-1.99.9-1.99 2L4 20c0 1.1.89 2 1.99 2H18c1.1 0 2-.9 2-2V8l-6-6H6zm7 7V3.5L18.5 9H13z",
    "all": "M20 4H4c-1.1 0-1.99.9-1.99 2L2 18c0 1.1.9 2 2 2h16c1.1 0 2-.9 2-2V6c0-1.1-.9-2-2-2zm0 4l-8 5-8-5V6l8 5 8-5v2z",
    "mail": "M20 4H4c-1.1 0-1.99.9-1.99 2L2 18c0 1.1.9 2 2 2h16c1.1 0 2-.9 2-2V6c0-1.1-.9-2-2-2zm0 4l-8 5-8-5V6l8 5 8-5v2z",
    "trash": "M6 19c0 1.1.9 2 2 2h8c1.1 0 2-.9 2-2V7H6v12zM19 4h-3.5l-1-1h-5l-1 1H5v2h14V4z",
    "archive": "M20.54 5.23l-1.39-1.68C18.88 3.21 18.47 3 18 3H6c-.47 0-.88.21-1.16.55L3.46 5.23C3.17 5.57 3 6.02 3 6.5V19c0 1.1.9 2 2 2h14c1.1 0 2-.9 2-2V6.5c0-.48-.17-.93-.46-1.27zM12 17.5L6.5 12H10v-2h4v2h3.5L12 17.5zM5.12 5l.81-1h12l.94 1H5.12z",
    "unarchive": "M20.55 5.22l-1.39-1.68C18.88 3.21 18.47 3 18 3H6c-.47 0-.88.21-1.15.55L3.46 5.22C3.17 5.57 3 6.01 3 6.5V19c0 1.1.89 2 2 2h14c1.1 0 2-.9 2-2V6.5c0-.49-.17-.93-.45-1.28zM12 9.5l5.5 5.5H14v2h-4v-2H6.5L12 9.5zM5.12 5l.82-1h12l.93 1H5.12z",
    "read": "M21.99 8c0-.72-.37-1.35-.94-1.7L12 1 2.95 6.3C2.38 6.65 2 7.28 2 8v10c0 1.1.9 2 2 2h16c1.1 0 2-.9 2-2l-.01-10zM12 13L3.74 7.84 12 3l8.26 4.84L12 13z",
    "unread": "M20 4H4c-1.1 0-1.99.9-1.99 2L2 18c0 1.1.9 2 2 2h16c1.1 0 2-.9 2-2V6c0-1.1-.9-2-2-2zm0 4l-8 5-8-5V6l8 5 8-5v2z",
    "refresh": "M17.65 6.35A7.958 7.958 0 0 0 12 4c-4.42 0-7.99 3.58-7.99 8s3.57 8 7.99 8c3.73 0 6.84-2.55 7.73-6h-2.08A5.99 5.99 0 0 1 12 18c-3.31 0-6-2.69-6-6s2.69-6 6-6c1.66 0 3.14.69 4.22 1.78L13 11h7V4l-2.35 2.35z",
    "more": "M12 8c1.1 0 2-.9 2-2s-.9-2-2-2-2 .9-2 2 .9 2 2 2zm0 2c-1.1 0-2 .9-2 2s.9 2 2 2 2-.9 2-2-.9-2-2-2zm0 6c-1.1 0-2 .9-2 2s.9 2 2 2 2-.9 2-2-.9-2-2-2z",
    "attach": "M16.5 6v11.5c0 2.21-1.79 4-4 4s-4-1.79-4-4V5a2.5 2.5 0 0 1 5 0v10.5c0 .55-.45 1-1 1s-1-.45-1-1V6H10v9.5a2.5 2.5 0 0 0 5 0V5c0-2.21-1.79-4-4-4S7 2.79 7 5v12.5c0 3.04 2.46 5.5 5.5 5.5s5.5-2.46 5.5-5.5V6h-1.5z",
    "reply": "M10 9V5l-7 7 7 7v-4.1c5 0 8.5 1.6 11 5.1-1-5-4-10-11-11z",
    "reply_all": "M7 8V5l-7 7 7 7v-3l-4-4 4-4zm6 1V5l-7 7 7 7v-4.1c5 0 8.5 1.6 11 5.1-1-5-4-10-11-11z",
    "forward": "M12 8V4l8 8-8 8v-4H4V8z",
    "close": "M19 6.41L17.59 5 12 10.59 6.41 5 5 6.41 10.59 12 5 17.59 6.41 19 12 13.41 17.59 19 19 17.59 13.41 12z",
    "minimize": "M6 19h12v2H6z",
    "expand": "M21 11V3h-8l3.29 3.29-10 10L3 13v8h8l-3.29-3.29 10-10z",
    "back": "M20 11H7.83l5.59-5.59L12 4l-8 8 8 8 1.41-1.41L7.83 13H20v-2z",
    "prev": "M15.41 7.41L14 6l-6 6 6 6 1.41-1.41L10.83 12z",
    "next": "M10 6L8.59 7.41 13.17 12l-4.58 4.59L10 18l6-6z",
    "drop": "M7 10l5 5 5-5z",
    "lock": "M18 8h-1V6c0-2.76-2.24-5-5-5S7 3.24 7 6v2H6c-1.1 0-2 .9-2 2v10c0 1.1.9 2 2 2h12c1.1 0 2-.9 2-2V10c0-1.1-.9-2-2-2zm-6 9c-1.1 0-2-.9-2-2s.9-2 2-2 2 .9 2 2-.9 2-2 2zm3.1-9H8.9V6c0-1.71 1.39-3.1 3.1-3.1 1.71 0 3.1 1.39 3.1 3.1v2z",
    "label": "M17.63 5.84C17.27 5.33 16.67 5 16 5L5 5.01C3.9 5.01 3 5.9 3 7v10c0 1.1.9 1.99 2 1.99L16 19c.67 0 1.27-.33 1.63-.84L22 12l-4.37-6.16z",
    "restore": "M19 4h-3.5l-1-1h-5l-1 1H5v2h14zM6 7v12c0 1.1.9 2 2 2h8c1.1 0 2-.9 2-2V7H6zm8 7v4h-4v-4H8l4-4 4 4h-2z",
    "clock": "M11.99 2C6.47 2 2 6.48 2 12s4.47 10 9.99 10C17.52 22 22 17.52 22 12S17.52 2 11.99 2zM12 20c-4.42 0-8-3.58-8-8s3.58-8 8-8 8 3.58 8 8-3.58 8-8 8zm.5-13H11v6l5.25 3.15.75-1.23-4.5-2.67z",
    "notices": "M12 22c1.1 0 2-.9 2-2h-4c0 1.1.9 2 2 2zm6-6v-5c0-3.07-1.63-5.64-4.5-6.32V4c0-.83-.67-1.5-1.5-1.5s-1.5.67-1.5 1.5v.68C7.64 5.36 6 7.92 6 11v5l-2 2v1h16v-1l-2-2zm-2 1H8v-6c0-2.48 1.51-4.5 4-4.5s4 2.02 4 4.5v6z",
    "person": "M12 12c2.21 0 4-1.79 4-4s-1.79-4-4-4-4 1.79-4 4 1.79 4 4 4zm0 2c-2.67 0-8 1.34-8 4v2h16v-2c0-2.66-5.33-4-8-4z",
    "bell": "M12 22c1.1 0 2-.9 2-2h-4c0 1.1.89 2 2 2zm6-6v-5c0-3.07-1.64-5.64-4.5-6.32V4c0-.83-.67-1.5-1.5-1.5s-1.5.67-1.5 1.5v.68C7.63 5.36 6 7.92 6 11v5l-2 2v1h16v-1l-2-2z",
    "file": "M14 2H6c-1.1 0-1.99.9-1.99 2L4 20c0 1.1.89 2 1.99 2H18c1.1 0 2-.9 2-2V8l-6-6zm2 16H8v-2h8v2zm0-4H8v-2h8v2zm-3-5V3.5L18.5 9H13z",
    "download": "M19 9h-4V3H9v6H5l7 7 7-7zM5 18v2h14v-2H5z",
    "help": "M11 18h2v-2h-2v2zm1-16C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm0 18c-4.41 0-8-3.59-8-8s3.59-8 8-8 8 3.59 8 8-3.59 8-8 8zm0-14c-2.21 0-4 1.79-4 4h2c0-1.1.9-2 2-2s2 .9 2 2c0 2-3 1.75-3 5h2c0-2.25 3-2.5 3-5 0-2.21-1.79-4-4-4z",
    "move": "M20 6h-8l-2-2H4c-1.1 0-1.99.9-1.99 2L2 18c0 1.1.9 2 2 2h16c1.1 0 2-.9 2-2V8c0-1.1-.9-2-2-2zm-6 12v-3h-4v-4h4V8l5 5-5 5z",
    "add": "M19 13h-6v6h-2v-6H5v-2h6V5h2v6h6v2z",
    "eye":"M12 4.5C7 4.5 2.73 7.61 1 12c1.73 4.39 6 7.5 11 7.5s9.27-3.11 11-7.5c-1.73-4.39-6-7.5-11-7.5zM12 17c-2.76 0-5-2.24-5-5s2.24-5 5-5 5 2.24 5 5-2.24 5-5 5zm0-8c-1.66 0-3 1.34-3 3s1.34 3 3 3 3-1.34 3-3-1.34-3-3-3z",
}


@register.filter
def report_md(text):
    from ops import report
    return report.markdown_html(text)


@register.simple_tag
def icon(name, size=20, cls=""):
    return format_html('<svg class="ic {}" width="{}" height="{}" viewBox="0 0 24 24" aria-hidden="true">'
                       '<path d="{}"/></svg>', cls, size, size, ICONS.get(name, ICONS["file"]))


@register.filter
def md(text):
    """ExCon's Markdown (role-play briefing, FAQ, notes), its HTML escaped first."""
    import html

    import markdown
    from django.utils.safestring import mark_safe
    return mark_safe(markdown.markdown(html.escape(text or "", quote=False), extensions=["tables", "sane_lists"]))
