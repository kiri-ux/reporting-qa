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
    text = ("YOUTUBE+ ADS - PAGE 1\nYouTube+ Placement Performance\n"
            "YouTube+ Creative Performance\n")
    assert check_required_widgets({"text": text, "products": set()}) == []
    out = check_required_widgets({"text": "YOUTUBE+ ADS - PAGE 1\n"
                                          "YouTube+ Creative Performance\n",
                                  "products": set()})
    assert [f["title"] for f in out] == ["No YouTube+ Placement Performance widget"]


def test_youtube_on_the_order_with_no_section_still_owes_the_plus_widget():
    """A report that dropped the YouTube pages entirely is the worst case."""
    out = check_required_widgets({"text": "OVERVIEW - PAGE 1\n",
                                  "products": {"YouTube Video Ads"}})
    assert [f["title"] for f in out] == ["No YouTube+ Placement Performance widget",
                                         "No YouTube+ Creative Performance widget"]


def _yt_ctx(text: str, *lines: str) -> dict:
    grid = "Line Item Performance\n" + "".join(
        f" {n}   1,000   10   1.00%\n" for n in lines)
    # Page one with its CTV Completion Rate tile, which any YouTube TV owes.
    # ...and the YouTube+ creative breakout, which any YouTube+ owes.
    return {"text": "CTV Completion Rate\n\fYouTube+ Creative Performance\n" + text + grid,
            "products": {"YouTube Video Ads"}}


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
            "spot_15.mp4         1,000          102.05%\n")
    out = check_completion_rates({"text": text})
    assert len(out) == 1 and out[0]["severity"] == "fail"
    # Up to 102% is OTT trackers, not a fault.
    assert check_completion_rates({"text": text.replace("102.05", "101.95")}) == []


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
    # Mobile Conquesting on its own gets no site and app list at all.
    assert _site_app_not_owed({"products": {"Mobile Conquesting"}}, {})
    # And a buy with a product that does get one still owes it.
    assert not _site_app_not_owed({"products": {"Mobile Conquesting", "Display"}},
                                  {W_CTV_PUBS: 1})


# ---------------------------------------- a widget for a product nobody bought
def test_an_amazon_display_widget_on_a_buy_with_no_amazon_display():
    """Fisher Tire bought Amazon Premium CTV and Video - its line items say
    Amazon Video and Amazon CTV and nothing else - and page 6 carried "Amazon
    Premium Display Conversion Performance by Ad Size" with 21 impressions at
    1920x1080 in it. That is a video frame, not a display banner."""
    from pathlib import Path
    from app.checks.parser import pdf_text
    from app.checks.rules import check_rogue_widgets
    fx = Path(__file__).parent / "fixtures" / "fisher_tire_amazon_display.pdf"
    out = check_rogue_widgets({"text": pdf_text(fx)})
    assert len(out) == 1 and out[0]["severity"] == "fail"
    assert out[0]["code"] == "widget_rogue"
    assert "Conversion Performance by Ad Size" in out[0]["detail"]
    assert "Amazon Video" in out[0]["detail"] and "Amazon CTV" in out[0]["detail"]


def test_a_real_amazon_display_buy_says_nothing(sample):
    """The everything-sample runs Amazon Display for real - "Retargeting Amazon
    Display", "Behavioral Amazon Premium Display" - and carries three of these
    widgets. It has to come out clean."""
    from app.checks.rules import check_rogue_widgets
    from app.checks.products import detect
    assert "Amazon Premium Display" in sample
    got = detect(sample, [])
    assert check_rogue_widgets({"text": sample, "products": got,
                                "expected_products": got}) == []


def test_a_report_with_no_line_items_makes_no_claim():
    """"No Amazon Display line" is true of every report whose line item grid
    did not parse, and that is not an answer about the buy."""
    from app.checks.rules import _rule_applies, check_rogue_widgets
    text = "Amazon Premium Display Conversion Performance by Ad Size\n"
    assert check_rogue_widgets({"text": text}) == []
    assert _rule_applies(check_rogue_widgets, {"text": text}) is False


def _amz(*lines: str) -> dict:
    grid = ("Amazon Premium Display Conversion Performance by Ad Size\n"
            "Line Item Performance\n"
            + "".join(f" {n}   1,000   10   1.00%\n" for n in lines))
    return {"text": grid}


