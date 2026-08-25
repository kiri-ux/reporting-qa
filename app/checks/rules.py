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

LINE_ITEM = re.compile(r"Line Item Performance$")
CREATIVE = re.compile(r"(Creative Performance|Creative Group Performance)$")
DEVICE_TITLES = ("Device Performance", "Device")


def _f(code, sev, title, detail):
    return {"code": code, "severity": sev, "title": title, "detail": detail}


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


def check_headline_ctr(ctx) -> list[dict]:
    imps, clicks, ctr = ctx["imps"], ctx["clicks"], ctx["ctr"]
    if not imps or clicks is None or ctr is None:
        return []
    if abs(clicks / imps * 100 - ctr) <= 0.011:
        return []

    # The footnote says CTR excludes CTV. When it does, the stated rate divides
    # non-CTV clicks by non-CTV impressions while the headline impression count
    # still shows everything. Expected, but worth naming so nobody re-raises it.
    ctv_i, _ctv_c = _ctv_totals(ctx)
    if ctv_i and imps - ctv_i > 0:
        ex_ctv = clicks / (imps - ctv_i) * 100
        if abs(ex_ctv - ctr) <= 0.011:
            return [_f("ctv_ctr_base", "info", "CTR is calculated excluding CTV",
                       f"Stated {ctr:.2f}%. Against all {int(imps):,} impressions that would be "
                       f"{clicks / imps * 100:.3f}%, but against the {int(imps - ctv_i):,} non-CTV "
                       f"impressions it is {ex_ctv:.3f}%, which matches. Expected behaviour.")]
    return [_f("headline_ctr", "fail", "Top-line CTR does not match its own numbers",
               f"Report states {ctr:.2f}%. {int(clicks):,} clicks / {int(imps):,} impressions "
               f"= {clicks / imps * 100:.3f}%.")]


def check_line_items(ctx) -> list[dict]:
    imps, clicks = ctx["imps"], ctx["clicks"]
    tables = [t for t in ctx["tables"] if LINE_ITEM.search(t.title or "")]
    if not tables or not imps:
        return []
    si = sum(t.total("Impressions") for t in tables)
    sc = sum(t.total("Clicks") for t in tables)
    out = []
    if abs(si - imps) > 1:
        out.append(_f("line_items_impressions", "fail",
                      "Line items do not sum to the top line",
                      f"Line items total {si:,.0f} impressions against a stated {imps:,.0f} "
                      f"({si - imps:+,.0f})."))
    if clicks is not None and abs(sc - clicks) > 0.5:
        _ctv_i, ctv = _ctv_totals(ctx)
        # allow a click or two of slack: a wrapped line-item name can leave one
        # row on the wrong side of the CTV test
        if ctv > 0 and abs((sc - clicks) - ctv) <= 2:
            out.append(_f("ctv_click_base", "info",
                          "CTV clicks excluded from the top line",
                          f"Line items total {sc:.0f} clicks against a stated {clicks:.0f}. "
                          f"The {sc - clicks:.0f} difference is the CTV line items, which is "
                          f"expected behaviour."))
        else:
            gap = abs(sc - clicks)
            material = gap > 5 or (clicks and gap / clicks > 0.005)
            out.append(_f("line_items_clicks", "fail" if material else "warn",
                          "Line item clicks do not sum to the top line",
                          f"Line items total {sc:.0f} clicks against a stated {clicks:.0f} "
                          f"({sc - clicks:+.0f})."))
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
    """Device breakout excludes Mobile Conquesting, PPC, YouTube, LinkedIn and
    Performance Max, so it is compared against device-eligible impressions only."""
    dev = None
    for t in ctx["tables"]:
        if (t.title or "").strip() in DEVICE_TITLES or (t.title or "").endswith("Device Performance"):
            dev = t
            break
    if dev is None or not ctx["imps"]:
        return []
    device_total = dev.total("Impressions")
    excluded = settings.excluded_products
    eligible = 0.0
    li = [t for t in ctx["tables"] if LINE_ITEM.search(t.title or "")]
    for t in li:
        for name, v in t.body:
            if not is_device_excluded(name, excluded):
                eligible += v.get("Impressions", 0.0)
    if eligible <= 0:
        return []
    diff = device_total - eligible
    pct = diff / eligible * 100
    if pct > 1:
        return [_f("device_over", "fail",
                   "Device breakout exceeds what was served",
                   f"Device totals {device_total:,.0f} against {eligible:,.0f} device-eligible "
                   f"impressions (+{pct:.1f}%). Filtering can only remove impressions.")]
    if pct < -settings.device_under_tolerance_pct:
        return [_f("device_under", "warn",
                   "Device breakout well under the eligible total",
                   f"Device totals {device_total:,.0f} against {eligible:,.0f} eligible "
                   f"({pct:.1f}%). Unknown-device filtering does not usually account for this.")]
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
    hits = []
    for pg in range(1, pages + 1):
        txt = pdf_text(path, pg, pg)
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
def check_geofence_names(ctx) -> list[dict]:
    text = ctx["text"]
    i = text.find("Geo-Fencing Performance")
    if i < 0:
        return []
    seg_lines = text[i:].split("\n")
    seg = []
    for n, line in enumerate(seg_lines):
        if n and line.strip() and not line.startswith((" ", "\t")) and "Performance" in line:
            break
        seg.append(line)
        if n > 220:
            break
    hdr = next((l for l in seg if "Business Name" in l and "Address" in l), None)
    if not hdr:
        return []
    addr = hdr.index("Address")
    rows = [l for l in seg if re.search(r"\s{2,}[A-Z]{2}\s{2,}\d{5}\s", l)]
    blank = [l for l in rows if (len(l) - len(l.lstrip())) >= addr - 2]
    if not rows or not blank:
        return []
    return [_f("geofence_no_business_name", "info",
               "Geo-fence rows have no business name",
               f"{len(blank)} of {len(rows)} rows show an address with no business name, "
               f"latitude or longitude. Expected if the fence was built from an address list.")]


