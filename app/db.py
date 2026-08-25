from __future__ import annotations

import datetime as dt

from sqlalchemy import (JSON, Boolean, Date, DateTime, ForeignKey, Integer,
                        String, Text, create_engine)
from sqlalchemy.orm import (DeclarativeBase, Mapped, mapped_column,
                            relationship, sessionmaker)

from .config import settings

url = settings.database_url
if url.startswith("postgres://"):
    url = url.replace("postgres://", "postgresql+psycopg://", 1)
elif url.startswith("postgresql://"):
    url = url.replace("postgresql://", "postgresql+psycopg://", 1)

engine = create_engine(url, pool_pre_ping=True, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, future=True)


class Base(DeclarativeBase):
    pass


class Batch(Base):
    """One delivery of reports, normally one market for one month."""
    __tablename__ = "batches"

    id: Mapped[int] = mapped_column(primary_key=True)
    received_at: Mapped[dt.datetime] = mapped_column(DateTime, default=dt.datetime.utcnow)
    source: Mapped[str] = mapped_column(String(32), default="email")
    email_from: Mapped[str] = mapped_column(String(255), default="")
    email_subject: Mapped[str] = mapped_column(String(512), default="")
    market: Mapped[str] = mapped_column(String(128), default="")
    period: Mapped[str] = mapped_column(String(32), default="")          # "2026-07"
    status: Mapped[str] = mapped_column(String(24), default="pending")   # pending|running|done|error
    notified_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    # When the most recent report landed. Reports arrive one email at a time, so
    # this is what decides a batch has gone quiet and the digest can go out.
    last_report_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    note: Mapped[str] = mapped_column(Text, default="")

    reports: Mapped[list["Report"]] = relationship(back_populates="batch", cascade="all, delete-orphan")

    @property
    def failed(self) -> int:
        return sum(1 for r in self.reports if r.severity == "fail")

    @property
    def warned(self) -> int:
        return sum(1 for r in self.reports if r.severity == "warn")

    @property
    def clean(self) -> int:
        return sum(1 for r in self.reports if r.severity == "pass")


class Report(Base):
    __tablename__ = "reports"

    id: Mapped[int] = mapped_column(primary_key=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey("batches.id", ondelete="CASCADE"), index=True)
    filename: Mapped[str] = mapped_column(String(512))
    stored_path: Mapped[str] = mapped_column(String(1024), default="")
    client: Mapped[str] = mapped_column(String(255), default="", index=True)
    account_ids: Mapped[str] = mapped_column(String(255), default="")
    market: Mapped[str] = mapped_column(String(128), default="", index=True)
    period: Mapped[str] = mapped_column(String(32), default="", index=True)
    is_lifetime: Mapped[bool] = mapped_column(Boolean, default=False)
    pages: Mapped[int] = mapped_column(Integer, default=0)

    impressions: Mapped[int] = mapped_column(Integer, default=0)
    clicks: Mapped[int] = mapped_column(Integer, default=0)

    products: Mapped[str] = mapped_column(String(512), default="")
    severity: Mapped[str] = mapped_column(String(12), default="pass")    # pass|warn|fail
    findings: Mapped[list] = mapped_column(JSON, default=list)
    owner_buyer: Mapped[str] = mapped_column(String(255), default="")
    owner_team: Mapped[str] = mapped_column(String(255), default="")

    # The reporter's own verdict, separate from what the checks found. A clean
    # report still needs a human to have looked at it before it ships, and a
    # failing one can be waived when the finding is a known data quirk rather
    # than something wrong with the report.
    review_state: Mapped[str] = mapped_column(String(16), default="new")
    # new | reviewed | waived | needs_fix
    reviewed_by: Mapped[str] = mapped_column(String(128), default="")
    reviewed_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    review_note: Mapped[str] = mapped_column(Text, default="")
    # Indexes of findings a person has looked at and accepted. The finding
    # stays on the report - it is a note about a known quirk, not a mistake -
    # but it stops counting against the severity.
    acked: Mapped[list] = mapped_column(JSON, default=list)

    batch: Mapped["Batch"] = relationship(back_populates="reports")

    def is_acked(self, i: int) -> bool:
        return i in (self.acked or [])

    @property
    def open_findings(self) -> list:
        """Findings nobody has accepted yet."""
        return [f for i, f in enumerate(self.findings or [])
                if not self.is_acked(i) and (f.get("severity") in ("fail", "warn"))]

    @property
    def effective_severity(self) -> str:
        """Severity counting only findings nobody has accepted.

        A report can carry a finding that is true, understood and not worth
        acting on - CTV excluded from the CTR base, a creative type that never
        renders a preview. Ticking it off has to clear the flag without
        deleting the note, or the next person to open the report re-discovers
        it from scratch.
        """
        # No findings recorded at all: trust the stored verdict. A report that
        # could not be parsed has severity "fail" and nothing itemised, and
        # computing from an empty list would quietly call it clean.
        if not self.findings:
            return self.severity
        levels = {f.get("severity") for f in self.open_findings}
        if "fail" in levels:
            return "fail"
        if "warn" in levels:
            return "warn"
        return "pass"

    @property
    def ready(self) -> bool:
        """Good to go: a person has signed off, and nothing is failing that
        they did not knowingly wave through."""
        if self.review_state == "waived":
            return True
        return self.review_state == "reviewed" and self.effective_severity != "fail"

    @property
    def board_state(self) -> str:
        """One word for the cycle board."""
        if self.review_state == "needs_fix":
            return "needs_fix"
        if self.ready:
            return "ready"
        sev = self.effective_severity
        if sev == "fail":
            return "errors"
        if sev == "warn":
            return "warnings"
        return "in"


