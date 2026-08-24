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
    starts_on: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    ends_on: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    needs_lifetime: Mapped[bool] = mapped_column(Boolean, default=True)
    buyer: Mapped[str] = mapped_column(String(255), default="")
    team_member: Mapped[str] = mapped_column(String(255), default="")
    buyer_email: Mapped[str] = mapped_column(String(255), default="")
    team_email: Mapped[str] = mapped_column(String(255), default="")
    active: Mapped[bool] = mapped_column(Boolean, default=True)


def init_db() -> None:
    Base.metadata.create_all(engine)
