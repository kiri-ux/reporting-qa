"""The checks that read the report's words rather than its arithmetic.

The 317-page everything-sample is the reference document. Where a rule fires on
it, the finding is real and named here, so a future change that silences one
fails a test instead of going quiet.
"""
from pathlib import Path

import pytest

from app.checks import quality as q

SAMPLE_TXT = Path("/root/work/sample.txt")
SAMPLE_PDF = Path("/root/work/sample.pdf")


@pytest.fixture(scope="module")
def sample() -> str:
    if not SAMPLE_TXT.exists():
        pytest.skip("everything-sample not present")
    return SAMPLE_TXT.read_text()


# ---------------------------------------------------------------- helpers
def test_grid_rows_joins_a_name_that_wrapped_below_its_own_numbers(sample):
    """TapClicks wraps the tail of a long name onto the NEXT line, under the
    numbers. Reading line by line splits one line item into two."""
    names = [n for n, _ in q.line_item_names(sample)]
    assert "Matt Heilala - Lookalike Facebook/Instagram Premium" in names
    assert "Alpha Roofing - Severe Thunderstorm Weather Trigger Mobile" in names
    # The whole summary grid, not just the ten rows on page one - and each row
    # once, though two grid titles both end in "Line Item Performance".
    # 3,756 in the main grid plus the ten DOOH rows, which count in "DOOH Ads
    # Served" and were being thrown away.
    assert len(names) == 3766


def test_section_at_names_the_page_a_fault_is_on(sample):
    i = sample.index("requesting data for more assignments")
    assert q.section_at(sample, i) == "TIKTOK CONVERSIONS"


# ------------------------------------------------- strategy categorisation
def test_uncategorised_strategy_lines_are_found(sample):
    out = q.check_strategy_categorized({"text": sample})
    assert len(out) == 1 and out[0]["severity"] == "fail"
    detail = out[0]["detail"]
    # The two that show up on the product breakout donut as their own slices.
    assert "Mad Hatter Chimney Cleaning - Keywords" in detail
    assert "Monterey County Bank - Geo-Retargeting Lookalike" in detail


def test_a_product_word_anywhere_in_the_name_is_enough():
    """Demanding the product come LAST flagged 263 of one report's 3,756 line
    items - trailing order numbers and "- Non-Muncie" are fine names."""
    text = ("Line Item Performance\n"
            "Name   Impressions   Clicks   CTR\n"
            "Acme - Retargeting Social Mirror - 132867   100   1   1.00%\n"
            "Acme - Behavioral Display - Non-Muncie      100   1   1.00%\n")
    assert q.check_strategy_categorized({"text": text}) == []


def test_a_name_with_no_product_word_at_all_is_flagged():
    text = ("Line Item Performance\n"
            "Name   Impressions   Clicks   CTR\n"
            "Acme - Keywords   100   1   1.00%\n")
    out = q.check_strategy_categorized({"text": text})
    assert len(out) == 1 and "Acme - Keywords" in out[0]["detail"]


# ------------------------------------------------------------ truncation
def test_a_cell_clipped_mid_word_is_found(sample):
    """"...Behavioral Social Mirro" - the column ate the last letter."""
    out = q.check_truncated_text({"text": sample})
    detail = " ".join(f["detail"] for f in out)
    assert "Behavioral Social Mirro'" in detail


def test_a_longer_sibling_is_not_a_truncation():
    """"Social Mirror" and "Social Mirror CTV" are two line items, not one
    clipped one. This is why the tolerance is three characters."""
    text = ("Line Item Performance\n"
            "Name   Impressions   Clicks   CTR\n"
            "Acme - AI Social Mirror       100   1   1.00%\n"
            "Acme - AI Social Mirror CTV   100   1   1.00%\n")
    assert q.check_truncated_text({"text": text}) == []


def test_a_whole_word_added_is_not_a_truncation():
    text = ("Line Item Performance\n"
            "Name   Impressions   Clicks   CTR\n"
            "Acme - Display      100   1   1.00%\n"
            "Acme - Display AI   100   1   1.00%\n")
    assert q.check_truncated_text({"text": text}) == []