def test_only_an_amazon_video_or_ctv_buy_is_asked():
    """Her rule: only Amazon video + CTV products, and no Amazon Display. The
    widget is not wrong in itself - a client running Amazon Premium Display
    owes it - so the question is only ever put to that one buy."""
    from app.checks.rules import _rule_applies, check_rogue_widgets as C

    # Not an Amazon buy at all. Never mind that the widget is sitting there.
    assert C(_amz("Acme - Homeowners Behavioral Display")) == []

    # An Amazon buy that includes Display. The widget is owed.
    assert C(_amz("Acme - Homeowners Behavioral Amazon Display",
                  "Acme - Homeowners Behavioral Amazon Video")) == []

    # A report carrying none of these widgets is not a check standing down, it
    # is a check that was never about that report.
    assert _rule_applies(C, {"text": "Line Item Performance\n"
                                     " Acme - Homeowners Display  9  1  1%\n"}) is False

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


def test_the_check_is_not_scoped_by_product():
    """It cannot be. The scope matches what the report was detected as running,
    and this check is about a product that is NOT part of the buy - scoping it
    by that product would skip every report it is about. See the guard in
    test_recheck."""
    from app.checks.rules import CHECK_PRODUCTS
    assert "check_rogue_widgets" not in CHECK_PRODUCTS


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
    from app.checks.rules import check_rogue_widgets
    out = check_rogue_widgets({"text": _ww()})
    assert len(out) == 1
    assert out[0]["title"].startswith("2 Amazon Premium Display widgets")
    assert '"Amazon Premium Display Click Performance by Ad Size"' in out[0]["detail"]
    assert '"Amazon Premium Display Conversion Performance by Ad Size"' in out[0]["detail"]
    # "Amazon CTV" puts the product after the word, "Video Amazon" before it.
    assert '"Amazon CTV"' in out[0]["detail"]
    assert '"Video Amazon"' in out[0]["detail"]


def test_a_conversion_breakout_is_not_evidence_the_product_ran():
    """Charlottesville's Earthly Cleaning bought Display and Performance Max -
    four line items, not one of them CTV - and TapClicks printed "CTV Click
    Conversion Performance Breakout" and "CTV View-through Conversion
    Performance Breakout" on it anyway. Those two titles were the whole of the
    evidence, so the report was credited with CTV and then FAILED for having no
    Top CTV Publishers widget: a missing widget for a product nobody bought,
    off the back of two widgets that should not be there."""
    from app.checks.products import detect
    titles = ["CTV Click Conversion Performance Breakout",
              "CTV View-through Conversion Performance Breakout",
              "Social Mirror CTV Click Conversion Performance Breakout",
              "Amazon Premium Video + CTV View-through Conversion Performance"]
    for t in titles:
        assert "CTV" not in detect(t + "\n", []), t
    # The widgets that DO mean it ran, each under its own product: Amazon
    # Premium is its own buy, not the plain Connected TV one.
    for t, want in (("Connected TV (CTV) Creative Performance", "CTV"),
                    ("Connected TV (CTV) Completion Performance by Strategy", "CTV"),
                    ("Amazon Premium OTT Creative Performance", "Amazon CTV"),
                    ("Amazon Premium CTV Creative Performance", "Amazon CTV"),
                    ("Amazon Premium Video Creative Performance", "Amazon Video")):
        assert want in detect(t + "\n", []), t


def test_a_real_ctv_buy_keeps_its_product_from_the_line_items():
    """The line items are the record, so a CTV buy whose only CTV widget was a
    conversion breakout does not lose the product and get failed the other way
    round - "ordered but not on the report"."""
    from app.checks.parser import extract_tables, pdf_text
    from app.checks.products import detect
    fx = Path(__file__).parent / "fixtures" / "renegade_marine.pdf"
    text = pdf_text(fx)
    # Take every CTV-named creative and completion grid title off it, leaving
    # only the conversion breakouts and the line items.
    stripped = "\n".join(
        "" if ("Creative Performance" in l or "Completion Performance" in l)
        and ("CTV" in l or "OTT" in l or "Connected TV" in l) else l
        for l in text.split("\n"))
    assert "Creative Performance" not in "".join(
        l for l in stripped.split("\n") if "CTV" in l)
    assert "CTV" in detect(stripped, extract_tables(stripped, strict=True))


PPC_REPORTS = ["ski_barn_ppc_pages.pdf", "usdan_no_ppc_boilerplate.pdf",
               "earthly_cleaning_ppc_pages.pdf"]


