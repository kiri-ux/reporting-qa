"""The check suite. Each rule returns zero or more findings.

A finding is a dict: code, severity (fail|warn|info), title, detail.
Severity drives the dashboard colour and whether anyone gets pinged.
"""
from __future__ import annotations

import re
from pathlib import Path

from ..config import settings
from .parser import (SKIP_LINE, Table, extract_tables, headline, meta_from_filename,
                     meta_from_text, page_count, page_ink_pct, pdf_text, tokens)
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
    if not out:
        # Worded as a confirmation, not a bare list. "Products match the order"
        # above an unadorned "Social Mirror" reads like the thing it found
        # wrong, which is the opposite of what it means.
        names = ", ".join(sorted(found))
        out.append(_f("products_match", "info", "Products match the order",
                      (f"Nothing missing, nothing extra. Both the report and the "
                       f"live orders show: {names}.") if names else
                      "No products were detected on the report to compare."))
    return out


RULES = [
    check_headline_ctr,
    check_line_items,
    check_creative,
    check_device,
    check_row_math,
    check_rate_ceiling,
    check_thumbnails,
    check_blank_pages,
    check_geofence_names,
    check_products,
]

SEV_ORDER = {"fail": 2, "warn": 1, "info": 0}


def run_all(path: Path, filename: str | None = None,
            expected_products: set[str] | None = None) -> dict:
    text = pdf_text(path)
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
    }
    findings: list[dict] = []
    for rule in RULES:
        try:
            findings.extend(rule(ctx))
        except Exception as exc:                              # never let one rule sink a report
            findings.append(_f("rule_error", "warn", f"Check {rule.__name__} could not run", str(exc)))

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
    }