def test_an_ellipsis_label_is_found():
    """The chart kind: "Category Tar...: 77.78%" needs wrap text turned on."""
    text = ("Line Item Performance\n"
            "Name   Impressions   Clicks   CTR\n"
            "Acme - Display   100   1   1.00%\n"
            "Category Tar...: 77.78%\n")
    out = q.check_truncated_text({"text": text})
    assert any("Category Tar..." in f["detail"] for f in out)


# ------------------------------------------------------- widget errors
def test_the_tiktok_widget_error_is_found(sample):
    out = q.check_widget_errors({"text": sample})
    assert len(out) == 1 and out[0]["severity"] == "fail"
    assert "TIKTOK CONVERSIONS" in out[0]["detail"]


def test_a_clean_report_reports_no_widget_errors():
    assert q.check_widget_errors({"text": "OVERVIEW - PAGE 1\nAll fine.\n"}) == []


# ------------------------------------------------- Social Mirror ad sizes
SIZED = ("SOCIAL MIRROR ADS - PAGE 1\n"
         "Social Mirror Creative Performance\n"
         "Preview Image   Creative Name   Impressions   Clicks   CTR\n"
         "                ESUMC_300x250-Youth-2026.jpg   8,329   732   8.79%\n"
         "                ICPA_8.17_Social Mirror_The countdown is on!"
         "   3,187   256   8.03%\n")


def test_a_social_mirror_creative_carrying_an_ad_size_is_found():
    out = q.check_social_mirror_sizes({"text": SIZED, "market": "Conquest"})
    assert len(out) == 1 and out[0]["severity"] == "fail"
    assert "ESUMC_300x250-Youth-2026.jpg" in out[0]["detail"]
    assert "countdown" not in out[0]["detail"]


def test_display_creatives_keep_their_sizes():
    """Only Social Mirror. A Display name carries 728x90 by design."""
    text = ("DISPLAY ADS - PAGE 1\n"
            "Display Creative Performance\n"
            "Preview Image   Creative Name   Impressions   Clicks   CTR\n"
            "                Farmington_PYCE Fall 728x90.gif   4,380   88   2.01%\n")
    assert q.check_social_mirror_sizes({"text": text, "market": "Conquest"}) == []


def test_curtis_keeps_the_sizes_because_they_asked_for_them():
    assert q.check_social_mirror_sizes(
        {"text": SIZED, "market": "Curtis Media Group"}) == []


def test_the_samples_social_mirror_creatives_carry_no_sizes(sample):
    assert q.check_social_mirror_sizes({"text": sample, "market": "Conquest"}) == []


# ------------------------------------------------- conversion / creative names
def test_a_blank_conversion_name_is_found():
    """A nameless row prints indented to the numbers, so its first cell is one."""
    text = ("DISPLAY CONVERSIONS - PAGE 1\n"
            " Conversion Name        Click Conversions\n"
            "Sun Federal Apply Page View   21   1   0\n"
            "                              20   1   0\n")
    out = q.check_conversion_names({"text": text})
    assert any(f["code"] == "conversion_name_blank" for f in out)


def test_unnamed_reads_a_number_in_the_name_column_as_no_name():
    assert q.unnamed("") and q.unnamed("  ") and q.unnamed("20") and q.unnamed("1,024")
    assert not q.unnamed("Sun Federal Apply Page View")


def test_a_conversion_named_retargeting_is_found():
    text = (" Conversion Name        Click Conversions\n"
            "Acme - Retargeting   21   1   0\n")
    out = q.check_conversion_names({"text": text})
    assert any(f["code"] == "conversion_name_retargeting" for f in out)


def test_the_samples_conversions_are_all_named(sample):
    assert q.check_conversion_names({"text": sample}) == []


def test_the_samples_creatives_are_all_named(sample):
    assert q.check_creative_names({"text": sample}) == []


