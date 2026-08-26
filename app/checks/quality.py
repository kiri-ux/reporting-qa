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

# TapClicks prints a small icon before the first row of a grid, and it comes
# out of the PDF as a private-use glyph. Harmless while the first line of a
# name was being discarded; the moment names started at their real first line
# it turned up on the front of them.
ICON = re.compile(r"[-]+\s*")


def _clean_cell(s: str) -> str:
    return ICON.sub("", s).strip()

# The wording a widget title tends to end in. On its own this is not enough to
# end a grid - see _title_of_next_widget.
NEXT_WIDGET = re.compile(
    r"^\s*\S.*(?:Performance|Breakout|Publishers|Screenshots|Details|"
    r"Conversions|by Strategy|by Day|by Creative|by Ad Size)\s*$")


def _is_chrome(line: str) -> bool:
    return any(c in line for c in CHROME)


def section_at(text: str, pos: int) -> str:
    """The page header in force at this point in the report."""
    last = ""
    for m in PAGE_HEADER.finditer(text, 0, max(pos, 1)):
        last = m.group(1).strip()
    return last or "the report"


def _header_row(t: str) -> bool:
    """A column header: several cells, none of them a number."""
    cells = [c for c in re.split(r"\s{2,}", t) if c]
    return len(cells) >= 2 and not any(NUMERIC.match(c) for c in cells)


def _looks_like_row(t: str) -> bool:
    """A data row that happens to end in a widget word is still a data row.

    "Acme - Behavioral Display Performance   100   1   1.00%" carries numbers;
    a widget title never does.
    """
    cells = [c for c in re.split(r"\s{2,}", t) if c]
    return len(cells) >= 3 and all(NUMERIC.match(c) for c in cells[1:])


def grid_rows(text: str, start: int, stop_at_new_section: bool = True,
              min_cells: int = 3) -> list[tuple[str, int]]:
    """Rows of a grid, as (first cell, offset), with wrapped cells joined.

    A row is "a line whose cells after the first are all numeric", and the text
    lines around it are the rest of its name.

    WHICH row they belong to is the subtlety. TapClicks centers a row
    vertically, so a name on three lines prints its numbers on the MIDDLE one -
    the name wraps above its own figures as well as below. Treating every text
    line as the tail of the row above therefore did two things at once: it
    threw away the first line of every name, which is the half carrying the
    client, and it glued the next row's opening line onto the previous row's
    name.

    On Window World that put "Amazon CTV" and the start of the next line item
    into one name, so BOTH line items read as CTV, both were left out of the
    clicks total, and the CTR check went on to divide by an empty set.

    Rows of one grid all open the same way - "<Client> - ..." - so the opening
    is learned from the first row and used to cut the block of text lines
    between two rows into the tail of the one above and the head of the one
    below.

    Ending a grid is the hard part. The page-header section is one boundary,
    but several grids share a section, and without a second one a line item
    table ran on into the publisher list below it and reported "Tubi - Free
    Movies & TV" as a badly named strategy. The other boundary is the next
    widget's title - which cannot be recognized by its wording alone, because
    a wrapped name like "Services/Homeowners/Retargeting Performance" reads
    exactly like one. What separates them is that a real title has a column
    header under it and a wrapped name does not.
    """
    rows: list[tuple[str, int]] = []
    cur: list[str] | None = None
    cur_at = 0
    here = section_at(text, start)
    pending: list[str] = []          # text lines whose row is not settled yet
    lead = ""                        # how a row of this grid opens

    def split_pending() -> list[str]:
        """The part of the held text that belongs to the row ABOVE.

        Whatever is left is the opening of the row about to start. With no
        lead learned yet - the first row of the grid - everything held is that
        first row's opening, because there is no row above it to own it.
        """
        nonlocal pending
        block, pending = pending, []
        if not lead:
            pending = block
            return []
        for j, t in enumerate(block):
            if t.startswith(lead):
                pending = block[j:]
                return block[:j]
        return block

    lines, pos = [], start
    for line in text[start:].split("\n"):
        lines.append((line, pos))
        pos += len(line) + 1

    for i, (line, at) in enumerate(lines):
        t = line.strip()
        if not t:
            continue
        # Tested before the chrome filter: the page header shares its line with
        # "Date range Aug 18, 2026", so skipping it as boilerplate threw away
        # the one line that says the section changed.
        m = PAGE_HEADER.match(line)
        if m:
            if stop_at_new_section and m.group(1).strip() != here:
                break
            continue
        if _is_chrome(line):
            continue
        cells = [c for c in re.split(r"\s{2,}", t) if c]
        if len(cells) >= min_cells and all(NUMERIC.match(c) for c in cells[1:]):
            tail = split_pending()
            if cur is not None:
                cur.extend(tail)
                rows.append((" ".join(cur), cur_at))
            head = pending
            pending = []
            cur, cur_at = head + [_clean_cell(cells[0])], at
            if not lead:
                # The opening of the first row, which every other row of this
                # grid repeats. Two words is enough to recognize "Window World"
                # without demanding the whole client name match character for
                # character - and short enough that a one-word client still
                # yields something.
                lead = " ".join(" ".join(cur).split()[:2])
            continue
        # No "only once rows have started" guard here: a grid with no rows at
        # all is exactly the case that ran on into the next widget and reported
        # a DOOH publisher list as twenty badly named strategies.
        if _title_of_next_widget(lines, i):
            break
        if len(cells) == 1:
            c = _clean_cell(t)
            if c:
                pending.append(c)
        elif cur is not None and len(cells) >= min_cells:
            cur.extend(split_pending())
            rows.append((" ".join(cur), cur_at))
            cur = None
    if cur is not None:
        cur.extend(split_pending())
        rows.append((" ".join(cur), cur_at))
    return rows


