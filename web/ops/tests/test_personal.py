"""Members' own mailboxes (/inbox/me/, Settings → personal mailboxes): a copy of the team's
mail, personal mail between teammates and with their team leader, the leader's read-only view
of them, and answers to personas that the team inbox always has too."""

from django.test import Client

from ops import conf, push
from ops.models import Mail, Message, Response, Team, TeamRole
from ops.tests.test_leaders import leader, setup_exercise
from ops.tests.test_ops import deliver_now, participant

AJAX = {"headers": {"x-requested-with": "fetch"}}


def send(c, base="/inbox/me", **data):
    r = c.post(f"{base}/send/", data, **AJAX)
    assert r.status_code == 200, r.content
    return r.json()


def test_team_mail_reaches_every_members_own_mailbox(excon):
    setup_exercise(excon)
    pa, po = participant("a", "Jane"), participant("a", "Omar")
    ta = Team.objects.get(code="a")
    deliver_now("1-15")
    team_mail = Mail.objects.get(owner=None, team=ta, folder="inbox")
    mine = Mail.objects.get(owner=pa.person, folder="inbox")
    assert (mine.thread, mine.message, mine.subject) == (team_mail.thread, team_mail.message, team_mail.subject)
    assert Mail.objects.filter(owner=po.person, folder="inbox", thread=team_mail.thread).exists()
    assert not Mail.objects.filter(owner__team__code="b", team=ta).exists()  # teams never cross
    page = pa.get("/inbox/me/")
    assert page.context["v"].owner == pa.person and [r["thread"] for r in page.context["rows"]] == [team_mail.thread]
    assert page.context["member_tabs"]["current"] == "me" and page.context["my_address"] == "jane@a.teams.simex.kobolabs.dev"
    # Reading it here counts as the team reading the inject; the team inbox's copy stays unread.
    pa.get(f"/inbox/me/t/{mine.thread}/")
    assert Message.objects.get(pk=team_mail.message_id).opened_at
    assert Mail.objects.get(pk=team_mail.pk).read_at is None
    assert po.get(f"/inbox/me/t/{mine.thread}/", follow=False).status_code == 200  # Omar's own copy
    assert pa.get("/inbox/poll/").json()["me_unread"] == 0


def test_answer_from_own_mailbox_is_the_teams_cc_reaches_a_teammate(excon):
    setup_exercise(excon)
    pa, po = participant("a", "Jane"), participant("a", "Omar")
    la = leader("a")
    ta = Team.objects.get(code="a")
    deliver_now("1-15")
    mine = Mail.objects.get(owner=pa.person, folder="inbox")
    reach = lambda k: pa.get(f"/inbox/me/lookup/?key={k}").status_code == 200  # noqa: E731 (the book grows with use)
    assert reach(f"m:{po.person.id}") and not reach(f"m:{pa.person.id}") and reach(f"t:{ta.id}")
    send(pa, to="p:undac-a-tl", cc=f"m:{po.person.id}", subject="Re: Update", body="Plan by 15:00", in_reply_to=mine.id)
    resp = Response.objects.get()
    team_copy = resp.mail
    assert (team_copy.owner, team_copy.folder, team_copy.thread) == (None, "sent", mine.thread)
    assert resp.message == mine.message and resp.match == "exact" and resp.person == pa.person
    assert Mail.objects.filter(owner=pa.person, folder="sent", thread=mine.thread).exists()
    omar = Mail.objects.get(owner=po.person, folder="inbox", body="Plan by 15:00")
    assert omar.from_addr == "jane@a.teams.simex.kobolabs.dev" and omar.cc_label.startswith("Team A (Omar)")
    # Omar's reply all: Jane and the leader persona, not himself.
    page = po.get(f"/inbox/me/t/{omar.thread}/")
    assert set(page.context["thread_js"]["replyAll"]) == {f"m:{pa.person.id}", "p:undac-a-tl"}
    # The leader has it from Jane's own address, and answers her only: the Response's answer.
    got = Mail.objects.get(owner=la.person, folder="inbox", body="Plan by 15:00")
    page = la.get(f"/inbox/t/{got.thread}/")
    assert page.context["thread_js"]["reply"] == [f"m:{pa.person.id}"] and page.context["can_reply"]
    send(la, base="/inbox", to=f"m:{pa.person.id}", subject="Re: Re: Update", body="Good, Jane.", in_reply_to=got.id)
    m = Message.objects.get(author=la.person)
    assert (m.kind, m.response, m.status) == ("reply", resp, "sent")
    assert Mail.objects.filter(owner=pa.person, folder="inbox", body="Good, Jane.", thread=mine.thread, message=m).exists()
    assert Mail.objects.filter(owner=la.person, folder="sent", body="Good, Jane.").exists()
    assert not Mail.objects.filter(owner=None, body="Good, Jane.").exists()  # not to the team
    assert not Mail.objects.filter(owner=po.person, body="Good, Jane.").exists()


