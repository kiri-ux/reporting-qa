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


def grid_rows(text: str, start: int, stop_at_new_section: bool = True) -> list[tuple[str, int]]:
    """Rows of a grid, as (first cell, offset), with wrapped cells joined.

    TapClicks wraps a long name onto the lines BELOW its own numbers, so a row
    is "a line whose cells after the first are all numeric" and every text-only
    line after it belongs to the row above.

    Ending a grid is the hard part. The page-header section is one boundary,
    but several grids share a section, and without a second one a line item
    table ran on into the publisher list below it and reported "Tubi - Free
    Movies & TV" as a badly named strategy. The other boundary is the next
    widget's title - which cannot be recognised by its wording alone, because
    a wrapped name like "Services/Homeowners/Retargeting Performance" reads
    exactly like one. What separates them is that a real title has a column
    header under it and a wrapped name does not.
    """
    rows: list[tuple[str, int]] = []
    cur: list[str] | None = None
    cur_at = 0
    here = section_at(text, start)

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
        if len(cells) >= 3 and all(NUMERIC.match(c) for c in cells[1:]):
            if cur:
                rows.append((" ".join(cur), cur_at))
            cur, cur_at = [cells[0]], at
            continue
        # No "only once rows have started" guard here: a grid with no rows at
        # all is exactly the case that ran on into the next widget and reported
        # a DOOH publisher list as twenty badly named strategies.
        if _title_of_next_widget(lines, i):
            break
        if cur is not None and len(cells) == 1:
            cur.append(t)
        elif cur is not None and len(cells) >= 3:
            rows.append((" ".join(cur), cur_at))
            cur = None
    if cur:
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
def _f(code, sev, title, detail) -> dict:
    return {"code": code, "severity": sev, "title": title, "detail": detail}


def _sample(items: list[str], n: int = 8) -> str:
    """The first n, then a count. A finding somebody has to act on gets a
    bigger n - a list of names to fix is useless truncated at eight."""
    shown = "; ".join(items[:n])
    return shown + (f"; and {len(items) - n} more" if len(items) > n else "")


# ------------------------------------------------- 1. strategy categorisation
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
        for name, at in grid_rows(text, m.end()):
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
    return out


def check_strategy_categorized(ctx) -> list[dict]:
    """Every strategy line has to name the product it runs."""
    text = ctx.get("text") or ""
    bad, seen = [], set()
    for name, _at in line_item_names(text):
        low = name.lower()
        if any(w in low for w in PRODUCT_WORDS):
            continue
        if name in seen:
            continue
        seen.add(name)
        bad.append(name)
    if not bad:
        return []
    return [_f("strategy_uncategorized", "fail",
               f"{len(bad)} strategy line{'s' if len(bad) > 1 else ''} not "
               f"categorised to a product",
               "TapClicks reads the product out of the line item name, and "
               "these carry no product word - so each one lands on the product "
               "breakout as its own slice instead of joining its product. The "
               "fix is the name, in the order: " + _sample(sorted(bad), 30))]


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

    cut = []
    for line in text.split("\n"):
        if _is_chrome(line):
            continue
        for m in ELLIPSIS.finditer(line):
            frag = line[max(0, m.start() - 40):m.end() + 8].strip()
            if frag not in cut:
                cut.append(frag)
    if cut:
        out.append(_f("text_truncated", "fail",
                      f"{len(cut)} label{'s' if len(cut) > 1 else ''} cut off",
                      "The text runs past the space it was given and ends in "
                      "an ellipsis. Widen the column or turn wrap text on: "
                      + _sample(cut)))

    clipped = _clipped_cells(text)
    if clipped:
        out.append(_f("text_truncated", "fail",
                      f"{len(clipped)} grid cell{'s' if len(clipped) > 1 else ''} "
                      f"cut off mid-word",
                      "The same value appears in full elsewhere on the report, "
                      "so this one lost its last characters to the column "
                      "width: " + _sample([f"{a!r} (should be {b!r})"
                                           for a, b in clipped])))
    return out


def _clipped_cells(text: str) -> list[tuple[str, str]]:
    """Names that are one or two characters short of another name.

    "...Behavioral Social Mirro" against "...Behavioral Social Mirror" is a
    clipped cell. The tolerance has to stay tight: at four characters
    "Social Mirror" starts matching "Social Mirror CTV", which is a different
    line item, not a truncation of this one.
    """
    names = sorted({n for n, _ in line_item_names(text)})
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
    return hits


