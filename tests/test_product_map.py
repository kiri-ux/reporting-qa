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
