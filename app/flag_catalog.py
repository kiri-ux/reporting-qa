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

A test walks `CHECKS` and fails if anything is missing from this file. A check
nobody can find is a check that gets argued about from memory.

AND IT LIVES OUTSIDE `app/checks/` ON PURPOSE. The rules fingerprint is a hash
of every file in that folder, and anything whose hash changes puts every report
on the board in the queue to be judged again. This file is prose about the
rules, not a rule - a wording fix here should not cost seven hundred reports a
re-read.
"""
from __future__ import annotations

# (group title, [(check function name, what going wrong looks like)])
FLAG_GROUPS: list[tuple[str, list[tuple[str, str]]]] = [
    ("The numbers agree with each other", [
        ("check_headline_ctr",
         "The big CTR at the top does not match the impressions and clicks "
         "printed beside it."),
        ("check_row_math",
         "A row's own CTR does not match that row's own impressions and "
         "clicks."),
        ("check_line_items",
         "The line items do not add up to the campaign total above them."),
        ("check_creative",
         "The creative rows do not add up to the line item they belong to."),
        ("check_device",
         "The device breakout describes more impressions than were served."),
        ("check_social_placement_totals",
         "Facebook and Instagram placements add up to more than the platform "
         "tiles say."),
        ("check_store_visits",
         "The store visit figure does not agree with the table underneath it."),
        ("check_month_within_lifetime",
         "One month reports more than the whole campaign has ever delivered."),
        ("check_rate_ceiling",
         "A rate is above what that rate can be - a CTR over 5%, a completion "
         "rate over 100%."),
    ]),
    ("What the client bought", [
        ("check_products",
         "A product on the report that is not on the live orders, or a product "
         "they are paying for that is not on the report. Website Visitor ID "
         "and Additional Billing are never expected - they are billed line "
         "items with no widget."),
        ("check_strategy_categorized",
         "A strategy line that does not name the product it runs, so it cannot "
         "be checked against the order."),
        ("check_impression_pacing",
         "Delivery more than 50% off what the order asked for, either way."),
        ("check_pacing",
         "A full month's spend that does not look like a full month's budget."),
        ("check_lifetime_goal",
         "A finished campaign that did not deliver what it was sold."),
    ]),
    ("The right report for the right client", [
        ("check_client_data",
         "Data on the report that belongs to a different client."),
        ("check_client_matches_order",
         "The report is for a different client than the row it arrived in."),
        ("check_date_range",
         "The printed date range is not the period this report claims to "
         "cover."),
        ("check_market_logo",
         "Page one carries the reporting tool's own mark instead of the "
         "partner's or the client's."),
    ]),
    ("Widgets that should be there, and ones that should not", [
        ("check_required_widgets",
         "A product is on the report without the widget it owes - CTV with no "
         "completion rates, Mobile Conquesting with no fence breakout."),
        ("check_geofence_widget",
         "Geo-fenced Mobile Conquesting with no geo-fencing breakout behind "
         "it."),
        ("check_completion_present",
         "A video or audio product that never says how much of it got "
         "watched."),
        ("check_rogue_ctv",
         "A CTV widget on a report with no CTV on it - usually the template "
         "left switched on."),
        ("check_blank_pages",
         "A page that came out blank where a widget should be."),
        ("check_widget_errors",
         "A widget that printed an error message where its table should be."),
        ("check_page_banners",
         "The template's section banners were left switched on."),
    ]),
    ("Creative", [
        ("check_thumbnails",
         "A creative preview that did not render - an empty box where the ad "
         "should be. HTML5 creatives are skipped: a zip of markup has no still "
         "frame to show."),
        ("check_blank_screenshots",
         "An ad screenshot cell with no screenshot in it."),
        ("check_preview_links",
         "A creative variant with no link to look at it. HTML5 creatives - "
         "the ones whose name ends in .zip - are skipped, because there is "
         "nothing to link to."),
        ("check_creative_names",
         "A creative row that does not say which creative it is."),
        ("check_social_mirror_sizes",
         "A Social Mirror creative named with an ad size, which is a display "
         "name on a social ad."),
    ]),
    ("Completion rates", [
        ("check_completion_rates",
         "A completion rate above 100%."),
        ("check_zero_completion",
         "A completion widget sitting at 0% all the way down, which is a "
         "broken widget rather than a result."),
        ("check_some_zero_completion",
         "One video, CTV or audio row at 0% among others that watched fine."),
    ]),
    ("Names and labels", [
        ("check_geofence_names",
         "A geo-fencing row with no business name on it."),
        ("check_conversion_names",
         "A conversion named for a tag or an id rather than what the user "
         "did."),
        ("check_devices_known",
         "A row of the device breakout that is not an actual device."),
        ("check_truncated_text",
         "Text cut off for want of space."),
    ]),
    ("Sites", [
        ("check_site_ctr",
         "A site clicking at a rate a person would not - the usual sign of "
         "bot traffic on a placement."),
    ]),
]


def flags() -> list[dict]:
    """[{group, checks: [{label, what}]}] - the catalog, ready to render.

    The label comes from `CHECKS`, so the two can never drift into saying
    different things about the same rule.
    """
    from .checks.rules import CHECKS

    labels = {fn.__name__: label for fn, label in CHECKS}
    out = []
    for title, items in FLAG_GROUPS:
        rows = [{"key": key, "label": labels.get(key, key), "what": what}
                for key, what in items if key in labels]
        if rows:
            out.append({"group": title, "checks": rows})
    return out


def described() -> set[str]:
    return {key for _t, items in FLAG_GROUPS for key, _w in items}