# ------------------------------------------------------ blank screenshots
def test_the_screenshot_cells_are_located_by_their_coordinates():
    """Screenshot images are not in the text layer, so the cells have to be cut
    out of a rendering - which needs the row labels' coordinates."""
    if not SAMPLE_PDF.exists():
        pytest.skip("everything-sample not present")
    pages = q.page_words(SAMPLE_PDF)
    page = next(p for p in pages if any(w[4] == "Screenshots" for w in p["words"]))
    top, bottom, cells = q._screenshot_cells(page)
    assert bottom > top
    assert len(cells) == 3
    assert cells[0][2].startswith("300x250-WFNissan-Geofencing-Ad1")


def test_a_populated_screenshot_row_passes():
    if not SAMPLE_PDF.exists():
        pytest.skip("everything-sample not present")
    assert q.check_blank_screenshots({"path": SAMPLE_PDF}) == []


def test_is_blank_separates_an_ad_from_an_empty_cell():
    """A real ad has thousands of colours; an empty cell has the table fill and
    its border. There is nothing in between, which is why the threshold is a
    flat count."""
    from PIL import Image
    flat = Image.new("RGB", (60, 60), (102, 163, 209))
    assert q.is_blank(flat)

    ad = Image.new("RGB", (60, 60))
    ad.putdata([(x * 4 % 256, y * 4 % 256, (x + y) % 256)
                for y in range(60) for x in range(60)])
    assert not q.is_blank(ad)


def test_a_border_alone_still_counts_as_blank():
    from PIL import Image
    im = Image.new("RGB", (60, 60), (102, 163, 209))
    for i in range(60):
        im.putpixel((i, 0), (0, 102, 179))
        im.putpixel((0, i), (0, 102, 179))
    assert q.is_blank(im)


# ------------------------------------------------------------- grid bounds
# Where one grid stops is the whole game for these rules. Every finding below
# was a false positive at some point in getting it right.
def test_a_grid_stops_at_the_next_widget_title():
    """A DOOH publisher list under an empty line item grid was reported as
    twenty badly named strategies."""
    text = ("DOOH Line Item Performance\n"
            "Strategy Name        DOOH Ads Served\n"
            "\n"
            " Site and App Performance\n"
            " Name          Impressions   Clicks   CTR\n"
            "screenversemedia.com   13,868   0   0.00%\n"
            "coinstar.com            1,584   0   0.00%\n")
    assert q.check_strategy_categorized({"text": text}) == []


def test_a_wrapped_name_ending_in_a_widget_word_does_not_end_the_grid():
    """"Services/Homeowners/Retargeting Performance" is the tail of a line item
    name, not a title. Treating it as one cut a 3,756-row grid off at 30."""
    text = ("Line Item Performance\n"
            "Line Item Name   Impressions   Clicks   CTR\n"
            "Peters - Troy - HVAC   10,412   203   1.95%\n"
            "Services/Homeowners/Retargeting Performance\n"
            "Max\n"
            "Durham Lead - Address Retargeting Mobile   10,183   7   0.07%\n"
            "Acme - Keywords   100   1   1.00%\n")
    out = q.check_strategy_categorized({"text": text})
    assert len(out) == 1 and "Acme - Keywords" in out[0]["detail"]


def test_a_grid_stops_when_the_page_header_changes_section(sample):
    """The header shares its line with "Date range", so a chrome filter that
    ran first threw it away and a grid ran on for two hundred pages."""
    i = sample.index("Mobile Conquesting Creative Performance")
    rows = q.grid_rows(sample, i + 40)
    assert all("BARCK" not in n for n, _ in rows)
    assert len(rows) < 60


def test_every_real_fixture_stays_clean_of_the_new_rules():
    """Seven real reports. The word-reading rules must not invent work."""
    from app.checks.parser import pdf_text
    fixtures = sorted((Path(__file__).parent / "fixtures").glob("*.pdf"))
    if not fixtures:
        pytest.skip("no fixtures")
    noisy = {}
    for f in fixtures:
        text = pdf_text(f)
        ctx = {"text": text, "market": "", "path": f}
        for fn in (q.check_strategy_categorized, q.check_truncated_text,
                   q.check_conversion_names, q.check_creative_names,
                   q.check_social_mirror_sizes, q.check_widget_errors):
            out = fn(ctx)
            if out:
                noisy.setdefault(f.name, []).extend(x["title"] for x in out)
    assert noisy == {}, noisy