# ------------------------------------------------- 3. blank ad screenshots
# The screenshot cells are images, so the text layer says nothing about
# whether one rendered. What it does give is coordinates: the "Screenshot" row
# label, the "Ad Preview" label below it, and the ad names across the top. That
# is enough to cut each cell out of a rendering of the page and look at it.
#
# A cell holding a real ad has thousands of distinct colours. An empty one has
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
    import subprocess
    from .parser import _bin
    out = subprocess.run([_bin("pdftotext"), "-bbox-layout", str(path), "-"],
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
               "The cell is there and named, but there is no image in it - the "
               "partner sees an empty blue box: " + _sample(blank))]


def is_blank(crop) -> bool:
    """A cell holding a real ad has thousands of colours; an empty one has the
    table fill and its border."""
    colors = crop.getcolors(maxcolors=300000) or []
    return len(colors) < BLANK_COLORS


def _empty_cells(path, page_no: int, top: float, bottom: float,
                 cells: list[tuple], page: dict):
    """Yield (ad name, is blank) by looking at the rendered page."""
    import subprocess
    import tempfile
    from pathlib import Path as _P
    from PIL import Image
    from .parser import _bin

    dpi = 100
    with tempfile.TemporaryDirectory() as tmp:
        stem = str(_P(tmp) / "p")
        subprocess.run([_bin("pdftoppm"), "-f", str(page_no), "-l", str(page_no),
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
                      "The row has numbers but no label, so nobody reading the "
                      "report can tell what converted. On " + _sample(blank)))
    if retg:
        out.append(_f("conversion_name_retargeting", "fail",
                      f"{len(retg)} conversion{'s' if len(retg) > 1 else ''} named "
               f"after a targeting strategy",
                      "A conversion is named for what the user did, not how "
                      "they were reached - \"Retargeting\" here is the line "
                      "item's name in the wrong field: " + _sample(retg)))
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
               "The row carries impressions and clicks but no file name, so "
               "there is no way to say which ad it was. On " + _sample(blank))]


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
               "A Social Mirror ad renders into a social feed, so a display "
               "size in the name is left over from a display build and means "
               "nothing to the client: " + _sample(bad))]


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
    hits = []
    for line in text.split("\n"):
        l = line.strip()
        if not l:
            continue
        ll = l.lower()
        if any(e in ll for e in WIDGET_ERRORS):
            where = section_at(text, low.index(ll) if ll in low else 0)
            frag = f"{where}: {l.lstrip('0123456789. ')[:120]}"
            if frag not in hits:
                hits.append(frag)
    if not hits:
        return []
    return [_f("widget_error", "fail",
               f"{len(hits)} widget{'s' if len(hits) > 1 else ''} printed an "
               f"error instead of its data",
               "TapClicks could not build the widget and wrote the reason into "
               "the page. Re-pull the report: " + _sample(hits))]


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
            if got[i] > total + 0.5:
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


# The last row of a grid absorbs the next widget's title as if it were a
# wrapped name - "minefun.io" comes out as "minefun.io Top CTV Publishers".
# Stripping it here rather than in grid_rows, because a line item name really
# can end in "Performance Max" and losing that would break a different rule.
GLUED_TITLE = re.compile(
    r"\s+(?:Top\s+\d*\s*)?[A-Z][\w+&'. -]*?"
    r"(?:Performance|Publishers|Breakout|Screenshots|Conversions)$")


def site_rows(text: str) -> list[tuple[str, str, float, float, float | None]]:
    """(widget, name, impressions, clicks, printed CTR) per site."""
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
                            nums[0], nums[1], pct))
    return out


def check_site_ctr(ctx) -> list[dict]:
    """No site should be clicking at a rate a person would not."""
    text = ctx.get("text") or ""
    bad = []
    for _title, name, imps, clicks, printed in site_rows(text):
        if imps < 50:
            continue                # a handful of impressions makes any rate
        real = clicks / imps * 100 if imps else 0.0
        worst = max(real, printed or 0.0)
        if worst <= SITE_CTR_CEILING:
            continue
        if printed is not None and abs(printed - real) > 0.05:
            shown = (f"{name}: {clicks:,.0f} clicks on {imps:,.0f} impressions "
                     f"is {real:.2f}% (the report prints {printed:.2f}%)")
        else:
            shown = (f"{name}: {clicks:,.0f} clicks on {imps:,.0f} impressions "
                     f"is {real:.2f}%")
        bad.append((worst, shown))
    if not bad:
        return []
    bad.sort(reverse=True)
    return [_f("site_ctr_high", "fail",
               f"{len(bad)} site{'s' if len(bad) > 1 else ''} clicking above "
               f"{SITE_CTR_CEILING:.0f}%",
               "A double-digit rate on one placement is a click farm, not an "
               "audience - usually a game or utility app where the ad sits "
               "under a button people are trying to press: "
               + _sample([s for _w, s in bad], 10))]
