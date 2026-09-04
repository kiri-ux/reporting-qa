"""The check suite. Each rule returns zero or more findings.

A finding is a dict: code, severity (fail|warn|info), title, detail.
Severity drives the dashboard color and whether anyone gets pinged.
"""
from __future__ import annotations

import datetime as dt
import re
from pathlib import Path

from ..config import settings
from .parser import (as_number, date_range, SKIP_LINE, Table, extract_tables, headline,
                     meta_from_filename, meta_from_text, page_count, page_ink_pct,
                     pdf_text, tokens)
from .products import NOT_IN_MONTHLY_REPORT, detect as detect_products
from .quality import (check_blank_screenshots, check_conversion_names,
                      check_creative_names, check_social_mirror_sizes,
                      check_strategy_categorized, check_truncated_text,
                      check_completion_present, check_site_ctr,
                      check_store_visits, STORE_TABLE,
                      check_social_placement_totals, COMPLETION_OWED,
                      section_bodies,
                      check_widget_errors, check_page_banners, SITE_GRID,
                      widget_at,
                      line_item_names, creative_rows, PLACEMENT_GRID,
                      _where,
                      CONVERSION_HEADER, SOCIAL_MIRROR_GRID)

LINE_ITEM = re.compile(r"Line Item Performance$")
CREATIVE = re.compile(r"(Creative Performance|Creative Group Performance)$")
DEVICE_TITLES = ("Device Performance", "Device")


# Private-use glyphs are TapClicks' icon font leaking into the text layer, and
# a device name arrives with its whole Description column glued on. Neither
# belongs in a panel somebody is reading to settle an argument.
_PUA = re.compile(r"[\ue000-\uf8ff]")


def _clean(v: str) -> str:
    v = _PUA.sub("", str(v))
    return re.sub(r"\s{2,}", " ", v).strip()


def _short_name(name: str, limit: int = 28) -> str:
    """A row label without the description that followed it into the cell.

    The device grid puts a sentence of explanation in the next column and the
    layout dump glues it on, so "Desktop" arrives as "Desktop A personal
    computing device that remains stationary at one location." A known device
    name is matched outright; everything else is cut at the first sentence.
    """
    n = _clean(name)
    low = n.lower()
    for d in sorted(KNOWN_DEVICES, key=len, reverse=True):
        if low.startswith(d):
            return n[:len(d)]
    n = n.split(".")[0].strip()
    return n if len(n) <= limit else n[:limit - 1].rstrip() + "\u2026"


def _f(code, sev, title, detail, trace=None, where=""):
    """A finding, optionally carrying the arithmetic behind it.

    `trace` is a list of (label, value) pairs - the numbers the rule actually
    used and where they came from. The viewer hides it behind an Investigate
    button, because the question a disputed finding always raises is "where did
    that figure come from" and the answer should not require reading the code.
    """
    out = {"code": code, "severity": sev, "title": title, "detail": detail}
    if where:
        out["where"] = _clean(where)
    if trace:
        out["trace"] = [{"label": _clean(l), "value": _clean(v)} for l, v in trace]
    return out


# A line item name ends with its product, e.g. "... - Geo-Retargeting Mobile",
# "... - Keyword PPC", "... Performance Max". Mobile Conquesting line items end
# in "Mobile", so match on the tail rather than searching the whole string.
PRODUCT_TAIL = {
    "Mobile Conquesting": r"\bMobile$",
    "PPC": r"\bPPC$",
    "YouTube": r"\bYouTube\b",
    "LinkedIn": r"\bLinkedIn\b",
    "Performance Max": r"\bPerformance Max$",
}


def is_device_excluded(line_item_name: str, excluded: set[str]) -> bool:
    name = (line_item_name or "").strip()
    for product in excluded:
        pattern = PRODUCT_TAIL.get(product, r"\b" + re.escape(product) + r"\b")
        if re.search(pattern, name, re.I):
            return True
    return False


# ---------------------------------------------------------------- reconciliation
CTV_RE = re.compile(r"\bCTV\b", re.I)


def _ctv_totals(ctx) -> tuple[float, float]:
    """Impressions and clicks sitting on CTV line items."""
    imps = clicks = 0.0
    for t in ctx["tables"]:
        if not LINE_ITEM.search(t.title or ""):
            continue
        for name, v in t.body:
            if CTV_RE.search(name or ""):
                imps += v.get("Impressions", 0.0)
                clicks += v.get("Clicks", 0.0)
    return imps, clicks


# What the top-line CTR leaves out. The report says so itself, in the footnote
# under the tiles: "CTR and Total Engagement Rate do not include impressions or
# view-throughs from CTV, YouTube, or PMax campaigns." The TapClicks widget
# behind it excludes strategy names matching *CTV*, *OTT*, *Performance Max*,
# *PMax* and *YouTube*.
#
# Both halves of the fraction are filtered, which is what the old handling got
# wrong: it took CTV impressions out of the denominator and left every click in
# the numerator, so it only ever recognized the case by accident. On a report
# running CTV the honest arithmetic is filtered clicks over filtered
# impressions, and the line item grid is where that breakdown lives.
CTR_EXCLUDED = re.compile(r"\bCTV\b|\bOTT\b|YouTube|Performance Max|\bPMax\b", re.I)

# The CLICKS tile is a different filter, and the footnote does not describe it.
# Service One settled it: line items 3,111, tile 3,008, difference 103 - and
# the CTV and OTT lines carry exactly 103 (61 + 41 + 1 + 0). The YouTube+ line
# carries the other 8 and is plainly IN the tile, so YouTube must not be taken
# out of this side of the arithmetic even though the CTR footnote excludes it.
CLICKS_EXCLUDED = re.compile(r"\bCTV\b|\bOTT\b", re.I)

CTR_FOOTNOTE = "do not include impressions or view-throughs"


def _ctr_basis(ctx) -> tuple[float, float, float, float] | None:
    """(kept impressions, kept clicks, all impressions, all clicks).

    None when the line item grid is not complete enough to divide by - if its
    own total is well short of the headline, a subtotal off it is not a number
    anybody should act on.
    """
    from .quality import line_item_totals
    rows = line_item_totals(ctx.get("text") or "")
    if not rows:
        return None
    all_i = sum(r[1] for r in rows)
    all_c = sum(r[2] for r in rows)
    imps = ctx.get("imps") or 0
    if not all_i or not imps or abs(all_i - imps) / imps > 0.02:
        return None                 # the grid does not describe this campaign
    keep = [r for r in rows if not CTR_EXCLUDED.search(r[0])]
    if not keep:
        # Nothing survives the footnote's exclusions, which cannot be what the
        # tile did - it printed a rate, and a rate over nothing is not a
        # number. The names have been read wrong; abstaining is the only
        # honest answer, and it is what the caller does with None.
        return None
    return sum(r[1] for r in keep), sum(r[2] for r in keep), all_i, all_c


# EVERY FINDING SAYS WHERE TO LOOK.
#
# The three tiles and the date range are on page one of every report, so these
# do not need finding in the text - they are simply where they always are.
TILE_CTR = "p1 · Click-Through Rate"
TILE_TOP = "p1 · Impressions and Clicks"
COVER = "p1 · cover page"
DATE_RANGE = "p1 · Date range"


def _grid_spot(ctx, title: str = "Line Item Performance") -> str:
    """Page and widget for the grid a top-line finding sends you to."""
    text = ctx.get("text") or ""
    at = text.find(title)
    return _where(ctx, at, title) if at >= 0 else TILE_TOP


def check_headline_ctr(ctx) -> list[dict]:
    imps, clicks, ctr = ctx["imps"], ctx["clicks"], ctx["ctr"]
    if not imps or clicks is None or ctr is None:
        return []
    plain = clicks / imps * 100
    if abs(plain - ctr) <= 0.011:
        return []

    basis = _ctr_basis(ctx)
    excluded_here = CTR_EXCLUDED.search(ctx.get("text") or "")
    trace = [("Top-line impressions", f"{imps:,.0f}"),
             ("Top-line clicks", f"{clicks:,.0f}"),
             ("Stated CTR", f"{ctr:.2f}%"),
             ("Clicks / impressions", f"{plain:.3f}%")]
    if basis:
        kept_i, kept_c, all_i, all_c = basis
        trace += [
            ("Line items on the report", f"{all_i:,.0f} impressions, {all_c:,.0f} clicks"),
            ("Left out of the CTR tile", "strategy names matching CTV, OTT, "
                                         "YouTube, Performance Max, PMax"),
            ("After leaving those out", f"{kept_i:,.0f} impressions, {kept_c:,.0f} clicks"),
            ("Filtered clicks / filtered impressions",
             f"{kept_c / kept_i * 100:.3f}%" if kept_i else "n/a")]
        if kept_i and abs(kept_c / kept_i * 100 - ctr) <= 0.011:
            return [_f("ctr_excludes_products", "info",
                       "CTR is calculated with CTV, YouTube and PMax left out",
                       f"Stated {ctr:.2f}%. Against all {imps:,.0f} impressions "
                       f"that would be {plain:.3f}%, but the tile leaves CTV, "
                       f"OTT, YouTube and Performance Max out of both halves - "
                       f"{kept_c:,.0f} clicks over {kept_i:,.0f} impressions is "
                       f"{kept_c / kept_i * 100:.3f}%, which matches. The "
                       f"report's own footnote says so. Expected.", trace, where=TILE_CTR)]
        # Guarded. Every line item on the report can be one the footnote
        # excludes - an Amazon CTV plus Amazon Video buy is exactly that - and
        # dividing by the empty remainder turned a check into a crash, which
        # reached the screen as "check_headline_ctr could not run".
        filtered = f"{kept_c / kept_i * 100:.3f}%" if kept_i else "nothing left to divide"
        return [_f("headline_ctr", "fail",
                   "Top-line CTR does not match its own numbers",
                   f"Report states {ctr:.2f}%. All {clicks:,.0f} clicks over all "
                   f"{imps:,.0f} impressions is {plain:.3f}%. Leaving out CTV, "
                   f"OTT, YouTube and Performance Max as the footnote says, "
                   f"{kept_c:,.0f} over {kept_i:,.0f} is {filtered}. "
                   f"Neither is the stated rate.", trace, where=TILE_CTR)]

    if excluded_here:
        # The products the footnote excludes are on this report, and the line
        # item grid cannot say how much of the campaign they are. Claiming the
        # rate is wrong would be guessing.
        #
        # The names as the parser read them go in the trace. When this rule is
        # wrong it is because a wrapped line item name was assembled wrong, and
        # that is invisible from the numbers alone - the report looks fine and
        # the finding looks reasoned.
        from .quality import line_item_totals
        seen = line_item_totals(ctx.get("text") or "")
        if seen:
            trace = trace + [("Names as read",
                              "; ".join(_short_name(n, 70) for n, _i, _c in seen))]
        return [_f("ctr_unverifiable", "info",
                   "CTR could not be checked against its own numbers",
                   f"Stated {ctr:.2f}%, against {plain:.3f}% for all "
                   f"{clicks:,.0f} clicks over all {imps:,.0f} impressions. The "
                   f"tile leaves CTV, OTT, YouTube and Performance Max out of "
                   f"both halves and this report runs at least one of them, so "
                   f"the two are not comparable - and the line items left after "
                   f"that do not give a rate either.", trace, where=TILE_CTR)]

    return [_f("headline_ctr", "fail", "Top-line CTR does not match its own numbers",
               f"Report states {ctr:.2f}%. {clicks:,.0f} clicks / {imps:,.0f} impressions "
               f"= {plain:.3f}%.", trace, where=TILE_CTR)]


