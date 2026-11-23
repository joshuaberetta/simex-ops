"""Claude's reading of the files teams send (ops/docscan.py, ops/ai_docs.py), with a fake client."""

import io
import json
import zipfile

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile

from ops import ai, ai_docs, conf, docscan, reset
from ops.models import AiCall, DocumentAnalysis, Mail, MailAttachment, Notice, Response
from ops.tests.test_ai import FakeClient, answers
from ops.tests.test_ops import SAMPLE, arm, deliver_now, do_import, participant

DOC_OUT = {"summary": "A project plan for the shelter response.", "doc_type": "project plan", "answers_inject": "partly",
           "ai_written": 88, "ai_signals": "Uniform bold headings; 2,000 words in 3 editing minutes.",
           "passages": [{"quote": "ensure a comprehensive and robust response", "why": "stock chatbot phrasing"}],
           "language": "English"}


def docx(text="Shelter plan\nWe will set up 3 sites in Ndola.", minutes="3", sessions=2, rows=None):
    w = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    paras = "".join(f"<w:p><w:r><w:t>{line}</w:t></w:r></w:p>" for line in text.split("\n"))
    table = ""
    if rows:
        table = "<w:tbl>" + "".join("<w:tr>" + "".join(f"<w:tc><w:p><w:r><w:t>{c}</w:t></w:r></w:p></w:tc>" for c in r)
                                    + "</w:tr>" for r in rows) + "</w:tbl>"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("word/document.xml", f'<w:document xmlns:w="{w}"><w:body>{paras}{table}</w:body></w:document>')
        z.writestr("word/settings.xml", f'<w:settings xmlns:w="{w}"><w:rsids>'
                   + "".join(f'<w:rsid w:val="00{i:06d}"/>' for i in range(sessions)) + "</w:rsids></w:settings>")
        z.writestr("docProps/core.xml",
                   '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
                   'xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/">'
                   "<dc:creator>Jane Doe</dc:creator><cp:lastModifiedBy>Jane Doe</cp:lastModifiedBy>"
                   "<cp:revision>2</cp:revision><dcterms:created>2026-11-23T07:02:00Z</dcterms:created>"
                   "<dcterms:modified>2026-11-23T07:05:00Z</dcterms:modified></cp:coreProperties>")
        z.writestr("docProps/app.xml",
                   '<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties">'
                   f"<Application>Microsoft Office Word</Application><TotalTime>{minutes}</TotalTime><Pages>2</Pages></Properties>")
    return buf.getvalue()


def doc_answers(kw):
    if kw["system"] == ai.DOC_SYSTEM:
        return DOC_OUT, "end_turn"
    return answers(kw)


@pytest.fixture
def fake():
    c = FakeClient(doc_answers)
    ai.set_client(c)
    conf.put("ai_enabled", "1")
    return c


def send_file(excon, name="plan.docx", data=None, to="p:undac-a-tl", who=None):
    do_import(excon, SAMPLE)
    arm()
    deliver_now("1-15")
    pa = who or participant("a", "Jane")
    mail = Mail.objects.get(owner=None, team__code="a")
    pa.post("/inbox/send/", {"to": to, "subject": "Re: Update", "body": "Our plan attached", "in_reply_to": mail.id,
                             "files": SimpleUploadedFile(name, data or docx())})
    return pa


# --- reading files in code ----------------------------------------------------------------------

def test_docx_text_tables_and_facts():
    d = docscan.read("plan.docx", "", docx(rows=[["Site", "People"], ["Ndola", "1200"]]))
    assert d.kind == "text" and "We will set up 3 sites in Ndola." in d.text and "Ndola | 1200" in d.text
    f = d.facts
    assert f["application"] == "Microsoft Office Word" and f["editing_minutes"] == "3" and f["editing_sessions"] == "2"
    assert f["author"] == "Jane Doe" and f["created"] == "2026-11-23T07:02:00Z" and d.pages == 2


def test_pdf_image_and_unreadable_files():
    pdf = (b"%PDF-1.7\n1 0 obj << /Type /Page >> endobj 2 0 obj << /Type /Pages >> endobj\n"
           b"3 0 obj << /Producer (Microsoft\\256 Word) /CreationDate (D:20261123100200+03'00') >> endobj")
    d = docscan.read("plan.pdf", "application/pdf", pdf)
    assert d.kind == "pdf" and d.pages == 1 and d.facts["producer"].startswith("Microsoft")
    assert d.facts["created"] == "2026-11-23T10:02:00+03:00"
    assert docscan.read("a.png", "", b"\x89PNG\r\n\x1a\n" + b"0" * 20).media_type == "image/png"
    assert docscan.read("old.doc", "", b"\xd0\xcf\x11\xe0").kind == "none"
    assert docscan.read("x.zip", "", docx_like_zip()).kind == "none"
    assert docscan.read("bad.docx", "", b"PK\x03\x04 broken").kind == "none"  # never raises
    assert "first 60,000" in docscan.read("n.txt", "text/plain", b"word " * 20000).note


def docx_like_zip():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("readme.txt", "hello")
    return buf.getvalue()


# --- the reading --------------------------------------------------------------------------------