# ------------------------------------------- social placement vs its totals
def test_the_placement_grid_is_read_despite_its_prose_column(sample):
    """A "Where your ads appear" paragraph sits between the name and the
    numbers, so "every cell after the first is a number" is false of every row.
    """
    m = q.PLACEMENT_GRID.search(sample)
    rows = q._placement_rows(sample, m.end())
    assert len(rows) == 10
    assert rows[0] == ("Facebook Feed", 389012.0, 9887.0)
    assert rows[-1][0] == "Unknown"


def test_the_platform_tiles_are_read(sample):
    assert q._tile(sample, "Facebook News Feed Performance") == (810307.0, 16116.0, 1.99)
    assert q._tile(sample, "Instagram Performance") == (201135.0, 2778.0, 1.38)


def test_a_grid_under_its_total_is_fine(sample):
    """The grid shows ten placements and Meta has more than ten, so coming in
    under the total is the normal case and says nothing."""
    assert q.check_social_placement_totals({"text": sample}) == []


def test_a_grid_over_its_total_is_a_double_count():
    text = ("PUBLISHERS & INVENTORY - PAGE 1\n"
            "Social Placement Performance\n"
            " Placement      Where your ads appear   Impressions   Clicks   CTR\n"
            "Facebook Feed   Your ads appear here.      20,000       400   2.00%\n"
            "Facebook Reels  Your ads appear here.      20,000       400   2.00%\n"
            "Facebook News Feed Performance\n"
            "  35,785   857   2.39%\n"
            "  Impressions  Clicks  CTR\n")
    out = q.check_social_placement_totals({"text": text})
    assert any(f["code"] == "placement_over_total" for f in out)


def test_a_tile_whose_ctr_disagrees_with_itself_is_found():
    text = ("Social Placement Performance\n"
            "Facebook News Feed Performance\n"
            "  35,785   857   9.99%\n"
            "  Impressions  Clicks  CTR\n")
    out = q.check_social_placement_totals({"text": text})
    assert any(f["code"] == "tile_ctr" for f in out)


def test_threads_and_unknown_are_not_counted_against_a_platform(sample):
    """Neither belongs to Facebook or Instagram, and adding them to either
    would push the grid over the total and invent a double count."""
    assert q._platform_of("Threads") == ""
    assert q._platform_of("Unknown") == ""
    assert q._platform_of("Instagram Reels") == "instagram"
    assert q._platform_of("Audience Network (Native, Banner, and Interstital)") == "audience"


# ---------------------------------------------------------------- the working
def test_a_trace_is_cleaned_before_anyone_reads_it():
    """TapClicks' icon font leaks private-use glyphs into the text layer, and a
    device name arrives with its whole Description column glued on."""
    from app.checks.rules import _clean, _short_name
    assert _clean("Site and App  Performance") == "Site and App Performance"
    assert _short_name(
        "Desktop A personal computing device that remains stationary.") == "Desktop"
    assert _short_name("Connected TV An internet enabled device") == "Connected TV"
    assert _short_name("Acme - Behavioral Display", 60) == "Acme - Behavioral Display"


def test_the_ctr_finding_carries_its_arithmetic():
    """The point of the trace: the numbers that produced the verdict, without
    anyone having to read the code to find out where they came from."""
    from app.checks.rules import run_all
    pdf = Path(__file__).parent / "fixtures" / "central_penn.pdf"
    if not pdf.exists():
        pytest.skip("fixture missing")
    r = run_all(pdf)
    f = next(x for x in r["findings"] if x["code"] == "ctr_excludes_products")
    labels = [t["label"] for t in f["trace"]]
    assert "Stated CTR" in labels
    assert "After leaving those out" in labels
    assert "Filtered clicks / filtered impressions" in labels


