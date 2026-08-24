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


def _rows_from_csv(raw: bytes) -> list[list[str]]:
    text = raw.decode("utf-8-sig", errors="replace")
    return [[(c or "") for c in r] for r in csv.reader(io.StringIO(text))]


def _rows_from_xlsx(raw: bytes, sheet: str | None = None) -> list[list[str]]:
    from openpyxl import load_workbook
    wb = load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
    ws = wb[sheet] if sheet and sheet in wb.sheetnames else wb[wb.sheetnames[0]]
    out = []
    for row in ws.iter_rows(values_only=True):
        out.append(["" if v is None else (v.strftime("%Y-%m-%d")
                                          if hasattr(v, "strftime") else str(v))
                    for v in row])
    return out


def import_orders(db: Session, raw, filename: str = "orders.csv",
                  sheet: str | None = None, replace: bool = True,
                  period: str | None = None):
    """Returns the IO export's result dict when it recognises that format,
    otherwise a plain row count."""
    """Accepts CSV or XLSX, and recognises the IO tool's own export, which needs
    its own eligibility rules rather than being read as a flat list."""
    from pathlib import Path as _P
    blobs = raw if isinstance(raw, list) else [raw]
    first = blobs[0]
    if isinstance(first, (str, _P)):
        # streamed from S3: peek at the header only
        with open(first, "r", encoding="utf-8-sig", errors="replace", newline="") as fh:
            header = next(csv.reader(fh), [])
        from .orders_io import import_io_export, looks_like_io_export
        if looks_like_io_export(header):
            return import_io_export(db, blobs, period=period, replace=replace)
        rows = _rows_from_csv(open(first, "rb").read())
    elif filename.lower().endswith((".xlsx", ".xlsm")):
        rows = _rows_from_xlsx(first, sheet)
    else:
        rows = _rows_from_csv(first)
    if rows:
        from .orders_io import import_io_export, looks_like_io_export
        if looks_like_io_export(rows[0]):
            if filename.lower().endswith((".xlsx", ".xlsm")):
                import csv as _csv, io as _io
                conv = []
                for b in blobs:
                    buf = _io.StringIO()
                    _csv.writer(buf).writerows(_rows_from_xlsx(b, sheet))
                    conv.append(buf.getvalue().encode())
                blobs = conv
            return import_io_export(db, blobs, period=period, replace=replace)
    all_rows = rows if len(blobs) == 1 else [r for b in blobs for r in _rows_from_csv(b)]
    return _import_rows(db, all_rows, replace=replace)


def import_order_csv(db: Session, raw: bytes, replace: bool = True) -> int:
    return import_orders(db, raw, "orders.csv", replace=replace)


def _import_rows(db: Session, rows: list[list[str]], replace: bool = True) -> int:
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


def expected_products(db: Session, client: str, account_ids: str) -> set[str] | None:
    """Products the client's qualifying orders say belong on this report.
    Returns None when the client is not on the order list, so the check stays
    quiet rather than guessing."""
    lines = db.scalars(select(OrderLine)).all()
    if not lines:
        return None
    ids = _keyify(client, account_ids)
    hit = [l for l in lines if ids and (ids & _keyify(l.client, l.account_ids))]
    if not hit:
        norm = re.sub(r"[^a-z0-9]", "", (client or "").lower())
        hit = [l for l in lines if norm and re.sub(r"[^a-z0-9]", "", l.client.lower()) == norm]
    if not hit:
        return None
    return {l.product for l in hit if l.product}


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
