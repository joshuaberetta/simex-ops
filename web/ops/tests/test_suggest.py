"""Suggest a reply (ops/suggest.py) and the library (ops/kb.py): the timeline of what a team has,
the tool loop with a fake model, the desk's and ExCon's routes, and the role-play scripts
document import (roleplay.read_scripts). No exercise content: everything here is made up."""

import gzip
import json
from datetime import timedelta
from types import SimpleNamespace as NS

import pytest
from django.utils import timezone

from ops import ai, conf, kb, roleplay, suggest
from ops.models import KbDocument, Mail, ReplySuggestion, RolePlay, RolePlaySlot, Team
from ops.tests.test_desk import desk_client, setup, team_writes
from ops.tests.test_leaders import leader
from ops.tests.test_ops import deliver_now, participant

FETCH = {"x-requested-with": "fetch"}


def bundle(docs):
    return gzip.compress(json.dumps({"format": kb.FORMAT, "documents": docs}).encode())


DOCS = [
    {"key": "d1", "path": "B-12 Settlements.pdf", "title": "B-12 Settlements.pdf", "category": "baseline",
     "release": "all", "text": "Village Zorba, Plaida South, Plaida province, population 16,032."},
    {"key": "d2", "path": "1-15 Flood report.pdf", "title": "1-15 Flood report.pdf", "category": "simex-injects",
     "release": "with", "serial": "1-15", "text": "Zorba: water still rising, 25% of homes flooded."},
    {"key": "d3", "path": "MapAction/Zorba map.pdf", "title": "Zorba map.pdf", "category": "simex-maps",
     "release": "no", "text": "Reference map of Zorba."},
]


def test_library_import_keeps_excon_release_and_rejects_other_files(excon):
    assert excon.post("/library/", {"file": _file(b"{}", "x.json")}).status_code == 302
    assert KbDocument.objects.count() == 0
    excon.post("/library/", {"file": _file(bundle(DOCS), "lib.json.gz")})
    assert KbDocument.objects.count() == 3
    d3 = KbDocument.objects.get(key="d3")
    excon.post(f"/library/{d3.id}/", {"release": "with", "serial": "1-15"})
    d3.refresh_from_db()
    assert (d3.release, d3.serial) == ("with", "1-15")
    changed = [dict(DOCS[0], text="New text"), DOCS[1], DOCS[2]]
    n = kb.apply(kb.read_bundle(bundle(changed)))
    d3.refresh_from_db()
    assert n == {"new": 0, "updated": 1, "kept": 1, "deleted": 0} and d3.release == "with"
    assert KbDocument.objects.get(key="d1").text == "New text"
    assert excon.get("/library/").status_code == 200 and excon.get(f"/library/{d3.id}/text/").status_code == 200
    pa = participant("a")
    assert pa.get("/library/").status_code == 403


def _file(data, name):
    from django.core.files.uploadedfile import SimpleUploadedFile
    return SimpleUploadedFile(name, data)


def test_timeline_follows_deliveries_and_role_plays(excon):
    setup(excon)
    kb.apply(kb.read_bundle(bundle(DOCS)))
    ta, tb = Team.objects.get(code="a"), Team.objects.get(code="b")
    play = RolePlay.objects.create(key="2-10", serial="2-10", title="Zorba NGO visit", script="SCRIPT: you run Zorba camp")
    s = RolePlaySlot.objects.create(play=play, start=timezone.now() + timedelta(hours=2), minutes=10)
    s.teams.set([ta])
    tl = suggest.Timeline(ta)
    d1, d2, d3 = (KbDocument.objects.get(key=k) for k in ("d1", "d2", "d3"))
    assert tl.doc(d1)[0] == "known" and tl.doc(d2)[0] == "upcoming" and tl.doc(d3)[0] == "not_given"
    assert tl.inject("1-15")[0] == "upcoming" and tl.play("2-10")[0] == "upcoming"
    assert suggest.Timeline(tb).play("2-10")[0] == "upcoming"  # not scheduled for B: still not played
    deliver_now("1-15")
    s.start = timezone.now() - timedelta(minutes=1)
    s.save()
    tl = suggest.Timeline(ta)
    assert tl.doc(d2)[0] == "known" and tl.inject("1-15")[0] == "known" and tl.play("2-10")[0] == "known"
    assert tl.ref("rp:2-10")[0] == "known" and tl.ref("doc:d3")[0] == "not_given" and tl.ref("new")[0] == "known"
    # a document given as an inject attachment counts as given, whatever its release
    from ops.models import Attachment, Inject
    Attachment.objects.create(inject=Inject.objects.get(mel_id="1-15"), filename="Zorba map.pdf", data=b"x", size=1)
    assert suggest.Timeline(ta).doc(d3)[0] == "known"


