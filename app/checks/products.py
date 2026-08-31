"""Work out which products a report actually contains.

Two signals, in order of trust:

1. **Section titles.** "Native Display Creative Performance" is unambiguous.
2. **Line-item name suffixes.** "... Geo-Retargeting Mobile" is Mobile
   Conquesting, "... AI Audio" is Online Audio.

Raw text search is deliberately not used: every report carries a footnote
mentioning CTV and TikTok whether or not either product is on the buy.
"""
from __future__ import annotations

import re

# Section title fragment -> product. Longest first so "Social Mirror CTV" wins
# over "Social Mirror".
SECTION_PATTERNS: list[tuple[str, str]] = [
    ("Social Mirror CTV", r"Social Mirror CTV"),
    ("Native Display", r"Native Display (?:Creative|Click|Conversion)"),
    ("Performance Max", r"Performance Max"),
    ("Mobile Conquesting", r"Mobile Conquesting"),
    ("Social Mirror", r"Social Mirror"),
    ("Online Audio", r"Online Audio"),
    ("CTV", r"Connected TV \(CTV\)"),
    ("Meta", r"^Meta\b"),
    ("TikTok", r"^TikTok\b"),
    ("DOOH", r"^DOOH\b"),
    ("PPC", r"^PPC\b"),
    ("YouTube", r"^YouTube\b"),
    ("Live Chat", r"^Live Chat\b"),
    ("Video", r"^Video (?:Creative|Completion|Click|View-through)"),
    ("Display", r"^Display (?:Creative|Click|Conversion)"),
]

# Line-item name suffix -> product. Order matters.
TAIL_PATTERNS: list[tuple[str, str]] = [
    # DOOH first. Its line items are named "... Venue Targeting DOOH Video"
    # and "... DOOH Display", so the generic Video and Display tails claimed
    # them - which put a billboard campaign on the report as Video, told the
    # order check it was running a product it was not, and would have demanded
    # a completion rate for something nobody watches to the end.
    ("DOOH", r"\bDOOH(?: Video| Display)?$"),
    ("Social Mirror CTV", r"Social Mirror CTV$"),
    ("Native Display", r"Native Display$"),
    ("Native Display", r"\bNative$"),
    ("Performance Max", r"Performance Max$"),
    ("Online Audio", r"\bAudio$"),
    ("Mobile Conquesting", r"\bMobile$"),
    ("Social Mirror", r"Social Mirror$"),
    ("CTV", r"\bCTV$"),
    ("Video", r"\bVideo$"),
    ("Display", r"\bDisplay$"),
    ("PPC", r"\bPPC$"),
    ("Meta", r"\bMeta$"),
    ("TikTok", r"\bTikTok$"),
    ("YouTube", r"\bYouTube$"),
]

# What the order tool calls a product -> what the report calls it.
ORDER_PRODUCT_MAP = {
    "mobile conquesting display & video ads": "Mobile Conquesting",
    "meta display & video ads": "Meta",
    "native display ads": "Native Display",
    "display ads": "Display",
    "social mirror ads": "Social Mirror",
    # Sold as its own line item, and the report gives it its own widgets, so it
    # is its own product on both sides. Read as plain Social Mirror it went
    # missing from the expected list entirely.
    "social mirror ctv ads": "Social Mirror CTV",
    "social mirror ctv": "Social Mirror CTV",
    "video ads": "Video",
    "online audio ads": "Online Audio",
    "connected tv ads": "CTV",
    "connected tv": "CTV",
    "ctv": "CTV",
    "amazon premium ctv + video ads": "CTV",
    "youtube video ads": "YouTube",
    # All three are the YouTube section of the report. Without them the
    # fallback found "video ads" inside "YouTube+ Video Ads" and filed a live
    # YouTube order under Video.
    "youtube+ video ads": "YouTube",
    "youtube tv ads": "YouTube",
    "search engine optimization+": "SEO",
    "pay-per-click ads": "PPC",
    "performance max ads": "Performance Max",
    "tiktok ads": "TikTok",
    # The IO tool writes this one as "Digital Out-Of-Home (DOOH) Display &
    # Video Ads". Hyphens, brackets, and a "Display & Video Ads" tail that the
    # generic Video key matched first - so a billboard order came through as
    # Video, the report's DOOH read as a product with no live order, and Video
    # was counted twice over.
    "digital out of home ads": "DOOH",
    "digital out of home": "DOOH",
    "dooh": "DOOH",
    "live chat": "Live Chat",
    "seo": "SEO",
}

