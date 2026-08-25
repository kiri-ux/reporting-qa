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

ACC = re.compile(r"\b\d{4,6}\b")

# Test rows that must never appear on a board, block a delivery, or count
# toward anyone's workload.
EXCLUDED_PARTNERS = {"dummy partner", "test partner", "test", "zzz test"}


def excluded(market: str) -> bool:
    return (market or "").strip().lower() in EXCLUDED_PARTNERS


def _key(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


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

    rows: dict[tuple[str, str, str], Expected] = {}
    for l in db.scalars(select(OrderLine)).all():
        if excluded(l.market):
            continue
        live = cyc.was_live(l.starts_on, l.ends_on)
        life = cyc.needs_lifetime(l.ends_on)
        if not live and not life:
            continue
        p = _match_partner(idx, l.market)
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

    # THE FLIGHT SPANS EVERY ORDER, not just the one that ended.
    #
    # A lifetime entry is created by the order line that ended inside the
    # window, so taking its dates gives the flight of that ONE order. A client
    # with two overlapping orders - one 2024-2025, one 2025-2026 - would then
    # be told to pull from 2025, losing the first year. The range is the
    # earliest start and the latest end across everything that client runs.
    span: dict[tuple[str, str], list] = {}
    for l in db.scalars(select(OrderLine)).all():
        if excluded(l.market):
            continue
        k = (_key(l.market), _key(l.client))
        cur = span.get(k)
        if cur is None:
            span[k] = [l.starts_on, l.ends_on]
            continue
        if l.starts_on and (cur[0] is None or l.starts_on < cur[0]):
            cur[0] = l.starts_on
        if l.ends_on and (cur[1] is None or l.ends_on > cur[1]):
            cur[1] = l.ends_on
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
    reports = db.scalars(select(Report).where(Report.period == period)).all()
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


def by_group(db: Session, period: str,
             expected: list[Expected] | None = None) -> list[GroupRow]:
    exp = expected if expected is not None else expected_for(db, period)
    idx = _partner_index(db)
    targets: dict[str, str] = {}
    for p in idx.values():
        g = p.group or p.partner
        if p.delivery_target and g not in targets:
            targets[g] = p.delivery_target

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
        buyers = [b for b in dict.fromkeys(e.buyer for e in rows if e.buyer)]
        out.append(GroupRow(
            group=g, target=targets.get(g, ""), expected=rows,
            buyer=", ".join(buyers) or (p.buyer if p else ""),
            reporter=(p.reporting_team if p else ""),
            trainer=(p.trainer if p else "")))
    out.sort(key=lambda g: (g.ready, g.group.lower()))   # unfinished first
    return out


def summary(expected: list[Expected]) -> dict:
    c = {s: 0 for s in STATES}
    for e in expected:
        c[e.state] += 1
    c["total"] = len(expected)
    c["lifetimes"] = sum(1 for e in expected if e.kind == "lifetime")
    return c