def _ppc_ctx(name, products):
    from app.checks.parser import pdf_pages, pdf_text
    fx = Path(__file__).parent / "fixtures" / name
    pages = pdf_pages(fx)
    return {"path": fx, "pages": len(pages), "page_text": pages,
            "text": pdf_text(fx), "products": set(products),
            "expected_products": set(products)}


def test_ppc_pages_on_a_buy_with_no_ppc():
    """Ski Barn, Usdan Summer Camp and Earthly Cleaning carry the PPC
    cost-per-click glossary and the ad extension breakdown with no PPC on the
    order. They are media widgets with no data, and that is the flag - not a
    product on a buy that does not include it."""
    from app.checks.rules import check_blank_pages, check_rogue_widgets

    for name in PPC_REPORTS:
        ctx = _ppc_ctx(name, {"Display", "Meta"})
        assert "Amount Spent on the PPC campaign" in ctx["text"], name
        assert check_rogue_widgets(ctx) == [], name
        out = check_blank_pages(ctx)
        assert len(out) == 1 and out[0]["code"] == "blank_widget_page", name
        assert "with a widget but no data" in out[0]["title"], name
        from app.checks.rules import PPC_WIDGET
        ppc = {i + 1 for i, t in enumerate(ctx["page_text"]) if PPC_WIDGET.search(t)}
        assert ppc and ppc <= {pg for pg, _w in out[0]["pages"]}, name


def test_a_client_who_runs_ppc_says_nothing():
    """Whether the product is read off the report or off the order."""
    from app.checks.rules import check_blank_pages

    base = _ppc_ctx("ski_barn_ppc_pages.pdf", {"Display"})
    for key in ("products", "expected_products"):
        live = dict(base, **{key: set(base[key]) | {"PPC"}})
        out = check_blank_pages(live)
        from app.checks.rules import PPC_WIDGET
        ppc = {i + 1 for i, t in enumerate(live["page_text"]) if PPC_WIDGET.search(t)}
        flagged = {pg for f in out for pg, _w in f.get("pages", [])}
        assert ppc and not (ppc & flagged), key


def test_performance_max_owns_its_own_google_widgets():
    """"Performance Max Other Google Conversions" is PMax's, and PMax is a
    product the client may well be buying. Only PPC's own copy counts."""
    from app.checks.rules import PPC_WIDGET

    assert not PPC_WIDGET.search(
        "Performance Max Other Google Conversions - these are automatic "
        "conversions Google tracks")
    assert PPC_WIDGET.search(
        '"Calls from ads" in the PPC Other Google Conversions widget')


def test_the_families_are_one_table():
    """The next one somebody spots should be a line, not a check."""
    from app.checks.rules import ROGUE_WIDGETS
    labels = [row[0] for row in ROGUE_WIDGETS]
    assert "Amazon Premium Display" in labels and "PPC" not in labels
    for row in ROGUE_WIDGETS:
        assert len(row) == 5, row[0]
        assert row[2] in ("line", "product", "products"), row[0]


def test_a_ctv_tile_that_is_plainly_a_click_through_rate():
    """Window World's page one read 0.34% and another read 0.23% - those are
    click-through rates, in the tile that holds the share of viewers who
    watched an ad to the end.

    Worth saying on its own, before anything is compared to anything: the tile
    alone settles it, so it does not matter whether the report carries a CTV
    grid to measure against. "CTV VCR could not be checked" on a report whose
    tile is plainly a CTR is the tool declining to say the one thing it knows.
    """
    from app.checks.parser import pdf_text
    from app.checks.rules import CTV_TILE_FLOOR, _ctv_tile_pct, check_ctv_tile

    text = _ww()
    tile, _at = _ctv_tile_pct(text)
    assert tile is not None and tile < CTV_TILE_FLOOR
    out = check_ctv_tile({"text": text, "expected_products": {"CTV"}})
    assert [f["title"] for f in out] == ["CTV VCR is not a completion rate"]
    assert out[0]["severity"] == "fail"
    assert "0.34%" in out[0]["detail"]
    # No CTV order needed, and no CTV grid: the tile settles it on its own.
    # That is the difference from "could not be checked", which is what this
    # report was getting.
    assert [f["title"] for f in check_ctv_tile({"text": text})] == \
        ["CTV VCR is not a completion rate"]