# Products that never appear in the standard monthly report, so their absence is
# not a finding. SEO is delivered as its own report.
NOT_IN_MONTHLY_REPORT = {"SEO"}

# WHAT A PRODUCT PRINTS ON THE REPORT BESIDES ITSELF.
#
# Field Of Dreams runs one product - Mobile Conquesting - and its report
# carries a Display slice, a "Field Of Dreams - AI Display" line item and a
# Display Creative Performance widget. All of it IS the Mobile Conquesting buy:
# the order calls the product "Mobile Conquesting Display & Video Ads", and
# Display and Video are the formats it is delivered in, not other products
# somebody bought.
#
# Read literally, the report was carrying "Display with no live order" - three
# times, on a report that was right every time.
#
# This is used ONLY to forgive a product on the report. It never makes one
# expected: a Mobile Conquesting order does not owe a Display section.
#
# The membership is taken from what the order tool calls each product. Every
# one of these is sold as "... Display & Video Ads".
DELIVERS = {
    "Mobile Conquesting": {"Display", "Video"},
    "Meta": {"Display", "Video"},
    "TikTok": {"Display", "Video"},
    "DOOH": {"Display", "Video"},
    "Performance Max": {"Display", "Video"},
    "Native Display": {"Display"},
}


# ONE LINE ITEM, EITHER PRODUCT, NOT NECESSARILY BOTH.
#
# "CTV + Video Ads" is a buy that runs both, and its report carries both. Amazon
# Premium is sold the same way on paper - "Amazon Premium CTV + Video Ads" - but
# an Amazon buy can deliver all its impressions through one of the two, so a
# report showing CTV and no Video is a normal Amazon month, not a missing
# product. Window World's July was exactly that.
#
# So this product's two halves are ONE expectation with two acceptable answers:
# the report owes CTV or Video, and either satisfies it. Both are still allowed
# on the report, and if NEITHER turns up that is still a finding.
ANY_OF: list[tuple[str, frozenset]] = [
    ("amazon premium", frozenset({"CTV", "Video"})),
]


def any_of_groups(raw_names) -> list[frozenset]:
    """The either-or expectations these order line names carry."""
    out: list[frozenset] = []
    for name in raw_names or ():
        flat = _flat(name)
        for needle, grp in ANY_OF:
            if needle in flat and grp not in out:
                out.append(grp)
    return out


def formats_covered(expected) -> set[str]:
    """Everything the expected products print in their own right."""
    out: set[str] = set()
    for p in expected or ():
        out |= DELIVERS.get(p, set())
    return out

