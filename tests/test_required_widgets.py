"""The widget rules, checked against the everything-sample.

The sample is a 317-page report that carries every widget TapClicks can
produce, so it is the one document that must come out completely clean. If a
rule fires on it, the rule is wrong - not the report.
"""
from pathlib import Path

import pytest

from app.checks.rules import (BARCK, REQUIRED_WIDGETS, _rule_applies, _sections,
                              check_required_widgets)

SAMPLE = Path("/root/work/sample.txt")

TITLES = [
    "Top CTV Publishers",
    "Site and App Performance",
    "Amazon Inventory Source Performance",
    "Amazon Premium Site and App Performance",
    "YouTube+ Placement Performance",
]

# THE CHANNEL LIST IS ONE WIDGET UNDER TWO NAMES. TapClicks writes it "Top 10
# YouTube TV Channel Performance" on a TV buy and drops the TV elsewhere, so
# either spelling satisfies it and neither is dropped on its own.
CHANNEL_TITLES = ["Top 10 YouTube TV Channel Performance",
                  "Top 10 YouTube Channel Performance"]


@pytest.fixture(scope="module")
def sample() -> str:
    if not SAMPLE.exists():
        pytest.skip("everything-sample not present")
    return SAMPLE.read_text()


def _drop(text: str, title: str) -> str:
    return "\n".join(l for l in text.split("\n") if l.strip() != title)


def test_sample_is_clean(sample):
    assert check_required_widgets({"text": sample, "products": set()}) == []


@pytest.mark.parametrize("title", TITLES)
def test_removing_a_widget_fails_the_report(sample, title):
    out = check_required_widgets({"text": _drop(sample, title), "products": set()})
    assert [f for f in out if title in f["title"]], f"{title} went unnoticed"
    assert all(f["severity"] == "fail" for f in out)


def test_ctv_and_social_mirror_ctv_share_one_publishers_widget(sample):
    """Two products, one widget: the report prints a single Top CTV Publishers
    list across both. Asking for one each failed a report that carried
    everything it owed."""
    text = sample.replace("Top CTV Publishers", "Something Else", 1)
    out = check_required_widgets({"text": text, "products": set()})
    assert [f["title"] for f in out] == []

    # None at all is still a failure.
    text = sample.replace("Top CTV Publishers", "Something Else")
    out = check_required_widgets({"text": text, "products": set()})
    assert [f["title"] for f in out] == ["No Top CTV Publishers widget"]


def test_amazon_widget_does_not_satisfy_the_generic_one():
    """"Amazon Premium Site and App Performance" contains the generic title.

    A substring search would let an Amazon-only report claim it carries the
    site and app breakout that BARCK+ owes, which is the whole reason headings
    are compared as whole lines.
    """
    text = ("AMAZON ADS - PAGE 1\n"
            "GEOS - PAGE 1\nBARCK+ Zip Code Performance\n"
            "Amazon Inventory Source Performance\n"
            "Amazon Premium Site and App Performance\n")
    out = check_required_widgets({"text": text, "products": set()})
    assert [f["title"] for f in out] == ["No Site and App Performance widget"]


def test_youtube_tv_only_does_not_owe_the_youtube_plus_widgets():
    """"unless we're only running YouTube TV" - the report says which it is."""
    text = ("YOUTUBE TV ADS - PAGE 1\n"
            "PUBLISHERS & INVENTORY - PAGE 1\n"
            "Top 10 YouTube TV Channel Performance\n")
    assert check_required_widgets({"text": text, "products": {"YouTube Video Ads"}}) == []


def test_youtube_plus_owes_the_placement_widget_and_not_the_channel_list():
    """The two are an either/or. A YouTube+ buy has placements to break out;
    the channel list is the TV buy's widget."""
    text = "YOUTUBE+ ADS - PAGE 1\nYouTube+ Placement Performance\n"
    assert check_required_widgets({"text": text, "products": set()}) == []
    out = check_required_widgets({"text": "YOUTUBE+ ADS - PAGE 1\n",
                                  "products": set()})
    assert [f["title"] for f in out] == ["No YouTube+ Placement Performance widget"]


