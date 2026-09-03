"""What a cycle owes, what has arrived, and whether a partner can ship.

The order list says what should exist. The batches say what turned up. This
joins the two into one row per expected report, which is what the cycle board
renders and what the delivery packager reads.
"""
from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .checks.products import earns_a_report, on_a_report
from .cycle import Cycle, cycle_for, month_label
from .db import OrderLine, Partner, Report
from .partners import first_name, is_seo

ACC = re.compile(r"\b\d{4,6}\b")

# "IO Pending Launch", with or without the ": Element Missing" that follows it.
PENDING_LAUNCH = re.compile(r"pending\s+launch", re.I)

# Test rows that must never appear on a board, block a delivery, or count
# toward anyone's workload.
EXCLUDED_PARTNERS = {"dummy partner", "test partner", "test", "zzz test"}


def excluded(market: str) -> bool:
    return (market or "").strip().lower() in EXCLUDED_PARTNERS


_KEY_CACHE: dict[str, str] = {}


def _key(s: str) -> str:
    """Memoised: this is called about seventy thousand times building one board,
    on a few hundred distinct strings."""
    hit = _KEY_CACHE.get(s)
    if hit is None:
        hit = re.sub(r"[^a-z0-9]", "", (s or "").lower())
        if len(_KEY_CACHE) < 20000:
            _KEY_CACHE[s] = hit
    return hit


# Which status wins when one order carries several. Live first: an order with
# a live line item and a cancelled one is a live order, and marking it dead is
# the kind of wrong that gets a report pulled to the wrong end date.
_STATUS_ORDER = ("live", "paused", "pending", "complete", "cancel")


def _status_rank(status: str) -> int:
    s = (status or "").strip().lower()
    if not s:
        return 99
    for i, word in enumerate(_STATUS_ORDER):
        if word in s:
            return i
    return len(_STATUS_ORDER)


@dataclass
class Expected:
    """One report that should exist this cycle."""
    market: str
    group: str
    client: str
    kind: str                      # "monthly" | "lifetime"
    account_ids: str = ""
    line_ids: str = ""
    products: list = field(default_factory=list)
    # The client's whole flight: FIRST start and LAST end across every order,
    # because two overlapping orders are one continuous campaign even though
    # the export lists them separately. This is the range a lifetime has to
    # cover, so the reporter needs both dates, not just the end.
    starts_on: dt.date | None = None
    ends_on: dt.date | None = None
    buyer: str = ""
    reporter: str = ""
    # Why a lifetime is on the board: the order that ended, and when. A client
    # can have one campaign finishing and another running for months, and
    # without this the row looks like a lifetime asked for on a live campaign.
    life_note: str = ""
    report: Report | None = None
    # Handled this cycle without a PDF - SEO, mostly, which is pulled outside
    # TapClicks. Who said so, and anything they typed.
    done_by: str = ""
    done_note: str = ""
    # "done"  - handled this cycle, there is just no PDF (SEO, mostly).
    # "none"  - no report is owed at all. A paused order that did not actually
    #           run this month is the case this exists for: the export cannot
    #           tell the difference between paused on the 2nd and paused on the
    #           30th, and a person can.
    done_kind: str = "done"
    # PUT BACK ON THE BOARD BY HAND. Every rule for deciding a report is not
    # owed is a rule about the usual case, and somebody who knows this client
    # gets the last word. Who said so and why.
    forced_by: str = ""
    forced_note: str = ""
    # THE LAST DAY THIS CLIENT ACTUALLY DELIVERED, when the campaign is
    # cancelled or complete. The end date on the order is what it was SOLD to
    # run to - nothing on the export says when somebody hit cancel - so a
    # lifetime pulled to that date covers weeks of nothing. This is the date to
    # pull to instead.
    stopped_on: dt.date | None = None
    # THE ORDER'S STATUS IN ITS OWN WORDS, across every line behind this row.
    # Every flag on this dataclass is that fact reduced to a yes or a no, and
    # when a row looks wrong the first question is what the order actually
    # says - which meant opening the IO tool in another tab.
    statuses: list = field(default_factory=list)
    # AND WHICH ORDER SAID IT. The list above is every status across the row,
    # which on a client running two orders cannot say which one is which -
    # Kerr-Bilt Trailers showed 50360 and 53901 above one live mark and one
    # complete mark, with nothing tying either to either. Read off the line
    # items themselves: {order id: status}.
    order_status: dict = field(default_factory=dict)

    @property
    def state(self) -> str:
        """missing | in | warnings | errors | needs_fix | ready"""
        if self.report:
            return self.report.board_state
        # A REPORT ALWAYS WINS. The mark says "nothing is coming"; if something
        # came anyway, it is the thing to judge.
        return "ready" if self.done_by else "missing"

    @property
    def ready(self) -> bool:
        if self.report:
            return bool(self.report.ready)
        return bool(self.done_by)

    @property
    def waiting(self) -> bool:
        """A newer file arrived and nobody has said yes or no to it yet.

        THIS IS WORK, AND IT WAS FILED UNDER FINISHED. A parked file leaves the
        sign-off alone - deliberately, because the copy that was signed off is
        still the copy the partner gets - so the row stayed "Good to go" and
        went to the Completed bucket, which is the one nobody reads. The amber
        tag was on a row you would only find by going to look for it.

        The sign-off stands. The row is open, because there is a decision on
        it.
        """
        return bool(self.report is not None
                    and getattr(self.report, "has_pending", False))

    @property
    def open_row(self) -> bool:
        """Is there something to do on this row? The Pending bucket's test."""
        return (not self.ready) or self.waiting

    @property
    def done_only(self) -> bool:
        """Checked off with no PDF behind it."""
        return bool(self.done_by) and self.report is None

    @property
    def not_owed(self) -> bool:
        """Marked as owing no report at all this cycle."""
        return self.done_kind == "none" and self.report is None

    @property
    def ident(self) -> str:
        return f"{_key(self.market)}|{_key(self.client)}|{self.kind}"


# SEO IS NOT IN THE SERVING FILE AND NEVER WILL BE.
#
# The serving file is ad delivery - impressions against days - and SEO is not
# served, so an SEO client is absent from it every month for ever. The rule
# that reads absence as "it did not run" took Whitley's whole SEO list off the
# board with "not in the serving file at all - either it did not run, or the
# serving file spells this client differently".
#
# There is no special case for it below, and there does not need to be: an SEO
# row never enters `ran_days`, which is the only thing the serving rules read.
# Splitting SEO onto its own row is what makes that true, and it is a better
# answer than exempting a mixed client's whole row - the digital half of that
# buy IS served, and is still judged.


def _order_windows(l) -> list:
    """(order id, its own start, its own end) for every order behind this row.

    A row is one client and one product across every order that carries it, and
    its dates are the widest of them - so "when did this campaign end" asked of
    the row gives the LAST order's end, about a campaign that may have finished
    weeks ago. Southeastern Cooling finished 51648 on 31 August and has 51649
    running through September; rolled up, the row ends 30 September and no
    lifetime was owed for the campaign that actually ended.
    """
    out = []
    for d in (getattr(l, "detail", None) or []):
        if not isinstance(d, dict):
            continue
        try:
            st = dt.date.fromisoformat(str(d.get("order_starts") or d.get("starts"))[:10])
        except (ValueError, TypeError):
            st = None
        try:
            en = dt.date.fromisoformat(str(d.get("order_ends") or d.get("ends"))[:10])
        except (ValueError, TypeError):
            en = None
        out.append((str(d.get("order") or ""), st, en))
    if not out:
        out.append((l.account_ids or "", l.order_starts_on or l.starts_on,
                    l.order_ends_on or l.ends_on))
    return out


