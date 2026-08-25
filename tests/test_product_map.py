"""Which product an order line item is selling.

The bug this file exists for: the fallback walked the map in insertion order
and took the first key found anywhere inside the name. "video ads" sits inside
"YouTube+ Video Ads", so Allegheny Trucks' live YouTube+ order was recorded as
Video - and the report was failed twice over, once for a Video product that
was never running and once for the YouTube that was.
"""
from app.checks.products import (ORDER_PRODUCT_MAP, SECTION_PATTERNS,
                                 TAIL_PATTERNS, map_order_product as m)


def test_youtube_plus_is_youtube_not_video():
    assert m("YouTube+ Video Ads") == "YouTube"
    assert m("YouTube TV Ads") == "YouTube"
    assert m("YouTube Video Ads") == "YouTube"


def test_plain_video_is_still_video():
    assert m("Video Ads") == "Video"


def test_a_compound_name_keeps_its_own_product():
    assert m("Meta Display & Video Ads") == "Meta"
    assert m("Mobile Conquesting Display & Video Ads") == "Mobile Conquesting"
    assert m("Native Display Ads") == "Native Display"
    assert m("Social Mirror Ads") == "Social Mirror"


def test_the_longest_key_wins_not_the_first_one_written():
    """Insertion order is not a specificity ranking, and treating it as one is
    what put a YouTube order under Video."""
    longest = max(ORDER_PRODUCT_MAP, key=len)
    assert m(longest.title()) == ORDER_PRODUCT_MAP[longest]


def test_a_key_only_counts_on_whole_words():
    """Otherwise a three-letter key like "seo" matches inside anything."""
    assert m("Videos of Ads") != "Video"


def test_seo_is_recognised_even_though_it_has_no_report_section():
    assert m("Search Engine Optimization+") == "SEO"


def test_every_mapped_product_can_appear_on_a_report():
    """An order product the report can never name would be expected forever and
    never found - a permanent failure on every report that carries it. SEO is
    the one exception: it is delivered as its own report."""
    known = {p for p, _ in SECTION_PATTERNS} | {p for p, _ in TAIL_PATTERNS}
    unshowable = set(ORDER_PRODUCT_MAP.values()) - known
    assert unshowable == {"SEO"}, unshowable


def test_an_unknown_product_maps_to_nothing_rather_than_guessing():
    assert m("Skywriting Ads") is None
    assert m("") is None


# ------------------------------------------------------------------- DOOH
def test_the_io_tools_dooh_name_is_dooh():
    """"Digital Out-Of-Home (DOOH) Display & Video Ads" - hyphens, brackets,
    and a "Display & Video Ads" tail the generic Video key matched first. A
    billboard order came through as Video, the report's DOOH read as a product
    with no live order, and Video was counted twice over."""
    assert m("Digital Out-Of-Home (DOOH) Display & Video Ads") == "DOOH"
    assert m("Digital Out-Of-Home Display & Video Ads") == "DOOH"
    assert m("DOOH Display & Video Ads") == "DOOH"


def test_the_earliest_match_wins_not_the_longest():
    """Specificity is not length. "DOOH Display & Video Ads" carries both
    "dooh" and "video ads", and the product leads the name - the "Display &
    Video Ads" tail is the format that follows it."""
    assert m("DOOH Display & Video Ads") == "DOOH"
    assert m("Mobile Conquesting Display & Video Ads") == "Mobile Conquesting"
    assert m("Meta Display & Video Ads") == "Meta"
    assert m("Amazon Premium CTV + Video Ads") == "CTV"


def test_punctuation_does_not_hide_a_product():
    """Hyphens and brackets are the IO tool's, not part of the product."""
    assert m("PAY-PER-CLICK ADS") == "PPC"
    assert m("Connected TV (CTV) Ads") == "CTV"


def test_every_product_on_a_real_order_maps_to_something():
    """Order 52753, as it actually reads. An unmapped line is dropped at import
    and its product is then "on the report with no live order"."""
    for name in ("Amazon Premium CTV + Video Ads", "Connected TV Ads",
                 "Video Ads", "Digital Out-Of-Home (DOOH) Display & Video Ads",
                 "YouTube+ Video Ads", "Meta Display & Video Ads"):
        assert m(name) is not None, name