def test_youtube_on_the_order_with_no_section_still_owes_the_plus_widget():
    """A report that dropped the YouTube pages entirely is the worst case."""
    out = check_required_widgets({"text": "OVERVIEW - PAGE 1\n",
                                  "products": {"YouTube Video Ads"}})
    assert [f["title"] for f in out] == ["No YouTube+ Placement Performance widget"]


def _yt_ctx(text: str, *lines: str) -> dict:
    grid = "Line Item Performance\n" + "".join(
        f" {n}   1,000   10   1.00%\n" for n in lines)
    return {"text": text + grid, "products": {"YouTube Video Ads"}}


def test_either_spelling_of_the_channel_list_satisfies_a_tv_buy():
    """Braden's Furniture ran YouTube TV only, carried "Top 10 YouTube TV
    Channel Performance" on page 18, and was failed for having no "Top 10
    YouTube Channel Performance" widget. It is the same widget."""
    for title in CHANNEL_TITLES:
        ctx = _yt_ctx(f"YOUTUBE+ ADS - PAGE 1\n{title}\n",
                      "Bradens Furniture - YouTube TV")
        assert check_required_widgets(ctx) == []
    # Neither of them is a failure, named for the buy that was made.
    ctx = _yt_ctx("YOUTUBE+ ADS - PAGE 1\n", "Bradens Furniture - YouTube TV")
    assert [f["title"] for f in check_required_widgets(ctx)] == \
        ["No Top 10 YouTube TV Channel Performance widget"]


def test_the_sample_runs_both_so_it_owes_the_placement_widget(sample):
    """The everything-sample carries YouTube TV lines and YouTube+ lines, so
    by her rule the placement breakout is the one it owes."""
    from app.checks.rules import _youtube_lines
    tv, other = _youtube_lines({"text": sample})
    assert tv and other
    text = sample
    for drop in CHANNEL_TITLES:
        text = _drop(text, drop)
    assert check_required_widgets({"text": text, "products": set()}) == []


def test_youtube_tv_line_items_owe_the_channel_list_and_not_the_placements():
    """Her rule, in her words: if the YouTube line items are ONLY YouTube TV,
    the channel performance is needed and the placement widget is not."""
    ctx = _yt_ctx("YOUTUBE+ ADS - PAGE 1\nTop 10 YouTube TV Channel Performance\n",
                  "Bradens Furniture - YouTube TV")
    assert check_required_widgets(ctx) == []
    ctx = _yt_ctx("YOUTUBE+ ADS - PAGE 1\nYouTube+ Placement Performance\n",
                  "Bradens Furniture - YouTube TV")
    assert [f["title"] for f in check_required_widgets(ctx)] == \
        ["No Top 10 YouTube TV Channel Performance widget"]


def test_one_non_tv_youtube_line_flips_it_to_the_placement_widget():
    """And if any non-YouTube-TV line is in there, the placement widget is
    needed and the channel list is not - even with TV alongside it."""
    ctx = _yt_ctx("YOUTUBE+ ADS - PAGE 1\nYouTube+ Placement Performance\n",
                  "Bradens Furniture - YouTube TV",
                  "Bradens Furniture - In-Market Auto YouTube+")
    assert check_required_widgets(ctx) == []
    ctx = _yt_ctx("YOUTUBE+ ADS - PAGE 1\nTop 10 YouTube TV Channel Performance\n",
                  "Bradens Furniture - In-Market Auto YouTube+")
    assert [f["title"] for f in check_required_widgets(ctx)] == \
        ["No YouTube+ Placement Performance widget"]