class Loop:
    """A fake model: searches, reads, then submits an answer citing a planned inject."""

    def __init__(self):
        self.calls = []
        self.messages = NS(create=self.create)
        self.beta = NS(messages=NS(create=self.create))

    def create(self, **kw):
        self.calls.append(json.loads(json.dumps(kw, default=str)))
        n = len(self.calls)
        usage = NS(input_tokens=1000, output_tokens=100, cache_read_input_tokens=0, cache_creation_input_tokens=0)
        if n == 1:
            content = [NS(type="thinking", thinking="", signature="sig"),
                       NS(type="tool_use", id="t1", name="search", input={"query": "Zorba"})]
        elif n == 2:
            content = [NS(type="tool_use", id="t2", name="read", input={"ref": "rp:2-10"})]
        else:
            content = [NS(type="tool_use", id="t3", name="submit", input={
                "asks": "Where is Zorba?", "body": "Hi team,\nZorba is in Plaida South.\nBest,\nMarc",
                "facts": [{"fact": "Zorba is in Plaida South", "source": "doc:d1"},
                          {"fact": "25% of homes flooded", "source": "doc:d2"}],
                "corrections": [], "not_available": ["GPS coordinates"],
                "hold_back": [{"what": "flooding figures", "source": "inject:1-15", "when": "later"}],
                "notes": "", "questions": []})]
        return NS(model=kw["model"], stop_reason="tool_use", stop_details=None, usage=usage, content=content)


@pytest.fixture
def loop():
    c = Loop()
    ai.set_client(c)
    conf.put("ai_enabled", "1")
    suggest.RUN_INLINE = True
    yield c
    suggest.RUN_INLINE = False


def test_desk_asks_for_a_suggestion_with_the_timeline(excon, loop):
    setup(excon)
    kb.apply(kb.read_bundle(bundle(DOCS)))
    ta = Team.objects.get(code="a")
    play = RolePlay.objects.create(key="2-10", serial="2-10", title="Zorba NGO visit", script="SCRIPT: Zorba camp has 3 latrines",
                                   outcomes="SECRET OUTCOME")
    RolePlaySlot.objects.create(play=play, start=timezone.now() + timedelta(hours=2), minutes=10)
    deliver_now("3-13")  # Team A answers it; 1-15 (with the flood report) is still to come
    pa = participant("a", "Jane")
    _, r = team_writes(pa, ta, "Where is Zorba? Send GPS please.")
    thread = Mail.objects.get(team=None, owner=None, folder="inbox", desk_response=r).thread
    d = desk_client()
    j = d.get(f"/inbox/desk/suggest/?thread={thread}").json()
    assert j["suggestion"] is None and j["ai"]
    j = d.post("/inbox/desk/suggest/", {"thread": thread, "prompt": "keep it short"}, headers=FETCH).json()["suggestion"]
    s = ReplySuggestion.objects.get()
    assert j["status"] == "done" and s.steps == 3 and s.response == r and s.thread == thread
    assert j["mail"] == Mail.objects.get(team=None, owner=None, folder="inbox", desk_response=r).id
    body = s.body
    assert body["body"].startswith("Hi team") and body["not_available"] == ["GPS coordinates"]
    # the cited flood report comes with 1-15, which hasn't reached Team A: the desk is warned
    assert [f["state"] for f in body["facts"]] == ["known", "upcoming"]
    assert len(body["warnings"]) == 1 and "doc:d2" in body["warnings"][0]
    first = loop.calls[0]["messages"][0]["content"][0]["text"]
    assert "keep it short" in first and "rp:2-10 [NOT YET played with the team" in first
    assert "Where is Zorba?" in first
    search = json.dumps(loop.calls[1]["messages"][-1])
    assert "doc:d1" in search and "the participants have it" in search and "NOT YET given to the team" in search
    read = json.dumps(loop.calls[2]["messages"][-1])
    assert "Zorba camp has 3 latrines" in read and "SECRET OUTCOME" in read  # for the desk's eyes: the notes
    # the tool loop keeps one cache breakpoint on the conversation (plus the system prompt)
    marks = json.dumps(loop.calls[2]["messages"]).count("cache_control")
    assert marks == 1
    # its GET by id; the same for ExCon on its reply page
    assert d.get(f"/inbox/desk/suggest/{s.id}/").json()["suggestion"]["status"] == "done"
    assert excon.get(f"/suggestions/?response={r.id}").json()["suggestion"]["id"] == s.id
    page = excon.get(f"/replies/{r.id}/").content.decode()
    assert "data-suggest" in page and 'data-url="/suggestions/"' in page
    assert 'data-suggest' in d.get(f"/inbox/desk/t/{thread}/").content.decode()


