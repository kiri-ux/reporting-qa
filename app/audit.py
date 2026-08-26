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

ORDER_ID = re.compile(r"#\s*(\d{4,6})")
LIFETIME = re.compile(r"\blifetime\b", re.I)

# "7MOU SG - Benton Rodeo #53915 LIFETIME" -> "Benton Rodeo". The prefix is the
# market's own shorthand, which is not what the client is called anywhere else.
PREFIX = re.compile(r"^\s*[A-Z0-9&']{2,8}\s+[A-Z]{2,4}\s*-\s*", re.I)


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
        name = PREFIX.sub("", name)
        name = re.sub(r"[\\/\s]+", " ", name).strip(" -/\\")
        if not name and not ids:
            continue
        # A header row names columns, not campaigns.
        if _key(name) in ("campaign", "client", "market", "list", "campaigns"):
            continue
        out.append({"raw": best, "client": name, "ids": ids,
                    "kind": "lifetime" if LIFETIME.search(best) else "monthly"})
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
            missing.append({**row, "why": _why(db, period, row, not_owed)})

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
            "covered": sorted(covered)}


def _why(db, period: str, row: dict, not_owed: list) -> str:
    """Why this row is not on the board, in the board's own words where it has
    them and in the order line's where it does not."""
    from .db import OrderLine
    from .cycle import cycle_for
    from sqlalchemy import select

    want = _key(row["client"])
    # 1. The board looked at it and decided not to ask. That reason is the best
    #    one there is, because it is the actual rule that fired.
    for s in not_owed:
        if _key(s.get("client", "")) == want and s.get("kind", row["kind"]) == row["kind"]:
            return s.get("why", "not owed this cycle")

    # 2. No order line carries these ids at all.
    lines = []
    if db is not None:
        ids = row["ids"]
        if ids:
            for l in db.scalars(select(OrderLine)).all():
                have = set((l.account_ids or "").replace(",", " ").split())
                if have & set(ids):
                    lines.append(l)
        if not lines:
            for l in db.scalars(select(OrderLine)).all():
                if _key(l.client or "") == want:
                    lines.append(l)
    if not lines:
        return ("no order line carries "
                + (", ".join(row["ids"]) if row["ids"] else "this client")
                + " - it is not in the export, or the export is out of date")

    if all(getattr(l, "canceled", False) for l in lines):
        return "every line on this order is canceled"

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
        if ends and not any(cyc.needs_lifetime(l.order_ends_on or l.ends_on)
                            for l in lines):
            last = max((l.order_ends_on or l.ends_on) for l in lines
                       if (l.order_ends_on or l.ends_on))
            return (f"the campaign runs to {last}, past this cycle's lifetime "
                    f"window (to {cyc.lifetime_cutoff})")
        return "no line ends inside this cycle's lifetime window"

    if not any(cyc.was_live(l.starts_on, l.ends_on) for l in lines):
        return "no line was live during the data month"
    return ("the order is loaded and looks live - open the client's order lines,"
            " this is worth a closer look")
