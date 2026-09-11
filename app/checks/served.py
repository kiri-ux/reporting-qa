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


# WHAT A REPORT CALLS THINGS THAT THE ORDER SPELLS OUT.
#
# An order says "Mobile Conquesting Display & Video Ads". The report's line
# items say "Close Lumber - Geo-Retargeting Mobile" - the product is one word
# at the end, and the order's own pattern needs both words, so every Mobile
# Conquesting line on every report matched nothing at all.
NICKNAMES: list[tuple[str, str]] = [
    ("Mobile Conquesting", r"\bmobile\b"),
    ("CTV", r"\bott\b"),
    ("Native Display", r"\bnativ\w*\b"),
]

# The last resort. These are formats, not products: an order sells "Mobile
# Conquesting Display & Video Ads", so a line item saying Display might be any
# of three products and only means Display when nothing better is in the name.
GENERIC = {r"video\b", r"display\b", r"audio\b"}


def _leads() -> tuple[list, list, list]:
    """The vocabulary in three tiers, most specific first.

    THE TIER IS THE SPECIFICITY RULE, and it used to be the position in one
    flat list - which put the one-word nicknames ahead of the full product
    names that come later in the order's own list. So "Collective Heads - KLOS
    - Cross Platform Mobile Retargeting Social Mirror" matched `mobile` and was
    filed as Mobile Conquesting, taking 37,128 impressions off Social Mirror
    and putting them on a product that had not served them. Social Mirror read
    91% short of its order and Mobile Conquesting 73% over.

    A name that says Social Mirror is a Social Mirror line however many other
    words are in it. A nickname only decides a name that names nothing.
    """
    named = [(p, rx) for p, rx in PRODUCT_LEADS if rx not in GENERIC]
    generic = [(p, rx) for p, rx in PRODUCT_LEADS if rx in GENERIC]
    return named, list(NICKNAMES), generic


LEADS = _leads()


def report_product(name: str) -> str | None:
    """Which product a line item on the REPORT belongs to, or None.

    The order's patterns are anchored to the front of a product name. Here the
    product is somewhere inside a line item name somebody wrote, so the same
    patterns are searched instead of matched - in tier order, which is what
    keeps "DOOH Video" a DOOH line and "Retargeting Mobile" a Mobile
    Conquesting one.
    """
    flat = _flat(name)
    if not flat:
        return None
    for tier in LEADS:
        for product, rx in tier:
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
        # AND NEITHER ARE A SPEND-ONLY PRODUCT'S.
        #
        # Performance Max and PPC are bought in dollars: they have no
        # impression goal, so want_total already leaves them out. Counting
        # their impressions on the served side put 218,084 of Peters - Troy's
        # PMax delivery in the numerator against an 80,000 goal that was Online
        # Audio's alone, and the row read "+226% over".
        if product and (not is_paced(product) or product in SPEND_PRODUCTS):
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


def days_in_month(period: str | None) -> int | None:
    """How many days there are in "2026-08". None when it cannot be read."""
    import calendar

    if not period:
        return None
    try:
        y, m = (int(x) for x in str(period).split("-")[:2])
        return calendar.monthrange(y, m)[1]
    except (ValueError, IndexError, calendar.IllegalMonthError):
        return None


def pro_rata(goal, days, period) -> tuple[float | None, int | None]:
    """The month's goal cut to the days the product actually had.

    A MONTHLY GOAL IS A RATE, NOT A TARGET FOR THE CALENDAR MONTH. Kermit
    Celebration Days launched on 20 and 27 August and its report read "62%
    short" across the board - Display 59,323 against 150,000, Mobile
    Conquesting 39,222 against 100,000, CTV and Video 32,365 against 160,000.
    Every one of those is a full month's goal charged to a campaign that had
    twelve days, or five. Against the days they actually ran, all four are on
    pace or slightly ahead, and the report that read as a disaster was fine.

    Returns (goal for those days, days in the month). The goal comes back
    untouched, with None beside it, whenever there is nothing to cut it by - a
    lifetime carries no day count, and a line that ran the whole month is
    already being measured against the right figure.
    """
    if goal is None or not days:
        return goal, None
    in_month = days_in_month(period)
    if not in_month or days >= in_month:
        return goal, None
    return float(goal) * days / in_month, in_month


