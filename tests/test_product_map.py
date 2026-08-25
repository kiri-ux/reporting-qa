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


# ------------------------------------------------------------ SKyPAC, order 51251
#
# Two live line items - "TikTok Display & Video Ads" and "YouTube+ Video Ads" -
# and the board said the client was running Video. Nothing in the export says
# Video; the word is in the FORMAT half of both names. The report was then
# failed twice: once for a Video product that did not exist, and once for the
# TikTok and YouTube that did.
def test_the_product_leads_the_name_and_the_format_follows_it():
    assert m("TikTok Display & Video Ads") == "TikTok"
    assert m("YouTube+ Video Ads") == "YouTube"
    assert m("Meta Display & Video Ads") == "Meta"
    assert m("Mobile Conquesting Display & Video Ads") == "Mobile Conquesting"


def test_skypacs_live_line_items_produce_no_phantom_video():
    live = ["TikTok Display & Video Ads", "YouTube+ Video Ads",
            "Social Mirror Ads", "Social Mirror CTV Ads"]
    assert {m(x) for x in live} == {"TikTok", "YouTube", "Social Mirror",
                                    "Social Mirror CTV"}


def test_social_mirror_ctv_is_its_own_product():
    """It is its own line item to order and its own widget on the report.

    Read as plain Social Mirror it vanished from the expected list, and the
    report's Social Mirror CTV pages came out as a product nobody had bought.
    """
    assert m("Social Mirror CTV Ads") == "Social Mirror CTV"
    assert m("Social Mirror Ads") == "Social Mirror"


def test_an_unseen_format_tail_does_not_change_the_product():
    """The IO tool grows spellings faster than any dictionary is updated."""
    for tail in ("", " Ads", " Display Ads", " Display & Video Ads",
                 " Video Ads", " Display, Video & Audio Ads"):
        assert m("TikTok" + tail) == "TikTok", tail
        assert m("Native Display" + tail) == "Native Display", tail


# ------------------------------------------------- what the finding actually says
from app.checks.rules import check_products


def _ctx(expected, found):
    return {"expected_products": set(expected), "products": set(found)}


def test_the_finding_names_only_the_difference():
    """It printed both full lists and left you to subtract them.

    "Expected from live orders and absent here: Video. On the report: Social
    Mirror, Social Mirror CTV, TikTok, YouTube." - five product names to read
    before you can see that the answer is Video.
    """
    out = check_products(_ctx(["Social Mirror", "Video"],
                              ["Social Mirror", "TikTok"]))
    titles = [f["title"] for f in out]
    assert "Ordered but not on the report: Video" in titles
    assert "On the report with no live order: TikTok" in titles
    # The products that matched are not what anybody is reading this line for.
    assert not any("Social Mirror" in t for t in titles)


def test_the_full_lists_are_still_there_behind_investigate():
    """"Why is that expected?" is a fair question - just not on the line whose
    job is to name the problem."""
    out = check_products(_ctx(["Video"], ["TikTok"]))
    trace = {r["label"]: r["value"] for r in out[0]["trace"]}
    assert trace["Live orders say"] == "Video"
    assert trace["Detected on the report"] == "TikTok"


def test_a_matching_report_raises_nothing():
    assert check_products(_ctx(["TikTok", "YouTube"], ["TikTok", "YouTube"])) == []


def test_no_order_list_means_no_claim():
    assert check_products({"expected_products": None, "products": {"Video"}}) == []


# ------------------------------------------- Bloomsburg Chevrolet, order 43852
#
# One line item, "CTV + Video Ads", and a report carrying both a CTV section
# and a Video section. Read as CTV alone, the Video was a product with no live
# order on a buy that was plainly both.
from app.checks.products import map_order_products as mp


def test_a_plus_in_the_product_name_means_two_products():
    assert mp("CTV + Video Ads") == ["CTV", "Video"]
    assert mp("Amazon Premium CTV + Video Ads") == ["CTV", "Video"]


def test_a_plus_that_belongs_to_the_name_is_not_a_separator():
    """"YouTube+ Video Ads" is one product whose name ends in a plus. Splitting
    on it would put a live YouTube order back under Video - the original bug."""
    assert mp("YouTube+ Video Ads") == ["YouTube"]
    assert mp("Search Engine Optimization+") == ["SEO"]


def test_an_ampersand_is_a_format_not_a_second_product():
    """"Display & Video Ads" is how one product is delivered, not two."""
    assert mp("Meta Display & Video Ads") == ["Meta"]
    assert mp("TikTok Display & Video Ads") == ["TikTok"]
    assert mp("Digital Out-Of-Home (DOOH) Display & Video Ads") == ["DOOH"]


def test_the_single_answer_is_still_the_first_one():
    assert m("CTV + Video Ads") == "CTV"
    assert m("Video Ads") == "Video"


# ------------------------------------------- Field Of Dreams, order 51118
#
# Reported three times, and right every time. One product on the order -
# Mobile Conquesting - and a report carrying a Display slice, a "Field Of
# Dreams - AI Display" line item and a Display Creative Performance widget.
# All of it IS the Mobile Conquesting buy: the order calls the product "Mobile
# Conquesting Display & Video Ads".
def test_a_product_is_not_rogue_when_an_ordered_product_prints_it():
    out = check_products(_ctx(["Mobile Conquesting"],
                              ["Display", "Mobile Conquesting"]))
    assert out == []


def test_the_same_holds_for_the_other_display_and_video_products():
    for product in ("Meta", "TikTok", "DOOH", "Performance Max"):
        assert check_products(_ctx([product], [product, "Display"])) == []
        assert check_products(_ctx([product], [product, "Video"])) == []


def test_a_real_rogue_is_still_caught():
    out = check_products(_ctx(["Mobile Conquesting"],
                              ["Mobile Conquesting", "TikTok"]))
    assert [f["title"] for f in out] == ["On the report with no live order: TikTok"]


def test_forgiving_a_format_does_not_make_it_expected():
    """A Mobile Conquesting order does not OWE a Display section. This rule
    only ever forgives a product on the report, never demands one."""
    out = check_products(_ctx(["Mobile Conquesting"], ["Mobile Conquesting"]))
    assert out == []


def test_social_mirror_is_not_on_the_list():
    """Its order name is just "Social Mirror Ads" - it does not say it delivers
    Display and Video, so nothing here claims it does. Kept deliberately narrow
    so this does not quietly stop catching real ones."""
    from app.checks.products import DELIVERS
    assert "Social Mirror" not in DELIVERS
    assert "CTV" not in DELIVERS
