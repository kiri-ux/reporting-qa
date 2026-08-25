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
    assert len(names) == 3756


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
