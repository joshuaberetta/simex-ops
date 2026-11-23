"""Senders → Import a list of senders: plan (what it adds and changes, flagging values set
here), apply only the ticked rows, export in the same columns."""
import io

import openpyxl
from django.core.files.uploadedfile import SimpleUploadedFile

from ops import senders_import
from ops.models import Sender


def _xlsx(rows, header=("Alias", "Display name", "Organisation / use", "Category")):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(list(header))
    for row in rows:
        ws.append(list(row))
    buf = io.BytesIO()
    wb.save(buf)
    return SimpleUploadedFile("simex-aliases.xlsx", buf.getvalue())


def test_plan_flags_changes_and_applies_ticked_rows(excon):
    who = Sender.objects.create(local_part="who", display_name="World Health Org", domain="epeigonia.org")
    ifrc = Sender.objects.create(local_part="ifrc", display_name="")  # nothing set: filled in, not flagged
    cnn = Sender.objects.create(local_part="cnn-news", inapp_local="news", domain="cnn.com")
    old = Sender.objects.create(local_part="old-one", display_name="Old")
    r = excon.post("/senders/import/", {"file": _xlsx([
        ["who@epeigonia.org", "WHO", "EMT Focal point", "UN"],
        ["IFRC@epeigonia.org", "IFRC", "IFRC updates", "RCRC"],
        ["news@cnn.com", "CNN", None, "Media"],
        ["msf@epeigonia.org", "MSF France", "Doctors without Borders", "NGO"],
        ["donotreply@epeigonia.org", None, None, "Media"],
        ["bad address@@", "Bad", None, None],
        ["msf@epeigonia.org", "Again", None, None],
    ])}, follow=True)
    page = r.content.decode()
    assert "(replaces)" in page and "World Health Org" in page  # who's name changes
    assert "same address" in page  # news@cnn.com is cnn-news
    assert "not a valid address" in page and "the same username as line 5" in page
    assert "old-one" in page  # active here, not in the file
    items, missing = senders_import.plan(excon.session["senders_rows"])
    act = {i["row"]["line"]: (i["action"], i["replaces"]) for i in items}
    assert act[2] == ("update", True) and act[3] == ("update", False) and act[4] == ("update", False)
    assert act[5] == ("add", False) and act[7][0] == "skip" and act[8][0] == "skip"
    assert [m["sender"].local_part for m in missing] == ["old-one"]
    # Leave WHO as it is (unticked), apply the rest, make old-one inactive.
    excon.post("/senders/import/apply/", {"line": ["3", "4", "5", "6"], "deactivate": [str(old.id)]})
    who.refresh_from_db(); ifrc.refresh_from_db(); cnn.refresh_from_db(); old.refresh_from_db()
    assert who.display_name == "World Health Org" and who.notes == ""
    assert ifrc.display_name == "IFRC" and ifrc.inapp_address == "ifrc@epeigonia.org" and ifrc.category == "RCRC"
    assert cnn.display_name == "CNN" and cnn.inapp_address == "news@cnn.com" and cnn.local_part == "cnn-news"
    msf = Sender.objects.get(local_part="msf")
    assert msf.inapp_address == "msf@epeigonia.org" and msf.notes == "Doctors without Borders" and msf.active
    assert Sender.objects.get(local_part="donotreply").display_name == ""
    assert not old.active
    assert not Sender.objects.filter(display_name="Again").exists()
    # Applying needs a fresh upload.
    assert excon.post("/senders/import/apply/").status_code == 302


def test_export_round_trips(excon):
    Sender.objects.create(local_part="cnn-news", inapp_local="news", domain="cnn.com", display_name="CNN",
                          notes="Breaking news", category="Media")
    r = excon.get("/senders/export/")
    assert r.status_code == 200
    rows = senders_import.read("s.xlsx", r.content)
    assert rows[0]["username"] == "cnn-news" and rows[0]["address"] == "news@cnn.com" and rows[0]["active"] == "yes"
    items, _ = senders_import.plan(rows)
    assert items[0]["action"] == "same"


def test_username_column_never_renames(excon):
    Sender.objects.create(local_part="who", domain="epeigonia.org")
    rows = senders_import.read("s.csv", b"Username,Alias\nwho-2,who@epeigonia.org\n")
    items, _ = senders_import.plan(rows)
    assert items[0]["action"] == "skip" and "already the SIMEX Mail address of who" in items[0]["problems"][0]


def test_senders_page_saves_notes_and_category(excon):
    x = Sender.objects.create(local_part="who")
    excon.post("/senders/", {"id": x.id, "local_part": "who", "display_name": "WHO", "category": "UN",
                             "notes": "EMT focal point", "active": "1"})
    x.refresh_from_db()
    assert x.category == "UN" and x.notes == "EMT focal point"