# ------------------------------------------------------------ site CTR
def test_a_site_clicking_at_46_percent_is_found():
    """The real one: "Slicing Hero: Sword Master", 783 impressions, 365 clicks.
    An ad under a button people are trying to press."""
    text = ("PUBLISHERS & INVENTORY - PAGE 1\n"
            "Site and App Performance\n"
            " Name    Impressions   Clicks   CTR\n"
            "imdb.com                     9,401     2    0.02%\n"
            "T-Mobile Play                7,321   168    2.29%\n"
            "Slicing Hero: Sword Master     783   365   46.62%\n")
    out = q.check_site_ctr({"text": text})
    assert len(out) == 1 and out[0]["severity"] == "fail"
    assert "Slicing Hero" in out[0]["detail"]
    assert "T-Mobile Play" not in out[0]["detail"]


def test_a_handful_of_impressions_is_not_evidence():
    """9 clicks on 30 impressions is 30% and means nothing."""
    text = ("Site and App Performance\n"
            " Name   Impressions   Clicks   CTR\n"
            "Tiny App      30     9   30.00%\n")
    assert q.check_site_ctr({"text": text}) == []


def test_the_samples_sites_are_all_below_the_ceiling(sample):
    assert q.check_site_ctr({"text": sample}) == []


def test_a_glued_widget_title_is_stripped_from_a_site_name():
    """The last row of a grid absorbs the next widget's heading as if it were a
    wrapped name, so "minefun.io" arrived as "minefun.io Top CTV Publishers"."""
    text = ("Site and App Performance\n"
            " Name   Impressions   Clicks   CTR\n"
            "minefun.io Top CTV Publishers   95   24   25.26%\n")
    out = q.check_site_ctr({"text": text})
    assert "minefun.io:" in out[0]["detail"]
    assert "Publishers" not in out[0]["detail"]


# ------------------------------------------- video and audio owe a rate
def test_the_sample_reports_completion_for_every_watched_product(sample):
    """Every video and audio section of the everything-sample carries it."""
    assert q.check_completion_present({"text": sample}) == []


def test_a_video_section_with_no_completion_is_found():
    text = ("VIDEO ADS - PAGE 1\n"
            "Video Creative Performance\n"
            "Preview   Creative Name   Impressions   Clicks   CTR\n"
            "          spot.mp4          1,000   10   1.00%\n")
    out = q.check_completion_present({"text": text})
    assert len(out) == 1 and out[0]["severity"] == "fail"
    assert "Video" in out[0]["detail"]


def test_an_online_audio_section_with_no_completion_is_found():
    text = ("ONLINE AUDIO ADS - PAGE 1\n"
            "Online Audio Creative Performance\n"
            "Creative Name   Impressions   Clicks   CTR\n"
            "spot.mp3            2,762    0   0.00%\n")
    out = q.check_completion_present({"text": text})
    assert "Online Audio" in out[0]["detail"]


def test_completion_reported_as_a_column_counts(sample):
    """Social Mirror CTV has no completion WIDGET - its rate is a column inside
    the creative grid. A list of exact widget titles would have failed it."""
    body = q.section_bodies(sample)["SOCIAL MIRROR CTV ADS"]
    assert "Completion Performance" not in body
    assert "Video Completion Rate" in body
    assert q.check_completion_present({"text": sample}) == []


def test_display_and_dooh_do_not_owe_a_completion_rate():
    """Nothing gets watched to the end on a display banner or a billboard."""
    text = ("DISPLAY ADS - PAGE 1\nDisplay Creative Performance\n"
            "DOOH ADS - PAGE 1\nDOOH Creative Performance\n")
    assert q.check_completion_present({"text": text}) == []


def test_amazon_display_alone_does_not_owe_one():
    """Amazon Premium Display shares the section and has nothing to complete."""
    text = ("AMAZON ADS - PAGE 1\n"
            "Amazon Premium Display Creative Performance\n"
            "Creative Name   Preview Link   Impressions   Clicks   CTR\n"
            "banner.jpg      Click to View     1,000   10   1.00%\n")
    assert q.check_completion_present({"text": text}) == []


