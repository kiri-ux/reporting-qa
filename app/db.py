from __future__ import annotations

import datetime as dt

from sqlalchemy import (JSON, Boolean, Date, DateTime, Float, ForeignKey,
                        Integer, String, Text, UniqueConstraint, create_engine)
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
    # Every check that ran, with what it verified. Kept alongside the findings
    # so the report page can say what was looked at, not only what went wrong.
    checks: Mapped[list] = mapped_column(JSON, default=list)
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
    # When a re-check pulled a sign-off because a new failure appeared. The
    # name stays on the row - somebody has to be told - but the report is back
    # to unreviewed, and showing the name as though it were still signed is how
    # a report reads as reviewed when nobody has read this answer.
    signoff_cleared_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    # Indexes of findings a person has looked at and accepted. The finding
    # stays on the report - it is a note about a known quirk, not a mistake -
    # but it stops counting against the severity.
    acked: Mapped[list] = mapped_column(JSON, default=list)
    # The fingerprint of the checking code that produced these findings. A
    # report stamped with an older one is re-checked in the background, because
    # findings are written once and a fixed rule does not reach back on its own.
    rules_version: Mapped[str] = mapped_column(String(32), default="", index=True)

    # A fingerprint of page one's top-left corner, which is where the partner
    # station's logo goes. Stored so that a logo turning up across several
    # unrelated markets can be recognized as the reporting tool's default
    # rather than anybody's.
    logo_hash: Mapped[str] = mapped_column(String(32), default="", index=True)

    # How this copy got here. A report somebody uploaded by hand was put there
    # deliberately, and the automatic feed arriving later with its own copy is
    # not automatically an improvement on it.
    source: Mapped[str] = mapped_column(String(16), default="")   # "" | manual

    # A newer file that arrived for a report nobody wants overwritten - one
    # already signed off, or one uploaded by hand. It waits here until somebody
    # says which copy is the real one, rather than replacing work silently.
    pending_path: Mapped[str] = mapped_column(String(1024), default="")
    pending_name: Mapped[str] = mapped_column(String(255), default="")
    pending_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    # What this file was called when it arrived, when that is not what it is
    # filed as. Worth saying out loud: a report whose name was missing its
    # order id came out of a folder somebody assembled by hand, and the same
    # folder is where the next one will come from.
    renamed_from: Mapped[str] = mapped_column(String(512), default="")
    # WHAT THIS REPORT WAS LAST FILED AS, in the partner's folder.
    #
    # A report's name is built from the client, the month and every order id
    # touching it, so a re-read that adds an order id renames it. Re-packaging
    # then uploads the new name and leaves the old file sitting beside it, and
    # the partner opens a folder holding the same report twice. This is the
    # only safe way to remove the stale one: it names a file this tool put
    # there itself, for this report, so nothing else in the folder is at risk.
    delivered_as: Mapped[str] = mapped_column(String(255), default="")

    batch: Mapped["Batch"] = relationship(back_populates="reports")

    @property
    def signed_off_by(self) -> str:
        """The name to show as having signed this off, or "".

        Only when the sign-off still stands. `reviewed_by` outlives a reset so
        the person can be told, and printing it regardless put a reviewer's
        initial beside a report in the unreviewed state.
        """
        if self.review_state in ("reviewed", "waived", "needs_fix"):
            return self.reviewed_by or ""
        return ""

    @property
    def signoff_pulled(self) -> str:
        """Why this report went back to unreviewed, or ""."""
        if self.review_state == "new" and self.signoff_cleared_at and self.reviewed_by:
            return (f"{self.reviewed_by} signed this off, then a re-check found a "
                    f"failure that was not there at the time, so the sign-off "
                    f"was taken off. It needs signing off again")
        return ""

    @property
    def has_pending(self) -> bool:
        return bool(self.pending_path and self.pending_at)

    @property
    def protected(self) -> str:
        """Why a newer file must not just overwrite this one, or "" if it may.

        Two cases, and they are different kinds of deliberate: somebody read
        this copy and signed it off, or somebody put this copy here by hand.
        Either way the automatic feed turning up later with its own version is
        a question, not an answer.
        """
        if self.review_state in ("reviewed", "waived"):
            return f"signed off by {self.reviewed_by or 'someone'}"
        if self.source == "manual":
            return "uploaded by hand"
        return ""

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
    # A SECOND LINK FOR THE SAME MONTH, on purpose.
    #
    # Blank is the partner's one link, which never moves. A tag makes a
    # separate folder beside it - "corrected", "without Meta", whatever the
    # reason was - with its own link to hand to somebody, and leaves the
    # original exactly as the partner already has it.
    tag: Mapped[str] = mapped_column(String(64), default="", index=True)


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
    # PIN THE DRIVE FOLDER, rather than matching it by name.
    #
    # The drive's folders were named by hand over ten years and do not match
    # the roster - "Results Media Solutions Chico" lives in "Results Radio
    # Chico" - so the match is a best guess that refuses rather than guesses
    # wrong. When somebody has fixed a folder by hand, a guess is not good
    # enough: this is the folder, by id, and no matching runs.
    drive_folder_id: Mapped[str] = mapped_column(String(128), default="")

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
    # The export's own line item ids, which is what the IO tool and TapClicks
    # both key on. The order id alone is not enough to point at one line when
    # a client runs several products under it.
    line_ids: Mapped[str] = mapped_column(String(512), default="")
    campaign: Mapped[str] = mapped_column(String(512), default="")
    product: Mapped[str] = mapped_column(String(128), default="", index=True)
    # The WIDEST span across every order this client runs this product under -
    # first start to last end. Right for "when does this campaign end", wrong
    # for "was it running in July".
    starts_on: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    ends_on: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    # EACH ORDER'S OWN WINDOW, kept because the widest span lies about the gaps.
    #
    # Blair Regional YMCA ran Social Mirror CTV to June 2026 and starts again in
    # August. Merged, that is one flight from 2025 to December 2026, and July -
    # a month in which no Social Mirror CTV ran at all - sits inside it. The
    # July report was failed for a product nobody was running.
    #
    # A list of [start, end] ISO strings, either of which may be null.
    flights: Mapped[list] = mapped_column(JSON, default=list)
    # Is ANY of the line items behind this row actually running? A paused line
    # is neither expected on the report nor a surprise when it turns up - the
    # buy exists, it is just not delivering - so it makes no claim either way.
    live: Mapped[bool] = mapped_column(Boolean, default=True)
    # CANCELED IS NOT THE SAME AS PAUSED. A canceled buy is not owed on the
    # report at all - not this month, not on the lifetime - but if it turns up
    # anyway that is not an error either: it ran and was stopped, and the data
    # is real. These rows used to be dropped at import, so a report carrying a
    # canceled product read as carrying a product nobody ordered.
    canceled: Mapped[bool] = mapped_column(Boolean, default=False)
    # EVERY LINE BEHIND THIS ROW IS "IO Complete". The campaign is over,
    # whatever end date the export still carries - order 45911's four line
    # items are all complete and two of them are dated to the end of 2026, so
    # waiting for the date means waiting for a lifetime nobody will ask for.
    complete: Mapped[bool] = mapped_column(Boolean, default=False)
    # EVERY LINE BEHIND THIS ROW IS "IO Paused". The buy is not delivering
    # today, but it delivered up to the day somebody paused it - so it is owed
    # a monthly for the month it ran in. These rows used to be dropped at
    # import, which is why 53392 and 54937 were on nobody's board.
    paused: Mapped[bool] = mapped_column(Boolean, default=False)
    # What this line item is meant to spend in a month, and what it actually
    # spent. Pacing is the comparison of the two, and neither number is on the
    # report - both come off the order.
    budget: Mapped[float | None] = mapped_column(Float, nullable=True)
    spend: Mapped[float | None] = mapped_column(Float, nullable=True)
    # What the order says should be SERVED in a month. None means the export
    # did not carry the column - a different claim from zero.
    impressions: Mapped[float | None] = mapped_column(Float, nullable=True)
    # THE ORDER'S OWN CAMPAIGN DATES, as distinct from the line item's.
    #
    # A line item is re-flighted, paused and restarted inside an order that
    # runs for years, so the two answer different questions: the line item says
    # whether the product was delivering this month, and the ORDER says what a
    # lifetime report has to cover. Reading the line item for both is why a
    # lifetime showed 2024-07-17 to 2026-12-31 on an order that runs 2023-05-05
    # to 2026-07-31.
    order_starts_on: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    order_ends_on: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    # The whole campaign, not one month of it. A lifetime report is measured
    # against these; a monthly against the two above.
    total_impressions: Mapped[float | None] = mapped_column(Float, nullable=True)
    total_budget: Mapped[float | None] = mapped_column(Float, nullable=True)
    # The products sold on the same line item as this one. "CTV + Video Ads" is
    # one buy with one goal, so pacing treats the pair as one row.
    sold_with: Mapped[str] = mapped_column(String(255), default="")
    needs_lifetime: Mapped[bool] = mapped_column(Boolean, default=True)
    buyer: Mapped[str] = mapped_column(String(255), default="")
    team_member: Mapped[str] = mapped_column(String(255), default="")
    buyer_email: Mapped[str] = mapped_column(String(255), default="")
    team_email: Mapped[str] = mapped_column(String(255), default="")
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class KnownLogo(Base):
    """A page-one logo somebody has passed judgement on.

    The check does not guess which mark is the reporting tool's default. It is
    told, once, by a person looking at a picture of the actual crop - because
    every rule for guessing it also catches a partner group that covers several
    markets and prints one perfectly good logo across all of them.
    """
    __tablename__ = "known_logos"

    id: Mapped[int] = mapped_column(primary_key=True)
    logo_hash: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    kind: Mapped[str] = mapped_column(String(16), default="generic")  # generic | ok
    marked_by: Mapped[str] = mapped_column(String(128), default="")
    marked_at: Mapped[dt.datetime] = mapped_column(DateTime, default=dt.datetime.utcnow)


