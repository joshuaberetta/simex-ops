"""What a file a team sent holds, read in code (no model): its text and the facts the file keeps
about itself (the application that made it, its author, created and modified times, the
editing time and number of editing sessions Word records). ops/ai_docs.py gives both to the
model. Files come from participants: untrusted, so every read is capped (zip members, cells,
characters) and a file that can't be read is reported as such, never raised.

read(filename, content_type, data) -> Doc
  kind: "text" (the text is extracted here), "pdf" or "image" (the model reads the file itself),
  or "none" (note says why).
"""

import io
import re
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone as dt_tz
from xml.etree import ElementTree as ET

MAX_CHARS = 60_000  # text given to the model; the rest is cut
MAX_MEMBER = 20 * 1024 * 1024  # bytes read of one zip member (a zip bomb stops here)
MAX_PDF = 20 * 1024 * 1024
MAX_PDF_PAGES = 100
MAX_IMAGE = 5 * 1024 * 1024  # the API's limit per image
MAX_CELLS = 20_000

NS = {
    "w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "cp": "http://schemas.openxmlformats.org/package/2006/metadata/core-properties",
    "dc": "http://purl.org/dc/elements/1.1/",
    "dcterms": "http://purl.org/dc/terms/",
    "ep": "http://schemas.openxmlformats.org/officeDocument/2006/extended-properties",
    "text": "urn:oasis:names:tc:opendocument:xmlns:text:1.0",
    "office": "urn:oasis:names:tc:opendocument:xmlns:office:1.0",
    "meta": "urn:oasis:names:tc:opendocument:xmlns:meta:1.0",
}
W = "{%s}" % NS["w"]
IMAGES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif", ".webp": "image/webp"}
TEXT = (".txt", ".md", ".csv", ".tsv", ".json", ".html", ".htm", ".rtf")
OLD = {".doc": "Word 97-2003", ".xls": "Excel 97-2003", ".ppt": "PowerPoint 97-2003"}


@dataclass
class Doc:
    kind: str  # text | pdf | image | none
    text: str = ""
    media_type: str = ""
    pages: int | None = None
    words: int = 0
    facts: dict = field(default_factory=dict)
    note: str = ""  # why it can't be read, or that the text was cut


def ext(filename):
    m = re.search(r"\.[A-Za-z0-9]{1,5}$", filename or "")
    return m.group(0).lower() if m else ""


def read(filename, content_type, data):
    data = bytes(data or b"")
    e = ext(filename)
    if not data:
        return Doc("none", note="The file is empty or was not stored.")
    try:
        if data.startswith(b"%PDF") or e == ".pdf":
            return _pdf(data)
        if e in IMAGES or (content_type or "").startswith("image/"):
            return _image(data, e, content_type)
        if e in OLD:
            return Doc("none", note=f"An old {OLD[e]} file: not readable here.")
        if zipfile.is_zipfile(io.BytesIO(data)):
            with zipfile.ZipFile(io.BytesIO(data)) as zf:
                names = set(zf.namelist())
                if "word/document.xml" in names:
                    return _finish(*_docx(zf))
                if "ppt/presentation.xml" in names:
                    return _finish(*_pptx(zf))
                if "xl/workbook.xml" in names:
                    return _finish(*_xlsx(data, zf))
                if "content.xml" in names and "meta.xml" in names:
                    return _finish(*_odf(zf))
            return Doc("none", note="A zip archive or an unknown format: not readable here.")
        if e in TEXT or (content_type or "").startswith("text/"):
            text = data[:MAX_CHARS * 4].decode("utf-8", errors="replace")
            if e in (".html", ".htm"):
                text = re.sub(r"<[^>]+>", " ", re.sub(r"(?is)<(script|style).*?</\1>", " ", text))
            return _finish(text, {})
    except Exception as ex:  # a damaged file: reported, never raised
        return Doc("none", note=f"The file could not be read ({type(ex).__name__}).")
    return Doc("none", note="Not a document format read here.")


def _finish(text, facts, pages=None):
    text = re.sub(r"[ \t]+\n", "\n", re.sub(r"\n{3,}", "\n\n", text or "")).strip()
    words = len(text.split())
    note = ""
    if len(text) > MAX_CHARS:
        note = f"Only the first {MAX_CHARS:,} of {len(text):,} characters are shown."
        text = text[:MAX_CHARS]
    if not text:
        return Doc("none", facts=facts, pages=pages, note="No text in the file (perhaps scanned pages or only images).")
    return Doc("text", text=text, pages=pages, words=words, facts=facts, note=note)


