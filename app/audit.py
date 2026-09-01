"""Compare the board against a list somebody keeps by hand.

The board is built from the order export. The reporting tracker is built from
what people know. When the two disagree, one of them is wrong, and until now
finding out which meant reading 42 rows of a spreadsheet against 42 rows of a
web page.

MATCHED ON ORDER ID, NOT ON NAME. "7MOU SG - Susquehanna River Valley Visitors
Bureau #45716/ #54719/ #54966" and the board's "Susquehanna River Valley
Visitors Bureau" are the same campaign written two ways; the numbers in them
are the same either way. A row with no id at all falls back to the name.
"""
from __future__ import annotations

import re

from .cycle import month_label

ORDER_ID = re.compile(r"#\s*(\d{4,6})")
LIFETIME = re.compile(r"\blifetime\b", re.I)
SEO = re.compile(r"\bSEO\b", re.I)

# "7MOU SG - Benton Rodeo #53915 LIFETIME" -> "Benton Rodeo". The prefix is the
# market's own shorthand, which is not what the client is called anywhere else.
#
# THE SECOND WORD IS OPTIONAL. This wanted two of them - "7MOU SG" - and half
# the tracker writes one: "ADM - VSCU KC" kept its prefix, so the client came
# out as "ADM - VSCU KC", matched nothing on the board by name, and every one
# of those rows was explained by the catch-all reason instead of the real one.
#
# AND IT IS CASE SENSITIVE NOW. These are shouted market codes, and matching
# them case-insensitively while the second word is optional would strip the
# front off any client whose name happens to start with a short word and a
# dash. A code is uppercase or it is not a code.
PREFIX = re.compile(r"^\s*[A-Z0-9&']{2,8}(?:\s+[A-Z]{2,4})?\s*-\s*")