def test_personal_mail_between_teammates_and_with_the_leader(excon, settings, django_capture_on_commit_callbacks):
    settings.VAPID_PRIVATE_KEY = "Uq1-1K7JXzT4Bo8SzbsxJAX5BMAI0uBkN6lSV4jL8A8"
    setup_exercise(excon)
    pa, po, pz = participant("a", "Jane"), participant("a", "Omar"), participant("a", "Zoe")
    la = leader("a")
    ta = Team.objects.get(code="a")
    sent = []
    push.set_sender(lambda info, body: sent.append(body) or 201)
    push.RUN_INLINE = True
    from ops.models import PushSubscription
    PushSubscription.objects.create(person=po.person, endpoint="https://fcm.googleapis.com/x", p256dh="k", auth="a")
    with django_capture_on_commit_callbacks(execute=True):
        send(pa, to=f"m:{po.person.id}", subject="Logistics", body="Can you call the airport?")
    try:
        assert len(sent) == 1 and "/inbox/me/t/" in sent[0]  # Omar's device, to his own mailbox
    finally:
        push.set_sender(None)
        push.RUN_INLINE = False
    got = Mail.objects.get(owner=po.person, folder="inbox")
    assert got.subject == "Logistics" and not Response.objects.exists()  # not an answer to a persona
    assert not Mail.objects.filter(owner=None, subject="Logistics").exists()  # not in the team inbox
    assert not Mail.objects.filter(owner=pz.person).exists()
    # To the team from her own mailbox: the team inbox and every other member's own.
    send(pa, to=f"t:{ta.id}", subject="Shift plan", body="Me first.")
    assert Mail.objects.filter(owner=None, folder="inbox", subject="Shift plan").exists()
    owners = {m.owner_id for m in Mail.objects.filter(subject="Shift plan", folder="inbox", owner__isnull=False)}
    assert {po.person.id, pz.person.id} <= owners and pa.person.id not in owners  # not back to her
    assert not Mail.objects.filter(subject="Shift plan", owner__role="tl").exists()
    # The leader writes to two members, and copies the team in another message.
    send(la, base="/inbox", to=f"m:{pa.person.id}", cc=f"m:{po.person.id}", subject="You two", body="Go to the port.")
    assert set(Mail.objects.filter(subject="You two", folder="inbox").values_list("owner_id", flat=True)) == {
        pa.person.id, po.person.id}
    m = Message.objects.get(subject="You two")
    assert m.kind == "leader" and m.status == "sent" and m.author == la.person
    send(la, base="/inbox", to=f"t:{ta.id}", cc=f"m:{pz.person.id}", subject="All", body="Briefing.")
    assert Mail.objects.filter(owner=None, subject="All").count() == 1
    assert Mail.objects.filter(owner=pz.person, subject="All").count() == 1  # once, though also in Cc
    # Teams never cross: another team's member isn't in the book and can't be written to.
    pb = participant("b", "Bea")
    r = pa.post("/inbox/me/send/", {"to": f"m:{pb.person.id}", "subject": "x", "body": "y"}, **AJAX)
    assert r.status_code == 400 and not Mail.objects.filter(owner=pb.person).exists()