def test_nobody_else_gets_suggestions(excon, loop):
    setup(excon)
    deliver_now("3-13")
    pa = participant("a", "Jane")
    _, r = team_writes(pa, Team.objects.get(code="a"))
    la = leader("a")
    for c in (pa, la):
        assert c.get(f"/inbox/desk/suggest/?response={r.id}").status_code == 403
        assert c.post("/inbox/desk/suggest/", {"response": r.id}).status_code == 403
        assert c.get(f"/suggestions/?response={r.id}").status_code == 403
    assert not ReplySuggestion.objects.exists()
    # a leader's view of the team inbox never shows the panel
    assert "data-suggest" not in la.get("/inbox/team/").content.decode()


def test_suggestion_needs_ai_on_and_fails_cleanly(excon):
    setup(excon)
    deliver_now("3-13")
    pa = participant("a", "Jane")
    _, r = team_writes(pa, Team.objects.get(code="a"))
    d = desk_client()
    resp = d.post("/inbox/desk/suggest/", {"response": r.id}, headers=FETCH)
    assert resp.status_code == 400 and "off" in resp.json()["error"]

    class Refuses(Loop):
        def create(self, **kw):
            return NS(model=kw["model"], stop_reason="refusal", stop_details=None, content=[],
                      usage=NS(input_tokens=10, output_tokens=0, cache_read_input_tokens=0, cache_creation_input_tokens=0))
    ai.set_client(Refuses())
    conf.put("ai_enabled", "1")
    suggest.RUN_INLINE = True
    try:
        j = d.post("/inbox/desk/suggest/", {"response": r.id}, headers=FETCH).json()["suggestion"]
    finally:
        suggest.RUN_INLINE = False
    assert j["status"] == "failed" and "declined" in j["error"]


SCRIPTS_MD = """**9-02   Kick-off meeting**

Monday
**13:00**
*None*

SCRIPT: You greet the team \\- warmly\\!
Then ask about roles.
**9-77   Zorba NGO visit**
**Task: list needs**

Tuesday
**1440 \\- A**
**1450 \\- B**
*9-77 Handout*

SCRIPT:
You run the Zorba camp.
"""


def test_scripts_document_sets_scripts_and_adds_missing_events(excon):
    from ops.tests.test_roleplay import do_import, teams4
    teams4()
    do_import()  # the synthetic MEL: 9-02 has a script and a title
    before = RolePlay.objects.get(key="9-02")
    src = roleplay.read_scripts(SCRIPTS_MD.encode(), "scripts.md")
    rows = {r["serial"]: r for r in src[0]["rows"]}
    assert rows["9-02"]["script"] == "You greet the team - warmly!\nThen ask about roles."
    assert rows["9-77"]["time"] == "1440 - A\n1450 - B" and rows["9-77"]["documents"] == "9-77 Handout"
    assert rows["9-77"]["day"] == "Tuesday" and rows["9-77"]["script"].startswith("Task: list needs")
    recs = roleplay.records(src)
    r902 = next(r for r in recs if r["key"] == "9-02")
    assert set(r902) == {"key", "serial", "script"}  # an event already here takes only the script
    roleplay.apply(recs)
    after = RolePlay.objects.get(key="9-02")
    assert after.title == before.title and after.method == before.method and after.outcomes == before.outcomes
    assert after.script.startswith("You greet the team")
    new = RolePlay.objects.get(key="9-77")
    assert new.title == "Zorba NGO visit" and new.slots.count() == 2
    # through the Role play tab: no "delete what isn't in the file" for a scripts document
    page = excon.post("/roleplay/import/", {"file": _file(SCRIPTS_MD.encode(), "scripts.md")}, follow=True)
    assert b"scripts document" in page.content and b"no longer in the file" not in page.content


