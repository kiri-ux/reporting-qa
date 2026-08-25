"""What a cycle owes, what has arrived, and whether a partner can ship.

The order list says what should exist. The batches say what turned up. This
joins the two into one row per expected report, which is what the cycle board
renders and what the delivery packager reads.
"""
from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from .cycle import Cycle, cycle_for
from .db import OrderLine, Partner, Report
from .partners import is_seo

ACC = re.compile(r"\b\d{4,6}\b")

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
    report: Report | None = None

    @property
    def state(self) -> str:
        """missing | in | warnings | errors | needs_fix | ready"""
        return self.report.board_state if self.report else "missing"

    @property
    def ready(self) -> bool:
        return bool(self.report and self.report.ready)

    @property
    def ident(self) -> str:
        return f"{_key(self.market)}|{_key(self.client)}|{self.kind}"


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


def expected_for(db: Session, period: str) -> list[Expected]:
    """Every report this cycle owes, joined to whatever has arrived.

    A client owes a monthly if any of its order lines was live during the data
    month, and a lifetime if any line ENDED inside the cycle's lifetime window
    - which runs past month end to the 3rd business day, so a campaign that
    finished on the 1st ships with the monthlies instead of waiting a month.
    """
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
    cols = db.execute(select(
        OrderLine.market, OrderLine.client, OrderLine.account_ids,
        OrderLine.line_ids, OrderLine.buyer, OrderLine.product,
        OrderLine.starts_on, OrderLine.ends_on).where(
            or_(OrderLine.ends_on.is_(None), OrderLine.ends_on >= cyc.starts_on),
            or_(OrderLine.starts_on.is_(None),
                OrderLine.starts_on <= cyc.ends_on))).all()

    # The client's whole flight, aggregated by the database rather than by
    # walking every line again in Python. This is the only reason the finished
    # lines are read at all, and there are a lot of them.
    from sqlalchemy import func
    spans = db.execute(select(
        OrderLine.market, OrderLine.client,
        func.min(OrderLine.starts_on), func.max(OrderLine.ends_on))
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
    for l in cols:
        if excluded(l.market):
            continue
        live = cyc.was_live(l.starts_on, l.ends_on)
        life = cyc.needs_lifetime(l.ends_on)
        if not live and not life:
            continue
        if l.market in pcache:
            p = pcache[l.market]
        else:
            p = pcache[l.market] = _match_partner(idx, l.market)
        group = (p.group if p and p.group else l.market) or l.market
        for kind, wanted in (("monthly", live), ("lifetime", life)):
            if not wanted:
                continue
            k = (_key(l.market), _key(l.client), kind)
            e = rows.get(k)
            if e is None:
                e = rows[k] = Expected(
                    market=l.market, group=group, client=l.client, kind=kind,
                    account_ids=l.account_ids, line_ids=l.line_ids, buyer=l.buyer,
                    reporter=(p.reporting_team if p else ""))
            # SEO belongs to a different person, and whichever line happened to
            # be read first decided the buyer - so a client with one SEO line
            # showed its SEO manager as the buyer for everything it ran.
            if is_seo(l.product):
                seo_buyer.setdefault(k, True)
            else:
                if seo_buyer.get(k, True):     # nothing real on it yet
                    e.buyer = l.buyer or e.buyer
                seo_buyer[k] = False
            if l.product and l.product not in e.products:
                e.products.append(l.product)
            # A client's lifetime covers several products, so its line ids are
            # the union of them - not whichever line happened to be first.
            for lid in (l.line_ids or "").split(","):
                lid = lid.strip()
                if lid and lid not in e.line_ids:
                    e.line_ids = (e.line_ids + ", " + lid).strip(", ")
            if l.starts_on and (e.starts_on is None or l.starts_on < e.starts_on):
                e.starts_on = l.starts_on
            if l.ends_on and (e.ends_on is None or l.ends_on > e.ends_on):
                e.ends_on = l.ends_on

    # The span is built in the pass above, from every line the client runs -
    # including the ones that are neither live nor ending this cycle.
    for (mk, ck, kind), e in rows.items():
        if kind == "lifetime" and (mk, ck) in span:
            e.starts_on, e.ends_on = span[(mk, ck)]

    _attach_reports(db, period, rows)
    out = list(rows.values())
    out.sort(key=lambda e: (e.group.lower(), e.market.lower(),
                            e.client.lower(), e.kind))
    return out


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
        kind = "lifetime" if r.is_lifetime else "monthly"
        hit = None
        for a in ACC.findall(r.account_ids or "") or []:
            hit = by_account.get((a, kind))
            if hit:
                break
        if hit is None:
            hit = by_client.get((_key(r.client), kind))
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
        out.append(GroupRow(
            group=g, target=targets.get(g, ""), expected=rows,
            buyer=buyer,
            reporter=(p.reporting_team if p else ""),
            trainer=(p.trainer if p else ""),
            seo=((p.seo if p else "") if has_seo else "")))
    out.sort(key=lambda g: (g.ready, g.group.lower()))   # unfinished first
    return out


def summary(expected: list[Expected]) -> dict:
    c = {s: 0 for s in STATES}
    for e in expected:
        c[e.state] += 1
    c["total"] = len(expected)
    c["lifetimes"] = sum(1 for e in expected if e.kind == "lifetime")
    return c