def _title_of_next_widget(lines: list[tuple[str, int]], i: int) -> bool:
    """Is lines[i] a widget title, rather than the tail of a wrapped name?

    A title is followed by its column header. A wrapped name is followed by
    blank space, or by the next row.
    """
    line = lines[i][0]
    if not NEXT_WIDGET.match(line) or len(line) - len(line.lstrip()) > 4:
        return False
    seen = 0
    for nxt, _ in lines[i + 1:]:
        t = nxt.strip()
        if not t or _is_chrome(nxt) or PAGE_HEADER.match(nxt):
            continue
        seen += 1
        if seen > 3:
            return False
        if _header_row(t):
            return True
    return False


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


# ---------------------------------------------------------------- findings
def _f(code, sev, title, detail, trace=None, where="") -> dict:
    """A finding. `where` is a pill on the report page - the page number and
    the widget - because "1 site clicking above 5%" is true and unhelpful until
    you know which of forty-one pages to open."""
    out = {"code": code, "severity": sev, "title": title, "detail": detail}
    if where:
        out["where"] = where
    if trace:
        out["trace"] = [{"label": l, "value": v} for l, v in trace]
    return out


def _where(ctx, offset: int, widget: str = "") -> str:
    page_of = ctx.get("page_of")
    page = page_of(offset) if page_of and offset is not None and offset >= 0 else 0
    bits = [f"p{page}"] if page else []
    if widget:
        bits.append(widget)
    return " · ".join(bits)


def _sample(items: list[str], n: int = 8) -> str:
    """The first n, then a count. A finding somebody has to act on gets a
    bigger n - a list of names to fix is useless truncated at eight."""
    shown = "; ".join(items[:n])
    return shown + (f"; and {len(items) - n} more" if len(items) > n else "")


# ------------------------------------------------- 1. strategy categorization
# A line item is named "<Client> - <targeting> <Product>". TapClicks reads the
# product out of that name to file the line under a product on the breakout
# donut. When the name carries no product word at all there is nothing to read,
# so the raw name becomes its own slice - which is what "JAGHousing -
# Geo-Retargeting Social Mirror" sitting between "Display" and "Meta" means.
#
# Matching on the product word appearing ANYWHERE rather than at the end is
# deliberate. Plenty of good names carry a trailing qualifier - an order number,
# "- Non-Muncie", "(Crozet)" - and demanding the product come last flagged 263
# of one report's 3,756 line items. Requiring it merely to be present flagged 8,
# every one of them a name a buyer needs to fix.
PRODUCT_WORDS = (
    "social mirror", "facebook", "instagram", "meta", "performance max", "pmax",
    "mobile", "display", "video", "ctv", "ott", "native", "youtube", "amazon",
    "tiktok", "linkedin", "online audio", "audio", "ppc", "pay-per-click",
    "dooh", "news feed", "dynamic", "live chat", "geo-framing", "geo framing",
    "visitor id", "reputation", "seo", "twitch", "spotify", "pandora",
)

LINE_ITEM_GRID = re.compile(r"^[ \t]*((?:[A-Z][\w+&/'. -]*)?Line Item Performance)[ \t]*$", re.M)


def line_item_names(text: str) -> list[tuple[str, int]]:
    """Every line item on the report, once.

    "DOOH Line Item Performance" and "Line Item Performance" are two grids in
    the same section, so reading from each title covers the second one twice.
    """
    seen: set[int] = set()
    out: list[tuple[str, int]] = []
    for m in LINE_ITEM_GRID.finditer(text):
        # DOOH counts in "DOOH Ads Served" and has no clicks or CTR column, so
        # its rows are a name and one number. The three-cell rule that keeps
        # prose out of every other grid was throwing all of them away, and the
        # line item sum came up short by exactly the DOOH figure.
        two = m.group(1).strip().upper().startswith("DOOH")
        for name, at in grid_rows(text, m.end(), min_cells=2 if two else 3):
            if at not in seen:
                seen.add(at)
                out.append((name, at))
    return out


def line_item_totals(text: str) -> list[tuple[str, float, float]]:
    """(name, impressions, clicks) for every line item on the report.

    The report's own footnote says the top-line CTR leaves CTV, YouTube and
    PMax out of BOTH halves of the fraction, so checking it needs the campaign
    broken down far enough to leave them out too. This grid is the only place
    that breakdown exists in the text.
    """
    out = []
    for name, at in line_item_names(text):
        eol = text.find("\n", at)
        line = text[at:eol if eol > 0 else len(text)]
        cells = [c for c in re.split(r"\s{2,}", line.strip()) if c]
        vals = []
        for c in cells[1:]:
            if NUMERIC.match(c) and not c.endswith("%"):
                try:
                    vals.append(float(c.replace(",", "").lstrip("$")))
                except ValueError:
                    pass
        if len(vals) >= 2:
            out.append((name, vals[0], vals[1]))
        elif len(vals) == 1:
            # DOOH counts in "DOOH Ads Served" and has no clicks column, so its
            # rows carry one number. Skipping them left the line item sum
            # 36,666 short of a top line that plainly included them - exactly
            # the DOOH figure, on a report that was then failed for it.
            out.append((name, vals[0], 0.0))
    return out