def check_products(ctx) -> list[dict]:
    """Compare the products on the report against the products the client's live
    orders say should be there. Skips silently when no order list is loaded."""
    expected = ctx.get("expected_products")
    if expected is None:
        return []
    found = ctx["products"]
    expected = {p for p in expected if p not in NOT_IN_MONTHLY_REPORT}

    out = []
    missing = sorted(expected - found)
    if missing:
        out.append(_f("product_missing", "fail",
                      f"{len(missing)} product{'s' if len(missing) > 1 else ''} on the order "
                      f"but not on the report",
                      "Expected from live orders and absent here: " + ", ".join(missing) +
                      ". On the report: " + (", ".join(sorted(found)) or "nothing detected") + "."))
    rogue = sorted(found - expected)
    if rogue and expected:
        out.append(_f("product_rogue", "fail",
                      f"{len(rogue)} product{'s' if len(rogue) > 1 else ''} on the report "
                      f"with no live order",
                      "On the report but not on any qualifying order: " + ", ".join(rogue) +
                      ". Expected: " + (", ".join(sorted(expected)) or "none") + "."))
    # NOTHING IS RAISED WHEN THEY MATCH.
    #
    # An "everything is fine" line read as an accusation on a narrow screen -
    # "Products match the order" above a bare "Social Mirror" - and once the
    # wording was fixed it was still noise in a list whose whole job is naming
    # what needs attention. A problem raises a finding; silence means they
    # matched.
    return out



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
            out.append(_f("completion_over_100", "fail",
                          "Completion rate above 100%",
                          f"{m.group(0).strip()}: {label} shows "
                          f"{', '.join(v + '%' for v in bad)}. More completions "
                          f"than impressions is not possible."))
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
    "other", "unknown",
}


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
        if name not in odd:
            odd.append(name)
    if not odd:
        return []
    return [_f("unknown_device", "warn",
               f"{len(odd)} unrecognised device{'s' if len(odd) > 1 else ''} "
               f"in the device breakout",
               "Not a device TapClicks reports: " + ", ".join(odd[:8]) +
               ("..." if len(odd) > 8 else "") +
               ". Known devices are " + ", ".join(sorted(KNOWN_DEVICES)) + ".")]


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

    # Widgets owed once per product family that ran.
    wanted: dict[str, list[str]] = {}
    for fam_codes, section, titles, why in REQUIRED_WIDGETS:
        if not (codes & fam_codes or section in secs):
            continue
        for t in titles:
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
    (check_completion_rates, "No completion rate is above 100%"),
    (check_devices_known,  "Every row of the device breakout is an actual device"),
    (check_required_widgets, "Every product carries the widgets it owes"),
]

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
    if name == "check_geofence_names":
        # No geo-fencing on the report means nothing was verified. Reporting a
        # pass would claim every business name is filled in on a table that is
        # not there.
        text = ctx.get("text") or ""
        i = text.find("Geo-Fencing Performance")
        if i < 0:
            return False
        return any("Business Name" in l and "Address" in l
                   for l in text[i:i + 20000].split("\n"))
    return True


def run_all(path: Path, filename: str | None = None,
            expected_products: set[str] | None = None,
            flight: tuple | None = None, period: str | None = None) -> dict:
    text = pdf_text(path)
    is_lifetime = meta_from_filename(filename or path.name)["is_lifetime"]
    imps, clicks, ctr = headline(text)
    tables = extract_tables(text, strict=True)
    ctx = {
        "path": path,
        "text": text,
        "pages": page_count(path),
        "tables": tables,
        "products": detect_products(text, tables),
        "expected_products": expected_products,
        "imps": imps, "clicks": clicks, "ctr": ctr,
        "date_range": date_range(text),
        "is_lifetime": bool(is_lifetime),
        "period": period,
        "flight": flight,
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