def test_a_sent_file_is_read_once_with_its_facts(excon, fake):
    send_file(excon)
    copies = MailAttachment.objects.exclude(sha256="").filter(attachment__isnull=True)
    assert copies.count() >= 2 and len(set(copies.values_list("sha256", flat=True))) == 1  # team Sent + own Sent...
    ai.tick()
    d = DocumentAnalysis.objects.get()
    assert d.status == "done" and d.team.code == "a" and d.ai_written == 88 and d.doc_type == "project plan"
    assert d.response == Response.objects.get() and d.inject.mel_id == "1-15" and d.person.name == "Jane"
    call = next(c for c in fake.calls if c["system"] == ai.DOC_SYSTEM)
    text = json.dumps(call["messages"])
    assert "We will set up 3 sites in Ndola" in text and "3 minutes of editing time" in text and "2 editing sessions" in text
    assert "Hello Team A" in text  # the inject it answers
    assert AiCall.objects.filter(purpose="document").count() == 1
    n = Notice.objects.get(key__startswith="docai:")
    assert n.url == f"/documents/{d.id}/" and "plan.docx" in n.body
    ai.tick()
    assert len([c for c in fake.calls if c["system"] == ai.DOC_SYSTEM]) == 1  # never twice


def test_files_already_sent_are_covered_when_it_is_turned_on(excon, fake):
    conf.put("ai_documents", "0")
    send_file(excon)
    ai.tick()
    assert not DocumentAnalysis.objects.exists()
    conf.put("ai_documents", "1")
    ai.tick()
    assert DocumentAnalysis.objects.get().status == "done"


def test_pdf_goes_to_the_model_as_a_document(excon, fake):
    send_file(excon, "plan.pdf", b"%PDF-1.7\n1 0 obj << /Type /Page >> endobj")
    ai.tick()
    call = next(c for c in fake.calls if c["system"] == ai.DOC_SYSTEM)
    blocks = call["messages"][0]["content"]
    assert blocks[-1]["type"] == "document" and blocks[-1]["source"]["media_type"] == "application/pdf"


def test_forwarded_inject_documents_and_unreadable_files(excon, fake):
    send_file(excon, "old.doc", b"\xd0\xcf\x11\xe0 legacy")
    ai.tick()
    d = DocumentAnalysis.objects.get()
    assert d.status == "skipped" and "97-2003" in d.error
    assert not [c for c in fake.calls if c["system"] == ai.DOC_SYSTEM]


def test_on_request_only_when_automatic_is_off(excon, fake):
    conf.put("ai_auto", "0")
    send_file(excon)
    ai.tick()
    assert not DocumentAnalysis.objects.exists()
    f = MailAttachment.objects.filter(attachment__isnull=True).exclude(sha256="").first()
    r = excon.post(f"/documents/queue/{f.mail.team_id if f.mail.team_id else 0}/{f.sha256}/", {"next": "/replies/"})
    assert r.status_code == 302
    ai.tick()
    assert DocumentAnalysis.objects.get().status == "done"


def test_cleared_mailboxes_drop_the_readings(excon, fake):
    send_file(excon)
    ai.tick()
    assert DocumentAnalysis.objects.exists()
    reset.clear_team(DocumentAnalysis.objects.get().team)
    assert not DocumentAnalysis.objects.exists()
    ai.tick()
    assert not DocumentAnalysis.objects.exists()  # its files are gone: nothing found again
    reset.start_afresh()
    assert not DocumentAnalysis.objects.exists()


def test_excon_pages_and_members_never_see_it(excon, fake):
    pa = send_file(excon)
    ai.tick()
    d = DocumentAnalysis.objects.get()
    page = excon.get(f"/documents/{d.id}/").content.decode()
    assert "A project plan for the shelter response." in page and "3 minutes of editing time" in page
    assert "stock chatbot phrasing" in page
    assert "Likely AI-assisted" in excon.get(f"/replies/{d.response_id}/").content.decode()
    assert "plan.docx" in excon.get("/dashboard/").content.decode()
    assert "project plan" in excon.get(f"/documents/?team={d.team_id}").content.decode()
    assert "Reading each document" in excon.get("/settings/").content.decode()
    assert pa.get(f"/documents/{d.id}/").status_code in (302, 403)
    for url in ("/inbox/", f"/inbox/t/{d.response.mail.thread}/"):
        assert "shelter response" not in pa.get(url).content.decode()


def test_leader_sees_the_reading_and_reports_carry_it(excon, fake):
    from ops import report
    from ops.tests.test_leaders import leader
    la = leader("a")
    send_file(excon)
    ai.tick()
    d = DocumentAnalysis.objects.get()
    html = la.get(f"/inbox/team/t/{d.response.mail.thread}/").content.decode()
    assert "plan.docx" in html and "Likely AI-assisted" in html and "shelter response" in html
    dash = la.get("/inbox/dashboard/").content.decode()
    assert "Documents your team sent" in dash and "plan.docx" in dash
    fig = report.team_figures(d.team, "live")
    docs = [x for x in fig["ai_written"] if x.get("document")]
    assert docs == [{"document": d.id, "file": "plan.docx", "inject": "1-15", "band": "likely AI-assisted",
                     "signals": d.ai_signals, "wpm": None}]
    assert "ai_written" not in report.for_team(fig, False)


def test_a_teams_file_is_its_own(excon, fake):
    """The same file sent by two teams is read for each."""
    send_file(excon)
    pb = participant("b", "Bob")
    mail = Mail.objects.filter(owner=None, team__code="b").first()
    pb.post("/inbox/send/", {"to": "p:undac-b-tl", "subject": "Re: Update", "body": "Ours", "in_reply_to": mail.id,
                             "files": SimpleUploadedFile("plan.docx", docx())})
    ai.tick()
    ai.tick()
    assert sorted(DocumentAnalysis.objects.values_list("team__code", flat=True)) == ["a", "b"]
