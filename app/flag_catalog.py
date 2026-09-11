"""Every check the tool runs, in words, grouped for reading.

WHY THIS EXISTS SEPARATELY FROM `CHECKS`.

The rules sheet said what makes a report OWED and nothing about what is done
to it once it arrives - so the answer to "what does this thing actually look
for" was the findings list on whichever report happened to be open, which only
ever shows the ones that fired. Forty checks, and no way to see the forty.

The label in `CHECKS` is the claim being made about a passing report - "Line
item totals add up to the headline". That is the right sentence for a checklist
on a report and the wrong one for a catalog: it does not say what a failure
looks like or why anybody cares. So each one gets a second line here, written
the other way round - what goes wrong.

AND THEN WHO FIXES IT, which is the question a finding actually raises. A
finding says a report is wrong; it does not say whose desk it goes to, and
somebody reading one for the first time has no way to tell a template problem
from an order problem from a broken feed. Three answers:

  reporter  whoever pulls the report. Template, widgets, filters, date range,
            the logo, what got left switched on.
  buyer     whoever set the campaign up. The order, the products on it, the
            budget, and the names given to strategies, fences and conversions.
  admin     a data error. Goes to Alyssa on the reporting team.

AND MOST OF THEM SAY "VERIFY FIRST", which is the honest shape of it. A
finding is the tool's reading of a PDF, and the tool misreads pages. Sending
one straight to somebody else's desk without a person confirming it is how a
false positive becomes an afternoon of two people's time - so the owner tag on
those rows is a second step, not the first.

A test walks `CHECKS` and fails if anything is missing from this file. A check
nobody can find is a check that gets argued about from memory.

`how` IS ALLOWED TO BE EMPTY AND SAYS SO ON THE PAGE. The ones written here are
what the checking code itself knows - what to look at to tell which cause it
is. What happens next at Vici is process rather than code, and a plausible
guess printed in that column would be worse than a blank one, because somebody
would follow it. The page counts the blanks.

AND IT LIVES OUTSIDE `app/checks/` ON PURPOSE. The rules fingerprint is a hash
of every file in that folder, and anything whose hash changes puts every report
on the board in the queue to be judged again. This file is prose about the
rules, not a rule - a wording fix here should not cost seven hundred reports a
re-read.
"""
from __future__ import annotations

REPORTER = "reporter"
BUYER = "buyer"
ADMIN = "admin"

PACKS_MEANS = ("The report can still go out. The fix is a message to send, "
               "not something to hold delivery for.")

VERIFY_MEANS = ("Check it yourself before it goes anywhere. A finding is the "
                "tool's reading of a PDF and the tool misreads pages, so a "
                "false positive sent on is two people's afternoon.")

WHO_MEANS = {
    REPORTER: "Whoever pulls the report. Template, widgets, filters, the date "
              "range, the logo, and whatever got left switched on.",
    BUYER: "Whoever set the campaign up. The order, its products and budget, "
           "and the names given to strategies, fences and conversions.",
    ADMIN: "Data error, send to Alyssa on the reporting team to look into.",
}