# --- formats -----------------------------------------------------------------------------------

def _member(zf, name):
    try:
        with zf.open(name) as f:
            return f.read(MAX_MEMBER)
    except KeyError:
        return None


def _xml(zf, name):
    raw = _member(zf, name)
    if not raw:
        return None
    try:
        return ET.fromstring(raw)
    except ET.ParseError:
        return None


def _txt(root, path):
    el = root.find(path, NS) if root is not None else None
    return (el.text or "").strip() if el is not None and el.text else ""


def _office_facts(zf):
    """docProps/core.xml and app.xml of a Word, PowerPoint or Excel file."""
    core, app = _xml(zf, "docProps/core.xml"), _xml(zf, "docProps/app.xml")
    facts = {
        "application": " ".join(x for x in (_txt(app, "ep:Application"), _txt(app, "ep:AppVersion")) if x),
        "author": _txt(core, "dc:creator"),
        "last_modified_by": _txt(core, "cp:lastModifiedBy"),
        "created": _txt(core, "dcterms:created"),
        "modified": _txt(core, "dcterms:modified"),
        "revision": _txt(core, "cp:revision"),
        "editing_minutes": _txt(app, "ep:TotalTime"),
        "template": _txt(app, "ep:Template"),
        "title": _txt(core, "dc:title"),
    }
    return {k: v for k, v in facts.items() if v}


def _para(p):
    out = []
    for el in p.iter():
        if el.tag == W + "t" and el.text:
            out.append(el.text)
        elif el.tag == W + "tab":
            out.append("\t")
        elif el.tag in (W + "br", W + "cr"):
            out.append("\n")
    return "".join(out)


def _docx(zf):
    root = _xml(zf, "word/document.xml")
    body = root.find("w:body", NS) if root is not None else None
    lines = []
    for el in (list(body) if body is not None else []):
        if el.tag == W + "p":
            lines.append(_para(el))
        elif el.tag == W + "tbl":
            for tr in el.iter(W + "tr"):
                cells = [" ".join(_para(p) for p in tc.iter(W + "p")).strip() for tc in tr.findall("w:tc", NS)]
                lines.append(" | ".join(cells))
            lines.append("")
    facts = _office_facts(zf)
    settings = _xml(zf, "word/settings.xml")
    rsids = settings.find("w:rsids", NS) if settings is not None else None
    if rsids is not None:
        facts["editing_sessions"] = str(len(list(rsids)))
    if root is not None:
        authors = {el.get(W + "author") for el in root.iter() if el.tag in (W + "ins", W + "del") and el.get(W + "author")}
        if authors:
            facts["tracked_change_authors"] = str(len(authors))
    comments = _xml(zf, "word/comments.xml")
    if comments is not None:
        n = len(comments.findall("w:comment", NS))
        if n:
            facts["comments"] = str(n)
    return "\n".join(lines), facts, _int(_txt(_xml(zf, "docProps/app.xml"), "ep:Pages"))


def _pptx(zf):
    slides = sorted((n for n in zf.namelist() if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)),
                    key=lambda n: int(re.search(r"(\d+)", n.rsplit("/", 1)[1]).group(1)))
    parts = []
    for i, name in enumerate(slides, 1):
        root = _xml(zf, name)
        paras = []
        for p in (root.iter("{%s}p" % NS["a"]) if root is not None else []):
            t = "".join(x.text or "" for x in p.iter("{%s}t" % NS["a"])).strip()
            if t:
                paras.append(t)
        parts.append(f"[Slide {i}]\n" + "\n".join(paras))
    return "\n\n".join(parts), _office_facts(zf), len(slides) or None


def _xlsx(data, zf):
    import openpyxl
    facts = _office_facts(zf)
    wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    parts, cells = [], 0
    try:
        for ws in wb.worksheets:
            rows = []
            for row in ws.iter_rows(values_only=True):
                vals = ["" if v is None else str(v) for v in row]
                while vals and not vals[-1]:
                    vals.pop()
                if vals:
                    rows.append("\t".join(vals))
                cells += len(vals)
                if cells > MAX_CELLS:
                    break
            parts.append(f"[Sheet {ws.title}]\n" + "\n".join(rows))
            if cells > MAX_CELLS:
                parts.append(f"(cut after {MAX_CELLS:,} cells)")
                break
    finally:
        wb.close()
    return "\n\n".join(parts), facts, None


