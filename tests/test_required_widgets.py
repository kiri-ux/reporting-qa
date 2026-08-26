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
    "Top 10 YouTube Channel Performance",
    "Top 10 YouTube TV Channel Performance",
]


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


def test_one_of_two_ctv_widgets_is_still_short(sample):
    """CTV and Social Mirror CTV each owe their own publishers widget."""
    text = sample.replace("Top CTV Publishers", "Something Else", 1)
    out = check_required_widgets({"text": text, "products": set()})
    assert len(out) == 1
    assert out[0]["title"] == "Only 1 of 2 Top CTV Publishers widgets"


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


def test_youtube_plus_owes_both_of_its_widgets():
    text = "YOUTUBE+ ADS - PAGE 1\nYouTube+ Placement Performance\n"
    out = check_required_widgets({"text": text, "products": set()})
    assert [f["title"] for f in out] == ["No Top 10 YouTube Channel Performance widget"]


def test_youtube_on_the_order_with_no_section_still_owes_the_plus_widgets():
    """A report that dropped the YouTube pages entirely is the worst case."""
    out = check_required_widgets({"text": "OVERVIEW - PAGE 1\n",
                                  "products": {"YouTube Video Ads"}})
    assert len(out) == 2


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