# (group title, [(check function name, what going wrong looks like, who fixes
#  it, verify it yourself first, how to fix it - "" where nobody has written it
#  down yet)])
FLAG_GROUPS: list[tuple[str, list[tuple[str, str, str, bool, str]]]] = [
    ("The numbers agree with each other", [
        ("check_headline_ctr",
         "The big CTR at the top does not match the impressions and clicks "
         "printed beside it.",
         ADMIN, True,
         "Do the division yourself first - IMPR/CLICKS * 100. If it comes "
         "out to the printed CTR the tool misread the page and the flag "
         "can be checked off. If it doesn't match, alert Alyssa."),
        ("check_row_math",
         "A row's own CTR does not match that row's own impressions and "
         "clicks.",
         ADMIN, True,
         "Do the division yourself first - IMPR/CLICKS * 100. If it comes "
         "out to the printed CTR the tool misread the page and the flag "
         "can be checked off. If it doesn't match, alert Alyssa."),
        ("check_line_items",
         "The line items do not add up to the campaign total above them.",
         ADMIN, True,
         "This is usually a line item filtered out of the widget rather "
         "than a wrong number. Total the impressions of the rows before "
         "sending to Alyssa."),
        ("check_creative",
         "The creative rows add up to MORE than the line item they belong to.",
         ADMIN, True,
         "Verify the numbers first, send to Alyssa if you're seeing more "
         "impressions in the creative section than what is reported for "
         "total impressions for the product."),
        ("check_device",
         "The device breakout describes more impressions than were served.",
         ADMIN, True,
         "Verify the numbers first, send to Alyssa if you're seeing more "
         "impressions in the device section than what is reported for "
         "total impressions."),
        ("check_social_placement_totals",
         "Facebook and Instagram placements add up to more than the platform "
         "tiles say.",
         ADMIN, True,
         "Verify the numbers first, send to Alyssa if you're seeing more "
         "impressions in the placements section than what is reported for "
         "total impressions for the product."),
        ("check_store_visits",
         "The store visit figure does not agree with the table underneath it.",
         ADMIN, True,
         "If the store visits in the grid are less than the big number "
         "AND there is blank space under the grid (meaning more rows "
         "could fit), send to Alyssa. If it's clear there are more "
         "locations than can be visible, check off the flag."),
        ("check_month_within_lifetime",
         "One month reports more than the whole campaign has ever delivered.",
         REPORTER, False,
         "This is telling you that the monthly report for this client has "
         "more serve than the lifetime report. One of the two reports has "
         "the wrong date range on it, probably the lifetime. Check the "
         "start and end dates on both reports."),
    ]),
    ("What the client bought", [
        ("check_products",
         "A product on the report that is not on the live orders, or a product "
         "they are paying for that is not on the report. Website Visitor ID "
         "and Additional Billing are never expected - they are billed line "
         "items with no widget.",
         BUYER, True,
         "Verify the report is missing the product, then open the order "
         "lines to see the current status of the product. If there is a "
         "live product for the current month that is not showing on the "
         "report, alert the buyer to check the connections."),
        ("check_strategy_categorized",
         "A strategy line that does not name the product it runs, so it cannot "
         "be checked against the order.",
         BUYER, False,
         "The strategy line is missing the product name from the report, "
         "so the donut chart on page one isn't correctly showing the "
         "product name. Alert the buyer."),
        ("check_pacing_off",
         "Delivery or spend more than 50% off what the order asked for, "
         "either way. Not flagged when it is OVER and a cancelled line item "
         "ran that month - that buy served before it was stopped.",
         BUYER, False,
         "You can package the report, but flag the buyer in case this is a "
         "reporting issue."),
        ("check_lifetime_goal",
         "A finished campaign that did not deliver what it was sold.",
         BUYER, False,
         "You can package the report, but flag the buyer in case this is "
         "a reporting issue."),
    ]),
    ("The right report for the right client", [
        ("check_client_wrong",
         "Data on the report that belongs to a different client, or a report "
         "sitting in a different client's row.",
         REPORTER, False,
         "Verify this first - it could be a typo or abbreviation preventing "
         "the tool from seeing that match. Use your judgment if it's the right "
         "client, pull with the correct client if not."),
        ("check_date_range",
         "The printed date range is not the period this report claims to "
         "cover.",
         REPORTER, False,
         "Verify then repull with the correct dates, if needed."),
        ("check_market_logo",
         "Page one carries the reporting tool's own mark instead of the "
         "partner's or the client's.",
         ADMIN, True,
         "The template's logo slot was left at the default. Verify, then "
         "alert Alyssa to update."),
    ]),
    ("Widgets that should be there, and ones that should not", [
        ("check_required_widgets",
         "A product is on the report without the widget it owes - CTV with no "
         "completion rates, Mobile Conquesting with no fence breakout.",
         ADMIN, True,
         "Verify the widget should be there, flag Alyssa if something is "
         "truly missing."),
        ("check_rogue_widgets",
         "A widget for a product this buy does not include - an Amazon Premium "
         "Display breakout on an Amazon CTV + Video buy, PPC pages on a buy "
         "with no PPC.",
         ADMIN, True,
         "Verify against the order, then flag Alyssa - the widget is reporting "
         "a product the client did not buy."),
        ("check_geofence_widget",
         "Geo-fenced Mobile Conquesting with no geo-fencing breakout behind "
         "it.",
         ADMIN, True,
         "Verify the widget should be there, flag Alyssa if something is "
         "truly missing."),
        ("check_completion_present",
         "A video or audio product that never says how much of it got "
         "watched.",
         ADMIN, True,
         "Verify the widget should be there, flag Alyssa if something is "
         "truly missing."),
        ("check_rogue_ctv",
         "A CTV widget on a report with no CTV on it - usually the template "
         "left switched on.",
         ADMIN, True,
         "Verify the widget shouldn't be there, flag Alyssa if something "
         "is truly missing."),
        ("check_blank_pages",
         "A page that came out blank where a widget should be.",
         REPORTER, False,
         "Delete the blank page."),
        ("check_widget_errors",
         "A widget that printed an error message where its table should be.",
         REPORTER, False,
         "Delete the errored widget page."),
        ("check_page_banners",
         "The template's section banners were left switched on.",
         REPORTER, False,
         "Repull without the section headers."),
    ]),
    ("Creative", [
        ("check_thumbnails",
         "A creative preview that did not render - an empty box where the ad "
         "should be, or a thumbnail missing message.",
         REPORTER, False,
         "All previews are now in, resend the report from the scheduler."),
        ("check_blank_screenshots",
         "An ad screenshot cell with no screenshot in it.",
         ADMIN, True,
         "If the error is because of a broken image, not a missing image, "
         "flag Alyssa. Otherwise you can mark off the flag."),
        ("check_variant_preview_links",
         "A variant with no PREVIEW LINK. Separate from the screenshot check "
         "above.",
         ADMIN, True,
         "Verify, then alert Alyssa if missing."),
        ("check_creative_names",
         "A creative row that does not say which creative it is.",
         ADMIN, True,
         "Verify you see a blank creative name, alert Alyssa if yes."),
        ("check_creative_shape",
         "A Social Mirror preview that is a display banner - a leaderboard or "
         "a banner sitting in the Social Mirror grid, where the artwork should "
         "be a square, a feed image or a story. The name may be right; the "
         "creative is not.",
         BUYER, True,
         "Look at the preview column. A social creative is square, 1200x628 or "
         "a tall story; a leaderboard or a banner in that grid is a display "
         "build that went out as a social ad. Flag the buyer to have the "
         "right creative loaded."),
        ("check_social_mirror_sizes",
         "A Social Mirror creative named with an ad size, which is a display "
         "name on a social ad.",
         BUYER, True,
         "Verify, then flag the buyer to fix the Social Mirror naming."),
    ]),
    ("Completion rates", [
        ("check_ctv_tile",
         "The CTV completion rate on page one does not sit inside the CTV "
         "figures on the rest of the report, so it is built over rows that "
         "are not CTV.",
         ADMIN, True,
         "Verify, then alert Alyssa. The client has been sent a number that "
         "matches nothing else on their report - it needs resending."),
        ("check_completion_rates",
         "A completion rate above 100%, in any widget that has the column.",
         ADMIN, True,
         "Verify, then alert Alyssa."),
        ("check_zero_completion",
         "A completion widget sitting at 0% all the way down, which is a "
         "broken widget rather than a result.",
         ADMIN, True,
         "Verify, then alert Alyssa."),
        ("check_some_zero_completion",
         "One video, CTV or audio row at 0% among others that watched fine.",
         ADMIN, True,
         "Verify, then alert Alyssa."),
    ]),
    ("Names and labels", [
        ("check_geofence_names",
         "A geo-fencing row with no business name on it.",
         BUYER, True,
         "Okay to package but alert the buyer to check if they should be "
         "there."),
        ("check_conversion_names",
         "A conversion named for a tag or an id rather than what the user "
         "did.",
         BUYER, False,
         "Rename it to the action - 'Called', 'Form submitted'. The "
         "client is being asked to read the tag manager otherwise."),
        ("check_devices_known",
         "A row of the device breakout that is not an actual device.",
         ADMIN, False,
         "Verify, then send to Alyssa to fix, if needed."),
        ("check_truncated_text",
         "Text cut off for want of space.",
         REPORTER, False,
         "Verify, repull the report directly from the dashboard if needed "
         "to expand the widget size."),
    ]),
    ("Sites", [
        ("check_site_ctr",
         "A site clicking at a rate a person would not - the usual sign of "
         "bot traffic on a placement.",
         BUYER, False,
         "Okay to package but alert the buyer to check on the high CTR."),
    ]),
]