def check_strategy_categorized(ctx) -> list[dict]:
    """Every strategy line has to name the product it runs."""
    text = ctx.get("text") or ""
    bad, seen, first_at = [], set(), -1
    for name, at in line_item_names(text):
        low = name.lower()
        if any(w in low for w in PRODUCT_WORDS):
            continue
        if name in seen:
            continue
        seen.add(name)
        bad.append(name)
        if first_at < 0:
            first_at = at
    if not bad:
        return []
    return [_f("strategy_uncategorized", "fail",
               f"{len(bad)} strategy line{'s' if len(bad) > 1 else ''} not "
               f"categorized to a product",
               "No product word in the name, so each lands on the product "
               "breakout as its own slice. Fix in the order: "
               + _sample(sorted(bad), 30),
               where=_where(ctx, first_at, "Line Item Performance"))]


# ------------------------------------------------------ 2. truncated text
# Two ways a cell runs out of room. The chart kind prints an ellipsis
# ("Category Tar...") and is easy. The grid kind just stops mid-word, and the
# only reason it is findable at all is that the same name usually appears
# somewhere else in the report at full length.
ELLIPSIS = re.compile(r"[A-Za-z0-9)\]](?:\.\.\.|…)")


def check_truncated_text(ctx) -> list[dict]:
    """Nothing on the report should be cut off for want of space."""
    text = ctx.get("text") or ""
    out = []

    cut, cut_at = [], -1
    at = 0
    for line in text.split("\n"):
        here, at = at, at + len(line) + 1
        if _is_chrome(line):
            continue
        for m in ELLIPSIS.finditer(line):
            frag = line[max(0, m.start() - 40):m.end() + 8].strip()
            if frag not in cut:
                cut.append(frag)
                if cut_at < 0:
                    cut_at = here
    if cut:
        out.append(_f("text_truncated", "fail",
                      f"{len(cut)} label{'s' if len(cut) > 1 else ''} cut off",
                      "Runs past the space it was given. Widen the column or "
                      "turn wrap text on: " + _sample(cut),
                      where=_where(ctx, cut_at)))

    clipped, clipped_at = _clipped_cells(text)
    if clipped:
        out.append(_f("text_truncated", "fail",
                      f"{len(clipped)} grid cell{'s' if len(clipped) > 1 else ''} "
                      f"cut off mid-word",
                      "The same value appears in full elsewhere, so this one "
                      "lost its last characters to the column width: "
                      + _sample([f"{a!r} (should be {b!r})" for a, b in clipped]),
                      where=_where(ctx, clipped_at, "Line Item Performance")))
    return out


def _clipped_cells(text: str) -> tuple[list[tuple[str, str]], int]:
    """Names that are one or two characters short of another name.

    "...Behavioral Social Mirro" against "...Behavioral Social Mirror" is a
    clipped cell. The tolerance has to stay tight: at four characters
    "Social Mirror" starts matching "Social Mirror CTV", which is a different
    line item, not a truncation of this one.
    """
    # A name is rebuilt from lines that wrapped, so it does not appear in the
    # text verbatim - the offset has to come from where its row started.
    at_of: dict[str, int] = {}
    for n, at in line_item_names(text):
        at_of.setdefault(n, at)
    names = sorted(at_of)
    hits = []
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            if not b.startswith(a):
                break
            extra = b[len(a):]
            if len(extra) > 3:
                continue
            # " AI" is a whole word - a different line item. "r" and " -" are
            # what is left when a cell ran out of room.
            if extra.startswith((" ", "\t")) and re.search(r"[A-Za-z0-9]", extra):
                continue
            hits.append((a, b))
    return hits, (at_of.get(hits[0][0], -1) if hits else -1)


# ------------------------------------------------- 3. blank ad screenshots
# The screenshot cells are images, so the text layer says nothing about
# whether one rendered. What it does give is coordinates: the "Screenshot" row
# label, the "Ad Preview" label below it, and the ad names across the top. That
# is enough to cut each cell out of a rendering of the page and look at it.
#
# A cell holding a real ad has thousands of distinct colors. An empty one has
# the table fill and its border - two or three. There is no middle ground, so
# the threshold does not have to be clever.
BLANK_COLORS = 12

_BBOX_WORD = re.compile(
    r'<word xMin="([\d.]+)" yMin="([\d.]+)" xMax="([\d.]+)" yMax="([\d.]+)">(.*?)</word>')
_BBOX_PAGE = re.compile(r'<page width="([\d.]+)" height="([\d.]+)"')


def page_words(path) -> list[list[tuple]]:
    """(xMin, yMin, xMax, yMax, text) per word, per page, in PDF points.

    One pdftotext call for the whole document. Asking page by page cost 317
    subprocesses on the everything-sample.
    """
    import html
    from .. import proc as _proc
    from .parser import _bin
    out = _proc.run([_bin("pdftotext"), "-bbox-layout", str(path), "-"],
                         capture_output=True, text=True, timeout=300).stdout
    pages = []
    for chunk in out.split("<page ")[1:]:
        h = _BBOX_PAGE.search("<page " + chunk)
        words = [(float(a), float(b), float(c), float(d), html.unescape(e))
                 for a, b, c, d, e in _BBOX_WORD.findall(chunk)]
        pages.append({"height": float(h.group(2)) if h else 612.0, "words": words})
    return pages


