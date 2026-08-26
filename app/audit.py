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
        name = LIFETIME.sub("", ORDER_ID.sub("", best))
        name = re.sub(r"\bSEO\b\s*$", "", name)
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
    """What the list has that the board does not, and the other way round."""
    from .board import excluded, expected_for

    listed = parse_list(text)
    board = [e for e in expected_for(db, period) if not excluded(e.market)]
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
            missing.append(row)

    extra = [e for e in board if id(e) not in hit]
    return {"listed": listed, "board": board, "matched": matched,
            "missing": missing, "extra": extra}