def check_line_items(ctx) -> list[dict]:
    """The line items have to add up to the campaign they describe.

    Read off the summary grid rather than the strict table parser. The parser
    stops when the column alignment shifts, which on a long report is after
    about seventeen rows - so this check was comparing a page of line items
    against a whole campaign and failing every large report for it.
    """
    from .quality import line_item_totals
    imps, clicks = ctx["imps"], ctx["clicks"]
    if not imps:
        return []
    rows = line_item_totals(ctx.get("text") or "")
    if not rows:
        return []
    si = sum(r[1] for r in rows)
    sc = sum(r[2] for r in rows)
    biggest = sorted(rows, key=lambda r: -r[1])[:6]
    out = []

    # A tolerance, because a top-ten grid is not the whole campaign and a
    # rounding difference is not a finding. Half a percent, or two on a tiny
    # report, whichever is larger.
    slack = max(2.0, imps * 0.005)
    if abs(si - imps) > slack:
        out.append(_f("line_items_impressions", "fail",
                      "Line items do not sum to the top line",
                      f"Line items total {si:,.0f} impressions against a stated "
                      f"{imps:,.0f} ({si - imps:+,.0f}, "
                      f"{(si - imps) / imps * 100:+.2f}%).",
                      [("Top-line impressions", f"{imps:,.0f}"),
                       ("Line items counted", f"{len(rows)}"),
                       ("Their impressions", f"{si:,.0f}"),
                       ("Difference", f"{si - imps:+,.0f} impressions"),
                       ("Largest line items",
                        "; ".join(f"{_short_name(n, 48)}: {i:,.0f}"
                                  for n, i, _c in biggest))],
                      where=_grid_spot(ctx)))

    if clicks is None:
        return out

    gap = sc - clicks
    # The Clicks tile is filtered on some templates and not on others: it can
    # leave out CTV, OTT, YouTube and PMax. So the question is not whether the
    # two numbers differ but how much of the difference those products explain,
    # and whether what is left over is worth anybody's time.
    excluded = [r for r in rows if CLICKS_EXCLUDED.search(r[0])]

    # AN EXCLUSION THAT SWALLOWS THE WHOLE REPORT IS NOT AN EXCLUSION.
    #
    # It is the sound of names having been read wrong. A tile that filters out
    # every line item would print nothing, and it printed 84 - so whatever this
    # classification is, it is not what the tile did.
    #
    # Failing the report on the strength of it is the worst of both: a correct
    # report marked wrong, on reasoning that is visibly broken the moment you
    # open Investigate. Better to say the check could not run.
    if excluded and len(excluded) == len(rows):
        out.append(_f("clicks_unverifiable", "info",
                      "The clicks tile could not be checked against the line items",
                      f"Every line item on this report reads as CTV or OTT, and a "
                      f"tile filtering all of them out would print nothing rather "
                      f"than {clicks:,.0f}. The names are being read wrong, so no "
                      f"claim is made either way. Investigate has the names as "
                      f"they were read.",
                      [("Top-line clicks", f"{clicks:,.0f}"),
                       ("Line items counted", f"{len(rows)}"),
                       ("Their clicks", f"{sc:,.0f}"),
                       ("Names as read",
                        "; ".join(_short_name(n, 70) for n, _i, _c in rows))],
                      where=_grid_spot(ctx)))
        return out

    excl = sum(r[2] for r in excluded)
    unexplained = gap - excl
    ctrace = [("Top-line clicks", f"{clicks:,.0f}"),
              ("Line items counted", f"{len(rows)}"),
              ("Their clicks", f"{sc:,.0f}"),
              ("Difference", f"{gap:+,.0f} clicks"),
              ("Clicks on CTV and OTT line items", f"{excl:,.0f}"),
              # Named, not just totaled. A remainder of eight clicks is only
              # findable if you can see which lines were taken out and for how
              # much - the total on its own says "trust me".
              ("Which lines those are",
               "; ".join(f"{_short_name(n, 44)}: {c:,.0f}"
                         for n, _i, c in sorted(excluded, key=lambda r: -r[2]))
               or "none"),
              ("Left unexplained", f"{unexplained:+,.0f} clicks")]

    # WHICH LINES THE TILE EXCLUDES IS A JUDGMENT, NOT A FACT.
    #
    # "Retargeting Social Mirror OTT" is a Social Mirror line with an OTT
    # placement, and whether the Clicks tile leaves it out is not something the
    # PDF says. So the remainder after subtracting the CTV and OTT lines is
    # never going to land on nought, and on WVU Parkersburg it came out at 52
    # clicks against a tile of 39,566 - a warning, every month, about one
    # eighth of one percent.
    #
    # Under a percent of the tile is that judgment being slightly off, and
    # nothing anybody can act on. Five percent is a line item missing from the
    # pull.
    noise = max(25.0, clicks * 0.01)
    material = max(100.0, clicks * 0.05)
    if abs(gap) <= max(2.0, clicks * 0.005):
        return out
    if unexplained != 0 and abs(unexplained) <= noise:
        return out
    if unexplained == 0:
        out.append(_f("clicks_exclude_products", "info",
                      "The top-line clicks leave CTV and OTT out",
                      f"Line items total {sc:,.0f} clicks against a stated "
                      f"{clicks:,.0f}. The CTV and OTT line items carry {excl:,.0f} "
                      f"clicks, which that tile excludes and which accounts "
                      f"for all {abs(gap):,.0f} of the difference. Expected.",
                      ctrace, where=_grid_spot(ctx)))
    elif abs(unexplained) <= material:
        # Small, but not nothing. Saying "expected" would be a claim the
        # arithmetic does not support, and the remainder is worth a look even
        # when it is too small to hold a report up.
        out.append(_f("clicks_part_explained", "warn",
                      f"{abs(unexplained):,.0f} click"
                      f"{'s' if abs(unexplained) != 1 else ''} unaccounted for",
                      f"Line items total {sc:,.0f} clicks against a stated "
                      f"{clicks:,.0f}. The CTV and OTT lines carry {excl:,.0f}, "
                      f"which that tile excludes - that leaves "
                      f"{abs(unexplained):,.0f} of the {abs(gap):,.0f} "
                      f"difference.", ctrace, where=_grid_spot(ctx)))
    else:
        out.append(_f("line_items_clicks", "fail",
                      "Line item clicks do not sum to the top line",
                      f"Line items total {sc:,.0f} clicks against a stated "
                      f"{clicks:,.0f} ({gap:+,.0f}). The CTV and OTT lines carry "
                      f"{excl:,.0f}, which that tile can exclude. "
                      f"{abs(unexplained):,.0f} clicks unaccounted for.", ctrace,
                      where=_grid_spot(ctx)))
    return out


def check_creative(ctx) -> list[dict]:
    imps = ctx["imps"]
    tables = [t for t in ctx["tables"] if CREATIVE.search(t.title or "")]
    if not tables or not imps:
        return []
    si = sum(t.total("Impressions") for t in tables)
    if si > imps * 1.001:
        spot = _where(ctx, ctx["text"].find(tables[0].title or ""),
                      tables[0].title or "Creative Performance")
        return [_f("creative_over_top", "fail",
                   "Creative table claims more than the campaign delivered",
                   f"Creative tables total {si:,.0f} impressions against a stated {imps:,.0f} "
                   f"(+{si - imps:,.0f}). Usually a de-duplication problem upstream.",
                   where=spot)]
    if si < imps * 0.999:
        spot = _where(ctx, ctx["text"].find(tables[0].title or ""),
                      tables[0].title or "Creative Performance")
        return [_f("creative_under_top", "info",
                   "Creative tables cover part of the campaign",
                   f"Creative tables total {si:,.0f} against {imps:,.0f}. Normal when a channel "
                   f"(CTV, Performance Max) reports completions or events rather than clicks.",
                   where=spot)]
    return []


def check_device(ctx) -> list[dict]:
    """The device breakout cannot describe more impressions than were served.

    It used to be compared against a "device-eligible" subtotal - the campaign
    with Mobile Conquesting, PPC, YouTube, LinkedIn and Performance Max taken
    out, on the belief that the device widget leaves those out. Some reports
    do not: Credit King's device table sums to 193,744 against a headline of
    193,746, the whole campaign, and got failed for exceeding a subset it was
    never confined to.

    So the ceiling is the headline, which is a fact, and the eligible subtotal
    is only used to notice a breakout that has come in far too LOW.
    """
    dev = None
    for t in ctx["tables"]:
        if (t.title or "").strip() in DEVICE_TITLES or (t.title or "").endswith("Device Performance"):
            dev = t
            break
    if dev is None or not ctx["imps"]:
        return []
    device_total = dev.total("Impressions")
    imps = ctx["imps"]
    # Where to look. A device breakout is one widget on one page of forty, and
    # "the device breakout is wrong" is true and unhelpful until you know which.
    title = (dev.title or "Device Performance").strip()
    where = _where(ctx, (ctx.get("text") or "").find(title), title)

    excluded = settings.excluded_products
    eligible = 0.0
    li = [t for t in ctx["tables"] if LINE_ITEM.search(t.title or "")]
    for t in li:
        for name, v in t.body:
            if not is_device_excluded(name, excluded):
                eligible += v.get("Impressions", 0.0)

    trace = [("Device breakout total", f"{device_total:,.0f} impressions"),
             ("Top-line impressions", f"{imps:,.0f}"),
             ("Device rows", ", ".join(f"{_short_name(n)}: {v.get('Impressions', 0):,.0f}"
                                       for n, v in dev.body[:8]) or "none")]
    if eligible > 0:
        trace.append(("Line items outside " + ", ".join(sorted(excluded)),
                      f"{eligible:,.0f} impressions"))

    over = device_total - imps
    if imps and over / imps * 100 > 1:
        return [_f("device_over", "fail",
                   "Device breakout exceeds what was served",
                   f"Device totals {device_total:,.0f} against a top line of "
                   f"{imps:,.0f} (+{over / imps * 100:.1f}%). A breakdown cannot "
                   f"describe more impressions than the campaign served.", trace,
                   where=where)]

    if eligible > 0:
        pct = (device_total - eligible) / eligible * 100
        if pct < -settings.device_under_tolerance_pct:
            return [_f("device_under", "warn",
                       "Device breakout well under the eligible total",
                       f"Device totals {device_total:,.0f} against {eligible:,.0f} "
                       f"eligible ({pct:.1f}%). Unknown-device filtering does not "
                       f"usually account for this.", trace, where=where)]
    return []


def check_row_math(ctx) -> list[dict]:
    out = []
    for t in ctx["tables"]:
        for name, v in t.body:
            imps, clicks, ctr = v.get("Impressions"), v.get("Clicks"), v.get("CTR")
            if not imps or clicks is None or ctr is None:
                continue
            expected = clicks / imps * 100
            if abs(expected - ctr) > max(0.011, expected * 0.03):
                at = ctx["text"].find(name[:40]) if name else -1
                out.append(_f("row_ctr", "warn", "Row CTR does not match its own numbers",
                              f"{t.title or 'table'} / \"{name[:60]}\": shows {ctr:.2f}%, "
                              f"{clicks:.0f}/{imps:.0f} = {expected:.3f}%.",
                              where=_where(ctx, at, t.title or "")))
    return out[:5]


RATE_RE = re.compile(r"\b(\d{2,4}\.\d{2})%")


def check_rate_ceiling(ctx) -> list[dict]:
    text = ctx["text"]
    bad, first = set(), -1
    for m in RATE_RE.finditer(text):
        if float(m.group(1)) > 100:
            bad.add(m.group(1))
            if first < 0:
                first = m.start()
    if not bad:
        return []
    return [_f("rate_over_100", "warn", "Rate printed above 100%",
               "Values found: " + ", ".join(f"{b}%" for b in sorted(bad)[:5]) +
               ". Completion rates and CTR cannot exceed 100%.",
               where=_where(ctx, first, widget_at(text, first)))]


# ---------------------------------------------------------------- previews
def check_thumbnails(ctx) -> list[dict]:
    """ONE FINDING PER WIDGET, NOT ONE PER REPORT.

    This counted every "Thumbnail not available" in the document and then
    pinned the total to wherever the first one happened to be. Eastern Floor
    Covering's Social Mirror Creative Performance table has two, and the
    finding sitting on that table said three - the third was in another widget
    on another page. Read against the page it names it was simply wrong, and a
    number you cannot check against what is in front of you is worse than no
    number at all.
    """
    text = ctx["text"]
    counts: dict[str, int] = {}
    nouns: dict[str, str] = {}
    for m in re.finditer("Thumbnail not available", text):
        where = _where(ctx, m.start(), widget_at(text, m.start()))
        counts[where] = counts.get(where, 0) + 1

    # AND THE ONES THAT PRINT NOTHING AT ALL. Wine and Design Newport News' CTV
    # grid has five creatives: four say "Thumbnail not available" and the fifth
    # says nothing, which is the same failure with nothing to count. The text
    # cannot tell that cell from one holding a working thumbnail - an image is
    # empty in pdftotext exactly like an empty cell is - so this one is decided
    # on the rendered pixels.
    try:
        from .quality import blank_previews, page_words
        path = ctx.get("path")
        if path:
            pages = ctx.get("page_words") or page_words(path)
            ctx["page_words"] = pages
            for page_no, title, noun in blank_previews(path, pages):
                where = f"p{page_no} · {title}" if title else f"p{page_no}"
                counts[where] = counts.get(where, 0) + 1
                nouns[where] = noun
    except Exception:      # a preview count is never worth failing the run
        pass

    out = []
    for where, n in counts.items():
        noun = nouns.get(where, "creative preview")
        out.append(_f("missing_thumbnail", "warn",
                      f"{n} {noun}{'s' if n > 1 else ''} did not render",
                      'The cell prints "Thumbnail not available", a broken '
                      'image, or nothing at all, in place of the picture.',
                      where=where))
    return out