def test_leader_sees_members_roles_and_their_mailboxes_read_only(excon):
    setup_exercise(excon)
    pa, la = participant("a", "Jane"), leader("a")
    pa.person.team_role = TeamRole.objects.create(name="Deputy Team Leader")
    pa.person.save()
    deliver_now("1-15")
    page = la.get("/inbox/members/")
    assert page.status_code == 200 and "Deputy Team Leader" in page.content.decode()
    assert f"/inbox/members/{pa.person.id}/" in page.content.decode()
    mine = Mail.objects.get(owner=pa.person, folder="inbox")
    box = la.get(f"/inbox/members/{pa.person.id}/")
    assert box.context["v"].readonly and [r["thread"] for r in box.context["rows"]] == [mine.thread]
    la.get(f"/inbox/members/{pa.person.id}/t/{mine.thread}/")
    assert Mail.objects.get(pk=mine.pk).read_at is None  # not marked read for her
    assert la.post(f"/inbox/members/{pa.person.id}/send/", {"to": "p:x", "body": "y"}).status_code == 404
    pb = participant("b", "Bea")
    assert la.get(f"/inbox/members/{pb.person.id}/").status_code == 404  # another team's member
    assert pa.get(f"/inbox/members/{pa.person.id}/").status_code == 403  # leaders only
    assert excon.get(f"/inbox/as/{pa.person.team_id}/member/{pa.person.id}/").status_code == 200
    assert "Deputy Team Leader" in la.get(f"/inbox/members/{pa.person.id}/").content.decode()  # in the side bar


def test_personal_mailboxes_off(excon):
    conf.put("personal_mailboxes", "0")
    setup_exercise(excon)
    pa, po = participant("a", "Jane"), participant("a", "Omar")
    deliver_now("1-15")
    assert not Mail.objects.filter(owner__role="member").exists()
    assert pa.get("/inbox/me/").url == "/inbox/"
    page = pa.get("/inbox/")
    assert not page.context.get("member_tabs") and not any(b["kind"] == "member" for b in page.context["simex_js"]["book"])
    assert Client().get("/inbox/me/").status_code == 302


def test_members_choose_the_from_address(excon):
    setup_exercise(excon)
    pa, po = participant("a", "Jane"), participant("a", "Omar")
    ta = Team.objects.get(code="a")
    deliver_now("1-15")
    choices = pa.get("/inbox/").context["from_choices"]
    assert [c["addr"] for c in choices] == ["a@teams.simex.kobolabs.dev", "jane@a.teams.simex.kobolabs.dev"]
    assert pa.get("/inbox/").context["from_default"] == "team" and pa.get("/inbox/me/").context["from_default"] == "me"
    # From My mail, as the team: the team inbox's Sent from the team's address, and her own Sent too.
    mine = Mail.objects.get(owner=pa.person, folder="inbox")
    j = send(pa, **{"from": "team"}, to="p:undac-a-tl", subject="Re: Update", body="As the team.", in_reply_to=mine.id)
    r = Response.objects.get()
    assert (r.mail.owner, r.mail.from_addr, r.match) == (None, "a@teams.simex.kobolabs.dev", "exact")
    assert Mail.objects.filter(owner=pa.person, folder="sent", body="As the team.", thread=mine.thread).exists()
    assert j["url"].startswith("/inbox/me/t/")
    # From the team inbox, as herself: her own Sent, and the team's (an answer to a persona).
    team_mail = Mail.objects.get(owner=None, folder="inbox", team=ta)
    send(pa, base="/inbox", **{"from": "me"}, to="p:undac-a-tl", subject="Re: Update", body="As me.",
         in_reply_to=team_mail.id)
    r2 = Response.objects.latest("id")
    assert r2.mail.owner is None and r2.mail.from_addr == "jane@a.teams.simex.kobolabs.dev"
    assert Mail.objects.filter(owner=pa.person, folder="sent", body="As me.").exists()
    # Personal mail sent from the team tab stays out of the team inbox.
    j = send(pa, base="/inbox", **{"from": "me"}, to=f"m:{po.person.id}", subject="Just you", body="x")
    assert not Mail.objects.filter(owner=None, subject="Just you").exists() and j["url"].startswith("/inbox/me/")
    # The team writing to itself makes no sense.
    r = pa.post("/inbox/send/", {"from": "team", "to": f"t:{ta.id}", "subject": "x", "body": "y"}, **AJAX)
    assert r.status_code == 400


