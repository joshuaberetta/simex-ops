"""Rich message bodies (SIMEX Mail's composer: bold, italics, lists, links, colours, images in
the text). The HTML comes from a browser's contenteditable, so it is untrusted: clean() rebuilds
it from an allowlist of tags, attributes and style properties (stdlib parser, no dependency);
nothing it doesn't know survives, text is always escaped. Images in the text are files of the
message (MailAttachment / MessageAttachment with a cid) and the HTML refers to them only as
"cid:<cid>", which is how email carries them too; render() maps each to the file's URL for
SIMEX Mail. Mail.body / Message.body stay the plain text (search, snippets, SIMEX AI, reports):
to_text() makes it from the HTML."""

import html as htmlmod
import re
import secrets
from html.parser import HTMLParser

MAX_HTML = 400_000  # characters of HTML kept (images are files, not in here)
IMAGE_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp"}  # never SVG (it can carry script)

TAGS = {"p", "div", "br", "span", "b", "strong", "i", "em", "u", "s", "strike", "del", "sub", "sup",
        "ul", "ol", "li", "blockquote", "a", "img", "h1", "h2", "h3", "h4", "pre", "code", "hr",
        "table", "thead", "tbody", "tr", "td", "th", "font"}
VOID = {"br", "img", "hr"}
# Dropped with everything inside them.
DROP = {"script", "style", "head", "title", "iframe", "object", "embed", "svg", "math", "template", "noscript",
        "textarea", "select", "option", "button", "form", "input", "video", "audio", "canvas", "frame", "frameset",
        "applet", "link", "meta", "base", "xml"}
ATTRS = {"a": {"href", "title"}, "img": {"src", "alt", "width", "height", "data-cid"},
         "td": {"colspan", "rowspan"}, "th": {"colspan", "rowspan"}, "font": {"color"}, "ol": {"start"}}
STYLES = {"color", "background-color", "font-weight", "font-style", "text-decoration", "text-decoration-line",
          "text-align", "font-size", "margin-left", "padding-left"}
STYLE_VALUE = re.compile(r"^[#a-zA-Z0-9\s,.%()-]{1,60}$")
CID = re.compile(r"^[A-Za-z0-9._-]{1,80}$")
NUMBER = re.compile(r"^\d{1,4}%?$")
BLOCK = {"p", "div", "li", "blockquote", "h1", "h2", "h3", "h4", "pre", "tr", "table", "ul", "ol", "hr"}


def image_type(data):
    """The type of an image in the text from its first bytes (never what the browser said), or
    None if it isn't one we show."""
    head = bytes(data[:16])
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    return None


def new_cid():
    return f"img-{secrets.token_hex(8)}"


def _style(value):
    out = []
    for part in (value or "").split(";"):
        if ":" not in part:
            continue
        prop, val = (x.strip() for x in part.split(":", 1))
        prop = prop.lower()
        low = val.lower()
        if (prop in STYLES and STYLE_VALUE.match(val) and "url" not in low and "expression" not in low
                and "var(" not in low):
            out.append(f"{prop}: {val}")
    return "; ".join(out)


def _href(value):
    v = (value or "").strip()
    low = re.sub(r"[\s\x00-\x1f]+", "", v).lower()
    if low.startswith(("http://", "https://", "mailto:")):
        return v
    return None


class _Cleaner(HTMLParser):
    def __init__(self, image):
        super().__init__(convert_charrefs=True)
        self.image = image  # cid -> src to write (None: drop the image), or None: keep cid:
        self.out, self.open, self.drop, self.cids = [], [], 0, []

    def handle_starttag(self, tag, attrs):
        if self.drop:
            if tag in DROP:
                self.drop += 1
            return
        if tag in DROP:
            self.drop = 1
            return
        if tag not in TAGS:
            return
        allowed, kept = ATTRS.get(tag, set()), []
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag == "img":
            src = a.get("src", "")
            cid = a.get("data-cid") or (src[4:] if src.lower().startswith("cid:") else "")
            if not CID.match(cid or ""):
                return  # an image from anywhere else (the web, data:): never shown
            url = f"cid:{cid}" if self.image is None else self.image(cid)
            if not url:
                return
            self.cids.append(cid)
            kept.append(("src", url))
            if not url.startswith("cid:"):  # shown from its file: the editor keeps which one
                kept.append(("data-cid", cid))
            for k in ("alt", "width", "height"):
                if k in a and (k == "alt" or NUMBER.match(a[k])):
                    kept.append((k, a[k][:200]))
        else:
            for k, v in a.items():
                if k == "style":
                    s = _style(v)
                    if s:
                        kept.append(("style", s))
                elif k in allowed:
                    if k == "href":
                        v = _href(v)
                        if v is None:
                            continue
                    elif k in ("colspan", "rowspan", "start") and not v.isdigit():
                        continue
                    elif k == "color" and not STYLE_VALUE.match(v):
                        continue
                    kept.append((k, v[:2000]))
            if tag == "a":
                kept += [("target", "_blank"), ("rel", "noopener noreferrer")]
        self.out.append(f"<{tag}" + "".join(f' {k}="{htmlmod.escape(v, quote=True)}"' for k, v in kept) + ">")
        if tag not in VOID:
            self.open.append(tag)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in VOID and self.open and self.open[-1] == tag:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if self.drop:
            if tag in DROP:
                self.drop -= 1
            return
        if tag not in TAGS or tag in VOID or tag not in self.open:
            return
        while self.open:
            t = self.open.pop()
            self.out.append(f"</{t}>")
            if t == tag:
                break

    def handle_data(self, data):
        if not self.drop:
            self.out.append(htmlmod.escape(data, quote=False))

    def result(self):
        self.close()
        return "".join(self.out) + "".join(f"</{t}>" for t in reversed(self.open))