def _live_in_month(cyc, l, open_only: bool = False) -> bool:
    """Is any LINE ITEM behind this row still open and running in the month?

    THE ROLLED-UP WINDOW IS THE WIDEST OF EVERY LINE, and asking it whether the
    client ran this month is asking the wrong question. Todd's Jewelry has
    eight line items, every one of them IO Complete and finished by 31 July,
    and four more that are Pending Launch with elements missing. Rolled up, the
    row spans August and the board asked for a report on a campaign where
    nothing was running.

    The per-line detail has been on the row since build 109 and nothing read it
    for this. A line counts when its OWN dates touch the month - nothing here
    looks at status. "IO Complete" on a line that finished on 31 August means
    it delivered the whole month and is owed a report; cancellation is judged
    further down, where a cancelled line joins a monthly without creating one.
    Rows imported before that detail existed fall back to the old test, which
    is the same answer they have always given.
    """
    detail = [d for d in (getattr(l, "detail", None) or []) if isinstance(d, dict)]
    if not detail:
        return cyc.was_live(l.starts_on, l.ends_on) and not (
            open_only and bool(getattr(l, "canceled", False)))

    def _d(v):
        try:
            return dt.date.fromisoformat(str(v)[:10]) if v else None
        except ValueError:
            return None

    # A LINE THAT HAS NOT LAUNCHED IS NOT RUNNING, whatever its dates say.
    #
    # "IO Pending Launch: Element Missing" is a buy waiting on creative. Its
    # flight dates are already set, so on dates alone it reads as delivering -
    # and on Todd's Jewelry it was the only thing keeping the row alive, on an
    # order whose eight real line items all finished by 31 July. The import
    # already drops these when the ORDER is pending too; under a live order
    # header they survive, and they should not be the reason for a report.
    # `open_only` asks the stricter question: is any line that is STILL OPEN
    # running this month? That is what decides whether a report is owed. The
    # looser question - any line at all, cancelled included - decides whether a
    # product belongs on a report that is being pulled anyway.
    #
    # Roof Top Services is the difference. Its three Meta line items are one
    # complete, one cancelled dated to 30 August, and one starting in
    # September. Something is "running" in August only if you count the
    # cancelled one, and a cancelled line is not a reason to pull a report.
    return any(cyc.was_live(_d(d.get("starts")), _d(d.get("ends")))
               and not PENDING_LAUNCH.search(str(d.get("status") or ""))
               and not (open_only and d.get("canceled"))
               for d in detail)


def _reporter_names(idx: dict) -> set:
    """Every reporter on the roster, off the partner index already in hand.

    THIS USED TO ASK THE DATABASE. It cached on a COUNT of the partner table -
    so building a board of eight hundred rows ran eight hundred COUNT queries
    to answer the same question, which is the sort of thing that turns a fast
    page into a slow one without ever looking wrong.

    The index is built once at the top of the board and holds every partner.
    """
    return {(p.reporting_team or "") for p in idx.values()}


def overrides(db: Session, period: str) -> dict:
    """Every hand override on this cycle, by ident."""
    from .db import CycleDone
    return {m.ident: m for m in db.scalars(
        select(CycleDone).where(CycleDone.period == period)).all()}


# A CAMPAIGN THAT BARELY RAN IS NOT OWED A MONTHLY.
#
# The Grove Event Venue started on 30 July, so its July report covers two days
# of delivery - a page of near-zero numbers that reads as a broken report and
# takes a reporter's time to pull, look at and explain. Anything under a week
# waits for next month, when there is a month to report on.
#
# LIFETIMES ARE EXEMPT. A campaign that ended in the first days of the month
# ran for its whole flight; the fact that only two of those days fall inside
# this cycle says nothing about the report it owes.
MIN_DAYS_IN_MONTH = 7

# A campaign no longer than this, ending in its first month, owes only its
# lifetime: the two reports would cover the same days and print the same
# numbers.
SHORT_CAMPAIGN_DAYS = 30


def days_in_cycle(cyc, starts_on, ends_on) -> int:
    """How many days of this month the line actually delivered.

    A missing date is open-ended, which is what the rest of this module assumes
    too: a blank end means still running.
    """
    first = max(starts_on, cyc.starts_on) if starts_on else cyc.starts_on
    last = min(ends_on, cyc.ends_on) if ends_on else cyc.ends_on
    return max(0, (last - first).days + 1)


STATES = ["missing", "in", "warnings", "errors", "needs_fix", "ready"]
STATE_LABEL = {"missing": "Not received", "in": "In, unreviewed",
               "warnings": "Warnings", "errors": "Errors",
               "needs_fix": "Needs fix", "ready": "Good to go"}


def _partner_index(db: Session) -> dict[str, Partner]:
    return {_key(p.partner): p for p in db.scalars(select(Partner)).all()}


def _match_partner(idx: dict[str, Partner], market: str) -> Partner | None:
    k = _key(market)
    if k in idx:
        return idx[k]
    best = None
    for pk, p in idx.items():          # longest containing name wins
        if pk and (pk in k or k in pk):
            if best is None or len(_key(best.partner)) < len(pk):
                best = p
    return best


def _not_canceled():
    """Rows the client did not cancel. NULL counts as not canceled, for order
    lines loaded before the column existed."""
    from sqlalchemy import or_ as _or
    return _or(OrderLine.canceled.is_(False), OrderLine.canceled.is_(None))