def test_a_report_owing_nothing_abstains():
    """No claim is made about widgets on a report with no product that owes one."""
    ctx = {"text": "DISPLAY ADS - PAGE 1\nOVERVIEW - PAGE 1\n",
           "products": {"Display Ads"}}
    assert _rule_applies(check_required_widgets, ctx) is False


def test_a_ctv_report_is_judged(sample):
    assert _rule_applies(check_required_widgets,
                         {"text": sample, "products": set()}) is True


def test_section_headers_are_read_off_the_page_header(sample):
    secs = _sections(sample)
    assert {"CTV ADS", "SOCIAL MIRROR CTV ADS", "AMAZON ADS",
            "YOUTUBE+ ADS", "YOUTUBE TV ADS"} <= secs
    # The body text mentions plenty of products; only page headers count.
    assert "DISPLAY ADS" in secs and "BARCK+" not in secs


def test_barck_is_detected_from_its_own_widget(sample):
    assert BARCK.search(sample)
    assert not BARCK.search("a BARCK+ mention mid-sentence")


def test_the_table_has_no_leftover_provisional_titles():
    """The old guesses ("Inventory Source", "Channel Performance") were
    substrings that matched almost anything. They must not come back."""
    titles = [t for _c, _s, ts, _w in REQUIRED_WIDGETS for t in ts]
    assert "Inventory Source" not in titles
    assert "Channel Performance" not in titles


# ---------------------------------------------------------------- completion
from app.checks.rules import KNOWN_DEVICES, check_completion_rates, check_devices_known


def test_connected_audio_and_connected_device_are_real_devices():
    """Both read as junk until you see the sample's own description column."""
    assert {"connected audio", "connected device"} <= KNOWN_DEVICES


def test_the_sample_device_table_is_all_known(sample):
    """The page footer used to come out as a device.

    The device block ran until the next heading, and a table that ends near the
    bottom of a page hits "Digital Marketing Report" first - so the footer was
    read as a row and reported as an unrecognized device.
    """
    assert check_devices_known({"text": sample}) == []


def test_a_completion_performance_widget_over_100_fails():
    text = ("Video Completion Performance by Creative\n"
            "Creative            Impressions    Completion Rate\n"
            "spot_15.mp4         1,000          101.05%\n")
    out = check_completion_rates({"text": text})
    assert len(out) == 1 and out[0]["severity"] == "fail"


def test_exactly_100_is_fine():
    text = ("Completion Performance\nName   Imps   Rate\nRoku   100   100.00%\n")
    assert check_completion_rates({"text": text}) == []


# ------------------------------------------- Sholley Insurance: Media Player
def test_media_player_is_a_device():
    """Flagged as "not a device TapClicks reports" on a report whose own table
    describes it: "A personal device, either mobile or stationary, that plays
    media, such as Smart Speakers and iPods"."""
    assert "media player" in KNOWN_DEVICES


def test_a_row_the_table_describes_is_a_device_whatever_the_list_says():
    """A hard-coded list has to be updated by somebody who has seen the new
    name. The description is in the report already."""
    text = ("Device Performance\n"
            "Device Name          Description                                   Impressions   Clicks   CTR\n"
            "Holographic Visor    A head worn device that projects video into "
            "the wearer's field of view.        10        0   0.00%\n")
    assert check_devices_known({"text": text}) == []


def test_a_row_with_no_description_is_still_caught():
    """The junk this check exists for arrives with nothing beside it, which is
    how the description rule still leaves it caught."""
    text = ("Device Performance\n"
            "Device Name     Description        Impressions   Clicks   CTR\n"
            "Mobile          A portable electronic device that can connect to "
            "the internet.       100    5   5.00%\n"
            "Toaster         12    0   0.00%\n")
    out = check_devices_known({"text": text})
    assert out and "Toaster" in out[0]["detail"]