class Inbound(Base):
    """Every delivery attempt that reached the app, accepted or not.

    Without this, a report that never appears has no trail at all: the sender
    saw a 200, the dashboard shows nothing, and there is no way to tell a
    rejected key from a missing attachment from a batch filed under a market
    nobody thought to look at.
    """
    __tablename__ = "inbound"

    id: Mapped[int] = mapped_column(primary_key=True)
    received_at: Mapped[dt.datetime] = mapped_column(DateTime, default=dt.datetime.utcnow,
                                                     index=True)
    source: Mapped[str] = mapped_column(String(32), default="")
    sender: Mapped[str] = mapped_column(String(255), default="")
    subject: Mapped[str] = mapped_column(String(512), default="")
    filenames: Mapped[str] = mapped_column(Text, default="")
    files: Mapped[int] = mapped_column(Integer, default=0)
    bytes: Mapped[int] = mapped_column(Integer, default=0)
    accepted: Mapped[bool] = mapped_column(Boolean, default=True)
    outcome: Mapped[str] = mapped_column(Text, default="")
    batch_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    market: Mapped[str] = mapped_column(String(128), default="")
    period: Mapped[str] = mapped_column(String(32), default="")


class Delivery(Base):
    """One partner group's finished cycle, packaged and shared."""
    __tablename__ = "deliveries"

    id: Mapped[int] = mapped_column(primary_key=True)
    period: Mapped[str] = mapped_column(String(32), index=True)
    group: Mapped[str] = mapped_column(String(255), index=True)
    target: Mapped[str] = mapped_column(String(32), default="drive")   # drive|dropbox|local
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=dt.datetime.utcnow)
    reports: Mapped[int] = mapped_column(Integer, default=0)
    bytes: Mapped[int] = mapped_column(Integer, default=0)
    local_path: Mapped[str] = mapped_column(String(1024), default="")
    # The link handed to the client. For a market that archives to Drive but
    # delivers by Dropbox, this is the Dropbox one.
    share_url: Mapped[str] = mapped_column(Text, default="")
    # Where the same reports were filed internally. Usually the Drive folder,
    # and the same as share_url when Drive is also what the client gets.
    archive_url: Mapped[str] = mapped_column(Text, default="")
    ok: Mapped[bool] = mapped_column(Boolean, default=True)
    message: Mapped[str] = mapped_column(Text, default="")


class Partner(Base):
    """Who runs reporting for each partner, from the reporting roster sheet.

    Two jobs. It names the reporter, trainer and recipients for a partner, and
    it supplies the fallback owner when a line item comes out of the IO export
    with no campaign manager on it - the partner's buyer normally, or the SEO
    person for an SEO line item.
    """
    __tablename__ = "partners"

    id: Mapped[int] = mapped_column(primary_key=True)
    partner: Mapped[str] = mapped_column(String(255), index=True)
    buyer: Mapped[str] = mapped_column(String(128), default="")
    buyer_email: Mapped[str] = mapped_column(String(255), default="")
    seo: Mapped[str] = mapped_column(String(128), default="")
    seo_email: Mapped[str] = mapped_column(String(255), default="")
    manager: Mapped[str] = mapped_column(String(128), default="")
    reporting_team: Mapped[str] = mapped_column(String(128), default="")
    to_emails: Mapped[str] = mapped_column(Text, default="")
    trainer: Mapped[str] = mapped_column(String(128), default="")
    reporting_notes: Mapped[str] = mapped_column(Text, default="")
    buyer_notes: Mapped[str] = mapped_column(Text, default="")
    # Markets that ship as one delivery. "7 Mountains PA Selinsgrove" and
    # "7 Mountains KY" both carry group "7 Mountains", so the partner gets one
    # link covering every market rather than one per market.
    # Mapped to "partner_group": GROUP is a reserved SQL word, so an
    # unquoted ADD COLUMN group ... is a syntax error on Postgres.
    group: Mapped[str] = mapped_column("partner_group", String(255), default="", index=True)
    # Where that group's zip goes. Blank uses the default target.
    delivery_target: Mapped[str] = mapped_column(String(32), default="")

    @property
    def recipients(self) -> list[str]:
        """The To: cell holds several addresses, separated by ; or , and
        sometimes annotated with a name in brackets."""
        import re as _re
        out = []
        for part in _re.split(r"[;,]", self.to_emails or ""):
            m = _re.search(r"[\w.+-]+@[\w-]+\.[\w.-]+", part)
            if m:
                out.append(m.group(0))
        return out