def expected_for(db: Session, period: str,
                 skipped: list | None = None) -> list[Expected]:
    """Every report this cycle owes, joined to whatever has arrived.

    A client owes a monthly if any of its order lines was live during the data
    month, and a lifetime if any line ENDED inside the cycle's lifetime window
    - which runs past month end to the 3rd business day, so a campaign that
    finished on the 1st ships with the monthlies instead of waiting a month.
    """
    from .serving import served_days
    cyc = cycle_for(period)
    idx = _partner_index(db)

    # ONE PASS, AND ONLY THE COLUMNS THIS NEEDS.
    #
    # This used to load every order line as a full ORM object, twice - once for
    # the expected rows and again for the flight spans. On a board with 13,000
    # lines that is 26,000 objects built and 13,000 JSON columns decoded that
    # nothing reads, and it was seven tenths of a second of the page on its own,
    # every time anybody opened or filtered the board.
    # The date test is the same one `was_live` and `needs_lifetime` apply, moved
    # into SQL: a line that ended before this month and is not ending inside the
    # lifetime window has nothing to say about this cycle. A missing date is
    # open-ended, so NULL stays in.
    from sqlalchemy import or_
    # Every reporter on the roster, once, for the first-name shortening below.
    reporter_pool = _reporter_names(idx)

    cols = db.execute(select(
        OrderLine.market, OrderLine.client, OrderLine.account_ids,
        OrderLine.line_ids, OrderLine.buyer, OrderLine.product,
        OrderLine.starts_on, OrderLine.ends_on,
        OrderLine.order_starts_on, OrderLine.order_ends_on,
        OrderLine.canceled,
        # AND THE ORDER-LEVEL FLAG. Leaving a column out of this select is why
        # every order pill on the board was gray for a week: the code reads
        # `l.order_canceled`, and on a row selected column by column that
        # attribute simply does not exist, so it reads False on every line and
        # never once fails.
        OrderLine.order_canceled,
        OrderLine.complete, OrderLine.status,
        # THE LINE ITEMS THEMSELVES, which is where the per-order status lives.
        #
        # Leaving this out of the select is why every order pill on the board
        # was gray. The code that colors them reads `l.detail`, and on a row
        # selected column by column that attribute simply does not exist - so
        # it read None on every line, said nothing, and never once failed. The
        # tooltip then blamed the export for a status the board had not asked
        # for.
        OrderLine.detail).where(
            # The ORDER's end counts as well as the line item's: a lifetime is
            # owed on an order that finishes this cycle even when the line item
            # behind it stopped in May.
            or_(OrderLine.ends_on.is_(None),
                OrderLine.ends_on >= cyc.starts_on,
                OrderLine.order_ends_on >= cyc.starts_on),
            or_(OrderLine.starts_on.is_(None),
                OrderLine.starts_on <= cyc.ends_on))).all()

    # The client's whole flight, aggregated by the database rather than by
    # walking every line again in Python. This is the only reason the finished
    # lines are read at all, and there are a lot of them.
    from sqlalchemy import func
    # THE ORDER'S DATES, NOT THE LINE ITEM'S.
    #
    # A lifetime report covers the campaign the client bought, and that is the
    # order: 5 May 2023 to 31 July 2026. Its line items are re-flighted, paused
    # and restarted inside that - so reading them gave a lifetime range of
    # 17 July 2024 to 31 December 2026 on the same order, which is neither end
    # of what was sold. COALESCE, because an order loaded before these columns
    # existed has only the line item's dates to offer.
    spans = db.execute(select(
        OrderLine.market, OrderLine.client,
        func.min(func.coalesce(OrderLine.order_starts_on, OrderLine.starts_on)),
        func.max(func.coalesce(OrderLine.order_ends_on, OrderLine.ends_on)))
        .where(_not_canceled())
        .group_by(OrderLine.market, OrderLine.client)).all()
    span: dict[tuple[str, str], list] = {}
    for market, client, first, last in spans:
        if excluded(market):
            continue
        k = (_key(market), _key(client))
        cur = span.get(k)
        if cur is None:
            span[k] = [first, last]
            continue
        if first and (cur[0] is None or first < cur[0]):
            cur[0] = first
        if last and (cur[1] is None or last > cur[1]):
            cur[1] = last

    # And the partner match is memoised on the market name. It scans the whole
    # roster looking for the longest containing name, which is fine 206 times
    # and not fine 13,000 times.
    pcache: dict[str, Partner | None] = {}

    rows: dict[tuple[str, str, str], Expected] = {}
    # Whether the buyer currently on each Expected came off an SEO line, and is
    # therefore still waiting for a real one.
    seo_buyer: dict[tuple[str, str, str], bool] = {}
    # The longest any one of a client's products delivered this month. A client
    # is owed a monthly if ANY of them ran a real week - one product starting on
    # the 30th does not excuse the rest of the campaign.
    ran_days: dict[tuple[str, str, str], int] = {}
    # The end date that actually put a lifetime on the board. A client can have
    # one order finishing this month and another running to October; the
    # lifetime is about the one that finished.
    life_end: dict[tuple[str, str, str], dt.date] = {}
    # The order behind that end, so an order is never compared with itself, and
    # every window a client has, so an ending campaign can be tested against
    # the ones still running.
    life_order: dict[tuple[str, str, str], str] = {}
    # That order's OWN window, for the overlap test - see life_end above.
    life_span: dict[tuple[str, str, str], tuple] = {}
    windows: dict[tuple[str, str], list[tuple]] = {}
    # Products that belong on a report but never earn one - see below.
    ride_along: dict[tuple[str, str], set] = {}
    # And what every one of the client's orders says it is, whether or not that
    # order earns a row of its own this cycle. The pills name the whole
    # campaign, so the colors have to come from the whole campaign too.
    order_status_all: dict[tuple[str, str], dict[str, str]] = {}

    # WHEN DID IT STOP? The export never says.
    #
    # "Cancelled" and "IO Complete" say a campaign is over and not which month
    # it ended in, while its end date still reads whatever it was sold to run
    # to. So closing out on the flag alone closes out YEARS of finished
    # campaigns into this one cycle - 7 Mountains grew by a hundred rows and
    # almost none of them were finished in July.
    #
    # The serving file dates it. A campaign that delivered this month is one
    # that finished this month; one that last delivered in March finished in
    # March, and is not this cycle's work. With no serving file loaded there is
    # nothing to test against and the flag stands on its own, as before.
    served_now = served_days(db, period)
    # And the last day each one delivered, which is what dates a close-out -
    # measured against the last day the FILE has, not the last day of the
    # month. A file covering only part of the month makes every client on the
    # board look like it stopped on the day the data ran out.
    from .serving import coverage_end, last_served
    stopped_day = last_served(db, period)
    data_ends = coverage_end(db, period)

    # IS THIS CLIENT FINISHED, OR IS ONE LINE ITEM OF THEIRS FINISHED?
    #
    # Build 89 read "cancelled or complete" off a single line and put a
    # lifetime on the board for the whole client, and 7 Mountains grew by a
    # hundred rows overnight. Almost none of them were finished campaigns: a
    # cancelled line item inside a live order is an ordinary thing, and every
    # campaign that has ever ended sits at "IO Complete" forever.
    #
    # Closing a campaign out is a statement about the CLIENT, not about one
    # line of their order. So the flag only earns a lifetime when nothing they
    # have is still running - which is the same thing the end date says, only
    # sooner and more reliably.
    # EVERY LINE THE CLIENT HAS, NOT THE ONES THIS CYCLE CARES ABOUT.
    #
    # `cols` above is filtered to lines that touch this month, and orders 54169
    # and 48327 are why that is the wrong set to answer "is this campaign
    # over". Both have a line at IO Pending Launch dated to start in September,
    # which the date filter drops - so every line the board could see was
    # complete, and it closed out an order that has not launched half of what
    # was sold. A campaign is finished when NOTHING on it is left, including
    # the parts that have not started yet.
    all_stopped: dict[tuple[str, str], bool] = {}
    # And whether ANY line was stopped, which is a different question: it is
    # the one that says "this campaign did not run to the date on the order".
    any_stopped: dict[tuple[str, str], bool] = {}
    for market, client, product, canceled, complete in db.execute(select(
            OrderLine.market, OrderLine.client, OrderLine.product,
            OrderLine.canceled, OrderLine.complete)).all():
        if excluded(market) or is_seo(product):
            continue
        k = (_key(market), _key(client))
        stopped = bool(canceled or complete)
        all_stopped[k] = all_stopped.get(k, True) and stopped
        any_stopped[k] = any_stopped.get(k, False) or stopped

    for l in cols:
        if excluded(l.market):
            continue
        # ONE WINDOW PER ORDER, not one per rolled-up row.
        #
        # This is what the overlap test reads, and a row carrying two orders
        # was offering it a single window spanning both - so the test compared
        # "51648, 51649" against itself and found, correctly, that it overlaps.
        # Southeastern Cooling's two orders do not overlap at all: 51648 ran to
        # 31 August and 51649 starts on 1 September.
        for _oid, _ws, _we in _order_windows(l):
            windows.setdefault((_key(l.market), _key(l.client)), []).append(
                (_oid, _ws, _we, l.product or ""))
        # EVERY ORDER'S STATUS, OFF EVERY LINE, BEFORE ANYTHING IS FILTERED.
        #
        # This was being read further down, inside the loop that builds the
        # expected rows - and that loop skips any line which is neither live
        # nor owed a lifetime. But a finished order that overlaps the campaign
        # still gets its id onto the row, because the row is about the whole
        # campaign, not about one order. So the pill was there and its status
        # was not, and a gray pill said the export had no status on order 51903
        # when the export has one on every row it ships.
        for d in (getattr(l, "detail", None) or []):
            if not isinstance(d, dict):
                continue
            oid, st = str(d.get("order") or ""), (d.get("status") or "").strip()
            if not oid or not st:
                continue
            seen = order_status_all.setdefault(
                (_key(l.market), _key(l.client)), {})
            if _status_rank(st) < _status_rank(seen.get(oid, "")):
                seen[oid] = st
        # A CANCELLED BUY OWES A LIFETIME, NOT A MONTHLY.
        #
        # Canceling does not mean it never ran - it ran and was stopped - so
        # the campaign still needs closing out. What it does not need is
        # another month's report on a month it was cancelled in.
        gone = bool(getattr(l, "canceled", False))
        # A CANCELLED LINE ITEM IS NOT A CANCELLED CAMPAIGN.
        #
        # One product pulled off an order that is still running delivered its
        # part of the month, and the client is still owed their monthly - the
        # report just has one fewer section on it. Only the ORDER being
        # cancelled stops the monthly, and that case is owed a lifetime for
        # what it did run instead.
        #
        # These were one flag, so a single cancelled line took the whole
        # client's monthly off the board.
        order_gone = bool(getattr(l, "order_canceled", False))
        live = (not order_gone) and _live_in_month(cyc, l)
        # A CANCELLED LINE JOINS A MONTHLY. IT DOES NOT CREATE ONE.
        #
        # Both halves of that come from the same place: nothing in the export
        # says WHEN somebody hit cancel, only what the line was sold to run to.
        # So a cancelled line spanning the month may have delivered all of it
        # or none of it, and the tool cannot tell which.
        #
        # Joining is safe either way - the product appears on a report that is
        # being pulled regardless. Creating is not: order 51378 has one live
        # product (Website Visitor ID, which never appears on a report), one
        # starting in September, and seven cancelled or complete lines, one of
        # them dated into September. Letting that one cancelled line ask for a
        # report produced a monthly for a client with nothing to report on, and
        # the board could only say "this is worth a closer look".
        creates_monthly = (not order_gone) and _live_in_month(cyc, l,
                                                              open_only=True)
        # A lifetime is owed when the ORDER ends inside the window. The line
        # item ending is not the campaign ending - River Valley Builders'
        # Performance Max was re-flighted to run to the end of the year, and
        # the old flight's 31 July end was putting a lifetime on the board that
        # nobody owes.
        # SEO IS NOT OWED A LIFETIME. It is bought by the month and reported
        # on by the month; there is no campaign that finishes and no
        # delivery-to-date to sum up. An SEO-only client was getting a lifetime
        # row nobody was ever going to pull.
        # A cancelled or completed campaign closes out NOW. Either way its end
        # date on the export is still whatever it was sold to run to, so
        # waiting for that date means waiting for a lifetime nobody is ever
        # going to be asked for - order 45911's four line items are all "IO
        # Complete" and two of them are dated to the end of 2026.
        #
        # ONLY WHEN THE WHOLE CLIENT HAS STOPPED. One cancelled line item under
        # a live order is not a campaign that finished, and reading it as one
        # put a hundred lifetimes on 7 Mountains that nobody owed.
        #
        # AND ONLY WHEN IT STOPPED THIS MONTH, where the serving file can say.
        ck_key = (_key(l.market), _key(l.client))
        # DID IT STOP INSIDE THIS MONTH, or is it still going into the next one?
        #
        # Order 49421 reads "IO Complete" and ends on 6 August - a day past
        # this cycle's lifetime cutoff - and it delivered every day of July, so
        # it is still running into August and its lifetime belongs to August's
        # cycle. Order 45911 also reads complete, is dated to the end of
        # December, and last delivered in the middle of July: that one really
        # has finished, and waiting for December means waiting forever.
        #
        # The difference is not on the order. It is the last day with delivery
        # on it: before month end means it stopped, at month end means it is
        # still going. With no serving file the flag stands on its own.
        last_day = stopped_day.get(ck_key)
        stopped_in_month = (last_day is None or data_ends is None
                            or last_day < data_ends)
        finished = (gone or bool(getattr(l, "complete", False))) and \
            all_stopped.get(ck_key, False) and stopped_in_month
        # A CAMPAIGN THAT NEVER DELIVERED HAS NOTHING TO REPORT.
        #
        # Orders 51217 and 50760 were both sold, cancelled, and never served a
        # single impression - 50760's own notes say so - and both were owed a
        # lifetime, because their end dates fall inside this cycle and a date
        # is all the export gives. There is no report to pull for a campaign
        # with no delivery behind it.
        #
        # This is also what dates a close-out. "Cancelled" and "IO Complete"
        # say a campaign is over and not WHICH MONTH, so without this the flag
        # alone closed out years of finished campaigns into one cycle.
        #
        # With no serving file loaded there is nothing to test against and the
        # dates stand on their own, exactly as before.
        ran = (not served_now
               or served_now.get((_key(l.market), _key(l.client)), 0) > 0)
        # ANY ORDER BEHIND THIS ROW ENDING IN THE WINDOW, not the widest of
        # them. See _order_windows.
        life = (not is_seo(l.product)) and ran and (
            finished or any(cyc.needs_lifetime(en)
                            for _o, _s, en in _order_windows(l) if en))
        if not live and not life:
            continue
        # A PRODUCT THAT DOES NOT BRING A REPORT WITH IT.
        #
        # Website Visitor ID and Additional Billing are invoiced and never
        # appear on a report. Live Chat does appear on one, but is only ever
        # sold alongside another digital product - so none of the three is a
        # reason on its own to expect a report. They still count when the
        # client has something else running: this drops the LINE from earning
        # a row, not the client from the board.
        if not earns_a_report(l.product):
            # It still belongs ON the report, when there is one. Live Chat is
            # sold alongside a digital product and appears on that product's
            # report; it just never brings one with it. Held here and added to
            # the row after the rows exist, so it cannot create one.
            if live and on_a_report(l.product):
                ride_along.setdefault((_key(l.market), _key(l.client)),
                                      set()).add(l.product)
            continue
        # A cancelled line's product still belongs on the report, when one is
        # being pulled. It just cannot be the reason for pulling it.
        if live and not creates_monthly:
            ride_along.setdefault((_key(l.market), _key(l.client)),
                                  set()).add(l.product)
        if l.market in pcache:
            p = pcache[l.market]
        else:
            p = pcache[l.market] = _match_partner(idx, l.market)
        group = (p.group if p and p.group else l.market) or l.market
        for kind, wanted in (("monthly", creates_monthly), ("lifetime", life)):
            if not wanted:
                continue
            # SEO GETS ITS OWN ROW. IT IS A DIFFERENT FILE.
            #
            # A client running SEO and Social Mirror was one row expecting one
            # PDF, and the two reports are not one PDF - the SEO one is pulled
            # by hand outside TapClicks and arrives on its own. One row meant
            # whichever file turned up first satisfied the row and the other
            # was never asked for again.
            #
            # Only the monthly splits. A lifetime is a campaign finishing, and
            # SEO is not sold as a flight that ends.
            if kind == "monthly" and is_seo(l.product):
                kind = "seo"
            k = (_key(l.market), _key(l.client), kind)
            e = rows.get(k)
            if e is None:
                e = rows[k] = Expected(
                    market=l.market, group=group, client=l.client, kind=kind,
                    account_ids=l.account_ids, line_ids=l.line_ids, buyer=l.buyer,
                    # First names, so the report table reads the way people
                    # talk. Judged across every reporter, so two of them
                    # sharing a first name keep their surnames.
                    reporter=first_name(p.reporting_team if p else "",
                                        reporter_pool))
            # SEO belongs to a different person, and whichever line happened to
            # be read first decided the buyer - so a client with one SEO line
            # showed its SEO manager as the buyer for everything it ran.
            if kind == "seo":
                # Its own row, so its own owner - no contest to settle.
                e.buyer = l.buyer or e.buyer
            elif is_seo(l.product):
                seo_buyer.setdefault(k, True)
            else:
                if seo_buyer.get(k, True):     # nothing real on it yet
                    e.buyer = l.buyer or e.buyer
                seo_buyer[k] = False
            if kind == "monthly":
                ran_days[k] = max(ran_days.get(k, 0),
                                  days_in_cycle(cyc, l.starts_on, l.ends_on))
            else:
                # THE ORDER THAT ENDED IN THIS CYCLE, not the client's latest.
                #
                # This took the biggest end date across everything the client
                # runs, so a client with one campaign finishing and another
                # sold to start later had a lifetime row about the LATER order
                # - which is not owed a lifetime and is not what anybody asked
                # about. Southeastern Cooling finished 51648 on 31 August and
                # has 51649 running through September; the row was about 51649
                # and its overlap test compared September against September.
                #
                # An end inside the lifetime window always beats one outside
                # it, and within each of those the later date wins - so a
                # client with two campaigns finishing this cycle still gets the
                # last one, and a client with none keeps the old answer for the
                # "runs past the window" message.
                for _oid, _ost, ends in _order_windows(l):
                    if not ends:
                        continue
                    inside = cyc.needs_lifetime(ends)
                    held = life_end.get(k)
                    held_inside = bool(held) and cyc.needs_lifetime(held)
                    better = (held is None
                              or (inside and not held_inside)
                              or (inside == held_inside and ends > held))
                    if not better:
                        continue
                    life_end[k] = ends
                    life_order[k] = _oid or l.account_ids or ""
                    life_span[k] = (_ost, ends)
                    # WHICH RULE PUT IT HERE. "Order 54169 ends 2026-08-31"
                    # on a row for a campaign that is plainly still live reads
                    # as the tool being wrong, and there is no way to tell from
                    # the row which of the two paths asked for it.
                    if finished and not cyc.needs_lifetime(ends):
                        why = ("every line on this campaign is cancelled or "
                               "complete")
                        if last_day:
                            why += f", and it last delivered {last_day}"
                        e.life_note = (f"Order {life_order[k] or '?'} is dated "
                                       f"to {ends.isoformat()}, but {why}")
                    else:
                        e.life_note = (f"Order {life_order[k] or '?'} ends "
                                       f"{ends.isoformat()}")
            if l.product and l.product not in e.products:
                e.products.append(l.product)
            for st in (getattr(l, "status", "") or "").split(","):
                st = st.strip()
                if st and st not in e.statuses:
                    e.statuses.append(st)
            # ONE STATUS PER ORDER, off the line items. An order with a live
            # line and a cancelled one is a live order, so the one that means
            # "still delivering" wins - a red pill on an order still running
            # is the kind of wrong that gets a report pulled to the wrong date.
            for d in (getattr(l, "detail", None) or []):
                if not isinstance(d, dict):
                    continue
                oid, st = str(d.get("order") or ""), (d.get("status") or "").strip()
                if not oid or not st:
                    continue
                if _status_rank(st) < _status_rank(e.order_status.get(oid, "")):
                    e.order_status[oid] = st
            # A client's lifetime covers several products, so its line ids are
            # the union of them - not whichever line happened to be first.
            for lid in (l.line_ids or "").split(","):
                lid = lid.strip()
                if lid and lid not in e.line_ids:
                    e.line_ids = (e.line_ids + ", " + lid).strip(", ")
            # AND SO ARE ITS ORDER IDS.
            #
            # River Valley Builders' lifetime row read "31050" while the report
            # itself, opened, correctly showed 31050 31171 43182 - because the
            # line ids were unioned here and the order ids were whatever the
            # first line carried. The row is about the campaign, and the
            # campaign is every order behind it.
            for oid in (l.account_ids or "").replace(",", " ").split():
                oid = oid.strip()
                if oid and oid not in e.account_ids.split():
                    e.account_ids = (e.account_ids + " " + oid).strip()
            if l.starts_on and (e.starts_on is None or l.starts_on < e.starts_on):
                e.starts_on = l.starts_on
            if l.ends_on and (e.ends_on is None or l.ends_on > e.ends_on):
                e.ends_on = l.ends_on

    # THE LIFETIME COVERS THE CAMPAIGN THAT ENDED, not everything the client
    # has ever run. Overlapping orders are one continuous campaign, so the
    # start still reaches back across them - but an order that is still running
    # to October says nothing about the one that finished in July, and letting
    # it set the end read as "a lifetime is needed" on a campaign with months
    # left in it.
    for (mk, ck, kind), e in rows.items():
        if kind != "lifetime":
            continue
        end = life_end.get((mk, ck, kind)) or (span.get((mk, ck)) or [None, None])[1]
        start = (span.get((mk, ck)) or [None, None])[0]
        e.ends_on = end
        if start and end and start <= end:
            e.starts_on = start
        # AND ITS ORDER IDS ARE EVERY ORDER INSIDE THAT RANGE.
        #
        # River Valley Builders' lifetime row read "31050" while the report
        # itself was correctly filed as 31050 31171 43182. A lifetime covers
        # the campaign, and overlapping orders are one campaign - so the row
        # has to name the same orders the report does, or the person holding
        # the row cannot tell it is the same thing.
        if e.starts_on and e.ends_on:
            have = e.account_ids.split()
            for order, w_start, w_end, _product in windows.get((mk, ck), []):
                if not order:
                    continue
                if w_end and w_end < e.starts_on:
                    continue
                if w_start and w_start > e.ends_on:
                    continue
                for oid in order.replace(",", " ").split():
                    if oid and oid not in have:
                        have.append(oid)
            e.account_ids = " ".join(have)

    # AND THE RIDE-ALONGS GO ON THE ROWS THAT EXIST.
    #
    # Live Chat is on the report of whatever it was sold with, so a report
    # without it is missing something - but a client running Live Chat and
    # nothing else is not owed a report at all. Added here, after the rows are
    # built, which is the difference between the two.
    # AND THEY GO ON THE MONTHLY, NOT ON THE SEO ROW. SEO is a separate file
    # pulled outside TapClicks; Live Chat does not appear on it.
    for (mk, ck, kind), e in list(rows.items()):
        if kind == "seo":
            continue
        for product in sorted(ride_along.get((mk, ck), ())):
            if product not in e.products:
                e.products.append(product)

    # SEO AND LIVE CHAT AND NOTHING ELSE IS TWO REPORTS, NOT ONE.
    #
    # Live Chat never brings a report with it - it is sold alongside something
    # else and appears on that something's report. When the something else is
    # SEO, that report is a separate file pulled outside TapClicks, so there is
    # nowhere for the Live Chat to go: Alegre Construction had an SEO row, a
    # live Live Chat line, and no monthly at all, so the Live Chat quietly
    # vanished and the board could only say "worth a closer look".
    #
    # The client IS running another product, which is the test Live Chat has
    # always had. So they are owed a monthly, and it carries the Live Chat.
    for (mk, ck), products in ride_along.items():
        if not products or (mk, ck, "monthly") in rows:
            continue
        seo_row = rows.get((mk, ck, "seo"))
        if seo_row is None:
            continue                     # nothing else running: no report owed
        rows[(mk, ck, "monthly")] = Expected(
            market=seo_row.market, group=seo_row.group, client=seo_row.client,
            kind="monthly", account_ids=seo_row.account_ids,
            line_ids=seo_row.line_ids, buyer=seo_row.buyer,
            reporter=seo_row.reporter, products=sorted(products),
            starts_on=seo_row.starts_on, ends_on=seo_row.ends_on,
            statuses=list(seo_row.statuses),
            order_status=dict(seo_row.order_status))

    # AND EVERY PILL GETS ITS COLOR.
    #
    # Last, because the order ids on a row are not settled until here: the
    # window test above adds orders that never went through the loop which
    # reads statuses, and a monthly row picks up orders the same way. Anything
    # still without one is genuinely not in the export.
    for (mk, ck, _kind), e in rows.items():
        known = order_status_all.get((mk, ck), {})
        for oid in e.account_ids.split():
            if not e.order_status.get(oid) and known.get(oid):
                e.order_status[oid] = known[oid]

    # AND WHERE A CAMPAIGN ACTUALLY STOPPED, for the ones that were cancelled.
    from .serving import last_served, serving_later
    stopped = last_served(db, period)
    # AND NOT IF IT IS STILL DELIVERING IN A LATER MONTH. The stop day is read
    # out of this cycle's period alone, so a campaign that ran to 28 August and
    # kept going into September reads as one that stopped on the 28th.
    still_going = serving_later(db, period)
    for (mk, ck, kind), e in rows.items():
        if kind != "lifetime":
            continue
        if not any_stopped.get((mk, ck)):
            continue
        if (mk, ck) in still_going:
            continue
        day = stopped.get((mk, ck))
        # Only when it is EARLIER than what the order says AND earlier than the
        # data itself runs. A file covering only part of the month otherwise
        # says every campaign on the board stopped on the day the data ran out
        # - which is what "stopped 2026-07-31" was, on every row at once.
        if (day and e.ends_on and day < e.ends_on
                and (data_ends is None or day < data_ends)):
            e.stopped_on = day

    # HOW MANY DAYS IT ACTUALLY SERVED, IF ANYBODY KNOWS.
    #
    # Everything above this line reads dates: a line sold January to December
    # and paused on the 2nd is indistinguishable from one paused on the 30th,
    # and "IO Complete" is where every campaign that ever ended comes to rest.
    # The serving file is the only thing that answers the actual question, so
    # where it has an answer it replaces the inference rather than joining it.
    #
    # A MONTH WITH NO FILE LOADED IS NOT A MONTH WHERE NOBODY RAN. Absent means
    # fall back to the dates - concluding otherwise would empty the board on
    # the strength of a file that was never uploaded.
    served = served_now

    # Under a week in the month, and nothing has arrived for it: not owed.
    # A report that HAS turned up is never hidden - somebody pulled it, and
    # taking it off the board would lose the work rather than save it.
    # A CLIENT THE LOADED FILE DOES NOT MENTION SERVED NOTHING. Otherwise the
    # file can only ever add rows, and taking off the ones that did not run is
    # the entire reason for loading it. The risk is a client the two tools
    # spell differently reading as dark - so that case gets its own reason and
    # its own count on the orders page, rather than looking like an answer.
    unmatched: set[tuple[str, str]] = set()
    short: dict[tuple[str, str, str], int] = {}
    for k, n in ran_days.items():
        if k not in rows:
            continue
        # SEO IS NOT IN THE SERVING FILE AND NEVER WILL BE.
        #
        # The serving file is ad delivery - impressions against days. SEO is
        # not served, so it is absent from that file for every SEO client every
        # month, and the rule that reads absence as "it did not run" took the
        # whole of Whitley's SEO list off the board: "not in the serving file
        # at all - either it did not run, or the serving file spells this
        # client differently to the order export". Fourteen rows of one wrong
        # answer, and the answer accused the file of being misspelled.
        #
        # These reports are pulled by hand and uploaded, so the row has to
        # stay. It is the only thing telling anybody they are owed.
        if not served:
            days = n
        else:
            days = served.get((k[0], k[1]), 0)
            if (k[0], k[1]) not in served:
                unmatched.add((k[0], k[1]))
        if days < MIN_DAYS_IN_MONTH:
            short[k] = days

    # And a lifetime that has already gone out is not owed again.
    done = _lifetimes_delivered(db, period)
    # Clients that have had a monthly before, for the one-month-campaign rule
    # below: "first monthly" only means anything if there has not been one.
    seen_before = _clients_with_a_monthly(db, period)

    _attach_reports(db, period, rows)
    # EVERY RULE BELOW IS A RULE ABOUT THE USUAL CASE. Somebody who knows this
    # client gets the last word, so a row marked "needs a report" by hand
    # survives all of them, and says who kept it there.
    marks = overrides(db, period)
    for k in list(rows):
        mk, ck, kind = k
        e = rows[k]
        m = marks.get(e.ident)
        if m is not None and getattr(m, "reason", "") == "needed":
            e.forced_by = m.marked_by or "kept on by hand"
            e.forced_note = m.note or ""
            continue
        if kind == "monthly" and k in short and e.report is None:
            if skipped is not None:
                n = short[k]
                # SAY WHERE THE NUMBER CAME FROM. "Ran 3 days" off the flight
                # dates is a guess and reads like a fact; off the serving file
                # it IS a fact, and the difference is the whole point of
                # loading the file.
                if (mk, ck) in served:
                    why = ("did not serve at all this month, per the serving file"
                           if n == 0 else
                           f"served {n} day{'' if n == 1 else 's'} this month, "
                           f"per the serving file")
                elif served:
                    # NOT THE SAME CLAIM. The file was loaded and this client
                    # is not in it - which is either a campaign that went dark
                    # or two tools spelling a name differently, and saying
                    # which one out loud is how the second gets noticed.
                    why = ("not in the serving file at all - either it did not "
                           "run, or the serving file spells this client "
                           "differently to the order export")
                else:
                    why = "ran %d day%s this month" % (n, "" if n == 1 else "s")
                skipped.append({"market": e.market, "client": e.client,
                                "why": why, "kind": "monthly", "days": n,
                                "starts": e.starts_on, "ends": e.ends_on})
            del rows[k]
            continue
        # AN ENDED ORDER THAT OVERLAPS A RUNNING ONE IS NOT A FINISHED
        # CAMPAIGN. Field Of Dreams' Mobile Conquesting finished on 31 July
        # while their Meta ran to 14 October - the client is not dark, the buy
        # simply changed shape, and a campaign-to-date report in the middle of
        # a live campaign is not what a lifetime is for.
        if kind == "lifetime" and e.report is None:
            # AGAINST THE ENDING ORDER'S OWN WINDOW, not the client's whole
            # flight. Southeastern Cooling's 51648 ran to 31 August and 51649
            # starts on 1 September: they do not overlap, so the lifetime is
            # owed. Compared against the client's widest dates - May to the end
            # of September - everything overlaps everything.
            _own_start, _own_end = life_span.get(k, (e.starts_on, e.ends_on))
            overlap = _running_overlap(windows.get((mk, ck), []),
                                       life_order.get(k, ""),
                                       _own_start, _own_end,
                                       cyc.lifetime_cutoff)
            if overlap:
                if skipped is not None:
                    skipped.append({"market": e.market, "client": e.client,
                                    "why": f"order {overlap} is still running "
                                           f"and overlaps this one",
                                    "kind": "lifetime", "days": 0,
                                    "starts": e.starts_on, "ends": e.ends_on})
                del rows[k]
                continue

        if kind == "lifetime" and e.report is None and ck in done:
            # ONLY IF THE LIFETIME THAT WENT OUT COVERED THIS ENDING.
            #
            # A campaign whose end date is extended after its lifetime was sent
            # goes back on the normal schedule: monthlies again, and a new
            # lifetime when it finishes for real. So the delivered cycle has to
            # be at or after the month the campaign now ends in - if the end
            # moved out past it, the report that went out was about a different
            # finish and this one is still owed.
            when = done[ck]
            if e.ends_on and when >= e.ends_on.strftime("%Y-%m"):
                if skipped is not None:
                    skipped.append({"market": e.market, "client": e.client,
                                    "why": f"lifetime already delivered in {month_label(when)}",
                                    "kind": "lifetime", "days": 0,
                                    "starts": e.starts_on, "ends": e.ends_on})
                del rows[k]

    # A CAMPAIGN SHORTER THAN A MONTH THAT IS ENDING GETS ONE REPORT, NOT TWO.
    #
    # Its first monthly and its lifetime would cover the same days and carry
    # the same numbers, and the lifetime is the one the client is owed. Only
    # when it IS the first - a client who has had monthlies before is mid
    # relationship, and last month's report is not this month's.
    for (mk, ck, kind), e in list(rows.items()):
        if kind != "monthly" or e.report is not None or e.forced_by:
            continue
        life = rows.get((mk, ck, "lifetime"))
        if life is None or ck in seen_before:
            continue
        if not (life.starts_on and life.ends_on):
            continue
        days = (life.ends_on - life.starts_on).days + 1
        if days <= SHORT_CAMPAIGN_DAYS:
            if skipped is not None:
                skipped.append({"market": e.market, "client": e.client,
                                "why": f"campaign ran {days} days and its "
                                       f"lifetime covers the same ground",
                                "kind": "monthly", "days": days,
                                "starts": life.starts_on, "ends": life.ends_on})
            del rows[(mk, ck, kind)]

    # A ROW SOMEBODY PUT ON BY HAND WHEN THE EXPORT HAS NEVER HEARD OF IT.
    #
    # The "needed" override has always been able to KEEP a row the rules would
    # have removed, and that covers most of what it is for. It could not create
    # one - so approving a client whose orders are not in the export at all
    # recorded a decision and changed nothing, silently, which is the worst of
    # the three things it could have done. 53872 is exactly that case: it is on
    # the list precisely BECAUSE the export does not have it.
    #
    # These rows carry no products and no dates, because nothing knows them.
    # They exist so the client is on the cycle and a report can be uploaded
    # against them, which is what approving one is asking for.
    for ident, m in overrides(db, period).items():
        if getattr(m, "reason", "") != "needed":
            continue
        mk, _, rest = (ident or "").partition("|")
        ck, _, kind = rest.partition("|")
        if not mk or not ck or (mk, ck, kind) in rows:
            continue
        p = _match_partner(idx, m.market or "")
        rows[(mk, ck, kind)] = Expected(
            market=m.market or "", group=(p.group if p and p.group else m.market) or "",
            client=m.client or "", kind=kind or "monthly",
            # The order number off the row that was approved, so the board
            # shows it and a search by order id can find it.
            account_ids=(getattr(m, "ref", "") or ""),
            # AND WHAT IT IS A REPORT FOR. These rows used to carry no products
            # at all - nothing knew them - so the board showed a blank in the
            # column every other row fills in, and the product checks had
            # nothing to judge the PDF against. Whoever added the row said.
            products=[x.strip() for x in
                      (getattr(m, "products", "") or "").split(",") if x.strip()],
            reporter=first_name(p.reporting_team if p else "", reporter_pool),
            buyer=(p.buyer if p else ""),
            forced_by=m.marked_by or "put on by hand",
            forced_note=m.note or "")

    out = list(rows.values())
    _stamp_done(db, period, out)
    out.sort(key=lambda e: (e.group.lower(), e.market.lower(),
                            e.client.lower(), e.kind))
    return out