def _screenshot_cells(page: dict) -> tuple[float, float, list[tuple[float, float, str]]]:
    """(band top, band bottom, [(x start, x end, ad name)]) or (0, 0, [])."""
    words = page["words"]
    label = next((w for w in words if w[4] == "Screenshot"), None)
    if not label:
        return 0.0, 0.0, []
    top = label[1]
    below = [w for w in words if w[1] > top + 4]
    preview = next((w for w in sorted(below, key=lambda w: w[1])
                    if w[4] == "Preview"), None)
    bottom = preview[1] if preview else min(
        [w[1] for w in below] + [page["height"]])

    # The ad names sit on the row immediately above the label. The widget's own
    # title is one row further up, and taking both split the first cell in two.
    above = [w for w in words if w[3] <= top + 1]
    if not above:
        return 0.0, 0.0, []
    row_y = max(w[1] for w in above)
    header = sorted([w for w in above if abs(w[1] - row_y) < 3 and w[0] > label[2]],
                    key=lambda w: w[0])
    if not header:
        return 0.0, 0.0, []

    # Words of one ad name run together; a gap wider than a space starts the
    # next column.
    cols: list[list[tuple]] = [[header[0]]]
    for w in header[1:]:
        if w[0] - cols[-1][-1][2] > 12:
            cols.append([w])
        else:
            cols[-1].append(w)
    cells = []
    for i, grp in enumerate(cols):
        x0 = grp[0][0]
        x1 = cols[i + 1][0][0] if i + 1 < len(cols) else None
        cells.append((x0, x1, " ".join(w[4] for w in grp)))
    return top - 6, bottom - 6, cells


def check_blank_screenshots(ctx) -> list[dict]:
    """Every ad screenshot cell has to actually hold a screenshot."""
    path = ctx.get("path")
    if not path:
        return []
    pages = ctx.get("page_words") or page_words(path)
    ctx["page_words"] = pages

    blank = []
    for n, page in enumerate(pages, start=1):
        if not any(w[4] == "Screenshots" for w in page["words"]):
            continue
        top, bottom, cells = _screenshot_cells(page)
        if not cells or bottom - top < 8:
            continue
        for name, empty in _empty_cells(path, n, top, bottom, cells, page):
            if empty:
                blank.append(f"page {n}: {name}")
    if not blank:
        return []
    return [_f("blank_screenshot", "fail",
               f"{len(blank)} ad screenshot did not render"
               if len(blank) == 1 else
               f"{len(blank)} ad screenshots did not render",
               "Named, but no image in the cell: " + _sample(blank))]


def is_blank(crop) -> bool:
    """A cell holding a real ad has thousands of colors; an empty one has the
    table fill and its border."""
    colors = crop.getcolors(maxcolors=300000) or []
    return len(colors) < BLANK_COLORS


def _empty_cells(path, page_no: int, top: float, bottom: float,
                 cells: list[tuple], page: dict):
    """Yield (ad name, is blank) by looking at the rendered page."""
    from .. import proc as _proc
    import tempfile
    from pathlib import Path as _P
    from PIL import Image
    from .parser import _bin

    dpi = 100
    with tempfile.TemporaryDirectory() as tmp:
        stem = str(_P(tmp) / "p")
        _proc.run([_bin("pdftoppm"), "-f", str(page_no), "-l", str(page_no),
                        "-r", str(dpi), "-png", str(path), stem],
                       capture_output=True, timeout=120)
        hits = sorted(_P(tmp).glob("p-*.png"))
        if not hits:
            return
        im = Image.open(hits[0]).convert("RGB")
        sc = dpi / 72.0
        width_pt = im.width / sc
        for x0, x1, name in cells:
            box = (max(int(x0 * sc) - 4, 0), int(top * sc),
                   int((x1 if x1 else width_pt) * sc) - 6, int(bottom * sc))
            if box[2] - box[0] < 8 or box[3] - box[1] < 8:
                continue
            yield name, is_blank(im.crop(box))


# ------------------------------------------------- 4. conversion names
# A conversion name is what the client reads to know what the ad achieved.
# Blank tells them nothing, and "Retargeting" is the targeting strategy leaking
# into a field meant for the action - it is the buyer's name for the line item,
# pasted where the conversion's name should be.
CONVERSION_HEADER = re.compile(r"^[ \t]*Conversion Name\b.*$", re.M)
RETARGETING = re.compile(r"retargeting", re.I)


def unnamed(name: str) -> bool:
    """Did this row come through with no name?

    A row whose name cell is empty is printed indented to where the numbers
    start, so the first thing on the line is a figure. Reading the column
    geometry off the header is the obvious alternative and does not survive
    contact with these reports - the creative grids indent their names past a
    Preview Image column, so "starts at the header's x" is false for a whole
    class of perfectly good rows.
    """
    return not name.strip() or bool(NUMERIC.match(name.strip()))


def check_conversion_names(ctx) -> list[dict]:
    """Conversions have to be named for what the user did."""
    text = ctx.get("text") or ""
    blank, retg = [], []
    for m in CONVERSION_HEADER.finditer(text):
        where = section_at(text, m.start())
        for name, _at in grid_rows(text, m.end()):
            if unnamed(name):
                blank.append(where)
            elif RETARGETING.search(name) and name not in retg:
                retg.append(name)
    out = []
    if blank:
        out.append(_f("conversion_name_blank", "fail",
                      f"{len(blank)} conversion{'s' if len(blank) > 1 else ''} with no name",
                      "Numbers but no label, on " + _sample(blank)))
    if retg:
        out.append(_f("conversion_name_retargeting", "fail",
                      f"{len(retg)} conversion{'s' if len(retg) > 1 else ''} named "
               f"after a targeting strategy",
                      "Named for how they were reached, not what they did: " + _sample(retg)))
    return out


# ------------------------------------------------- 5. creative names
CREATIVE_GRID = re.compile(r"^[ \t]*(\S[^\n]*Creative Performance[^\n]*)$", re.M)


def creative_rows(text: str) -> list[tuple[str, str]]:
    out = []
    for m in CREATIVE_GRID.finditer(text):
        title = m.group(1).strip()
        for name, _at in grid_rows(text, m.end()):
            out.append((title, name))
    return out


