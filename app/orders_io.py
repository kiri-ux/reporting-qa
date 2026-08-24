"""Import the IO tool's order export.

The export is at daily grain, so 57 line items arrive as 5,000 rows, and both
ids come wrapped in HTML anchors. It also carries two statuses: one on the order
and one on the line item. Both matter.

Eligibility, as specified:
  * only live IOs, or orders that were live at some point in the report period
  * no RFPs, at either level
  * nothing that ended before the period started
  * one report per client, so products roll up across that client's orders
"""
from __future__ import annotations

import csv
import datetime as dt
import io
import re

from dateutil import parser as dp
from sqlalchemy.orm import Session

from .checks.products import map_order_product
from .db import OrderLine

SIGNATURE = {"client_business_unit", "orders_status", "product", "orders_end_date"}

RFP = re.compile(r"\bRFP\b", re.I)
DEAD_LINE_STATUS = re.compile(r"^(Cancelled)$", re.I)
HTML = re.compile(r"<[^>]+>")


def looks_like_io_export(headers: list[str]) -> bool:
    return SIGNATURE.issubset({(h or "").strip().lower() for h in headers})


def _txt(v) -> str:
    return HTML.sub("", str(v or "")).strip()


def _date(v):
    v = _txt(v)
    if not v:
        return None
    try:
        return dp.parse(v).date()
    except Exception:
        return None


def period_bounds(period: str) -> tuple[dt.date, dt.date]:
    y, m = (int(x) for x in period.split("-"))
    start = dt.date(y, m, 1)
    end = dt.date(y + (m == 12), (m % 12) + 1, 1) - dt.timedelta(days=1)
    return start, end


def previous_period(today: dt.date | None = None) -> str:
    today = today or dt.date.today()
    first = today.replace(day=1)
    return (first - dt.timedelta(days=1)).strftime("%Y-%m")


def import_io_export(db: Session, raw: bytes | list[bytes], period: str | None = None,
                     replace: bool = True) -> dict:
    """Load the export, keep only what should get a report, one row per
    client + product.

    Accepts several exports at once. They are merged and de-duplicated on
    order id plus line item id, so overlapping date ranges are harmless."""
    period = period or previous_period()
    p_start, p_end = period_bounds(period)

    blobs = raw if isinstance(raw, list) else [raw]
    rows: list[dict] = []
    for blob in blobs:
        rows.extend(csv.DictReader(io.StringIO(blob.decode("utf-8-sig", errors="replace"))))
    if not rows:
        return {"kept": 0, "clients": 0, "skipped": {}, "guidance": {}}

    skipped: dict[str, int] = {}

    def skip(reason):
        skipped[reason] = skipped.get(reason, 0) + 1

    seen: set[tuple] = set()
    kept: dict[tuple[str, str], dict] = {}
    dupes = 0
    export_dates: list[dt.date] = []          # the `date` column the range filters on
    order_starts: list[dt.date] = []          # earliest possible line start per order

    for r in rows:
        order_id = _txt(r.get("orders_id"))
        line_id = _txt(r.get("id"))
        d = _date(r.get("date"))
        if d:
            export_dates.append(d)

        key = (order_id, line_id)
        if key in seen:                       # daily grain, and exports may overlap
            dupes += 1
            continue
        seen.add(key)

        order_status = _txt(r.get("orders_status"))
        line_status = _txt(r.get("status"))
        client = _txt(r.get("client"))
        product_raw = _txt(r.get("product"))

        if not client or not product_raw:
            skip("no client or product"); continue
        if RFP.search(order_status) or RFP.search(line_status):
            skip("RFP"); continue

        order_end = _date(r.get("orders_end_date"))
        line_end = _date(r.get("end_date")) or _date(r.get("end_date.1"))
        end = line_end or order_end
        start = (_date(r.get("start_date.1")) or _date(r.get("start_date"))
                 or _date(r.get("orders_start_date")))

        if end and end < p_start:
            skip("ended before the period"); continue
        if start and start > p_end:
            skip("starts after the period"); continue
        if DEAD_LINE_STATUS.match(line_status):
            skip("line item cancelled"); continue
        if order_status.lower() not in {"io live", "io complete"}:
            skip(f"order status {order_status or 'blank'}"); continue

        product = map_order_product(product_raw)
        if not product:
            skip(f"unmapped product: {product_raw}"); continue

        os_ = _date(r.get("orders_start_date"))
        if os_:
            order_starts.append(os_)

        k = (client, product)
        if k not in kept:
            kept[k] = {
                "market": _txt(r.get("client_business_unit")),
                "client": client, "product": product, "order_id": order_id,
                "campaign": product_raw, "starts_on": start, "ends_on": end,
                "manager": _txt(r.get("campaign_manager")),
            }
        else:                                  # widest flight across that client's orders
            cur = kept[k]
            if start and (cur["starts_on"] is None or start < cur["starts_on"]):
                cur["starts_on"] = start
            if end and (cur["ends_on"] is None or end > cur["ends_on"]):
                cur["ends_on"] = end

    if replace:
        db.query(OrderLine).delete()

    for (client, product), v in kept.items():
        manager, email = v["manager"], ""
        m = re.match(r"(.*?)\s*\(([^)]+)\)\s*$", manager)
        if m:
            manager, email = m.group(1).strip(), m.group(2).strip()
        db.add(OrderLine(
            market=v["market"], client=client, account_ids=v["order_id"],
            campaign=v["campaign"], product=product,
            starts_on=v["starts_on"], ends_on=v["ends_on"],
            team_member=manager, team_email=email,
            needs_lifetime=bool(v["ends_on"] and p_start <= v["ends_on"] <= p_end),
        ))
    db.commit()

    guidance = _export_guidance(export_dates, order_starts, p_start)
    return {"kept": len(kept), "clients": len({c for c, _ in kept}),
            "period": period, "rows_read": len(rows), "duplicate_rows": dupes,
            "files": len(blobs), "guidance": guidance,
            "skipped": dict(sorted(skipped.items(), key=lambda x: -x[1]))}


def _export_guidance(export_dates: list[dt.date], order_starts: list[dt.date],
                     period_start: dt.date) -> dict:
    """Work out the date range the next TapClicks export needs.

    The export's date filter runs on the line item's start date, not on
    delivery. A campaign that launched in 2020 and is still running only
    appears in an export whose range reaches back to 2020. So the range has to
    start at the earliest start date you still care about, not 30 days ago.
    """
    if not export_dates:
        return {}
    covered_from, covered_to = min(export_dates), max(export_dates)
    earliest_order = min(order_starts) if order_starts else covered_from

    # An order whose own start predates the export window may have line items
    # that started before the window and were therefore filtered out.
    at_risk = earliest_order < covered_from
    pull_from = min(earliest_order, covered_from)

    return {
        "covered_from": covered_from.isoformat(),
        "covered_to": covered_to.isoformat(),
        "earliest_order_start": earliest_order.isoformat(),
        "pull_from": pull_from.isoformat(),
        "pull_to": dt.date.today().isoformat(),
        "may_be_truncated": at_risk,
    }