class CycleDone(Base):
    """A row somebody handled this cycle without a PDF.

    SEO is the case that forced it. The work is done outside TapClicks, so
    there is nothing to upload, and the row sat at "Not received" all month
    holding its partner off "ready" for a report that was never coming.

    ONE CYCLE ONLY. It is not a rule about the client, and next month the row
    comes back asking for a report - which is what she wants, because SEO
    reports are going to start being uploaded.
    """
    __tablename__ = "cycle_done"
    __table_args__ = (UniqueConstraint("period", "ident", name="uq_cycle_done"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    period: Mapped[str] = mapped_column(String(16), index=True)
    # market|client|kind, normalized - the same identity the board rows carry.
    ident: Mapped[str] = mapped_column(String(255), index=True)
    market: Mapped[str] = mapped_column(String(255), default="")
    client: Mapped[str] = mapped_column(String(255), default="")
    kind: Mapped[str] = mapped_column(String(16), default="monthly")
    # "done" - handled, no PDF coming. "none" - no report was owed in the first
    # place. Both take the row off the board for this cycle; only one of them
    # means somebody did the work.
    reason: Mapped[str] = mapped_column(String(16), default="done")
    note: Mapped[str] = mapped_column(String(255), default="")
    marked_by: Mapped[str] = mapped_column(String(128), default="")
    marked_at: Mapped[dt.datetime] = mapped_column(DateTime, default=dt.datetime.utcnow)


class ServedDays(Base):
    """How many days a client actually delivered on, in one month.

    The one fact the order export cannot give. It carries a flight and a
    status: a line sold January to December and paused on the 2nd reads exactly
    like one paused on the 30th, and every campaign that ever finished sits at
    "IO Complete" forever. So "did this run in July" has been an inference off
    two dates, and the inference has been wrong in both directions.

    Counted from a serving file, one row per client per business unit per day.
    A month with no file loaded has no rows here at all, and the board falls
    back to reading dates - "nobody ran in July" is not a thing to conclude
    from a file nobody uploaded.
    """
    __tablename__ = "served_days"
    __table_args__ = (UniqueConstraint("period", "market_key", "client_key",
                                       name="uq_served_days"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    period: Mapped[str] = mapped_column(String(16), index=True)
    # Normalized, because the serving tool and the order tool do not spell a
    # client the same way and never have.
    market_key: Mapped[str] = mapped_column(String(255), index=True)
    client_key: Mapped[str] = mapped_column(String(255), index=True)
    # And as written, so the page can show what was in the file.
    market: Mapped[str] = mapped_column(String(255), default="")
    client: Mapped[str] = mapped_column(String(255), default="")
    days: Mapped[int] = mapped_column(Integer, default=0)
    first_day: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    last_day: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    loaded_at: Mapped[dt.datetime] = mapped_column(DateTime,
                                                   default=dt.datetime.utcnow)


class SavedView(Base):
    """A named set of filters on the cycle board.

    The filters have lived in the URL since build 39, which makes a view
    shareable but not findable - you have to still have the link. This is the
    same thing with a name on it.

    The query string is stored rather than the whole URL: a view saved on July
    should open on whatever cycle you are looking at, so the period is not part
    of what is saved.
    """
    __tablename__ = "saved_views"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    query: Mapped[str] = mapped_column(String(2048), default="")
    created_by: Mapped[str] = mapped_column(String(128), default="")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=dt.datetime.utcnow)


class RecheckJob(Base):
    """Progress of an on-demand re-check.

    In the process, not the database, this was invisible to the other gunicorn
    worker - press the button, get redirected to the worker that knows nothing
    about it, and the card shows no job at all. Worse, a job that died left
    "0 of 6" on screen forever with nothing able to say so. A row can be read
    by either worker and carries the time of its last progress, which is what
    tells a slow job from a stopped one.
    """
    __tablename__ = "recheck_jobs"

    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    partner_group: Mapped[str] = mapped_column(String(255), default="")
    period: Mapped[str] = mapped_column(String(16), default="")
    state: Mapped[str] = mapped_column(String(16), default="running")
    total: Mapped[int] = mapped_column(Integer, default=0)
    done: Mapped[int] = mapped_column(Integer, default=0)
    changed: Mapped[int] = mapped_column(Integer, default=0)
    note: Mapped[str] = mapped_column(String(255), default="")
    started_at: Mapped[dt.datetime] = mapped_column(DateTime, default=dt.datetime.utcnow)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime, default=dt.datetime.utcnow)

    @property
    def stalled(self) -> bool:
        """No progress for two minutes. A big report takes ten seconds, so this
        is long enough to be a real stop rather than a slow one."""
        if self.state != "running":
            return False
        return (dt.datetime.utcnow() - (self.updated_at or self.started_at)).total_seconds() > 120


class DeliveryJob(Base):
    """Progress of a packaging run.

    Packaging a partner uploads every one of its PDFs - forty-five pages and
    nine megabytes each, one after another - and that was happening inside the
    browser request. Several minutes of a spinner, with no way to tell a slow
    upload from a dead one, and a proxy timeout at the end of it.

    Same shape as RecheckJob and for the same reason: a thread's progress has
    to be readable from the other gunicorn worker.
    """
    __tablename__ = "delivery_jobs"

    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    partner_group: Mapped[str] = mapped_column(String(255), default="")
    period: Mapped[str] = mapped_column(String(16), default="")
    state: Mapped[str] = mapped_column(String(16), default="running")
    total: Mapped[int] = mapped_column(Integer, default=0)
    done: Mapped[int] = mapped_column(Integer, default=0)
    note: Mapped[str] = mapped_column(String(255), default="")
    started_at: Mapped[dt.datetime] = mapped_column(DateTime, default=dt.datetime.utcnow)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime, default=dt.datetime.utcnow)

    @property
    def stalled(self) -> bool:
        """No progress for four minutes. One nine-megabyte upload can take a
        while, so this is longer than a re-check's."""
        if self.state != "running":
            return False
        return (dt.datetime.utcnow() - (self.updated_at or self.started_at)).total_seconds() > 240


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
    # running | done. A sync downloads ~850 MB and parses a couple of million
    # rows; holding a browser request open for that is what made the page hang.
    state: Mapped[str] = mapped_column(String(16), default="done")
    started_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    # Which version of the product mapping read this export. An unchanged file
    # still has to be read again when the code interpreting it changes.
    map_version: Mapped[str] = mapped_column(String(32), default="")
    # WHO OR WHAT STARTED THIS ONE.
    #
    # Three different things can begin a sync - a button, a deploy whose import
    # rules changed, a batch of reports arriving - and none of them said so. So
    # opening the page and finding one running looked like the tool doing
    # something on its own for no reason anybody could name.
    trigger: Mapped[str] = mapped_column(String(32), default="")


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
    ("reports", "rules_version", "VARCHAR(32) DEFAULT '' NOT NULL"),
    ("reports", "source", "VARCHAR(16) DEFAULT '' NOT NULL"),
    ("reports", "pending_path", "VARCHAR(1024) DEFAULT '' NOT NULL"),
    ("reports", "pending_name", "VARCHAR(255) DEFAULT '' NOT NULL"),
    ("reports", "pending_at", "TIMESTAMP"),
    ("reports", "checks", "JSON"),
    ("order_lines", "line_ids", "VARCHAR(512) DEFAULT '' NOT NULL"),
    ("order_sync", "state", "VARCHAR(16) DEFAULT 'done' NOT NULL"),
    ("order_sync", "started_at", "TIMESTAMP"),
    ("order_sync", "map_version", "VARCHAR(32) DEFAULT '' NOT NULL"),
    ("reports", "signoff_cleared_at", "TIMESTAMP"),
    ("order_lines", "flights", "JSON"),
    ("order_lines", "live", "BOOLEAN DEFAULT TRUE NOT NULL"),
    ("order_lines", "canceled", "BOOLEAN DEFAULT FALSE NOT NULL"),
    ("order_lines", "complete", "BOOLEAN DEFAULT FALSE NOT NULL"),
    ("order_lines", "paused", "BOOLEAN DEFAULT FALSE NOT NULL"),
    ("cycle_done", "reason", "VARCHAR(16) DEFAULT 'done' NOT NULL"),
    ("reports", "logo_hash", "VARCHAR(32) DEFAULT '' NOT NULL"),
    ("order_sync", "trigger", "VARCHAR(32) DEFAULT '' NOT NULL"),
    ("order_lines", "budget", "DOUBLE PRECISION"),
    ("order_lines", "spend", "DOUBLE PRECISION"),
    ("order_lines", "impressions", "DOUBLE PRECISION"),
    ("order_lines", "order_starts_on", "DATE"),
    ("order_lines", "order_ends_on", "DATE"),
    ("order_lines", "total_impressions", "DOUBLE PRECISION"),
    ("order_lines", "total_budget", "DOUBLE PRECISION"),
    ("order_lines", "sold_with", "VARCHAR(255) DEFAULT '' NOT NULL"),
    ("reports", "renamed_from", "VARCHAR(512) DEFAULT '' NOT NULL"),
    ("reports", "delivered_as", "VARCHAR(255) DEFAULT '' NOT NULL"),
    ("deliveries", "tag", "VARCHAR(64) DEFAULT '' NOT NULL"),
    ("partners", "drive_folder_id", "VARCHAR(128) DEFAULT '' NOT NULL"),
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