def _key(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def parse_list(text: str) -> list[dict]:
    """One entry per line of a pasted list, or per row of a pasted CSV.

    Tabs and commas both split columns, because this arrives as a paste out of
    Google Sheets one day and a saved CSV the next. Any column can hold the
    campaign; the one carrying an order id wins, and failing that the longest.
    """
    out = []
    for raw in (text or "").splitlines():
        line = raw.strip().strip("|").strip()
        if not line:
            continue
        # Tabs from a Google Sheets paste, commas from a saved CSV, pipes from
        # a markdown copy. All three turn up.
        cells = [c.strip().strip('"') for c in
                 re.split(r"\t|\||,(?=(?:[^\"]*\"[^\"]*\")*[^\"]*$)", line)]
        cells = [c for c in cells if c]
        if not cells:
            continue
        best = max(cells, key=lambda c: (len(ORDER_ID.findall(c)), len(c)))
        # A COMMA IN THE CLIENT'S OWN NAME SPLITS IT IN HALF. "Altiery
        # Gingerich Insurance Agency, LLC #53106 SEO" comes out of a CSV as two
        # cells, and the half carrying the id is "LLC #53106 SEO". The id is
        # still right; the name is a fragment, so its other half is glued back.
        i = cells.index(best)
        while i and len(re.sub(r"[^A-Za-z]", "", ORDER_ID.sub("", best))) < 8:
            i -= 1
            if ORDER_ID.search(cells[i]):
                break
            best = cells[i] + ", " + best
        ids = ORDER_ID.findall(best)
        # THE NAME ENDS AT THE MARKER. "Sorge Funeral Home & Crematory #45911
        # LIFETIME -End Date 2026-12-31" is a client and a note somebody left
        # themselves; removing the word LIFETIME and keeping the rest gave a
        # client called "Sorge Funeral Home & Crematory -End Date 2026-12-31".
        head = LIFETIME.split(best)[0]
        head = re.split(r"\bSEO\b", head)[0]
        name = ORDER_ID.sub("", head)
        # THE MARKET CODE IS KEPT, NOT JUST REMOVED.
        #
        # It is the only thing on the row that says which partner it belongs
        # to, and a whole partner missing from the order export reads exactly
        # like six unrelated missing orders when you cannot group them.
        m = PREFIX.match(name)
        prefix = (m.group(0).strip(" -") if m else "").strip()
        name = PREFIX.sub("", name)
        name = re.sub(r"[\\/\s]+", " ", name).strip(" -/\\")
        if not name and not ids:
            continue
        # A header row names columns, not campaigns.
        if _key(name) in ("campaign", "client", "market", "list", "campaigns"):
            continue
        # SEO IS ITS OWN ROW ON THE BOARD, so it has to be its own row here.
        #
        # The tracker writes it the same way it writes LIFETIME - "WHIT -
        # Jefferson Hospital #54153 SEO" - and the word was already being
        # stripped off the client's name. Read as a monthly it would look for a
        # digital row that does not exist and report every SEO client on the
        # list as missing from the board.
        kind = ("lifetime" if LIFETIME.search(best)
                else "seo" if SEO.search(best) else "monthly")
        out.append({"raw": best, "client": name, "ids": ids, "prefix": prefix,
                    "kind": kind})
    return out


def audit(db, period: str, text: str, group: str = "") -> dict:
    """What the list has that the board does not, and the other way round.

    AND WHY, for every row that is missing. "Not on the board" is where the
    question starts, not where it ends - the useful answer is which of the
    dozen reasons this cycle has for not owing a report applies to this one.
    """
    from .board import excluded, expected_for

    listed = parse_list(text)
    # The board's own reasons for the rows it decided not to ask for.
    not_owed: list = []
    board = [e for e in expected_for(db, period, skipped=not_owed)
             if not excluded(e.market)]
    if group:
        g = _key(group)
        board = [e for e in board
                 if g in _key(e.group) or g in _key(e.market)]

    # Two ways in to the same row, because either side can be missing an id.
    by_id: dict[tuple[str, str], list] = {}
    by_name: dict[tuple[str, str], list] = {}
    for e in board:
        for i in re.findall(r"\d{4,6}", e.account_ids or ""):
            by_id.setdefault((i, e.kind), []).append(e)
        by_name.setdefault((_key(e.client), e.kind), []).append(e)

    hit: set[int] = set()
    missing, matched = [], []
    for row in listed:
        found = []
        for i in row["ids"]:
            found += by_id.get((i, row["kind"]), [])
        if not found:
            found = by_name.get((_key(row["client"]), row["kind"]), [])
        if found:
            for e in found:
                hit.add(id(e))
            matched.append({**row, "board": found[0]})
        else:
            missing.append({**row, "why": _why(db, period, row, not_owed),
                            "status": _order_status(db, row)})

    # THE LIST SCOPES ITSELF.
    #
    # A list covering one partner compared against the whole board reports the
    # other 145 partners as "missing from your list", which is 1,050 rows of
    # noise around the handful that matter. The partners the list DOES cover
    # are the ones it can say anything about - and they are known, because its
    # rows matched board rows in them.
    covered = {e.group for e in board if id(e) in hit}
    extra = [e for e in board if id(e) not in hit
             and (not covered or e.group in covered)]
    return {"listed": listed, "board": board, "matched": matched,
            "missing": missing, "extra": extra,
            "gone": _partners_missing_entirely(listed, missing),
            "covered": sorted(covered)}


NOT_IN_EXPORT = "not in the export"


def _served_nothing(db, period: str, want: str) -> bool:
    """Was a serving file loaded for this cycle, and this client not in it?

    Only worth saying when the file exists. With nothing loaded, silence about
    a client means nothing at all - and a reason that reads like evidence when
    it is an empty table is the kind of confident wrong answer this page is
    supposed to be getting rid of.
    """
    if db is None:
        return False
    try:
        from sqlalchemy import select
        from .db import ServedDays
        rows = db.scalars(select(ServedDays)
                          .where(ServedDays.period == period)).all()
    except Exception:                                        # noqa: BLE001
        return False
    if not rows:
        return False
    return not any(_key(r.client or "") == want for r in rows)


def _partners_missing_entirely(listed: list, missing: list) -> list:
    """Market codes whose every row is missing because the export never had it.

    ONE MISSING ORDER AND A MISSING PARTNER LOOK THE SAME ONE ROW AT A TIME.
    "no order line carries 52029" is a fair thing to say about one campaign and
    a very unfair thing to say six times about a partner whose orders are not
    in the feed at all - which is a far worse problem, because every client
    they have is invisible to the board and nothing else would ever mention it.

    Worked out from the codes on the pasted list, so it needs no way of turning
    "ROI SAM" into a partner name: if every row carrying a code is missing, and
    missing for that reason, the code is the thing that is gone.
    """
    total: dict[str, int] = {}
    for row in listed:
        p = (row.get("prefix") or "").strip()
        if p:
            total[p] = total.get(p, 0) + 1
    lost: dict[str, int] = {}
    for row in missing:
        p = (row.get("prefix") or "").strip()
        if p and NOT_IN_EXPORT in (row.get("why") or ""):
            lost[p] = lost.get(p, 0) + 1
    # TWO ROWS BEFORE SAYING IT. With one row on the list under a code there is
    # no way to tell a missing order from a missing partner, and a panel that
    # cries partner at every single missing order is a panel people stop
    # reading - which costs the one time it is real.
    return sorted(
        ({"prefix": p, "rows": n} for p, n in lost.items()
         if n >= 2 and total.get(p) == n),
        key=lambda d: (-d["rows"], d["prefix"]))


def _dropped_reason(db, names: set, ids=()) -> str:
    """What the last sync said about rows it threw away for this client.

    The import records why each client's rows were dropped, and until now only
    the lookup page ever read it. It is the missing half of several answers on
    this page: the line that would have explained the row is precisely the one
    that is not in the table to be looked at.
    """
    if db is None:
        return ""
    # INDEXED ONCE. The last sync's drop log runs to a couple of thousand
    # clients on the real export, and this is asked for every missing row.
    idx = getattr(db, "_qa_drop_index", None)
    if idx is None:
        idx, by_order = {}, {}
        try:
            from sqlalchemy import desc, select
            from .db import OrderSync
            sync = db.scalars(select(OrderSync)
                              .where(OrderSync.ok.is_(True))
                              .order_by(desc(OrderSync.id)).limit(1)).first()
            for pair, why in (getattr(sync, "dropped", None) or {}).items():
                _market, _, client = pair.partition("|")
                k = _key(client)
                if k and why not in idx.setdefault(k, []):
                    idx[k].append(why)
            by_order = dict(getattr(sync, "dropped_orders", None) or {})
        except Exception:                                    # noqa: BLE001
            idx, by_order = {}, {}
        try:
            db._qa_drop_index = idx      # for the life of this request only
            db._qa_drop_orders = by_order
        except Exception:                                    # noqa: BLE001
            pass
    # THE ORDER'S OWN REASON FIRST. The client map holds the first reason
    # recorded for that client, and a client with two orders has two - which is
    # how order 51554 came to be called an RFP when its two lines are IO
    # Complete and ended in May.
    for oid in (ids or ()):
        why = (getattr(db, "_qa_drop_orders", None) or {}).get(str(oid).strip())
        if why:
            return why
    for name in names:
        why = idx.get(name)
        if why:
            # TWO REASONS IS INFORMATION, THREE IS NOISE. A client can be
            # dropped for more than one thing - some rows an RFP, some ended -
            # and picking whichever the export happened to list first would
            # answer a different question each time the file changed.
            return ", and ".join(why[:2])
    return ""


def _order_status(db, row) -> str:
    """What the export says this order's line items are, in its own words.

    ASKED FOR SO A REJECT CAN BE MADE WITHOUT OPENING ANOTHER TAB. "Nothing
    served this month" and "IO Paused" together are a decision; either one on
    its own is a question, and the second half was in the IO tool.
    """
    if db is None:
        return ""
    try:
        by_id, by_client = _order_index(db)
    except Exception:                                        # noqa: BLE001
        return ""
    lines = []
    for oid in row.get("ids") or ():
        lines.extend(by_id.get(str(oid).strip(), ()))
    if not lines:
        lines = list(by_client.get(_key(row.get("client", "")), ()))
    want = {str(i).strip() for i in (row.get("ids") or ()) if str(i).strip()}
    seen = []
    # THE IMPORT'S OWN RECORD FIRST, because a row that was dropped has no
    # order line left to read a status off - and those are the rows on this
    # table. See order_statuses in orders_io.
    for oid in want:
        st = (_sync_statuses(db) or {}).get(oid)
        if st and st not in seen:
            seen.append(st)
    for l in lines:
        for d in (getattr(l, "detail", None) or []):
            if not isinstance(d, dict):
                continue
            if want and str(d.get("order") or "").strip() not in want:
                continue
            st = (d.get("status") or d.get("order_status") or "").strip()
            if st and st not in seen:
                seen.append(st)
    # Two is a mixed order, which is worth seeing. Six is the whole status
    # vocabulary and says nothing.
    return ", ".join(seen[:3])


def _sync_statuses(db) -> dict:
    """{order id: status} off the last sync, read once per request."""
    cached = getattr(db, "_qa_order_statuses", None)
    if cached is not None:
        return cached
    out = {}
    try:
        from sqlalchemy import desc, select
        from .db import OrderSync
        sync = db.scalars(select(OrderSync).where(OrderSync.ok.is_(True))
                          .order_by(desc(OrderSync.id)).limit(1)).first()
        out = dict(getattr(sync, "order_statuses", None) or {})
    except Exception:                                        # noqa: BLE001
        out = {}
    try:
        db._qa_order_statuses = out
    except Exception:                                        # noqa: BLE001
        pass
    return out


def _order_end_dates(lines, ids) -> list:
    """Each ORDER's own end date behind these rows, for the ids asked about.

    The rolled-up row carries the latest end across every order it covers,
    which is the wrong date to quote at somebody asking about one of them.
    """
    import datetime as _dt
    want = {str(i).strip() for i in (ids or ()) if str(i).strip()}
    out = []
    for l in lines:
        for d in (getattr(l, "detail", None) or []):
            if not isinstance(d, dict):
                continue
            if want and str(d.get("order") or "").strip() not in want:
                continue
            raw = d.get("order_ends") or d.get("ends")
            if not raw:
                continue
            try:
                out.append(_dt.date.fromisoformat(str(raw)[:10]))
            except ValueError:
                continue
    return out


def _order_index(db):
    """Every order line, indexed by order id and by client, built once.

    Explaining one missing row used to read the whole order table - twice, if
    the ids did not match. A list of a hundred and fifty missing rows read it
    three hundred times to answer the same question.
    """
    from .db import OrderLine
    from sqlalchemy import select

    cached = getattr(db, "_qa_order_index", None)
    if cached is not None:
        return cached
    by_id: dict[str, list] = {}
    by_client: dict[str, list] = {}
    for l in db.scalars(select(OrderLine)).all():
        for oid in (l.account_ids or "").replace(",", " ").split():
            by_id.setdefault(oid, []).append(l)
        by_client.setdefault(_key(l.client or ""), []).append(l)
    out = (by_id, by_client)
    try:
        db._qa_order_index = out          # for the life of this request only
    except Exception:                                        # noqa: BLE001
        pass
    return out


def _why(db, period: str, row: dict, not_owed: list) -> str:
    """Why this row is not on the board, in the board's own words where it has
    them and in the order line's where it does not."""
    from .db import OrderLine
    from .cycle import cycle_for
    from sqlalchemy import select

    want = _key(row["client"])

    def board_said(names) -> str:
        for s in not_owed:
            if _key(s.get("client", "")) in names and \
                    s.get("kind", row["kind"]) == row["kind"]:
                return s.get("why", "not owed this cycle")
        return ""

    # 1. The board looked at it and decided not to ask. That reason is the best
    #    one there is, because it is the actual rule that fired.
    said = board_said({want})
    if said:
        return said

    # 2. No order line carries these ids at all.
    #
    # READ ONCE, NOT ONCE PER ROW. This walked the whole order table twice for
    # every row it had to explain - 2,400 lines rebuilt 150 times over, on a
    # page that already takes its time.
    lines = []
    if db is not None:
        by_id, by_client = _order_index(db)
        for oid in row["ids"]:
            lines.extend(by_id.get(oid, ()))
        if not lines:
            lines = list(by_client.get(want, ()))

    # 1b. ASK AGAIN UNDER THE NAME THE BOARD USES.
    #
    # The two tools spell clients differently, which is the whole reason this
    # page exists, and the order id is the one thing they agree on. Matching
    # the board's reason by name alone meant a row whose name did not line up
    # fell past the real answer and got the catch-all - "the order is loaded
    # and looks live, worth a closer look" - for a campaign the board had
    # perfectly good reasons about. Four VSCU orders read that way while the
    # truth was that they ran one day in August.
    if lines:
        said = board_said({_key(l.client or "") for l in lines})
        if said:
            return said
    if not lines:
        # ASK WHY IT IS NOT HERE BEFORE SAYING IT WAS NEVER SENT.
        #
        # "It is not in the export" was said about orders that are plainly IN
        # the export - 53437 has three paused line items that ended on 30 June,
        # 54338 is complete - because the import drops everything outside the
        # cycle and this only ever looked at what survived. An empty table
        # therefore read as an empty feed.
        #
        # That is the worst kind of wrong answer here: it accuses the export,
        # which is somebody else's system, and sends whoever is on this page to
        # go and check a file that is perfectly correct. The import already
        # writes down why it threw each client's rows away. This asks.
        gone = _dropped_reason(db, {want}, row["ids"])
        if gone:
            return ("the export has this order and every line on it " + gone)
        return ("no order line carries "
                + (", ".join(row["ids"]) if row["ids"] else "this client")
                + " - it is not in the export, or the export is out of date")

    # "EVERY LINE ON THIS ORDER IS CANCELED" WAS A CLAIM ABOUT THE SURVIVORS.
    #
    # The import keeps the lines that touch this cycle and throws the rest
    # away, so "every line" here means every line that got through - and order
    # 50236 proved how badly that reads. Its Mobile Conquesting was PAUSED and
    # ended on 31 July, so it was dropped for being out of the window; its two
    # canceled lines run to December, so they were kept. The board then told
    # her every line was canceled about an order whose only live line had
    # simply finished, and the IO tool on screen said Paused.
    #
    # So the claim is scoped to what it is actually about, and the reason the
    # others went is added when the last sync recorded one.
    if all(getattr(l, "canceled", False) for l in lines):
        gone = _dropped_reason(db, {_key(l.client or "") for l in lines} | {want},
                               row["ids"])
        if gone:
            # WRITTEN, NOT GLUED. The first version of this bolted the drop
            # reason onto the end with a dash and read as two half-sentences
            # that had never met - "canceled - and a line item ended before
            # 2026-08 started". The reader has to end up knowing one thing:
            # there was another line, it was not canceled, and here is why it
            # is not on this cycle.
            return ("the only lines on this order that reach this cycle are "
                    "canceled. The other one is not canceled - it " + gone)
        return "every line on this order is canceled"

    # THE ORDER IS THERE AND NOTHING ON IT EARNS A REPORT.
    #
    # Live Chat rides along with a digital product; Website Visitor ID and
    # Additional Billing never appear on a report at all. An order carrying
    # only those is not a miss - it is the rule working - and it was being
    # explained with the reason belonging to an order that is not in the
    # export, which sends somebody off to check a feed for a line that is
    # sitting right there. "#26734 LIVE CHAT ONLY" is exactly this shape.
    from .checks.products import earns_a_report
    live = [l for l in lines if not getattr(l, "canceled", False)]
    if live and not any(earns_a_report(l.product or "") for l in live):
        what = sorted({(l.product or "").strip() for l in live if l.product})
        if len(what) == 1:
            return f"{what[0]} does not earn a report on its own"
        return ("nothing on this order earns a report on its own - "
                + ", ".join(what))

    cyc = cycle_for(period)
    ends = [l.ends_on for l in lines if l.ends_on]
    starts = [l.starts_on for l in lines if l.starts_on]
    if ends and max(ends) < cyc.starts_on:
        return f"every line ended by {max(ends)}, before this cycle"
    if starts and min(starts) > cyc.ends_on:
        return f"nothing starts until {min(starts)}, after this cycle"

    if row["kind"] == "lifetime":
        from .partners import is_seo
        if all(is_seo(l.product or "") for l in lines):
            return "SEO is not owed a lifetime"
        # THE ORDER'S OWN END DATE, NOT THE CLIENT'S WIDEST.
        #
        # An order line row is one client and one product rolled up across
        # every order behind it, and its `order_ends_on` is the LATEST of them.
        # North Bay TIP has order 54783, IO Complete, finished 13 August, and
        # order 55433 still running to 26 September - so the row said 26
        # September and this sentence reported it as "the campaign", about an
        # order that ended six weeks earlier. The per-order detail is on the
        # row; asked properly it gives the right date for the order in hand.
        own = _order_end_dates(lines, row["ids"])
        if own and any(cyc.needs_lifetime(e) for e in own):
            return ("order " + (", ".join(row["ids"]) or "this one")
                    + f" ends {max(e for e in own if cyc.needs_lifetime(e))}, "
                      "inside this cycle's lifetime window - but another order "
                      "for this client is still running, so the lifetime waits "
                      "until the campaign is finished. Approve it here if this "
                      "one should be reported now.")
        if ends and not any(cyc.needs_lifetime(e) for e in (own or [
                (l.order_ends_on or l.ends_on) for l in lines])):
            last = max(own or [(l.order_ends_on or l.ends_on) for l in lines
                               if (l.order_ends_on or l.ends_on)])
            return (f"the campaign runs to {last}, past this cycle's lifetime "
                    f"window (to {cyc.lifetime_cutoff})")
        return "no line ends inside this cycle's lifetime window"

    # THE SAME TEST THE BOARD USES, off the per-line detail rather than the
    # rolled-up window - otherwise this page and the board disagree about the
    # same order, which is worse than either being wrong on its own.
    from .board import _live_in_month
    if not any(_live_in_month(cyc, l, open_only=True) for l in lines):
        # BOTH HALVES, WHEN BOTH ARE TRUE.
        #
        # "No line was live" is the order export's answer and the serving file
        # has its own, and they are far more convincing together than either is
        # alone. Roof Top Services was told only that it was missing from the
        # serving file - which invites the thought that the two tools spell the
        # client differently - when the orders say the same thing: the one line
        # spanning August is cancelled and the next starts in September.
        return ("no line item was running in " + month_label(period)
                + (", and nothing served for this client that month either"
                   if _served_nothing(db, period, want) else ""))
    return ("the order is loaded and looks live - open the client's order lines,"
            " this is worth a closer look")
