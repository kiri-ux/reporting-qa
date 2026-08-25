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
    key = _flat(name)
    if not key:
        return None
    flat = {_flat(k): v for k, v in ORDER_PRODUCT_MAP.items()}
    if key in flat:
        return flat[key]
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
