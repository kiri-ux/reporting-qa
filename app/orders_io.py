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
from pathlib import Path

from dateutil import parser as dp
from sqlalchemy import select
from sqlalchemy.orm import Session

from .checks.products import map_order_product, map_order_products
from .db import OrderLine

SIGNATURE = {"client_business_unit", "orders_status", "product", "orders_end_date"}

RFP = re.compile(r"\bRFP\b", re.I)
DEAD_LINE_STATUS = re.compile(r"^(Cancelled)$", re.I)
HTML = re.compile(r"<[^>]+>")

# Order 55216 sat at "IO Pending Launch" with an IO Live line item under it, so
# the whole order was dropped, the product never joined the expected set, and
# the report was failed for carrying a product with no live order. The header
# was the only thing that was wrong.
#
# The line item can now rescue an order the header would have dropped. It
# deliberately cannot do the reverse: W&L Subaru's Meta lines sit at "IO
# Paused" under a live order, and a line paused halfway through the month still
# delivered for half of it and still owes a report. So this rule only ever adds
# line items, never removes one that used to be expected.
LIVE_STATUS = {"io live", "io complete"}

# Two things the order header IS authoritative about, because both are
# deliberate acts rather than a state nobody updated. An RFP was never sold,
# and cancelling an order cancels what is under it.
DEAD_ORDER_STATUS = re.compile(r"^(Cancelled)$", re.I)


def looks_like_io_export(headers: list[str]) -> bool:
    return SIGNATURE.issubset({(h or "").strip().lower() for h in headers})


def _txt(v) -> str:
    v = str(v or "")
    if "<" in v:                       # ids arrive wrapped in an anchor tag
        v = HTML.sub("", v)
    return v.strip()


ISO = re.compile(r"^(\d{4})-(\d{2})-(\d{2})")


def _date(v):
    """The export writes dates as 2024-02-07 22:00:00. Slicing that is far
    cheaper than dateutil, and this runs a few million times."""
    if not v:
        return None
    v = v.strip() if isinstance(v, str) else _txt(v)
    if not v:
        return None
    m = ISO.match(v)
    if m:
        try:
            return dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
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


# Only these columns are read. Pulling them by index with csv.reader is about
# three times faster than csv.DictReader building a 30-key dict per row, which
# matters at a couple of million rows.
WANTED = ("orders_id", "id", "orders_status", "status", "client", "product",
          "client_business_unit", "orders_start_date", "orders_end_date",
          "start_date", "start_date.1", "end_date", "end_date.1", "date",
          "campaign_manager")


def _open_source(src):
    """Yield (getter, row) pairs from a path or blob, streaming rather than
    loading. A single export can be 400 MB."""
    if isinstance(src, (str, Path)):
        fh = open(src, "r", encoding="utf-8-sig", errors="replace", newline="")
        close = True
    else:
        fh = io.StringIO(src.decode("utf-8-sig", errors="replace"))
        close = False
    try:
        reader = csv.reader(fh)
        header = next(reader, None)
        if not header:
            return
        idx = {name: i for i, name in enumerate(header) if name in WANTED}
        blank = ""
        for row in reader:
            n = len(row)
            yield {k: (row[i] if i < n else blank) for k, i in idx.items()}
    finally:
        if close:
            fh.close()