# ---------------------------------------------------------------- empty widgets
def check_blank_pages(ctx) -> list[dict]:
    path, pages = ctx["path"], ctx["pages"]
    # One pdftotext call for the whole document rather than one per page. On a
    # forty-one page report that was forty-one subprocesses and most of the
    # wait after uploading a corrected PDF.
    from .parser import pdf_pages
    per_page = ctx.get("page_text")
    if per_page is None:
        per_page = ctx["page_text"] = pdf_pages(path)
    hits = []
    for pg in range(1, min(pages, len(per_page)) + 1):
        txt = per_page[pg - 1]
        body = [l.strip() for l in txt.split("\n") if l.strip() and not SKIP_LINE.search(l)]
        if not body:
            continue
        if sum(c.isdigit() for c in " ".join(body)) > 4:
            continue
        if page_ink_pct(path, pg) < 1.0:
            hits.append((pg, " ".join(body)[:70]))
    if not hits:
        return []
    detail = "; ".join(f"page {p} of {pages}: {t}" for p, t in hits[:4])
    return [_f("blank_widget_page", "warn",
               f"{len(hits)} page{'s' if len(hits) > 1 else ''} with a widget but no data",
               detail, where=f"p{hits[0][0]}")]


# ---------------------------------------------------------------- geo-fencing
# A geo-fence row prints its state and ZIP - "  FL   32405 " - which is what
# tells a data row from the heading, the column header and the page furniture
# around it.
GEOFENCE_ROW = re.compile(r"\s{2,}[A-Z]{2}\s{2,}\d{5}\s")


def _geofence_block(text: str) -> tuple[str | None, list[str]]:
    """(header line, data rows) for the geo-fencing table, or (None, [])."""
    i = text.find("Geo-Fencing Performance")
    if i < 0:
        return None, []
    seg = []
    for n, line in enumerate(text[i:].split("\n")):
        if n and line.strip() and not line.startswith((" ", "\t")) and "Performance" in line:
            break
        seg.append(line)
        if n > 220:
            break
    hdr = next((l for l in seg if "Business Name" in l and "Address" in l), None)
    if not hdr:
        return None, []
    return hdr, [l for l in seg if GEOFENCE_ROW.search(l)]


def _geofence_rows(text: str) -> list[str]:
    return _geofence_block(text)[1]


def check_geofence_names(ctx) -> list[dict]:
    hdr, rows = _geofence_block(ctx["text"])
    if not hdr or not rows:
        return []
    addr = hdr.index("Address")
    blank = [l for l in rows if (len(l) - len(l.lstrip())) >= addr - 2]
    if not blank:
        return []
    return [_f("geofence_no_business_name", "info",
               "Geo-fence rows have no business name",
               f"{len(blank)} of {len(rows)} rows show an address with no business name, "
               f"latitude or longitude. Expected if the fence was built from an address list.",
               where=_where(ctx, ctx["text"].find("Geo-Fencing Performance"),
                            "Geo-Fencing Performance"))]


GEOFENCE_LINE = re.compile(r"\bgeo[- ]?fenc", re.I)
GEOFENCE_WIDGET = "Geo-Fencing Performance"

# ONLY MOBILE CONQUESTING OWES THE FENCE LIST.
#
# Geo-fencing is a targeting method, and most of the catalog can be bought that
# way - "Bloomsburg Theater Ensemble - Geo-Fencing Social Mirror" is a Social
# Mirror line. The Geo-Fencing Performance widget belongs to Mobile
# Conquesting, its Event variant and its Political variant, and to nothing
# else, so every geo-fenced Social Mirror line on the board was failing for a
# widget it was never going to carry.
GEOFENCE_PRODUCT = re.compile(r"\bmobile\b|\bconquest", re.I)


# ---------------------------------------------------------------- rogue CTV
# A widget that only makes sense if CTV is running.
# NOT ANCHORED TO ITS OWN LINE. Page one prints three tile headings side by
# side - "Your Product Breakout by Impressions   CTV Completion Rate   CTV Cost
# Per Completed View" - so a whole-line match never fired on the layout this
# tile actually appears in.
CTV_WIDGET = re.compile(r"\bCTV (?:Completion Rate|Cost Per Completed View)\b")
# What a CTV line item is called. OTT is the old name for the same thing.
CTV_LINE = re.compile(r"\bctv\b|\bott\b", re.I)


def check_rogue_ctv(ctx) -> list[dict]:
    """A CTV tile on a report with no CTV on it.

    The tile is on the template, so it prints whether or not the client bought
    CTV - and it prints a completion rate, which on a client running no CTV is
    a percentage of nothing sitting on page one under the client's name.
    """
    from .quality import line_item_totals

    text = ctx.get("text") or ""
    m = CTV_WIDGET.search(text)
    if not m:
        return []
    if any(CTV_LINE.search(n) for n, _i, _c in line_item_totals(text)):
        return []
    want = ctx.get("expected_products")
    if want and any(CTV_LINE.search(p) for p in want):
        return []          # the order says CTV even if the grid does not
    return [_f("ctv_widget_no_ctv", "fail",
               "CTV tile on a report with no CTV",
               "No CTV or OTT line item is running and no CTV product is on the "
               "order. Take the tile off the template.",
               where=_where(ctx, m.start(), m.group(0).strip()))]


def check_geofence_widget(ctx) -> list[dict]:
    """A geo-fencing MOBILE CONQUESTING strategy owes the geo-fencing breakout.

    The line items name the strategy - "Watsontown Trucking - 8.1 Geo-Fencing
    Mobile" - and the widget is the list of fences behind it. Without it the
    client is told a number and not where it came from.
    """
    from .quality import _sample, line_item_totals

    text = ctx.get("text") or ""
    if not text:
        return []
    fenced = [n for n, _i, _c in line_item_totals(text)
              if GEOFENCE_LINE.search(n) and GEOFENCE_PRODUCT.search(n)]
    if not fenced:
        return []
    if GEOFENCE_WIDGET in text:
        return []
    return [_f("geofence_widget_missing", "fail",
               "No Geo-Fencing Performance widget",
               f"{len(fenced)} geo-fencing strategy line"
               f"{'s are' if len(fenced) > 1 else ' is'} running - "
               # Trimmed. A wrapped tile description can end up glued to a
               # line item name, and the whole paragraph was printed here.
               + _sample([_short_name(n, 60) for n in sorted(fenced)], 6) +
               " - and the report does not carry the Geo-Fencing Performance "
               "breakout that lists the fences.",
               where=_where(ctx, text.find(fenced[0]), "Line Item Performance"))]


def _product_trace(expected: set, found: set, why=None,
                   about=()) -> list[tuple[str, str]]:
    """The evidence for THIS finding, and nothing else.

    It printed both full lists, what they agreed on, and every order the client
    has - twenty lines to explain one missing product. What matched is not in
    dispute, and the six orders that are not about Meta are not either.
    """
    about = set(about)
    rows = [("Live orders say", ", ".join(sorted(expected)) or "nothing"),
            ("Detected on the report", ", ".join(sorted(found)) or "nothing")]
    for label, value in (why or []):
        # "Meta · order 14885" - keep the ones naming a product in question.
        head = str(label).split("\u00b7")[0].strip()
        if head in about:
            rows.append((label, value))
    return rows


def check_products(ctx) -> list[dict]:
    """Compare the products on the report against the products the client's live
    orders say should be there. Skips silently when no order list is loaded."""
    expected = ctx.get("expected_products")
    if expected is None:
        return []
    found = ctx["products"]
    expected = {p for p in expected if p not in NOT_IN_MONTHLY_REPORT}

    # THE FINDING NAMES THE DIFFERENCE, NOTHING ELSE.
    #
    # It used to print both full lists and leave you to subtract them - four
    # product names to read before you could see which one was the problem, on
    # a line whose whole job was to name it. The products that matched are not
    # what anybody is looking for here.
    # AN EITHER-OR EXPECTATION IS SATISFIED BY EITHER.
    #
    # "Amazon Premium CTV + Video Ads" is one line item that can deliver all of
    # its impressions through one half, so a report with CTV and no Video is a
    # normal Amazon month. If NEITHER turns up it is still a finding, and both
    # halves stay allowed on the report.
    short = expected - found
    for group in ctx.get("expected_any") or []:
        if set(group) & found:
            short -= set(group)

    out = []
    missing = sorted(short)
    if missing:
        out.append(_f("product_missing", "fail",
                      "Ordered but not on the report: " + ", ".join(missing),
                      "", trace=_product_trace(expected, found,
                                               ctx.get("expected_why"), missing)))
    # A product is not rogue when one of the ordered products is what prints
    # it. Mobile Conquesting is sold as "Mobile Conquesting Display & Video
    # Ads" and its report carries a Display section - which is the buy, not a
    # Display campaign nobody ordered.
    from .products import formats_covered
    rogue = sorted(found - expected - formats_covered(expected)
                   - set(ctx.get("quiet_products") or ()))
    # NOT ON A LIFETIME. A campaign-to-date report shows everything that ever
    # ran on it, and the order list only carries what is still loaded - lines
    # that finished before the reporting month are dropped at import. WVU
    # Parkersburg's Social Mirror CTV ended in December and was on the report
    # for exactly the right reason.
    if ctx.get("is_lifetime"):
        rogue = []
    if rogue and expected:
        out.append(_f("product_rogue", "fail",
                      "On the report with no live order: " + ", ".join(rogue),
                      "", trace=_product_trace(expected, found,
                                               ctx.get("expected_why"), rogue)))
    # NOTHING IS RAISED WHEN THEY MATCH.
    #
    # An "everything is fine" line read as an accusation on a narrow screen -
    # "Products match the order" above a bare "Social Mirror" - and once the
    # wording was fixed it was still noise in a list whose whole job is naming
    # what needs attention. A problem raises a finding; silence means they
    # matched.
    return out



# HOW FAR OFF BUDGET IS WORTH SAYING SOMETHING ABOUT.
#
# Half. This is not a pacing tool - a campaign that underspends by 8% is a
# media question and not this tool's business. Half the budget missing, or half
# again over it, on a FULL month, is the shape of a reporting fault: a product
# reported for the wrong date range, a line item missing from the pull, two
# flights added together.
PACING_BAND = 0.5

# How far under its goal a finished campaign may land before it is worth
# saying. Ten percent: the goal is often derived from a monthly figure, so a
# tighter band would flag arithmetic rather than delivery.
GOAL_BAND = 0.10