def test_suggestions_stay_out_of_participant_facing_code():
    """The suggestions' notes may quote role-play scripts and planned injects: only the desk's
    and ExCon's views (views_suggest.py) serve them."""
    import pathlib
    import re
    here = pathlib.Path(suggest.__file__).parent
    for name in ("views_inbox.py", "views_leader.py", "ai.py", "npc.py", "report.py", "views_ai.py", "views_report.py",
                 "render.py", "delivery.py", "desk.py", "inbound.py", "desk_ai.py", "suggest_leader.py", "calendar.py",
                 "views_calendar.py", "cal_seed.py", "views_cal_seed.py"):
        src = (here / name).read_text()
        assert not re.search(r"\bimport suggest\b|\bsuggest\.|ReplySuggestion|views_suggest", src), name


def test_an_answer_before_looking_in_the_documents_is_sent_back_once(excon):
    setup(excon)
    deliver_now("3-13")
    kb.apply(kb.read_bundle(bundle(DOCS)))
    pa = participant("a", "Jane")
    _, r = team_writes(pa, Team.objects.get(code="a"), "Where is Zorba?")

    class Hasty(Loop):
        def create(self, **kw):
            self.calls.append(json.loads(json.dumps(kw, default=str)))
            usage = NS(input_tokens=10, output_tokens=10, cache_read_input_tokens=0, cache_creation_input_tokens=0)
            return NS(model=kw["model"], stop_reason="tool_use", stop_details=None, usage=usage, content=[
                NS(type="tool_use", id=f"t{len(self.calls)}", name="submit", input={"body": "In Plaida.", "facts": []})])
    c = Hasty()
    ai.set_client(c)
    conf.put("ai_enabled", "1")
    s = suggest.run(ReplySuggestion.objects.create(response=r).id)
    assert s.status == "done" and s.steps == 2 and s.body["body"] == "In Plaida." and s.body["hold_back"] == []
    back = c.calls[1]["messages"][-1]["content"][0]
    assert back["is_error"] and "search the participants' documents" in back["content"]


def _docx(text):
    import io
    import zipfile
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml", '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                   f'<w:body><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:body></w:document>')
    return buf.getvalue()


def _pdf(text):
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject
    import io
    w = PdfWriter()
    page = w.add_blank_page(612, 792)
    font = DictionaryObject({NameObject("/Type"): NameObject("/Font"), NameObject("/Subtype"): NameObject("/Type1"),
                             NameObject("/BaseFont"): NameObject("/Helvetica")})
    page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): DictionaryObject({NameObject("/F1"): w._add_object(font)})})
    stream = DecodedStreamObject()
    stream.set_data(f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode())
    page[NameObject("/Contents")] = w._add_object(stream)
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue()


def test_excon_adds_guidance_files_that_count_as_background(excon):
    excon.post("/library/", {"file": _file(bundle(DOCS), "lib.json.gz")})
    r = excon.post("/library/", {"files": [_file(_pdf("EMTCC coordinates non classified teams"), "EMT Handbook (2023).pdf"),
                                           _file(_docx("Triage standards for EMT type 1"), "EMT_notes.docx"),
                                           _file(b"\x00\x01junk", "scan.bin")], "release": "ref"})
    assert r.status_code == 302
    hb = KbDocument.objects.get(title="EMT Handbook (2023)")
    assert hb.key.startswith(kb.UPLOAD) and hb.release == "ref" and hb.category == "guidance"
    assert "EMTCC coordinates" in hb.text and "[Page 1]" in hb.text
    assert "Triage standards" in KbDocument.objects.get(title="EMT notes").text
    assert KbDocument.objects.count() == 5  # the junk file was refused
    # the same file name again replaces the text; the export's import never removes them
    excon.post("/library/", {"files": [_file(_pdf("Second edition"), "EMT Handbook (2023).pdf")], "release": "ref"})
    hb.refresh_from_db()
    assert "Second edition" in hb.text and KbDocument.objects.count() == 5
    kb.apply(kb.read_bundle(bundle(DOCS[:1])), delete_missing=True)
    assert KbDocument.objects.filter(key__startswith=kb.UPLOAD).count() == 2 and KbDocument.objects.count() == 3
    # a team always "has" background guidance, labelled as such
    state, label = suggest.Timeline(Team.objects.get(code="a")).doc(hb)
    assert state == "known" and "background guidance" in label
    assert "background guidance" in suggest.library_line()
    excon.post(f"/library/{hb.id}/", {"title": "WHO EMT Handbook", "release": "ref"})
    hb.refresh_from_db()
    assert hb.title == "WHO EMT Handbook"
    assert excon.get("/library/").status_code == 200


