"""Checks on what the report SAYS, not on whether its numbers add up.

The rules in rules.py mostly do arithmetic. These read the words: a strategy
line the buyer named badly, a grid cell the layout clipped, a conversion still
carrying the word "retargeting", a widget that printed an error instead of a
table. None of it is a maths fault, and all of it is visible to whoever the
report is sent to.
"""
from __future__ import annotations

import re

# A page header, e.g. "TIKTOK CONVERSIONS - PAGE 1". Used to say WHERE a fault
# is, since pdftotext drops the form feeds that would have given page numbers.
PAGE_HEADER = re.compile(r"^\s*([A-Z][A-Z0-9 &+/'.-]{2,60}?)\s+-\s+(?:PAGE\s+\d+|SUMMARY GRIDS)",
                         re.M)

# Boilerplate that appears on every page and is never a data row.
CHROME = ("Digital Marketing Report", "Date range ", "Created On ",
          "Grid contains more rows")

NUMERIC = re.compile(r"^\$?-?[\d,]+(?:\.\d+)?%?$")


def _is_chrome(line: str) -> bool:
    return any(c in line for c in CHROME)


def section_at(text: str, pos: int) -> str:
    """The page header in force at this point in the report."""
    last = ""
    for m in PAGE_HEADER.finditer(text, 0, max(pos, 1)):
        last = m.group(1).strip()
    return last or "the report"


def grid_rows(text: str, start: int, stop_at_new_section: bool = True) -> list[tuple[str, int]]:
    """Rows of a grid, as (first cell, offset), with wrapped cells joined.

    TapClicks wraps a long name onto the lines BELOW its own numbers, so a row
    is "a line whose cells after the first are all numeric" and every text-only
    line after it belongs to the row above.
    """
    rows: list[tuple[str, int]] = []
    cur: list[str] | None = None
    cur_at = 0
    pos = start
    here = section_at(text, start)
    for line in text[start:].split("\n"):
        at = pos
        pos += len(line) + 1
        t = line.strip()
        if not t or _is_chrome(line):
            continue
        m = PAGE_HEADER.match(line)
        if m:
            if stop_at_new_section and m.group(1).strip() != here:
                break
            continue
        cells = [c for c in re.split(r"\s{2,}", t) if c]
        if len(cells) >= 3 and all(NUMERIC.match(c) for c in cells[1:]):
            if cur:
                rows.append((" ".join(cur), cur_at))
            cur, cur_at = [cells[0]], at
        elif cur is not None and len(cells) == 1:
            cur.append(t)
        elif cur is not None and len(cells) >= 3:
            rows.append((" ".join(cur), cur_at))
            cur = None
    if cur:
        rows.append((" ".join(cur), cur_at))
    return rows


def widget_rows(text: str, title_test) -> list[tuple[str, str, int]]:
    """(widget title, first cell, offset) for every grid whose title passes."""
    out = []
    for m in re.finditer(r"^[ \t]*(\S[^\n]*?)[ \t]*$", text, re.M):
        title = m.group(1).strip()
        if not title_test(title) or _is_chrome(title):
            continue
        for name, at in grid_rows(text, m.end()):
            out.append((title, name, at))
    return out