def import_io_export(db: Session, sources, period: str | None = None,
                     replace: bool = True) -> dict:
    """Load the export, keep only what should get a report, one row per
    client + product.

    Accepts a path, a blob, or a list of either. Several exports are merged and
    de-duplicated on order id plus line item id, so overlapping date ranges are
    harmless. Rows are streamed rather than collected, because a single export
    can be 400 MB and the service has 512 MB to work with.
    """
    period = period or previous_period()
    p_start, p_end = period_bounds(period)

    if not isinstance(sources, list):
        sources = [sources]

    skipped: dict[str, int] = {}

    def skip(reason):
        skipped[reason] = skipped.get(reason, 0) + 1

    seen: set[tuple] = set()
    kept: dict[tuple[str, str], dict] = {}
    # Line items kept on their own status while their order header disagreed.
    # Worth counting: a handful is housekeeping, a hundred is a process problem.
    header_overruled = 0
    dupes = 0
    rows_read = 0
    date_min = date_max = None        # kept as raw strings, compared lexically
    order_start_min = None

    for src in sources:
        for r in _open_source(src):
            rows_read += 1

            d = r.get("date") or ""
            if d:
                if date_min is None or d < date_min:
                    date_min = d
                if date_max is None or d > date_max:
                    date_max = d

            order_id = _txt(r.get("orders_id"))
            line_id = _txt(r.get("id"))
            key = (order_id, line_id)
            if key in seen:              # daily grain, and exports may overlap
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
            if DEAD_ORDER_STATUS.match(order_status):
                skip("order cancelled"); continue
            if order_status.lower() not in LIVE_STATUS:
                if line_status.lower() in LIVE_STATUS:
                    header_overruled += 1        # the line item rescued it
                else:
                    skip(f"order status {order_status or 'blank'}"
                         + (f", line item {line_status}" if line_status else ""))
                    continue

            # A LINE ITEM CAN SELL TWO PRODUCTS.
            #
            # "CTV + Video Ads" is one line on the order and two sections on
            # the report. Read as CTV alone, Bloomsburg Chevrolet's Video was
            # a product with no live order - on a report where the buy was
            # plainly both.
            products = map_order_products(product_raw)
            if not products:
                skip(f"unmapped product: {product_raw}"); continue

            os_ = _date(r.get("orders_start_date"))
            if os_ and (order_start_min is None or os_ < order_start_min):
                order_start_min = os_

            # A PAUSED LINE ITEM MAKES NO CLAIM EITHER WAY.
            #
            # W&L Subaru's only Meta line is IO Paused and ended on 30 June, and
            # the July report was failed for a missing Meta section. A paused
            # buy is not delivering, so it is not owed on the report - and if
            # its product does turn up, that is not a surprise either.
            line_live = line_status.lower() in LIVE_STATUS or (
                not line_status and order_status.lower() in LIVE_STATUS)
            for product in products:
                k = (client, product)
                if k not in kept:
                    kept[k] = {
                        "market": _txt(r.get("client_business_unit")),
                        "client": client, "product": product, "order_id": order_id,
                        "campaign": product_raw, "starts_on": start, "ends_on": end,
                        "manager": _txt(r.get("campaign_manager")),
                        "orders": set(), "lines": set(), "flights": [],
                        "live": False,
                    }
                else:                    # widest flight across that client's orders
                    cur = kept[k]
                    if start and (cur["starts_on"] is None or start < cur["starts_on"]):
                        cur["starts_on"] = start
                    if end and (cur["ends_on"] is None or end > cur["ends_on"]):
                        cur["ends_on"] = end
                # Live if ANY line item behind this row is. A client running one
                # product across a live order and a paused one is running it.
                kept[k]["live"] = kept[k]["live"] or line_live
                # Each order's own window as well as the merged one. The merged
                # span answers "when does this end"; only the individual windows
                # can answer "was it running in July", and a client who stopped
                # in June and restarts in August has a July the merged span
                # hides completely.
                kept[k]["flights"].append(
                    [start.isoformat() if start else None,
                     end.isoformat() if end else None])
                # Every order and line item that rolled into this row, so a
                # client running one product across three orders can be traced.
                if order_id:
                    kept[k]["orders"].add(order_id)
                if line_id:
                    kept[k]["lines"].add(line_id)

    if not rows_read:
        return {"kept": 0, "clients": 0, "skipped": {}, "guidance": {},
                "rows_read": 0, "duplicate_rows": 0, "files": len(sources)}

    # What the old list said, before it is thrown away. A report's product
    # check is an answer about the ORDERS as much as about the PDF, so a client
    # whose product set just changed is carrying a stale verdict - and nothing
    # else would ever notice, because the PDF has not changed and the checking
    # code has not changed either.
    before = _products_by_client(db)

    if replace:
        db.query(OrderLine).delete()

    # The export's campaign manager IS the buyer. The two had drifted apart in
    # the schema, which is why the order list showed an empty Buyer column
    # beside a populated Owner one. Blanks fall back to the reporting roster:
    # the partner's buyer, or its SEO person on an SEO line item.
    from .partners import find as find_partner, resolve_owner
    partner_cache: dict[str, object] = {}
    fallbacks = 0

    for (client, product), v in kept.items():
        manager, email = v["manager"], ""
        m = re.match(r"(.*?)\s*\(([^)]+)\)\s*$", manager)
        if m:
            manager, email = m.group(1).strip(), m.group(2).strip()

        market = v["market"]
        if market not in partner_cache:
            partner_cache[market] = find_partner(db, market)
        buyer, buyer_email = resolve_owner(partner_cache[market], product, manager, email)
        if buyer and not manager:
            fallbacks += 1

        db.add(OrderLine(
            market=market, client=client,
            account_ids=", ".join(sorted(v["orders"]))[:255] or v["order_id"],
            line_ids=", ".join(sorted(v["lines"]))[:512],
            campaign=v["campaign"], product=product,
            starts_on=v["starts_on"], ends_on=v["ends_on"],
            flights=v["flights"], live=bool(v["live"]),
            buyer=buyer, buyer_email=buyer_email,
            needs_lifetime=bool(v["ends_on"] and p_start <= v["ends_on"] <= p_end),
        ))
    db.commit()

    restamped = _restamp_changed_clients(db, before)

    guidance = _export_guidance(_date(date_min), _date(date_max), order_start_min)
    return {"kept": len(kept), "clients": len({c for c, _ in kept}),
            "period": period, "rows_read": rows_read, "duplicate_rows": dupes,
            "files": len(sources), "guidance": guidance, "roster_fallbacks": fallbacks,
            "header_overruled": header_overruled, "restamped": restamped,
            "skipped": dict(sorted(skipped.items(), key=lambda x: -x[1]))}