def pro_rata_note(full, days, in_month, started, money=False) -> str:
    """"150,000 a month · 12 of 31 days from Aug 20" - what the row is cut
    from, said on the row rather than left in a tooltip."""
    if not in_month or full is None:
        return ""
    figure = f"${full:,.0f}" if money else f"{full:,.0f}"
    note = f"{figure} a month · {days} of {in_month} days"
    if started:
        note += f" from {started.strftime('%b %-d')}"
    return note


def pacing_rows(text: str, ordered: dict, period: str | None = None) -> list[dict]:
    """One row per product the order bought, plus a total row for impressions.

    `ordered` is roster.ordered_for(): {product: {budget, impressions}}. For a
    lifetime those are the whole campaign's figures rather than one month's.

    WITH `period`, A GOAL IS CUT TO THE DAYS THE PRODUCT ACTUALLY HAD. See
    pro_rata. Without it, and on a lifetime - which carries no day count - the
    figures are the ones the order states.
    """
    from .spend import report_spend

    # A CANCELLED BUY IS NOT PACED, AND THE PANEL HAS TO AGREE WITH THE CHECKS
    # ABOUT THAT. Both pacing checks drop these rows already - a cancelled buy
    # is not short of a goal that stopped being asked for the day somebody
    # called it off - but the panel was building them anyway. Kerr-Bilt's
    # cancelled PPC sat in the spend list as "-/$2,800 no comparison" with its
    # money inside "All spend $412/$4,800", so the report read 91% short of a
    # figure more than half of which had been cancelled.
    ordered = {k: v for k, v in ordered.items() if not v.get("stopped")}
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
            full = want.get("budget")
            goal, in_month = pro_rata(full, when["days"], period)
            rows.append({"product": product, "unit": "money",
                         "served": got, "ordered": goal, "full": full,
                         "in_month": in_month,
                         "month_note": pro_rata_note(full, when["days"],
                                                     in_month, when["started"],
                                                     money=True),
                         "basis": want.get("basis") or "",
                         "pace": pacing_pct(got, goal), **when})
            continue
        # A grouped buy - "CTV, Video" - takes the delivery of both halves.
        parts = [x.strip() for x in product.split(",")]
        got = sum(served["by_product"].get(p, 0.0) for p in parts) or None
        full = want.get("impressions")
        goal, in_month = pro_rata(full, when["days"], period)
        rows.append({"product": product, "unit": "impressions",
                     "served": got, "ordered": goal, "full": full,
                     "in_month": in_month,
                     "month_note": pro_rata_note(full, when["days"], in_month,
                                                 when["started"]),
                     "basis": want.get("basis") or "",
                     "pace": pacing_pct(got, goal), **when})

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
    #
    # AND IT ADDS UP THE ROWS' OWN GOALS, so a total under four pro-rated rows
    # is not a full month's goal. Kermit's four products were each cut to the
    # twelve or five days they ran and the total underneath still said 440,000,
    # which is 62% short of nothing in particular.
    want_total = sum(r["ordered"] for r in rows
                     if r["unit"] != "money" and r.get("ordered") is not None)
    full_total = sum(r.get("full") or 0.0 for r in rows if r["unit"] != "money")
    if bought_impressions and (want_total or served["total"]):
        rows.append({"product": "All impressions", "unit": "impressions",
                     "served": served["total"] or None,
                     "ordered": want_total or None,
                     "full": full_total or None,
                     "month_note": (f"{full_total:,.0f} a month across the products above"
                                    if full_total and round(full_total) != round(want_total)
                                    else ""),
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
        full_money = sum(r.get("full") or 0.0 for r in money)
        if want_money or spent_total:
            money.append({"product": "All spend", "unit": "money",
                          "served": spent_total or None,
                          "ordered": want_money or None,
                          "full": full_money or None,
                          "month_note": (f"${full_money:,.0f} a month across the "
                                         f"products above"
                                         if full_money
                                         and round(full_money) != round(want_money)
                                         else ""),
                          "pace": pacing_pct(spent_total or None,
                                             want_money or None),
                          "total": True})
    return imps + money
