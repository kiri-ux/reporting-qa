"""What the report says was spent, per product.

The Spend Overview section prints a tile per product - "PPC Ad Cost",
"LinkedIn Ad Cost", "Performance Max Cost" - each with its figure underneath.
That figure is the whole month's spend for that product, and the order says
what it was meant to be. Pacing is the comparison of the two.

Read off the TILES rather than by adding up a cost grid. The grid is a top-N
list on most reports, so its total is not the campaign's, and the tile is the
number the client reads anyway.

Column position is how a tile is matched to its figure. Two tiles sit side by
side on one line - "PPC Ad Cost" and "PPC Cost-Per-Click" - and their values
sit side by side underneath, so taking the first dollar amount on the next line
gives the cost-per-click as often as the cost. The value belonging to a tile is
the one nearest its own column.
"""
from __future__ import annotations

import re

MONEY = re.compile(r"\$\s?([\d,]+(?:\.\d+)?)")

# Tile label -> the product it is the spend for. The wording is TapClicks' own.
SPEND_TILES: list[tuple[str, str]] = [
    ("Performance Max Cost", "Performance Max"),
    ("PPC Ad Cost", "PPC"),
    ("LinkedIn Ad Cost", "LinkedIn"),
    ("Client Ad Cost", ""),          # product taken from the section it is in
]

# How far below a label its figure can be. The tile prints a label, a one-line
# description, then the number, with blank lines between - six is comfortable
# and stops a tile borrowing the next widget's figure when its own is missing.
LOOK_AHEAD = 6


def _amounts(line: str) -> list[tuple[int, float]]:
    """(column, value) for every dollar amount on this line."""
    out = []
    for m in MONEY.finditer(line):
        try:
            out.append((m.start(), float(m.group(1).replace(",", ""))))
        except ValueError:
            pass
    return out


def tile_value(text: str, label: str) -> float | None:
    """The figure printed under this tile, matched by column."""
    lines = text.split("\n")
    for i, line in enumerate(lines):
        at = line.find(label)
        if at < 0:
            continue
        # A label inside a sentence is a footnote about the tile, not the tile.
        after = line[at + len(label):at + len(label) + 1]
        if after and after not in " \t":
            continue
        for nxt in lines[i + 1:i + 1 + LOOK_AHEAD]:
            got = _amounts(nxt)
            if not got:
                continue
            # Nearest column wins. "PPC Ad Cost" and "PPC Cost-Per-Click" share
            # a line and their values share the line below it.
            return min(got, key=lambda c: abs(c[0] - at))[1]
    return None


# "<Product> Cost per Line Item" - the grid, for reports that print no tile.
COST_GRID = re.compile(r"^[ \t]*(.+?)\s+Cost per Line Item[ \t]*$", re.M)

# The grid says so itself when it is a top-N list, and a partial total is worse
# than none: it paces a full month as short.
TRUNCATED = "Grid contains more rows"


def grid_spend(text: str) -> dict[str, float]:
    """Spend read off the cost grid, for reports with no Spend Overview tile.

    Reliance Bank is twelve pages of PPC with no tile anywhere - its only cost
    is the 1,824.25 in PPC Cost per Line Item - so pacing printed a dash where
    the month's spend belongs and said "no comparison" about a number that is
    on the report.

    The Cost column is found by its own header rather than by position: the
    grid also carries Impressions, Clicks, CTR and Avg. CPC, and taking the
    money-looking one gives the cost-per-click as often as the cost.
    """
    out: dict[str, float] = {}
    names = {p.lower(): p for _l, p in SPEND_TILES if p}
    lines = text.split("\n")
    for m in COST_GRID.finditer(text):
        product = names.get(m.group(1).strip().lower())
        if not product:
            continue
        i = text.count("\n", 0, m.start()) + 1
        col, total, rows = None, 0.0, 0
        for line in lines[i:i + 60]:
            t = line.strip()
            if not t:
                continue
            if TRUNCATED in line:
                col, rows = None, 0
                break
            cells = [c for c in re.split(r"\s{2,}", t) if c]
            if col is None:
                if "Cost" in cells:
                    col = cells.index("Cost")
                continue
            if _ends_the_grid(line, cells):
                break
            if len(cells) <= col:
                continue
            try:
                total += float(cells[col].replace(",", "").lstrip("$"))
                rows += 1
            except ValueError:
                continue
        if col is not None and rows:
            out[product] = total
    return out


def _ends_the_grid(line: str, cells: list[str]) -> bool:
    """The next widget's heading: one cell, and not a number."""
    if COST_GRID.match(line):
        return True
    return len(cells) == 1 and not re.fullmatch(r"[\d,.$%-]+", cells[0])


def report_spend(text: str) -> dict[str, float]:
    """Every product whose spend this report prints.

    The tile wins where there is one: it is the whole month, and the grid can
    be a top-N list. The grid fills in for a report that prints no tile at all.
    """
    out: dict[str, float] = {}
    for label, product in SPEND_TILES:
        if not product:
            continue
        got = tile_value(text, label)
        if got is not None:
            out[product] = got
    for product, got in grid_spend(text).items():
        out.setdefault(product, got)
    return out