def test_device_findings_say_which_page():
    """"the device breakout is wrong" is true and unhelpful until you know
    which of forty pages it is on."""
    text = ("DEVICES - PAGE 8\nDevice Performance\n"
            "Device Name   Impressions   Clicks   CTR\n"
            "Blender       10    0   0.00%\n")
    out = check_devices_known({"text": text, "page_text": [text],
                               "page_of": lambda o: 8})
    assert out and out[0].get("where", "").startswith("p8")


# ------------------------------------------- R&R Heating: the publishers grid
def test_a_short_device_table_does_not_read_the_next_widget_as_devices():
    """Six unrecognized devices on a report whose device table has two rows,
    both of them real. Plex, TCL Channel, Sling TV and Tubi are CTV
    PUBLISHERS - the widget after the device one.

    The block boundary anchored on a title starting in column one, and
    pdftotext -layout indents the whole page, so it never matched and the block
    ran on into whatever came next.
    """
    text = (" Device Performance\n"
            " Device Name    Description                        Impressions   Clicks   CTR\n"
            " Connected TV   An internet enabled device that provides streaming\n"
            "                content directly on the TV.            14,999       24   0.16%\n"
            " Streaming Device  A stick/dongle device that connects to a TV and\n"
            "                provides streaming content.             8,104        6   0.07%\n"
            "\n"
            " Top CTV Publishers\n"
            " Publisher Image   Publisher              Impressions   Clicks   CTR\n"
            "                   Plex: Stream Movies, Shows, Live TV      900     1   0.11%\n"
            "                   TCL Channel                              800     0   0.00%\n"
            "                   Sling TV                                 700     0   0.00%\n"
            "                   Tubi - Movies & TV Shows                 600     0   0.00%\n")
    assert check_devices_known({"text": text}) == []


def test_the_widget_boundary_matches_an_indented_title():
    from app.checks.rules import WIDGET_END
    assert WIDGET_END.search("   Top CTV Publishers\n")
    assert WIDGET_END.search("  Display Creative Performance\n")


# ------------------------------------------- a billboard has no site and no app
def test_a_dooh_only_report_does_not_owe_the_site_and_app_widget():
    text = ("DOOH ADS - PAGE 1\nBARCK+ Zip Code Performance\n"
            "DOOH Line Item Performance\n")
    assert check_required_widgets({"text": text, "products": {"DOOH"}}) == []


def test_a_report_running_something_else_as_well_still_owes_it():
    text = ("DOOH ADS - PAGE 1\nBARCK+ Zip Code Performance\n"
            "DOOH Line Item Performance\n")
    out = check_required_widgets({"text": text, "products": {"DOOH", "Display"}})
    assert [f["title"] for f in out] == ["No Site and App Performance widget"]


def test_a_top_something_widget_ends_the_device_table():
    """WVU Parkersburg's device table is followed by Top CTV TV Devices, whose
    header row is "Device Make" and whose first row is "Telly". The block ran
    on into it and reported both as devices TapClicks does not report.

    Chasing suffixes one at a time was losing - Publishers, then Devices, then
    Makes. Every one of these widgets is titled "Top something"."""
    from app.checks.rules import check_devices_known
    text = (" Device Performance\n"
            " Device Name    Description                     Impressions   Clicks\n"
            " Mobile         A portable electronic device.     1,842,279   28,647\n"
            " Connected TV   An internet enabled device.         434,285      115\n"
            "\n"
            "                   Top CTV TV Devices\n"
            " Device Make       Impressions    Video Completion Rate\n"
            "Telly                  131,648                   72.33%\n"
            "Vizio                  110,216                   99.13%\n")
    assert check_devices_known({"text": text}) == []