def check_pacing(ctx) -> list[dict]:
    """A full month's spend should look like a full month's budget.

    Only whole months. A lifetime report covers a campaign's entire flight and
    a monthly budget says nothing about it, and a report for a part-month would
    be under by exactly the part that has not happened yet.
    """
    if ctx.get("is_lifetime"):
        return []
    budgets = ctx.get("budgets") or {}
    if not budgets:
        return []
    from .served import MIN_DAYS_TO_PACE
    from .spend import report_spend

    # The same "when did it launch" the impression rows carry. A budget is a
    # month's budget, so a line three days old is under it by definition.
    when = {p: v for p, v in (ctx.get("ordered") or {}).items()}
    spent = report_spend(ctx.get("text") or "")
    out = []
    for product, budget in sorted(budgets.items()):
        if not budget or budget <= 0:
            continue
        got = spent.get(product)
        if got is None:
            continue                    # this report does not print its spend
        ratio = got / budget
        if abs(ratio - 1.0) < PACING_BAND:
            continue
        row = when.get(product) or {}
        days = row.get("days")
        if days is not None and days <= MIN_DAYS_TO_PACE:
            continue
        way = "under" if ratio < 1 else "over"
        out.append(_f("pacing", "warn",
                      f"{product} spend is {abs(1 - ratio) * 100:.0f}% {way} budget",
                      f"${got:,.2f} spent against a monthly budget of "
                      f"${budget:,.2f}.",
                      [("Spend on the report", f"${got:,.2f}"),
                       ("Monthly budget on the order", f"${budget:,.2f}"),
                       ("That is", f"{ratio * 100:.0f}% of budget")]
                      + _when_rows(row)
                      + [("Flagged at", f"{PACING_BAND * 100:.0f}% either way")]))
    return out


# --------------------------------- the month against the campaign it sits in
# A LIFETIME COVERS THE MONTH. The two reports go out together, in the same
# folder, to the same person - so if the month prints more impressions than the
# whole campaign, one of the two was pulled with the wrong range and whoever
# opens them will see it before we do.
#
# ONLY EVER FLAGGED ON THE PAIR. Nothing here fires without both reports in
# hand, and the finding names the other one so it can be opened.
def check_month_within_lifetime(ctx) -> list[dict]:
    sib = ctx.get("sibling") or {}
    if not sib:
        return []
    # BOTH HALVES HAVE TO HAVE BEEN READ BY THE SAME CODE.
    #
    # This is a comparison between two stored numbers, and a stored number is
    # only as good as the reader that wrote it. McNutt Site Services' lifetime
    # was stored at 10 impressions and 2 clicks - read off page nine by a
    # parser that has since been corrected - and the monthly was failed for
    # printing 54,544 against it on three builds in a row, because each fix
    # corrected the monthly and left the number it was being compared to.
    #
    # A finding this loud should not be made out of a figure nobody has
    # re-read. When the other half is stale this abstains and says so, and the
    # sweep will come back to it: a re-check that changes a report's numbers
    # marks its sibling stale on the way past.
    if sib.get("fresh") is False:
        return []
    # Whichever of the two this report is, the comparison is the same one: the
    # month cannot be bigger than the campaign that contains it.
    mine = {"impressions": ctx.get("imps"), "clicks": ctx.get("clicks")}
    if ctx.get("is_lifetime"):
        month, life, other = sib, mine, "monthly"
    else:
        month, life, other = mine, sib, "lifetime"

    trace, bad = [], []
    for label, key in (("Impressions", "impressions"), ("Clicks", "clicks")):
        m, l = month.get(key), life.get(key)
        if not m or not l:            # a figure nobody read is not a comparison
            continue
        trace.append((f"{label}, the month", f"{m:,.0f}"))
        trace.append((f"{label}, the campaign", f"{l:,.0f}"))
        # A LITTLE OVER IS ROUNDING, NOT A WRONG RANGE. TapClicks prints these
        # rounded and the two reports are pulled minutes apart, so a hair of
        # daylight between them is the tool, not a mistake.
        if m > l * 1.005:
            bad.append(f"{label.lower()} {m:,.0f} against {l:,.0f}")
    if not bad:
        return []
    name = sib.get("filename") or f"the {other}"
    return [_f("month_over_lifetime", "fail",
               "The month reports more than the whole campaign",
               f"This month prints {'; '.join(bad)} on the lifetime. A month is "
               f"inside the campaign it belongs to, so one of the two was "
               f"pulled with the wrong date range. The other report is "
               f"{name}.",
               trace, where=_where(ctx, 0))]


def check_lifetime_goal(ctx) -> list[dict]:
    """A finished campaign that did not deliver what it was sold.

    Only on a lifetime, because that is the report where the question can be
    answered: the campaign is over, so what it served is final. A monthly is
    a slice and pacing on it is the other check.

    It is a WARNING, not a failure. The goal is often the monthly figure across
    the flight rather than a total the order states outright, and a campaign
    can legitimately finish a little under - the reporter needs to see it, not
    to be blocked by it.
    """
    if not ctx.get("is_lifetime"):
        return []
    ordered = ctx.get("ordered") or {}
    # A CANCELLED CAMPAIGN IS NOT SHORT OF ITS GOAL.
    #
    # Canceling changes the deal: what a campaign was SOLD to deliver stopped
    # being what it was asked to deliver on the day somebody stopped it. Sorge
    # Funeral Home's cancelled buy read "finished 100% under its goal, 3,873
    # served against 2,400,000 sold (20 months at the monthly figure on the
    # order)" - twenty months it was never going to run.
    ordered = {k: v for k, v in ordered.items() if not v.get("stopped")}
    # AND A FLAT PRODUCT HAS NO DELIVERY GOAL. SEO, Live Chat, Website Visitor
    # ID and Additional Billing are sold by the month, so a figure in their
    # impressions column is derived, not something the campaign was asked to
    # deliver - and the served side does not count them either.
    from .served import is_paced, served_impressions
    ordered = {k: v for k, v in ordered.items() if is_paced(k)}
    if not ordered:
        return []

    served = served_impressions(ctx.get("text") or "")
    goal = sum(v["impressions"] for v in ordered.values()
               if v.get("impressions"))
    if not goal:
        return []
    got = served["total"]
    if got >= goal * (1 - GOAL_BAND):
        return []
    short = goal - got
    basis = next((v.get("basis") for v in ordered.values() if v.get("basis")), "")
    trace = [("Served on the report", f"{got:,.0f}"),
             ("The campaign was sold", f"{goal:,.0f}"),
             ("Short by", f"{short:,.0f} ({short / goal * 100:.0f}%)")]
    if basis:
        trace.append(("Goal is", basis))
    for product, want in sorted(ordered.items()):
        if want.get("impressions"):
            # A GROUPED BUY TAKES THE DELIVERY OF BOTH HALVES. "CTV, Video" is
            # one row here and two products on the report, so looking it up by
            # its joined name found nothing and the trace read "0 of 250,000"
            # under a report that had served 137,296.
            got_p = sum(served["by_product"].get(x.strip(), 0.0)
                        for x in product.split(","))
            trace.append((product,
                          f"{got_p:,.0f} of {want['impressions']:,.0f}"))
    return [_f("lifetime_short_of_goal", "warn",
               f"Campaign finished {short / goal * 100:.0f}% under its goal",
               f"{got:,.0f} impressions served against {goal:,.0f} sold"
               + (f" ({basis})" if basis else "") + ".",
               trace=trace, where=COVER)]


# Anything further off the order than this gets a finding of its own rather
# than only a number in the pacing panel.
PACE_BAND = 50.0


def _when_rows(row) -> list[tuple[str, str]]:
    """When this line went live and how much of the month it had.

    "99% short" on a line that launched on the 28th is arithmetic, not news.
    The count decides whether there is a finding at all; the date is what the
    reader needs to judge the ones that survive.
    """
    out = []
    if row.get("started"):
        out.append(("Line launched", row["started"].strftime("%b %d, %Y")))
    days = row.get("days")
    if days is not None:
        out.append(("Ran this month", f"{days} day{'s' if days != 1 else ''}"))
    return out


def check_impression_pacing(ctx) -> list[dict]:
    """Impressions more than 50% off the order, either way.

    Spend has its own check above. This is the other half - and the half that
    covers a lifetime, where the campaign is finished and the number is final.

    The pacing panel has always shown the percentage. A number in a panel is
    something you have to go and read; a finding is something that finds you -
    and 568,121 served against 180,000 ordered is not a rounding difference,
    it is either the wrong order attached or a campaign nobody is watching.
    """
    ordered = ctx.get("ordered") or {}
    # See check_lifetime_goal: a buy somebody called off is not behind on it.
    ordered = {k: v for k, v in ordered.items() if not v.get("stopped")}
    if not ordered:
        return []
    from .served import MIN_DAYS_TO_PACE, is_paced, pacing_rows

    # THE SAME SENTENCE TWICE.
    #
    # On a lifetime the campaign check reports the whole campaign and this one
    # reports a product, and when the campaign IS one product those are one
    # fact: Paragon Casino Resort's lifetime carried "Social Mirror is 53%
    # short - 86,573 against 182,500" directly above "Campaign finished 53%
    # under its goal - 86,573 against 182,500". Two warnings, one number, and
    # a reader counting findings sees two problems.
    #
    # The campaign line is the one that keeps its ground: it is the point of a
    # lifetime, it carries the goal's basis, and it says which page. Only when
    # it is going to fire - it only ever reports UNDER, so an over-delivering
    # product still gets said here.
    one_product = (bool(ctx.get("is_lifetime"))
                   and len([p for p in ordered if is_paced(p)]) == 1)

    out = []
    for row in pacing_rows(ctx.get("text") or "", ordered):
        pace = row.get("pace")
        if pace is None or abs(pace) < PACE_BAND or row.get("total"):
            continue
        if row["unit"] == "money":
            continue                        # check_pacing already has the money
        if one_product and pace < 0:
            continue                        # the campaign line already said it
        # A WEEK OR LESS OF THE MONTH IS NOT OFF PACE, IT IS NEW.
        days = row.get("days")
        if days is not None and days <= MIN_DAYS_TO_PACE:
            continue

        def fmt(v):
            return f"{v:,.0f}"
        word = "over" if pace > 0 else "short"
        trace = [("Served on the report", fmt(row["served"])),
                 ("Ordered", fmt(row["ordered"])),
                 ("Difference", f"{pace:+.0f}%")]
        trace += _when_rows(row)
        if row.get("basis"):
            trace.append(("Order figure is", row["basis"]))
        out.append(_f("pacing_off", "warn",
                      f"{row['product']} is {abs(pace):.0f}% {word}",
                      f"{fmt(row['served'])} served against {fmt(row['ordered'])} "
                      f"ordered"
                      + (f" ({row['basis']})" if row.get("basis") else "")
                      + ".",
                      trace=trace))
    return out


def check_market_logo(ctx) -> list[dict]:
    """The corner of page one must not carry the reporting tool's own mark.

    It does not guess which mark that is - it is told, once, from a report that
    has it. Guessing was tried: a logo on three or more markets could not be
    any one partner's, so it must be the tool's. Seven Mountains disproved that
    in a day, running three markets on this board with one perfectly correct
    logo across all of them.
    """
    if not ctx.get("logo_generic"):
        return []
    return [_f("generic_logo", "fail",
               "Page one carries the reporting tool's default logo",
               "", where="p1")]


def _client_of(line_item: str) -> str:
    """The client a line item name starts with.

    TapClicks names every line item "<Client> - <strategy> <product>", so the
    part before the first dash says which client the DATA belongs to.
    """
    head = (line_item or "").split(" - ")[0]
    return re.sub(r"[^a-z0-9]", "", head.lower())


# How close two spellings of a client have to be to be the same client. A
# transposition inside a name this long scores about .89; two different
# companies whose names both start "Jiffy Lube" score well under .8, because
# what follows is what tells them apart and that is where the difference is.
NAME_MATCH = 0.85


def _client_head(rows) -> str:
    """The client name as the line items write it, unflattened."""
    biggest = max(rows, key=lambda r: r[1])[0] if rows else ""
    return (biggest or "").split(" - ")[0].strip() or "?"


def near(a: str, b: str) -> bool:
    """One name misspelled, rather than two different names."""
    from difflib import SequenceMatcher
    if min(len(a), len(b)) < 8:
        return False                      # too short for a typo to be obvious
    return SequenceMatcher(None, a, b).ratio() >= NAME_MATCH