def check_creative_names(ctx) -> list[dict]:
    """Every creative row has to say which creative it is."""
    text = ctx.get("text") or ""
    blank = [t for t, n in creative_rows(text) if unnamed(n)]
    if not blank:
        return []
    return [_f("creative_name_blank", "fail",
               f"{len(blank)} creative{'s' if len(blank) > 1 else ''} with no name",
               "Impressions and clicks but no file name, on " + _sample(blank))]


# --------------------------------------- 6. Social Mirror creatives with sizes
# A Social Mirror ad is one creative rendered into a social feed, so the display
# ad size in the file name is left over from a display build and means nothing
# to the client reading it. Display and Mobile Conquesting names keep theirs.
# Not \b: an underscore is a word character, so "\b" refused to start on the
# "_300x250" the sizes are actually written as.
AD_SIZE = re.compile(r"(?<!\d)\d{2,4}\s*[xX]\s*\d{2,4}(?!\d)")
SOCIAL_MIRROR_GRID = re.compile(r"Social Mirror.*Creative Performance", re.I)

# Curtis asked for the sizes and gets to keep them.
SIZE_EXEMPT = ("curtis",)


def check_social_mirror_sizes(ctx) -> list[dict]:
    """Social Mirror creative names should not carry an ad size."""
    market = (ctx.get("market") or "") + " " + (ctx.get("client") or "")
    if any(x in market.lower() for x in SIZE_EXEMPT):
        return []
    text = ctx.get("text") or ""
    bad = []
    for title, name in creative_rows(text):
        if not SOCIAL_MIRROR_GRID.search(title):
            continue
        if AD_SIZE.search(name) and name not in bad:
            bad.append(name)
    if not bad:
        return []
    return [_f("social_mirror_ad_size", "fail",
               f"{len(bad)} Social Mirror creative"
               f"{'s' if len(bad) > 1 else ''} named with an ad size",
               _sample(bad))]


# ------------------------------------------------- 7. widgets that errored
# TapClicks prints its own error into the widget's space and carries on. The
# page still has a heading and a border, so it does not read as broken until
# somebody looks - which on a 300-page report is nobody.
WIDGET_ERRORS = (
    "requesting data for more assignments than is allowed",
    "within the assignment limit",
    "no data available for the selected",
    "an error occurred while loading this widget",
    "this widget could not be loaded",
)


def check_widget_errors(ctx) -> list[dict]:
    """No widget may print an error where its table should be."""
    text = ctx.get("text") or ""
    low = text.lower()
    hits, first = [], None
    for line in text.split("\n"):
        l = line.strip()
        if not l:
            continue
        ll = l.lower()
        if any(e in ll for e in WIDGET_ERRORS):
            off = low.index(ll) if ll in low else 0
            sec = section_at(text, off)
            frag = f"{sec}: {l.lstrip('0123456789. ')[:120]}"
            if frag not in hits:
                hits.append(frag)
                if first is None:
                    first = (off, sec)
    if not hits:
        return []
    return [_f("widget_error", "fail",
               f"{len(hits)} widget{'s' if len(hits) > 1 else ''} printed an "
               f"error instead of its data",
               "TapClicks wrote the reason into the page instead. Re-pull the "
               "report: " + _sample(hits),
               where=_where(ctx, first[0] if first else -1,
                            first[1] if first else ""))]


# ------------------------------------------- 8. social placement vs its totals
# Three numbers describe the same campaign on the same page: the placement
# grid, the three platform tiles beside the funnel, and the funnel itself. The
# funnel is a picture, so it is out of reach, but the other two are readable.
#
# They do not add up to each other, and that is expected: the grid shows the
# top ten placements and Meta has more than ten, so the grid is always a
# subset. What is NOT expected is the grid exceeding the tile, which can only
# mean a placement is being counted twice.
PLATFORM_TILES = (
    ("facebook", "Facebook News Feed Performance"),
    ("audience", "Audience Network Performance"),
    ("instagram", "Instagram Performance"),
)

PLACEMENT_GRID = re.compile(r"^[ \t]*Social Placement Performance[ \t]*$", re.M)
NUM = re.compile(r"[\d,]+(?:\.\d+)?%?")


def _platform_of(placement: str) -> str:
    p = placement.lower()
    if p.startswith("audience network"):
        return "audience"
    if p.startswith("instagram"):
        return "instagram"
    if p.startswith("facebook"):
        return "facebook"
    return ""                       # Threads, Messenger, Unknown - unassigned


def _row_numbers(text: str, at: int, name: str) -> tuple[float, float] | None:
    """(impressions, clicks) off the row that starts at this offset."""
    eol = text.find("\n", at)
    line = text[at:eol if eol > 0 else len(text)]
    nums = [n for n in NUM.findall(line[len(line) - len(line.lstrip()) + len(name):])
            if not n.endswith("%")]
    vals = []
    for n in nums:
        try:
            vals.append(float(n.replace(",", "")))
        except ValueError:
            pass
    return (vals[0], vals[1]) if len(vals) >= 2 else None


def _tile(text: str, title: str) -> tuple[float, float, float] | None:
    """(impressions, clicks, ctr) off a three-number platform tile."""
    i = text.find(title)
    if i < 0:
        return None
    for line in text[i + len(title):i + 1500].split("\n"):
        nums = NUM.findall(line)
        if len(nums) < 3:
            continue
        pct = [n for n in nums if n.endswith("%")]
        plain = [n for n in nums if not n.endswith("%")]
        if len(plain) >= 2 and pct:
            try:
                return (float(plain[0].replace(",", "")),
                        float(plain[1].replace(",", "")),
                        float(pct[0].rstrip("%").replace(",", "")))
            except ValueError:
                return None
    return None