def test_leader_labels_mail_from_the_team_account_and_personal_accounts(excon):
    from ops.models import MailLabel, ThreadLabel
    setup_exercise(excon)
    pa, la = participant("a", "Jane"), leader("a")
    deliver_now("1-15")
    send(pa, base="/inbox", to="p:undac-a-tl", subject="From the team", body="x")
    send(pa, to="p:undac-a-tl", subject="From me", body="y")
    labels = {lb.auto: lb for lb in MailLabel.objects.filter(owner=la.person)}
    assert labels["team"].name == "Team account" and labels["member"].name == "Personal account"
    on = {(tl.label.auto, Mail.objects.filter(owner=la.person, thread=tl.thread).first().subject)
          for tl in ThreadLabel.objects.filter(label__owner=la.person)}
    assert on == {("team", "From the team"), ("member", "From me")}
    labels["member"].name = "Members"
    labels["member"].save()
    send(pa, to="p:undac-a-tl", subject="Again", body="z")
    assert MailLabel.objects.filter(owner=la.person).count() == 2  # renamed, still the one used


def test_team_tab_announces_personal_mail_too(excon):
    setup_exercise(excon)
    pa, po = participant("a", "Jane"), participant("a", "Omar")
    since = pa.get("/inbox/poll/").json()["latest"]
    deliver_now("1-15")
    send(po, to=f"m:{pa.person.id}", subject="Psst", body="x")
    new = pa.get(f"/inbox/poll/?since={since}").json()["new"]
    assert sorted((n["subject"], n["url"].split("/t/")[0]) for n in new) == [("Psst", "/inbox/me"),
                                                                             ("Update for Team A", "/inbox")]


def test_one_message_per_participant_lands_in_their_own_mailbox_only(excon):
    """each_person with personal mailboxes: each member's item in their own mailbox, none in the
    team inbox (it used to hold one copy per member); their answer is still the team's."""
    setup_exercise(excon)
    pa, po = participant("a", "Jane"), participant("a", "Omar")
    ta = Team.objects.get(code="a")
    from ops.models import Inject
    Inject.objects.filter(mel_id="3-13").update(delivery="each_person")
    deliver_now("3-13")
    assert not Mail.objects.filter(owner=None, team=ta, folder="inbox").exists()
    mine = Mail.objects.get(owner=pa.person, folder="inbox")
    assert mine.message.person == pa.person and mine.message.inbox_owner == pa.person
    assert Mail.objects.filter(owner=po.person, folder="inbox").count() == 1  # Omar's own, not Jane's too
    assert [r["thread"] for r in pa.get("/inbox/me/").context["rows"]] == [mine.thread]
    pa.get(f"/inbox/me/t/{mine.thread}/")
    assert Message.objects.get(pk=mine.message_id).opened_at  # counts as read
    send(pa, to=f"p:{mine.persona}", subject="Re: it", body="Thanks", in_reply_to=mine.id)
    resp = Response.objects.get()
    assert resp.message == mine.message and resp.match == "exact" and resp.mail.owner is None  # the team's Sent
    # Without personal mailboxes: the team inbox, addressed to each member, as before.
    conf.put("personal_mailboxes", "0")
    Mail.objects.all().delete()
    Inject.objects.filter(mel_id="3-13").update(status="scheduled", attempt=2)
    deliver_now("3-13")
    assert set(Mail.objects.filter(folder="inbox", team=ta).values_list("owner", "to_person")) >= {
        (None, pa.person.id), (None, po.person.id)}
