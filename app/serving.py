"""What actually served, by client, by day.

EVERY RULE ABOUT "DID THIS RUN" HAS BEEN AN INFERENCE UNTIL NOW.

The order export gives a flight and a status, and neither answers the question.
A line item sold 1 January to 31 December that was paused on the 2nd looks
identical to one paused on the 30th. "IO Complete" is where every campaign that
ever finished comes to rest, so it says nothing about when. Cancelled says the
buy stopped and not which month. So the board has been reading dates and
guessing, and the guesses have been wrong in both directions - reports asked
for on campaigns that did not run, and campaigns that ran with no row at all.

Delivery data settles it. If a client served on 19 days in July, they are owed
a July report. If they served on none, they are not, and the board can say so
in those words instead of inventing a reason.

ONE ROW PER CLIENT PER BUSINESS UNIT PER DAY is the grain. Which columns carry
those three is read off the header rather than fixed, because this file is
going to arrive from a different tool than the order export and nobody should
have to rename a column to make it load. What it matched is reported back, so
a file that loads wrong says so on the sync page instead of quietly counting
the wrong column.
"""
from __future__ import annotations

import datetime as dt
import re

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .db import ServedDays


def _norm(h: str) -> str:
    """Header text down to letters and digits: "Client's Name" -> clientsname."""
    return re.sub(r"[^a-z0-9]", "", (h or "").lower())


# WHAT EACH COLUMN MIGHT BE CALLED. Ordered - the first alias that matches wins,
# so the specific spellings sit ahead of the loose ones.
COLUMN_ALIASES: dict[str, tuple[str, ...]] = {
    "client": ("client", "clientname", "advertiser", "advertisername",
               "account", "accountname", "customer", "campaignclient"),
    "market": ("clientbusinessunit", "businessunit", "bu", "market",
               "partner", "station", "office", "clientbu"),
    "day": ("date", "day", "servedate", "servingdate", "deliverydate",
            "reportdate", "activitydate", "dt"),
    # Optional. A row that exists but served nothing is not a day of delivery,
    # and some exports write a row per calendar day regardless.
    "impressions": ("impressions", "imps", "impressionsdelivered", "delivered",
                    "servedimpressions", "totalimpressions"),
    "spend": ("spend", "cost", "adspend", "amountspent", "clientadcost"),
    "clicks": ("clicks", "totalclicks"),
    # Optional. With it the answer can be per product; without it, per client.
    "product": ("product", "productname", "linetype", "channel"),
    "order_id": ("ordersid", "orderid", "order", "iod", "ioid"),
}

# A file is a serving file when it names a client, a business unit and a date.
# The order export has a date column too, which is why the business unit alone
# is not enough to tell them apart - but it has no plain "date" column and this
# has no "orders_status", so the pair separates them.
REQUIRED = ("client", "market", "day")


def map_columns(headers: list[str]) -> dict[str, int]:
    out: dict[str, int] = {}
    seen = [_norm(h) for h in headers]
    for field, names in COLUMN_ALIASES.items():
        for alias in names:
            if alias in seen:
                out[field] = seen.index(alias)
                break
    return out


def looks_like_serving(headers: list[str]) -> bool:
    """Client, business unit and a date, and nothing that makes it the order
    export - which carries its own statuses and would otherwise qualify."""
    cols = map_columns(headers)
    if not all(f in cols for f in REQUIRED):
        return False
    seen = {_norm(h) for h in headers}
    return not ({"ordersstatus", "ordersenddate"} & seen)


ISO = re.compile(r"^(\d{4})-(\d{2})-(\d{2})")


def _date(v):
    if not v:
        return None
    s = v.strip() if isinstance(v, str) else str(v).strip()
    if not s:
        return None
    m = ISO.match(s)
    if m:
        try:
            return dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    try:
        from dateutil import parser as dp
        return dp.parse(s).date()
    except Exception:
        return None


def _num(v) -> float:
    s = str(v or "").replace(",", "").replace("$", "").strip()
    if not s:
        return 0.0
    try:
        return float(s)
    except ValueError:
        return 0.0