def _placement_rows(text: str, start: int) -> list[tuple[str, float, float]]:
    """(placement, impressions, clicks) from the Social Placement grid.

    grid_rows cannot read this one: a "Where your ads appear" column of prose
    sits between the name and the numbers, so "every cell after the first is a
    number" is false of every row. Here the rule is the other way round - the
    LAST three cells are the numbers, and everything before them is the name
    and its description.
    """
    rows = []
    here = section_at(text, start)
    for line in text[start:].split("\n"):
        t = line.strip()
        if not t:
            continue
        m = PAGE_HEADER.match(line)
        if m:
            if m.group(1).strip() != here:
                break
            continue
        if _is_chrome(line):
            continue
        cells = [c for c in re.split(r"\s{2,}", t) if c]
        if len(cells) < 3 or len(line) - len(line.lstrip()) > 2:
            continue                # a wrapped description line, not a row
        tail = cells[-3:]
        if not all(NUM.fullmatch(c) for c in tail) or not tail[-1].endswith("%"):
            continue
        try:
            rows.append((cells[0], float(tail[0].replace(",", "")),
                         float(tail[1].replace(",", ""))))
        except ValueError:
            continue
    return rows


def check_social_placement_totals(ctx) -> list[dict]:
    """The placement grid and the platform tiles have to agree with each other.

    Only in one direction. The grid is capped at ten placements and Meta has
    more than ten, so a grid that comes in UNDER its tile is the normal case
    and says nothing. A grid that comes in over it is a placement counted
    twice.
    """
    text = ctx.get("text") or ""
    m = PLACEMENT_GRID.search(text)
    if not m:
        return []

    sums: dict[str, list[float]] = {}
    for name, imps, clicks in _placement_rows(text, m.end()):
        plat = _platform_of(name)
        if not plat:
            continue                # Threads, Messenger, Unknown
        cur = sums.setdefault(plat, [0.0, 0.0])
        cur[0] += imps
        cur[1] += clicks

    out = []
    for key, title in PLATFORM_TILES:
        tile = _tile(text, title)
        if not tile:
            continue
        imps, clicks, ctr = tile

        # The tile has to agree with itself before it can be used as a total.
        if imps and abs((clicks / imps * 100) - ctr) > 0.05:
            out.append(_f("tile_ctr", "fail", f"{title} CTR does not match its "
                          f"own numbers",
                          f"{clicks:,.0f} clicks on {imps:,.0f} impressions is "
                          f"{clicks / imps * 100:.2f}%, but the tile prints "
                          f"{ctr:.2f}%."))

        got = sums.get(key)
        if not got:
            continue
        for label, i, total in (("impressions", 0, imps), ("clicks", 1, clicks)):
            # A COUNT OR TWO OVER IS ROUNDING, NOT DOUBLE COUNTING.
            #
            # "The grid adds up to 4,154 clicks but the total says 4,153" is a
            # finding nobody can act on: the two numbers come from different
            # queries and the tile rounds. A real double count is a whole
            # placement - hundreds - so the slack is the larger of two units
            # or a tenth of a percent.
            slack = max(2.0, total * 0.001)
            if got[i] > total + slack:
                out.append(_f("placement_over_total", "fail",
                              f"Placement rows exceed the {title.split()[0]} total",
                              f"The placement grid adds up to {got[i]:,.0f} "
                              f"{label} for {title.split(' Performance')[0]}, "
                              f"but the total beside it says {total:,.0f}. The "
                              f"grid is a subset of the total, so it cannot be "
                              f"larger - a placement is being counted twice."))
    return out


# ------------------------------------------------- 9. sites clicking too much
# A site with a click-through rate in the double digits is not a good
# placement, it is a click farm - almost always a game or utility app where
# the ad sits under a button people are trying to press. "Slicing Hero: Sword
# Master", 783 impressions and 365 clicks, is what it looks like.
#
# Both the printed rate and the arithmetic are tested, because these grids
# print a CTR that does not always agree with their own two columns.
SITE_GRID = re.compile(
    r"^[ \t]*((?:[A-Z][\w+&'. -]*)?Site and App Performance)[ \t]*$", re.M)
SITE_CTR_CEILING = 5.0

# Below this many impressions the 5% line is arithmetic rather than behavior.
# "BeReal. Your friends for real.: 83 clicks on 1,278 impressions is 6.49%" is
# a placement nobody is going to act on, and one click either way moves it by
# a tenth of a point.
SITE_CTR_MIN_IMPS = 5000

# ...but a small placement can still be unmistakable, and throwing those away
# would have lost the case this check was written for: "Slicing Hero: Sword
# Master", 365 clicks on 783 impressions, 46.62%. No volume of impressions
# makes that a coincidence. So a low-volume row is judged against a rate
# nothing organic reaches instead of being dropped.
SITE_CTR_LOUD = 15.0
SITE_CTR_FLOOR_IMPS = 50


# The last row of a grid absorbs the next widget's title as if it were a
# wrapped name - "minefun.io" comes out as "minefun.io Top CTV Publishers".
# Stripping it here rather than in grid_rows, because a line item name really
# can end in "Performance Max" and losing that would break a different rule.
GLUED_TITLE = re.compile(
    r"\s+(?:Top\s+\d*\s*)?[A-Z][\w+&'. -]*?"
    r"(?:Performance|Publishers|Breakout|Screenshots|Conversions)$")