def _stamp_done(db: Session, period: str, rows: list[Expected]) -> None:
    """Carry this cycle's hand-checked rows onto the board.

    Done here rather than in the view so every reader agrees - the board, the
    CSV export and the partner counts were three different answers to "is this
    partner finished" the first time this only lived in the page.
    """
    from .db import CycleDone
    marks = {m.ident: m for m in db.scalars(
        select(CycleDone).where(CycleDone.period == period)).all()}
    if not marks:
        return
    for e in rows:
        m = marks.get(e.ident)
        if m is not None and getattr(m, "reason", "") != "needed":
            e.done_by = m.marked_by or "checked off"
            e.done_note = m.note or ""
            e.done_kind = getattr(m, "reason", "") or "done"


# Products that never get a report of their own. A client running one of these
# and nothing else is not being reported on, so an order carrying only these is
# not a live campaign anybody is waiting on - and it must not hold up the
# lifetime of the campaign that IS finishing.
NO_OWN_REPORT = ("live chat",)


def _no_own_report(product: str) -> bool:
    return (product or "").strip().lower() in NO_OWN_REPORT


def _running_overlap(windows, own_order: str, starts, ends, cutoff) -> str:
    """The order id of a live campaign this ending one overlaps, or "".

    A client whose Mobile Conquesting finished while their Meta runs to October
    is not dark: the buy changed shape. A lifetime is for a campaign that has
    actually finished, so an ended order that overlaps a running one waits.

    LIVE CHAT ALONE IS NOT A CAMPAIGN THAT IS STILL RUNNING. It gets no report
    of its own, so an order carrying only Live Chat is nothing anybody is
    waiting on - and River Valley Builders' lifetime was held up by two of
    them.
    """
    if not ends:
        return ""
    # Which products each order carries, so "Live Chat only" can be told from
    # "Live Chat and Display".
    by_order: dict[str, set] = {}
    for order, _s, _e, product in windows:
        by_order.setdefault(order or "", set()).add(product)
    for order, w_start, w_end, _product in windows:
        if order and own_order and order == own_order:
            continue                       # the campaign cannot outlast itself
        if all(_no_own_report(p) for p in by_order.get(order or "", set())):
            continue
        still_running = w_end is None or w_end > cutoff
        if not still_running:
            continue
        if w_start is None or w_start <= ends:
            if w_end is None or starts is None or w_end >= starts:
                return order or "another order"
    return ""


