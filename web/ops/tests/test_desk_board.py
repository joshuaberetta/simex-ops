"""The persona desk's Assign tab: the board (desk_board.board, moves through desk.mark) and the
default assignments of personas to people on the desk, by team (DeskDefault, desk.auto_assign,
desk.apply_defaults)."""

from django.contrib.auth import get_user_model

from ops import desk, desk_board
from ops.models import DeskConversation, DeskDefault, DeskNotice, Mail, Sender, Team
from ops.tests.test_desk import FETCH, desk_client, setup, team_writes
from ops.tests.test_desk_assign import again, titles
from ops.tests.test_ops import deliver_now, participant


def desk_thread(team):
    return Mail.objects.filter(team=None, desk_team=team).order_by("id").values_list("thread", flat=True).first()


def owner(thread):
    c = DeskConversation.objects.filter(thread=thread).first()
    return (c.owner, c.auto) if c else (None, False)


def test_default_owner_by_persona_and_team(excon):
    setup(excon)
    d, e = desk_client(), desk_client("Ben Desk", "ben.desk")
    anna, ben = d.person.user, e.person.user
    ta, tb = Team.objects.get(code="a"), Team.objects.get(code="b")
    s = Sender.objects.get(local_part="donotreply")
    DeskDefault.objects.create(sender=s, team=None, user=anna)
    DeskDefault.objects.create(sender=s, team=tb, user=ben)
    assert desk.default_owner("donotreply", ta.id) == anna and desk.default_owner("DoNotReply", tb.id) == ben
    assert desk.default_owner("nobody-here", ta.id) is None
    # A {team} persona: each team's own username.
    ops = Sender.objects.create(local_part="ops-{team}", display_name="Ops {team}")
    DeskDefault.objects.create(sender=ops, team=ta, user=ben)
    assert desk.default_owner(f"ops-{ta.code}", ta.id) == ben and desk.default_owner(f"ops-{tb.code}", tb.id) is None
    # Never the team leader persona; never someone no longer on the desk.
    tl = Sender.objects.filter(team_leader=True).first() or Sender.objects.create(local_part="tl-{team}", team_leader=True)
    DeskDefault.objects.create(sender=tl, team=None, user=anna)
    assert desk.default_owner(tl.local_part.replace("{team}", ta.code), ta.id) is None
    ben.is_active = False
    ben.save()
    assert desk.default_owner("donotreply", tb.id) == anna


def test_defaults_given_once_when_a_conversation_starts(excon):
    setup(excon)
    d, e = desk_client(), desk_client("Ben Desk", "ben.desk")
    anna, ben = d.person.user, e.person.user
    ta, tb = Team.objects.get(code="a"), Team.objects.get(code="b")
    s = Sender.objects.get(local_part="donotreply")
    DeskDefault.objects.create(sender=s, team=None, user=anna)
    DeskDefault.objects.create(sender=s, team=tb, user=ben)
    deliver_now("3-13")  # the persona writes first: quietly given
    ka, kb = desk_thread(ta), desk_thread(tb)
    assert owner(ka) == (anna, True) and owner(kb) == (ben, True)
    assert not DeskNotice.objects.filter(kind="assign").exists()
    st = desk.states([ka], anna)[ka]
    assert st["mine"] and st["auto"] and st["assigned_by"] == desk.DEFAULTS
    assert b"default assignments" in d.get(f"/inbox/desk/t/{ka}/").content
    # The team writes: its owner hears of it like anyone on it.
    pa = participant("a")
    team_writes(pa, ta)
    assert titles(d) and "wrote" in titles(d)[-1] and titles(e) == []
    # Given by hand: the defaults never change it, nor give it again once released.
    d.post("/inbox/desk/mark/", {"threads": ka, "act": "assign", "assignee": ben.id}, headers=FETCH)
    assert owner(ka) == (ben, False)
    d.post("/inbox/desk/mark/", {"threads": ka, "act": "release"}, headers=FETCH)
    again(pa, ta)
    assert owner(ka) == (None, False)
    # Apply now does, as asked: the open conversations nobody has.
    assert [t for t, _ in desk.unassigned_open()] == [ka]
    assert d.post("/inbox/desk/assign/defaults/apply/", {}).status_code == 302
    assert owner(ka) == (anna, True)


def test_no_rule_at_first_then_when_the_team_writes(excon):
    setup(excon)
    d = desk_client()
    ta = Team.objects.get(code="a")
    deliver_now("3-13")
    ka = desk_thread(ta)
    assert owner(ka) == (None, False)
    DeskDefault.objects.create(sender=Sender.objects.get(local_part="donotreply"), team=ta, user=d.person.user)
    team_writes(participant("a"), ta)
    assert owner(ka) == (d.person.user, True)