def _key(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


# THE ORDER TOOL PUTS THE PRODUCT IN THE CLIENT NAME. The serving file says
# "A-1 Appliance"; the IO tool has that same client as "A-1 Appliance -
# Display", because one client running two products is two client records
# there. Keyed strictly they are two different clients, and A-1 came back as a
# client that served for thirty days with no order behind it.
PRODUCT_SUFFIX = re.compile(
    r"\s[-\u2013]\s(?:display|video|ctv|connected tv|social mirror(?: ctv)?|"
    r"meta|ppc|pay-per-click|performance max|pmax|native(?: display)?|"
    r"online audio|audio|youtube|tiktok|linkedin|dooh|seo|live chat|"
    r"mobile conquesting|amazon)\s*$", re.I)


def _base_key(s: str) -> str:
    """The key with a trailing product name taken off, when there is one."""
    return _key(PRODUCT_SUFFIX.sub("", s or ""))


MONTHS = ("january", "february", "march", "april", "may", "june", "july",
          "august", "september", "october", "november", "december")


def normalize_period(period: str | None) -> str | None:
    """"July 2026", "2026-7", "7/2026" all mean 2026-07.

    THE BOX SAYS "e.g. 2026-07" AND IS A TEXT FIELD, so it gets typed in
    however the person thinks about months. "July 2026" matched none of a file
    full of July and reported "0 clients across no month, 16,574 rows read" -
    which reads like the file is broken when the file is fine.
    """
    s = (period or "").strip()
    if not s:
        return None
    m = re.match(r"^(\d{4})[-/](\d{1,2})$", s)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}"
    m = re.match(r"^(\d{1,2})[-/](\d{4})$", s)
    if m:
        return f"{m.group(2)}-{int(m.group(1)):02d}"
    low = s.lower()
    year = re.search(r"\b(20\d{2})\b", low)
    for i, name in enumerate(MONTHS, start=1):
        if low.startswith(name[:3]) and year:
            return f"{year.group(1)}-{i:02d}"
    return s


def import_serving(db: Session, rows, *, period: str | None = None,
                   replace: bool = True) -> dict:
    """Count the days each client delivered on, per period.

    A DAY COUNTS WHEN SOMETHING WAS DELIVERED ON IT, not when a row exists for
    it. Several of these exports write a row for every calendar day of the
    flight and put zeros in it, and counting those back is the same guess the
    dates were already making.
    """
    rows = list(rows)
    if not rows:
        raise ValueError("The serving file is empty.")
    head = 0
    for i, r in enumerate(rows[:10]):
        if all(f in map_columns(r) for f in REQUIRED):
            head = i
            break
    cols = map_columns(rows[head])
    missing = [f for f in REQUIRED if f not in cols]
    if missing:
        raise ValueError(
            "The serving file needs a client, a business unit and a date "
            "column. Could not find: " + ", ".join(missing) + ". Header reads: "
            + ", ".join(str(h) for h in rows[head][:12]))

    # Whether the file carries a figure at all. With none, every row present
    # counts as a day - which is the best the file can support and is said out
    # loud rather than assumed.
    money = [f for f in ("impressions", "spend", "clicks") if f in cols]

    period = normalize_period(period)
    days: dict[tuple[str, str, str], set] = {}
    names: dict[tuple[str, str, str], tuple[str, str]] = {}
    found_months: set[str] = set()
    read = 0
    for r in rows[head + 1:]:
        if not any(str(c).strip() for c in r):
            continue

        def g(field):
            i = cols.get(field)
            return str(r[i]).strip() if i is not None and i < len(r) else ""

        client, market = g("client"), g("market")
        when = _date(g("day"))
        if not client or not when:
            continue
        read += 1
        if money and not any(_num(g(f)) > 0 for f in money):
            continue                       # a row with nothing in it is not a day
        p = when.strftime("%Y-%m")
        found_months.add(p)
        if period and p != period:
            continue
        k = (p, _key(market), _key(client))
        days.setdefault(k, set()).add(when)
        names.setdefault(k, (market, client))

    # A PERIOD THAT MATCHES NOTHING IS A TYPO, NOT AN EMPTY FILE.
    #
    # "2026-7" against a file of July dates dropped every row and reported "0
    # clients across no month, 16,574 rows read" - which reads like the file is
    # the problem when the file is fine. Normalizing the box handles the near
    # misses; this handles the rest by naming what the file actually holds.
    if period and not days and found_months:
        raise ValueError(
            f"Nothing in this file is dated {period}. It holds "
            + ", ".join(sorted(found_months))
            + ". Leave the period blank to load whatever is in the file.")
    if not days:
        if not read:
            raise ValueError("No rows with both a client and a date in them.")
        raise ValueError(
            f"Read {read:,} rows and none of them counted as a day of "
            f"delivery. Every one has a zero in "
            + " and ".join(money) + "." if money else
            f"Read {read:,} rows and none of them counted.")

    if replace:
        # DELETED AND *FLUSHED* BEFORE ANYTHING IS INSERTED.
        #
        # Without the flush both the deletes and the inserts go to the database
        # in one batch, and it does the inserts first - so re-loading a month
        # that is already there died on "duplicate key value violates unique
        # constraint uq_served_days". Which is the normal thing to do: the file
        # covers the last 180 days and gets re-uploaded whenever it is refreshed.
        periods = {k[0] for k in days} or ({period} if period else set())
        if periods:
            db.query(ServedDays).filter(
                ServedDays.period.in_(sorted(periods))).delete(
                    synchronize_session=False)
            db.flush()

    for k, dates in days.items():
        p, mk, ck = k
        market, client = names[k]
        db.add(ServedDays(period=p, market_key=mk, client_key=ck,
                          market=market[:255], client=client[:255],
                          days=len(dates), first_day=min(dates),
                          last_day=max(dates)))
    db.commit()
    return {"rows_read": read, "clients": len(days),
            "periods": sorted({k[0] for k in days}),
            "counted_on": ", ".join(money) or "a row per day, no figures in the file",
            "columns": {f: str(rows[head][i]) for f, i in sorted(cols.items())}}


