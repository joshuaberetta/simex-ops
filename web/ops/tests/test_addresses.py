"""Team and member addresses in SIMEX Mail (Senders tab, ops/addresses.py): patterns like the
personas', namesakes and personas never sharing an address, older mail still recognised."""

from ops import addresses, conf
from ops.models import Mail, Sender, Team
from ops.tests.test_leaders import setup_exercise
from ops.tests.test_ops import deliver_now, participant
from ops.tests.test_personal import send
from ops.views_inbox import _key_for_sender


def _set(excon, **parts):
    return excon.post("/senders/addresses/", {k: parts.get(k, "") for k in addresses.KEYS}, follow=True)


def test_defaults_are_unchanged():
    p = participant("a", "Jane").person
    assert addresses.team_address(Team.objects.get(code="a")) == "a@teams.simex.kobolabs.dev"
    assert addresses.member_addresses()[p.id] == "jane@a.teams.simex.kobolabs.dev"


def test_configured_addresses_everywhere(excon):
    setup_exercise(excon)
    r = _set(excon, team_local="undac-{team}", team_domain="epeigonia.org", member_local="{first}.{last}",
             member_domain="epeigonia.org")
    assert b"addresses saved" in r.content and b"undac-a@epeigonia.org" in r.content
    pa, po = participant("a", "Jane Doe"), participant("a", "Omar Ali")
    pb = participant("b", "Jane Doe (JD)")  # a namesake in another team, on the same domain
    ta = Team.objects.get(code="a")
    mine = addresses.member_addresses()
    assert mine[pa.person.id] == "jane.doe@epeigonia.org"
    assert mine[pb.person.id] == f"jane.doe.{pb.person.id}@epeigonia.org"
    choices = pa.get("/inbox/").context["from_choices"]
    assert [c["addr"] for c in choices] == ["undac-a@epeigonia.org", "jane.doe@epeigonia.org"]
    assert po.get("/inbox/lookup/?q=jane.doe@epeigonia.org").json()["key"] == f"m:{pa.person.id}"
    # Mail sent from them carries the address, and an answer goes back to the right mailbox.
    deliver_now("1-15")
    send(pa, base="/inbox/me", **{"from": "me"}, to=f"m:{po.person.id}", subject="Hi", body="x")
    got = Mail.objects.get(owner=po.person, folder="inbox", subject="Hi")
    assert got.from_addr == "jane.doe@epeigonia.org" and _key_for_sender(got) == f"m:{pa.person.id}"
    team_mail = Mail(from_addr="undac-a@epeigonia.org", persona="")
    assert _key_for_sender(team_mail) == f"t:{ta.id}"
    # Mail from before the change keeps working.
    assert _key_for_sender(Mail(from_addr="a@teams.simex.kobolabs.dev")) == f"t:{ta.id}"
    assert _key_for_sender(Mail(from_addr="jane.doe@a.teams.simex.kobolabs.dev")) == f"m:{pa.person.id}"
    # Back to the defaults.
    _set(excon)
    assert all(not conf.get(f"addr_{k}") for k in addresses.KEYS)


def test_bad_or_clashing_patterns_are_refused(excon):
    assert b"needs {team}" in _set(excon, team_local="undac", team_domain="epeigonia.org").content
    assert b"needs {name}" in _set(excon, member_local="member").content
    Sender.objects.create(local_part="undac-{team}", domain="epeigonia.org")
    r = _set(excon, team_local="undac-{team}", team_domain="epeigonia.org")
    assert b"Already a persona" in r.content and not conf.get("addr_team_local")
    # ... and a persona can't take a team's address.
    Sender.objects.filter(local_part="undac-{team}").delete()
    _set(excon, team_local="undac-{team}", team_domain="epeigonia.org")
    r = excon.post("/senders/", {"local_part": "undac-{team}", "domain": "epeigonia.org", "display_name": "X"},
                   follow=True)
    assert b"address in SIMEX Mail" in r.content and not Sender.objects.filter(local_part="undac-{team}").exists()


def test_a_persona_address_is_never_a_members():
    Sender.objects.create(local_part="jane", display_name="Jane the journalist")
    conf.put("addr_member_domain", "simex.kobolabs.dev")
    p = participant("a", "Jane").person
    assert addresses.member_addresses()[p.id] == f"jane.{p.id}@simex.kobolabs.dev"