def flags() -> list[dict]:
    """[{group, checks: [{label, what, who, how}]}] - the catalog, ready to render.

    The label comes from `CHECKS`, so the two can never drift into saying
    different things about the same rule.
    """
    from .checks.rules import CHECKS

    labels = {fn.__name__: label for fn, label in CHECKS}
    out = []
    for title, items in FLAG_GROUPS:
        rows = [{"key": key, "label": labels.get(key, key), "what": what,
                 "who": who, "verify": verify, "how": how,
                 # OKAY TO PACKAGE. Read out of the fix itself rather than kept
                 # as a separate flag, so it cannot say one thing while the
                 # words beside it say another - change the words and the icon
                 # follows.
                 "packs": "packag" in (how or "").lower()}
                for key, what, who, verify, how in items if key in labels]
        if rows:
            out.append({"group": title, "checks": rows})
    return out


def unwritten() -> int:
    """How many checks still have nobody's fix written against them.

    On the page rather than in somebody's head: a column half filled in looks
    finished from a distance, and the blanks are the point of it.
    """
    return sum(1 for _t, items in FLAG_GROUPS for _k, _w, _o, _v, how in items
               if not how)


def described() -> set[str]:
    return {key for _t, items in FLAG_GROUPS for key, _w, _o, _v, _h in items}