# What the name STARTS with, when the exact name is not in the map above. The
# IO tool grows new spellings faster than anybody updates a dictionary -
# "TikTok Display & Video Ads", "Digital Out-Of-Home (DOOH) Display & Video
# Ads" - and every one of them ends in a format the generic keys match. Read
# left to right and the answer is never in doubt.
#
# Order matters: the longer name first wherever one leads with the other
# ("social mirror ctv" before "social mirror", "connected tv" before "ctv").
PRODUCT_LEADS: list[tuple[str, str]] = [
    ("DOOH", r"(?:digital out of home|dooh)\b"),
    ("Mobile Conquesting", r"mobile conquesting\b"),
    ("Native Display", r"native display\b"),
    ("Performance Max", r"performance max\b"),
    ("Social Mirror CTV", r"social mirror ctv\b"),
    ("Social Mirror", r"social mirror\b"),
    ("Online Audio", r"online audio\b"),
    ("CTV", r"(?:amazon premium|connected tv|ctv)\b"),
    ("YouTube", r"youtube\+?\b"),
    ("TikTok", r"tiktok\b"),
    ("Meta", r"(?:meta|facebook|instagram)\b"),
    # LINKEDIN WAS NEVER IN HERE. It is a product the tool knows about
    # everywhere else - the export carries monthly_linkedin_ad_spend, the
    # device check excludes it by name - and the map had no entry, so every
    # LinkedIn line item was thrown out of the order list as an unmapped
    # product. Credit Union Audit Group sells nothing else, so the client
    # vanished from the board entirely while delivering 31 days a month.
    ("LinkedIn", r"linkedin\b"),
    # Sold, billed, and never on a report. See NOT_ON_A_REPORT below.
    ("Website Visitor ID", r"website visitor id\b"),
    ("Additional Billing", r"additional billing\b"),
    ("PPC", r"(?:pay per click|ppc|google ads)\b"),
    ("SEO", r"(?:search engine optimization|seo)\b"),
    ("Live Chat", r"live chat\b"),
    ("Video", r"video\b"),
    ("Display", r"display\b"),
    ("Online Audio", r"audio\b"),
]



PUNCT = re.compile(r"[^a-z0-9+]+")


def _flat(s: str) -> str:
    """Lowercase, punctuation to single spaces.

    "Digital Out-Of-Home (DOOH) Display & Video Ads" and "digital out of home"
    only meet each other once the hyphens and brackets are gone.
    """
    return PUNCT.sub(" ", (s or "").lower()).strip()


def map_order_product(name: str) -> str | None:
    """The product an order line item is selling.

    The fallback used to walk the map in insertion order and take the first
    key that appeared anywhere inside the name. "video ads" sits inside
    "YouTube+ Video Ads", so a live YouTube+ order was recorded as Video - and
    the report was then failed twice over, once for a Video product that was
    never running and once for the YouTube that was.

    So the fallback goes longest key first, and a key only counts when it lands
    on whole words. "video ads" no longer wins inside "youtube+ video ads",
    because "youtube+ video ads" is longer and is tried first.
    """
    got = map_order_products(name)
    return got[0] if got else None


# A product name can name two products. " + " is the IO tool's word for "and":
# "CTV + Video Ads" is one line item selling both, and the report carries a CTV
# section and a Video section for it. Read as CTV alone, the report's Video was
# a product with no live order.
#
# Matched with spaces around it on purpose. "YouTube+ Video Ads" is one product
# whose name happens to end in a plus, and splitting that would put a live
# YouTube order back under Video - the bug this whole file exists for.
PLUS = re.compile(r"\s\+\s")


def map_order_products(name: str) -> list[str]:
    """Every product an order line item is selling, in the order they appear."""
    key = _flat(name)
    if not key:
        return []
    parts = [p.strip() for p in PLUS.split(key) if p.strip()]
    if len(parts) > 1:
        out: list[str] = []
        for part in parts:
            got = _map_one(part)
            if got and got not in out:
                out.append(got)
        if out:
            return out
    got = _map_one(key)
    return [got] if got else []


def _map_one(key: str) -> str | None:
    flat = {_flat(k): v for k, v in ORDER_PRODUCT_MAP.items()}
    if key in flat:
        return flat[key]

    # The product LEADS the name; whatever follows is the format it is sold in.
    # "TikTok Display & Video Ads" is TikTok, and no amount of matching inside
    # the tail gets that right, because "video ads" really is in there. So the
    # opening words are read on their own, before anything else is tried.
    for product, rx in PRODUCT_LEADS:
        if re.match(rx, key):
            return product

    # Earliest match wins, longest breaks the tie. Specificity is not length:
    # "DOOH Display & Video Ads" contains both "dooh" and "video ads", and
    # sorting by length alone handed a billboard order to Video. The product
    # leads the name; "Display & Video Ads" is the format that follows it.
    best = None
    for order_name in flat:
        at = _at(order_name, key)
        if at is None:
            at = 0 if _whole(key, order_name) else None
        if at is None:
            continue
        rank = (at, -len(order_name))
        if best is None or rank < best[0]:
            best = (rank, flat[order_name])
    return best[1] if best else None