def site_rows(text: str) -> list[tuple[str, str, float, float, float | None, int]]:
    """(widget, name, impressions, clicks, printed CTR, offset) per site."""
    out = []
    for m in SITE_GRID.finditer(text):
        title = m.group(1).strip()
        for name, at in grid_rows(text, m.end()):
            eol = text.find("\n", at)
            line = text[at:eol if eol > 0 else len(text)]
            cells = [c for c in re.split(r"\s{2,}", line.strip()) if c]
            nums, pct = [], None
            for c in cells[1:]:
                if not NUMERIC.match(c):
                    continue
                try:
                    v = float(c.rstrip("%").replace(",", "").lstrip("$"))
                except ValueError:
                    continue
                if c.endswith("%"):
                    pct = v
                else:
                    nums.append(v)
            if len(nums) >= 2:
                out.append((title, GLUED_TITLE.sub("", name).strip(),
                            nums[0], nums[1], pct, at))
    return out


def check_site_ctr(ctx) -> list[dict]:
    """No site should be clicking at a rate a person would not.

    A double-digit rate on one placement is a click farm, not an audience -
    usually a game or utility app where the ad sits under a button people are
    trying to press. That explanation belongs here, in the code, rather than on
    the report page every month: the people reading it know what it means and
    are there for the numbers.
    """
    text = ctx.get("text") or ""
    bad = []
    for title, name, imps, clicks, printed, at in site_rows(text):
        if imps < SITE_CTR_FLOOR_IMPS:
            continue                # a handful of impressions makes any rate
        real = clicks / imps * 100 if imps else 0.0
        worst = max(real, printed or 0.0)
        ceiling = (SITE_CTR_CEILING if imps >= SITE_CTR_MIN_IMPS
                   else SITE_CTR_LOUD)
        if worst <= ceiling:
            continue
        shown = (f"{name}: {clicks:,.0f} clicks on {imps:,.0f} impressions "
                 f"is {real:.2f}%")
        if printed is not None and abs(printed - real) > 0.05:
            shown += f" (the report prints {printed:.2f}%)"
        bad.append((worst, shown, at, title))
    if not bad:
        return []
    bad.sort(key=lambda b: -b[0])
    return [_f("site_ctr_high", "fail",
               f"{len(bad)} site{'s' if len(bad) > 1 else ''} clicking above "
               f"{SITE_CTR_CEILING:.0f}%",
               _sample([b[1] for b in bad], 10),
               where=_where(ctx, bad[0][2], bad[0][3]))]


# ------------------------------------------- 10. video and audio owe a rate
# Anything a person watches or listens to has a completion rate, and the client
# is paying for the watching. A video or audio section with no completion
# figures anywhere in it is a report that cannot answer the one question those
# products exist to answer.
#
# The test is the word "Completion" appearing anywhere inside that product's own
# ads section, rather than a list of exact widget titles. TapClicks reports it
# five different ways - a widget for Video and Online Audio ("Video Completion
# Performance by Line Item"), a strategy grid for CTV, a column inside the
# creative grid for Social Mirror CTV - and a title list would have to know all
# five and stay right as they change.
COMPLETION_OWED = (
    ("VIDEO ADS", None),
    ("CTV ADS", None),
    ("SOCIAL MIRROR CTV ADS", None),
    ("ONLINE AUDIO ADS", None),
    ("YOUTUBE+ ADS", None),
    ("YOUTUBE TV ADS", None),
    # Amazon Premium Display sits in the same section and has nothing to
    # complete, so this one is only owed when the video half is running.
    ("AMAZON ADS", re.compile(r"Amazon Premium (?:Video|OTT)", re.I)),
)

FRIENDLY_SECTION = {
    "VIDEO ADS": "Video",
    "CTV ADS": "CTV",
    "SOCIAL MIRROR CTV ADS": "Social Mirror CTV",
    "ONLINE AUDIO ADS": "Online Audio",
    "YOUTUBE+ ADS": "YouTube+",
    "YOUTUBE TV ADS": "YouTube TV",
    "AMAZON ADS": "Amazon Premium Video",
}


def section_bodies(text: str) -> dict[str, str]:
    """Everything printed under each page-header section, joined.

    A section runs over many pages and its pages are not contiguous, so this
    collects all of them - "VIDEO ADS - PAGE 1" and "VIDEO ADS - SUMMARY GRIDS"
    are the same section and the completion widget can be on either.
    """
    heads = list(PAGE_HEADER.finditer(text))
    out: dict[str, list[str]] = {}
    for i, m in enumerate(heads):
        end = heads[i + 1].start() if i + 1 < len(heads) else len(text)
        out.setdefault(m.group(1).strip(), []).append(text[m.start():end])
    return {k: "".join(v) for k, v in out.items()}


# Products that owe a completion rate, named the way `detect` names them. Used
# on the older template that prints no "SECTION - PAGE n" banners at all, where
# there are no sections to look inside.
WATCHED_PRODUCTS = ("Video", "CTV", "Social Mirror CTV", "Online Audio", "YouTube")


def check_completion_present(ctx) -> list[dict]:
    """Every video and audio product has to report how much got watched."""
    text = ctx.get("text") or ""
    bodies = section_bodies(text)
    if not any(sec in bodies for sec, _o in COMPLETION_OWED):
        return _completion_without_sections(ctx, text)
    missing, trace = [], []
    for section, only_if in COMPLETION_OWED:
        body = bodies.get(section)
        if body is None:
            continue
        if only_if is not None and not only_if.search(body):
            continue
        name = FRIENDLY_SECTION.get(section, section.title())
        if "Completion" in body:
            trace.append((name, "completion figures found in its section"))
        else:
            missing.append(name)
            trace.append((name, "no completion figures anywhere in its section"))
    if not missing:
        return []
    return [_f("completion_missing", "fail",
               f"{len(missing)} product{'s' if len(missing) > 1 else ''} with no "
               f"completion rate",
               "No completion figures anywhere in the section for: " + _sample(missing) + ".",
               trace)]