def _odf(zf):
    root, meta = _xml(zf, "content.xml"), _xml(zf, "meta.xml")
    lines = []
    for el in (root.iter() if root is not None else []):
        if el.tag in ("{%s}p" % NS["text"], "{%s}h" % NS["text"]):
            lines.append("".join(el.itertext()))
    m = meta.find("office:meta", NS) if meta is not None else None
    facts = {
        "application": _txt(m, "meta:generator"),
        "author": _txt(m, "meta:initial-creator"),
        "last_modified_by": _txt(m, "dc:creator"),
        "created": _txt(m, "meta:creation-date"),
        "modified": _txt(m, "dc:date"),
        "revision": _txt(m, "meta:editing-cycles"),
        "editing_minutes": _iso_minutes(_txt(m, "meta:editing-duration")),
    }
    return "\n".join(lines), {k: v for k, v in facts.items() if v}, None


def _pdf(data):
    if len(data) > MAX_PDF:
        return Doc("none", note=f"A PDF over {MAX_PDF // (1024 * 1024)} MB: not read.")
    pages = len(re.findall(rb"/Type\s*/Page(?![a-zA-Z])", data)) or None
    if pages and pages > MAX_PDF_PAGES:
        return Doc("none", pages=pages, note=f"A PDF of {pages} pages (over {MAX_PDF_PAGES}): not read.")
    facts = {}
    for key, name in (("application", rb"Creator"), ("producer", rb"Producer"), ("author", rb"Author"),
                      ("created", rb"CreationDate"), ("modified", rb"ModDate")):
        m = re.search(rb"/" + name + rb"\s*\(((?:[^()\\]|\\.){0,200})\)", data)
        if m:
            v = _pdf_string(m.group(1))
            facts[key] = _pdf_date(v) if key in ("created", "modified") else v
    for key, tag in (("application", rb"xmp:CreatorTool"), ("producer", rb"pdf:Producer")):
        m = re.search(rb"<" + tag + rb">([^<]{1,200})</" + tag + rb">", data)
        if m and key not in facts:
            facts[key] = m.group(1).decode("utf-8", errors="replace").strip()
    return Doc("pdf", media_type="application/pdf", pages=pages, facts={k: v for k, v in facts.items() if v})


def _image(data, e, content_type):
    if len(data) > MAX_IMAGE:
        return Doc("none", note=f"An image over {MAX_IMAGE // (1024 * 1024)} MB: not read.")
    media = _image_type(data) or IMAGES.get(e) or content_type
    if media not in IMAGES.values():
        return Doc("none", note="An image format the model doesn't read.")
    return Doc("image", media_type=media)


def _image_type(data):
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"GIF8"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


# --- values ------------------------------------------------------------------------------------

def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _iso_minutes(v):
    """ISO 8601 duration (ODF's editing-duration, PT1H2M3S) -> minutes, as text."""
    m = re.fullmatch(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)(?:\.\d+)?S)?", v or "")
    if not m or not v:
        return ""
    d, h, mi, s = (int(x or 0) for x in m.groups())
    return str(d * 1440 + h * 60 + mi + round(s / 60))


def _pdf_string(raw):
    if raw.startswith(b"\xfe\xff"):
        return raw[2:].decode("utf-16-be", errors="replace").strip()
    return re.sub(rb"\\(.)", rb"\1", raw).decode("latin-1", errors="replace").strip()


def _pdf_date(v):
    """D:20261123100200Z / D:20261123100200+03'00' -> ISO 8601 (UTC where the offset is known)."""
    m = re.match(r"D:(\d{4})(\d{2})(\d{2})(\d{2})?(\d{2})?(\d{2})?(Z|[+-]\d{2}'?\d{2}'?)?", v or "")
    if not m:
        return v
    y, mo, d, h, mi, s, tz = m.groups()
    iso = f"{y}-{mo}-{d}T{h or '00'}:{mi or '00'}:{s or '00'}"
    if tz == "Z":
        return iso + "Z"
    if tz:
        return iso + tz[:3] + ":" + tz.replace("'", "")[3:5]
    return iso


def parse_time(v):
    """A file's created / modified value -> aware datetime, or None."""
    if not v:
        return None
    try:
        t = datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=dt_tz.utc)
