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
  admin     nobody at this end. The figures printed do not agree with each
            other, or a widget did not render - the data or the platform is
            wrong and it goes upstream.

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

WHO_MEANS = {
    REPORTER: "Whoever pulls the report. Template, widgets, filters, the date "
              "range, the logo, and whatever got left switched on.",
    BUYER: "Whoever set the campaign up. The order, its products and budget, "
           "and the names given to strategies, fences and conversions.",
    ADMIN: "Nobody at this end. The figures do not agree with each other or a "
           "widget did not render, so it goes upstream.",
}

# (group title, [(check function name, what going wrong looks like, who fixes
#  it, how to fix it - "" where nobody has written it down yet)])
FLAG_GROUPS: list[tuple[str, list[tuple[str, str, str, str]]]] = [
    ("The numbers agree with each other", [
        ("check_headline_ctr",
         "The big CTR at the top does not match the impressions and clicks "
         "printed beside it.",
         ADMIN,
         "Do the division yourself first - clicks over impressions, as printed "
         "on the same tiles. If it comes out to the printed CTR the tool "
         "misread the page and it is ours; if it does not, the tile is "
         "computing over a different range than the two beside it."),
        ("check_row_math",
         "A row's own CTR does not match that row's own impressions and "
         "clicks.",
         ADMIN,
         "One row wrong among many that are right is a data problem on that "
         "row. Every row wrong is the column - check whether the CTR column is "
         "pulling from a different date range than the impressions."),
        ("check_line_items",
         "The line items do not add up to the campaign total above them.",
         REPORTER,
         "Usually a line item filtered out of the widget rather than a wrong "
         "number. Compare the count of rows against the order before assuming "
         "the arithmetic is wrong."),
        ("check_creative",
         "The creative rows do not add up to the line item they belong to.",
         REPORTER,
         "Same shape as the line item one: a creative missing from the grid "
         "reads as a total that is too small."),
        ("check_device",
         "The device breakout describes more impressions than were served.",
         ADMIN, ""),
        ("check_social_placement_totals",
         "Facebook and Instagram placements add up to more than the platform "
         "tiles say.",
         ADMIN, ""),
        ("check_store_visits",
         "The store visit figure does not agree with the table underneath it.",
         ADMIN, ""),
        ("check_month_within_lifetime",
         "One month reports more than the whole campaign has ever delivered.",
         REPORTER,
         "One of the two reports has the wrong date range on it, and it is "
         "nearly always the lifetime - a campaign-to-date pulled to this month "
         "rather than to the campaign's start. Check the printed range on both."),
        ("check_rate_ceiling",
         "A rate is above what that rate can be - a CTR over 5%, a completion "
         "rate over 100%.",
         ADMIN, ""),
    ]),
    ("What the client bought", [
        ("check_products",
         "A product on the report that is not on the live orders, or a product "
         "they are paying for that is not on the report. Website Visitor ID "
         "and Additional Billing are never expected - they are billed line "
         "items with no widget.",
         BUYER,
         "Open the order lines on the row before touching the report: a "
         "product that was cancelled, or one added after the report was built, "
         "explains most of these. Only what the ORDER says is wrong is the "
         "buyer's; a widget left switched on is the reporter's."),
        ("check_strategy_categorized",
         "A strategy line that does not name the product it runs, so it cannot "
         "be checked against the order.",
         BUYER,
         "The name is set where the campaign was built, so it is fixed there "
         "and not on the report. Renaming it makes every future report right "
         "as well as this one."),
        ("check_impression_pacing",
         "Delivery more than 50% off what the order asked for, either way.",
         BUYER,
         "The finding prints what was ordered and what was served. Cancelled "
         "line items are already out of the goal, so a gap this size is real "
         "pacing rather than arithmetic."),
        ("check_pacing",
         "A full month's spend that does not look like a full month's budget.",
         BUYER, ""),
        ("check_lifetime_goal",
         "A finished campaign that did not deliver what it was sold.",
         BUYER,
         "This is the last report the client gets on that campaign, so it is "
         "the last chance to answer for the gap. Worth a look before it goes "
         "out even when the number is close."),
    ]),
    ("The right report for the right client", [
        ("check_client_data",
         "Data on the report that belongs to a different client.",
         REPORTER,
         "A data source left pointed at the previous client. Stop before "
         "anything else - this one must not be delivered."),
        ("check_client_matches_order",
         "The report is for a different client than the row it arrived in.",
         REPORTER,
         "Two different things wear this: a report filed against the wrong row, "
         "or the right report named wrongly. The order id in the filename is "
         "what decides which."),
        ("check_date_range",
         "The printed date range is not the period this report claims to "
         "cover.",
         REPORTER,
         "Re-pull with the right range. A range one day out at either end is "
         "usually the calendar picker rather than a mistake."),
        ("check_market_logo",
         "Page one carries the reporting tool's own mark instead of the "
         "partner's or the client's.",
         REPORTER,
         "The template's logo slot was left at the default. It is the first "
         "thing on the page the client sees, so it does not go out like that."),
    ]),
    ("Widgets that should be there, and ones that should not", [
        ("check_required_widgets",
         "A product is on the report without the widget it owes - CTV with no "
         "completion rates, Mobile Conquesting with no fence breakout.",
         REPORTER,
         "The widget is missing from the template rather than empty. Add it and "
         "re-pull."),
        ("check_geofence_widget",
         "Geo-fenced Mobile Conquesting with no geo-fencing breakout behind "
         "it.",
         REPORTER, ""),
        ("check_completion_present",
         "A video or audio product that never says how much of it got "
         "watched.",
         REPORTER, ""),
        ("check_rogue_ctv",
         "A CTV widget on a report with no CTV on it - usually the template "
         "left switched on.",
         REPORTER,
         "Switch the section off. A widget with nothing behind it reads to the "
         "client as a product that failed."),
        ("check_blank_pages",
         "A page that came out blank where a widget should be.",
         REPORTER, ""),
        ("check_widget_errors",
         "A widget that printed an error message where its table should be.",
         ADMIN,
         "The error text is on the page and worth quoting upstream verbatim - "
         "it is the only thing anybody has to go on."),
        ("check_page_banners",
         "The template's section banners were left switched on.",
         REPORTER, ""),
    ]),
    ("Creative", [
        ("check_thumbnails",
         "A creative preview that did not render - an empty box where the ad "
         "should be. HTML5 creatives are skipped: a zip of markup has no still "
         "frame to show.",
         ADMIN,
         "Re-pulling sometimes fills them in, which tells you it was the "
         "render rather than the asset. If the same box is empty twice, the "
         "creative itself has no still frame stored."),
        ("check_blank_screenshots",
         "An ad screenshot cell with no screenshot in it.",
         ADMIN, ""),
        ("check_variant_preview_links",
         "A variant with no PREVIEW LINK. Separate from the screenshot check "
         "above and deliberately so: on a Social Mirror AI grid the preview is "
         "a link rather than a picture, so the thing that goes missing is the "
         "link and the repair is a different one. HTML5 creatives - the ones "
         "whose name ends in .zip - are skipped, because there is nothing to "
         "link to.",
         ADMIN,
         "Nothing on this end can supply the link - it is the ad's own URL and "
         "the grid either has it or does not. The variants without one are "
         "usually the ones that served nothing."),
        ("check_creative_names",
         "A creative row that does not say which creative it is.",
         BUYER,
         "Named at trafficking, so it is fixed there rather than on the report."),
        ("check_social_mirror_sizes",
         "A Social Mirror creative named with an ad size, which is a display "
         "name on a social ad.",
         BUYER,
         "A social ad has no size, so a name like 300x250 on one came off a "
         "display naming convention. Harmless to the numbers and visible to "
         "the client."),
    ]),
    ("Completion rates", [
        ("check_completion_rates",
         "A completion rate above 100%.",
         ADMIN, ""),
        ("check_zero_completion",
         "A completion widget sitting at 0% all the way down, which is a "
         "broken widget rather than a result.",
         ADMIN,
         "A whole column of zeroes is not a result anybody got. Treat it as a "
         "widget that did not report rather than a campaign nobody watched."),
        ("check_some_zero_completion",
         "One video, CTV or audio row at 0% among others that watched fine.",
         ADMIN, ""),
    ]),
    ("Names and labels", [
        ("check_geofence_names",
         "A geo-fencing row with no business name on it.",
         BUYER,
         "The fence is named where it was drawn. A row reading as coordinates "
         "or an id tells the client nothing about where their ads ran."),
        ("check_conversion_names",
         "A conversion named for a tag or an id rather than what the user "
         "did.",
         BUYER,
         "Rename it to the action - \"Called\", \"Form submitted\". The client "
         "is being asked to read the tag manager otherwise."),
        ("check_devices_known",
         "A row of the device breakout that is not an actual device.",
         ADMIN, ""),
        ("check_truncated_text",
         "Text cut off for want of space.",
         REPORTER, ""),
    ]),
    ("Sites", [
        ("check_site_ctr",
         "A site clicking at a rate a person would not - the usual sign of "
         "bot traffic on a placement.",
         BUYER,
         "The finding names the site and its rate. It is a placement to "
         "exclude rather than a number to correct, and it goes on before the "
         "next flight rather than on this report."),
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
                 "who": who, "how": how}
                for key, what, who, how in items if key in labels]
        if rows:
            out.append({"group": title, "checks": rows})
    return out


def unwritten() -> int:
    """How many checks still have nobody's fix written against them.

    On the page rather than in somebody's head: a column half filled in looks
    finished from a distance, and the blanks are the point of it.
    """
    return sum(1 for _t, items in FLAG_GROUPS for _k, _w, _o, how in items
               if not how)


def described() -> set[str]:
    return {key for _t, items in FLAG_GROUPS for key, _w, _o, _h in items}
