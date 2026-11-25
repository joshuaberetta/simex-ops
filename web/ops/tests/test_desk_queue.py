"""The persona desk's queue (desk.states, desk.mark, ops/desk_ai.py): what still needs an
answer first, whoever on the desk read it, with follow-ups, who took what and Claude's reading."""

import re
from datetime import timedelta

from django.utils import timezone

from ops import ai, conf, desk, desk_ai, push
from ops.models import DeskConversation, DeskNotice, Mail, NpcProfile, NpcReply, PushSubscription, Sender, Team
from ops.tests.test_ai import FakeClient
from ops.tests.test_desk import FETCH, desk_client, setup, team_writes
from ops.tests.test_ops import deliver_now, participant


def rows(d, folder="inbox"):
    page = d.get(f"/inbox/desk/?folder={folder}")
    return page, {r["thread"]: r["state"] for r in page.context["rows"]}


def answer(d, box, team, body="Open until 18:00.", persona="donotreply"):
    r = d.post("/inbox/desk/send/", {"from": f"p:{persona}", "to": f"t:{team.id}", "subject": "Re: News", "body": body,
                                     "in_reply_to": box.id}, headers=FETCH)
    assert r.json()["ok"], r.json()


def test_waiting_until_a_persona_answers_whoever_read_it(excon):
    setup(excon)
    deliver_now("3-13")
    ta = Team.objects.get(code="a")
    pa = participant("a")
    got, _ = team_writes(pa, ta)
    d = desk_client()
    page, st = rows(d)
    assert st[got.thread]["section"] == desk.ATTENTION and st[got.thread]["waiting"]
    assert page.context["sections"]["attention"]["n"] == 1 and page.context["desk_side"]["attention"] == 1
    assert b"Needs attention" in page.content and b"Waiting" in page.content
    # Reading it isn't answering it.
    t = d.get(f"/inbox/desk/t/{got.thread}/")
    assert t.context["desk_state"]["waiting"] and b"Waiting for an answer" in t.content
    assert rows(d)[1][got.thread]["section"] == desk.ATTENTION
    box = Mail.objects.get(team=None, folder="inbox")
    answer(d, box, ta)
    page, st = rows(d)
    assert st[got.thread]["section"] == desk.REST and not st[got.thread]["waiting"]
    assert page.context["desk_side"]["attention"] == 0
    # The team writes again: waiting again.
    team_copy = Mail.objects.filter(team=ta, owner=None, folder="inbox").order_by("-id").first()
    pa.post("/inbox/send/", {"to": "p:donotreply", "subject": "Re: News", "body": "And tomorrow?", "in_reply_to": team_copy.id})
    assert rows(d)[1][got.thread]["section"] == desk.ATTENTION


