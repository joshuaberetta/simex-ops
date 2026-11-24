"""AI personas → Import profiles (ops/npc_import.py): plan, apply the ticked rows, export in the
same columns; and switching ticked personas to answer on their own (taking over the desk's
waiting conversations) and back."""
import io

import openpyxl
from django.core.files.uploadedfile import SimpleUploadedFile

from ops import conf, npc_import
from ops.models import NpcProfile, NpcReply, Sender
from ops.tests.test_npc import fake, make_npc, write_to  # noqa: F401 (fixture)
from ops.tests.test_ops import participant

HEADER = ["Username", "Organisation", "Role", "Personality and voice", "What they know and may share", "Boundaries",
          "Timing", "Office hours from", "Office hours to", "Answers on its own"]


def _xlsx(rows, header=HEADER, name="profiles.xlsx"):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(list(header))
    for row in rows:
        ws.append(list(row))
    buf = io.BytesIO()
    wb.save(buf)
    return SimpleUploadedFile(name, buf.getvalue())


def test_plan_and_apply_ticked_rows(excon):
    ccdpr = Sender.objects.create(local_part="ccdpr", display_name="CCDPR")
    rco = Sender.objects.create(local_part="rco", display_name="RCO", inapp_local="rc.office", domain="un.ep")
    kept = NpcProfile.objects.create(sender=rco, personality="Brisk.", role="Resident Coordinator")
    Sender.objects.create(local_part="undac-{team}-tl")
    tl = Sender.objects.get(team_leader=True) if Sender.objects.filter(team_leader=True).exists() else \
        Sender.objects.create(local_part="tl-persona", team_leader=True)
    ercs = Sender.objects.create(local_part="ercs")
    r = excon.post("/npcs/import/", {"file": _xlsx([
        ["ccdpr", "CCDPR", "Director", "Formal, long emails.\nSigns 'Director, CCDPR'.", "- Runs the NEOC\n- 40 staff",
         "- No casualty figures", "Busy official", "8", "17:00", None],
        ["rc.office@un.ep", None, "Resident Coordinator a.i.", "Warm.", None, None, "instant", None, None, None],
        ["ercs", None, None, "Volunteer-minded.", None, None, "sometimes", None, None, "maybe"],
        ["nobody", None, None, "x", None, None, None, None, None, None],
        ["undac-{team}-tl", None, None, "x", None, None, None, None, None, None],
        [tl.local_part, None, None, "x", None, None, None, None, None, None],
        ["ccdpr", None, None, "again", None, None, None, None, None, "yes"],
    ])}, follow=True)
    page = r.content.decode()
    assert "new profile" in page and "(replaces)" in page and "not on the Senders tab" in page
    items = npc_import.plan(excon.session["npc_rows"])
    act = {i["row"]["line"]: i["action"] for i in items}
    assert act == {2: "add", 3: "update", 4: "skip", 5: "skip", 6: "skip", 7: "skip", 8: "skip"}
    probs = " ".join(" ".join(i["problems"]) for i in items)
    assert "timing 'sometimes'" in probs and "not yes or no" in probs and "{team}" in probs
    assert "team leader persona" in probs and "on line 2 too" in probs
    upd = next(i for i in items if i["row"]["line"] == 3)
    assert upd["replaces"] and {c["field"] for c in upd["changes"]} == {"role", "personality and voice", "timing"}
    excon.post("/npcs/import/apply/", {"line": ["2", "3"]})
    p = NpcProfile.objects.get(sender=ccdpr)
    assert p.personality == "Formal, long emails.\nSigns 'Director, CCDPR'." and p.knowledge == "- Runs the NEOC\n- 40 staff"
    assert p.organisation == "CCDPR" and p.role == "Director" and p.timing == "busy" and p.no_reply_chance == 0.1
    assert (p.office_from, p.office_to) == (8, 17) and not p.enabled and p.auto_send
    kept.refresh_from_db()
    assert kept.personality == "Warm." and kept.role == "Resident Coordinator a.i." and kept.timing == "instant"
    assert not NpcProfile.objects.filter(sender=ercs).exists()
    assert excon.post("/npcs/import/apply/").status_code == 302  # needs a fresh upload
    # Export: the profiles, then the personas without one; it reads back as unchanged.
    data = excon.get("/npcs/export/").content
    rows = list(openpyxl.load_workbook(io.BytesIO(data)).active.iter_rows(values_only=True))
    assert rows[0][0] == "Username" and {r[0] for r in rows[1:]} >= {"ccdpr", "rco", "ercs"}
    assert "undac-{team}-tl" not in {r[0] for r in rows}
    again = npc_import.plan(npc_import.read("x.xlsx", data))
    assert {i["sender"].local_part: i["action"] for i in again if i["profile"]} == {"ccdpr": "same", "rco": "same"}


def test_import_can_turn_one_on_and_csv(excon):
    s = Sender.objects.create(local_part="unicef")
    csv = "username,personality,answers on its own,timing,custom: from (min),custom: to (min)\nunicef,Kind.,yes,,10,40\n"
    excon.post("/npcs/import/", {"file": SimpleUploadedFile("p.csv", csv.encode())})
    page = excon.get("/npcs/import/").content.decode()
    assert "would start answering the teams on their own" in page
    excon.post("/npcs/import/apply/", {"line": ["2"]})
    p = NpcProfile.objects.get(sender=s)
    assert p.enabled and p.timing == "custom" and (p.min_minutes, p.max_minutes) == (10, 40)


def test_switch_on_takes_over_waiting_conversations(excon, fake):
    p = make_npc("bureaucrat")
    p.enabled = False
    p.save()
    pa = participant("test", "Jo")
    write_to(pa)
    assert not NpcReply.objects.exists()  # off: the desk has it
    assert "AI personas" in excon.get("/injects/").content.decode()  # the tab shows even with the setting off
    excon.post("/npcs/switch/", {"pid": [p.id], "action": "on", "waiting": "1"})
    p.refresh_from_db()
    job = NpcReply.objects.get()
    assert p.enabled and job.status == "queued"
    from django.utils import timezone
    assert (job.due_at - timezone.now()).total_seconds() <= 5 * 60 + 5  # within minutes, not the bureaucrat's hours
    # Once answered or queued, switching on again queues nothing more.
    excon.post("/npcs/switch/", {"pid": [p.id], "action": "on", "waiting": "1"})
    assert NpcReply.objects.count() == 1
    excon.post("/npcs/switch/", {"pid": [p.id], "action": "off"})
    p.refresh_from_db()
    assert not p.enabled
    # Settings off: switching on writes the flag but takes nothing over.
    conf.put("npc_enabled", "0")
    NpcReply.objects.all().delete()
    excon.post("/npcs/switch/", {"pid": [p.id], "action": "on", "waiting": "1"})
    assert NpcProfile.objects.get(pk=p.id).enabled and not NpcReply.objects.exists()
