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
# and canceling an order cancels what is under it.
DEAD_ORDER_STATUS = re.compile(r"^(Cancelled)$", re.I)

# PAUSED IS NOT DROPPED. IT RAN AND THEN STOPPED.
#
# 53392 and 54937 both sit at "IO Paused" and neither was on the board, because
# a paused header is not a live one and the import threw the order away. But a
# campaign paused on the 20th delivered for nineteen days, and those nineteen
# days are owed a report.
#
# So a paused order is kept and lands on the board on the strength of its
# dates. If it turns out it did not run at all this month, that is a judgment
# nothing in the export can make - mark the row "no report needed" and it comes
# off, for that cycle only.
PAUSED_STATUS = re.compile(r"^(IO\s+)?Paused$", re.I)


# THE SAME EXPORT COMES IN TWO SPELLINGS.
#
# The nightly S3 file uses snake_case column names; a sheet pulled by hand out
# of the IO tool uses the display names - "Order's Status", "Client Business
# Unit". Same columns, same meaning, and a reader that only knows one of them
# rejects a perfectly good file with a confusing message about it not looking
# like an export.
HEADER_ALIASES = {
    "order id": "orders_id",
    "order's id": "orders_id",
    "id": "id",
    "order's status": "orders_status",
    "status": "status",
    "client": "client",
    "product": "product",
    "client business unit": "client_business_unit",
    "order's start date": "orders_start_date",
    "order's end date": "orders_end_date",
    "start date": "start_date",
    "end date": "end_date",
    "campaign manager": "campaign_manager",
    "order type": "orders_type",
    "order's type": "orders_type",
    "monthly campaign budget": "monthly_campaign_budget",
    "monthly budget": "monthly_campaign_budget",
    "monthly meta ad spend": "monthly_meta_ad_spend",
    "monthly ppc ad spend": "monthly_ppc_ad_spend",
    "monthly linkedin ad spend": "monthly_linkedin_ad_spend",
    "monthly linked ad spend": "monthly_linkedin_ad_spend",

    # THE MONEY, UNDER ITS NEW NAMES.
    #
    # The orders-db export dropped monthly_campaign_budget and total_campaign_
    # budget and carries the same two figures as budget_combined and
    # total_budget_combined, with client_monthly_budget and client_total_budget
    # holding the same values again. "Client" in those names is misleading:
    # they are PER LINE ITEM. Order 36184 has three, and they read 1500, 500
    # and 500 - which is the line item's own monthly budget, not the client's.
    #
    # All four map to the two canonical names, so the first-non-empty rule that
    # already handles repeated columns picks whichever the export filled in.
    "budget_combined": "monthly_campaign_budget",
    "client_monthly_budget": "monthly_campaign_budget",
    "total_budget_combined": "total_campaign_budget",
    "client_total_budget": "total_campaign_budget",
}


def normalize_header(h: str) -> str:
    """The snake_case name for a column, whichever spelling arrived."""
    raw = (h or "").strip()
    low = raw.lower()
    return HEADER_ALIASES.get(low, low.replace(" ", "_").replace("'", ""))


def looks_like_io_export(headers: list[str]) -> bool:
    got = {normalize_header(h) for h in headers}
    return SIGNATURE.issubset(got)


def _txt(v) -> str:
    v = str(v or "")
    if "<" in v:                       # ids arrive wrapped in an anchor tag
        v = HTML.sub("", v)
    return v.strip()


def _num(v):
    """A money or impression figure off the export, or None.

    None and 0 are different answers - "the export does not carry this column"
    against "the order says nothing is being spent" - and a pacing line that
    treats a missing column as a budget of nothing reads 100% under.
    """
    s = _txt(v).replace(",", "").replace("$", "").strip()
    if not s or s in {"-", "--", "n/a", "N/A"}:
        return None
    try:
        return float(s)
    except ValueError:
        return None


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
WANTED = ("orders_id", "id", "orders_status", "status", "orders_type",
          "client", "product",
          "client_business_unit", "orders_start_date", "orders_end_date",
          "start_date", "end_date", "date",
          "campaign_manager",
          # Money. Not on the report and not derivable from it - pacing is the
          # comparison of what the order says to spend against what it spent.
          "monthly_campaign_budget", "monthly_meta_ad_spend",
          "monthly_ppc_ad_spend", "monthly_linkedin_ad_spend",
          "monthly_pm_ad_spend", "monthly_campaign_impressions",
          # The whole campaign. A lifetime report covers all of it, so a
          # monthly figure says nothing about whether it delivered.
          "total_campaign_impressions", "total_campaign_budget",
          "total_meta_ad_spend", "total_ppc_ad_spend",
          "total_linkedin_ad_spend", "total_pm_ad_spend")