def test_no_reply_needed_follow_up_and_take(excon):
    setup(excon)
    deliver_now("3-13")
    ta, tb = Team.objects.get(code="a"), Team.objects.get(code="b")
    pa, pb = participant("a"), participant("b")
    got_a, _ = team_writes(pa, ta, "Thanks, noted.")
    got_b, _ = team_writes(pb, tb, "Where is the convoy?")
    d, e = desk_client(), desk_client("Ben Desk", "ben.desk")
    _, st = rows(d)
    assert [t for t, s in st.items() if s["section"] == desk.ATTENTION] == [got_a.thread, got_b.thread]  # oldest first
    # No reply needed, from the conversation: on to the next one waiting.
    r = d.post("/inbox/desk/mark/", {"threads": got_a.thread, "act": "done", "next": f"/inbox/desk/t/{got_a.thread}/",
                                     "next_done": f"/inbox/desk/t/{got_b.thread}/"})
    assert r.url == f"/inbox/desk/t/{got_b.thread}/"
    s = rows(e)[1][got_a.thread]
    assert s["section"] == desk.REST and s["done_by"] == "Anna Desk"
    # Another message from the team brings it back.
    team_copy = Mail.objects.get(team=ta, owner=None, folder="inbox")
    pa.post("/inbox/send/", {"to": "p:donotreply", "subject": "Re: News", "body": "One more thing?", "in_reply_to": team_copy.id})
    assert rows(e)[1][got_a.thread]["section"] == desk.ATTENTION
    # Take it: the others see who has it.
    assert d.post("/inbox/desk/mark/", {"threads": got_b.thread, "act": "take"}, headers=FETCH).json()["n"] == 1
    assert rows(d)[1][got_b.thread]["mine"] and rows(e)[1][got_b.thread]["owner"] == "Anna Desk"
    assert not rows(e)[1][got_b.thread]["mine"]
    # Answered, then a follow-up in 30 minutes: later, then due, then done once a persona writes.
    answer(d, Mail.objects.get(team=None, folder="inbox", desk_team=tb), tb, "On its way.")
    d.post("/inbox/desk/mark/", {"threads": got_b.thread, "act": "follow", "minutes": "30", "note": "Convoy ETA"})
    s = rows(d)[1][got_b.thread]
    assert s["section"] == desk.LATER and s["follow"]["note"] == "Convoy ETA" and not s["follow"]["due"]
    DeskConversation.objects.filter(thread=got_b.thread).update(follow_at=timezone.now() - timedelta(minutes=1))
    assert rows(d)[1][got_b.thread]["section"] == desk.ATTENTION
    answer(d, Mail.objects.filter(team=None, folder="inbox", desk_team=tb).first(), tb, "ETA 16:00.", persona="cnn")
    s = rows(d)[1][got_b.thread]
    assert s["section"] == desk.REST and s["follow"] is None
    # Only the desk's own conversations, and only the desk.
    assert d.post("/inbox/desk/mark/", {"threads": "nope", "act": "done"}, headers=FETCH).json()["n"] == 0
    assert d.post("/inbox/desk/mark/", {"threads": got_b.thread, "act": "x"}, headers=FETCH).status_code == 400
    assert pa.post("/inbox/desk/mark/", {"threads": got_b.thread, "act": "done"}).status_code == 403


def test_no_reply_needed_from_the_toolbars(excon):
    """No reply needed is a ✓ in the conversation's toolbar (Reopen once marked) and in the
    list's toolbar for the selected rows, many at once."""
    setup(excon)
    deliver_now("3-13")
    teams = [Team.objects.get(code=c) for c in "abc"]
    got = [team_writes(participant(t.code), t, "Thanks, noted.")[0] for t in teams]
    d = desk_client()
    t = d.get(f"/inbox/desk/t/{got[0].thread}/")
    bar = t.content.decode().split('class="gm-toolbar"')[1].split("</div>")[0]
    assert "data-desk-done" in bar and 'name="next_done"' in bar
    page = d.get("/inbox/desk/")
    assert b"data-desk-bulk-done" in page.content
    # The selected rows, at once: back to the list, told how many.
    r = d.post("/inbox/desk/mark/", {"threads": [g.thread for g in got[:2]], "act": "done", "next": "/inbox/desk/"},
               follow=True)
    assert r.redirect_chain[-1][0] == "/inbox/desk/" and b"2 conversations marked as needing no reply." in r.content
    _, st = rows(d)
    assert [st[g.thread]["section"] for g in got] == [desk.REST, desk.REST, desk.ATTENTION]
    # Marked: the toolbar offers Reopen instead.
    bar = d.get(f"/inbox/desk/t/{got[0].thread}/").content.decode().split('class="gm-toolbar"')[1].split("</div>")[0]
    assert "data-desk-done" not in bar and 'value="reopen"' in bar
    # Not on the teams' own mailboxes.
    assert b"data-desk-bulk-done" not in participant("d").get("/inbox/").content


