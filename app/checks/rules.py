"""The check suite. Each rule returns zero or more findings.

A finding is a dict: code, severity (fail|warn|info), title, detail.
Severity drives the dashboard colour and whether anyone gets pinged.
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
                      check_widget_errors, SITE_GRID,
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
# the numerator, so it only ever recognised the case by accident. On a report
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
                       f"report's own footnote says so. Expected.", trace)]
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
                   f"Neither is the stated rate.", trace)]

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
                   f"that do not give a rate either.", trace)]

    return [_f("headline_ctr", "fail", "Top-line CTR does not match its own numbers",
               f"Report states {ctr:.2f}%. {clicks:,.0f} clicks / {imps:,.0f} impressions "
               f"= {plain:.3f}%.", trace)]


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
                                  for n, i, _c in biggest))]))

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
                        "; ".join(_short_name(n, 70) for n, _i, _c in rows))]))
        return out

    excl = sum(r[2] for r in excluded)
    unexplained = gap - excl
    ctrace = [("Top-line clicks", f"{clicks:,.0f}"),
              ("Line items counted", f"{len(rows)}"),
              ("Their clicks", f"{sc:,.0f}"),
              ("Difference", f"{gap:+,.0f} clicks"),
              ("Clicks on CTV and OTT line items", f"{excl:,.0f}"),
              # Named, not just totalled. A remainder of eight clicks is only
              # findable if you can see which lines were taken out and for how
              # much - the total on its own says "trust me".
              ("Which lines those are",
               "; ".join(f"{_short_name(n, 44)}: {c:,.0f}"
                         for n, _i, c in sorted(excluded, key=lambda r: -r[2]))
               or "none"),
              ("Left unexplained", f"{unexplained:+,.0f} clicks")]

    # Material is measured against the campaign, not against the excluded
    # products. Eight clicks adrift on a report with three thousand of them is
    # a rounding difference, however it is arrived at.
    material = max(5.0, clicks * 0.005)
    if abs(gap) <= max(2.0, clicks * 0.005):
        return out
    if unexplained == 0:
        out.append(_f("clicks_exclude_products", "info",
                      "The top-line clicks leave CTV and OTT out",
                      f"Line items total {sc:,.0f} clicks against a stated "
                      f"{clicks:,.0f}. The CTV and OTT line items carry {excl:,.0f} "
                      f"clicks, which that tile excludes and which accounts "
                      f"for all {abs(gap):,.0f} of the difference. Expected.",
                      ctrace))
    elif abs(unexplained) <= material:
        # Small, but not nothing. Saying "expected" would be a claim the
        # arithmetic does not support, and the remainder is worth a look even
        # when it is too small to hold a report up.
        out.append(_f("clicks_part_explained", "warn",
                      f"{abs(unexplained):,.0f} click"
                      f"{'s' if abs(unexplained) != 1 else ''} unaccounted for",
                      f"Line items total {sc:,.0f} clicks against a stated "
                      f"{clicks:,.0f}. The CTV and OTT line items carry {excl:,.0f}, "
                      f"which that tile excludes - that explains all but "
                      f"{abs(unexplained):,.0f} of the "
                      f"{abs(gap):,.0f} difference. Small enough not to hold the "
                      f"report up, but it is not nothing. Investigate lists the "
                      f"lines that were taken out.", ctrace))
    else:
        out.append(_f("line_items_clicks", "fail",
                      "Line item clicks do not sum to the top line",
                      f"Line items total {sc:,.0f} clicks against a stated "
                      f"{clicks:,.0f} ({gap:+,.0f}). The CTV and OTT line items carry "
                      f"{excl:,.0f}, which that tile can exclude - that still "
                      f"leaves {abs(unexplained):,.0f} "
                      f"clicks unaccounted for.", ctrace))
    return out


def check_creative(ctx) -> list[dict]:
    imps = ctx["imps"]
    tables = [t for t in ctx["tables"] if CREATIVE.search(t.title or "")]
    if not tables or not imps:
        return []
    si = sum(t.total("Impressions") for t in tables)
    if si > imps * 1.001:
        return [_f("creative_over_top", "fail",
                   "Creative table claims more than the campaign delivered",
                   f"Creative tables total {si:,.0f} impressions against a stated {imps:,.0f} "
                   f"(+{si - imps:,.0f}). Usually a de-duplication problem upstream.")]
    if si < imps * 0.999:
        return [_f("creative_under_top", "info",
                   "Creative tables cover part of the campaign",
                   f"Creative tables total {si:,.0f} against {imps:,.0f}. Normal when a channel "
                   f"(CTV, Performance Max) reports completions or events rather than clicks.")]
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
                out.append(_f("row_ctr", "warn", "Row CTR does not match its own numbers",
                              f"{t.title or 'table'} / \"{name[:60]}\": shows {ctr:.2f}%, "
                              f"{clicks:.0f}/{imps:.0f} = {expected:.3f}%."))
    return out[:5]


RATE_RE = re.compile(r"\b(\d{2,4}\.\d{2})%")


def check_rate_ceiling(ctx) -> list[dict]:
    bad = sorted({m for m in RATE_RE.findall(ctx["text"]) if float(m) > 100})
    if not bad:
        return []
    return [_f("rate_over_100", "warn", "Rate printed above 100%",
               "Values found: " + ", ".join(f"{b}%" for b in bad[:5]) +
               ". Completion rates and CTR cannot exceed 100%.")]


# ---------------------------------------------------------------- previews
def check_thumbnails(ctx) -> list[dict]:
    n = ctx["text"].count("Thumbnail not available")
    if not n:
        return []
    return [_f("missing_thumbnail", "warn",
               f"{n} creative preview{'s' if n > 1 else ''} did not render",
               'The report prints "Thumbnail not available" in place of the preview image.')]


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
               detail)]


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
               f"latitude or longitude. Expected if the fence was built from an address list.")]


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
                   "there is no way to tell which period this covers.")]
    start, end = got
    fmt = "%b %d, %Y"
    printed = f"{start.strftime(fmt)} to {end.strftime(fmt)}"

    if ctx.get("is_lifetime"):
        want = ctx.get("flight")            # (first start, last end) across all orders
        if not want or not want[0]:
            return []
        w_start, w_end = want
        out = []
        # A lifetime that starts at the month boundary while the campaign began
        # earlier is the classic wrong-range pull.
        if (w_start - start).days < -3:
            out.append(_f(
                "lifetime_short", "fail", "Lifetime report does not go back to the campaign start",
                f"Printed {printed}, but this client's earliest order starts "
                f"{w_start.strftime(fmt)}. Re-pull with the range set to the full flight."))
        if w_end and (end - w_end).days < -3:
            out.append(_f(
                "lifetime_cut", "fail", "Lifetime report stops before the campaign ends",
                f"Printed {printed}, but the latest order runs to {w_end.strftime(fmt)}."))
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
               f"so it should read {first.strftime(fmt)} to {last.strftime(fmt)}.")]



# ---------------------------------------------------------------- completion
PCT = re.compile(r"(\d{1,3}(?:,\d{3})*(?:\.\d+)?)\s*%")

# Where one widget's rows stop. The next heading is the obvious boundary, but
# the PAGE FOOTER comes first whenever a widget ends near the bottom of a page
# - and missing it swept a DOOH report's Site and App rows into its device
# table, which then read as twenty-one unrecognised devices.
WIDGET_END = re.compile(
    r"(^\S.*(?:Performance|Publishers|Breakout|Screenshots)\s*$"
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

    odd = []
    for line in block.split("\n"):
        if not line.strip() or line.startswith((" ", "\t")):
            continue
        cells = re.split(r"\s{2,}", line.strip())
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
               f"{len(odd)} unrecognised device{'s' if len(odd) > 1 else ''} "
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
        out.append(_f("widget_missing", "fail", f"No {title} widget"
                      if not have else f"Only {have} of {n} {title} widgets",
                      detail))

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
        owed(title, len(whys), " and ".join(whys))

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
    if BARCK.search(text):
        owed(W_SITE_APP, 1, "BARCK+ targeting")
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
    (check_products,       "The products on the report match the live orders"),
    (check_date_range,     "The date range matches the period this report covers"),
    (check_market_logo,    "Page one carries the partner's logo, not a generic one"),
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
    "check_products": "no order list loaded for this client",
    "check_date_range": "the report prints no date range",
    "check_headline_ctr": "no top-line impressions or clicks on the report",
    "check_line_items": "no grids on the report",
    "check_creative": "no grids on the report",
    "check_device": "no grids on the report",
    "check_row_math": "no grids on the report",
    "check_rate_ceiling": "no grids on the report",
    "check_completion_rates": "no completion widget on the report",
    "check_devices_known": "no device breakout on the report",
    "check_required_widgets": "none of this report's products owe a widget",
    "check_geofence_names": "no geo-fencing table on the report",
    "check_strategy_categorized": "no line item grid on the report",
    "check_truncated_text": "no line item grid on the report",
    "check_blank_screenshots": "no ad screenshot widget on the report",
    "check_conversion_names": "no conversion breakout on the report",
    "check_creative_names": "no creative grid on the report",
    "check_widget_errors": "",
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
        return ctx.get("expected_products") is not None
    if name == "check_date_range":
        return bool(ctx.get("date_range"))
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
        return any(SOCIAL_MIRROR_GRID.search(t) for t, _n in
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


def run_all(path: Path, filename: str | None = None,
            expected_products: set[str] | None = None,
            flight: tuple | None = None, period: str | None = None,
            market: str = "", expected_why: list | None = None,
            expected_any: list | None = None,
            quiet_products: set | None = None,
            logo_generic: bool = False, logo_known: bool = False,
            logo_hash: str = "") -> dict:
    from .parser import pdf_pages
    # One call, and it gives the page boundaries for free - which is what lets
    # a finding say WHERE on a forty-one page report to look.
    per_page = pdf_pages(path)
    text = "".join(per_page)
    is_lifetime = meta_from_filename(filename or path.name)["is_lifetime"]
    imps, clicks, ctr = headline(text)
    tables = extract_tables(text, strict=True)
    ctx = {
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
        # Needed by the Social Mirror ad-size rule, which Curtis is exempt from.
        "market": market,
        "client": meta_from_filename(filename or path.name).get("client", ""),
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
    meta.update({k: v for k, v in meta_from_filename(filename or path.name).items()
                 if v or k == "is_lifetime"})
    if not meta.get("client"):
        meta["client"] = meta_from_filename(filename or path.name)["client"]

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