def _clients_with_a_monthly(db: Session, period: str) -> set[str]:
    """Clients that have had a monthly report before this cycle."""
    rows = db.execute(
        select(Report.client).where(Report.is_lifetime.is_(False),
                                    Report.period < period).distinct()).all()
    return {_key(c) for (c,) in rows if c}


def _lifetimes_delivered(db: Session, period: str) -> dict[str, str]:
    """Clients that already have a lifetime report, and the newest cycle it
    was delivered in.

    A lifetime is owed once, when the campaign ends. If one has already gone
    out, asking for it again every month is a row nobody can clear - and the
    only way to clear it today is to pull a duplicate report.
    """
    rows = db.execute(
        select(Report.client, func.max(Report.period))
        .where(Report.is_lifetime.is_(True), Report.period <= period)
        .group_by(Report.client)).all()
    out: dict[str, str] = {}
    for client, when in rows:
        k = _key(client)
        if k and (k not in out or when > out[k]):
            out[k] = when or ""
    return out


def lifetimes_delivered(db: Session, limit: int = 400) -> list[dict]:
    """Every lifetime report on the board, newest first - the record of which
    campaigns have had theirs, so nobody pulls a second one."""
    rows = db.scalars(
        select(Report).where(Report.is_lifetime.is_(True))
        .order_by(Report.period.desc(), Report.market.asc(),
                  Report.client.asc()).limit(limit)).all()
    return [{"id": r.id, "client": r.client, "market": r.market,
             "period": r.period, "orders": r.account_ids,
             "state": r.review_state} for r in rows]