def test_a_ctv_buy_beside_a_mobile_one_is_still_a_ctv_buy():
    """STANLEY STEEMER DOTHAN. Runs Mobile Conquesting and Social Mirror CTV,
    and its BARCK+ line is the CTV one - "Business Services/Offices/Cleaning
    Services B2B Behavioral Social Mirror CTV". Failed for no Site and App
    Performance widget, which Social Mirror CTV does not get: its inventory is
    the publisher list, and that was on the report.

    BARCK+ is the only thing that owes this widget."""
    from app.checks.rules import W_CTV_PUBS, _site_app_not_owed

    mixed = {"products": {"Mobile Conquesting", "Social Mirror CTV"}}
    assert _site_app_not_owed(mixed, {W_CTV_PUBS: 1})
    # The publisher list has to actually be there.
    assert not _site_app_not_owed(mixed, {})
    # And a buy with nothing that gets a publisher list still owes it.
    assert not _site_app_not_owed({"products": {"Mobile Conquesting"}},
                                  {W_CTV_PUBS: 1})


# ---------------------------------------- a widget for a product nobody bought
def test_an_amazon_display_widget_on_a_buy_with_no_amazon_display():
    """Fisher Tire bought Amazon Premium CTV and Video - its line items say
    Amazon Video and Amazon CTV and nothing else - and page 6 carried "Amazon
    Premium Display Conversion Performance by Ad Size" with 21 impressions at
    1920x1080 in it. That is a video frame, not a display banner."""
    from pathlib import Path
    from app.checks.parser import pdf_text
    from app.checks.rules import check_rogue_amazon_display
    fx = Path(__file__).parent / "fixtures" / "fisher_tire_amazon_display.pdf"
    out = check_rogue_amazon_display({"text": pdf_text(fx)})
    assert len(out) == 1 and out[0]["severity"] == "fail"
    assert out[0]["code"] == "widget_rogue"
    assert "Conversion Performance by Ad Size" in out[0]["detail"]
    assert "Amazon Video" in out[0]["detail"] and "Amazon CTV" in out[0]["detail"]


def test_a_real_amazon_display_buy_says_nothing(sample):
    """The everything-sample runs Amazon Display for real - "Retargeting Amazon
    Display", "Behavioral Amazon Premium Display" - and carries three of these
    widgets. It has to come out clean."""
    from app.checks.rules import check_rogue_amazon_display
    assert "Amazon Premium Display" in sample
    assert check_rogue_amazon_display({"text": sample}) == []


def test_a_report_with_no_line_items_makes_no_claim():
    """"No Amazon Display line" is true of every report whose line item grid
    did not parse, and that is not an answer about the buy."""
    from app.checks.rules import _rule_applies, check_rogue_amazon_display
    text = "Amazon Premium Display Conversion Performance by Ad Size\n"
    assert check_rogue_amazon_display({"text": text}) == []
    assert _rule_applies(check_rogue_amazon_display, {"text": text}) is False


def _amz(*lines: str) -> dict:
    grid = ("Amazon Premium Display Conversion Performance by Ad Size\n"
            "Line Item Performance\n"
            + "".join(f" {n}   1,000   10   1.00%\n" for n in lines))
    return {"text": grid}


def test_only_an_amazon_video_or_ctv_buy_is_asked():
    """Her rule: only Amazon video + CTV products, and no Amazon Display. The
    widget is not wrong in itself - a client running Amazon Premium Display
    owes it - so the question is only ever put to that one buy."""
    from app.checks.rules import _rule_applies, check_rogue_amazon_display as C

    # Not an Amazon buy at all. Never mind that the widget is sitting there.
    ctx = _amz("Acme - Homeowners Behavioral Display")
    assert _rule_applies(C, ctx) is False and C(ctx) == []

    # An Amazon buy that includes Display. The widget is owed.
    ctx = _amz("Acme - Homeowners Behavioral Amazon Display",
               "Acme - Homeowners Behavioral Amazon Video")
    assert _rule_applies(C, ctx) is False and C(ctx) == []

    # Either half of the CTV + Video buy on its own is enough to ask. An Amazon
    # month can deliver all of its impressions through one of the two, and the
    # video-only month is where a Display widget full of video frames turns up.
    for line in ("Acme - Homeowners Behavioral Amazon Video",
                 "Acme - Homeowners Behavioral Amazon CTV",
                 "Acme - Homeowners Behavioral Amazon OTT",
                 "Acme - Homeowners Behavioral Amazon Prime CTV",
                 "Acme - Homeowners Behavioral CTV Amazon"):
        ctx = _amz(line)
        assert _rule_applies(C, ctx) is True, line
        assert len(C(ctx)) == 1, line