def test_ai_persona_about_to_answer_is_later(excon):
    setup(excon)
    conf.put("npc_enabled", "1")
    conf.put("npc_scope", "all")
    NpcProfile.objects.create(sender=Sender.objects.get(local_part="donotreply"), enabled=True, timing="custom",
                              min_minutes=30, max_minutes=30)
    deliver_now("3-13")
    got, _ = team_writes(participant("a"), Team.objects.get(code="a"))
    d = desk_client()
    s = rows(d)[1][got.thread]
    assert s["section"] == desk.LATER and s["npc"]["status"] == "queued"
    page = d.get("/inbox/desk/").content.decode()
    assert "AI persona answers at" in page and "no reply needed" in page
    assert "no need to answer it here" in d.get(f"/inbox/desk/t/{got.thread}/").content.decode()
    NpcReply.objects.update(status="held")
    assert rows(d)[1][got.thread]["section"] == desk.ATTENTION
    # Its timing drew no answer (or ExCon skipped it): a person answers, and the desk says so.
    NpcReply.objects.update(status="skipped")
    page, st = rows(d)
    assert st[got.thread]["section"] == desk.ATTENTION and st[got.thread]["npc"]["status"] == "skipped"
    assert "AI persona not answering this one" in page.content.decode()


def test_ai_persona_about_to_answer_is_no_news_for_the_desk(excon, django_capture_on_commit_callbacks):
    """A team writing to an AI persona that will answer: no push to the desk's devices, and the
    bell says nobody needs to answer."""
    setup(excon)
    conf.put("npc_enabled", "1")
    conf.put("npc_scope", "all")
    NpcProfile.objects.create(sender=Sender.objects.get(local_part="donotreply"), enabled=True, timing="custom",
                              min_minutes=30, max_minutes=30)
    deliver_now("3-13")
    ta, pa = Team.objects.get(code="a"), participant("a")
    pushed = []
    push.set_sender(lambda info, data: pushed.append(data) or 201)
    push.RUN_INLINE = True
    try:
        d = desk_client()
        PushSubscription.objects.create(person=d.person, endpoint="https://fcm.googleapis.com/x", p256dh="k", auth="a")
        with django_capture_on_commit_callbacks(execute=True):
            got, _ = team_writes(pa, ta)
        assert NpcReply.objects.get().status == "queued" and not pushed
        d.post("/inbox/desk/mark/", {"threads": got.thread, "act": "watch"})
        with django_capture_on_commit_callbacks(execute=True):
            team_writes(pa, ta, "And the bridge?")
        assert NpcReply.objects.count() == 1 and not pushed  # the queued answer answers this one too
        n = DeskNotice.objects.filter(user=d.person.user, kind="reply").latest("id")
        assert "AI persona" in n.body and "no need to answer" in n.body
        # The AI persona off: the desk hears of the team's message as usual.
        NpcProfile.objects.update(enabled=False)
        with django_capture_on_commit_callbacks(execute=True):
            team_writes(pa, ta, "Anyone?")
        assert pushed
    finally:
        push.set_sender(None)
        push.RUN_INLINE = False


def triage_answer(need):
    def answer(kw):
        assert kw["system"] == ai.system("desk")
        return {"need": need, "urgency": "high" if need == "reply" else "low", "ask": "Road status",
                "reason": "Asks a question."}, "end_turn"
    return answer


def test_claude_reads_what_needs_an_answer(excon):
    setup(excon)
    deliver_now("3-13")
    ta, tb = Team.objects.get(code="a"), Team.objects.get(code="b")
    got_a, _ = team_writes(participant("a"), ta, "Thanks, noted.")
    got_b, _ = team_writes(participant("b"), tb, "Where is the convoy?")
    c = FakeClient(triage_answer("none"))
    ai.set_client(c)
    conf.put("ai_enabled", "1")
    try:
        conf.put("desk_triage", "0")
        assert desk_ai.tick(5) == 0 and not c.calls
        conf.put("desk_triage", "1")
        assert desk_ai.tick(1) == 1
        assert "Thanks, noted." in c.calls[0]["messages"][0]["content"][0]["text"]
        c.answer = triage_answer("reply")
        assert desk_ai.tick(5) == 1 and desk_ai.tick(5) == 0  # once per team message
        d = desk_client()
        page, st = rows(d)
        assert st[got_a.thread]["section"] == desk.QUIET and st[got_a.thread]["triage"]["need"] == "none"
        assert st[got_b.thread]["section"] == desk.ATTENTION and st[got_b.thread]["triage"]["urgency"] == "high"
        assert b"Urgent" in page.content and b"No reply needed?" in page.content
        assert list(st)[:2] == [got_b.thread, got_a.thread]  # needs attention first
        # Answered: the reading no longer shows. Manual mode: only when asked.
        answer(d, Mail.objects.get(team=None, folder="inbox", desk_team=tb), tb)
        assert rows(d)[1][got_b.thread]["triage"] is None
        conf.put("ai_auto", "0")
        DeskConversation.objects.update(ai_upto=0, ai_status="")
        assert desk_ai.tick(5) == 0
        d.post("/inbox/desk/mark/", {"threads": got_a.thread, "act": "triage"})
        assert desk_ai.tick(5) == 1
    finally:
        ai.set_client(None)