def served_days(db: Session, period: str) -> dict[tuple[str, str], int]:
    """{(market key, client key): days delivered}, empty when nothing is loaded.

    EMPTY IS NOT ZERO. A period with no serving file loaded has to fall back to
    reading dates - answering "nobody ran in July" from a file that was never
    uploaded would take the whole board down.
    """
    return {(r.market_key, r.client_key): r.days for r in db.scalars(
        select(ServedDays).where(ServedDays.period == period)).all()}


def unmatched_count(db: Session, period: str) -> int:
    """How many, not a sample of them. The sample says what the problem looks
    like; the number says how big it is, and 40+ hid the difference between a
    handful of dark campaigns and the board losing three hundred rows."""
    from .db import OrderLine

    known = {(r.market_key, r.client_key) for r in db.scalars(
        select(ServedDays).where(ServedDays.period == period)).all()}
    if not known:
        return 0
    return sum(1 for market, client in db.execute(
        select(OrderLine.market, OrderLine.client).distinct()).all()
        if (_key(market), _key(client)) not in known)


def unmatched(db: Session, period: str, limit: int = 40) -> list[str]:
    """Order-list clients the serving file does not mention.

    A CLIENT THE FILE DOES NOT NAME IS TREATED AS HAVING SERVED NOTHING, which
    is the point of loading it - but it is also exactly what a client the two
    tools spell differently looks like. So the number is put on the page. A
    handful is campaigns that went dark; two hundred is a matching problem, and
    the difference should not need anybody to go looking for it.
    """
    from .db import OrderLine

    known = {(r.market_key, r.client_key) for r in db.scalars(
        select(ServedDays).where(ServedDays.period == period)).all()}
    if not known:
        return []
    out = set()
    for market, client in db.execute(
            select(OrderLine.market, OrderLine.client).distinct()).all():
        if (_key(market), _key(client)) not in known:
            out.add(f"{market} - {client}" if market else client)
    return sorted(out)[:limit]


