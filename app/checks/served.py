"""What the report says was actually delivered, per product.

The order says what a month was bought to do; this is the other half of the
comparison. Impressions come off the Line Item Performance grid, which is the
only place in the text where the campaign is broken down far enough to attach
a number to a product. Money comes off the Spend Overview tiles.

ATTRIBUTION IS DELIBERATELY CAUTIOUS. A line item whose name maps to one
product counts toward that product. One that maps to none, or to more than
one, counts toward the total and nothing else - because "CTV + Video" served
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
    flat = 0.0
    for name, imps, _clicks in line_item_totals(text or ""):
        product = report_product(name)
        # A FLAT PRODUCT'S IMPRESSIONS ARE NOT PART OF THE DELIVERY TOTAL.
        # It has no goal and no row, so counting what it served put delivery
        # in the numerator that had nothing under it in the denominator.
        if product and not is_paced(product):
            flat += imps
            continue
        total += imps
        if product:
            by_product[product] = by_product.get(product, 0.0) + imps
        else:
            unattributed += imps
    return {"by_product": by_product, "total": total,
            "unattributed": unattributed, "flat": flat}


def pacing_pct(served: float | None, ordered: float | None) -> float | None:
    """How far off the order this delivery is, as a signed percentage.

    NEGATIVE IS SHORT. The arithmetic is the same distance from the order
    either way, but "+3% short" reads as three percent to the good - the sign
    has to agree with the word beside it.

    None when the comparison cannot be made, rather than 0, which would read as
    perfectly on pace.
    """
    if served is None or not ordered:
        return None
    return (served / ordered * 100.0) - 100.0


# Products that pace on money rather than impressions - and the list is short
# for a reason: it is exactly the three the report prints a spend tile for.
# Meta is bought on its own order field too, but the report shows no Meta
# spend, so pacing it on money would compare a number to nothing. Meta paces
# on impressions like everything else.
SPEND_PRODUCTS = ("Performance Max", "PPC", "LinkedIn")

# Bought by the month, not by delivery: there is no impression count to pace
# and no spend on the report to compare, so a row for them is a row of dashes.
#
# THESE NAMES HAVE TO BE THE PRODUCT NAMES. This was a hand-written list and
# "Visitor ID" is not what the product is called - the mapping calls it
# "Website Visitor ID" - so the one entry meant to keep it out never matched
# it, and every order carrying it got a row reading "-/- no comparison".
# Additional Billing was not in the list at all. Matching is on the flattened
# name now, so a near-miss like that cannot come back silently.
NOT_PACED = ("Live Chat", "SEO", "Website Video", "Reputation Management",
             "Website Visitor ID", "Additional Billing", "Geo-Framing")
_NOT_PACED = {_flat(p) for p in NOT_PACED}


def is_paced(product: str) -> bool:
    """False for anything sold flat - it has no delivery number to pace on.

    A grouped buy - "CTV, Video" - is one line item with one goal, so a flat
    product anywhere in it takes the whole row out, the same as before.
    """
    names = [product] + [x.strip() for x in (product or "").split(",")]
    return not any(_flat(p) in _NOT_PACED for p in names if p)

# A line with a week or less of the month behind it is not off pace, it is new.
# Pacing a three-day-old campaign against a month's goal says 99% short, every
# month, about every launch.
MIN_DAYS_TO_PACE = 7


def pacing_rows(text: str, ordered: dict) -> list[dict]:
    """One row per product the order bought, plus a total row for impressions.

    `ordered` is roster.ordered_for(): {product: {budget, impressions}}. For a
    lifetime those are the whole campaign's figures rather than one month's.
    """
    from .spend import report_spend

    served = served_impressions(text)
    spent = report_spend(text or "")
    rows: list[dict] = []

    for product in sorted(ordered):
        if not is_paced(product):
            continue
        want = ordered[product]
        # When the line went live, and how much of the month it had. A line
        # that started on the 28th cannot deliver a month's goal.
        when = {"started": want.get("started"), "days": want.get("days")}
        if product in SPEND_PRODUCTS:
            got = spent.get(product)
            rows.append({"product": product, "unit": "money",
                         "served": got, "ordered": want.get("budget"),
                         "basis": want.get("basis") or "",
                         "pace": pacing_pct(got, want.get("budget")), **when})
            continue
        # A grouped buy - "CTV, Video" - takes the delivery of both halves.
        parts = [x.strip() for x in product.split(",")]
        got = sum(served["by_product"].get(p, 0.0) for p in parts) or None
        rows.append({"product": product, "unit": "impressions",
                     "served": got, "ordered": want.get("impressions"),
                     "basis": want.get("basis") or "",
                     "pace": pacing_pct(got, want.get("impressions")), **when})

    # NOTHING WAS BOUGHT ON IMPRESSIONS, SO THERE IS NOTHING TO PACE ON THEM.
    #
    # A PPC-only order still serves impressions and the report still prints
    # them, but they are not what the month was sold on. "17,380/- no
    # comparison" sat above the spend row, putting the number nobody paces
    # where the eye lands first and making it look like a missing order figure.
    bought_impressions = any(r["unit"] != "money" for r in rows)

    # AND THE TOTAL COUNTS WHAT THE ROWS COUNT. It was summing every product
    # in the order, so a goal that had no row above it - a flat product - was
    # still in the denominator, and the total did not add up to the list it sat
    # under.
    want_total = sum(v["impressions"] for p, v in ordered.items()
                     if p not in SPEND_PRODUCTS and is_paced(p)
                     and v.get("impressions") is not None)
    if bought_impressions and (want_total or served["total"]):
        rows.append({"product": "All impressions", "unit": "impressions",
                     "served": served["total"] or None,
                     "ordered": want_total or None,
                     "pace": pacing_pct(served["total"] or None, want_total or None),
                     "total": True,
                     "unattributed": served["unattributed"],
                     "flat": served.get("flat") or 0.0})

    # IMPRESSIONS AND DOLLARS ARE TWO DIFFERENT QUESTIONS, so an order carrying
    # both gets two lists rather than one where the reader has to notice which
    # unit each row is in. Impressions first: most orders are bought that way.
    money = [r for r in rows if r["unit"] == "money"]
    imps = [r for r in rows if r["unit"] != "money"]
    if money:
        spent_total = sum(r["served"] for r in money if r["served"])
        want_money = sum(r["ordered"] for r in money if r["ordered"])
        if want_money or spent_total:
            money.append({"product": "All spend", "unit": "money",
                          "served": spent_total or None,
                          "ordered": want_money or None,
                          "pace": pacing_pct(spent_total or None,
                                             want_money or None),
                          "total": True})
    return imps + money