# WHICH COLUMN IS THIS PRODUCT'S MONTHLY MONEY.
#
# Most products are bought against the campaign budget - client ad cost. The
# four that are not each have their own column on the order, and comparing one
# of those against the campaign budget is comparing two different things.
SPEND_FIELD = {
    "Performance Max": "monthly_pm_ad_spend",
    "PPC": "monthly_ppc_ad_spend",
    "LinkedIn": "monthly_linkedin_ad_spend",
}

# The same four, for the whole campaign rather than the month.
TOTAL_SPEND_FIELD = {
    "Performance Max": "total_pm_ad_spend",
    "PPC": "total_ppc_ad_spend",
    "LinkedIn": "total_linkedin_ad_spend",
}


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
        # A REPEATED COLUMN IS READ FIRST-NON-EMPTY, NOT LAST.
        #
        # The export carries two start_date columns and two end_date columns,
        # and which of the pair holds the value is not the same for both: over
        # 5,034 rows of the real file the SECOND start_date is populated every
        # time and the FIRST end_date is populated every time. Their partners
        # are always blank.
        #
        # This used to take the last one for both. That is right for the start
        # and wrong for the end - so every line item's end date came back blank
        # and fell through to the order header's end date, which is the last
        # day of the whole order. A line item that finished in June 2025 on an
        # order running to the end of 2026 therefore looked live all the way
        # through, and its product was expected on reports for months after it
        # stopped. Blair Regional YMCA was reported four times for exactly
        # this: two Social Mirror line items, ended 6/30/25 and 6/30/26, both
        # stored as ending 2026-12-31.
        idx: dict[str, list[int]] = {}
        for i, name in enumerate(header):
            key = normalize_header(name)
            if key in WANTED:
                idx.setdefault(key, []).append(i)
        for row in reader:
            n = len(row)
            out = {}
            for k, cols in idx.items():
                v = ""
                for i in cols:
                    if i < n and row[i].strip():
                        v = row[i]
                        break
                out[k] = v
            yield out
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
    # THE CYCLE BEING WORKED, NOT "LAST MONTH".
    #
    # This defaulted to the calendar month before today, and on 31 August that
    # is July - so every line item starting 1 August was dropped on the way in
    # as "starts after the period". River Valley Builders Facebook, order
    # 55476, IO Live, 1 August to 31 December, came back on the board as a
    # client that delivered 31 days with no order behind it. So did 117 others,
    # which is what a whole month of new orders looks like when the window is
    # a month behind the board.
    if not period:
        from .config import settings
        from .cycle import current_period
        period = settings.default_period or current_period()
    p_start, p_end = period_bounds(period)

    # AND A MONTH OF HEADROOM PAST IT.
    #
    # The cycle rolls over on its own, and nothing re-reads the export when it
    # does - the ETag has not changed, so the file is never looked at again.
    # Loading next month's orders now means the rollover finds them already
    # here instead of leaving the same hole on the 1st. They are not owed a
    # report until their month comes round; the board decides that, not this.
    y2, m2 = (p_end.year + (p_end.month == 12), p_end.month % 12 + 1)
    horizon = period_bounds(f"{y2:04d}-{m2:02d}")[1]

    if not isinstance(sources, list):
        sources = [sources]

    skipped: dict[str, int] = {}
    # Product names the map has never seen. Kept rather than dropped, and named
    # on the page so they get added.
    unmapped: dict[str, int] = {}
    # Clients whose rows were read and thrown away, and why. Capped, because
    # this rides on the sync record.
    dropped: dict[str, str] = {}

    def note_drop(market, client, reason):
        """WHICH CLIENT WAS DROPPED, not just how many rows.

        The counts said "5,796 RFP" and nothing about whose. So "I can see this
        order in the export and it is not on the board" could only be answered
        by somebody downloading the export and running the importer over it by
        hand - which is exactly what it took, twice.
        """
        if len(dropped) >= 6000:
            return
        k = f"{(market or '').strip()}|{(client or '').strip()}"
        if k != "|":
            dropped.setdefault(k, reason)

    def skip(reason):
        skipped[reason] = skipped.get(reason, 0) + 1

    seen: set[str] = set()
    kept: dict[tuple[str, str], dict] = {}
    # Line items kept on their own status while their order header disagreed.
    # Worth counting: a handful is housekeeping, a hundred is a process problem.
    header_overruled = 0
    dupes = 0
    rows_read = 0
    date_min = date_max = None        # kept as raw strings, compared lexically
    order_start_min = None
    # EVERY DISTINCT ORDER END DATE IN THE FILE, and every distinct order id,
    # up to a handful of each. See the sweep after the read loop.
    seen_order_ends: set = set()
    seen_order_ids: set = set()
    # The earliest line-item start on each order, across every row read.
    order_first_start: dict = {}

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
            # A LINE ITEM CAN HAVE MORE THAN ONE FLIGHT, and the export carries
            # a row per flight. Deduping on the line item alone kept whichever
            # flight came first and dropped the rest - so River Valley Builders'
            # Performance Max, re-flighted to run 1 Aug to 31 Dec, was still
            # stored as ending 31 July, which is inside the lifetime window. It
            # was owed a lifetime report it does not owe.
            #
            # The daily grain still has to collapse, so the key is the flight:
            # same order, same line item, same dates.
            # ONE STRING, NOT A TUPLE OF FOUR. This set holds a member per
            # line-item flight across a couple of million rows, and a 4-tuple
            # is five objects where one will do. It is the largest thing this
            # import keeps in memory, and the service was being restarted for
            # exceeding its memory limit.
            order_status = _txt(r.get("orders_status"))
            line_status = _txt(r.get("status"))
            # AND THE STATUS IS PART OF WHAT MAKES A ROW DIFFERENT.
            #
            # Two exports of the same order, pulled a week apart, carry the
            # same order id, the same line id and the same flight - and not
            # the same status. Keyed without it, the FIRST file read wins and
            # the second is thrown away as a duplicate, so an order that was
            # "RFP Pending" on Tuesday and has been IO Live since is dropped
            # by the RFP filter and never seen again. The older file quietly
            # beats the newer one, and the page says "Overlapping exports are
            # fine".
            #
            # With the status in the key both rows are read, and the merge
            # already knows what to do with them: live wins over not-live, and
            # every status the line has carried is kept in its own words.
            key = (order_id + "\x00" + line_id + "\x00"
                   + str(r.get("start_date") or "")[:10] + "\x00"
                   + str(r.get("end_date") or "")[:10] + "\x00"
                   + order_status + "\x00" + line_status)
            if key in seen:              # daily grain, and exports may overlap
                dupes += 1
                continue
            seen.add(key)

            client = _txt(r.get("client"))
            market = _txt(r.get("client_business_unit"))
            product_raw = _txt(r.get("product"))

            if not client or not product_raw:
                skip("no client or product"); continue
            # AN RFP IS A PROPOSAL, NOT A BUY.
            #
            # It was only ever read off the two STATUS columns, and order 51217
            # sits at Order Type "Request for Proposal" with Order Status
            # "Cancelled" - so nothing said RFP anywhere the import was looking,
            # and a proposal that was never sold went on the board owed a
            # lifetime report. The TYPE column is where that fact actually
            # lives.
            order_type = _txt(r.get("orders_type"))
            if (RFP.search(order_status) or RFP.search(line_status)
                    or RFP.search(order_type)
                    or "request for proposal" in order_type.lower()):
                note_drop(market, client, "the export has it as an RFP, not a live order")
                skip("RFP"); continue

            order_end = _date(r.get("orders_end_date"))
            if order_end is not None and len(seen_order_ends) < 4:
                seen_order_ends.add(order_end)
            if order_id and len(seen_order_ids) < 4:
                seen_order_ids.add(order_id)

            # HOW FAR BACK THIS ORDER ACTUALLY GOES, taken from its line items
            # rather than from its header, and taken HERE - before the skips -
            # because the line items that reach furthest back are exactly the
            # ones that finished years ago and are about to be dropped.
            #
            # That is what the header was being used for, and the header cannot
            # be trusted: see the sweep after this loop. This is the same fact
            # from the rows themselves.
            _ls = _date(r.get("start_date"))
            if order_id and _ls is not None:
                cur = order_first_start.get(order_id)
                if cur is None or _ls < cur:
                    order_first_start[order_id] = _ls
            # The line item's own end, and only if it has one. Falling back to
            # the order header keeps a finished line item alive for the rest of
            # the order - which is how a Social Mirror that stopped in June was
            # still being expected on a July report a year later.
            line_end = _date(r.get("end_date"))
            end = line_end or order_end
            start = _date(r.get("start_date")) or _date(r.get("orders_start_date"))

            # A CLOSED ORDER STOPS EVERY LINE ITEM UNDER IT.
            #
            # Order 48135 is IO Complete and ended on 28 February. Its Social
            # Mirror line item 127806 is still dated 1 April to 31 December, so
            # the line item's own end kept it alive and it sat on Long Jewelers'
            # JULY report as a running buy, five months after somebody closed
            # the order. A line item cannot deliver past the order that pays
            # for it, and the header is the newer fact of the two: closing an
            # order is a deliberate act, while a line item's end date is
            # whatever it was when it was written.
            #
            # Only when the order is CLOSED. A live order with a stale header
            # date is the opposite case - there the line item is the newer fact
            # - which is why this is not a plain min() of the two.
            if (order_end and (line_end is None or order_end < line_end)
                    and (DEAD_ORDER_STATUS.match(order_status)
                         or order_status.lower() == "io complete")):
                end = order_end

            # A CANCELED LINE IS KEPT, NOT DROPPED.
            #
            # It used to be thrown away at import, so a report carrying the
            # product read as carrying a product nobody ordered - Roto Rooter's
            # PPC was canceled on 28 July and its July report was failed for
            # showing it. A canceled buy is not OWED on the report; it is not a
            # surprise there either. It ran, it was stopped, and the data is
            # real. Kept with live=False so every rule that asks "is this
            # delivering" gets the right answer, and canceled=True so the ones
            # that ask "was this ever owed" get theirs.
            canceled = bool(DEAD_LINE_STATUS.match(line_status)
                            or DEAD_ORDER_STATUS.match(order_status))
            if end and end < p_start:
                note_drop(market, client,
                          f"every line item ended before {period} started")
                skip("ended before the period"); continue
            if start and start > horizon:
                note_drop(market, client,
                          f"it starts after {period} and the month after it")
                skip("starts after the period"); continue
            paused = bool(PAUSED_STATUS.match(line_status)
                          or (not line_status
                              and PAUSED_STATUS.match(order_status)))
            if not canceled and not paused and order_status.lower() not in LIVE_STATUS:
                if line_status.lower() in LIVE_STATUS:
                    header_overruled += 1        # the line item rescued it
                else:
                    note_drop(market, client,
                              f"the order status is {order_status or 'blank'}"
                              + (f" and the line item {line_status}"
                                 if line_status else "")
                              + " - not a live order")
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
                # AN UNKNOWN PRODUCT NAME MUST NOT DELETE THE CLIENT.
                #
                # This used to throw the line away, and a client whose ONLY
                # product was one the map had not heard of disappeared from the
                # board completely - no row, no report expected, nothing saying
                # why. Credit Union Audit Group sells LinkedIn and nothing
                # else, delivers 31 days a month, and was simply not there.
                #
                # The line is kept under the name the export gave it. The
                # client is on the board, its dates and money are real, and the
                # product checks leave an unmapped product alone rather than
                # failing a report for not carrying something nobody can name.
                # It is counted and shown on the Order list page, which is what
                # gets it into the map.
                unmapped[product_raw.strip()] = unmapped.get(product_raw.strip(), 0) + 1
                products = [product_raw.strip()[:60] or "unmapped"]

            os_ = _date(r.get("orders_start_date"))
            if os_ and (order_start_min is None or os_ < order_start_min):
                order_start_min = os_

            # A PAUSED LINE ITEM MAKES NO CLAIM EITHER WAY.
            #
            # W&L Subaru's only Meta line is IO Paused and ended on 30 June, and
            # the July report was failed for a missing Meta section. A paused
            # buy is not delivering, so it is not owed on the report - and if
            # its product does turn up, that is not a surprise either.
            # EVERY LINE COMPLETE MEANS THE CAMPAIGN IS OVER. "IO Complete"
            # is a deliberate act like canceling, and it is the only thing on
            # the export that says a campaign finished early - order 45911's
            # four line items are all complete while two of them are dated to
            # the end of 2026.
            line_done = (line_status.lower() == "io complete"
                         or (not line_status
                             and order_status.lower() == "io complete"))
            line_live = (not canceled) and (
                line_status.lower() in LIVE_STATUS
                or (not line_status and order_status.lower() in LIVE_STATUS))
            for product in products:
                k = (client, product)
                if k not in kept:
                    kept[k] = {
                        "market": _txt(r.get("client_business_unit")),
                        "client": client, "product": product, "order_id": order_id,
                        "campaign": product_raw, "starts_on": start, "ends_on": end,
                        "manager": _txt(r.get("campaign_manager")),
                        "orders": set(), "lines": set(), "flights": [],
                        "detail": [],
                        "live": False, "canceled": True, "complete": True,
                        "paused": True, "status": set(),
                        "budget": None, "impressions": None,
                        # The ORDER's own campaign window, kept apart from the
                        # line item's. A lifetime report covers the order; the
                        # line item only says what was delivering this month.
                        "order_starts": None, "order_ends": None,
                        "total_budget": None, "total_impressions": None,
                        # Which products came off the SAME line item. "CTV +
                        # Video Ads" is one buy with one goal, and pacing each
                        # half against the whole goal says both are miles out.
                        "sold_with": set(products),
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
                # Canceled only while EVERY line behind this row is. One live
                # line and one canceled one is a product the client is running.
                kept[k]["canceled"] = kept[k]["canceled"] and canceled
                # Paused only while EVERY line behind this row is. One live
                # line and one paused one is a product still delivering.
                kept[k]["paused"] = kept[k]["paused"] and paused
                # THE STATUS IN ITS OWN WORDS, for every line behind this row.
                # One row can cover several line items and they do not have to
                # agree - "IO Live, IO Pending Launch" is the answer to why a
                # campaign that looks finished is not.
                kept[k]["status"].add(line_status or order_status or "")
                # Complete only while EVERY line behind this row is. One live
                # line means the campaign has not finished.
                kept[k]["complete"] = kept[k]["complete"] and line_done
                if len(products) > 1:
                    kept[k]["sold_with"].update(products)

                # The widest campaign window across the orders behind this row.
                cur = kept[k]
                if os_ and (cur["order_starts"] is None or os_ < cur["order_starts"]):
                    cur["order_starts"] = os_
                if order_end and (cur["order_ends"] is None
                                  or order_end > cur["order_ends"]):
                    cur["order_ends"] = order_end

                # ONE LINE ITEM'S MONEY COUNTS ONCE, against the first product
                # it maps to. "CTV + Video Ads" is one buy with one budget, and
                # adding it to both halves would say the client is spending
                # twice what the order says.
                money = imps = whole = all_imps = None
                if product == products[0]:
                    # A product with its own money column falls back to the
                    # campaign budget when that column is empty: Performance
                    # Max showed "no comparison" against a report that plainly
                    # printed its spend, because monthly_pm_ad_spend was blank
                    # on an order that carries a monthly campaign budget.
                    money = _num(r.get(SPEND_FIELD.get(product, "")))
                    if money is None:
                        money = _num(r.get("monthly_campaign_budget"))
                    if money is not None:
                        cur = kept[k]["budget"]
                        kept[k]["budget"] = money if cur is None else cur + money
                    imps = _num(r.get("monthly_campaign_impressions"))
                    if imps is not None:
                        cur = kept[k]["impressions"]
                        kept[k]["impressions"] = imps if cur is None else cur + imps
                    whole = _num(r.get(TOTAL_SPEND_FIELD.get(product, "")))
                    if whole is None:
                        whole = _num(r.get("total_campaign_budget"))
                    if whole is not None:
                        cur = kept[k]["total_budget"]
                        kept[k]["total_budget"] = whole if cur is None else cur + whole
                    # NOT EVERY COLUMN CALLED total_campaign_impressions HOLDS
                    # IMPRESSIONS. This export repeats the header four times
                    # and the populated one carries 0.999999999999 - a share of
                    # goal, not a count - which turned a lifetime's pacing into
                    # "523,636 / 1, 52,363,500% over". Anything under a
                    # thousand is not a campaign's impression total.
                    all_imps = _num(r.get("total_campaign_impressions"))
                    if all_imps is not None and all_imps < 1000:
                        all_imps = None
                    if all_imps is not None:
                        cur = kept[k]["total_impressions"]
                        kept[k]["total_impressions"] = (
                            all_imps if cur is None else cur + all_imps)
                # Each order's own window as well as the merged one. The merged
                # span answers "when does this end"; only the individual windows
                # can answer "was it running in July", and a client who stopped
                # in June and restarts in August has a July the merged span
                # hides completely.
                kept[k]["flights"].append(
                    [start.isoformat() if start else None,
                     end.isoformat() if end else None])
                # AND THE LINE ITEM ITSELF, tied to its own order and its own
                # dates. The lists above answer "when did this product run";
                # only this can answer "which order was that".
                kept[k]["detail"].append({
                    "order": order_id, "line": line_id,
                    "raw": product_raw,
                    "starts": start.isoformat() if start else None,
                    "ends": end.isoformat() if end else None,
                    "order_starts": os_.isoformat() if os_ else None,
                    "order_ends": order_end.isoformat() if order_end else None,
                    "status": line_status or order_status or "",
                    "order_status": order_status or "",
                    "live": bool(line_live), "canceled": bool(canceled),
                    "complete": bool(line_done), "paused": bool(paused),
                    "budget": money, "impressions": imps,
                    "total_budget": whole, "total_impressions": all_imps,
                })
                # Every order and line item that rolled into this row, so a
                # client running one product across three orders can be traced.
                if order_id:
                    kept[k]["orders"].add(order_id)
                if line_id:
                    kept[k]["lines"].add(line_id)

    if not rows_read:
        return {"kept": 0, "clients": 0, "skipped": {}, "guidance": {},
                "rows_read": 0, "duplicate_rows": 0, "files": len(sources)}

    # AN "ORDER END DATE" THAT IS THE SAME ON EVERY ORDER IS NOT A DATE.
    #
    # In the orders-db export orders_end_date reads 2026-12-31 on every row of
    # every file - 1,404 rows across four partners, five orders, one value. It
    # is the end of the range the export was pulled over, not the end of
    # anybody's campaign.
    #
    # Believed, it says every campaign in the business finishes on the same day
    # at the end of next year: no campaign ever ends in the month being
    # reported, so no lifetime is ever owed, and every row on the board reads
    # as running to 2026-12-31. Manning Media's pull date came out of the same
    # pair of columns and matched nothing on the order in the IO tool.
    #
    # Detected rather than hard-coded, because the day it starts carrying real
    # dates this should start using them: one distinct value across several
    # orders is a window bound; several values are dates.
    window_end = (len(seen_order_ends) == 1 and len(seen_order_ids) > 1)
    if window_end:
        for v in kept.values():
            v["order_ends"] = None
            # AND ITS PARTNER COLUMN GOES WITH IT. They are the same pair from
            # the same place, so one of them being a window bound is not a
            # reason to believe the other. Manning Media's order 55987 came
            # through headed 2018-03-21 to 2018-05-18 against a line item that
            # ran 29 June to 31 July 2026, and there is no 2018 anywhere on
            # that order in the IO tool - not in its dates and not in its edit
            # history.
            #
            # Replaced with the earliest start among the order's OWN line
            # items, which is the fact the header was standing in for.
            firsts = [order_first_start[o] for o in (v["orders"] or ())
                      if o in order_first_start]
            v["order_starts"] = min(firsts) if firsts else v["starts_on"]

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
            flights=v["flights"], detail=v["detail"], live=bool(v["live"]),
            canceled=bool(v.get("canceled")),
            paused=bool(v.get("paused")),
            status=", ".join(sorted(x for x in v.get("status") or () if x))[:48],
            complete=bool(v.get("complete")),
            budget=v["budget"], impressions=v["impressions"],
            order_starts_on=v["order_starts"], order_ends_on=v["order_ends"],
            total_budget=v["total_budget"],
            total_impressions=v["total_impressions"],
            sold_with=", ".join(sorted(v["sold_with"]))[:255],
            buyer=buyer, buyer_email=buyer_email,
            needs_lifetime=bool(v["ends_on"] and p_start <= v["ends_on"] <= p_end),
        ))
    db.commit()

    restamped = _restamp_changed_clients(db, before)

    guidance = _export_guidance(_date(date_min), _date(date_max), order_start_min)
    return {"kept": len(kept), "clients": len({c for c, _ in kept}),
            "order_end_is_a_window": window_end,
            "period": period, "rows_read": rows_read, "duplicate_rows": dupes,
            "dropped": dropped,
            "unmapped_products": dict(sorted(unmapped.items(),
                                             key=lambda kv: -kv[1])[:20]),
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
