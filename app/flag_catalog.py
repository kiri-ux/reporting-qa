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
        ("check_social_mirror_sizes",
         "A Social Mirror creative named with an ad size, which is a display "
         "name on a social ad.",
         BUYER, True,
         "Verify, then flag the buyer to fix the Social Mirror naming."),
    ]),
    ("Completion rates", [
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