def test_amazon_video_in_the_same_section_does_owe_one():
    text = ("AMAZON ADS - PAGE 1\n"
            "Amazon Premium Video Creative Performance\n"
            "Creative Name   Preview Link   Impressions   Clicks   CTR\n"
            "spot.mp4        Click to View     1,000   10   1.00%\n")
    out = q.check_completion_present({"text": text})
    assert len(out) == 1 and "Amazon Premium Video" in out[0]["detail"]


# The older template prints no section banners at all, so there is nothing to
# look inside and the question has to be asked of the whole report.
def test_a_report_with_no_section_banners_falls_back_to_its_products():
    out = q.check_completion_present(
        {"text": "Digital Marketing Report\nCTV Creative Performance\n",
         "products": {"CTV", "Display"}})
    assert len(out) == 1 and "CTV" in out[0]["detail"]


def test_the_fallback_is_satisfied_by_the_word_anywhere():
    out = q.check_completion_present(
        {"text": "Video Completion Performance by Line Item\n",
         "products": {"Video"}})
    assert out == []


def test_the_fallback_says_nothing_without_a_watched_product():
    assert q.check_completion_present(
        {"text": "Display Creative Performance\n",
         "products": {"Display", "Mobile Conquesting"}}) == []


def test_both_real_fixtures_with_video_report_their_completion():
    """central_penn runs CTV, watsontown runs CTV and Video. Both print it."""
    from app.checks.parser import pdf_text
    for stem in ("central_penn", "watsontown"):
        f = Path(__file__).parent / "fixtures" / f"{stem}.pdf"
        if not f.exists():
            pytest.skip("fixture missing")
        assert "Completion" in pdf_text(f)


def test_dooh_and_tiktok_never_owe_a_completion_rate():
    """Nothing gets watched to the end on a billboard, and TikTok does not
    report completion. Neither is in the owed list, on either path."""
    from app.checks.quality import COMPLETION_OWED, WATCHED_PRODUCTS
    owed = {s for s, _o in COMPLETION_OWED}
    assert "DOOH ADS" not in owed and "TIKTOK ADS" not in owed
    assert "DOOH" not in WATCHED_PRODUCTS and "TikTok" not in WATCHED_PRODUCTS

    text = ("DOOH ADS - PAGE 1\nDOOH Creative Performance\n"
            "TIKTOK ADS - PAGE 1\nTikTok Creative Performance\n")
    assert q.check_completion_present({"text": text}) == []
    assert q.check_completion_present(
        {"text": "DOOH Creative Performance\n",
         "products": {"DOOH", "TikTok"}}) == []


def test_a_dooh_video_line_item_is_dooh_not_video():
    """"... Venue Targeting DOOH Video" was read as Video by the generic tail,
    which put a billboard campaign on the report as a video product - and would
    then have demanded a completion rate for it."""
    import re as _re
    from app.checks.products import TAIL_PATTERNS

    def tail(name):
        return next((p for p, rx in TAIL_PATTERNS if _re.search(rx, name)), None)

    assert tail("Acme - Venue Targeting DOOH Video") == "DOOH"
    assert tail("Acme - Venue Targeting DOOH Display") == "DOOH"
    assert tail("Acme - Venue Targeting DOOH") == "DOOH"
    # and the generic tails still work on everything else
    assert tail("Acme - Retargeting Video") == "Video"
    assert tail("Acme - AI Display") == "Display"


# ------------------------------------------------------------ geo-fencing
def test_an_empty_geofence_widget_is_not_a_pass():
    """TapClicks prints the heading and the column header whether or not there
    is data under them. "The heading is here" was enough to tick "Every
    geo-fencing row has a business name" on reports with no geo-fencing rows
    at all - a claim about nothing."""
    from app.checks.rules import _geofence_rows, _rule_applies, check_geofence_names

    empty = ("Mobile Conquesting Geo-Fencing Performance\n"
             "Business Name   Address   City   State   Zip   Impressions\n"
             "\n")
    assert _geofence_rows(empty) == []
    assert _rule_applies(check_geofence_names, {"text": empty}) is False