def test_board_and_moving_cards(excon):
    setup(excon)
    d, e = desk_client(), desk_client("Ben Desk", "ben.desk")
    ben = e.person.user
    deliver_now("3-13")
    ta, tb = Team.objects.get(code="a"), Team.objects.get(code="b")
    got, _ = team_writes(participant("a"), ta)
    team_writes(participant("b"), tb, "And for us?")
    ka, kb = desk_thread(ta), desk_thread(tb)
    page = d.get("/inbox/desk/assign/")
    assert page.status_code == 200 and page.context["desk_tabs"]["current"] == "assign"
    cols = {c["uid"]: c for c in page.context["data"]["columns"]}
    assert {c["thread"] for c in cols[0]["cards"]} == {ka, kb}  # open, nobody's
    assert cols[ben.id]["n"] == 0 and cols[ben.id]["desk"] and cols[d.person.user.id]["me"]
    # Two dropped on Ben at once: told once.
    r = d.post("/inbox/desk/mark/", {"threads": [ka, kb], "act": "assign", "assignee": ben.id}, headers=FETCH)
    assert r.json() == {"ok": True, "n": 2}
    assert titles(e) == ["Anna Desk gave you 2 conversations"]
    j = d.get("/inbox/desk/assign/board/").json()
    cols = {c["uid"]: c for c in j["board"]["columns"]}
    assert cols[ben.id]["n"] == 2 and cols[ben.id]["attention"] == 2 and cols[0]["n"] == 0
    assert d.get(f"/inbox/desk/assign/board/?v={j['v']}").json() == {"v": j["v"]}  # nothing new
    # Filters: one team, one persona.
    j = d.get(f"/inbox/desk/assign/board/?team={ta.id}").json()
    assert [c["thread"] for col in j["board"]["columns"] for c in col["cards"]] == [ka]
    other = Sender.objects.get(local_part="cnn")
    j = d.get(f"/inbox/desk/assign/board/?persona={other.id}").json()
    assert not any(col["cards"] for col in j["board"]["columns"])
    # Answered ones only when asked for.
    d.post("/inbox/desk/mark/", {"threads": ka, "act": "done"}, headers=FETCH)
    j = d.get("/inbox/desk/assign/board/").json()
    assert ka not in [c["thread"] for col in j["board"]["columns"] for c in col["cards"]]
    j = d.get("/inbox/desk/assign/board/?recent=4").json()
    assert ka in [c["thread"] for col in j["board"]["columns"] for c in col["cards"]]
    # Someone no longer on the desk keeps a column for what they still have.
    ben.is_active = False
    ben.save()
    cols = {c["uid"]: c for c in d.get("/inbox/desk/assign/board/").json()["board"]["columns"]}
    assert cols[ben.id]["gone"] and cols[ben.id]["n"] == 1
    assert excon.get("/inbox/desk/assign/").status_code == 200  # ExCon may open it too
    assert got


def test_default_assignments_grid(excon):
    setup(excon)
    d, e = desk_client(), desk_client("Ben Desk", "ben.desk")
    anna, ben = d.person.user, e.person.user
    ta, tb = Team.objects.get(code="a"), Team.objects.get(code="b")
    s = Sender.objects.get(local_part="donotreply")
    page = d.get("/inbox/desk/assign/defaults/")
    assert page.status_code == 200 and s in [r["sender"] for r in page.context["g"]["rows"]]
    url = "/inbox/desk/assign/defaults/set/"
    assert d.post(url, {"sender": s.id, "team": "", "user": anna.id}, headers=FETCH).json()["ok"]
    assert d.post(url, {"sender": s.id, "team": tb.id, "user": ben.id}, headers=FETCH).json()["ok"]
    assert d.post(url, {"sender": s.id, "team": tb.id, "user": anna.id}, headers=FETCH).json()["ok"]  # changed, not added
    assert {(x.team_id, x.user_id) for x in DeskDefault.objects.all()} == {(None, anna.id), (tb.id, anna.id)}
    row = next(r for r in d.get("/inbox/desk/assign/defaults/").context["g"]["rows"] if r["sender"] == s)
    assert row["all"] == anna.id and {c["team"]: c["user"] for c in row["cells"]}[ta.id] is None
    # Bad choices.
    assert d.post(url, {"sender": s.id, "team": "", "user": 99999}, headers=FETCH).status_code == 400
    assert d.post(url, {"sender": 99999, "team": "", "user": anna.id}, headers=FETCH).status_code == 400
    participant_user = participant("a").person.user
    assert d.post(url, {"sender": s.id, "team": "", "user": participant_user.id}, headers=FETCH).status_code == 400
    # A whole row, then cleared; a cell back to nobody.
    assert d.post(url, {"act": "row", "sender": s.id, "user": ben.id}, headers=FETCH).json()["ok"]
    assert list(DeskDefault.objects.values_list("team_id", "user_id")) == [(None, ben.id)]
    assert d.post(url, {"sender": s.id, "team": "", "user": ""}, headers=FETCH).json()["ok"]
    assert not DeskDefault.objects.exists()
    d.post(url, {"sender": s.id, "team": ta.id, "user": ben.id}, headers=FETCH)
    d.post(url, {"act": "clear", "sender": s.id}, headers=FETCH)
    assert not DeskDefault.objects.exists()
    # Who has what, on the grid and the board's column heads.
    d.post(url, {"sender": s.id, "team": ta.id, "user": ben.id}, headers=FETCH)
    by = {p["id"]: p for p in desk_board.grid()["by_person"]}
    assert by[ben.id]["personas"][0]["teams"] == ta.name
    cols = {c["uid"]: c for c in d.get("/inbox/desk/assign/board/").json()["board"]["columns"]}
    assert cols[ben.id]["defaults"][0]["teams"] == ta.name
    # Kept through Start afresh, gone with the persona.
    from ops import reset
    reset.start_afresh()
    assert DeskDefault.objects.exists()
    s.delete()
    assert not DeskDefault.objects.exists()
    assert get_user_model().objects.filter(pk=ben.pk).exists()
