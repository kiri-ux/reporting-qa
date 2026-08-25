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


def report_spend(text: str) -> dict[str, float]:
    """Every product whose spend this report prints."""
    out: dict[str, float] = {}
    for label, product in SPEND_TILES:
        if not product:
            continue
        got = tile_value(text, label)
        if got is not None:
            out[product] = got
    return out