def _attach_reports(db: Session, period: str,
                    rows: dict[tuple[str, str, str], Expected]) -> None:
    """Match arrived reports to expected ones on account id first, then name.

    Account ids are the reliable join - clients are typed differently in the
    two systems ("NORTH CAROLINA FURNITURE MART" against "North Carolina
    Furniture Mart"), and a lifetime and a monthly for the same client are
    distinguished only by the report's own lifetime flag.
    """
    # `checks` is the full 27-line pass/fail list per report and the board never
    # prints it - it is a JSON column decoded for every report on the cycle for
    # nothing. Deferred, so the report page still gets it on demand.
    from sqlalchemy.orm import defer
    reports = db.scalars(select(Report).where(Report.period == period)
                         .options(defer(Report.checks))).all()
    by_client = {(_key(e.client), e.kind): e for e in rows.values()}
    by_account: dict[tuple[str, str], Expected] = {}
    for e in rows.values():
        for a in ACC.findall(e.account_ids or ""):
            by_account[(a, e.kind)] = e

    for r in reports:
        # THREE ROWS A CLIENT CAN OWE, not two. An SEO report is a different
        # file from the digital one, so it has to find the SEO row - otherwise
        # whichever file arrived first satisfied whichever row it matched and
        # the other was never asked for again.
        kind = ("lifetime" if r.is_lifetime else
                "seo" if getattr(r, "is_seo", False) else "monthly")
        hit = None
        for a in ACC.findall(r.account_ids or "") or []:
            hit = by_account.get((a, kind))
            if hit:
                break
        if hit is None:
            hit = by_client.get((_key(r.client), kind))
        # EVERY REPORT THAT ALREADY EXISTS PREDATES THE SPLIT.
        #
        # `is_seo` is new, so every report in the database is stamped False -
        # including the SEO ones already uploaded this cycle, whose row is now
        # the SEO row. Without this they would all come off the board as never
        # delivered, on a deploy that was supposed to be about tidying.
        #
        # So a monthly with no monthly row falls back to the SEO row and the
        # other way round. It only ever fires when the row it wanted is not
        # there, so a client owed both keeps both files apart.
        if hit is None and kind in ("monthly", "seo"):
            other = "seo" if kind == "monthly" else "monthly"
            for a in ACC.findall(r.account_ids or "") or []:
                hit = by_account.get((a, other))
                if hit:
                    break
            if hit is None:
                hit = by_client.get((_key(r.client), other))
        if hit is not None and hit.report is None:
            hit.report = r