class OrderLine(Base):
    """The order-level list: what reports should exist, and when campaigns end."""
    __tablename__ = "order_lines"

    id: Mapped[int] = mapped_column(primary_key=True)
    market: Mapped[str] = mapped_column(String(128), index=True)
    client: Mapped[str] = mapped_column(String(255))
    account_ids: Mapped[str] = mapped_column(String(255), default="")
    campaign: Mapped[str] = mapped_column(String(512), default="")
    product: Mapped[str] = mapped_column(String(128), default="", index=True)
    starts_on: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    ends_on: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    needs_lifetime: Mapped[bool] = mapped_column(Boolean, default=True)
    buyer: Mapped[str] = mapped_column(String(255), default="")
    team_member: Mapped[str] = mapped_column(String(255), default="")
    buyer_email: Mapped[str] = mapped_column(String(255), default="")
    team_email: Mapped[str] = mapped_column(String(255), default="")
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class OrderSync(Base):
    """Records the last successful pull of the order list, so a batch does not
    re-download an object that has not changed."""
    __tablename__ = "order_sync"

    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(512), default="")
    etag: Mapped[str] = mapped_column(String(255), default="")
    last_modified: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    synced_at: Mapped[dt.datetime] = mapped_column(DateTime, default=dt.datetime.utcnow)
    rows: Mapped[int] = mapped_column(Integer, default=0)
    ok: Mapped[bool] = mapped_column(Boolean, default=True)
    message: Mapped[str] = mapped_column(Text, default="")
    guidance: Mapped[dict] = mapped_column(JSON, default=dict)


# --------------------------------------------------------------------------
# Startup
#
# Gunicorn boots two workers at once and both call init_db(). SQLAlchemy's
# create_all() checks-then-creates, which is not atomic, so the loser of the
# race sees "table already exists" and the worker dies. Postgres gets a real
# advisory lock; SQLite just retries, since the failure is benign.
# --------------------------------------------------------------------------
import logging
import time

log = logging.getLogger("report-qa")

_LOCK_ID = 8_150_724          # arbitrary, just has to be consistent

# Columns added after the first release. create_all() never alters an existing
# table, so new columns are added here instead of pulling in Alembic.
ADDITIVE_COLUMNS: list[tuple[str, str, str]] = [
    ("order_lines", "product", "VARCHAR(128) DEFAULT '' NOT NULL"),
    ("reports", "products", "VARCHAR(512) DEFAULT '' NOT NULL"),
    ("order_sync", "guidance", "JSON"),
    ("batches", "last_report_at", "TIMESTAMP"),
    ("reports", "review_state", "VARCHAR(16) DEFAULT 'new' NOT NULL"),
    ("reports", "reviewed_by", "VARCHAR(128) DEFAULT '' NOT NULL"),
    ("reports", "reviewed_at", "TIMESTAMP"),
    ("reports", "review_note", "TEXT DEFAULT '' NOT NULL"),
    ("partners", "partner_group", "VARCHAR(255) DEFAULT '' NOT NULL"),
    ("partners", "delivery_target", "VARCHAR(32) DEFAULT '' NOT NULL"),
    ("deliveries", "archive_url", "TEXT"),
    ("reports", "acked", "JSON"),
]


def _existing_columns(conn, table: str) -> set[str]:
    from sqlalchemy import inspect
    insp = inspect(conn)
    if table not in insp.get_table_names():
        return set()
    return {c["name"] for c in insp.get_columns(table)}


def _apply_additive_columns(conn) -> None:
    from sqlalchemy import text as sql_text
    for table, column, ddl in ADDITIVE_COLUMNS:
        cols = _existing_columns(conn, table)
        if cols and column not in cols:
            conn.execute(sql_text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))
            log.info("added column %s.%s", table, column)


def init_db() -> None:
    from sqlalchemy import text as sql_text
    is_pg = engine.dialect.name == "postgresql"

    for attempt in range(5):
        try:
            with engine.begin() as conn:
                if is_pg:
                    conn.execute(sql_text("SELECT pg_advisory_xact_lock(:i)"), {"i": _LOCK_ID})
                Base.metadata.create_all(conn, checkfirst=True)
                _apply_additive_columns(conn)
            if not is_pg:
                log.warning(
                    "Running on %s, not Postgres. On Render this means DATABASE_URL is "
                    "unset, and everything will be lost on the next deploy.",
                    engine.dialect.name)
            return
        except Exception as exc:                       # another worker got there first
            if attempt == 4:
                raise
            log.warning("init_db retry %d after %s", attempt + 1, exc)
            time.sleep(0.5 * (attempt + 1))