def _at(needle: str, haystack: str):
    """Where `needle` starts in `haystack` on word boundaries, or None."""
    m = re.search(r"(?<![a-z0-9])" + re.escape(needle) + r"(?![a-z0-9])", haystack)
    return m.start() if m else None


def _whole(needle: str, haystack: str) -> bool:
    """Is `needle` in `haystack` on word boundaries?

    Without this "seo" matches inside "video ads" the moment the map grows a
    short key, which is the same class of bug one letter smaller.
    """
    return re.search(r"(?<![a-z0-9])" + re.escape(needle) + r"(?![a-z0-9])",
                     haystack) is not None


def detect(text: str, tables) -> set[str]:
    """The products this report actually shows.

    Two kinds of evidence: a widget section - "Mobile Conquesting Creative
    Performance", "Display Creative Performance" - and a line item name ending
    in its product. Both count.

    The tails looked like a liability for a while: Mobile Conquesting IS a
    display and video product, so its line items are named "... - AI Display",
    and a report running nothing but Mobile Conquesting could in principle be
    credited with Display on that alone. It has not happened. Every report
    checked so far - seven real ones and the 317-page sample - had its products
    fully covered by the sections, with the tails adding nothing, and the one
    report that looked like the bug turned out to carry a real Display
    Creative Performance widget. So the tails stay: on a template that prints
    no section titles they would be the only evidence there is.
    """
    found: set[str] = set()

    titles = [(t.title or "").strip() for t in tables if (t.title or "").strip()]
    # Live Chat and a few others head a page without forming a metric table.
    for line in text.split("\n"):
        s = line.strip()
        if s and not s.startswith("*") and len(s) < 80 and (
                "Performance" in s or "Submission Details" in s):
            titles.append(s)

    for title in titles:
        for product, rx in SECTION_PATTERNS:
            if re.search(rx, title):
                found.add(product)
                break

    from .rules import LINE_ITEM
    for t in tables:
        if not LINE_ITEM.search(t.title or ""):
            continue
        for name, _v in t.body:
            n = (name or "").strip()
            for product, rx in TAIL_PATTERNS:
                if re.search(rx, n, re.I):
                    found.add(product)
                    break
    return found


# ---------------------------------------------------------------- what a product
# owes, and whether it owes a report at all
#
# NOT EVERYTHING ON AN ORDER IS A THING A CLIENT READS ABOUT.
#
# Website Visitor ID and Additional Billing are real line items that are
# invoiced and never appear on a report - there is no widget for them and there
# never will be. Left in the expected set they fail every report they are on
# for missing a section that cannot exist; taken out of the order list
# entirely, their client disappears from the board. They are kept, and they
# are quiet.
NOT_ON_A_REPORT = {"Website Visitor ID", "Additional Billing"}

# AND SOME PRODUCTS DO NOT EARN A REPORT ON THEIR OWN.
#
# Live Chat belongs on a report and is only ever sold alongside another digital
# product, so it rides along - a client running Live Chat and nothing else is
# not owed a report for it. SEO is pulled outside TapClicks and has always been
# handled by hand.
RIDES_ALONG = {"Live Chat"}


def on_a_report(product: str) -> bool:
    """Should this product appear on the client's report at all?"""
    return bool(product) and product not in NOT_ON_A_REPORT


def earns_a_report(product: str) -> bool:
    """Is this product, on its own, a reason to owe the client a report?

    A client running nothing but Website Visitor ID, Additional Billing or Live
    Chat is not owed one. Live Chat is the interesting case: it belongs ON a
    report, it just never brings one with it.
    """
    return on_a_report(product) and product not in RIDES_ALONG
