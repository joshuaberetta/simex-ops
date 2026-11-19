"""Database models. Times are stored as naive UTC; the UI shows them in the exercise
timezone (config.TZ). Tables are created at startup (no migrations yet)."""

from datetime import datetime, timezone

from sqlalchemy import (JSON, Boolean, DateTime, ForeignKey, Integer, LargeBinary, String, Text,
                        create_engine, event)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker

from . import config


def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


# Inject statuses. Only "scheduled" injects are sent by the scheduler (and only when live
# sending is armed); "overdue" ones missed their time by more than the grace period and
# wait for someone to press Send now.
STATUSES = ["draft", "scheduled", "sending", "sent", "partial", "failed", "overdue", "cancelled"]
DELIVERY_MODES = {
    "each_team": "One email per team (To: the team's members)",
    "each_person": "One email per participant",
    "all_bcc": "One email, all participants in Bcc",
    "custom": "Only the addresses below",
}
# Fields an import may update; the snapshot of their last imported values is kept in
# Inject.imported so a re-import can tell local edits apart from MEL changes.
IMPORT_FIELDS = ["seq", "method", "mailbox", "scenario_dt", "send_at", "sender_local", "recipients_label",
                 "delivery", "subject", "body", "supporting_documents"]


class Inject(Base):
    __tablename__ = "injects"
    id: Mapped[int] = mapped_column(primary_key=True)
    mel_id: Mapped[str] = mapped_column(String(40), unique=True)
    seq: Mapped[int] = mapped_column(Integer, default=0)  # order in the MEL
    method: Mapped[str | None] = mapped_column(String(200))
    mailbox: Mapped[str | None] = mapped_column(String(60))
    scenario_dt: Mapped[str | None] = mapped_column(String(20))  # scenario clock, e.g. 2035-05-20T09:00
    send_at: Mapped[datetime | None] = mapped_column(DateTime)  # real time, UTC
    status: Mapped[str] = mapped_column(String(20), default="draft")
    # Sender: a username from the Senders list, with optional per-inject overrides.
    sender_local: Mapped[str | None] = mapped_column(String(120))
    from_name: Mapped[str | None] = mapped_column(String(200))  # override of the sender's display name
    reply_to: Mapped[str | None] = mapped_column(String(320))  # override
    delivery: Mapped[str] = mapped_column(String(20), default="each_team")
    team_ids: Mapped[list | None] = mapped_column(JSON)  # None = all teams
    to_extra: Mapped[str | None] = mapped_column(Text)
    cc: Mapped[str | None] = mapped_column(Text)
    bcc: Mapped[str | None] = mapped_column(Text)
    recipients_label: Mapped[str | None] = mapped_column(String(200))  # as written in the MEL
    subject: Mapped[str | None] = mapped_column(Text)
    body: Mapped[str | None] = mapped_column(Text)
    footer_mode: Mapped[str] = mapped_column(String(10), default="default")  # default | custom | none
    footer: Mapped[str | None] = mapped_column(Text)
    supporting_documents: Mapped[str | None] = mapped_column(Text)  # file names listed in the MEL
    imported: Mapped[dict | None] = mapped_column(JSON)
    attempt: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
    attachments: Mapped[list["Attachment"]] = relationship(back_populates="inject", cascade="all, delete-orphan",
                                                            order_by="Attachment.filename")


class Attachment(Base):
    __tablename__ = "attachments"
    id: Mapped[int] = mapped_column(primary_key=True)
    inject_id: Mapped[int] = mapped_column(ForeignKey("injects.id", ondelete="CASCADE"))
    filename: Mapped[str] = mapped_column(String(300))
    content_type: Mapped[str] = mapped_column(String(120), default="application/octet-stream")
    size: Mapped[int] = mapped_column(Integer, default=0)
    data: Mapped[bytes] = mapped_column(LargeBinary)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    inject: Mapped[Inject] = relationship(back_populates="attachments")


class Sender(Base):
    __tablename__ = "senders"
    id: Mapped[int] = mapped_column(primary_key=True)
    local_part: Mapped[str] = mapped_column(String(120), unique=True)  # may contain {team}
    display_name: Mapped[str] = mapped_column(String(200), default="")
    reply_to: Mapped[str | None] = mapped_column(String(320))
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class Team(Base):
    __tablename__ = "teams"
    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(40), unique=True)  # fills {team}, e.g. "a"
    name: Mapped[str] = mapped_column(String(120))  # fills {team_name}, e.g. "Team A"
    email: Mapped[str | None] = mapped_column(String(320))  # shared team inbox, if any (used instead of members)
    sort: Mapped[int] = mapped_column(Integer, default=0)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    people: Mapped[list["Person"]] = relationship(back_populates="team", order_by="Person.name")