@dataclass
class GroupRow:
    group: str
    target: str
    expected: list[Expected]
    buyer: str = ""
    reporter: str = ""
    trainer: str = ""
    # Only set when this group actually has an SEO line item this cycle. SEO is
    # pulled outside TapClicks and belongs to a different person, so a card
    # showing only the buyer sends the chase to the wrong desk.
    seo: str = ""

    @property
    def counts(self) -> dict:
        c = {s: 0 for s in STATES}
        for e in self.expected:
            c[e.state] += 1
        return c

    @property
    def ready(self) -> bool:
        return bool(self.expected) and all(e.ready for e in self.expected)

    @property
    def pct(self) -> int:
        if not self.expected:
            return 0
        return round(100 * sum(1 for e in self.expected if e.ready) / len(self.expected))

    @property
    def markets(self) -> list[str]:
        seen = []
        for e in self.expected:
            if e.market not in seen:
                seen.append(e.market)
        return seen


def markets_by_group(db: Session) -> dict[str, list[str]]:
    """group -> its markets, in one pass over the roster.

    A group is a set of markets - "7 Mountains PA" and "7 Mountains PA Altoona"
    are one partner - and reports are stored against the market, so acting on a
    whole partner means acting on all of them.
    """
    out: dict[str, list[str]] = {}
    for p in _partner_index(db).values():
        g = p.group or p.partner
        names = out.setdefault(g, [])
        if p.partner not in names:
            names.append(p.partner)
    return out