class LeaderLoop(Loop):
    """A fake model for a team leader: searches, tries to read a document the team hasn't got,
    then submits citing one it has and one it hasn't."""

    def create(self, **kw):
        self.calls.append(json.loads(json.dumps(kw, default=str)))
        n = len(self.calls)
        usage = NS(input_tokens=1000, output_tokens=100, cache_read_input_tokens=0, cache_creation_input_tokens=0)
        if n == 1:
            content = [NS(type="tool_use", id="t1", name="search", input={"query": "Zorba flooded"})]
        elif n == 2:
            content = [NS(type="tool_use", id="t2", name="read", input={"ref": "doc:d2"}),
                       NS(type="tool_use", id="t3", name="read", input={"ref": "inject:3-13"})]
        else:
            content = [NS(type="tool_use", id="t4", name="submit", input={
                "asks": "Where is Zorba?", "body": "Amina, Zorba is in Plaida South. Go.",
                "facts": [{"fact": "Zorba is in Plaida South", "source": "doc:d1"},
                          {"fact": "a map", "source": "doc:d3"}],
                "questions": ["Send someone today?"]})]
        return NS(model=kw["model"], stop_reason="tool_use", stop_details=None, usage=usage, content=content)


def test_team_leader_gets_a_suggestion_from_what_they_may_see(excon):
    from ops import suggest_leader
    from ops.models import Inject, LeaderSuggestion, Sender
    from ops.tests.test_leaders import setup_exercise
    setup_exercise(excon)
    docs = [DOCS[0], dict(DOCS[1], serial="3-13"), DOCS[2],
            {"key": "g1", "path": "x", "title": "EMT handbook", "release": "ref", "text": "Zorba-style EMTCC guidance."}]
    kb.apply(kb.read_bundle(bundle(docs)))
    Inject.objects.filter(mel_id="3-13").update(body="PLANNED SECRET ARTICLE")
    Sender.objects.filter(team_leader=True).update(notes="PERSONA NOTE SECRET")
    play = RolePlay.objects.create(key="2-10", serial="2-10", title="Zorba visit", script="RP SCRIPT SECRET", outcomes="RP OUTCOME")
    RolePlaySlot.objects.create(play=play, start=timezone.now() - timedelta(minutes=5), minutes=10)
    deliver_now("1-15")
    ta = Team.objects.get(code="a")
    la, pa, lb = leader("a"), participant("a", "Amina"), leader("b")
    team_mail = Mail.objects.get(owner=None, team=ta)
    pa.post("/inbox/send/", {"to": "p:undac-a-tl", "subject": "Re: Update", "body": "Where is Zorba?", "in_reply_to": team_mail.id})
    got = Mail.objects.get(owner=la.person, folder="inbox")
    page = la.get(f"/inbox/t/{got.thread}/").content.decode()
    assert "data-suggest" not in page  # AI off
    c = LeaderLoop()
    ai.set_client(c)
    conf.put("ai_enabled", "1")
    suggest_leader.RUN_INLINE = True
    try:
        page = la.get(f"/inbox/t/{got.thread}/").content.decode()
        assert 'data-url="/inbox/leader/suggest/"' in page and 'data-mode="leader"' in page
        assert "data-suggest" not in pa.get(f"/inbox/t/{got.thread}/").content.decode()
        j = la.post("/inbox/leader/suggest/", {"thread": got.thread, "prompt": "be brief"}, headers=FETCH).json()["suggestion"]
    finally:
        suggest_leader.RUN_INLINE = False
    s = LeaderSuggestion.objects.get()
    assert j["status"] == "done" and j["mail"] == got.id and s.steps == 3 and s.team == ta
    assert set(s.body) == {"asks", "body", "facts", "questions", "warnings"}  # no desk notes
    assert [f["state"] for f in s.body["facts"]] == ["known", "not_given"] and len(s.body["warnings"]) == 1
    sent = json.dumps(c.calls)
    assert "Where is Zorba?" in sent and "be brief" in sent and "Hello Team A" in sent
    assert "Plaida South" in sent and "EMTCC guidance" in sent and "background guidance" in sent
    for secret in ("PLANNED SECRET", "PERSONA NOTE", "RP SCRIPT", "RP OUTCOME", "homes flooded", "Reference map"):
        assert secret not in sent, secret
    # only that leader sees it; nobody else may ask
    assert la.get(f"/inbox/leader/suggest/?thread={got.thread}").json()["suggestion"]["id"] == s.id
    assert lb.get(f"/inbox/leader/suggest/{s.id}/").status_code == 404
    assert lb.get(f"/inbox/leader/suggest/?thread={got.thread}").json()["suggestion"] is None
    assert lb.post("/inbox/leader/suggest/", {"thread": got.thread}).status_code == 400
    assert pa.post("/inbox/leader/suggest/", {"thread": got.thread}).status_code == 403
    assert desk_client().get(f"/inbox/leader/suggest/?thread={got.thread}").status_code == 403
    conf.put("tl_suggest", "0")
    assert la.post("/inbox/leader/suggest/", {"thread": got.thread}).status_code == 400


