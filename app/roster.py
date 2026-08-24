"""Order-level list: what should arrive, who owns it, and which campaigns just
ended and therefore owe a lifetime report."""
from __future__ import annotations

import csv
import datetime as dt
import io
import re
from dateutil import parser as dp

from sqlalchemy import select
from sqlalchemy.orm import Session

from .db import OrderLine, Report

COLUMN_ALIASES = {
    "market": {"market", "station", "partner"},
    "client": {"client", "client name", "campaign", "advertiser"},
    "account_ids": {"account", "account id", "account ids", "campaign id", "#"},
    "campaign": {"campaign name", "campaign", "order", "line"},
    "starts_on": {"start", "start date", "campaign start date", "flight start"},
    "ends_on": {"end", "end date", "campaign end date", "flight end", "due"},
    "buyer": {"buyer"},
    "team_member": {"p&a team member", "team member", "pa team member", "owner"},
    "buyer_email": {"buyer email"},
    "team_email": {"team email", "team member email"},
}


def _norm(h: str) -> str:
    return re.sub(r"\s+", " ", (h or "").strip().lower()).strip("*: ")


def _map_headers(headers: list[str]) -> dict[str, int]:
    out: dict[str, int] = {}
    for i, h in enumerate(headers):
        n = _norm(h)
        for field, names in COLUMN_ALIASES.items():
            if n in names and field not in out:
                out[field] = i
    return out


def _date(v):
    v = (v or "").strip()
    if not v:
        return None
    try:
        return dp.parse(v).date()
    except Exception:
        return None


ACC = re.compile(r"\b\d{4,6}\b")


def import_order_csv(db: Session, raw: bytes, replace: bool = True) -> int:
    text = raw.decode("utf-8-sig", errors="replace")
    rows = list(csv.reader(io.StringIO(text)))
    if not rows:
        return 0
    header_idx = 0
    for i, r in enumerate(rows[:10]):
        if _map_headers(r).get("client") is not None or _map_headers(r).get("market") is not None:
            header_idx = i
            break
    cols = _map_headers(rows[header_idx])
    if "client" not in cols:
        raise ValueError("Could not find a client or campaign column in the CSV header.")

    if replace:
        db.query(OrderLine).delete()

    n = 0
    for r in rows[header_idx + 1:]:
        if not any(c.strip() for c in r):
            continue

        def g(field, default=""):
            i = cols.get(field)
            return (r[i].strip() if i is not None and i < len(r) else default)

        client = g("client")
        if not client or client.lower() in {"total", "market"}:
            continue
        accounts = g("account_ids") or " ".join(ACC.findall(client))
        db.add(OrderLine(
            market=g("market"), client=client, account_ids=accounts,
            campaign=g("campaign") or client,
            starts_on=_date(g("starts_on")), ends_on=_date(g("ends_on")),
            buyer=g("buyer"), team_member=g("team_member"),
            buyer_email=g("buyer_email"), team_email=g("team_email"),
        ))
        n += 1
    db.commit()
    return n


def _keyify(client: str, accounts: str) -> set[str]:
    ids = set(ACC.findall(accounts or "")) | set(ACC.findall(client or ""))
    return ids


def attach_owners(db: Session, report: Report) -> None:
    """Stamp buyer and team member onto a report by account id, then name."""
    ids = _keyify(report.client, report.account_ids)
    for ol in db.scalars(select(OrderLine)).all():
        if ids & _keyify(ol.client, ol.account_ids):
            report.owner_buyer = ol.buyer
            report.owner_team = ol.team_member
            if not report.market:
                report.market = ol.market
            return
    norm = re.sub(r"[^a-z0-9]", "", (report.client or "").lower())
    if not norm:
        return
    for ol in db.scalars(select(OrderLine)).all():
        if norm and norm == re.sub(r"[^a-z0-9]", "", ol.client.lower()):
            report.owner_buyer, report.owner_team = ol.buyer, ol.team_member
            if not report.market:
                report.market = ol.market
            return


def completeness(db: Session, market: str, period: str) -> dict:
    """What should have arrived for this market and period, versus what did."""
    lines = db.scalars(select(OrderLine).where(OrderLine.market == market)).all() if market \
        else db.scalars(select(OrderLine)).all()
    got = db.scalars(select(Report).where(Report.period == period)).all()
    if market:
        got = [r for r in got if (r.market or "") == market]

    got_ids = {i for r in got if not r.is_lifetime for i in _keyify(r.client, r.account_ids)}
    life_ids = {i for r in got if r.is_lifetime for i in _keyify(r.client, r.account_ids)}

    y, m = (int(x) for x in period.split("-"))
    period_end = (dt.date(y + (m == 12), (m % 12) + 1, 1) - dt.timedelta(days=1))
    period_start = dt.date(y, m, 1)

    missing, lifetime_due = [], []
    for ol in lines:
        ids = _keyify(ol.client, ol.account_ids)
        if ol.starts_on and ol.starts_on > period_end:
            continue
        if ol.ends_on and ol.ends_on < period_start:
            continue
        if not ids or not (ids & got_ids):
            missing.append({"client": ol.client, "accounts": ol.account_ids,
                            "market": ol.market, "buyer": ol.buyer, "team": ol.team_member})
        if ol.needs_lifetime and ol.ends_on and period_start <= ol.ends_on <= period_end:
            if not (ids & life_ids):
                lifetime_due.append({"client": ol.client, "accounts": ol.account_ids,
                                     "market": ol.market, "ended": ol.ends_on.isoformat(),
                                     "buyer": ol.buyer, "team": ol.team_member})
    return {"expected": len(lines), "received": len(got),
            "missing": missing, "lifetime_due": lifetime_due}