def _norm_client(name: str) -> str:
    """Loose enough that one client is one client.

    "Service One Credit Union (1)" is a browser's second download of the same
    report, not a second credit union - and a comparison that reads them as two
    quietly skips the report that needed re-reading.
    """
    s = re.sub(r"\s*\(\d+\)\s*$", "", (name or "").strip())
    return re.sub(r"[^a-z0-9]", "", s.lower())


def _products_by_client(db: Session) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for line in db.scalars(select(OrderLine)).all():
        out.setdefault(_norm_client(line.client), set()).add(line.product or "")
    return out


def _restamp_changed_clients(db: Session, before: dict[str, set[str]]) -> int:
    """Queue a re-check for every report whose client's products moved.

    Only the ones that moved. Clearing the stamp on all twelve hundred reports
    after every sync would re-read every PDF in the cycle daily, for an answer
    that is the same one it already had on all but a handful of them.
    """
    from .db import Report

    after = _products_by_client(db)
    changed = {c for c in set(before) | set(after)
               if before.get(c, set()) != after.get(c, set())}
    if not changed:
        return 0
    n = 0
    for rep in db.scalars(select(Report).where(Report.rules_version != "")).all():
        if _norm_client(rep.client) in changed:
            rep.rules_version = ""          # the sweep picks it up from here
            n += 1
    if n:
        db.commit()
    return n


def guidance_from_loaded(db: Session) -> dict:
    """The date range the next export needs, worked out from what is loaded.

    The import returns this too, but only on a successful run - so the moment
    a sync failed, the one panel telling you what range to pull disappeared,
    which is exactly when you most need it. This derives the same answer from
    the order lines already in the database, so the guidance is on screen
    whenever there is anything to be guided about.
    """
    from sqlalchemy import func, select as _select
    row = db.execute(_select(func.min(OrderLine.starts_on),
                             func.max(OrderLine.ends_on))).first()
    if not row or not row[0]:
        return {}
    earliest, latest = row[0], row[1]
    return {
        "covered_from": earliest.isoformat(),
        "covered_to": latest.isoformat() if latest else "",
        "earliest_order_start": earliest.isoformat(),
        "pull_from": earliest.isoformat(),
        "pull_to": dt.date.today().isoformat(),
        "may_be_truncated": False,
        "derived": True,          # from what is loaded, not from the last import
    }


def _export_guidance(covered_from: dt.date | None, covered_to: dt.date | None,
                     earliest_order: dt.date | None) -> dict:
    """Work out the date range the next TapClicks export needs.

    The export's date filter runs on the line item's start date, not on
    delivery. A campaign that launched in 2020 and is still running only
    appears in an export whose range reaches back to 2020. So the range has to
    start at the earliest start date you still care about, not 30 days ago.
    """
    if not covered_from:
        return {}
    earliest_order = earliest_order or covered_from

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