def served_but_no_order(db: Session, period: str,
                        limit: int = 200) -> list[tuple[str, str, int]]:
    """Clients that DELIVERED in the month but have no order line loaded.

    The other direction from unmatched(), and the sharper of the two. A client
    the order list does not name can be a campaign that went dark or a spelling
    the two tools disagree on. A client that served impressions and has no
    order at all cannot be either of those: something was running and the tool
    has nothing to judge it against.

    Which makes it the check for "did every partner's export land". The orders
    arrive as one file per partner now, and a file that did not land looks
    exactly like a partner with nothing running - unless the serving file says
    that partner delivered. Then it is missing, and this says so by name.

    Only clients with real delivery behind them: a single day is as likely to
    be a stray row as a campaign.
    """
    from .db import OrderLine

    rows = db.scalars(select(ServedDays).where(
        ServedDays.period == period)).all()
    if not rows:
        return []
    pairs = db.execute(select(OrderLine.market, OrderLine.client).distinct()).all()
    have = set()
    partners = set()
    for m, c in pairs:
        have.add((_key(m), _key(c)))
        have.add((_key(m), _base_key(c)))     # "A-1 Appliance - Display"
        partners.add(_key(m))
    out = []
    for r in rows:
        if (r.market_key, r.client_key) in have:
            continue
        if (r.days or 0) < 2:
            continue
        # WHICH OF THE TWO THINGS IT IS. A partner with no orders at all is a
        # file that did not land; a partner with orders but not this client is
        # one client missing from a file that did - or a name the two tools
        # spell differently, which is worth knowing before anybody goes looking
        # for a file that is already there.
        why = ("no orders loaded for this partner at all"
               if r.market_key not in partners
               else "this partner has orders, but not this client")
        out.append((r.market, r.client, r.days or 0, why))
    out.sort(key=lambda x: (-x[2], x[0] or "", x[1] or ""))
    return out[:limit]


def matched_on_base_name(db: Session, period: str,
                         limit: int = 200) -> list[tuple[str, str, str]]:
    """Clients the two tools spell differently: (partner, serving name, order name).

    Only where the difference is a trailing product - "A-1 Appliance" against
    "A-1 Appliance - Display". Those match, and the report checks were never
    affected because a report is matched on the ORDER ID off its filename and
    only falls back to the name. But it is worth being able to see the list:
    if the IO tool has BOTH names as separate client records, that is a real
    split rather than a spelling, and it is the kind of thing that only ever
    gets noticed by looking.
    """
    from .db import OrderLine

    rows = db.scalars(select(ServedDays).where(
        ServedDays.period == period)).all()
    if not rows:
        return []
    exact, base = set(), {}
    for m, c in db.execute(
            select(OrderLine.market, OrderLine.client).distinct()).all():
        exact.add((_key(m), _key(c)))
        bk = _base_key(c)
        if bk != _key(c):
            base[(_key(m), bk)] = c
    out = []
    for r in rows:
        if (r.market_key, r.client_key) in exact:
            continue
        hit = base.get((r.market_key, r.client_key))
        if hit:
            out.append((r.market, r.client, hit))
    out.sort()
    return out[:limit]


def coverage_end(db: Session, period: str):
    """The last day the loaded file actually has data for, in that month.

    NOT THE END OF THE MONTH. A file that stops on the 31st and a file that
    stops on the 12th look identical to a client that stopped on the 12th, and
    the whole board was flagged "stopped 2026-07-31" the first time only July
    was uploaded - because every client's last day was the last day there was.

    So "it stopped" means "it stopped before the data did", and a client still
    delivering on the last day the file knows about has not stopped at all.
    """
    end = db.scalar(select(func.max(ServedDays.last_day)).where(
        ServedDays.period == period))
    return end


def last_served(db: Session, period: str) -> dict[tuple[str, str], object]:
    """{(market key, client key): the last day it delivered this month}.

    THE END DATE ON A CANCELLED ORDER IS THE DATE IT WAS SOLD TO RUN TO, not
    the day it stopped. Nothing on the export says when somebody hit cancel, so
    a lifetime pulled to the order's end date covers weeks of nothing. The last
    day with delivery on it is the real end of the campaign, and it is the
    date the report should be pulled to.
    """
    return {(r.market_key, r.client_key): r.last_day for r in db.scalars(
        select(ServedDays).where(ServedDays.period == period)).all()
        if r.last_day}


def has_serving(db: Session, period: str) -> bool:
    return db.scalar(select(ServedDays.id).where(
        ServedDays.period == period).limit(1)) is not None