def market_names_for_group(db: Session, group: str) -> list[str]:
    """The markets under one group. Building the whole index for each of 145
    groups in turn is what made the board slow, so callers in a loop should ask
    for markets_by_group() once instead."""
    out = list(markets_by_group(db).get(group, []))
    if group not in out:
        out.append(group)
    return out


def by_group(db: Session, period: str,
             expected: list[Expected] | None = None) -> list[GroupRow]:
    exp = expected if expected is not None else expected_for(db, period)
    idx = _partner_index(db)
    # WHERE THE GROUP TAKES DELIVERY. The first partner with an answer used to
    # win, so a group whose first market said nothing shipped to Drive even
    # when the market beside it said Dropbox. Anything other than Drive is a
    # deliberate exception somebody typed, so it wins for the whole group.
    targets: dict[str, str] = {}
    for p in idx.values():
        g = p.group or p.partner
        t = (p.delivery_target or "").strip()
        if not t:
            continue
        if g not in targets or (targets[g] == "drive" and t != "drive"):
            targets[g] = t

    # The roster's own entry for the group, so a card can say who owns it
    # without every caller having to look the partner up again.
    people = {}
    for p in idx.values():
        g = p.group or p.partner
        people.setdefault(g, p)

    # Every name used in each role, so a first name that is not unique inside
    # its own role keeps its surname.
    name_pool = {
        "buyer": {(p.buyer or "") for p in idx.values()}
                 | {e.buyer or "" for e in exp},
        "reporter": {(p.reporting_team or "") for p in idx.values()},
        "trainer": {(p.trainer or "") for p in idx.values()},
        "seo": {(p.seo or "") for p in idx.values()},
    }

    groups: dict[str, list[Expected]] = {}
    for e in exp:
        groups.setdefault(e.group, []).append(e)
    out = []
    for g, rows in groups.items():
        p = people.get(g)
        # One name on the card, not a list. A group's line items can carry
        # several campaign managers, and "Anna Halligan, Bella Duddy" answers
        # the question "who do I chase" with "work it out yourself". When they
        # disagree, the reporting breakout's buyer is the answer.
        # SEO is owned by someone else and is not one of the buyers being
        # counted - a client running SEO alone would otherwise look like a
        # second buyer and push the whole group onto the roster fallback.
        real = [e for e in rows
                if not (e.products and all(is_seo(x) for x in e.products))]
        buyers = [b for b in dict.fromkeys(e.buyer for e in (real or rows) if e.buyer)]
        roster_buyer = p.buyer if p else ""
        buyer = buyers[0] if len(buyers) == 1 else (roster_buyer or ", ".join(buyers))

        has_seo = any(is_seo(prod) for e in rows for prod in (e.products or []))
        # FIRST NAMES. That is how everybody here refers to each other, and a
        # card already carrying a partner name, a percentage, a progress bar and
        # six state pills does not need a surname on top of it. Shortened per
        # role, so the trainer Katie and the buyer Katie stay two people - and a
        # surname comes back on both the moment two of them share a first name
        # inside the SAME role.
        out.append(GroupRow(
            group=g, target=targets.get(g, ""), expected=rows,
            buyer=first_name(buyer, name_pool["buyer"]),
            reporter=first_name(p.reporting_team if p else "", name_pool["reporter"]),
            trainer=first_name(p.trainer if p else "", name_pool["trainer"]),
            seo=first_name((p.seo if p else "") if has_seo else "",
                           name_pool["seo"])))
    out.sort(key=lambda g: (g.ready, g.group.lower()))   # unfinished first
    return out


def summary(expected: list[Expected]) -> dict:
    c = {s: 0 for s in STATES}
    for e in expected:
        c[e.state] += 1
    c["total"] = len(expected)
    c["lifetimes"] = sum(1 for e in expected if e.kind == "lifetime")
    return c
