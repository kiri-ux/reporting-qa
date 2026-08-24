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
    ("DOOH", r"\bDOOH(?: Display)?$"),
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
    "amazon premium ctv + video ads": "CTV",
    "youtube video ads": "YouTube",
    "pay-per-click ads": "PPC",
    "performance max ads": "Performance Max",
    "tiktok ads": "TikTok",
    "digital out of home ads": "DOOH",
    "live chat": "Live Chat",
    "seo": "SEO",
}

# Products that never appear in the standard monthly report, so their absence is
# not a finding. SEO is delivered as its own report.
NOT_IN_MONTHLY_REPORT = {"SEO"}


def map_order_product(name: str) -> str | None:
    key = re.sub(r"\s+", " ", (name or "").strip().lower())
    if key in ORDER_PRODUCT_MAP:
        return ORDER_PRODUCT_MAP[key]
    for order_name, product in ORDER_PRODUCT_MAP.items():           # partial fallback
        if order_name in key or key in order_name:
            return product
    return None


def detect(text: str, tables) -> set[str]:
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