def test_leader_suggestions_read_nothing_control_side():
    import pathlib
    import re
    src = (pathlib.Path(suggest.__file__).parent / "suggest_leader.py").read_text()
    code = src.split('"""', 2)[2]  # past the module's docstring
    assert not re.search(r"\bRolePlay\b|\.script\b|\.outcomes\b|\.notes\b|NpcProfile|ReplySuggestion|\bplanned\b"
                         r"|ReplyAnalysis|Grade|\bimport suggest\b|\bsuggest\.|roleplay|rp_briefing|rp_faq", code)


def test_persona_voice_comes_from_the_role_plays_that_play_them(excon, loop):
    """The character a persona is in the role plays (linked by ExCon or found by name) gives Suggest a reply
    and ChatSIMEX its voice, played yet or not and whether or not anyone is assigned to play it."""
    from ops import desk_chat
    from ops.models import Sender
    setup(excon)
    ta = Team.objects.get(code="a")
    s = Sender.objects.get(local_part="donotreply")
    s.display_name = "Grace Mwangi"
    s.save()
    by_name = RolePlay.objects.create(key="4-1", serial="4-1", seq=1, title="Meeting with Ms Mwangi",
                                      role="Grace Mwangi, district commissioner",
                                      script="Grace is brisk and impatient; she wants the airport reopened.")
    RolePlaySlot.objects.create(play=by_name, start=timezone.now() + timedelta(hours=3), minutes=10)  # still to come
    RolePlay.objects.create(key="4-2", serial="4-2", seq=2, title="Press scrum", role="Spokesperson",
                            script="Stays calm and speaks in proverbs.", personas="donotreply")
    RolePlay.objects.create(key="4-3", serial="4-3", seq=3, title="Checkpoint", role="Driver Kofi", script="You are Kofi.")
    assert [(p.key, why) for p, why in roleplay.characters(s)] == [("4-2", "linked"), ("4-1", "name")]
    assert not by_name.players.exists()

    deliver_now("3-13")
    _, r = team_writes(participant("a", "Jane"), ta, "When does the airport reopen?")
    thread = Mail.objects.get(team=None, owner=None, folder="inbox", desk_response=r).thread
    desk_client().post("/inbox/desk/suggest/", {"thread": thread}, headers=FETCH)
    first = loop.calls[0]["messages"][0]["content"][0]["text"]
    assert "The character as the role plays play it" in first
    assert "brisk and impatient" in first and "speaks in proverbs" in first
    assert "rp:4-2 [NOT YET played with the team (not scheduled)] (ExCon: this persona)" in first
    assert "rp:4-1 [NOT YET played with the team: due" in first and "(found by name)" in first
    assert "You are Kofi" not in first
    assert "whoever plays it" in loop.calls[0]["system"][0]["text"]

    brief = desk_chat.persona_brief(desk_chat.Scope(ta), s)
    assert "The character as the role plays play it" in brief and "brisk and impatient" in brief
    assert "speaks in proverbs" in brief and "You are Kofi" not in brief

    # ExCon links a persona on the role play's page; one that isn't a persona is flagged
    kofi = RolePlay.objects.get(key="4-3")
    page = excon.get(f"/roleplay/{by_name.id}/").content.decode()
    assert "Also found by name: Grace Mwangi (donotreply)" in page
    r = excon.post(f"/roleplay/{kofi.id}/", {"personas": "donotreply nobody-here", "title": kofi.title}, follow=True)
    kofi.refresh_from_db()
    assert kofi.personas == "donotreply, nobody-here" and "No persona with the username nobody-here" in r.content.decode()
    assert "4-3" in [p.key for p, _ in roleplay.characters(s)]