def clean(value, image=None):
    """Safe HTML from untrusted HTML. image: None keeps images as src="cid:<cid>" (what is
    stored); else a function cid -> URL (or None to leave that image out)."""
    p = _Cleaner(image)
    p.feed((value or "")[:MAX_HTML])
    return p.result().strip()


def cids(value):
    """The cids of the images an HTML body shows, in order."""
    p = _Cleaner(None)
    p.feed((value or "")[:MAX_HTML])
    p.result()
    return list(dict.fromkeys(p.cids))


# Formatting beyond paragraphs and line breaks: what makes an HTML body worth keeping (else
# the plain text says it all, and old and new mail look the same).
_RICH = re.compile(r"<(?!/?(p|div|br|span)\b)[a-z]|<span [^>]*style=|<(p|div) [^>]*style=", re.I)


def is_rich(cleaned):
    return bool(_RICH.search(cleaned or ""))


class _Text(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out, self.lists, self.href, self.link_text = [], [], [], []

    def _nl(self, n=1):
        text = "".join(self.out)
        have = len(text) - len(text.rstrip("\n"))
        if text and have < n:
            self.out.append("\n" * (n - have))

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "br":
            self.out.append("\n")
        elif tag in ("p", "h1", "h2", "h3", "h4", "blockquote", "pre", "table"):
            self._nl(2)
        elif tag in ("div", "tr"):
            self._nl(1)
        elif tag in ("ul", "ol"):
            self._nl(1)
            self.lists.append([tag, int(a.get("start") or 1) if str(a.get("start") or "").isdigit() else 1])
        elif tag == "li":
            self._nl(1)
            if self.lists and self.lists[-1][0] == "ol":
                self.out.append(f"{'  ' * (len(self.lists) - 1)}{self.lists[-1][1]}. ")
                self.lists[-1][1] += 1
            else:
                self.out.append(f"{'  ' * max(len(self.lists) - 1, 0)}- ")
        elif tag == "img":
            self.out.append(f"[image{': ' + a['alt'] if a.get('alt') else ''}]")
        elif tag == "hr":
            self._nl(1)
            self.out.append("---")
            self._nl(1)
        elif tag in ("td", "th"):
            self.out.append("\t")
        elif tag == "a":
            self.href.append(a.get("href") or "")
            self.link_text.append(len(self.out))

    def handle_endtag(self, tag):
        if tag in ("p", "h1", "h2", "h3", "h4", "blockquote", "pre", "table"):
            self._nl(2)
        elif tag in ("ul", "ol"):
            if self.lists:
                self.lists.pop()
            self._nl(1)
        elif tag == "a" and self.href:
            href, start = self.href.pop(), self.link_text.pop()
            shown = "".join(self.out[start:]).strip()
            if href and href.startswith("http") and href not in shown:
                self.out.append(f" ({href})")

    def handle_data(self, data):
        self.out.append(re.sub(r"[ \t\r\n]+", " ", data))


def to_text(cleaned):
    """The plain text of a (clean) HTML body."""
    p = _Text()
    p.feed(cleaned or "")
    p.close()
    lines = [line.rstrip() for line in "".join(p.out).split("\n")]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(line.lstrip(" ") if not line.lstrip().startswith(("- ",)) else line
                                            for line in lines)).strip()


def render(cleaned, files, url):
    """A stored body for SIMEX Mail: each cid: image pointed at its file (files: the Mail's
    MailAttachments; url(file) its address), images without a file left out."""
    by_cid = {f.cid: f for f in files if getattr(f, "cid", "")}
    return clean(cleaned, image=lambda c: url(by_cid[c]) if c in by_cid else None)


def quote_html(cleaned_or_text, is_html, header):
    """An earlier message under a forward or an emailed answer, as HTML."""
    from .render import text_to_html
    inner = cleaned_or_text if is_html else text_to_html(cleaned_or_text or "")
    return (f'<div>{htmlmod.escape(header)}</div>'
            f'<blockquote style="margin-left: 8px; padding-left: 8px">{inner}</blockquote>')