def test_a_geofence_widget_with_rows_is_judged():
    from app.checks.rules import _geofence_rows, _rule_applies, check_geofence_names

    filled = ("Mobile Conquesting Geo-Fencing Performance\n"
              "Business Name   Address   City   State   Zip   Impressions   Clicks   CTR\n"
              "Bay City Point  500 E 23rd St  Panama City  FL  32405  3,330  6  0.18%\n")
    assert len(_geofence_rows(filled)) == 1
    assert _rule_applies(check_geofence_names, {"text": filled}) is True


def test_no_geofence_widget_at_all_still_abstains():
    from app.checks.rules import _rule_applies, check_geofence_names
    assert _rule_applies(check_geofence_names,
                         {"text": "Display Creative Performance\n"}) is False


def test_the_samples_geofence_rows_are_found(sample):
    from app.checks.rules import _geofence_rows
    assert len(_geofence_rows(sample)) >= 15


# ------------------------------------------------------------ store visits
STORES = ("VISITS - PAGE 1\n"
          "Mobile Conquesting Visit Performance   Mobile Conquesting Number of Store Locations\n"
          "\n"
          "                                                          3\n"
          "                                        " + q.LOCATIONS_LABEL + "\n"
          "\n"
          "                                        Mobile Conquesting Visits by Store Location\n"
          "                                        Business Name   Address   City   State   Zip   Visits\n"
          "\n"
          "                                        Close's Lumber   142 Davis St   Bradford   PA   16701   200\n"
          "                                        Close's Lumber   11 Buckler DR   Roulette   PA   16746     3\n"
          "                                        Close's Lumber   625 N Union ST   Olean   NY   14760     1\n"
          "\n"
          "           204                816\n"
          "\n"
          "        Visits               Estimated Visits (4x Verified Data)\n")


def test_a_consistent_visits_page_says_nothing():
    got = q.store_visits(STORES)
    assert got["locations"] == 3 and len(got["rows"]) == 3 and got["visits"] == 204
    assert q.check_store_visits({"text": STORES}) == []


def test_a_location_count_that_does_not_match_the_table_is_found():
    text = STORES.replace("                          3\n", "                          5\n")
    out = q.check_store_visits({"text": text})
    assert any(f["code"] == "store_locations_mismatch" for f in out)


def test_visits_that_do_not_match_the_table_are_found():
    text = STORES.replace("           204     ", "           250     ")
    out = q.check_store_visits({"text": text})
    f = next(x for x in out if x["code"] == "store_visits_mismatch")
    assert "204" in f["detail"] and "250" in f["detail"]


def test_a_clipped_table_is_not_expected_to_add_up():
    """Ten rows of a longer list cannot sum to the whole, and saying so every
    month would be noise, not a finding."""
    text = STORES.replace("Business Name",
                          "Grid contains more rows, but they have been clipped.\nBusiness Name")
    text = text.replace("           204     ", "           250     ")
    assert q.check_store_visits({"text": text}) == []


def test_the_samples_visits_page_reconciles(sample):
    got = q.store_visits(sample)
    assert got and got["locations"] == 1 and got["rows"] == [1.0] and got["visits"] == 1
    assert q.check_store_visits({"text": sample}) == []


def test_a_report_with_no_visits_page_abstains():
    from app.checks.rules import _rule_applies, check_store_visits
    assert q.store_visits("Display Creative Performance\n") is None
    assert _rule_applies(check_store_visits, {"text": "Display Ads\n"}) is False


def test_a_display_creative_widget_really_does_mean_display_ran():
    """Field Of Dreams looked like a false positive - a Mobile Conquesting
    report credited with Display off a line item called "AI Display". It turned
    out to carry a Display Creative Performance widget, so the finding was
    right. The tails were nearly removed over a bug that was not there."""
    from app.checks.parser import extract_tables
    from app.checks.products import detect
    text = ("Display Creative Performance\n"
            "Preview Image   Creative Name   Impressions   Clicks   CTR\n"
            "                Field Of Dreams_7.14__888x138   11,199   117   1.04%\n")
    assert "Display" in detect(text, extract_tables(text, strict=True))