def test_a_real_completion_rate_is_left_alone():
    """Every CTV fixture on hand runs 71% to 99%. None of them is the tile
    this is about."""
    from app.checks.parser import pdf_text
    from app.checks.rules import _ctv_tile_pct, check_ctv_tile

    for name in ("renegade_marine.pdf", "central_penn.pdf", "watsontown.pdf",
                 "fisher_tire_amazon_display.pdf"):
        fx = Path(__file__).parent / "fixtures" / name
        text = pdf_text(fx)
        tile, _at = _ctv_tile_pct(text)
        assert tile and tile > 50, name
        assert [f for f in check_ctv_tile({"text": text,
                                           "expected_products": {"CTV"}})
                if "not a completion rate" in f["title"]] == [], name


def test_mobile_conquesting_does_not_owe_site_and_app():
    """Close Lumber runs Mobile Conquesting and nothing else. Its BARCK+ pages
    are visits, and there is no site and app list for it."""
    from app.checks.rules import run_all
    fx = Path(__file__).parent / "fixtures" / "close_lumber_barck_mc.pdf"
    r = run_all(fx, "Lifetime_Close Lumber 43722.pdf")
    assert r["products"] == ["Mobile Conquesting"]
    assert not [f for f in r["findings"] if f["code"] == "widget_missing"
                and "Site and App" in f["title"]]


def test_barck_on_a_buy_that_does_not_run_it():
    """BARCK+ runs on Display, Native Display, Native Video, Social Mirror,
    Video, CTV, Social Mirror CTV and Video + CTV. Close Lumber is Mobile
    Conquesting only and carried three BARCK+ widgets."""
    from app.checks.rules import check_blank_pages, check_rogue_widgets, run_all
    fx = Path(__file__).parent / "fixtures" / "close_lumber_barck_mc.pdf"
    r = run_all(fx, "Lifetime_Close Lumber 43722.pdf")
    rogue = [f for f in r["findings"] if f["code"] == "widget_rogue"]
    assert len(rogue) == 1 and "BARCK+" in rogue[0]["title"]
    assert "BARCK+ Visit by Day" in rogue[0]["detail"]
    # The Visit by Day chart has two points on it. It is not an empty page.
    assert not [f for f in r["findings"] if f["code"] == "blank_widget_page"]

    from app.checks.parser import pdf_text
    text = pdf_text(fx)
    for ok in ({"Display"}, {"Mobile Conquesting", "Video"}, {"Social Mirror CTV"}):
        assert check_rogue_widgets({"text": text, "products": ok}) == [], ok
    # Nothing known about the buy is not a finding.
    assert check_rogue_widgets({"text": text}) == []


def test_the_assignment_limit_is_a_widget_with_no_data():
    """North Bay Trade's TikTok conversion widget printed TapClicks' assignment
    limit on a page of its own. That is a blank widget to delete, not a
    widget error to re-pull."""
    from app.checks.rules import run_all
    fx = Path(__file__).parent / "fixtures" / "north_bay_assignment_limit.pdf"
    r = run_all(fx, "September 2026_North Bay Trade Introduction Program TIP 55433.pdf")
    codes = [f["code"] for f in r["findings"]]
    assert "widget_error" not in codes
    blank = [f for f in r["findings"] if f["code"] == "blank_widget_page"]
    assert len(blank) == 1
    assert blank[0]["detail"] == "pg 5: text starting: TikTok Click and"
    assert blank[0]["pages"] == [[5, "TikTok Click and"]]


def test_youtube_tv_is_ctv():
    """Chalfant Corporation - Volkswagen of Boise runs YouTube TV. Its CTV tile
    belongs, it owes no creative breakout with impressions, and the page-one
    CTV Completion Rate tile is missing."""
    from app.checks.rules import run_all
    fx = Path(__file__).parent / "fixtures" / "chalfant_youtube_tv.pdf"
    r = run_all(fx, "September 2026_Chalfant Corporation - Volkswagen of Boise 45946 52407.pdf")
    titles = {f["title"] for f in r["findings"]}
    assert "CTV tile on a report with no CTV" not in titles
    assert "No YouTube TV Creative Performance widget" not in titles
    assert "No CTV Completion Rate on page one" in titles


def test_valero_law_group_youtube_tv_owes_the_completion_rate_tile():
    from app.checks.rules import run_all
    fx = Path(__file__).parent / "fixtures" / "valero_youtube_tv.pdf"
    r = run_all(fx, "September 2026_Valero Law Group 50052 52972.pdf")
    assert [f["title"] for f in r["findings"]] == ["No CTV Completion Rate on page one"]


