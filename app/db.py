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

    batch: Mapped["Batch"] = relationship(back_populates="reports")


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