def check_client_data(ctx) -> list[dict]:
    """The data on the report has to belong to the client it names.

    A report pulled against the wrong client passes everything else: the
    numbers are internally consistent, every widget is there, the products
    match somebody's orders - just not this client's. The one thing that gives
    it away is that the line items are named for a different company than the
    cover page.

    It takes a clear majority to say so, and it stays quiet when the line item
    names carry no client at all.
    """
    from .quality import line_item_totals

    named = _client_of((ctx.get("client") or "") + " - x")
    if not named or len(named) < 5:
        return []
    rows = line_item_totals(ctx.get("text") or "")
    if len(rows) < 2:
        return []

    hits: dict[str, float] = {}
    total = 0.0
    for name, imps, _clicks in rows:
        if " - " not in (name or ""):
            continue                       # no client on this row to read
        who = _client_of(name)
        if not who or len(who) < 5:
            continue
        weight = max(imps, 1.0)
        total += weight
        hits[who] = hits.get(who, 0.0) + weight
    if not hits or not total:
        return []

    # "The Home Store" and "River Valley Builders/The Home Store" are one
    # client written two ways, so containment counts as a match.
    #
    # SO DOES A TYPO. The order for Jiffy Lube Johnstown was booked as "Jiffy
    # Lube Jonhstown", two letters transposed, and against line items reading
    # "Jiffy Lube Johnstown - AI Video" that came out as 100% of the
    # impressions belonging to somebody else. A misspelled order is a
    # misspelled order; it is not a report pulled on the wrong client.
    def same(a: str, b: str) -> bool:
        if a == b or (len(a) >= 6 and len(b) >= 6 and (a in b or b in a)):
            return True
        return near(a, b)

    mine = sum(v for k, v in hits.items() if same(k, named))
    if mine / total >= 0.5:
        # Right client, spelled two ways. Not a report problem, but somebody
        # has to fix the order before it turns up on an invoice.
        typo = [k for k, v in sorted(hits.items(), key=lambda kv: -kv[1])
                if same(k, named) and k != named and k not in named
                and named not in k]
        if typo:
            return [_f("client_name_typo", "info",
                       "The order spells this client's name differently",
                       f"The order says \"{ctx.get('client')}\". The report's "
                       f"line items say \"{_client_head(rows)}\". Same client - "
                       f"the order has the typo.", where=COVER)]
        return []

    top = sorted(hits.items(), key=lambda kv: -kv[1])
    # ONE OTHER CLIENT HAS TO OWN IT. A report pulled against the wrong client
    # is that client's whole report; a spread across a dozen names is an
    # internal pull covering everybody, and calling that "the wrong client"
    # would be a finding nobody can act on.
    if not top or top[0][1] / total < 0.5 or same(top[0][0], named):
        return []
    biggest = max(rows, key=lambda r: r[1])[0]
    return [_f("wrong_client", "fail",
               "The data on this report is for a different client",
               f"{(1 - mine / total) * 100:.0f}% of the impressions sit on line "
               f"items named for somebody else - \"{_short_name(biggest, 60)}\" "
               f"for one.",
               trace=[("Report is for", ctx.get("client") or "?"),
                      ("Line items name", ", ".join(k for k, _ in top[:3])),
                      ("Impressions on this client",
                       f"{mine:,.0f} of {total:,.0f}")], where=COVER)]


# "&" AND "AND" ARE THE SAME WORD, AND STRIPPING PUNCTUATION MADE THEM TWO.
#
# W&L Mazda's report arrived filed as "W and L Mazda" and its cover page says
# "W&L Mazda". Flattened, that is "wandlmazda" against "wlmazda" - far enough
# apart that the fuzzy match said no - so a correct report was failed as a
# different client's, which is the loudest finding this tool has.
#
# The ampersand becomes the word before the punctuation goes, so the two
# spellings meet. Same for a plus sign, which the tracker uses the same way.
AMPERSAND = re.compile(r"\s*[&+]\s*")