# WHAT WAS ASKED ABOUT A CHECK, AND WHAT THE ANSWER WAS.
#
# Round-tripped through the sheet so a question does not have to be asked
# twice. NOT shown on the flags page - it is a conversation about the check
# rather than something a person reading a finding needs.
NOTES: dict[str, str] = {
    "check_creative":
        "asked: only flag when the impressions are GREATER than the product "
        "reports. Done - the under-by branch is gone.",
    "check_completion_rates":
        "asked: what percentage other than a completion rate is ever over "
        "100%? Right question, and the answer is none. Run against every "
        "stored report, the old check_rate_ceiling found exactly one figure "
        "outside a Completion Performance widget - Pluto TV at 100.51% in "
        "Watsontown's Top CTV Publishers grid, which is a completion rate in a "
        "widget with a different name. So this check reads the COLUMN now "
        "rather than the widget title, it catches that row by name, and "
        "check_rate_ceiling is deleted.",
    "check_pacing_off":
        "asked: what is check_pacing, and then - one check, not two rows for "
        "the same flag with two metrics. Merged. Impressions and dollars both "
        "still run and a report is checked on whichever it has; they just "
        "report as one line now, on the page and on every report's checklist.",
    "check_client_wrong":
        "asked: same as the one above, merge them. Done - one row. Both halves "
        "still run: the line items against the cover page, and the cover page "
        "against the row it arrived in.",
    "check_strategy_categorized":
        "asked: only flag when it shows on the donut on the title page. That "
        "is what it already means - the donut IS the product breakout, and a "
        "strategy with no product word in its name is what lands on it as its "
        "own slice. If you have one that is flagged and does NOT show on the "
        "donut, send it and I will narrow it.",
}