def test_cleared_with_the_mailboxes(excon):
    setup(excon)
    deliver_now("3-13")
    ta = Team.objects.get(code="a")
    got, _ = team_writes(participant("a"), ta)
    d = desk_client()
    d.post("/inbox/desk/mark/", {"threads": got.thread, "act": "take"})
    assert DeskConversation.objects.count() == 1
    from ops import reset
    reset.clear_team(ta)
    assert not DeskConversation.objects.exists()


def test_excon_hears_of_a_team_waiting_too_long(excon):
    from ops.models import Notice
    setup(excon)
    deliver_now("3-13")
    ta = Team.objects.get(code="a")
    got, _ = team_writes(participant("a"), ta)
    assert desk.tick() == 0  # not 20 minutes yet
    Mail.objects.filter(team=None, folder="inbox").update(created_at=timezone.now() - timedelta(minutes=25))
    conf.put("desk_wait_minutes", "0")
    assert desk.tick() == 0
    conf.put("desk_wait_minutes", "20")
    desk.tick()
    desk.tick()  # once per message waiting
    n = Notice.objects.get(key__startswith="desk-wait:")
    assert n.team == ta and "Team A waiting 20 min" in n.title and n.url == f"/inbox/desk/t/{got.thread}/"
    assert "Nobody" in n.note
    # Answered: nothing more.
    d = desk_client()
    answer(d, Mail.objects.get(team=None, folder="inbox"), ta)
    Notice.objects.all().delete()
    desk.tick()
    assert not Notice.objects.exists()
    assert excon.post("/settings/", {"desk_wait_minutes": "30", "grace_minutes": "10", "max_upload_mb": "10"}).status_code == 302
    assert conf.get("desk_wait_minutes") == "30"


def test_desk_dashboard(excon):
    from ops import desk_stats
    setup(excon)
    deliver_now("3-13")
    ta, tb = Team.objects.get(code="a"), Team.objects.get(code="b")
    got_a, _ = team_writes(participant("a"), ta, "Where is the convoy?")
    got_b, _ = team_writes(participant("b"), tb, "Thanks, noted.")
    Mail.objects.filter(team=None, folder="inbox").update(created_at=timezone.now() - timedelta(minutes=30))
    d = desk_client()
    d.post("/inbox/desk/mark/", {"threads": got_b.thread, "act": "done"})
    answer(d, Mail.objects.get(team=None, folder="inbox", desk_team=ta), ta)
    got_c, _ = team_writes(participant("c"), Team.objects.get(code="c"), "Road status?")
    Mail.objects.filter(team=None, folder="inbox", desk_team__code="c").update(created_at=timezone.now() - timedelta(minutes=25))
    s = desk_stats.compute("live")
    assert (s["received"], s["answered"], s["closed"], s["waiting"], s["attention"]) == (3, 1, 1, 1, 1)
    assert s["median"] == 30 and s["longest"] == 25 and s["over_target"] == 1 and s["untaken"] == 1
    assert s["oldest"][0]["thread"] == got_c.thread and s["oldest"][0]["team"].code == "c"
    rows = {r["team"].code: r for r in s["teams"]}
    assert rows["a"]["answered"] == 1 and rows["a"]["median"] == 30 and rows["c"]["waiting"] == 1
    assert s["teams"][0]["team"].code == "c"  # the team waiting longest first
    people = {p["name"]: p for p in s["people"]}
    assert people["Anna Desk"]["answers"] == 1 and people["Anna Desk"]["online"]
    assert s["personas"][0]["local"] == "donotreply" and s["personas"][0]["waiting"] == 1
    assert desk_stats.compute("test")["received"] == 0
    page = d.get("/inbox/desk/dashboard/")
    assert page.status_code == 200 and b"Keeping up with the teams" in page.content and b"Waiting longest" in page.content
    assert page.context["desk_tabs"]["current"] == "dashboard"
    assert excon.get("/inbox/desk/dashboard/").status_code == 200
    assert participant("d").get("/inbox/desk/dashboard/").status_code == 403


