"""Product abbreviations and their colors, from the Vici product legend.

The order list names products the long way ("Mobile Conquesting Display &
Video Ads") while the condensed client view has room for two or three
characters. This is the lookup, and it is deliberately data rather than logic
so adding a product is one line.

MATCHING IS IN LIST ORDER, MOST SPECIFIC FIRST - not by alias length.

Sorting by alias length looks right and is wrong: "display" is seven
characters and "tiktok" is six, so "TikTok Display & Video Ads" files itself
under Display Ads. The generic words are often the longest ones. So the list
below is ordered by how specific each product is, the compound and branded
products first and the bare Display/Video catch-alls last, and the first
product with any alias present in the string wins.

Keep new products above D and V.
"""
from __future__ import annotations

import re

# (code, hex, full name, aliases). The alias list is what gets matched; the
# full name is only shown in the tooltip.
PRODUCTS: list[tuple[str, str, str, tuple[str, ...]]] = [
    # --- compound and management variants, before their own base products ---
    ("MCE",  "#cceae8", "Mobile Conquesting EVENT / POLITICAL",
     ("mobile conquesting event", "mobile conquesting political", "mobile conquesting evt")),
    ("MM",   "#A0C5E8", "Meta Display & Video Ads (Mgmt)",
     ("meta display and video ads (mgmt)", "meta ads (mgmt)", "meta (mgmt)",
      "meta mgmt", "meta management")),
    ("PMM",  "#f3b9ed", "Performance Max Ads (Mgmt)",
     ("performance max ads (mgmt)", "performance max (mgmt)", "performance max mgmt")),
    ("ML",   "#61a6ef", "Meta Lead Display & Video Ads",       ("meta lead",)),
    ("SMC",  "#9966CC", "Social Mirror CTV Ads",               ("social mirror ctv",)),
    ("CV",   "#008080", "CTV + Video Ads",                     ("ctv + video", "ctv and video")),
    ("AV",   "#fee6ce", "Amazon Premium Video & OTT Ads",
     ("amazon premium video", "amazon premium (with twitch)", "twitch")),
    ("AD",   "#fd8c52", "Amazon Premium Display Ads",          ("amazon premium display", "amazon premium")),
    # --- branded and named products ---
    ("MC",   "#befd1c", "Mobile Conquesting Display & Video",  ("mobile conquesting", "mobile conquest")),
    ("NV",   "#a14796", "Native Video Ads",                    ("native video",)),
    ("ND",   "#bf3a7a", "Native Display Ads",                  ("native display", "native")),
    ("GF",   "#2E8B57", "Geo-Framing Display Ads",             ("geo-framing", "geo framing", "geoframing")),
    ("TT",   "#6febe6", "TikTok Display & Video Ads",          ("tiktok", "tik tok")),
    ("LI",   "#87CEEB", "LinkedIn Ads",                        ("linkedin", "linked in")),
    ("DY",   "#98FB98", "Dynamic Ads",                         ("dynamic",)),
    ("DOOH", "#D8BFD8", "Digital Out-Of-Home Display & Video",
     ("dooh", "digital out-of-home", "digital out of home", "out of home")),
    ("ID",   "#e6e827", "Website Visitor ID",                  ("website visitor id", "visitor id")),
    ("OA",   "#f6dc75", "Online Audio Ads",                    ("online audio", "audio")),
    ("YT",   "#fe0908", "YouTube Video Ads",                   ("youtube", "you tube")),
    ("SEO",  "#FFDFE9", "Search Engine Optimization+",         ("search engine optimization", "seo")),
    ("ORM",  "#bbaefe", "Online Reputation Management",        ("online reputation", "reputation management")),
    ("LC",   "#FFDAB9", "Live Chat",                           ("live chat",)),
    ("PM",   "#fa8bed", "Performance Max Ads",                 ("performance max",)),
    ("PPC",  "#fcb500", "Pay-Per-Click Ads",                   ("pay-per-click", "pay per click", "ppc")),
    ("SM",   "#E0B0FF", "Social Mirror Ads",                   ("social mirror",)),
    ("M",    "#006ae3", "Meta Display & Video Ads",            ("meta display", "meta video", "meta")),
    ("CTV",  "#7FFFD4", "Connected TV Ads",                    ("connected tv", "ott/ctv", "ott", "ctv")),
    # --- generic catch-alls, always last ---
    ("V",    "#ff4d45", "Video Ads",                           ("video",)),
    ("D",    "#fb8b76", "Display Ads",                         ("display",)),
]

BY_CODE = {c: (hexv, name) for c, hexv, name, _ in PRODUCTS}

UNKNOWN_COLOR = "#E6EAEE"


def _norm(s: str) -> str:
    s = (s or "").lower().replace("&", " and ")
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9+/() -]", " ", s)).strip()


# Aliases normalised the same way product names are, so "&" vs "and" and
# stray punctuation cannot silently stop a match.
_MATCHERS: list[tuple[str, tuple[str, ...]]] = [
    (code, tuple(sorted((_norm(a) for a in aliases), key=len, reverse=True)))
    for code, _h, _n, aliases in PRODUCTS
]


def code_for(product: str) -> str:
    """Abbreviation for a product name, or a short fallback if unrecognised."""
    n = _norm(product)
    if not n:
        return ""
    for code, aliases in _MATCHERS:          # list order = specificity order
        if any(a in n for a in aliases):
            return code
    # Not in the legend: initials, so an unknown product is still readable
    # rather than silently vanishing from the client view.
    words = [w for w in re.split(r"[ /-]+", n) if w and w != "ads"]
    return ("".join(w[0] for w in words[:3]) or n[:3]).upper()


def ink_on(hex_color: str) -> str:
    """Black or white text, whichever is legible on this background.

    Half the legend is pastel and half is saturated, so a single text color
    would be unreadable on one end or the other. sRGB relative luminance,
    per WCAG.
    """
    h = hex_color.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    try:
        r, g, b = (int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))
    except ValueError:
        return "#212121"

    def lin(c: float) -> float:
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

    lum = 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b)
    return "#212121" if lum > 0.42 else "#FFFFFF"


def pill(product: str) -> dict:
    """Everything a template needs to draw one product chip."""
    code = code_for(product)
    hexv, name = BY_CODE.get(code, (UNKNOWN_COLOR, product or "Unknown"))
    return {"code": code, "bg": hexv, "fg": ink_on(hexv),
            "name": name, "known": code in BY_CODE}