# ------------------------------------------------- what to call a finding
# THE FILTER WAS NAMED AFTER WHICHEVER REPORT GOT THERE FIRST.
#
# There has never been a table of code to name. The checks carry a label, the
# findings carry a code, and nothing joined them - so the Findings filter was
# built out of the findings' own titles, which carry that report's numbers:
# "Campaign finished 43% under its goal", "3 creative previews did not render",
# "Meta is 57% short". Three reports, three menu entries, one problem.
#
# Cutting the numbers out with a regular expression got most of the way and
# read like it: "1 of 8 variants have no preview link" came out as "of variants
# have no preview link". A name is a written sentence, so they are written.
#
# AND THE SAME PROBLEM IS ONE ENTRY. A row's CTR, a tile's CTR and the
# top-line CTR are one question - does the arithmetic on this page work - and
# splitting them across three menu entries makes somebody pick three to see
# what is really one list. Codes stay separate on the report, where the
# difference is the point; they merge here, where the question is what to go
# and look at.
#
# THIS FILE IS OUTSIDE app/checks/ ON PURPOSE - see the top of it. Renaming a
# finding is prose, and it should not cost seven hundred reports a re-read.
FINDING_KINDS: list[tuple[str, str, tuple[str, ...]]] = [
    # (key used in the URL, what the menu calls it, the codes it covers)
    ("ctr_mismatch", "CTR does not match its own numbers",
     ("headline_ctr", "tile_ctr", "row_ctr")),
    ("not_verifiable", "Could not be checked against its own numbers",
     ("ctr_unverifiable", "clicks_unverifiable")),
    ("line_item_totals", "Line items do not sum to the top line",
     ("line_items_impressions", "line_items_clicks")),
    ("creative_over", "Creative table claims more than was delivered",
     ("creative_over_top",)),
    ("device_mismatch", "Device breakout does not match what was served",
     ("device_over", "device_under")),
    ("placement_over", "Placement rows exceed the platform total",
     ("placement_over_total",)),
    ("store_mismatch", "Store visits do not match the table",
     ("store_locations_mismatch", "store_visits_mismatch")),
    ("month_over_lifetime", "The month reports more than the whole campaign",
     ("month_over_lifetime",)),
    ("totals_leave_out", "Top-line numbers leave some products out",
     ("clicks_exclude_products", "ctr_excludes_products",
      "clicks_part_explained")),
    ("product_missing", "Ordered but not on the report", ("product_missing",)),
    ("product_rogue", "On the report with no live order", ("product_rogue",)),
    ("pacing", "Delivery is off what was ordered", ("pacing", "pacing_off")),
    ("goal_short", "Campaign finished under its goal",
     ("lifetime_short_of_goal",)),
    ("strategy_uncategorized", "Strategy lines not categorized to a product",
     ("strategy_uncategorized",)),
    ("client_name_typo", "The order spells the client's name differently",
     ("client_name_typo",)),
    ("date_range_missing", "No date range printed on the report",
     ("date_range_missing",)),
    ("date_range_wrong", "Date range is not the report month",
     ("date_range_wrong",)),
    ("lifetime_range", "Lifetime range does not match the campaign",
     ("lifetime_short", "lifetime_cut", "lifetime_overrun")),
    ("wrong_client", "This is a different client's report",
     ("wrong_client", "wrong_client_file")),
    ("generic_logo", "Page one carries the default logo", ("generic_logo",)),
    ("widget_missing", "A widget these products owe is missing",
     ("widget_missing", "geofence_widget_missing")),
    ("widget_rogue", "A widget for a product this buy does not include",
     ("widget_rogue",)),
    ("widget_error", "A widget printed an error or no data",
     ("widget_error", "blank_widget_page")),
    ("page_banner", "Page banners still printing", ("page_banner",)),
    ("ctv_not_ctv", "CTV VCR not matching throughout",
     ("ctv_widget_no_ctv", "ctv_tile_off", "ctv_tile_unchecked")),
    ("completion_missing", "No completion rate on a product that owes one",
     ("completion_missing",)),
    ("completion_zero", "Completion rates at 0%",
     ("completion_all_zero", "completion_zero_row")),
    ("completion_over_100", "Completion rate above 100%",
     ("completion_over_100",)),
    ("site_ctr_high", "Sites clicking above the ceiling", ("site_ctr_high",)),
    ("previews_blank", "Creative previews did not render",
     ("missing_thumbnail", "blank_screenshot")),
    ("preview_link_blank", "Variants with no preview link",
     ("preview_link_blank",)),
    ("creative_name_blank", "Creatives with no name", ("creative_name_blank",)),
    ("social_mirror_ad_size", "Social Mirror creatives named with an ad size",
     ("social_mirror_ad_size",)),
    ("creative_shape", "A Social Mirror preview is a display banner",
     ("creative_shape",)),
    ("text_truncated", "Labels cut off", ("text_truncated",)),
    ("conversion_names", "Conversions badly named",
     ("conversion_name_blank", "conversion_name_retargeting")),
    ("geofence_no_business_name", "Geo-fence rows have no business name",
     ("geofence_no_business_name",)),
    ("unknown_device", "Unrecognized devices in the breakout",
     ("unknown_device",)),
    ("rule_error", "A check could not run", ("rule_error",)),
]

KIND_NAME: dict[str, str] = {key: name for key, name, _c in FINDING_KINDS}
KIND_OF: dict[str, str] = {code: key for key, _n, codes in FINDING_KINDS
                           for code in codes}


def kind_of(code: str) -> str:
    """Which menu entry this finding belongs under.

    An unknown code is its own kind rather than nothing: a check added and not
    written up here should still be filterable, under its bare code, which also
    makes it obvious that it needs a name.
    """
    return KIND_OF.get(code or "", code or "")


def kind_name(key: str) -> str:
    return KIND_NAME.get(key, key)


def kinds_for_check(name: str) -> list[str]:
    """The filter values that lead to what this check flags.

    The catalog is keyed on the CHECK; the board filters on the KIND of
    finding, and one check can write several codes that merge into one kind.
    This is the join, so "64 flagged" can be the link that shows you the 64.
    """
    from .checkctl import code_owners

    out: list[str] = []
    for code, owners in code_owners().items():
        if name in owners:
            key = kind_of(code)
            if key and key not in out:
                out.append(key)
    return sorted(out)