def _flat_name(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", AMPERSAND.sub(" and ", (s or "")).lower())


def _same_client(a: str, b: str) -> bool:
    """One client written two ways, or misspelled once."""
    if not a or not b:
        return False
    if a == b or (len(a) >= 6 and len(b) >= 6 and (a in b or b in a)):
        return True
    return near(a, b)


def _mostly_this_client(ctx, filed: str) -> bool:
    """Do the report's own line items mostly belong to the filed client?

    Weighted by impressions, because a report is what it mostly is: one small
    line naming the right client does not rescue five large ones naming
    somebody else, and a majority is the same bar check_client_data uses for
    the mirror-image question.

    False when the line items carry no client name at all - that is nothing
    known rather than a disagreement, and the cover-page comparison stands.
    """
    from .quality import line_item_totals

    rows = line_item_totals(ctx.get("text") or "")
    if len(rows) < 2:
        return False
    mine = total = 0.0
    for name, imps, _clicks in rows:
        if " - " not in (name or ""):
            continue
        who = _client_of(name)
        if not who or len(who) < 5:
            continue
        weight = max(imps, 1.0)
        total += weight
        if _same_client(who, filed):
            mine += weight
    return bool(total) and mine / total >= 0.5


def check_client_matches_order(ctx) -> list[dict]:
    """The report has to be for the client whose slot it arrived in.

    THIS IS THE ONE THAT CATCHES A WHOLE REPORT PULLED ON THE WRONG CLIENT.

    check_client_data compares the report against itself - cover page against
    line items - and a report pulled entirely on the wrong client agrees with
    itself perfectly. St. Francis AMT Program's July slot held six pages of
    Everett Railroad Co: cover page, line items, every widget. Nothing in the
    file disagreed with anything else in it, so nothing was said, and the only
    finding on the board was a TikTok line missing from a report that was never
    St. Francis's to begin with.

    The name the file arrived under is the other half of the comparison, and it
    matters more than it looks: every other check on this report - the products
    owed, the order it paces against, the flight its dates are judged on - was
    built from that name.
    """
    filed = _flat_name(ctx.get("filed_as") or "")
    cover = _flat_name(ctx.get("client") or "")
    if len(filed) < 5 or len(cover) < 5 or _same_client(filed, cover):
        return []
    # AND NOT WHEN THE REPORT'S OWN LINE ITEMS SAY THE FILED CLIENT.
    #
    # A CLIENT HERE IS OFTEN A CAMPAIGN. Belmont Park is on the board as
    # "Belmont Park Branding Meta Pmax" and its cover page reads "Belmont Park
    # LEISURE Meta + Pmax" - two campaigns of one advertiser, one order, one
    # report - and the two names have nothing in common past "Belmont Park", so
    # the fuzzy match said different client and the loudest finding this tool
    # has went on a correct report.
    #
    # Comparing two names could never settle that: "Ashley HomeStore -
    # Blacksburg" and "Ashley HomeStore - Roanoke" look just as alike and ARE
    # two clients. The line items are what tells them apart. Five of Belmont
    # Park's six read "Belmont Park - ...", which is the filed client on the
    # page in its own data; St. Francis's slot full of Everett Railroad had
    # nothing on any page that said St. Francis, and still does not.
    if _mostly_this_client(ctx, filed):
        return []
    return [_f("wrong_client_file", "fail",
               "This is a different client's report",
               f"It arrived as {ctx['filed_as']}'s report. The cover page says "
               f"{ctx['client']}. Everything else on this page was checked "
               f"against {ctx['filed_as']}'s order.",
               trace=[("Filed as", ctx.get("filed_as") or "?"),
                      ("Cover page says", ctx.get("client") or "?")], where=COVER)]


def check_date_range(ctx) -> list[dict]:
    """The printed date range has to match what the report claims to be.

    A monthly covers exactly its month. A LIFETIME covers the campaign's whole
    flight, and that is where this earns its keep: a lifetime pulled with the
    default monthly range looks completely normal - right client, right
    numbers, right products - and silently reports one month of a two-year
    campaign. Nothing else on the page gives it away.

    The expected flight is the FIRST start and the LAST end across every one
    of that client's orders, because overlapping orders are one continuous
    campaign to the client even though the export lists them separately.
    """
    got = ctx.get("date_range")
    if not got:
        return [_f("date_range_missing", "warn", "No date range printed on the report",
                   "Page one usually carries \"Date range ... to ...\". Without it "
                   "there is no way to tell which period this covers.",
                   where=DATE_RANGE)]
    start, end = got
    fmt = "%b %d, %Y"
    printed = f"{start.strftime(fmt)} to {end.strftime(fmt)}"

    if ctx.get("is_lifetime"):
        want = ctx.get("flight")            # (first start, last end) across all orders
        if not want or not want[0]:
            return []
        w_start, w_end = want
        # WHERE EACH OF THOSE DATES CAME FROM. A lifetime's expected range is
        # built out of the client's line items, so when it looks wrong the only
        # useful question is which line item supplied it - and answering that
        # took a screenshot of the IO tool and a guess. It goes on the finding.
        trace = [("Printed on the report", printed),
                 ("Expected", f"{w_start.strftime(fmt)} to "
                              f"{w_end.strftime(fmt) if w_end else 'open'}")]
        for l in (ctx.get("flight_lines") or [])[:12]:
            trace.append((f"Order {l.get('order') or '?'} · line {l.get('lines') or '?'}"
                          f" · {l.get('product') or ''}".strip(),
                          f"{l.get('starts') or '?'} to {l.get('ends') or 'open'}"
                          + ("" if l.get("live", True) else " · paused")))
        out = []
        # A lifetime that starts at the month boundary while the campaign began
        # earlier is the classic wrong-range pull.
        if (w_start - start).days < -3:
            out.append(_f(
                "lifetime_short", "fail", "Lifetime report does not go back to the campaign start",
                f"Printed {printed}, but this client's earliest order starts "
                f"{w_start.strftime(fmt)}. Re-pull with the range set to the full flight.",
                trace=trace, where=DATE_RANGE))
        if w_end and (end - w_end).days < -3:
            out.append(_f(
                "lifetime_cut", "fail", "Lifetime report stops before the campaign ends",
                f"Printed {printed}, but the latest order runs to {w_end.strftime(fmt)}.",
                trace=trace, where=DATE_RANGE))
        # A RANGE THAT STARTS BEFORE THE CAMPAIGN IS NOT A FINDING.
        #
        # Allegheny Orthodontic's lifetime printed May 04, 2025 to Aug 04, 2026
        # against an order running May 04, 2026 to Aug 04, 2026 - a year of
        # empty calendar on the front, because that is where TapClicks starts a
        # lifetime pull. Nothing is missing and no number changes. It was
        # flagged, and worse, flagged as starting AFTER the campaign, which is
        # the opposite of what the dates said.
        #
        # The end is different and still warns: a lifetime printed to the end
        # of the month on a campaign that stopped on the 9th tells the client
        # it ran three weeks it did not.
        if w_end and (end - w_end).days > 3:
            out.append(_f(
                "lifetime_overrun", "warn", "Lifetime report runs past the campaign end",
                f"Printed {printed}, but the last order ends {w_end.strftime(fmt)}. "
                f"Check the range against the order - and if the order list is "
                f"the one that is out of date, the trace below says which line "
                f"item supplied that end date.", trace=trace, where=DATE_RANGE))
        return out

    period = ctx.get("period")              # "2026-07"
    if not period:
        return []
    y, m = (int(x) for x in period.split("-"))
    first = dt.date(y, m, 1)
    last = dt.date(y + (m == 12), (m % 12) + 1, 1) - dt.timedelta(days=1)
    if start == first and end == last:
        return []
    return [_f("date_range_wrong", "fail", "Date range is not the report month",
               f"Printed {printed}. This is the {first.strftime('%B %Y')} report, "
               f"so it should read {first.strftime(fmt)} to {last.strftime(fmt)}.",
               where=DATE_RANGE)]



# ---------------------------------------------------------------- completion
PCT = re.compile(r"(\d{1,3}(?:,\d{3})*(?:\.\d+)?)\s*%")

# Where one widget's rows stop. The next heading is the obvious boundary, but
# the PAGE FOOTER comes first whenever a widget ends near the bottom of a page
# - and missing it swept a DOOH report's Site and App rows into its device
# table, which then read as twenty-one unrecognized devices.
# THE NEXT WIDGET'S TITLE, WHICH IS INDENTED LIKE EVERY OTHER LINE.
#
# This anchored on "^\S" - a title starting in column ONE. pdftotext -layout
# indents the entire page, so that never matched anything, and a widget block
# ran on until it hit the page footer or a fixed character limit.
#
# R&R Heating's device table has two rows and is followed by Top CTV
# Publishers, well inside the limit - so Plex, Sling TV, TCL Channel and Tubi
# were read as devices and the report was warned for six devices TapClicks does
# not report, none of which were devices or claimed to be.
#
# "Top ..." ANYTHING. WVU Parkersburg's device table is followed by Top CTV TV
# Devices, whose header row is "Device Make" and whose first row is "Telly" -
# so the device block ran on into it and reported Device Make and Telly as two
# devices TapClicks does not report. Chasing suffixes one at a time was losing:
# Publishers, then Devices, then Makes. Every one of these widgets is titled
# "Top something".
WIDGET_END = re.compile(
    r"(^\s*(?:Top\s+\S.*"
    r"|\S.*(?:Performance|Publishers|Breakout|Screenshots|Details|"
    r"Conversions|per Line Item|by Strategy|by Day|by Creative|by Ad Size))\s*$"
    r"|Digital Marketing Report|Date range \w{3} \d{2}, \d{4})", re.M)


def _widget_block(text: str, start: int, limit: int = 6000) -> str:
    block = text[start:start + limit]
    end = WIDGET_END.search(block)
    return block[:end.start()] if end else block


def check_completion_rates(ctx) -> list[dict]:
    """No completion rate can exceed 100%.

    A rate above 100 means more completions than impressions, which is
    arithmetically impossible - it is a counting fault upstream, not a good
    month. Every "Completion Performance" widget is checked, whatever product
    it belongs to.
    """
    text = ctx.get("text") or ""
    out, seen = [], set()
    for m in re.finditer(r"^.*Completion Performance.*$", text, re.M):
        block = _widget_block(text, m.end())
        for line in block.split("\n"):
            bad = [v for v in PCT.findall(line)
                   if _num(v) is not None and _num(v) > 100.0]
            if not bad:
                continue
            label = re.split(r"\s{2,}", line.strip())[0][:60]
            key = (m.group(0).strip()[:60], label)
            if key in seen:
                continue
            seen.add(key)
            page_of = ctx.get("page_of")
            out.append(_f("completion_over_100", "fail",
                          "Completion rate above 100%",
                          f"{label} shows {', '.join(v + '%' for v in bad)}. "
                          f"More completions than impressions is not possible.",
                          where=(f"p{page_of(m.start())} · " if page_of else "")
                                + m.group(0).strip()))
    return out


def _num(v: str):
    try:
        return float(v.replace(",", ""))
    except ValueError:
        return None


# ---------------------------------------------------------------- devices
# The devices TapClicks reports. Anything else in this table is a data fault -
# a site name, a publisher, a blank - not a device someone watched an ad on.
# Taken off the everything-sample's Device Performance table, which lists each
# one with its own description: Connected Device is the DOOH billboard, and
# Connected Audio is the smart speaker - both read as junk until you see them.
KNOWN_DEVICES = {
    "connected tv", "streaming device", "connected device", "connected audio",
    "desktop", "mobile", "tablet", "smart tv", "set top box", "game console",
    "media player", "other", "unknown",
}


def _has_description(cells: list[str]) -> bool:
    """Does this row carry TapClicks' own description of the device?

    A sentence, not a number and not a fragment: the description column reads
    "An internet enabled device that provides streaming content...". Six words
    is comfortably below the shortest real one and well above anything a
    stray heading brings with it.
    """
    for c in cells[1:]:
        c = c.strip()
        if as_number(c) is not None:
            continue
        if len(c.split()) >= 6:
            return True
    return False


def check_devices_known(ctx) -> list[dict]:
    """Every row of Device Performance has to be an actual device."""
    text = ctx.get("text") or ""
    i = text.find("Device Performance")
    if i < 0:
        return []
    block = _widget_block(text, i + len("Device Performance"), 3000)

    # THE COLUMN HEADER SETS WHERE A ROW STARTS, AND WHETHER THERE IS A TABLE.
    #
    # Reliance Bank draws this widget as a donut, with no table under it at all
    # - just TapClicks' click-type glossary, "URL Click: When users click on
    # the final URL in an ad." and six more, which came out as seven
    # unrecognized devices. No header, no table, nothing to check.
    #
    # And where there is a table, a device's description wraps onto the next
    # line with the row's figures printed beside the wrapped half, so "content
    # directly on the TV.  14,999  24  0.16%" looks exactly like a row until
    # you notice it starts fifteen columns further in than the header does.
    lines = [ln for ln in block.split("\n") if ln.strip()]
    edge = None
    for ln in lines:
        cells = re.split(r"\s{2,}", ln.strip())
        if len(cells) < 2 or any(as_number(c) is not None for c in cells):
            continue
        if cells[0].strip().lower() in ("device name", "device") or \
                any(c.strip().lower() == "impressions" for c in cells):
            edge = len(ln) - len(ln.lstrip())
            break
    if edge is None:
        return []

    odd = []
    for ln in lines:
        if abs((len(ln) - len(ln.lstrip())) - edge) > 1:
            continue
        cells = re.split(r"\s{2,}", ln.strip())
        name = cells[0].strip()
        if not name or name.lower() in ("device name", "device", "description"):
            continue
        if as_number(name) is not None:
            continue
        if name.lower() in KNOWN_DEVICES:
            continue
        # THE TABLE DESCRIBES ITS OWN ROWS.
        #
        # Every real device carries a Description - "A personal device, either
        # mobile or stationary, that plays media, such as Smart Speakers and
        # iPods" for Media Player, which was reported as not a device on the
        # strength of a hard-coded list that had not heard of it. A list has to
        # be updated by somebody who has seen the new name; the description is
        # in the report already.
        #
        # The junk this check exists to catch - a page footer read as a row -
        # has no description, which is why it still gets caught.
        if _has_description(cells):
            continue
        if name not in odd:
            odd.append(name)
    if not odd:
        return []
    return [_f("unknown_device", "warn",
               f"{len(odd)} unrecognized device{'s' if len(odd) > 1 else ''} "
               f"in the device breakout",
               "Not a device TapClicks reports: " + ", ".join(odd[:8]) +
               ("..." if len(odd) > 8 else "") +
               ". Known devices are " + ", ".join(sorted(KNOWN_DEVICES)) + ".",
               where=_where(ctx, i, "Device Performance"))]


# ---------------------------------------------------------------- widgets
# Which widgets a product owes.
#
# The titles are the exact heading strings, taken off a 317-page sample that
# carries every widget the report can produce. They are compared against whole
# lines, not searched for inside them, because "Amazon Premium Site and App
# Performance" contains "Site and App Performance" - a substring search would
# let an Amazon-only report satisfy the generic site/app requirement.
#
# A widget is owed when EITHER the order says the product ran OR the report
# itself carries that product's ad section. The report declaring a section is
# the stronger signal of the two, and it keeps working on a report whose order
# list has not loaded.
W_CTV_PUBS   = "Top CTV Publishers"
W_SITE_APP   = "Site and App Performance"
W_AMZ_INV    = "Amazon Inventory Source Performance"
W_AMZ_SITE   = "Amazon Premium Site and App Performance"
W_YT_PLACE   = "YouTube+ Placement Performance"
W_YT_CHAN    = "Top 10 YouTube Channel Performance"
W_YTTV_CHAN  = "Top 10 YouTube TV Channel Performance"

# (product codes, ad-section header, [widget titles], plain-English why)
REQUIRED_WIDGETS: list[tuple] = [
    ({"CTV", "CV"}, "CTV ADS",              [W_CTV_PUBS], "Connected TV"),
    ({"SMC"},       "SOCIAL MIRROR CTV ADS", [W_CTV_PUBS], "Social Mirror CTV"),
    ({"AD", "AV"},  "AMAZON ADS",           [W_AMZ_INV, W_AMZ_SITE], "Amazon Premium"),
]

# The ads section for a product, written as it appears in the page header
# ("CTV ADS - PAGE 1"). Matched on the header line so a mention in body copy
# cannot stand in for a section that is not there.
SECTION = re.compile(r"^\s*([A-Z][A-Z0-9 &+/'.-]{2,60}?)\s+-\s+PAGE\s+\d+", re.M)

BARCK = re.compile(r"^\s*BARCK\+", re.M)


def _sections(text: str) -> set[str]:
    return {m.group(1).strip() for m in SECTION.finditer(text)}


def _heading_counts(text: str) -> dict[str, int]:
    """How many times each heading appears, as a whole line of its own."""
    out: dict[str, int] = {}
    for line in text.split("\n"):
        t = line.strip()
        if t:
            out[t] = out.get(t, 0) + 1
    return out


def _dooh_only(ctx) -> bool:
    """Is DOOH the only thing on this report?

    A billboard has no site and no app, so the widgets that list them are not
    owed - there would be nothing in them.
    """
    products = {p for p in (ctx.get("products") or set())}
    return bool(products) and products <= {"DOOH"}


# Products whose inventory breakout is NOT the generic site-and-app list. A
# billboard has neither. A CTV ad runs on Samsung TV Plus and Pluto, which the
# report lists as Top CTV Publishers - and that widget IS the breakout.
NO_SITE_APP_PRODUCTS = {"DOOH", "CTV", "Social Mirror CTV", "Video"}


def _site_app_not_owed(ctx, heads: dict) -> bool:
    """Would a Site and App Performance widget have anything to list?

    KB House of Guns runs BARCK+ on one CTV line item and was FAILED for not
    carrying Site and App Performance. CTV does not get one: its inventory is
    the publisher list, which was on the report - so the fail was about a
    widget TapClicks would not print for that buy.

    The inventory widget has to actually BE there. This is "the breakout it
    carries is the right one for what ran", not "CTV reports owe nothing".
    """
    if _dooh_only(ctx):
        return True
    products = {p for p in (ctx.get("products") or set())}
    if not products or not products <= NO_SITE_APP_PRODUCTS:
        return False
    return heads.get(W_CTV_PUBS, 0) > 0 or heads.get(W_AMZ_SITE, 0) > 0


def check_required_widgets(ctx) -> list[dict]:
    """Products that owe a particular widget have to actually carry it."""
    from ..product_codes import code_for

    text = ctx.get("text") or ""
    if not text:
        return []
    codes = {code_for(p) for p in (ctx.get("products") or set())}
    secs = _sections(text)
    heads = _heading_counts(text)
    out = []

    def _section_spot(ctx, why: str) -> str:
        """The page a product's section starts on, or "" when there are none."""
        text = ctx.get("text") or ""
        for word in re.split(r"[,/]| and ", why):
            word = word.strip()
            if len(word) < 4:
                continue
            i = text.upper().find(word.upper() + " - PAGE")
            if i >= 0:
                return _where(ctx, i, word)
        return ""

    def owed(title: str, n: int, why: str):
        have = heads.get(title, 0)
        if have >= n:
            return
        if n > 1:
            detail = (f"This report runs {why}, and {n} {title} widgets are owed "
                      f"- one per product. {have} on the report.")
        else:
            detail = (f"This report runs {why}, which should carry a {title} "
                      f"widget. It is not on the report.")
        # WHERE THE PRODUCT'S OWN SECTION IS, when the report prints section
        # banners. A missing widget is looked for inside its product's pages,
        # and "somewhere in forty pages" is the same as nothing.
        out.append(_f("widget_missing", "fail", f"No {title} widget"
                      if not have else f"Only {have} of {n} {title} widgets",
                      detail, where=_section_spot(ctx, why)))

    # AMAZON'S CTV HAS ITS OWN PUBLISHER WIDGET.
    #
    # Amazon Premium is bought as "Amazon Premium CTV + Video Ads" and maps to
    # CTV, so the CTV rule asked for Top CTV Publishers. Amazon does not print
    # one - its publishers arrive as Amazon Premium Site and App Performance,
    # which was on the report all along. Window World ran nothing but Amazon
    # and was failed for a widget that does not exist for it.
    #
    # Only when Amazon is the ONLY CTV on the report. A buy with both still
    # owes the widget for the half that is not Amazon.
    amazon_only_ctv = (heads.get(W_AMZ_SITE, 0) > 0
                       and "CTV ADS" not in secs
                       and "SOCIAL MIRROR CTV ADS" not in secs)

    # Widgets owed once per product family that ran.
    wanted: dict[str, list[str]] = {}
    for fam_codes, section, titles, why in REQUIRED_WIDGETS:
        if not (codes & fam_codes or section in secs):
            continue
        for t in titles:
            if t == W_CTV_PUBS and amazon_only_ctv:
                continue
            wanted.setdefault(t, []).append(why)
    for title, whys in wanted.items():
        # ONE TOP CTV PUBLISHERS WIDGET COVERS BOTH. CTV and Social Mirror CTV
        # are two products and one widget - the report prints a single Top CTV
        # Publishers list across them - so asking for one each failed a report
        # that had everything it owed.
        n = 1 if title == W_CTV_PUBS else len(whys)
        owed(title, n, " and ".join(whys))

    # YouTube. A YouTube TV only campaign owes the TV channel widget and
    # nothing else, which is why this is not in the table above: the report's
    # own sections are what say which of the two ran.
    yt_plus = "YOUTUBE+ ADS" in secs
    yt_tv = "YOUTUBE TV ADS" in secs
    if "YT" in codes and not (yt_plus or yt_tv):
        yt_plus = True                      # ran YouTube, no section says which
    if yt_plus:
        owed(W_YT_PLACE, 1, "YouTube+")
        owed(W_YT_CHAN, 1, "YouTube+")
    if yt_tv:
        owed(W_YTTV_CHAN, 1, "YouTube TV")

    # BARCK+ targeting owes the generic site and app breakout. The report
    # names its own BARCK+ widget, so this does not depend on knowing which
    # products carry BARCK+ - if the targeting ran, the report says so.
    #
    # Except on a DOOH-only report. A billboard has no site and no app: there
    # is nothing for that widget to list, and TapClicks does not print one.
    if BARCK.search(text) and not _site_app_not_owed(ctx, heads):
        owed(W_SITE_APP, 1, "BARCK+ targeting")
    return out


def check_zero_completion(ctx) -> list[dict]:
    """A completion rate of 0% on every row is a broken widget, not a result.

    It is the one number a video or CTV buy exists to report, and KB House of
    Guns' Top CTV Publishers list ran 0.00% down every publisher - Samsung TV
    Plus, Philo, Vizio, DIRECTV, Pluto. Nobody watched none of every ad on all
    of them in the same month. The metric arrived unmapped, and it goes to the
    client reading as a campaign that did nothing at all.

    Every row, not some: one publisher at zero is ordinary.
    """
    from .quality import zero_completion
    text = ctx.get("text") or ""
    page_of = ctx.get("page_of")
    out = []
    for at, title, rows in zero_completion(text):
        where = (f"p{page_of(at)} · " if page_of else "") + title
        out.append(_f("completion_all_zero", "fail",
                      f"Every completion rate on {title} is 0%",
                      f"All {rows} rows show 0.00%. A completion rate is what "
                      f"a video or CTV buy is judged on, and a column of "
                      f"zeroes is the metric arriving unmapped rather than an "
                      f"audience that watched none of it.", where=where))
    return out


def check_some_zero_completion(ctx) -> list[dict]:
    """One creative at 0% among nine that watched fine.

    A whole column of zeroes is a broken widget and has its own finding. This
    is the other half: a single 0.00% is not a metric fault, but it is not
    something to send a client without having looked at it either.
    """
    from .quality import some_zero_completion
    text = ctx.get("text") or ""
    page_of = ctx.get("page_of")
    out = []
    for at, title, rows in some_zero_completion(text):
        where = (f"p{page_of(at)} · " if page_of else "") + title
        out.append(_f("completion_zero_row", "warn",
                      f"{len(rows)} row{'s' if len(rows) > 1 else ''} at 0% "
                      f"completion on {title}",
                      "Nobody finished these once: " + "; ".join(rows[:6])
                      + (f"; and {len(rows) - 6} more" if len(rows) > 6 else "")
                      + ".",
                      where=where))
    return out


def check_variant_preview_links(ctx) -> list[dict]:
    """Every variant on a Social Mirror AI grid has to carry its preview link.

    NOT THE SAME CHECK AS A MISSING PREVIEW IMAGE, which is why it has its own
    name. On a CTV or display grid the preview is a picture printed in the
    cell, and a blank cell means the thumbnail did not render. On this grid the
    preview is a LINK - the report prints "Click to View" and hangs the ad's
    URL off it - so a blank cell means a variant nobody reading the report can
    open. Two different columns, two different repairs, and one word for both
    made a finding that read as though the wrong thing was wrong.

    It is the column somebody clicks when they want to know what an
    underperforming variant actually looks like.
    """
    from .quality import missing_preview_links
    text = ctx.get("text") or ""
    page_of = ctx.get("page_of")
    out = []
    for at, title, blank, rows in missing_preview_links(text):
        # THE CHECK'S OWN NAME, not the line above the header. That line is the
        # merged group header on this grid and comes back as "Creative", which
        # is the word this check is being renamed to get away from.
        where = (f"p{page_of(at)} · " if page_of else "") + "Variant preview links"
        out.append(_f("preview_link_blank", "warn",
                      f"{blank} of {rows} variants have no preview link",
                      "The Preview Link column is empty on those rows. The "
                      "preview on this grid is a link rather than a picture, "
                      "so those variants cannot be opened from the report at "
                      "all.",
                      where=where))
    return out


# Every rule, with the plain-English claim it is making. The label is written
# as the thing that is TRUE when the check passes, because that is how it is
# read on the report page - a list of what was verified, not a list of rule
# names. A rule that finds nothing has confirmed its label.
CHECKS: list[tuple] = [
    (check_headline_ctr,   "The headline CTR matches its own impressions and clicks"),
    (check_line_items,     "Line item totals add up to the headline"),
    (check_creative,       "Creative totals match the line items"),
    (check_device,         "The device breakout matches the eligible total"),
    (check_row_math,       "Every row's CTR matches that row's own numbers"),
    (check_rate_ceiling,   "No rate is above its ceiling"),
    (check_thumbnails,     "Every creative preview rendered"),
    (check_blank_pages,    "No widget page came out blank"),
    (check_geofence_names, "Every geo-fencing row has a business name"),
    (check_geofence_widget,
     "Geo-fenced Mobile Conquesting carries its fence breakout"),
    (check_rogue_ctv,      "No CTV widget on a report with no CTV"),
    (check_products,       "The products on the report match the live orders"),
    (check_date_range,     "The date range matches the period this report covers"),
    (check_client_data,    "The data on the report belongs to the client it names"),
    (check_client_matches_order,
     "The report is for the client whose slot it arrived in"),
    (check_market_logo,    "Page one carries the partner's logo, not a generic one"),
    (check_pacing,         "A full month's spend is close to a full month's budget"),
    (check_impression_pacing,
     "Delivery is within 50% of what the order asked for"),
    (check_lifetime_goal,  "A finished campaign delivered what it was sold"),
    (check_month_within_lifetime,
     "The month does not report more than the whole campaign"),
    (check_zero_completion, "No completion widget is 0% all the way down"),
    (check_some_zero_completion, "No video, CTV or audio row sits at 0% watched"),
    (check_variant_preview_links,
     "Every variant carries its preview link"),
    (check_completion_rates, "No completion rate is above 100%"),
    (check_devices_known,  "Every row of the device breakout is an actual device"),
    (check_required_widgets, "Every product carries the widgets it owes"),
    (check_strategy_categorized, "Every strategy line names the product it runs"),
    (check_truncated_text,  "No text is cut off for want of space"),
    (check_blank_screenshots, "Every ad screenshot rendered"),
    (check_conversion_names, "Every conversion is named for what the user did"),
    (check_creative_names,  "Every creative row says which creative it is"),
    (check_social_mirror_sizes, "No Social Mirror creative is named with an ad size"),
    (check_widget_errors,   "No widget printed an error instead of its data"),
    (check_page_banners,    "The template's page banners are switched off"),
    (check_social_placement_totals,
     "Social placements add up to no more than their platform totals"),
    (check_site_ctr,        "No site is clicking at a rate a person would not"),
    (check_completion_present,
     "Every video and audio product reports how much got watched"),
    (check_store_visits,    "Store visits agree with the store table"),
]

# Why a rule had nothing to do. "Nothing to check against" is true of every
# skipped rule and tells you nothing about which one you are looking at.
SKIP_WHY = {
    "check_products": "no order list loaded for this client, or the loaded one "
                      "was read by older import code",
    "check_date_range": "the report prints no date range",
    "check_client_matches_order": "the file it arrived as carries no client name",
    "check_impression_pacing": "no order figures loaded for this client",
    "check_headline_ctr": "no top-line impressions or clicks on the report",
    "check_month_within_lifetime": "the other half of the pair has not been "
                                   "read by today's code yet - comparing "
                                   "against a number nobody has re-read is how "
                                   "a fixed report keeps failing",
    "check_line_items": "no grids on the report",
    "check_creative": "no grids on the report",
    "check_device": "no grids on the report",
    "check_row_math": "no grids on the report",
    "check_rate_ceiling": "no grids on the report",
    "check_completion_rates": "no completion widget on the report",
    "check_zero_completion": "no completion rate column on the report",
    "check_some_zero_completion": "no video, CTV or audio completion column on "
                                  "the report",
    "check_variant_preview_links": "no creative grid with a preview link column",
    "check_devices_known": "no device breakout on the report",
    "check_required_widgets": "none of this report's products owe a widget",
    "check_geofence_names": "no geo-fencing table on the report",
    "check_geofence_widget": "no geo-fenced Mobile Conquesting on the report",
    "check_rogue_ctv": "no CTV tile on the report",
    "check_strategy_categorized": "no line item grid on the report",
    "check_truncated_text": "no line item grid on the report",
    "check_blank_screenshots": "no ad screenshot widget on the report",
    "check_conversion_names": "no conversion breakout on the report",
    "check_creative_names": "no creative grid on the report that has a name column - PPC prints the ad preview instead of a file name",
    "check_widget_errors": "",
    "check_page_banners": "",
    "check_social_mirror_sizes": "no Social Mirror creative grid on the report",
    "check_social_placement_totals": "no social placement grid on the report",
    "check_site_ctr": "no site and app breakout on the report",
    "check_completion_present": "no video or audio product on the report",
    "check_store_visits": "no store visit breakout on the report",
}

RULES = [fn for fn, _ in CHECKS]

SEV_ORDER = {"fail": 2, "warn": 1, "info": 0}


def _rule_applies(rule, ctx) -> bool:
    """Did this check have anything to work with?

    A rule with no data returns an empty list exactly like a rule that found
    nothing wrong, and reporting "products match the order" when no order list
    is loaded is a claim the tool cannot make.
    """
    name = rule.__name__
    if name == "check_products":
        # AND the order list has to have been read by the current import code.
        #
        # The export is parsed once and only the answer is kept, so while the
        # loaded orders were produced by an older import this check is
        # comparing the report against a stale answer. It was producing
        # findings from it - the same one, three times, on a report that was
        # right - and no amount of explaining beats not saying it.
        return (ctx.get("expected_products") is not None
                and ctx.get("orders_current", True))
    if name == "check_date_range":
        return bool(ctx.get("date_range"))
    if name == "check_client_matches_order":
        # Needs a NAMED file. A report saved by hand is called "Digital
        # Marketing Report.pdf" and says nothing about who it is for.
        return bool(ctx.get("filed_as")) and bool(ctx.get("client"))
    if name == "check_client_data":
        # Needs a client to compare against and line items that name one.
        return bool(ctx.get("client")) and bool(ctx.get("text"))
    if name == "check_lifetime_goal":
        return bool(ctx.get("is_lifetime")) and bool(ctx.get("ordered"))
    if name == "check_impression_pacing":
        return bool(ctx.get("ordered"))
    if name == "check_pacing":
        # Needs a budget on the order AND a spend on the report. Most products
        # print no spend at all, and most orders have no budget loaded yet.
        return bool(ctx.get("budgets")) and not ctx.get("is_lifetime")
    if name == "check_market_logo":
        # Two ways to have nothing to say: the corner could not be read at
        # all, or nobody has ever marked the tool's default logo, so there
        # is nothing to compare against yet.
        return bool(ctx.get("logo_hash")) and bool(ctx.get("logo_known"))
    if name in ("check_headline_ctr",):
        return ctx.get("imps") is not None and ctx.get("clicks") is not None
    if name in ("check_line_items", "check_creative", "check_device",
                "check_row_math", "check_rate_ceiling"):
        return bool(ctx.get("tables"))
    if name == "check_completion_rates":
        return "Completion Performance" in (ctx.get("text") or "")
    if name == "check_zero_completion":
        return "Completion Rate" in (ctx.get("text") or "")
    if name == "check_some_zero_completion":
        from .quality import WATCHED_WIDGET
        text = ctx.get("text") or ""
        return "Completion Rate" in text and bool(WATCHED_WIDGET.search(text))
    if name == "check_variant_preview_links":
        from .quality import LINK_HEADER
        return bool(LINK_HEADER.search(ctx.get("text") or ""))
    if name == "check_devices_known":
        return "Device Performance" in (ctx.get("text") or "")
    if name == "check_required_widgets":
        # This one no longer needs the order list: a report carrying a CTV or
        # Amazon or YouTube ads section has already declared what ran. It only
        # abstains when nothing on the report owes a widget at all.
        text = ctx.get("text") or ""
        if BARCK.search(text):
            return True
        secs = _sections(text)
        if secs & {s for _c, s, _t, _w in REQUIRED_WIDGETS} or \
           secs & {"YOUTUBE+ ADS", "YOUTUBE TV ADS"}:
            return True
        from ..product_codes import code_for
        codes = {code_for(p) for p in (ctx.get("products") or set())}
        owed = {"YT"} | {c for cs, _s, _t, _w in REQUIRED_WIDGETS for c in cs}
        return bool(codes & owed)
    if name in ("check_strategy_categorized", "check_truncated_text"):
        return bool(line_item_names(ctx.get("text") or ""))
    if name == "check_blank_screenshots":
        return "Ad Screenshots" in (ctx.get("text") or "")
    if name == "check_conversion_names":
        return bool(CONVERSION_HEADER.search(ctx.get("text") or ""))
    if name == "check_creative_names":
        return bool(creative_rows(ctx.get("text") or ""))
    if name == "check_social_mirror_sizes":
        return any(SOCIAL_MIRROR_GRID.search(t) for t, _n, _at in
                   creative_rows(ctx.get("text") or ""))
    if name == "check_store_visits":
        return bool(STORE_TABLE.search(ctx.get("text") or ""))
    if name == "check_completion_present":
        from .quality import WATCHED_PRODUCTS
        bodies = section_bodies(ctx.get("text") or "")
        if any(sec in bodies for sec, _o in COMPLETION_OWED):
            return True
        return bool(set(ctx.get("products") or ()) & set(WATCHED_PRODUCTS))
    if name == "check_site_ctr":
        return bool(SITE_GRID.search(ctx.get("text") or ""))
    if name == "check_social_placement_totals":
        return bool(PLACEMENT_GRID.search(ctx.get("text") or ""))
    if name == "check_rogue_ctv":
        return bool(CTV_WIDGET.search(ctx.get("text") or ""))
    if name == "check_geofence_widget":
        from .quality import line_item_totals
        return any(GEOFENCE_LINE.search(n) and GEOFENCE_PRODUCT.search(n)
                   for n, _i, _c in line_item_totals(ctx.get("text") or ""))
    if name == "check_geofence_names":
        # No geo-fencing on the report means nothing was verified. Reporting a
        # pass would claim every business name is filled in on a table that is
        # not there.
        #
        # And an EMPTY widget is the same thing. TapClicks prints the heading
        # and the column header whether or not there is any data under them, so
        # "the heading is here" was enough to tick a report that has no
        # geo-fencing rows at all - a claim about nothing, on a lot of reports.
        return bool(_geofence_rows(ctx.get("text") or ""))
    return True


def _page_finder(pages: list[str]):
    """offset in the joined text -> page number, 1-based.

    "1 site clicking above 5%" is true and unhelpful on its own; "page 31" is
    the difference between checking it and taking it on trust.
    """
    import bisect
    ends, at = [], 0
    for p in pages:
        at += len(p)
        ends.append(at)

    def page_of(offset: int) -> int:
        if offset < 0 or not ends:
            return 0
        return min(bisect.bisect_right(ends, offset) + 1, len(ends))
    return page_of


# No month is longer than 31 days, so a printed range wider than this is a
# campaign to date. The gap is deliberate: a monthly pulled a few days either
# side of the month is still a monthly.
LIFETIME_DAYS = 45


def looks_like_lifetime(printed) -> bool:
    """Is this a campaign-to-date report, judged by what it prints?

    THE NAME ONLY SAYS SO WHEN SOMEBODY NAMED IT. A report pulled by hand
    arrives as "Digital Marketing Report.pdf", so a lifetime covering two years
    was read as a monthly - checked against one month, and passed, on a report
    that was never about that month.
    """
    if not printed or not printed[0] or not printed[1]:
        return False
    return (printed[1] - printed[0]).days > LIFETIME_DAYS


def _client_named(text: str, filename: str) -> str:
    """The client this report is for: the cover page first, the name second."""
    from_text = (meta_from_text(text) or {}).get("client", "").strip()
    if from_text:
        return from_text
    from_name = meta_from_filename(filename or "")
    return from_name.get("client", "") if from_name.get("named") else ""


def _filed_client(filename: str) -> str:
    """The client the FILE is named for, or "" when it carries no name."""
    got = meta_from_filename(filename or "")
    return got.get("client", "") if got.get("named") else ""


def run_all(path: Path, filename: str | None = None, for_client: str = "",
            expected_products: set[str] | None = None,
            flight: tuple | None = None, flight_lines: list | None = None,
            period: str | None = None,
            market: str = "", expected_why: list | None = None,
            expected_any: list | None = None,
            quiet_products: set | None = None,
            is_lifetime: bool | None = None, ordered: dict | None = None,
            logo_generic: bool = False, logo_known: bool = False,
            logo_hash: str = "", budgets: dict | None = None,
            orders_current: bool = True,
            sibling: dict | None = None) -> dict:
    from .parser import pdf_pages
    # One call, and it gives the page boundaries for free - which is what lets
    # a finding say WHERE on a forty-one page report to look.
    per_page = pdf_pages(path)
    text = "".join(per_page)
    # WHOEVER KNOWS BEST, IN ORDER.
    #
    # 1. The caller, when a person has said which this is - the upload form has
    #    a Monthly/Lifetime choice and the report row remembers it. That was
    #    being ignored, so a lifetime somebody had labeled by hand was checked
    #    against one month and failed for its own date range.
    # 2. The filename, when it follows the convention.
    # 3. The range the report prints: no monthly can be wider than its month.
    if is_lifetime is None:
        is_lifetime = meta_from_filename(filename or path.name)["is_lifetime"]
        if not is_lifetime and looks_like_lifetime(date_range(text)):
            is_lifetime = True
    is_lifetime = bool(is_lifetime)
    imps, clicks, ctr = headline(text)
    tables = extract_tables(text, strict=True)
    ctx = {
        # THE OTHER HALF OF THE PAIR, when this client is getting a monthly and
        # a lifetime in the same delivery. {"impressions", "clicks",
        # "is_lifetime", "filename"} off the counterpart report, or nothing at
        # all - and nothing at all means no comparison is made.
        "sibling": sibling or {},
        # Which orders were looked at and what their dates were. Three separate
        # "this is a false positive" rounds all needed exactly this to settle
        # them, and reading it off the code cost a screenshot, a guess and a
        # deploy each time. It goes on the finding instead.
        "expected_why": expected_why or [],
        # Expectations one of two products satisfies - see ANY_OF.
        "expected_any": expected_any or [],
        # Bought, but not owed this month - paused, or out of flight.
        # Neither expected nor a surprise.
        "quiet_products": quiet_products or set(),
        # Other markets whose reports carry this same header logo.
        # What the order says each product should spend in a month.
        "budgets": budgets or {},
        # What the order bought, per product - the whole campaign on a
        # lifetime, this month on a monthly.
        "ordered": ordered or {},
        # False while the loaded orders were produced by an older import than
        # the one running now. The product check abstains rather than answering
        # from data it knows is out of date.
        "orders_current": bool(orders_current),
        "logo_generic": bool(logo_generic),
        # Has anybody marked ANY logo as the default yet? Until somebody
        # has, this check has nothing to compare against and abstains.
        "logo_known": bool(logo_known),
        "logo_hash": logo_hash or "",
        "path": path,
        "text": text,
        "page_text": per_page,
        "page_of": _page_finder(per_page),
        "pages": page_count(path),
        "tables": tables,
        "products": detect_products(text, tables),
        "expected_products": expected_products,
        "imps": imps, "clicks": clicks, "ctr": ctr,
        "date_range": date_range(text),
        "is_lifetime": bool(is_lifetime),
        "period": period,
        "flight": flight,
        # The line items that flight was built from, so a date that looks wrong
        # can be traced to the row that supplied it.
        "flight_lines": flight_lines or [],
        # Needed by the Social Mirror ad-size rule, which Curtis is exempt from.
        "market": market,
        # WHO THIS REPORT IS FOR, from the report itself.
        #
        # This was read off the filename, and a file saved by hand is called
        # "Digital Marketing Report.pdf" - so a perfectly good Beech Bend
        # lifetime was failed for carrying somebody else's data, the somebody
        # else being a client named Digital Marketing Report. The cover page
        # says who it is for; the filename only says so when it was named.
        "client": _client_named(text, filename or path.name),
        # WHO THIS REPORT WAS BEING JUDGED AS, before anything opened it. The
        # whole check suite is built from this name - the products, the order,
        # the flight - so when it disagrees with the cover page, every other
        # finding on the page is being made against the wrong campaign.
        #
        # THE CALLER FIRST. A file uploaded against a board row is judged as
        # that row's client whatever it happens to be called, and TapClicks
        # calls every file you download by hand "Digital Marketing Report.pdf".
        # Bloomsburg Theater Ensemble's July slot held seven pages of Benton
        # Rodeo and nothing was said, because the filename named nobody.
        "filed_as": (for_client or "").strip() or _filed_client(filename or path.name),
    }
    findings: list[dict] = []
    checks: list[dict] = []
    for rule, label in CHECKS:
        try:
            out = rule(ctx) or []
        except Exception as exc:                              # never let one rule sink a report
            out = [_f("rule_error", "warn", f"Check {rule.__name__} could not run", str(exc))]
            checks.append({"key": rule.__name__, "label": label, "state": "error"})
            findings.extend(out)
            continue
        raised = [f for f in out if f["severity"] in ("fail", "warn")]
        findings.extend(out)
        # A rule that returns nothing has verified its label. A rule that
        # cannot run at all - no order list loaded, no such table on the page -
        # returns nothing too, so it says so rather than claiming a pass.
        checks.append({
            "key": rule.__name__, "label": label,
            "state": ("failed" if any(f["severity"] == "fail" for f in raised)
                      else "flagged" if raised
                      else "skipped" if not _rule_applies(rule, ctx)
                      else "passed"),
            "count": len(raised),
        })

    meta = meta_from_text(text)
    from_name = meta_from_filename(filename or path.name)
    # The filename wins only when it IS a name. A file saved as "Digital
    # Marketing Report.pdf" was overriding the client the report itself prints
    # on page one, which is how a report ended up filed under a client called
    # Digital Marketing Report.
    if from_name.get("named"):
        meta.update({k: v for k, v in from_name.items()
                     if v and k in ("client", "account_ids")})
    for k in ("client", "account_ids"):
        if not meta.get(k) and from_name.get(k):
            meta[k] = from_name[k]
    # Whatever the name said, plus what the printed range says.
    meta["is_lifetime"] = bool(is_lifetime)
    if is_lifetime and period:
        # A LIFETIME BELONGS TO THE CYCLE IT SHIPS IN, not to the month its
        # campaign began. Read off the printed range, a lifetime covering
        # Jan 2025 to Jul 2026 filed itself under 2025-01 and vanished off the
        # board somebody was working.
        meta["period"] = period

    worst = "pass"
    for f in findings:
        if SEV_ORDER.get(f["severity"], 0) > SEV_ORDER.get(worst if worst != "pass" else "info", -1):
            pass
    sevs = {f["severity"] for f in findings}
    if "fail" in sevs:
        worst = "fail"
    elif "warn" in sevs:
        worst = "warn"
    else:
        worst = "pass"

    return {
        "meta": meta,
        "products": sorted(ctx["products"]),
        "impressions": int(imps or 0),
        "clicks": int(clicks or 0),
        "pages": ctx["pages"],
        "severity": worst,
        "findings": findings,
        "checks": checks,
    }
