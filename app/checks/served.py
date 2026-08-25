"""What the report says was actually delivered, per product.

The order says what a month was bought to do; this is the other half of the
comparison. Impressions come off the Line Item Performance grid, which is the
only place in the text where the campaign is broken down far enough to attach
a number to a product. Money comes off the Spend Overview tiles.

ATTRIBUTION IS DELIBERATELY CAUTIOUS. A line item whose name maps to one
product counts towards that product. One that maps to none, or to more than
one, counts towards the total and nothing else - because "CTV + Video" served
40,000 impressions says nothing about how they split, and a made-up split is
worse than an honest "not attributed".
"""
from __future__ import annotations

import re

from .products import PRODUCT_LEADS, _flat
from .quality import line_item_totals


def _leads() -> list[tuple[str, str]]:
    """The order vocabulary, plus what a REPORT calls the same things.

    An order says "Mobile Conquesting Display & Video Ads". The report's line
    items say "Close Lumber - Geo-Retargeting Mobile" - the product is one word
    at the end, and the order's own pattern needs both words, so every Mobile
    Conquesting line on every report matched nothing at all.

    The extras go in beside their equivalents rather than at the end, because
    the order of this list IS the specificity rule: "Venue Targeting DOOH
    Video" is DOOH, and a rule that ran after Video would call it Video.
    """
    extra = {"Mobile Conquesting": r"\bmobile\b", "CTV": r"\bott\b",
             "Native Display": r"\bnativ\w*\b"}
    out: list[tuple[str, str]] = []
    for product, rx in PRODUCT_LEADS:
        out.append((product, rx))
        if product in extra:
            out.append((product, extra.pop(product)))
    out.extend(extra.items())
    return out


LEADS = _leads()


def report_product(name: str) -> str | None:
    """Which product a line item on the REPORT belongs to, or None.

    The order's patterns are anchored to the front of a product name. Here the
    product is somewhere inside a line item name somebody wrote, so the same
    patterns are searched instead of matched - in the same order, which is what
    keeps "DOOH Video" a DOOH line.
    """
    flat = _flat(name)
    if not flat:
        return None
    for product, rx in LEADS:
        if re.search(rx, flat):
            return product
    return None


def served_impressions(text: str) -> dict:
    """{"by_product": {product: impressions}, "total": float,
        "unattributed": float}"""
    by_product: dict[str, float] = {}
    total = 0.0
    unattributed = 0.0
    for name, imps, _clicks in line_item_totals(text or ""):
        total += imps
        product = report_product(name)
        if product:
            by_product[product] = by_product.get(product, 0.0) + imps
        else:
            unattributed += imps
    return {"by_product": by_product, "total": total,
            "unattributed": unattributed}


def pacing_pct(served: float | None, ordered: float | None) -> float | None:
    """Her arithmetic: 100% - (served / ordered).

    Positive is under-delivery, negative is over. None when the comparison
    cannot be made rather than 0, which would read as perfectly on pace.
    """
    if served is None or not ordered:
        return None
    return 100.0 - (served / ordered * 100.0)


# Products that pace on money rather than impressions - and the list is short
# for a reason: it is exactly the three the report prints a spend tile for.
# Meta is bought on its own order field too, but the report shows no Meta
# spend, so pacing it on money would compare a number to nothing. Meta paces
# on impressions like everything else.
SPEND_PRODUCTS = ("Performance Max", "PPC", "LinkedIn")


def pacing_rows(text: str, ordered: dict) -> list[dict]:
    """One row per product the order bought, plus a total row for impressions.

    `ordered` is roster.ordered_for(): {product: {budget, impressions}}.
    """
    from .spend import report_spend

    served = served_impressions(text)
    spent = report_spend(text or "")
    rows: list[dict] = []

    for product in sorted(ordered):
        want = ordered[product]
        if product in SPEND_PRODUCTS:
            got = spent.get(product)
            rows.append({"product": product, "unit": "money",
                         "served": got, "ordered": want.get("budget"),
                         "pace": pacing_pct(got, want.get("budget"))})
            continue
        got = served["by_product"].get(product)
        rows.append({"product": product, "unit": "impressions",
                     "served": got, "ordered": want.get("impressions"),
                     "pace": pacing_pct(got, want.get("impressions"))})

    want_total = sum(v["impressions"] for p, v in ordered.items()
                     if p not in SPEND_PRODUCTS and v.get("impressions") is not None)
    if want_total or served["total"]:
        rows.append({"product": "All impressions", "unit": "impressions",
                     "served": served["total"] or None,
                     "ordered": want_total or None,
                     "pace": pacing_pct(served["total"] or None, want_total or None),
                     "total": True,
                     "unattributed": served["unattributed"]})
    return rows