# ---------------------------------------------------------------------- DOOH
DOOH_AND_REST = ("DOOH ADS - PAGE 1\n"
                 "DOOH Line Item Performance\n"
                 "Strategy Name                       DOOH Ads Served\n"
                 "\n"
                 "Service One CU - Venue Targeting DOOH Video    36,666\n"
                 "\n"
                 "LINE ITEMS - PAGE 1\n"
                 "Line Item Performance\n"
                 "Line Item Name          Impressions   Clicks   CTR\n"
                 "Service One CU - Auto Loans     64,242   500   0.78%\n"
                 "Service One CU - AI CTV         36,057   103   0.29%\n")


def test_a_dooh_row_counts_towards_the_line_item_total():
    """DOOH counts in "DOOH Ads Served" and has no clicks or CTR column, so its
    rows are a name and one number. The three-cell rule that keeps prose out of
    every other grid threw all of them away, and Service One Credit Union was
    failed for a line item sum that came up short by exactly its DOOH figure."""
    rows = q.line_item_totals(DOOH_AND_REST)
    dooh = [r for r in rows if "DOOH" in r[0]]
    assert dooh == [("Service One CU - Venue Targeting DOOH Video", 36666.0, 0.0)]
    assert sum(r[1] for r in rows) == 36666 + 64242 + 36057


def test_a_dooh_row_brings_no_clicks_with_it():
    """There is no clicks column to read, and inventing one would break the
    other half of the same check."""
    assert all(r[2] == 0.0 for r in q.line_item_totals(DOOH_AND_REST)
               if "DOOH" in r[0])


def test_the_two_cell_rule_is_only_used_on_dooh_grids():
    """Everywhere else a two-cell line is a wrapped name or a stray caption,
    and taking them as rows is how a grid runs away with itself."""
    text = ("LINE ITEMS - PAGE 1\n"
            "Line Item Performance\n"
            "Line Item Name    Impressions   Clicks   CTR\n"
            "Acme - AI Display     14,524   163   1.12%\n"
            "Some caption           99\n")
    names = [n for n, _ in q.line_item_names(text)]
    assert names == ["Acme - AI Display Some caption 99"] or "Some caption" not in names


def test_the_samples_line_items_now_include_its_dooh(sample):
    rows = q.line_item_totals(sample)
    dooh = [r for r in rows if "DOOH" in r[0]]
    assert len(dooh) == 10
    assert sum(r[1] for r in dooh) == 9540
    # and the whole grid now lands within half a percent of the top line
    assert abs(sum(r[1] for r in rows) - 5_168_436) / 5_168_436 < 0.005


# ------------------------------------------------------------ file names
def test_a_duplicate_download_suffix_is_not_part_of_the_client_name():
    """"... 52753 (1).pdf" made "Service One Credit Union (1)" a different
    client from "Service One Credit Union" - it matched no order and filed
    itself as a new report instead of replacing the one it corrects."""
    from app.checks.parser import meta_from_filename as m
    plain = m("July 2026_Service One Credit Union 52750 52753.pdf")
    for name in ("July 2026_Service One Credit Union 52750 52753 (1).pdf",
                 "July 2026_Service One Credit Union 52750 52753 (12).pdf",
                 "July 2026_Service One Credit Union 52750 52753 copy.pdf",
                 "July 2026_Service One Credit Union 52750 52753 copy 2.pdf",
                 "July 2026_Service One Credit Union 52750 52753 (1) copy.pdf"):
        assert m(name) == plain, name


def test_a_bracketed_number_inside_a_real_name_is_left_alone():
    """Only a trailing one is a duplicate marker."""
    from app.checks.parser import meta_from_filename as m
    assert m("July 2026_Store (1) Ltd 52746.pdf")["client"] == "Store (1) Ltd"


def test_pdf_pages_reads_the_whole_document_in_one_call():
    """The blank-page check ran one pdftotext PER PAGE - forty-one subprocesses
    on a forty-one page report, and most of the wait after an upload."""
    from app.checks.parser import pdf_pages, page_count
    f = Path(__file__).parent / "fixtures" / "watsontown.pdf"
    if not f.exists():
        pytest.skip("fixture missing")
    pages = pdf_pages(f)
    assert len(pages) == page_count(f)
    assert "Watsontown" in "".join(pages)