def test_the_check_is_scoped_to_the_amazon_products():
    """A Run on this check reads the CTV and Video reports, not all 1,417."""
    from app.checks.rules import CHECK_PRODUCTS
    assert CHECK_PRODUCTS["check_rogue_amazon_display"] == ("CTV", "Video")


# ------------------------------------ the half of an Amazon buy with no widget
WW = Path(__file__).parent / "fixtures" / "window_world_bowling_green.pdf"


def _ww() -> str:
    from app.checks.parser import pdf_text
    return pdf_text(WW)


def test_an_amazon_ctv_half_with_no_ctv_widget_anywhere():
    """Window World - Bowling Green served 29,085 impressions on "Amazon CTV"
    and 1,202 on "Video Amazon" - 96% of the campaign was the CTV half - and
    the report carried the Video creative and completion widgets and not one
    CTV widget anywhere. The page-one tile read 0.34% with nothing on the
    report to read it off."""
    from app.checks.rules import _amazon_halves, check_required_widgets
    text = _ww()
    assert _amazon_halves(text) == {"ctv", "video"}
    assert [f["title"] for f in check_required_widgets({"text": text,
                                                        "products": set()})] == \
        ["No Amazon Premium CTV Creative Performance widget",
         "No Amazon Premium CTV Video Completion Performance by Creative widget"]


def test_plain_connected_tv_beside_amazon_ctv_covers_both(sample):
    """Renegade Marine runs Connected TV alongside Amazon Prime CTV - 14,142
    impressions on it - and TapClicks reports both under "Connected TV (CTV)
    Creative Performance". Asking for an Amazon-branded copy would fail a
    report that carries everything it owes."""
    from app.checks.parser import pdf_text
    from app.checks.rules import _amazon_halves, check_required_widgets
    fx = Path(__file__).parent / "fixtures" / "renegade_marine.pdf"
    text = pdf_text(fx)
    assert "ctv" in _amazon_halves(text)
    assert check_required_widgets({"text": text, "products": set()}) == []
    # And the everything-sample, which writes the same widgets with OTT in the
    # title instead of CTV.
    assert check_required_widgets({"text": sample, "products": set()}) == []


def test_a_half_that_served_nothing_is_owed_nothing():
    """An Amazon month can deliver all of it through one of the two, and
    TapClicks prints no widgets for the half that served nothing."""
    from app.checks.rules import _amazon_halves
    grid = ("Line Item Performance\n"
            " Acme - Boats Behavioral Amazon CTV      12,000   10   0.08%\n"
            " Acme - Boats Behavioral Amazon Video         0    0   0.00%\n")
    assert _amazon_halves(grid) == {"ctv"}


def test_both_display_widgets_are_named_when_they_share_a_line():
    """"Click Performance by Ad Size" and "Conversion Performance by Ad Size"
    print side by side, so in the text they are one line. A line-anchored
    pattern read the pair as one widget with a forty-word title - the same
    shape as the CTV tile bug."""
    from app.checks.rules import check_rogue_amazon_display
    out = check_rogue_amazon_display({"text": _ww()})
    assert len(out) == 1
    assert out[0]["title"].startswith("2 Amazon Premium Display widgets")
    assert '"Amazon Premium Display Click Performance by Ad Size"' in out[0]["detail"]
    assert '"Amazon Premium Display Conversion Performance by Ad Size"' in out[0]["detail"]
    # "Amazon CTV" puts the product after the word, "Video Amazon" before it.
    assert '"Amazon CTV"' in out[0]["detail"]
    assert '"Video Amazon"' in out[0]["detail"]