def test_desk_dashboard_requests_to_the_personas(excon):
    from ops import desk_stats
    from ops.models import ReplyAnalysis
    setup(excon)
    deliver_now("3-13")
    ta = Team.objects.get(code="a")
    _, resp = team_writes(participant("a"), ta, "We need 3 trucks. We will deploy at 14:00.")
    ReplyAnalysis.objects.create(response=resp, summary="Asks for trucks.", asks=["3 trucks"], commitments=["Deploy at 14:00"],
                                 facts=["200 families displaced"], flags=["escalate", "no_action"], clarity=1,
                                 ai_written=95, tone="stressed")
    s = desk_stats.compute("live")
    req = s["requests"][0]
    assert req["persona"]["local"] == "donotreply" and req["open"] == 1 and req["asks"][0]["text"] == "3 trucks"
    assert s["commitments"][0]["text"] == "Deploy at 14:00" and s["facts"][0]["text"] == "200 families displaced"
    assert s["flags"][0]["flags"] == ["Needs ExCon attention"]  # the desk's flags only, no judgement of the team
    d = desk_client()
    page = d.get("/inbox/desk/dashboard/").content.decode()
    assert "3 trucks" in page and "Deploy at 14:00" in page and "200 families" in page
    # Never tone, quality, AI-written. (95 standing alone: thread ids and times hold "95" by chance.)
    assert "stressed" not in page and not re.search(r"\b95\b", page) and "No action" not in page
    answer(d, Mail.objects.get(team=None, folder="inbox"), ta)
    assert desk_stats.compute("live")["requests"][0]["open"] == 0


def test_newest_first_like_any_email(excon):
    """The desk's Inbox in sections by default, or newest first, each login's choice kept."""
    setup(excon)
    deliver_now("3-13")
    ta, tb = Team.objects.get(code="a"), Team.objects.get(code="b")
    got_a, _ = team_writes(participant("a"), ta)
    got_b, _ = team_writes(participant("b"), tb)
    d = desk_client()
    answer(d, Mail.objects.filter(team=None, folder="inbox", desk_team=tb).get(), tb)
    page = d.get("/inbox/desk/")
    order = [r["thread"] for r in page.context["rows"]]
    assert page.context["sections"] and page.context["desk_order"] == "sections"
    assert order.index(got_a.thread) < order.index(got_b.thread)  # waiting first
    page = d.get("/inbox/desk/?order=newest")
    order = [r["thread"] for r in page.context["rows"]]
    assert page.context["sections"] is None and page.context["desk_order"] == "newest"
    assert order.index(got_b.thread) < order.index(got_a.thread)  # the latest at the top
    assert b"gm-section-attention" not in page.content and b"Waiting" in page.content  # rows still say where they stand
    page = d.get("/inbox/desk/")  # kept
    assert page.context["desk_order"] == "newest" and page.context["sections"] is None
    t = d.get(f"/inbox/desk/t/{got_b.thread}/")  # the arrows follow the same order
    assert t.status_code == 200
    page = d.get("/inbox/desk/?order=sections")
    assert page.context["sections"] and page.context["desk_order"] == "sections"