def _completion_without_sections(ctx, text: str) -> list[dict]:
    """The same question on a report that prints no section banners.

    Several of these templates exist and they carry no "VIDEO ADS - PAGE 1"
    headers, so there is no section to look inside. All that is left is which
    products the report shows and whether the word appears anywhere, which
    cannot say WHICH product is short - only that something is.
    """
    watched = sorted(set(ctx.get("products") or ()) & set(WATCHED_PRODUCTS))
    if not watched or "Completion" in text:
        return []
    return [_f("completion_missing", "fail",
               f"{len(watched)} product{'s' if len(watched) > 1 else ''} with no "
               f"completion rate",
               "No completion figures anywhere on the report. It runs: "
               + ", ".join(watched) + ".",
               [("Video and audio products on the report", ", ".join(watched)),
                ("The word \"Completion\" anywhere on the report", "no"),
                ("Section banners to look inside", "none - this template "
                 "prints no page-header sections")])]


# --------------------------------------- 11. store visits against their table
# The Visits page says three things about the same fact and they have to agree:
# a headline count of locations that tracked a visit, a table of those
# locations, and a Visits tile. Each is a separate TapClicks widget pulling its
# own query, so they can and do disagree.
STORE_TABLE = re.compile(r"Visits by Store Location", re.I)
LOCATIONS_LABEL = "NUMBER OF LOCATIONS THAT TRACKED A VISIT"
VISITS_LABEL = re.compile(r"^\s*Visits\s{2,}Estimated Visits", re.M)
# "CHEVROLET OF   420 Central   Bloomsburg   PA   17815   1"
STORE_ROW = re.compile(r"\s{2,}[A-Z]{2}\s{2,}\d{5}\s+([\d,]+)\s*$")
INT_ONLY = re.compile(r"^[\d,]+$")


def _number_above(lines: list[str], i: int, want: int = 1) -> list[float] | None:
    """The figures printed above a tile's caption.

    These pages put the number on one line and its label three blank lines
    below, which is the only relationship between them in the text.
    """
    for n in range(i - 1, max(i - 8, -1), -1):
        t = lines[n].strip()
        if not t:
            continue
        cells = [c for c in re.split(r"\s{2,}", t) if c]
        if cells and all(INT_ONLY.match(c) for c in cells) and len(cells) >= want:
            try:
                return [float(c.replace(",", "")) for c in cells]
            except ValueError:
                return None
        return None
    return None


def store_visits(text: str) -> dict | None:
    """(locations claimed, store rows, visits claimed) off the Visits page."""
    m = STORE_TABLE.search(text)
    if not m:
        return None
    lines = text.split("\n")
    # The whole visits block, from the store table to the next page header.
    start = text.count("\n", 0, m.start())
    end = start
    for n in range(start, min(start + 80, len(lines))):
        if PAGE_HEADER.match(lines[n]) and n > start:
            break
        end = n

    rows = []
    for n in range(start, end + 1):
        hit = STORE_ROW.search(lines[n])
        if hit:
            try:
                rows.append(float(hit.group(1).replace(",", "")))
            except ValueError:
                pass

    locations = visits = None
    for n, line in enumerate(lines):
        if LOCATIONS_LABEL in line and abs(n - start) < 40:
            got = _number_above(lines, n)
            if got:
                locations = got[0]
        if VISITS_LABEL.match(line) and abs(n - start) < 60:
            got = _number_above(lines, n, want=1)
            if got:
                visits = got[0]

    clipped = "Grid contains more rows" in "\n".join(lines[start:end + 1])
    return {"locations": locations, "rows": rows, "visits": visits,
            "clipped": clipped}


def check_store_visits(ctx) -> list[dict]:
    """The store visit figures have to agree with the table beneath them."""
    got = store_visits(ctx.get("text") or "")
    if not got or got["clipped"] or not got["rows"]:
        return []

    out = []
    trace = [("Locations that tracked a visit", f"{got['locations']:,.0f}"
              if got["locations"] is not None else "not printed"),
             ("Rows in the store table", f"{len(got['rows'])}"),
             ("Visits", f"{got['visits']:,.0f}"
              if got["visits"] is not None else "not printed"),
             ("Visits in the store table", f"{sum(got['rows']):,.0f}"),
             ("Store rows", ", ".join(f"{v:,.0f}" for v in got["rows"][:10]))]

    if got["locations"] is not None and int(got["locations"]) != len(got["rows"]):
        out.append(_f("store_locations_mismatch", "fail",
                      "Store location count does not match the table",
                      f"The report says {got['locations']:,.0f} location"
                      f"{'s' if got['locations'] != 1 else ''} tracked a visit, "
                      f"and lists {len(got['rows'])}. The table is not clipped, "
                      f"so they are counting different things.", trace))

    if got["visits"] is not None and abs(sum(got["rows"]) - got["visits"]) > 0.5:
        out.append(_f("store_visits_mismatch", "fail",
                      "Visits do not match the store table",
                      f"The Visits figure is {got['visits']:,.0f} and the store "
                      f"table adds up to {sum(got['rows']):,.0f} "
                      f"({sum(got['rows']) - got['visits']:+,.0f}). The table is "
                      f"not clipped, so every visit should be in it.", trace))
    return out