class Person(Base):
    __tablename__ = "people"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    email: Mapped[str] = mapped_column(String(320))
    team_id: Mapped[int | None] = mapped_column(ForeignKey("teams.id", ondelete="SET NULL"))
    role: Mapped[str] = mapped_column(String(20), default="member")  # member | tl
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    team: Mapped[Team | None] = relationship(back_populates="people")


class Setting(Base):
    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(String(60), primary_key=True)
    value: Mapped[str | None] = mapped_column(Text)


class TestRun(Base):
    __tablename__ = "test_runs"
    __test__ = False  # not a pytest class
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(20), default="active")  # active | paused | done | cancelled
    redirect: Mapped[str] = mapped_column(String(320))  # e.g. you+{key}@example.org
    dry_run: Mapped[bool] = mapped_column(Boolean, default=False)
    description: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    items: Mapped[list["TestRunItem"]] = relationship(back_populates="run", cascade="all, delete-orphan",
                                                     order_by="TestRunItem.due_at")


class TestRunItem(Base):
    __tablename__ = "test_run_items"
    __test__ = False
    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("test_runs.id", ondelete="CASCADE"))
    inject_id: Mapped[int] = mapped_column(ForeignKey("injects.id", ondelete="CASCADE"))
    due_at: Mapped[datetime] = mapped_column(DateTime)
    status: Mapped[str] = mapped_column(String(20), default="pending")  # pending | sending | sent | partial | failed | cancelled
    error: Mapped[str | None] = mapped_column(Text)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime)
    run: Mapped[TestRun] = relationship(back_populates="items")
    inject: Mapped[Inject] = relationship()


class Message(Base):
    """One email as handed to Resend (or recorded, for dry runs)."""
    __tablename__ = "messages"
    id: Mapped[int] = mapped_column(primary_key=True)
    inject_id: Mapped[int | None] = mapped_column(ForeignKey("injects.id", ondelete="SET NULL"))
    run_id: Mapped[int | None] = mapped_column(ForeignKey("test_runs.id", ondelete="SET NULL"))
    kind: Mapped[str] = mapped_column(String(10))  # live | test | oneoff
    target: Mapped[str] = mapped_column(String(200), default="")  # team / person / "all"
    idem_key: Mapped[str] = mapped_column(String(200), unique=True)
    from_addr: Mapped[str] = mapped_column(String(400))
    reply_to: Mapped[str | None] = mapped_column(String(320))
    to: Mapped[list] = mapped_column(JSON, default=list)
    cc: Mapped[list] = mapped_column(JSON, default=list)
    bcc: Mapped[list] = mapped_column(JSON, default=list)
    original_to: Mapped[list | None] = mapped_column(JSON)  # test sends: who it would have gone to
    subject: Mapped[str] = mapped_column(Text, default="")
    text: Mapped[str] = mapped_column(Text, default="")
    html: Mapped[str] = mapped_column(Text, default="")
    attachment_ids: Mapped[list] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(10), default="pending")  # pending | sent | failed | dry
    resend_id: Mapped[str | None] = mapped_column(String(100))
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime)
    inject: Mapped[Inject | None] = relationship()


class ImportBatch(Base):
    __tablename__ = "import_batches"
    id: Mapped[int] = mapped_column(primary_key=True)
    filename: Mapped[str] = mapped_column(String(300))
    payload: Mapped[list] = mapped_column(JSON)  # normalised records (participant-facing fields only)
    applied: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Audit(Base):
    __tablename__ = "audit"
    id: Mapped[int] = mapped_column(primary_key=True)
    at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    action: Mapped[str] = mapped_column(String(60))
    detail: Mapped[str | None] = mapped_column(Text)


def make_engine(url=None):
    url = url or config.database_url()
    kw = {"pool_pre_ping": True}
    if url.startswith("sqlite"):
        kw["connect_args"] = {"check_same_thread": False}
    engine = create_engine(url, **kw)
    if url.startswith("sqlite"):
        @event.listens_for(engine, "connect")
        def _fk(dbapi_conn, _):
            dbapi_conn.execute("PRAGMA foreign_keys=ON")
    return engine


engine = make_engine()
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


def init(bind=None):
    Base.metadata.create_all(bind or engine)


def audit(s, action, detail=None):
    s.add(Audit(action=action, detail=detail))