def test_native_video_owes_no_completion_rate():
    """American Society for Cell Biology runs Native Video, which the product
    map reads as Video. Native Video prints no completion rate."""
    from app.checks.quality import check_completion_present
    from app.checks.rules import run_all
    fx = Path(__file__).parent / "fixtures" / "cell_bio_native_video.pdf"
    r = run_all(fx, "September 2026_American Society for Cell Biology - Cell Bio 53093.pdf")
    assert "completion_missing" not in {f["code"] for f in r["findings"]}
    # A plain video line beside it still owes one.
    text = ("Line Item Performance\\n"
            " Acme - Retargeting Native Video   1,000   10   1.00%\\n"
            " Acme - Behavioral Video   1,000   10   1.00%\\n"
            "Video Creative Performance\\n")
    assert check_completion_present({"text": text, "products": {"Video"}})


def test_online_audio_runs_barck():
    """Thirwood Place runs Online Audio and carries BARCK+ Zip Code
    Performance. Online Audio is a BARCK+ product."""
    from app.checks.rules import run_all
    fx = Path(__file__).parent / "fixtures" / "thirwood_audio_barck.pdf"
    r = run_all(fx, "September 2026_Thirwood Place 54800.pdf")
    assert not [f for f in r["findings"] if f["code"] == "widget_rogue"]


def test_a_cancelled_barck_line_that_left_its_pages_ran():
    """Close Lumber's Social Mirror was cancelled. Its BARCK+ pages are on the
    report, so it ran: not rogue, but its Social Mirror pages are missing."""
    from app.checks.parser import pdf_text
    from app.checks.rules import check_required_widgets, check_rogue_widgets, run_all
    fx = Path(__file__).parent / "fixtures" / "close_lumber_barck_mc.pdf"
    r = run_all(fx, "Lifetime_Close Lumber 43722.pdf",
                expected_products={"Mobile Conquesting"},
                cancelled_products={"Social Mirror"})
    assert not [f for f in r["findings"] if f["code"] == "widget_rogue"]
    miss = [f for f in r["findings"] if f["code"] == "widget_missing"
            and "Social Mirror" in f["title"]]
    assert len(miss) == 1 and miss[0]["title"] == "No Social Mirror widgets"

    # Nothing of the cancelled product on the report: it never ran.
    text = pdf_text(fx)
    bare = "\n".join(l for l in text.split("\n") if "BARCK+" not in l)
    ctx = {"text": bare, "products": {"Mobile Conquesting"},
           "cancelled_products": {"Social Mirror"}}
    assert check_rogue_widgets(ctx) == []
    assert not [f for f in check_required_widgets(ctx)
                if "Social Mirror" in f["title"]]

    # A live BARCK+ product explains the pages; nothing to say.
    ctx = {"text": text, "products": {"Mobile Conquesting", "Display"},
           "cancelled_products": {"Social Mirror"}}
    assert not [f for f in check_required_widgets(ctx)
                if "Social Mirror" in f["title"]]
    # A cancelled product that is not BARCK+ does not excuse them.
    ctx = {"text": text, "products": {"Mobile Conquesting"},
           "cancelled_products": {"PPC"}}
    assert len(check_rogue_widgets(ctx)) == 1


def test_online_audio_owes_creative_and_completion_widgets():
    """John 3:16 Mission ran AI Audio and carried neither Online Audio
    widget. Thirwood Place carries both."""
    from app.checks.rules import run_all
    fx = Path(__file__).parent / "fixtures" / "john316_ott_audio.pdf"
    r = run_all(fx, "Lifetime_John 316 Mission 41994.pdf",
                expected_products={"CTV", "Online Audio"})
    got = sorted(f["title"] for f in r["findings"]
                 if f["code"] == "widget_missing" and "Audio" in f["title"])
    assert got == ["No Online Audio Completion Performance by Line Item widget",
                   "No Online Audio Creative Performance widget"]
    codes = [f["code"] for f in r["findings"]]
    assert "completion_missing" not in codes and "ctv_tile_unchecked" not in codes
    fx = Path(__file__).parent / "fixtures" / "thirwood_audio_barck.pdf"
    r = run_all(fx, "September 2026_Thirwood Place 1.pdf")
    assert not [f for f in r["findings"] if "Audio" in f["title"]]
